import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as gateway
from device_configuration import DRIVERS, validate_device_configuration


class CapturingSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class ConfigurationDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = {
            "DB_PATH": Path(directory.name) / "gateway.db",
            "DEVICE_CONFIGURATION_EXPORT_DIR": Path(directory.name) / "schemas",
            "device_metadata": {}, "device_states": {}, "devices": {},
            "device_wire_ids": {}, "browser_clients": set(),
            "pending_device_schemas": {}, "deferred_device_schemas": {},
            "last_device_heartbeat": {}, "fresh_device_parameters": {},
            "last_device_telemetry": {}, "last_parameter_telemetry": {},
            "last_device_probe": {},
            "configuration_apply_tasks": {}, "database_restore_in_progress": False,
            "configuration_confirmed_devices": set(),
            "deferred_connection_automations": set(), "pending_commands": {},
            "GATEWAY_SESSION_STARTED_AT": None,
        }
        for name, value in settings.items():
            patcher = patch.object(gateway, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gateway.init_db()
        self.configuration = {"format_version": 1, "components": [{
            "id": "lamp", "driver": "gpio_output", "label": "Lamp",
            "parameter_id": "enabled", "parameter_label": "Enabled",
            "pin": 4, "active_high": True, "initial_value": False,
        }]}
        _, empty_schema, _ = validate_device_configuration(
            {"format_version": 1, "components": []}, list(DRIVERS)
        )
        gateway.save_device_metadata("universal", {
            "schema": empty_schema, "schema_version": 1,
            "device_profile": "universal", "board_profile": "esp32-s3-devkitc-1",
            "driver_ids": list(DRIVERS), "firmware_id": "universal",
        })
        gateway.device_metadata.update({
            "lamp": {"schema": {"parameters": {"enabled": {
                "type": "boolean", "access": "read_write", "label": "Lamp"
            }}, "actions": {}}},
            "sensor": {"schema": {"parameters": {"reading": {
                "type": "number", "access": "read_only", "min": 0, "max": 100,
                "label": "Reading"
            }}, "actions": {}}},
        })

    async def asyncTearDown(self):
        tasks = [gateway.cancel_configuration_apply(device) for device in list(gateway.configuration_apply_tasks)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def connect(self, device_id):
        socket = CapturingSocket()
        gateway.devices[device_id] = socket
        gateway.last_device_heartbeat[device_id] = asyncio.get_running_loop().time()
        return socket

    async def request(self, action, **data):
        browser = CapturingSocket()
        await gateway.handle_browser_message(browser, {"action": action, **data})
        self.assertTrue(browser.messages)
        return browser.messages[-1]

    def program(self, body, trigger=None):
        return {
            "version": 2, "name": "Diagnostic test", "workspace": {"blocks": {}},
            "program": {
                "version": 1, "trigger": trigger or {"type": "gateway_started"},
                "body": body,
            },
        }

    async def test_configuration_validation_returns_field_paths_without_saving(self):
        invalid = copy.deepcopy(self.configuration)
        second = copy.deepcopy(invalid["components"][0])
        second.update({"id": "other", "parameter_id": "other_enabled"})
        invalid["components"].append(second)
        response = await self.request(
            "validate_device_configuration", device="universal",
            configuration=invalid, request_id="validation-1"
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["request_id"], "validation-1")
        self.assertEqual(response["errors"][0]["path"], "components.1.pin")
        self.assertIsNone(gateway.load_device_configuration("universal"))
        response = await self.request(
            "validate_device_configuration", device="universal",
            configuration=self.configuration, ui_preferences={"enabled": "slider"}
        )
        self.assertEqual(response["errors"][0]["path"], "ui_preferences.enabled")
        response = await self.request(
            "validate_device_configuration", device="universal",
            configuration=self.configuration, ui_preferences={"enabled": "list"}
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["summary"]["component_count"], 1)
        self.assertEqual(response["schema"]["parameters"]["enabled"]["type"], "boolean")
        self.assertIsNone(gateway.load_device_configuration("universal"))
        self.assertEqual(gateway.get_event_page()[3], 0)

    async def test_save_and_retry_echo_request_and_preserve_version(self):
        saved = await self.request(
            "save_device_configuration", device="universal",
            configuration=self.configuration, request_id="save-1"
        )
        self.assertTrue(saved["success"])
        self.assertEqual(saved["request_id"], "save-1")
        retry = await self.request("retry_device_configuration", device="universal", request_id="retry-1")
        self.assertTrue(retry["success"])
        self.assertEqual(retry["request_id"], "retry-1")
        self.assertEqual(retry["status"], "pending")
        self.connect("universal")
        retry = await self.request("retry_device_configuration", device="universal")
        self.assertEqual(retry["status"], "applying")
        record = gateway.load_device_configuration("universal")
        self.assertEqual(record["config_version"], saved["config_version"])
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_configuration_history").fetchone()[0], 1)

    async def test_applying_timeout_is_bounded_and_disconnect_returns_pending(self):
        gateway.save_device_configuration("universal", self.configuration)
        self.connect("universal")
        with patch.object(gateway, "CONFIGURATION_APPLY_TIMEOUT_SECONDS", 0.01):
            await gateway.send_device_configuration("universal")
            task = gateway.configuration_apply_tasks["universal"]
            await task
        record = gateway.load_device_configuration("universal")
        self.assertEqual(record["status"], "failed")
        self.assertIn("не подтвердило", record["last_error"])
        self.assertNotIn("universal", gateway.configuration_apply_tasks)
        await gateway.send_device_configuration("universal")
        task = gateway.cancel_configuration_apply("universal", pending=True)
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(gateway.load_device_configuration("universal")["status"], "pending")
        self.assertNotIn("universal", gateway.configuration_apply_tasks)

    async def test_saved_and_applied_versions_are_distinct_until_confirmation(self):
        saved = gateway.save_device_configuration("universal", self.configuration)
        _, schema, _ = validate_device_configuration(self.configuration, list(DRIVERS))
        await gateway.handle_device_configuration_result("universal", "universal", {
            "device": "universal", "config_version": 1, "config_hash": saved["config_hash"],
            "success": True, "schema_version": 1, "schema": schema,
        })
        applied = gateway.load_device_configuration("universal")
        self.assertEqual(applied["applied_version"], 1)
        changed = copy.deepcopy(self.configuration)
        changed["components"][0]["label"] = "Changed lamp"
        gateway.save_device_configuration("universal", changed)
        device = next(item for item in gateway.get_all_devices() if item["device"] == "universal")
        self.assertEqual(device["configuration_version"], 2)
        self.assertEqual(device["configuration_applied_version"], 1)
        self.assertEqual(device["configuration_applied_at"], applied["applied_at"])
        self.assertTrue(device["configuration_updated_at"])

    async def test_extreme_firmware_configuration_version_is_ignored(self):
        await gateway.handle_device_configuration_report("universal", "universal", {
            "device": "universal", "config_version": 10 ** 100,
            "schema_version": 10 ** 100, "config_hash": "0" * 64,
        })
        self.assertIsNone(gateway.load_device_configuration("universal"))

    async def test_configuration_migration_recovers_previous_applied_version(self):
        gateway.save_device_configuration("universal", self.configuration)
        gateway.update_device_configuration_status("universal", "applied", applied_at=gateway.current_time())
        with gateway.database_connection() as connection:
            connection.execute("""
                CREATE TABLE old_device_configurations (
                    device_id TEXT PRIMARY KEY, config_version INTEGER NOT NULL,
                    config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
                    status TEXT NOT NULL, last_error TEXT, updated_at TEXT NOT NULL,
                    applied_at TEXT
                )
            """)
            connection.execute("""
                INSERT INTO old_device_configurations
                SELECT device_id, config_version, config_json, config_hash,
                       status, last_error, updated_at, applied_at
                FROM device_configurations
            """)
            connection.execute("DROP TABLE device_configurations")
            connection.execute("ALTER TABLE old_device_configurations RENAME TO device_configurations")
        gateway.init_db()
        self.assertEqual(gateway.load_device_configuration("universal")["applied_version"], 1)

    async def test_retry_replaces_timer_without_old_task_clearing_new_attempt(self):
        gateway.save_device_configuration("universal", self.configuration)
        self.connect("universal")
        await gateway.send_device_configuration("universal")
        previous = gateway.configuration_apply_tasks["universal"]
        await gateway.send_device_configuration("universal")
        current = gateway.configuration_apply_tasks["universal"]
        self.assertIsNot(previous, current)
        await asyncio.gather(previous, return_exceptions=True)
        self.assertIs(gateway.configuration_apply_tasks["universal"], current)
        self.assertEqual(gateway.load_device_configuration("universal")["status"], "applying")

    async def test_dispatch_and_diagnostics_wait_for_configuration_confirmation(self):
        _, schema, _ = validate_device_configuration(self.configuration, list(DRIVERS))
        gateway.device_metadata["universal"]["schema"] = schema
        gateway.save_device_configuration("universal", self.configuration)
        socket = self.connect("universal")
        await gateway.send_device_configuration("universal")
        browser = CapturingSocket()
        self.assertFalse(await gateway.dispatch_device_command(
            "universal", "enabled", True, browser=browser
        ))
        self.assertFalse(await gateway.dispatch_device_action(
            "universal", "identify", browser=browser
        ))
        self.assertEqual(len(browser.messages), 2)
        self.assertTrue(all("подтверждения" in message["message"] for message in browser.messages))
        self.assertFalse(any(message.get("type") == "command" for message in socket.messages))
        config = self.program([{
            "type": "set_parameter", "device": "universal", "parameter": "enabled",
            "value": {"type": "constant", "value": True},
        }])
        response = await self.request("check_automation", config=config)
        self.assertFalse(response["ready"])
        self.assertIn("configuration_applying", [issue["code"] for issue in response["issues"]])

    async def test_connection_event_waits_for_confirmation_and_current_values_once(self):
        saved = gateway.save_device_configuration("universal", self.configuration)
        _, schema, _ = validate_device_configuration(self.configuration, list(DRIVERS))
        gateway.device_metadata["universal"]["schema"] = schema
        self.connect("universal")
        gateway.deferred_connection_automations.add("universal")
        gateway.device_states["universal"] = {"enabled": True}
        gateway.record_device_telemetry("universal", {"enabled": True})
        with patch.object(gateway, "fire_automation_event") as fire:
            await gateway.resume_device_configuration("universal")
            fire.assert_not_called()
            await gateway.send_device_configuration("universal")
            await gateway.handle_device_configuration_result("universal", "universal", {
                "device": "universal", "config_version": 1, "config_hash": saved["config_hash"],
                "success": True, "schema_version": 1, "schema": schema,
            })
            fire.assert_not_called()
            self.assertTrue(gateway.device_configuration_is_pending("universal"))
            gateway.device_states["universal"] = {"enabled": False}
            gateway.record_device_telemetry("universal", {"enabled": False})
            await gateway.resume_device_configuration("universal")
            await gateway.resume_device_configuration("universal")
            fire.assert_called_once_with("device_connected", "universal")
        self.assertFalse(gateway.device_configuration_is_pending("universal"))
        device = next(item for item in gateway.get_all_devices() if item["device"] == "universal")
        self.assertFalse(device["configuration_sync_pending"])

    async def test_empty_configuration_report_completes_connection(self):
        schema = gateway.device_metadata["universal"]["schema"]
        self.connect("universal")
        gateway.deferred_connection_automations.add("universal")
        with patch.object(gateway, "fire_automation_event") as fire:
            await gateway.handle_device_configuration_report("universal", "universal", {
                "device": "universal", "config_version": 0, "config_hash": "",
                "schema_version": 1, "schema": schema,
            })
            fire.assert_called_once_with("device_connected", "universal")
        self.assertFalse(gateway.device_configuration_is_pending("universal"))

    async def test_queued_command_is_revalidated_after_schema_change(self):
        self.connect("universal")
        pending = {
            "command_id": "obsolete", "device": "universal", "browser": None,
            "operation": {"kind": "parameter", "name": "enabled", "value": True},
            "payload": {"type": "command", "data": {"command": "set", "parameter": "enabled", "value": True}},
            "status": "queued", "attempts": 0, "timeout_task": None, "sending": False,
        }
        gateway.create_command_queue_record(pending)
        gateway.pending_commands["obsolete"] = pending
        self.assertFalse(await gateway.deliver_pending_command(pending))
        self.assertNotIn("obsolete", gateway.pending_commands)
        with gateway.database_connection() as connection:
            status, error = connection.execute(
                "SELECT status, error FROM command_queue WHERE command_id = 'obsolete'"
            ).fetchone()
        self.assertEqual(status, "failed")
        self.assertIn("Schema", error)
        self.assertEqual(gateway.devices["universal"].messages, [])

    async def test_diagnostics_warn_about_offline_command_target(self):
        config = self.program([{
            "type": "set_parameter", "device": "lamp", "parameter": "enabled",
            "value": {"type": "constant", "value": True},
        }])
        response = await self.request("check_automation", config=config, request_id="check-1")
        self.assertTrue(response["success"])
        self.assertFalse(response["ready"])
        self.assertIsNone(response["automation_id"])
        self.assertEqual(response["request_id"], "check-1")
        self.assertEqual(response["issues"][0]["code"], "target_offline")
        self.assertEqual(response["issues"][0]["device"], "lamp")

    async def test_offline_event_trigger_and_connection_condition_are_valid(self):
        config = self.program([{"type": "wait", "seconds": 0}], {
            "type": "parameter_changed", "device": "sensor", "parameter": "reading"
        })
        self.assertTrue((await self.request("check_automation", config=config))["ready"])
        config = self.program([{
            "type": "if", "branches": [{
                "condition": {"type": "device_connected", "device": "lamp"},
                "then": [{"type": "wait", "seconds": 0}],
            }], "else": [],
        }])
        self.assertTrue((await self.request("check_automation", config=config))["ready"])

    async def test_diagnostics_track_stale_reads_and_pending_schema(self):
        config = self.program([{
            "type": "if", "branches": [{
                "condition": {"type": "compare", "operator": "lt",
                    "left": {"type": "parameter", "device": "sensor", "parameter": "reading"},
                    "right": {"type": "constant", "value": 10}},
                "then": [{"type": "wait", "seconds": 0}],
            }], "else": [],
        }])
        self.connect("sensor")
        response = await self.request("check_automation", config=config)
        self.assertFalse(response["ready"])
        self.assertEqual(response["issues"][0]["code"], "parameter_stale")
        gateway.device_states["sensor"] = {"reading": 5}
        gateway.record_device_telemetry("sensor", {"reading": 5})
        self.assertTrue((await self.request("check_automation", config=config))["ready"])
        gateway.pending_device_schemas["sensor"] = {}
        response = await self.request("check_automation", config=config)
        self.assertFalse(response["ready"])
        self.assertEqual(response["issues"][0]["code"], "pending_schema")

    async def test_saved_scenario_diagnostics_do_not_execute_commands(self):
        config = self.program([{
            "type": "set_parameter", "device": "lamp", "parameter": "enabled",
            "value": {"type": "constant", "value": True},
        }])
        socket = self.connect("lamp")
        self.assertTrue(gateway.save_automation("saved-check", config)[0])
        response = await self.request("check_automation", automation_id="saved-check")
        self.assertTrue(response["ready"])
        self.assertEqual(socket.messages, [])
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM command_queue").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0], 0)
        response = await self.request("check_automation", automation_id="missing")
        self.assertFalse(response["success"])
        self.assertEqual(response["issues"][0]["code"], "automation_missing")


if __name__ == "__main__":
    unittest.main()
