"""CLI termination cleans owned work without depending on asyncio.run teardown."""

import asyncio
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway, run_until_shutdown


class ShutdownLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_runs_service_cleanup_and_removes_its_handler(self):
        loop = asyncio.get_running_loop()
        started, cleaned = asyncio.Event(), asyncio.Event()
        callback = None

        def install(signum, function):
            nonlocal callback
            self.assertEqual(signum, signal.SIGTERM)
            callback = function

        async def service():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("runtime.signal.getsignal", return_value=signal.SIG_DFL), patch.object(loop, "add_signal_handler", side_effect=install), patch.object(loop, "remove_signal_handler") as remove:
            task = asyncio.create_task(run_until_shutdown(service()))
            await asyncio.wait_for(started.wait(), 1)
            callback()
            await asyncio.wait_for(task, 1)
            self.assertTrue(cleaned.is_set())
            remove.assert_called_once_with(signal.SIGTERM)

    async def test_external_task_cancellation_still_propagates(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        async def service():
            started.set()
            await asyncio.Event().wait()

        with patch("runtime.signal.getsignal", return_value=signal.SIG_DFL), patch.object(loop, "add_signal_handler"), patch.object(loop, "remove_signal_handler"):
            task = asyncio.create_task(run_until_shutdown(service()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_windows_fallback_and_existing_handlers_are_respected(self):
        loop = asyncio.get_running_loop()

        async def result():
            return 42

        with patch("runtime.signal.getsignal", return_value=signal.SIG_DFL), patch.object(loop, "add_signal_handler", side_effect=NotImplementedError), patch.object(loop, "remove_signal_handler") as remove:
            self.assertEqual(await run_until_shutdown(result()), 42)
            remove.assert_not_called()
        with patch("runtime.signal.getsignal", return_value=lambda *args: None), patch.object(loop, "add_signal_handler") as install:
            self.assertEqual(await run_until_shutdown(result()), 42)
            install.assert_not_called()

    async def test_gateway_run_finishes_owned_tasks_and_preserves_only_manual_delivery(self):
        with TemporaryDirectory() as temporary:
            gateway = Gateway(Path(temporary) / "gateway.db")
            gateway.HOST, gateway.PORT = "127.0.0.1", 0
            owned = []
            futures = {}

            async def console():
                gateway.save_device_metadata("lamp", {"schema": {"parameters": {
                    "power": {"type": "boolean", "access": "read_write"},
                }, "actions": {}}, "schema_version": 1})
                config = {"version": 2, "name": "Waiting", "workspace": {"blocks": {}},
                          "program": {"version": 1, "trigger": {"type": "manual"},
                                      "body": [{"type": "wait", "seconds": 30}]}}
                self.assertTrue(gateway.save_automation("active", config)[0])
                self.assertTrue(gateway.start_automation_run("active", config, "manual"))
                for _ in range(20):
                    if gateway.automation_waiting_tasks:
                        break
                    await asyncio.sleep(0)
                self.assertTrue(gateway.automation_waiting_tasks)
                owned.extend(gateway.automation_tasks.values())
                trigger = asyncio.create_task(asyncio.sleep(30))
                gateway.automation_trigger_tasks["delayed"] = trigger
                gateway.automation_trigger_devices["delayed"] = "lamp"
                owned.append(trigger)
                for command_id, automation in (("manual", None), ("automatic", "active")):
                    future = asyncio.get_running_loop().create_future()
                    futures[command_id] = future
                    timer = asyncio.create_task(asyncio.sleep(30))
                    owned.append(timer)
                    pending = {
                        "command_id": command_id, "device": "lamp", "browser": None,
                        "operation": {"kind": "parameter", "name": "power", "value": False,
                                      "automation_id": automation},
                        "payload": {}, "attempts": 1, "status": "sent",
                        "timeout_task": timer, "automation_id": automation, "result_future": future,
                    }
                    gateway.create_command_queue_record(pending)
                    gateway.pending_commands[command_id] = pending

            with patch("runtime.sys.stdin", SimpleNamespace(isatty=lambda: True)), patch.object(gateway, "console_loop", side_effect=console):
                await asyncio.wait_for(gateway.run(), 3)
            self.assertTrue(gateway.shutting_down)
            self.assertTrue(all(task.done() for task in owned))
            self.assertEqual(gateway.automation_tasks, {})
            self.assertEqual(gateway.automation_trigger_tasks, {})
            self.assertEqual(set(gateway.pending_commands), {"manual"})
            self.assertEqual(futures["manual"].result()["status"], "unavailable")
            self.assertEqual(futures["automatic"].result()["status"], "unavailable")
            with gateway.database_connection() as connection:
                statuses = dict(connection.execute("SELECT command_id, status FROM command_queue"))
                run_status = connection.execute("SELECT status FROM automation_runs WHERE automation_id = 'active'").fetchone()[0]
            self.assertEqual(statuses, {"manual": "queued", "automatic": "unavailable"})
            self.assertEqual(run_status, "interrupted")
            self.assertFalse(gateway.start_automation_run("active", config={}, reason="late"))

    async def test_shutdown_marks_a_run_interrupted_even_before_task_first_starts(self):
        with TemporaryDirectory() as temporary:
            gateway = Gateway(Path(temporary) / "gateway.db")
            gateway.HOST, gateway.PORT = "127.0.0.1", 0

            async def console():
                config = {"version": 2, "name": "Not entered yet", "workspace": {"blocks": {}},
                          "program": {"version": 1, "trigger": {"type": "manual"},
                                      "body": [{"type": "wait", "seconds": 30}]}}
                self.assertTrue(gateway.save_automation("not-entered", config)[0])
                self.assertTrue(gateway.start_automation_run("not-entered", config, "manual"))
                # Return without yielding: the new task has not entered its finally.

            with patch("runtime.sys.stdin", SimpleNamespace(isatty=lambda: True)), patch.object(gateway, "console_loop", side_effect=console):
                await gateway.run()
            with gateway.database_connection() as connection:
                run = connection.execute("SELECT status, finished_at FROM automation_runs").fetchone()
                scenario = connection.execute("SELECT run_status, last_run_status FROM automations").fetchone()
            self.assertEqual(run[0], "interrupted")
            self.assertIsNotNone(run[1])
            self.assertEqual(scenario, ("idle", "interrupted"))
            self.assertIsNone(gateway.automation_progress_snapshot("not-entered"))


if __name__ == "__main__":
    unittest.main()
