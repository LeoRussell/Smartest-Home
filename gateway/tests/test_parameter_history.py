import asyncio
import copy
import json
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway
from history import HISTORY_MAX_SAMPLES, ParameterHistory


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class ParameterHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.now = 1_800_000_000.0
        clock = patch("history.time.time", return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.schema = {
            "parameters": {
                "temperature": {"type": "number", "access": "read_only", "unit": "°C"},
                "enabled": {"type": "boolean", "access": "read_write"},
                "speed": {"type": "integer", "access": "read_write", "min": 0, "max": 100},
                "private": {"type": "number", "access": "read_only", "history": False},
                "note": {"type": "string", "access": "read_only"},
            }, "actions": {},
        }
        self.metadata = {"schema": self.schema, "name": "Sensor", "schema_version": 1,
                         "protocol_version": 2, "firmware_version": "test", "trust_policy": "allow"}
        self.gateway.save_device_metadata("sensor", self.metadata)
        self.gateway.device_metadata["sensor"] = self.metadata
        self.history = self.gateway.history
        self.browser = Socket()

    def rows(self):
        with self.gateway.database_connection() as connection:
            return connection.execute("SELECT parameter_id, sampled_at, value FROM parameter_history ORDER BY id").fetchall()

    def test_numeric_sampling_boolean_edges_and_echo_deduplication(self):
        self.assertEqual(self.history.record("sensor", {"temperature": 20, "enabled": False}), 2)
        self.clock.return_value += 1
        self.assertEqual(self.history.record("sensor", {"temperature": 21, "enabled": True}), 1)
        self.assertEqual(self.history.record("sensor", {"enabled": True}), 0)
        self.clock.return_value += 30
        self.assertEqual(self.history.record("sensor", {"temperature": 21, "enabled": True}), 2)
        self.assertEqual(len(self.rows()), 5)

    def test_restart_preserves_sampling_boundary(self):
        self.history.record("sensor", {"temperature": 20})
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.clock.return_value += 1
        self.assertEqual(reopened.history.record("sensor", {"temperature": 22}), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_only_valid_actual_schema_values_are_recorded(self):
        self.assertEqual(self.history.record("sensor", {
            "private": 30, "note": "hello", "unknown": 1, "enabled": 1,
            "temperature": float("inf"), "speed": -1,
        }), 0)
        self.assertEqual(self.history.record("missing", {"temperature": 1}), 0)
        self.assertEqual(self.rows(), [])

    def test_settings_disable_and_survive_reopen(self):
        self.history.save_settings({"enabled": False, "retention_days": 7, "interval_seconds": 10})
        self.assertEqual(self.history.record("sensor", {"temperature": 20}), 0)
        reopened = ParameterHistory(self.gateway)
        self.assertEqual(reopened.settings()["enabled"], False)
        self.assertEqual(reopened.settings()["retention_days"], 7)
        self.assertEqual(reopened.settings()["interval_seconds"], 10)
        for message in (
            {"enabled": 1, "retention_days": 30, "interval_seconds": 30},
            {"enabled": True, "retention_days": 0, "interval_seconds": 30},
            {"enabled": True, "retention_days": 366, "interval_seconds": 30},
            {"enabled": True, "retention_days": 30, "interval_seconds": False},
        ):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.history.save_settings(message)

    def test_schema_history_contract(self):
        for option in (True, False, {"enabled": True}, {"interval_seconds": 10}, {"enabled": False, "interval_seconds": 1}):
            schema = copy.deepcopy(self.schema)
            schema["parameters"]["temperature"]["history"] = option
            self.assertTrue(self.gateway.validate_schema(schema), option)
        for option in (None, 1, {}, {"unknown": True}, {"enabled": 0}, {"interval_seconds": True}, {"interval_seconds": 0}):
            schema = copy.deepcopy(self.schema)
            schema["parameters"]["temperature"]["history"] = option
            self.assertFalse(self.gateway.validate_schema(schema), option)
        self.schema["parameters"]["note"]["history"] = True
        self.assertFalse(self.gateway.validate_schema(self.schema))
        self.schema["parameters"]["note"]["history"] = False
        self.assertTrue(self.gateway.validate_schema(self.schema))

    def test_downsampling_preserves_extrema_count_and_boolean_last_value(self):
        self.history.save_settings({"enabled": True, "retention_days": 30, "interval_seconds": 1})
        for index in range(120):
            self.history.record("sensor", {"temperature": index}, sampled_at=self.now - 3600 + index * 30 + 1)
        points = self.history.query({"device": "sensor", "parameter": "temperature", "range": "hour", "max_points": 20})["points"]
        self.assertLessEqual(len(points), 20)
        self.assertEqual(sum(point["count"] for point in points), 120)
        self.assertEqual(min(point["min"] for point in points), 0)
        self.assertEqual(max(point["max"] for point in points), 119)
        for index, value in enumerate((False, True, False)):
            self.history.record("sensor", {"enabled": value}, sampled_at=self.now - 3 + index)
        point = self.history.query({"device": "sensor", "parameter": "enabled", "range": "hour", "max_points": 10})["points"][-1]
        self.assertEqual((point["value"], point["min"], point["max"], point["count"]), (0, 0, 1, 3))

    def test_schema_unit_changes_do_not_mix_series(self):
        self.history.record("sensor", {"temperature": 20})
        self.schema["parameters"]["temperature"]["unit"] = "°F"
        self.clock.return_value += 1
        self.history.record("sensor", {"temperature": 68})
        response = self.history.query({"device": "sensor", "parameter": "temperature"})
        self.assertEqual(response["unit"], "°F")
        self.assertEqual([point["value"] for point in response["points"]], [68])

    def test_boolean_bucket_uses_latest_timestamp_after_clock_moves_backwards(self):
        self.history.save_settings({"enabled": True, "retention_days": 30, "interval_seconds": 1})
        self.history.record("sensor", {"enabled": False}, sampled_at=self.now - 20)
        self.history.record("sensor", {"enabled": True}, sampled_at=self.now - 5)
        # A later insertion can carry an earlier timestamp after a clock correction.
        self.history.record("sensor", {"enabled": False}, sampled_at=self.now - 15)
        request = {"device": "sensor", "parameter": "enabled", "range": "hour", "max_points": 10}
        point = self.history.query(request)["points"][-1]
        self.assertEqual(point["timestamp"], self.history.iso(self.now - 5))
        self.assertEqual((point["value"], point["min"], point["max"], point["count"]), (1, 0, 1, 3))
        # For an equal timestamp the later observation wins deterministically.
        self.history.record("sensor", {"enabled": False}, sampled_at=self.now - 5)
        point = self.history.query(request)["points"][-1]
        self.assertEqual((point["value"], point["count"]), (0, 4))

    def test_extreme_finite_values_do_not_produce_invalid_json_averages(self):
        self.history.record("sensor", {"temperature": 1e308}, sampled_at=self.now - 60)
        self.history.record("sensor", {"temperature": 1e308}, sampled_at=self.now - 30)
        response = self.history.query({"device": "sensor", "parameter": "temperature", "max_points": 10})
        self.assertEqual(response["points"][0]["value"], 1e308)
        json.dumps(response, allow_nan=False)

    def test_retention_and_hard_row_cap(self):
        self.history.record("sensor", {"temperature": 1}, sampled_at=self.now - 31 * 86400)
        self.history.cleanup(force=True)
        self.assertEqual(self.rows(), [])
        with patch("history.HISTORY_MAX_SAMPLES", 3):
            self.history.cleanup(force=True)
            for index in range(6):
                self.history.record("sensor", {"enabled": bool(index % 2)}, sampled_at=self.now + index)
            self.assertEqual(len(self.rows()), 3)
            self.assertEqual(self.rows()[0][1], self.now + 3)

    def test_renaming_device_keeps_measurements(self):
        self.history.record("sensor", {"temperature": 20})
        success, _ = self.gateway.update_device_identity("sensor", "renamed", "Renamed")
        self.assertTrue(success)
        response = self.history.query({"device": "renamed", "parameter": "temperature"})
        self.assertEqual(len(response["points"]), 1)
        self.assertEqual(self.gateway.get_database_stats()["history_sample_count"], 1)

    async def test_protocol_queries_errors_and_settings(self):
        self.assertFalse(await self.history.handle_browser_message(self.browser, {"action": []}))
        self.history.record("sensor", {"temperature": 20})
        await self.gateway.handle_browser_message(self.browser, {
            "action": "get_parameter_history", "device": "sensor", "parameter": "temperature", "request_id": "chart-1",
        })
        result = self.browser.messages[-1]
        self.assertTrue(result["success"])
        self.assertEqual((result["type"], result["request_id"]), ("parameter_history", "chart-1"))
        self.assertEqual(len(result["points"]), 1)
        for fields in ({"range": []}, {"max_points": True}, {"max_points": 0}, {"max_points": -1},
                       {"max_points": 501}, {"parameter": "missing"}, {"device": "' OR 1=1"}):
            await self.gateway.handle_browser_message(self.browser, {
                "action": "get_parameter_history", "device": "sensor", "parameter": "temperature", "request_id": "bad", **fields,
            })
            self.assertFalse(self.browser.messages[-1]["success"])
        await self.gateway.handle_browser_message(self.browser, {
            "action": "set_history_settings", "enabled": True, "retention_days": 14,
            "interval_seconds": 60, "request_id": "save-1",
        })
        result = self.browser.messages[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["type"], "history_settings_result")
        self.assertEqual(result["retention_days"], 14)
        self.assertEqual(result["max_samples"], HISTORY_MAX_SAMPLES)

    async def test_browser_hello_announces_supported_features_before_state(self):
        async def frames():
            yield json.dumps({"type": "browser_hello"})

        class BrowserSocket(Socket):
            remote_address = ("history-browser-test", 0)

            def __aiter__(self):
                return frames()

        browser = BrowserSocket()
        await self.gateway.handle_client(browser)
        capabilities = browser.messages[0]
        self.assertEqual(capabilities["type"], "gateway_capabilities")
        self.assertEqual(capabilities["protocol_version"], 1)
        self.assertIn("parameter_history", capabilities["features"])
        self.assertIn("automation_progress", capabilities["features"])
        self.assertTrue(any(message["type"] == "history_settings" for message in browser.messages))

    async def test_browser_errors_echo_action_and_request_id(self):
        await self.gateway.handle_browser_message(self.browser, {
            "action": "future_history_action", "request_id": "unsupported-chart",
        })
        error = self.browser.messages[-1]
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["code"], "unsupported_action")
        self.assertEqual(error["request_id"], "unsupported-chart")
        self.assertEqual(error["action"], "future_history_action")
        self.gateway.database_restore_in_progress = True
        try:
            await self.gateway.handle_browser_message(self.browser, {
                "action": "get_parameter_history", "request_id": "restore-chart",
            })
        finally:
            self.gateway.database_restore_in_progress = False
        error = self.browser.messages[-1]
        self.assertEqual(error["code"], "database_restoring")
        self.assertEqual(error["request_id"], "restore-chart")
        self.assertEqual(error["action"], "get_parameter_history")

    async def test_unexpected_history_error_replies_without_internal_details(self):
        with patch.object(self.history, "query", side_effect=RuntimeError("private diagnostic detail")):
            await self.gateway.handle_browser_message(self.browser, {
                "action": "get_parameter_history", "device": "sensor", "parameter": "temperature",
                "request_id": "internal-chart",
            })
        result = self.browser.messages[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["request_id"], "internal-chart")
        self.assertEqual(result["code"], "history_internal_error")
        self.assertNotIn("private diagnostic detail", result["message"])
        self.assertTrue(await self.history.handle_browser_message(self.browser, {"action": "get_history_settings"}))
        self.assertTrue(self.browser.messages[-1]["enabled"])

    async def test_backup_restores_measurements_settings_and_sampling_cache(self):
        self.history.save_settings({"enabled": True, "retention_days": 14, "interval_seconds": 10})
        self.history.record("sensor", {"temperature": 20})
        backup = self.gateway.create_database_backup()
        self.history.save_settings({"enabled": False, "retention_days": 1, "interval_seconds": 1})
        success, message = await self.gateway.restore_database_backup(backup["backup_id"])
        self.assertTrue(success, message)
        restored = self.gateway.history
        self.assertIsNot(restored, self.history)
        self.assertTrue(restored.settings()["enabled"])
        self.assertEqual(restored.settings()["retention_days"], 14)
        self.assertEqual(restored.sample_count, 1)
        self.assertEqual(restored.record("sensor", {"temperature": 20}), 0)
        self.clock.return_value += 11
        self.assertEqual(restored.record("sensor", {"temperature": 21}), 1)
        self.assertEqual((restored.sample_count, len(self.rows())), (2, 2))

    async def test_history_query_keeps_event_loop_responsive_and_rejects_restore_race(self):
        self.history.record("sensor", {"temperature": 20})
        started, release = threading.Event(), threading.Event()
        query = self.history.query

        def delayed_query(message, *, settings=None):
            started.set()
            release.wait(timeout=2)
            return query(message, settings=settings)

        request = {"action": "get_parameter_history", "device": "sensor", "parameter": "temperature"}
        with patch.object(self.history, "query", side_effect=delayed_query):
            task = asyncio.create_task(self.history.handle_browser_message(self.browser, request))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(task.done(), "A history read blocked the event loop")
                self.gateway.database_restore_in_progress = True
            finally:
                release.set()
                await task
                self.gateway.database_restore_in_progress = False
        self.assertFalse(self.browser.messages[-1]["success"])
        self.assertIn("database changed", self.browser.messages[-1]["message"])
        self.gateway.database_restore_in_progress = True
        try:
            with patch.object(self.history, "query", side_effect=AssertionError("Read during restore")):
                await self.history.handle_browser_message(self.browser, request)
        finally:
            self.gateway.database_restore_in_progress = False
        self.assertFalse(self.browser.messages[-1]["success"])

    async def test_old_backup_without_history_table_and_setting_migrates(self):
        backup = self.gateway.create_database_backup()
        backup_path = self.gateway.DATABASE_BACKUP_DIR / backup["backup_id"]
        with closing(sqlite3.connect(backup_path)) as connection:
            connection.execute("DROP TABLE parameter_history")
            connection.execute("DELETE FROM gateway_settings WHERE setting_key = 'parameter_history'")
            connection.commit()
        self.gateway.validate_database_backup(backup_path)
        success, message = await self.gateway.restore_database_backup(backup["backup_id"])
        self.assertTrue(success, message)
        self.assertEqual(self.gateway.history.settings()["retention_days"], 30)
        self.assertEqual(self.gateway.history.record("sensor", {"temperature": 20}), 1)
        self.assertEqual(self.gateway.get_database_stats()["history_sample_count"], 1)

    async def test_device_telemetry_and_ack_record_actual_values_without_echo_duplicates(self):
        gateway, schema = self.gateway, self.schema

        async def frames():
            yield json.dumps({"type": "hello", "device": "sensor", "protocol_version": 2,
                              "schema_version": 1, "schema": schema, "name": "Sensor", "firmware_version": "test"})
            yield json.dumps({"type": "state", "device": "sensor",
                              "data": {"parameters": {"temperature": 20, "enabled": False}}})
            self.assertTrue(await gateway.dispatch_device_command(
                "sensor", "enabled", True, requested_command_id="history-command"
            ))
            # Merely dispatching an optimistic desired value must not add history.
            self.assertEqual([row[2] for row in self.rows() if row[0] == "enabled"], [0])
            yield json.dumps({"type": "command_result", "device": "sensor", "command_id": "history-command",
                              "success": True, "data": {"parameter": "enabled", "value": True}})
            yield json.dumps({"type": "state", "device": "sensor", "data": {"parameters": {"enabled": True}}})

        class DeviceSocket:
            remote_address = ("history-test", 0)

            def __aiter__(self):
                return frames()

            async def send(self, payload):
                pass

            async def close(self, **kwargs):
                pass

        await gateway.handle_client(DeviceSocket())
        self.assertEqual([row[2] for row in self.rows() if row[0] == "enabled"], [0, 1])
        self.assertEqual([row[2] for row in self.rows() if row[0] == "temperature"], [20])


if __name__ == "__main__":
    unittest.main()
