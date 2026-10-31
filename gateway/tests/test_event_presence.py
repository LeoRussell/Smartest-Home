import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as gateway


class TestSocket:
    remote_address = ("event-presence-test", 0)

    def __init__(self, messages=(), responds_to_ping=True):
        self.incoming = messages
        self.messages = []
        self.closes = []
        self.responds_to_ping = responds_to_ping
        self.close_code = None
        self.close_reason = ""

    def __aiter__(self):
        async def iterate():
            for message in self.incoming:
                yield message
        return iterate()

    async def send(self, message):
        self.messages.append(json.loads(message))

    async def ping(self):
        pong = asyncio.get_running_loop().create_future()
        if self.responds_to_ping:
            pong.set_result(0.001)
        return pong

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason
        self.closes.append((code, reason))


class EventPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = {
            "DB_PATH": Path(directory.name) / "gateway.db",
            "DEVICE_CONFIGURATION_EXPORT_DIR": Path(directory.name) / "schemas",
            "devices": {}, "device_metadata": {}, "device_states": {},
            "browser_clients": set(), "device_wire_ids": {}, "device_handler_tasks": set(),
            "last_device_heartbeat": {}, "last_device_telemetry": {},
            "last_parameter_telemetry": {}, "last_device_probe": {},
            "fresh_device_parameters": {}, "pending_commands": {},
            "pending_device_schemas": {}, "deferred_device_schemas": {},
            "pending_device_connections": {}, "deferred_connection_automations": set(),
            "configuration_apply_tasks": {}, "configuration_confirmed_devices": set(),
            "automation_tasks": {}, "automation_trigger_tasks": {},
            "automation_trigger_devices": {}, "automation_stop_requests": set(),
            "automation_waiting_tasks": set(), "automation_restart_tasks": set(),
            "database_restore_in_progress": False, "GATEWAY_SESSION_STARTED_AT": None,
        }
        for name, value in settings.items():
            patcher = patch.object(gateway, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gateway.init_db()

    async def asyncTearDown(self):
        tasks = list(gateway.configuration_apply_tasks.values())
        tasks += list(gateway.automation_tasks.values())
        tasks += list(gateway.automation_trigger_tasks.values())
        tasks += [pending["timeout_task"] for pending in gateway.pending_commands.values()
                  if pending.get("timeout_task") is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def command(self, command_id, automation_id=None, kind="parameter"):
        pending = {
            "command_id": command_id, "device": "lamp", "browser": None,
            "operation": {"kind": kind, "name": "enabled" if kind == "parameter" else "identify", "value": True},
            "payload": {}, "attempts": 1, "status": "sent", "timeout_task": None,
            "automation_id": automation_id, "automation_run_id": "run-test",
        }
        gateway.create_command_queue_record(pending)
        return pending

    async def test_successful_scenario_operation_records_state_and_keeps_queue(self):
        pending = self.command("auto-scenario", "scenario-id")
        pending["automation_state_change"] = {"parameter": "enabled", "value": True}
        await gateway.send_pending_command_result(pending, "succeeded", True, None)
        events, _, _, count = gateway.get_event_page()
        self.assertEqual(count, 1)
        self.assertEqual(events[0]["event_type"], "parameter")
        self.assertEqual(json.loads(events[0]["event_value"])["source"], "automation")
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM command_queue WHERE command_id = 'auto-scenario'"
            ).fetchone()[0], "succeeded")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM device_events WHERE event_type = 'command'"
            ).fetchone()[0], 0)

    async def test_successful_scenario_action_has_run_result_without_command_event(self):
        pending = self.command("auto-action", "scenario-id", kind="action")
        await gateway.send_pending_command_result(pending, "succeeded", None, None)
        self.assertEqual(gateway.get_event_page()[3], 0)
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM command_queue WHERE command_id = 'auto-action'"
            ).fetchone()[0], "succeeded")

    async def test_manual_success_and_scenario_failures_remain_command_events(self):
        for command_id, automation_id, status in (
            ("manual-ok", None, "succeeded"),
            ("auto-failed", "scenario-id", "failed"),
            ("auto-timeout", "scenario-id", "timeout"),
        ):
            await gateway.send_pending_command_result(
                self.command(command_id, automation_id), status, None, "test" if status != "succeeded" else None
            )
        events, _, _, count = gateway.get_event_page()
        self.assertEqual(count, 3)
        self.assertTrue(all(event["event_type"] == "command" for event in events))

    async def test_failed_scenario_ack_keeps_actual_state_change_and_error(self):
        schema = {"parameters": {"level": {"label": "Level", "type": "integer", "access": "read_write"}}, "actions": {}}
        gateway.save_device_metadata("lamp", {
            "schema": schema, "schema_version": 1, "protocol_version": 2,
            "firmware_id": "lamp", "trust_policy": "allow",
        })
        pending = self.command("auto-real-failure", "scenario-id")
        pending["operation"].update({"name": "level", "value": 2})
        gateway.pending_commands[pending["command_id"]] = pending
        peer = TestSocket([
            json.dumps({"type": "hello", "device": "lamp", "schema": schema, "schema_version": 1}),
            json.dumps({"type": "state", "device": "lamp", "data": {"parameters": {"level": 0}}}),
            json.dumps({"type": "command_result", "device": "lamp", "command_id": pending["command_id"],
                        "success": False, "data": {"parameter": "level", "value": 1, "error": "partially applied"}}),
        ])
        await gateway.handle_client(peer)
        events = gateway.get_event_page()[0]
        state_events = [event for event in events if event["event_type"] == "parameter"]
        failures = [event for event in events if event["event_type"] == "command"]
        self.assertEqual(len(state_events), 1)
        self.assertEqual(json.loads(state_events[0]["event_value"])["value"], 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(json.loads(failures[0]["event_value"])["status"], "failed")

    async def test_historical_visibility_counts_pagination_search_and_raw_records(self):
        rows = [
            ("command", {"command_id": "manual-1", "status": "succeeded"}),
            ("command", {"command_id": "auto-old", "status": "succeeded", "marker": "hidden-marker"}),
            ("parameter", {"parameter": "enabled", "value": True, "source": "automation", "command_id": "auto-old"}),
            ("command", {"command_id": "auto-failure", "status": "failed"}),
            ("command", {"command_id": "legacy-2", "automation_id": "scenario-id", "status": "succeeded", "marker": "hidden-marker"}),
            ("connection", "Connected"),
            ("command", "not-json"),
        ]
        for event_type, value in rows:
            gateway.save_device_event("lamp", event_type, json.dumps(value) if isinstance(value, dict) else value)
        first, more, cursor, count = gateway.get_event_page(limit=2)
        self.assertEqual([event["id"] for event in first], [7, 6])
        self.assertTrue(more)
        self.assertEqual(count, 5)
        second, more, cursor, count = gateway.get_event_page(limit=2, before_id=cursor)
        self.assertEqual([event["id"] for event in second], [4, 3])
        self.assertTrue(more)
        self.assertEqual(count, 5)
        last, more, cursor, count = gateway.get_event_page(limit=2, before_id=cursor)
        self.assertEqual([event["id"] for event in last], [1])
        self.assertFalse(more)
        self.assertIsNone(cursor)
        self.assertEqual(count, 5)
        self.assertEqual(gateway.get_event_page(search="hidden-marker"), ([], False, None, 0))
        self.assertEqual(len(gateway.get_recent_events()), 5)
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_events").fetchone()[0], 7)

    async def test_malformed_history_metadata_does_not_hide_manual_results(self):
        for value in ("null", "[]", "42", "bad-json"):
            self.assertTrue(gateway.event_is_visible("command", value))
        for automation_id in ({}, [], True, 0, " "):
            value = json.dumps({"status": "succeeded", "command_id": "manual", "automation_id": automation_id})
            self.assertTrue(gateway.event_is_visible("command", value))

    async def test_healthy_quiet_transport_is_not_closed_or_made_fresh_by_pong(self):
        peer = TestSocket()
        gateway.devices["quiet"] = peer
        old = asyncio.get_running_loop().time() - 100
        gateway.last_device_heartbeat["quiet"] = old
        gateway.last_device_probe["quiet"] = old
        gateway.last_device_telemetry["quiet"] = old
        gateway.last_parameter_telemetry["quiet"] = {"reading": old}
        gateway.fresh_device_parameters["quiet"] = {"reading"}
        await gateway.probe_device_transport("quiet", peer)
        self.assertEqual(peer.closes, [])
        self.assertTrue(gateway.device_is_connected("quiet"))
        self.assertFalse(gateway.parameter_value_is_fresh("quiet", "reading"))
        self.assertEqual(gateway.last_device_telemetry["quiet"], old)

    async def test_dead_transport_is_closed_only_after_failed_pong(self):
        peer = TestSocket(responds_to_ping=False)
        gateway.devices["dead"] = peer
        old = asyncio.get_running_loop().time() - 100
        gateway.last_device_heartbeat["dead"] = old
        gateway.last_device_probe["dead"] = old
        with patch.object(gateway, "DEVICE_TRANSPORT_PROBE_TIMEOUT_SECONDS", 0.001):
            await gateway.probe_device_transport("dead", peer)
        self.assertEqual(peer.closes, [(1011, "Device did not answer a WebSocket ping")])

    async def test_partial_updates_do_not_refresh_other_parameters(self):
        gateway.devices["sensor"] = TestSocket()
        gateway.device_metadata["sensor"] = {"schema": {"parameters": {
            "a": {"type": "number", "access": "read_only"},
            "b": {"type": "number", "access": "read_only"},
        }, "actions": {}}}
        gateway.device_states["sensor"] = {"a": 1, "b": 2}
        gateway.fresh_device_parameters["sensor"] = {"a", "b"}
        old = asyncio.get_running_loop().time() - 100
        gateway.last_parameter_telemetry["sensor"] = {"a": old, "b": old}
        gateway.record_device_telemetry("sensor", {"a": 1})
        self.assertTrue(gateway.parameter_value_is_fresh("sensor", "a"))
        self.assertFalse(gateway.parameter_value_is_fresh("sensor", "b"))
        self.assertEqual(gateway.evaluate_automation_expression({
            "type": "parameter", "device": "sensor", "parameter": "a"
        }), 1)
        with self.assertRaisesRegex(gateway.AutomationExecutionError, "устарело"):
            gateway.evaluate_automation_expression({
                "type": "parameter", "device": "sensor", "parameter": "b"
            })
        before = dict(gateway.last_parameter_telemetry["sensor"])
        gateway.record_device_telemetry("sensor", {})
        self.assertEqual(gateway.last_parameter_telemetry["sensor"], before)
        self.assertEqual(gateway.last_parameter_telemetry["sensor"]["b"], old)

    async def test_stable_trigger_does_not_run_from_stale_parameter(self):
        gateway.devices["sensor"] = TestSocket()
        gateway.device_metadata["sensor"] = {"schema": {"parameters": {
            "a": {"type": "number", "access": "read_only"},
            "b": {"type": "number", "access": "read_only"},
        }, "actions": {}}}
        gateway.device_states["sensor"] = {"a": 1, "b": 2}
        gateway.fresh_device_parameters["sensor"] = {"a", "b"}
        gateway.last_parameter_telemetry["sensor"] = {
            "b": asyncio.get_running_loop().time() - 100
        }
        gateway.record_device_telemetry("sensor", {"a": 1})
        config = {
            "version": 2, "name": "Stale trigger", "workspace": {"blocks": {}},
            "program": {
                "version": 1,
                "trigger": {"type": "parameter_changed", "device": "sensor", "parameter": "b"},
                "body": [{"type": "wait", "seconds": 0}],
            },
        }
        self.assertTrue(gateway.save_automation("stale-trigger", config)[0])
        with patch.object(gateway, "start_automation_run") as start:
            task = asyncio.create_task(gateway.wait_for_stable_automation_trigger(
                "stale-trigger", config, "sensor", "b", 2
            ))
            gateway.automation_trigger_tasks["stale-trigger"] = task
            gateway.automation_trigger_devices["stale-trigger"] = "sensor"
            await task
            start.assert_not_called()

    async def test_empty_device_state_keeps_transport_and_reports_close_reason(self):
        schema = {"parameters": {"enabled": {"type": "boolean", "access": "read_write"}}, "actions": {}}
        gateway.save_device_metadata("quiet", {"schema": schema, "schema_version": 1, "trust_policy": "allow"})
        peer = TestSocket([
            json.dumps({"type": "hello", "device": "quiet", "schema": schema, "schema_version": 1}),
            json.dumps({"type": "state", "device": "quiet", "data": {"parameters": {}}}),
        ])
        peer.close_code, peer.close_reason = 1001, "test power off"
        with patch("builtins.print") as log:
            await gateway.handle_client(peer)
        lines = [" ".join(str(value) for value in call.args) for call in log.call_args_list]
        self.assertTrue(any("[CLOSE] quiet: code=1001" in line and "test power off" in line and "last_data_age=none" in line for line in lines))
        self.assertEqual(peer.closes, [])
        with gateway.database_connection() as connection:
            self.assertIsNone(connection.execute("SELECT last_telemetry_at FROM devices WHERE device_id='quiet'").fetchone()[0])

    async def test_malformed_decoder_frames_report_errors_and_connection_continues(self):
        frames = [json.dumps({"type": "browser_hello"}), "[" * 1500 + "]" * 1500, b"\xff"]
        if hasattr(sys, "get_int_max_str_digits") and sys.get_int_max_str_digits():
            frames.append('{"large":' + "1" * (sys.get_int_max_str_digits() + 1) + "}")
        frames.append(json.dumps({"type": "browser_message", "action": "get_devices"}))
        peer = TestSocket(frames)
        await gateway.handle_client(peer)
        self.assertEqual(len([message for message in peer.messages if message["type"] == "error"]), len(frames) - 2)
        self.assertEqual(peer.messages[-1]["type"], "device_list")
        self.assertEqual(peer.closes, [])


if __name__ == "__main__":
    unittest.main()
