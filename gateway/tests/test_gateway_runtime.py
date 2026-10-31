import asyncio
import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as gateway


class TemporaryGatewayDatabase:
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in (
            ("DB_PATH", Path(directory.name) / "gateway.db"),
            ("DEVICE_CONFIGURATION_EXPORT_DIR", Path(directory.name) / "schemas"),
            ("device_metadata", {}), ("devices", {}),
            ("device_states", {}), ("device_wire_ids", {}),
            ("device_handler_tasks", set()), ("browser_clients", set()),
            ("last_device_heartbeat", {}), ("fresh_device_parameters", {}),
            ("last_device_telemetry", {}), ("last_parameter_telemetry", {}),
            ("last_device_probe", {}),
            ("pending_commands", {}), ("pending_device_connections", {}),
            ("pending_device_schemas", {}), ("deferred_device_schemas", {}),
            ("deferred_connection_automations", set()),
            ("database_restore_in_progress", False),
            ("GATEWAY_SESSION_STARTED_AT", None),
        ):
            patcher = patch.object(gateway, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class DatabaseConnectionTests(TemporaryGatewayDatabase, unittest.TestCase):
    def test_commits_and_closes_connection(self):
        with gateway.database_connection() as connection:
            connection.execute("CREATE TABLE values_for_test (value INTEGER)")
            connection.execute("INSERT INTO values_for_test VALUES (1)")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with gateway.database_connection() as reopened:
            self.assertEqual(
                reopened.execute("SELECT value FROM values_for_test").fetchone(),
                (1,),
            )

    def test_rolls_back_and_closes_connection_on_error(self):
        with gateway.database_connection() as connection:
            connection.execute("CREATE TABLE values_for_test (value INTEGER)")

        with self.assertRaisesRegex(RuntimeError, "abort"):
            with gateway.database_connection() as failed_connection:
                failed_connection.execute("INSERT INTO values_for_test VALUES (1)")
                raise RuntimeError("abort")

        with self.assertRaises(sqlite3.ProgrammingError):
            failed_connection.execute("SELECT 1")
        with gateway.database_connection() as reopened:
            self.assertEqual(
                reopened.execute("SELECT COUNT(*) FROM values_for_test").fetchone()[0],
                0,
            )

    def test_event_pagination_keeps_matching_count_and_escapes_search(self):
        with gateway.database_connection() as connection:
            connection.execute(
                "CREATE TABLE devices (device_id TEXT, display_name TEXT, "
                "name TEXT, schema_json TEXT)"
            )
            connection.execute(
                "CREATE TABLE device_events (id INTEGER PRIMARY KEY, "
                "device_id TEXT, event_type TEXT, event_value TEXT, timestamp TEXT)"
            )
            connection.executemany(
                "INSERT INTO device_events VALUES (?, 'lamp', 'parameter', ?, '2026-10-29')",
                [(1, "Яркость 50%"), (2, "Яркость 75%"), (3, "Другое событие")],
            )

        first, has_more, cursor, count = gateway.get_event_page(limit=1, search="ЯРКОСТЬ")
        self.assertEqual([event["id"] for event in first], [2])
        self.assertTrue(has_more)
        self.assertEqual(count, 2)
        second, has_more, cursor, count = gateway.get_event_page(
            limit=1, before_id=cursor, search="ЯРКОСТЬ"
        )
        self.assertEqual([event["id"] for event in second], [1])
        self.assertFalse(has_more)
        self.assertIsNone(cursor)
        self.assertEqual(count, 2)
        self.assertEqual(gateway.get_event_page(search="%")[3], 2)

    def test_restored_schema_and_ui_preferences_refresh_saved_export(self):
        gateway.init_db()
        metadata = {
            "name": "Lamp", "schema_version": 1,
            "schema": {"parameters": {"enabled": {
                "type": "boolean", "access": "read_write", "label": "Original"
            }}, "actions": {}},
        }
        gateway.save_device_metadata("lamp", metadata)
        updated = copy.deepcopy(metadata)
        updated["schema_version"] = 2
        updated["schema"]["parameters"]["enabled"]["label"] = "Updated"
        gateway.save_device_metadata("lamp", updated)
        export_path = gateway.DEVICE_CONFIGURATION_EXPORT_DIR / "lamp.json"

        success, _ = gateway.save_parameter_ui_preference("lamp", "enabled", "list")
        self.assertTrue(success)
        self.assertEqual(json.loads(export_path.read_text(encoding="utf-8"))["ui_preferences"], {"enabled": "list"})
        success, _ = gateway.save_parameter_ui_preference("lamp", "enabled", "schema")
        self.assertTrue(success)
        self.assertEqual(json.loads(export_path.read_text(encoding="utf-8"))["ui_preferences"], {})

        snapshot = next(item for item in gateway.get_schema_history("lamp") if item["schema_version"] == 1)
        success, _ = gateway.restore_device_schema("lamp", snapshot["id"])
        self.assertTrue(success)
        self.assertEqual(json.loads(export_path.read_text(encoding="utf-8"))["schema"], metadata["schema"])

    def test_optional_export_directory_error_is_reported_without_raising(self):
        with (
            patch.object(gateway, "device_configuration_export_document", return_value={}),
            patch.object(Path, "mkdir", side_effect=PermissionError("access denied")),
        ):
            self.assertFalse(gateway.write_device_configuration_export("lamp"))

    def test_boolean_to_integer_state_changes_create_events(self):
        gateway.init_db()
        for device_id, before, after in (
            ("on", True, 1), ("off", False, 0),
            ("json", {"nested": [True]}, {"nested": [1]}),
        ):
            gateway.save_device_state(device_id, {"mode": before})
            _, changed, _, _ = gateway.save_device_state(device_id, {"mode": after})
            self.assertTrue(changed)
            events, _, _, count = gateway.get_event_page(device_id=device_id)
            self.assertEqual(count, 1)
            recorded = json.loads(events[0]["event_value"])["value"]
            self.assertTrue(gateway.schema_values_match(recorded, after))

        gateway.save_device_state("number", {"value": 1})
        _, changed, _, _ = gateway.save_device_state("number", {"value": 1.0})
        self.assertFalse(changed)


class AutomationValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.program = {
            "version": 1,
            "trigger": {"type": "gateway_started"},
            "body": [{"type": "wait", "seconds": 0}],
        }

    def test_program_version_requires_integer(self):
        for version in (True, 1.0):
            program = copy.deepcopy(self.program)
            program["version"] = version
            self.assertIsNotNone(gateway.validate_automation_program(program))

    def test_invalid_utf8_config_is_rejected_without_raising(self):
        config = {
            "version": 2,
            "name": "invalid\ud800",
            "workspace": {"blocks": {}},
            "program": self.program,
        }
        self.assertIsNotNone(gateway.validate_automation_config(config))

    def test_extremely_large_wait_is_rejected_without_raising(self):
        self.program["body"][0]["seconds"] = 10 ** 1000
        self.assertIn("Пауза", gateway.validate_automation_program(self.program))

    def test_ordered_comparison_rejects_enum_on_either_side(self):
        enum_expression = {"type": "parameter", "device": "lamp", "parameter": "mode"}
        constant = {"type": "constant", "value": 1}
        metadata = {"lamp": {"schema": {"parameters": {
            "mode": {"type": "enum", "access": "read_only"}
        }}}}
        with patch.object(gateway, "device_metadata", metadata):
            for left, right in ((constant, enum_expression), (enum_expression, constant)):
                self.program["body"] = [{
                    "type": "if", "branches": [{
                        "condition": {"type": "compare", "operator": "lt", "left": left, "right": right},
                        "then": [],
                    }], "else": [],
                }]
                self.assertIsNotNone(gateway.validate_automation_program(self.program))

    async def test_enum_inequality_distinguishes_boolean_from_integer(self):
        metadata = {"lamp": {"schema": {"parameters": {
            "mode": {"type": "enum", "access": "read_only", "options": [
                {"label": "Boolean", "value": True}, {"label": "Integer", "value": 1}
            ]}
        }}}}
        expression = {
            "type": "compare", "operator": "ne",
            "left": {"type": "parameter", "device": "lamp", "parameter": "mode"},
            "right": {"type": "constant", "value": 1},
        }
        with (
            patch.object(gateway, "device_is_connected", return_value=True),
            patch.object(gateway, "device_metadata", metadata),
            patch.object(gateway, "device_states", {"lamp": {"mode": True}}),
            patch.object(gateway, "fresh_device_parameters", {"lamp": {"mode"}}),
            patch.object(gateway, "last_parameter_telemetry", {
                "lamp": {"mode": asyncio.get_running_loop().time()}
            }),
        ):
            self.assertTrue(gateway.evaluate_automation_expression(expression))
            expression["operator"] = "eq"
            self.assertFalse(gateway.evaluate_automation_expression(expression))


class AutomationTelemetryTests(TemporaryGatewayDatabase, unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_telemetry_does_not_read_automation_database(self):
        with patch.object(gateway, "database_connection", side_effect=AssertionError("unexpected DB read")):
            await gateway.evaluate_automations("lamp", {"mode": True}, {"mode": True})

    async def test_enum_type_changes_schedule_matching_automation(self):
        gateway.init_db()
        gateway.device_metadata["lamp"] = {"schema": {"parameters": {"mode": {
            "type": "enum", "access": "read_write", "options": [
                {"label": str(value), "value": value} for value in (True, False, 1, 0)
            ]
        }}}}
        config = {
            "version": 2, "name": "Changed enum", "workspace": {"blocks": {}},
            "program": {
                "version": 1,
                "trigger": {"type": "parameter_changed", "device": "lamp", "parameter": "mode"},
                "body": [{"type": "wait", "seconds": 0}],
            },
        }
        with patch.object(gateway, "schedule_parameter_automation_trigger") as schedule:
            for before, after in ((True, 1), (False, 0)):
                config["program"]["trigger"]["value"] = after
                success, message = gateway.save_automation("enum_changed", config)
                self.assertTrue(success, message)
                await gateway.evaluate_automations("lamp", {"mode": before}, {"mode": after})
                schedule.assert_called_once()
                self.assertIs(type(schedule.call_args.args[-1]), int)
                self.assertEqual(schedule.call_args.args[-1], after)
                schedule.reset_mock()

    async def test_command_ack_rejects_different_enum_type(self):
        gateway.init_db()
        schema = {"parameters": {"mode": {
            "label": "Mode", "type": "enum", "access": "read_write", "options": [
                {"label": "Boolean", "value": True}, {"label": "Integer", "value": 1}
            ]
        }}, "actions": {}}
        gateway.save_device_metadata("lamp", {
            "schema": schema, "schema_version": 1, "protocol_version": 2,
            "firmware_id": "lamp", "trust_policy": "allow",
        })

        async def messages():
            yield json.dumps({
                "type": "hello", "device": "lamp", "protocol_version": 2,
                "schema_version": 1, "schema": schema,
            })
            yield json.dumps({
                "type": "state", "device": "lamp", "data": {"parameters": {"mode": True}}
            })
            dispatched = await gateway.dispatch_device_command(
                "lamp", "mode", 1, requested_command_id="enum-command"
            )
            self.assertTrue(dispatched)
            yield json.dumps({
                "type": "command_result", "device": "lamp", "command_id": "enum-command",
                "success": True, "data": {"parameter": "mode", "value": True},
            })
            yield json.dumps({
                "type": "state", "device": "lamp", "data": {"parameters": {"mode": 1}}
            })

        class DeviceSocket:
            remote_address = ("test", 1)

            def __aiter__(self):
                return messages()

            async def send(self, message):
                pass

            async def close(self, **kwargs):
                pass

        with patch.object(gateway, "evaluate_automations", wraps=gateway.evaluate_automations) as evaluate:
            await gateway.handle_client(DeviceSocket())
        self.assertEqual(evaluate.await_count, 2)
        self.assertIs(type(evaluate.await_args.args[1]["mode"]), bool)
        self.assertIs(type(evaluate.await_args.args[2]["mode"]), int)
        with gateway.database_connection() as connection:
            status, error = connection.execute(
                "SELECT status, error FROM command_queue WHERE command_id = 'enum-command'"
            ).fetchone()
        self.assertEqual(status, "failed")
        self.assertIn("отличающееся от запрошенного", error)


if __name__ == "__main__":
    unittest.main()
