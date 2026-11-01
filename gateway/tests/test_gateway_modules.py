"""Runtime isolation and short-lived bulk commands across disconnect/restart."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Gateway


class RecordingSocket:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send(self, value):
        if self.fail:
            raise ConnectionError("connection lost while sending")
        self.messages.append(json.loads(value))


class RuntimeIsolationTests(unittest.TestCase):
    def test_new_instances_do_not_open_database_or_share_mutable_state(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "not-created.db"
            first = Gateway(database)
            second = Gateway(database)
            first.devices["lamp"] = object()
            first.device_states["lamp"] = {"led": True}
            first.automation_stop_requests.add("scenario")
            first.COMMAND_TIMEOUT_SECONDS = 0.01
            self.assertFalse(database.exists())
            self.assertEqual(second.devices, {})
            self.assertEqual(second.device_states, {})
            self.assertEqual(second.automation_stop_requests, set())
            self.assertEqual(second.COMMAND_TIMEOUT_SECONDS, 5)
            self.assertIsNot(first.management, second.management)
            self.assertEqual(first.DATABASE_BACKUP_DIR, database.parent / "backups")


class ManagementRestoreSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.browser = RecordingSocket()
        self.gateway.browser_clients.add(self.browser)

    def set_retention(self, days):
        with self.gateway.database_connection() as connection:
            connection.execute(
                "UPDATE gateway_settings SET value_json = ? WHERE setting_key = 'event_retention_days'",
                (json.dumps(days),),
            )

    def last_message(self, message_type):
        return next(message for message in reversed(self.browser.messages) if message["type"] == message_type)

    async def test_restore_broadcasts_restored_groups_and_retention_without_browser_reload(self):
        group_id = self.gateway.management.save_group(None, "Original group")
        self.set_retention(7)
        backup = self.gateway.create_database_backup()
        self.gateway.management.save_group(group_id, "Changed group")
        self.set_retention(30)
        success, _ = await self.gateway.restore_database_backup(backup["backup_id"])
        self.assertTrue(success)
        self.assertEqual(self.last_message("device_groups")["groups"][0]["name"], "Original group")
        self.assertEqual(self.last_message("gateway_settings")["event_retention_days"], 7)
        self.assertFalse(self.gateway.database_restore_in_progress)

    async def test_safety_recovery_also_resynchronizes_groups_and_retention(self):
        group_id = self.gateway.management.save_group(None, "Backup group")
        self.set_retention(7)
        backup = self.gateway.create_database_backup()
        self.gateway.management.save_group(group_id, "Current group")
        self.set_retention(30)
        restore_file = self.gateway.restore_database_file
        calls = 0

        def fail_target_then_recover(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated target restore failure")
            restore_file(path)

        with patch.object(self.gateway, "restore_database_file", side_effect=fail_target_then_recover):
            success, message = await self.gateway.restore_database_backup(backup["backup_id"])
        self.assertFalse(success)
        self.assertIn("previous database was recovered", message)
        self.assertEqual(calls, 2)
        self.assertEqual(self.last_message("device_groups")["groups"][0]["name"], "Current group")
        self.assertEqual(self.last_message("gateway_settings")["event_retention_days"], 30)
        self.assertFalse(self.gateway.database_restore_in_progress)


class TransientCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.gateway.save_device_metadata("lamp", {
            "schema": {
                "parameters": {"led": {"type": "boolean", "access": "read_write"}},
                "actions": {"identify": {"label": "Identify"}},
            },
            "schema_version": 1, "command_dedup": True,
        })
        self.gateway.devices["lamp"] = RecordingSocket()

    async def asyncTearDown(self):
        tasks = []
        for pending in list(self.gateway.pending_commands.values()):
            task = pending.get("timeout_task")
            if task is not None:
                task.cancel()
                tasks.append(task)
            future = pending.get("result_future")
            if future is not None and not future.done():
                future.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.gateway.management.shutdown()

    def stored_status(self, command_id):
        with self.gateway.database_connection() as connection:
            return connection.execute(
                "SELECT status FROM command_queue WHERE command_id = ?", (command_id,)
            ).fetchone()[0]

    async def test_parameter_and_action_cancel_on_disconnect_instead_of_queueing(self):
        for kind in ("parameter", "action"):
            with self.subTest(kind=kind):
                self.gateway.devices["lamp"] = RecordingSocket()
                command_id = "bulk-" + kind
                if kind == "parameter":
                    future = await self.gateway.dispatch_device_command(
                        "lamp", "led", False, requested_command_id=command_id,
                        wait_for_result=True, transient=True,
                    )
                else:
                    future = await self.gateway.dispatch_device_action(
                        "lamp", "identify", requested_command_id=command_id,
                        wait_for_result=True, transient=True,
                    )
                self.gateway.devices.pop("lamp")
                await self.gateway.finish_pending_commands_for_device(
                    "lamp", "unavailable", "Disconnected"
                )
                self.assertEqual((await future)["status"], "unavailable")
                self.assertNotIn(command_id, self.gateway.pending_commands)
                self.assertEqual(self.stored_status(command_id), "unavailable")
                self.gateway.devices["lamp"] = RecordingSocket()
                await self.gateway.deliver_queued_commands("lamp")
                self.assertEqual(self.gateway.devices["lamp"].messages, [])

    async def test_send_failure_resolves_bulk_command_without_retry_queue(self):
        self.gateway.devices["lamp"] = RecordingSocket(fail=True)
        future = await self.gateway.dispatch_device_command(
            "lamp", "led", False, requested_command_id="bulk-send-failed",
            wait_for_result=True, transient=True,
        )
        self.assertEqual((await future)["status"], "unavailable")
        self.assertFalse(self.gateway.pending_commands)
        self.assertEqual(self.stored_status("bulk-send-failed"), "unavailable")

    async def test_retry_limit_finishes_bulk_command_as_timeout(self):
        self.gateway.COMMAND_TIMEOUT_SECONDS = 0.002
        future = await self.gateway.dispatch_device_command(
            "lamp", "led", False, requested_command_id="bulk-timeout",
            wait_for_result=True, transient=True,
        )
        result = await asyncio.wait_for(future, timeout=1)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(self.gateway.pending_commands)
        self.assertEqual(self.stored_status("bulk-timeout"), "timeout")
        self.assertEqual(len(self.gateway.devices["lamp"].messages), self.gateway.COMMAND_RETRY_LIMIT)

    async def test_restart_drops_persisted_bulk_but_preserves_ordinary_queue(self):
        await self.gateway.dispatch_device_command(
            "lamp", "led", False, requested_command_id="bulk-before-crash",
            transient=True,
        )
        await self.gateway.dispatch_device_action(
            "lamp", "identify", requested_command_id="ordinary-before-crash",
        )
        restarted = Gateway(self.gateway.DB_PATH)
        restarted.init_db()
        restarted.load_pending_commands()
        self.assertNotIn("bulk-before-crash", restarted.pending_commands)
        self.assertIn("ordinary-before-crash", restarted.pending_commands)
        self.assertEqual(self.stored_status("bulk-before-crash"), "unavailable")
