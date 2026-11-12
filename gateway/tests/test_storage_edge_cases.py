"""Persistence regressions for transaction failures and retained device facts."""

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class StorageEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()

    def metadata(self, **features):
        return {
            "name": "Sensor", "schema_version": 1, "protocol_version": 2,
            "trust_policy": "allow", "firmware_id": "sensor",
            "schema": {"parameters": {
                "first": {"type": "number", "access": "read_only"},
                "second": {"type": "number", "access": "read_only"},
            }, "actions": {}},
            **features,
        }

    def test_replay_guard_survives_reopen_and_requires_deduplication(self):
        self.gateway.save_device_metadata("sensor", self.metadata(
            command_dedup=True, command_replay_guard=True,
        ))
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.assertTrue(reopened.device_supports_command_replay_guard("sensor"))
        self.assertTrue(reopened.load_device_metadata("sensor")["command_replay_guard"])
        self.assertTrue(reopened.get_all_devices()[0]["command_replay_guard"])
        reopened.save_device_metadata("sensor", self.metadata(
            command_dedup=False, command_replay_guard=True,
        ))
        self.assertFalse(reopened.device_supports_command_replay_guard("sensor"))
        reopened.save_device_metadata("sensor", self.metadata(command_dedup=True))
        self.assertFalse(reopened.device_supports_command_replay_guard("sensor"))

    def test_old_database_does_not_gain_unsupported_replay_capability(self):
        # A database created before the guard exists can still claim the older,
        # bounded-cache feature. Migration must not promise stronger semantics.
        with self.gateway.database_connection() as connection:
            connection.execute("ALTER TABLE devices DROP COLUMN command_replay_guard")
            connection.execute("INSERT INTO devices(device_id, command_dedup) VALUES ('older', 1)")
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.assertTrue(reopened.device_supports_command_dedup("older"))
        self.assertFalse(reopened.device_supports_command_replay_guard("older"))
        self.assertFalse(reopened.get_all_devices()[0]["command_replay_guard"])

    def test_event_retention_keeps_ever_connected_device_count(self):
        self.gateway.save_device_metadata("sensor", self.metadata())
        self.gateway.save_device_event("sensor", "connection", "Connected", "2000-01-01T00:00:00+00:00")
        self.gateway.set_device_trust_policy("rejected", "deny")
        with self.gateway.database_connection() as connection:
            connection.execute(
                "UPDATE gateway_settings SET value_json = '1' WHERE setting_key = 'event_retention_days'"
            )
        self.assertEqual(self.gateway.get_database_stats()["ever_connected_device_count"], 1)
        self.assertEqual(self.gateway.management.cleanup_events(), 1)
        self.assertEqual(self.gateway.get_database_stats()["event_count"], 0)
        self.assertEqual(self.gateway.get_database_stats()["ever_connected_device_count"], 1)

    def test_failed_sample_batch_can_be_recorded_again_without_waiting(self):
        self.gateway.save_device_metadata("sensor", self.metadata())
        history = self.gateway.history
        now = time.time()
        history.record("sensor", {"first": 1}, sampled_at=now - 60)
        previous_cache = dict(history.recent)
        previous_count = history.sample_count
        with self.gateway.database_connection() as connection:
            connection.execute("""
                CREATE TRIGGER fail_second_sample BEFORE INSERT ON parameter_history
                WHEN NEW.parameter_id = 'second'
                BEGIN SELECT RAISE(ABORT, 'injected sample failure'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected sample failure"):
            history.record("sensor", {"first": 2, "second": 3}, sampled_at=now)
        self.assertEqual(dict(history.recent), previous_cache)
        self.assertEqual(history.sample_count, previous_count)
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM parameter_history").fetchone()[0], 1)
            connection.execute("DROP TRIGGER fail_second_sample")
        self.assertEqual(history.record("sensor", {"first": 2, "second": 3}, sampled_at=now), 2)
        self.assertEqual(history.sample_count, previous_count + 2)

    def test_recovery_rejects_command_ids_with_embedded_nul(self):
        command_id = "before\0after"
        operation = {"kind": "parameter", "name": "first", "value": 1}
        payload = {"type": "command", "device": "sensor", "data": {
            "command": "set", "command_id": command_id, "parameter": "first", "value": 1,
        }}
        with self.gateway.database_connection() as connection:
            connection.execute("""
                INSERT INTO command_queue(command_id, device_id, operation_json, payload_json,
                    status, attempts, created_at, updated_at)
                VALUES (?, 'sensor', ?, ?, 'queued', 1, '2000-01-01', '2000-01-01')
            """, (command_id, json.dumps(operation), json.dumps(payload)))
        self.gateway.load_pending_commands()
        self.assertFalse(self.gateway.pending_commands)
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM command_queue").fetchone()[0], "failed")

    async def test_failed_restore_does_not_resurrect_cancelled_manual_commands(self):
        operation = {"kind": "parameter", "name": "first", "value": 1}
        payload = {"type": "command", "device": "sensor", "data": {
            "command": "set", "command_id": "restore-interrupted", "parameter": "first", "value": 1,
        }}
        with self.gateway.database_connection() as connection:
            connection.execute("""
                INSERT INTO command_queue(command_id, device_id, operation_json, payload_json,
                    status, attempts, created_at, updated_at)
                VALUES ('restore-interrupted', 'sensor', ?, ?, 'sent', 1, '2000-01-01', '2000-01-01')
            """, (json.dumps(operation), json.dumps(payload)))
        self.gateway.load_pending_commands()
        self.assertIn("restore-interrupted", self.gateway.pending_commands)
        backup = self.gateway.create_database_backup()
        with patch.object(self.gateway.connections, "close_all", side_effect=RuntimeError("injected shutdown failure")):
            success, message = await self.gateway.restore_database_backup(backup["backup_id"])
        self.assertFalse(success)
        self.assertIn("Restore could not start", message)
        self.assertFalse(self.gateway.database_restore_in_progress)
        self.assertFalse(self.gateway.pending_commands)
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        reopened.load_pending_commands()
        self.assertFalse(reopened.pending_commands)
        with reopened.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM command_queue").fetchone()[0], "unavailable")


if __name__ == "__main__":
    unittest.main()
