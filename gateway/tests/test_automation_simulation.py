"""Preview shares expression semantics while forbidding physical and persistent work."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway
from simulation import MAX_REPORT_BYTES, MAX_STEPS, handle_simulation_message, simulate_automation


def constant(value):
    return {"type": "constant", "value": value}


def parameter(device, name):
    return {"type": "parameter", "device": device, "parameter": name}


def set_power(value=False):
    return {"type": "set_parameter", "device": "lamp", "parameter": "power", "value": constant(value), "block_id": "set-power"}


class Browser:
    def __init__(self):
        self.messages = []

    async def send(self, value):
        self.messages.append(json.loads(value))


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.browser = Browser()
        self.sensor = {"schema": {"parameters": {
            "temperature": {"type": "number", "access": "read_only", "min": -50, "max": 150, "label": "Temperature"},
        }, "actions": {}}, "schema_version": 1}
        self.lamp = {"schema": {"parameters": {
            "power": {"type": "boolean", "access": "read_write", "label": "Power"},
            "level": {"type": "integer", "access": "read_write", "min": 0, "max": 100, "label": "Level"},
        }, "actions": {"night": {"label": "Night mode", "arguments": {}}}}, "schema_version": 1}
        for device, metadata in (("sensor", self.sensor), ("lamp", self.lamp)):
            self.gateway.save_device_metadata(device, metadata)
            self.gateway.device_metadata[device] = metadata
        self.config = {
            "version": 2, "name": "Preview", "workspace": {"blocks": {}},
            "program": {"version": 1, "trigger": {"type": "manual"}, "body": [set_power()]},
        }

    def snapshot(self):
        with self.gateway.database_connection() as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            records = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        return records, deepcopy(self.gateway.device_states), dict(self.gateway.pending_commands), dict(self.gateway.automation_tasks)

    def run_preview(self, config=None, **kwargs):
        before = self.snapshot()
        message = {"action": "simulate_automation", "request_id": "simulation-test", "config": deepcopy(config or self.config), **kwargs}
        with (
            patch.object(self.gateway, "dispatch_device_command", new=AsyncMock(side_effect=AssertionError("Physical parameter command"))),
            patch.object(self.gateway, "dispatch_device_action", new=AsyncMock(side_effect=AssertionError("Physical action"))),
            patch.object(self.gateway, "start_automation_run", side_effect=AssertionError("Real run")),
            patch.object(self.gateway, "save_device_event", side_effect=AssertionError("Event mutation")),
            patch.object(self.gateway, "save_device_state", side_effect=AssertionError("Device state mutation")),
            patch.object(self.gateway, "create_command_queue_record", side_effect=AssertionError("Queue mutation")),
            patch("asyncio.sleep", new=AsyncMock(side_effect=AssertionError("Real wait"))),
        ):
            result = simulate_automation(self.gateway, message)
        self.assertEqual(self.snapshot(), before)
        return result

    def test_virtual_wait_and_commands_are_traced_without_effects(self):
        self.config["program"]["body"] = [{"type": "wait", "seconds": 3600, "block_id": "pause"}, set_power(True)]
        result = self.run_preview()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["elapsed_seconds"], 3600)
        self.assertEqual([step["type"] for step in result["steps"]], ["trigger", "wait", "set_parameter"])
        self.assertEqual(result["steps"][-1]["at_seconds"], 3600)
        self.assertEqual(result["steps"][-1]["block_id"], "set-power")
        self.assertEqual(result["final_parameters"], [{"device": "lamp", "parameter": "power", "value": True}])
        self.assertTrue(result["program_tag"])

    def test_override_selects_branch_and_sequential_assignment_changes_virtual_condition(self):
        self.config["program"]["body"] = [{"type": "if", "block_id": "threshold", "branches": [{
            "condition": {"type": "compare", "operator": "lt", "left": parameter("sensor", "temperature"), "right": constant(10)},
            "then": [set_power(True)],
        }], "else": [set_power(False)]}, {"type": "if", "branches": [{
            "condition": parameter("lamp", "power"), "then": [{"type": "wait", "seconds": 5}],
        }], "else": []}]
        result = self.run_preview(parameters=[{"device": "sensor", "parameter": "temperature", "value": 5}])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["elapsed_seconds"], 5)
        self.assertEqual(result["inputs"], [{"device": "sensor", "parameter": "temperature", "value": 5, "source": "override"}])
        self.assertTrue(result["steps"][1]["details"]["result"])
        other = self.run_preview(parameters=[{"device": "sensor", "parameter": "temperature", "value": 15}])
        self.assertTrue(other["success"], other)
        self.assertEqual(other["elapsed_seconds"], 0)
        self.assertEqual(other["final_parameters"][0]["value"], False)

    async def test_only_fresh_real_input_is_copied(self):
        self.config["program"]["body"] = [{"type": "if", "branches": [{
            "condition": {"type": "compare", "operator": "gt", "left": parameter("sensor", "temperature"), "right": constant(20)},
            "then": [set_power(True)],
        }], "else": []}]
        self.gateway.devices["sensor"] = object()
        self.gateway.device_states["sensor"] = {"temperature": 25}
        self.gateway.record_device_telemetry("sensor", {"temperature": 25})
        result = self.run_preview()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["inputs"][0]["source"], "live")
        self.gateway.last_parameter_telemetry["sensor"]["temperature"] = asyncio.get_running_loop().time() - 100
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertIn("устарело", result["message"])

    def test_short_circuit_does_not_require_unreached_input(self):
        self.config["program"]["body"] = [{"type": "if", "branches": [{
            "condition": {"type": "and", "operands": [constant(False), {"type": "compare", "operator": "gt", "left": parameter("sensor", "temperature"), "right": constant(20)}]},
            "then": [set_power(True)],
        }], "else": [{"type": "wait", "seconds": 2}]}]
        result = self.run_preview()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["elapsed_seconds"], 2)
        self.assertFalse(result["final_parameters"])

    def test_trigger_filter_and_stability_use_virtual_time_without_real_cooldown(self):
        self.config["program"]["trigger"] = {"type": "parameter_changed", "device": "sensor", "parameter": "temperature", "value": 10, "for_seconds": 60, "cooldown_seconds": 600}
        result = self.run_preview(parameters=[{"device": "sensor", "parameter": "temperature", "value": 5}])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "not_triggered")
        self.assertEqual(len(result["steps"]), 1)
        result = self.run_preview(parameters=[{"device": "sensor", "parameter": "temperature", "value": 10}])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["elapsed_seconds"], 60)
        self.assertTrue(any("между реальными" in warning for warning in result["warnings"]))
        offline = self.run_preview(parameters=[{"device": "sensor", "parameter": "temperature", "value": 10}], connections=[{"device": "sensor", "connected": False}])
        self.assertEqual(offline["status"], "not_triggered")

    def test_disconnected_trigger_and_virtual_connection_override(self):
        self.config["program"]["trigger"] = {"type": "device_disconnected", "device": "sensor"}
        result = self.run_preview()
        self.assertTrue(result["success"], result)
        result = self.run_preview(connections=[{"device": "sensor", "connected": True}])
        self.assertEqual(result["status"], "not_triggered")
        result = self.run_preview(connections=[{"device": "lamp", "connected": False}])
        self.assertFalse(result["success"])
        self.assertIn("отключено", result["message"])

    def test_unknown_action_effects_are_not_invented(self):
        self.config["program"]["body"] = [set_power(True), {"type": "invoke_action", "device": "lamp", "action": "night", "arguments": {}, "block_id": "night"}]
        result = self.run_preview()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["final_parameters"])
        self.assertEqual(result["steps"][-1]["details"]["effects"], "unknown")
        self.config["program"]["body"].append({"type": "if", "branches": [{"condition": parameter("lamp", "power"), "then": [set_power(False)]}], "else": []})
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertIn("неизвестно после", result["message"])

    def test_arithmetic_and_computed_command_values_match_real_type_constraints(self):
        self.config["program"]["body"] = [{"type": "set_parameter", "device": "lamp", "parameter": "level", "value": {"type": "arithmetic", "operator": "divide", "left": constant(10), "right": constant(2)}}]
        # The real evaluator returns 5.0, which the integer Schema rejects.
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertIn("Schema", result["message"])
        self.lamp["schema"]["parameters"]["level"]["type"] = "number"
        self.config["program"]["body"][0]["value"]["right"] = constant(0)
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertIn("вычислить", result["message"])
        self.config["program"]["body"][0]["value"].update(left=constant(1000), right=constant(1))
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertIn("не соответствует Schema", result["message"])

    def test_overrides_and_requests_are_strictly_validated(self):
        invalid = [
            {"parameters": [{"device": "lamp", "parameter": "power", "value": 1}]},
            {"parameters": [{"device": "lamp", "parameter": "missing", "value": True}]},
            {"parameters": [{"device": "lamp", "parameter": "power", "value": True}] * 2},
            {"connections": [{"device": "lamp", "connected": 1}]},
            {"connections": [{"device": "lamp", "connected": True}] * 2},
            {"connections": [{"device": "unknown", "connected": True}]},
            {"request_id": []}, {"parameters": None},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertFalse(self.run_preview(**fields)["success"])
        self.gateway.database_restore_in_progress = True
        self.assertFalse(self.run_preview()["success"])

    def test_saved_disabled_scenario_is_previewed_without_enabling_or_run_records(self):
        self.gateway.save_automation("disabled", self.config, enabled=False)
        before = self.snapshot()
        result = simulate_automation(self.gateway, {"action": "simulate_automation", "automation_id": "disabled", "request_id": "saved"})
        self.assertTrue(result["success"], result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["automation_id"], "disabled")
        self.assertFalse(simulate_automation(self.gateway, {"config": self.config, "automation_id": "disabled"})["success"])

    def test_legacy_configuration_is_supported(self):
        config = {"name": "Old rule", "trigger": {"device": "sensor", "parameter": "temperature", "operator": "below", "threshold": 10}, "target": {"device": "lamp", "kind": "parameter", "name": "power", "value": True}}
        result = self.run_preview(config, parameters=[{"device": "sensor", "parameter": "temperature", "value": 5}])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["final_parameters"][0]["value"], True)

    def test_long_program_stops_at_bounded_trace(self):
        branch = {"type": "if", "branches": [{"condition": constant(True), "then": [{"type": "wait", "seconds": 1}] * 94}], "else": []}
        self.config["program"]["body"] = [deepcopy(branch), deepcopy(branch)]
        result = self.run_preview()
        self.assertFalse(result["success"], result)
        self.assertLessEqual(len(result["steps"]), MAX_STEPS)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), MAX_REPORT_BYTES)

    async def test_large_live_strings_do_not_create_oversized_response(self):
        self.sensor["schema"]["parameters"]["memo"] = {"type": "string", "access": "read_only"}
        self.gateway.devices["sensor"] = object()
        self.gateway.device_states["sensor"] = {"memo": "x" * 1024}
        self.gateway.record_device_telemetry("sensor", {"memo": "x" * 1024})
        self.config["program"]["body"] = []
        for index in range(90):
            name = f"text{index}"
            self.lamp["schema"]["parameters"][name] = {"type": "string", "access": "read_write"}
            self.config["program"]["body"].append({"type": "set_parameter", "device": "lamp", "parameter": name, "value": parameter("sensor", "memo")})
        result = self.run_preview()
        self.assertFalse(result["success"])
        self.assertTrue(result["steps"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), MAX_REPORT_BYTES)

    async def test_message_hook_only_sends_correlated_simulation_result(self):
        before = self.snapshot()
        self.assertFalse(await handle_simulation_message(self.gateway, self.browser, {"action": "other"}))
        self.assertTrue(await handle_simulation_message(self.gateway, self.browser, {"action": "simulate_automation", "request_id": "hook", "config": self.config}))
        self.assertEqual(len(self.browser.messages), 1)
        self.assertEqual(self.browser.messages[0]["type"], "automation_simulation")
        self.assertEqual(self.browser.messages[0]["request_id"], "hook")
        self.assertEqual(self.snapshot(), before)

    async def test_unexpected_preview_error_is_correlated_and_does_not_close_browser(self):
        with patch("simulation.simulate_automation", side_effect=RuntimeError("private internals")):
            self.assertTrue(await handle_simulation_message(self.gateway, self.browser, {
                "action": "simulate_automation", "request_id": "unexpected", "config": self.config,
            }))
        result = self.browser.messages[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["request_id"], "unexpected")
        self.assertNotIn("private", result["message"])


if __name__ == "__main__":
    unittest.main()
