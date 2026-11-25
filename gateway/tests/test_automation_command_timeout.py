"""Automation must observe the durable delivery result after slow finalization."""

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class AutomationCommandTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_timeout_finalization_keeps_the_actual_delivery_error(self):
        for statement in (
            {"type": "set_parameter", "device": "lamp", "parameter": "power",
             "value": {"type": "constant", "value": False}},
            {"type": "invoke_action", "device": "lamp", "action": "test_action", "arguments": {}},
        ):
            with self.subTest(statement=statement["type"]), TemporaryDirectory() as directory:
                gateway = Gateway(Path(directory) / "gateway.db")
                gateway.init_db()
                gateway.COMMAND_TIMEOUT_SECONDS = 0.08
                gateway.COMMAND_RETRY_LIMIT = 2
                metadata = {
                    "schema": {"parameters": {"power": {"type": "boolean", "access": "read_write"}},
                               "actions": {"test_action": {"arguments": {}}}},
                    "schema_version": 1, "protocol_version": 2,
                    "command_dedup": True, "command_replay_guard": True,
                }
                gateway.save_device_metadata("lamp", metadata)
                gateway.device_metadata["lamp"] = metadata
                device = AsyncMock()
                gateway.devices["lamp"] = device
                gateway.broadcast = AsyncMock()
                config = {
                    "version": 2, "name": "Timeout regression", "workspace": {"blocks": {}},
                    "program": {"version": 1, "trigger": {"type": "manual"}, "body": [statement]},
                }
                success, message = gateway.save_automation("slow-result", config)
                self.assertTrue(success, message)
                original_result = gateway.send_pending_command_result
                finalizers = []
                pending_results = []

                async def slow_finalization(pending, status, value, error):
                    finalizers.append(asyncio.current_task())
                    pending_results.append(pending)
                    # This exceeds the former 0.32s automation watchdog while
                    # retaining real dispatch, retry timers and queue writes.
                    await asyncio.sleep(0.5)
                    await original_result(pending, status, value, error)

                try:
                    with patch.object(gateway, "send_pending_command_result", slow_finalization):
                        self.assertTrue(gateway.start_automation_run("slow-result", config, "manual"))
                        await asyncio.wait_for(gateway.automation_tasks["slow-result"], 10)
                        await asyncio.gather(*finalizers)
                    history = gateway.get_automation_run_history("slow-result")
                    self.assertEqual(len(pending_results), 1)
                    pending = pending_results[0]
                    self.assertFalse(pending["result_future"].cancelled())
                    self.assertEqual(pending["result_future"].result()["status"], "timeout")
                    with gateway.database_connection() as connection:
                        status, encoded_result = connection.execute(
                            "SELECT status, result_json FROM command_queue WHERE command_id = ?",
                            (pending["command_id"],),
                        ).fetchone()
                    result = json.loads(encoded_result)
                    self.assertEqual(status, "timeout")
                    self.assertEqual(history[0]["status"], "failed")
                    self.assertEqual(history[0]["error"], result["error"])
                    self.assertEqual(history[0]["steps"][0]["error"], result["error"])
                    self.assertEqual(device.send.await_count, gateway.COMMAND_RETRY_LIMIT)
                    self.assertFalse(gateway.pending_commands)
                    self.assertFalse(gateway.automation_tasks)
                finally:
                    tasks = set(gateway.automation_tasks.values()) | set(finalizers)
                    tasks.update(pending["timeout_task"] for pending in gateway.pending_commands.values()
                                 if pending.get("timeout_task") is not None)
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
