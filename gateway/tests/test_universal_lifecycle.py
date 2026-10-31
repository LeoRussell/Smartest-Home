"""Gateway integration with in-memory WebSocket peers, not MCU/hardware tests."""

import asyncio
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as gateway
from device_configuration import DRIVERS, validate_device_configuration


DEVICE_ID = "universal-lifecycle-test"
DRIVER_IDS = list(DRIVERS)


def validated_configuration(components=None):
    configuration, schema, error = validate_device_configuration({
        "format_version": 1,
        "components": components or [],
    }, DRIVER_IDS)
    if error:
        raise AssertionError(error)
    return configuration, schema


def gpio_component():
    return {
        "id": "lamp", "driver": "gpio_output", "label": "Lamp",
        "parameter_id": "enabled", "parameter_label": "Enabled",
        "pin": 4, "active_high": True, "initial_value": False,
    }


class MemorySocket:
    """Exercise the actual server handler without binding network ports."""

    remote_address = ("isolated-test", 0)

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def feed(self, message):
        self.incoming.put_nowait(json.dumps(message))

    async def send(self, message):
        self.outgoing.put_nowait(json.loads(message))

    async def receive(self, message_type):
        async def find_message():
            while True:
                message = await self.outgoing.get()
                if message.get("type") == message_type:
                    return message
        return await asyncio.wait_for(find_message(), timeout=3)

    async def close(self, **kwargs):
        if not self.closed.is_set():
            self.closed.set()
            self.incoming.put_nowait(None)

    async def wait_closed(self):
        await self.closed.wait()


class UniversalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        values = {
            "DB_PATH": Path(directory.name) / "gateway.db",
            "DEVICE_CONFIGURATION_EXPORT_DIR": Path(directory.name) / "schemas",
            "DATABASE_BACKUP_DIR": Path(directory.name) / "backups",
            "device_metadata": {}, "devices": {}, "device_states": {},
            "device_wire_ids": {}, "device_handler_tasks": set(),
            "browser_clients": set(), "last_device_heartbeat": {},
            "last_device_telemetry": {}, "last_parameter_telemetry": {},
            "last_device_probe": {}, "configuration_confirmed_devices": set(),
            "fresh_device_parameters": {}, "pending_commands": {},
            "pending_device_connections": {}, "pending_device_schemas": {},
            "deferred_device_schemas": {}, "deferred_connection_automations": set(),
            "automation_tasks": {}, "automation_trigger_tasks": {},
            "automation_trigger_devices": {}, "automation_stop_requests": set(),
            "automation_waiting_tasks": set(), "automation_restart_tasks": set(),
            "configuration_apply_tasks": {}, "database_restore_in_progress": False,
            "GATEWAY_SESSION_STARTED_AT": None,
        }
        for name, value in values.items():
            patcher = patch.object(gateway, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gateway.init_db()
        self.sockets = []
        self.handlers = []
        self.browser = MemorySocket()
        self.empty_configuration, self.empty_schema = validated_configuration()

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        for handler in self.handlers:
            if not handler.done():
                await asyncio.wait_for(handler, timeout=3)
            else:
                handler.result()
        tasks = list(gateway.configuration_apply_tasks.values())
        tasks += list(gateway.automation_trigger_tasks.values())
        tasks += list(gateway.automation_tasks.values())
        tasks += [pending["timeout_task"] for pending in gateway.pending_commands.values()
                  if pending.get("timeout_task") is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def until(self, predicate):
        async def wait_for_predicate():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait_for_predicate(), timeout=3)

    def connect(self, schema=None, version=1):
        socket = MemorySocket()
        self.sockets.append(socket)
        handler = asyncio.create_task(gateway.handle_client(socket))
        self.handlers.append(handler)
        socket.feed({
            "type": "hello", "device": DEVICE_ID, "name": "Universal test board",
            "protocol_version": 2, "firmware_version": "1.0.0",
            "command_dedup": False, "device_profile": "universal",
            "board_profile": "esp32-s3-devkitc-1", "driver_catalog": DRIVER_IDS,
            "schema_version": version, "schema": schema or self.empty_schema,
        })
        return socket, handler

    async def approve(self, decision="allow", remember=True):
        await self.until(lambda: DEVICE_ID in gateway.pending_device_connections)
        request = gateway.pending_device_connections[DEVICE_ID]
        await gateway.handle_browser_message(self.browser, {
            "action": "device_access_decision", "device": DEVICE_ID,
            "request_id": request["request_id"], "decision": decision,
            "remember": remember,
        })
        result = await self.browser.receive("device_access_decision_result")
        self.assertTrue(result["success"])

    async def join_empty(self):
        socket, handler = self.connect()
        await self.approve()
        await socket.receive("hello_ack")
        await socket.receive("device_config_request")
        socket.feed({
            "type": "device_config_report", "device": DEVICE_ID,
            "config_version": 0, "config_hash": "", "schema_version": 1,
            "configuration": self.empty_configuration, "schema": self.empty_schema,
        })
        await asyncio.sleep(0)
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], self.empty_schema)
        return socket, handler

    async def save_configuration(self, configuration):
        await gateway.handle_browser_message(self.browser, {
            "action": "save_device_configuration", "device": DEVICE_ID,
            "configuration": configuration, "ui_preferences": {},
        })
        result = await self.browser.receive("device_configuration_save_result")
        self.assertTrue(result["success"], result.get("message"))
        return result

    def acknowledge(self, socket, applied, schema, success=True, **extra):
        socket.feed({
            "type": "device_config_result", "device": DEVICE_ID,
            "config_version": applied["config_version"],
            "config_hash": applied["config_hash"], "success": success,
            "schema_version": applied["config_version"], "schema": schema,
            **extra,
        })

    def state(self, socket, parameters):
        socket.feed({"type": "state", "device": DEVICE_ID,
                     "data": {"parameters": parameters}})

    def configuration_status(self):
        record = gateway.load_device_configuration(DEVICE_ID)
        return record["status"] if record else None

    async def test_unapproved_and_denied_telemetry_never_becomes_device_state(self):
        socket, handler = self.connect()
        # A different firmware can send state before hello_ack. The server
        # must also enforce approval, independently of universal firmware.
        self.state(socket, {"enabled": True})
        await self.until(lambda: DEVICE_ID in gateway.pending_device_connections)
        self.assertNotIn(DEVICE_ID, gateway.devices)
        self.assertNotIn(DEVICE_ID, gateway.device_states)
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_events").fetchone()[0], 0)
        await self.approve(decision="deny", remember=False)
        await asyncio.wait_for(handler, timeout=3)
        self.assertNotIn(DEVICE_ID, gateway.device_states)
        self.assertIsNone(gateway.load_device_configuration(DEVICE_ID))

    async def test_empty_join_apply_command_and_reconnect_keep_one_configuration_version(self):
        socket, handler = await self.join_empty()
        configuration, schema = validated_configuration([gpio_component()])
        saved = await self.save_configuration(configuration)
        applied = await socket.receive("device_config_apply")
        self.assertEqual(saved["config_version"], 1)
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], self.empty_schema)
        self.assertEqual(self.configuration_status(), "applying")

        self.state(socket, {"enabled": True})
        await socket.receive("error")
        self.assertNotIn("enabled", gateway.device_states.get(DEVICE_ID, {}))
        self.acknowledge(socket, applied, schema)
        self.state(socket, {"enabled": False})
        await self.until(lambda: self.configuration_status() == "applied")
        await self.until(lambda: gateway.device_states.get(DEVICE_ID) == {"enabled": False})
        self.assertEqual(gateway.load_device_configuration(DEVICE_ID)["applied_version"], 1)

        self.assertTrue(await gateway.dispatch_device_command(
            DEVICE_ID, "enabled", True, requested_command_id="lifecycle-command"
        ))
        command = await socket.receive("command")
        socket.feed({
            "type": "command_result", "device": DEVICE_ID,
            "command_id": command["data"]["command_id"], "success": True,
            "data": {"parameter": "enabled", "value": True},
        })
        self.state(socket, {"enabled": True})
        await self.until(lambda: "lifecycle-command" not in gateway.pending_commands)
        self.assertTrue(gateway.device_states[DEVICE_ID]["enabled"])

        saved_again = await self.save_configuration(configuration)
        self.assertEqual(saved_again["config_version"], 1)
        same_apply = await socket.receive("device_config_apply")
        self.assertEqual(same_apply["config_hash"], applied["config_hash"])
        self.acknowledge(socket, same_apply, schema)
        await self.until(lambda: self.configuration_status() == "applied")

        await socket.close()
        await handler
        reconnect, _ = self.connect(schema, version=1)
        await reconnect.receive("hello_ack")
        reapplied = await reconnect.receive("device_config_apply")
        self.assertEqual(reapplied["config_version"], applied["config_version"])
        self.assertEqual(reapplied["config_hash"], applied["config_hash"])
        self.assertNotIn(DEVICE_ID, gateway.pending_device_connections)
        self.assertNotIn(DEVICE_ID, gateway.pending_device_schemas)
        self.acknowledge(reconnect, reapplied, schema)
        await self.until(lambda: self.configuration_status() == "applied")
        self.state(reconnect, {"enabled": True})
        await self.until(lambda: any(
            item["device"] == DEVICE_ID and not item["configuration_sync_pending"]
            for item in gateway.get_all_devices()
        ))
        with gateway.database_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM device_configuration_history WHERE device_id = ?", (DEVICE_ID,)
            ).fetchone()[0], 1)

    async def test_offline_configuration_and_failed_apply_retry_preserve_accepted_schema(self):
        socket, handler = await self.join_empty()
        first_configuration, first_schema = validated_configuration([gpio_component()])
        await self.save_configuration(first_configuration)
        first = await socket.receive("device_config_apply")
        self.acknowledge(socket, first, first_schema)
        self.state(socket, {"enabled": False})
        await self.until(lambda: self.configuration_status() == "applied")
        await socket.close()
        await handler

        changed = copy.deepcopy(gpio_component())
        changed["parameter_label"] = "New label"
        second_configuration, second_schema = validated_configuration([changed])
        saved = await self.save_configuration(second_configuration)
        self.assertEqual(saved["config_version"], 2)
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], first_schema)

        reconnect, _ = self.connect(first_schema, version=1)
        await reconnect.receive("hello_ack")
        second = await reconnect.receive("device_config_apply")
        self.assertEqual(second["config_version"], 2)
        self.acknowledge(reconnect, first, first_schema)
        await asyncio.sleep(0)
        self.assertEqual(self.configuration_status(), "applying")
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], first_schema)
        self.acknowledge(reconnect, second, first_schema, success=False, error="test device rejected configuration")
        await self.until(lambda: self.configuration_status() == "failed")
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], first_schema)
        self.assertEqual(gateway.load_device_configuration(DEVICE_ID)["applied_version"], 1)

        await gateway.handle_browser_message(self.browser, {
            "action": "retry_device_configuration", "device": DEVICE_ID,
            "request_id": "retry-lifecycle",
        })
        retried = await self.browser.receive("device_configuration_retry_result")
        self.assertTrue(retried["success"], retried.get("message"))
        self.assertEqual(retried["request_id"], "retry-lifecycle")
        self.assertEqual(retried["config_version"], 2)
        retry_apply = await reconnect.receive("device_config_apply")
        self.assertEqual(retry_apply["config_hash"], second["config_hash"])
        self.acknowledge(reconnect, retry_apply, second_schema)
        await self.until(lambda: self.configuration_status() == "applied")
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], second_schema)

    async def test_invalid_save_preserves_applied_configuration_and_current_state(self):
        socket, _ = await self.join_empty()
        configuration, schema = validated_configuration([gpio_component()])
        await self.save_configuration(configuration)
        applied = await socket.receive("device_config_apply")
        self.acknowledge(socket, applied, schema)
        self.state(socket, {"enabled": True})
        await self.until(lambda: self.configuration_status() == "applied")
        await self.until(lambda: gateway.device_states.get(DEVICE_ID) == {"enabled": True})

        duplicate_pin = copy.deepcopy(gpio_component())
        duplicate_pin.update({"id": "other", "parameter_id": "other_enabled"})
        await gateway.handle_browser_message(self.browser, {
            "action": "save_device_configuration", "device": DEVICE_ID,
            "configuration": {"format_version": 1, "components": [gpio_component(), duplicate_pin]},
            "ui_preferences": {},
        })
        rejected = await self.browser.receive("device_configuration_save_result")
        self.assertFalse(rejected["success"])
        record = gateway.load_device_configuration(DEVICE_ID)
        self.assertEqual(record["config_version"], 1)
        self.assertEqual(record["config_hash"], applied["config_hash"])
        self.assertEqual(record["status"], "applied")
        self.assertEqual(gateway.load_device_metadata(DEVICE_ID)["schema"], schema)
        self.assertEqual(gateway.device_states[DEVICE_ID], {"enabled": True})
        self.assertTrue(socket.outgoing.empty())

    async def test_validated_nvs_report_recovers_gateway_configuration_after_database_loss(self):
        configuration, schema = validated_configuration([gpio_component()])
        gateway.set_device_trust_policy(DEVICE_ID, "allow")
        socket, _ = self.connect(schema, version=7)
        await socket.receive("hello_ack")
        await socket.receive("device_config_request")
        canonical = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        report = {
            "type": "device_config_report", "device": DEVICE_ID,
            "config_version": 7, "config_hash": "0" * 64,
            "schema_version": 7, "configuration": configuration, "schema": schema,
        }
        socket.feed(report)
        await self.until(lambda: any(event["event_type"] == "configuration"
                                    for event in gateway.get_recent_events()))
        self.assertIsNone(gateway.load_device_configuration(DEVICE_ID))
        socket.feed({**report, "config_hash": digest})
        await self.until(lambda: self.configuration_status() == "applied")
        record = gateway.load_device_configuration(DEVICE_ID)
        self.assertEqual(record["config_version"], 7)
        self.assertEqual(record["config_hash"], digest)
        self.assertEqual(record["config"], configuration)
        self.state(socket, {"enabled": False})
        await self.until(lambda: any(
            item["device"] == DEVICE_ID and not item["configuration_sync_pending"]
            for item in gateway.get_all_devices()
        ))
        exported = json.loads((gateway.DEVICE_CONFIGURATION_EXPORT_DIR / f"{DEVICE_ID}.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["hardware_configuration"], configuration)
        self.assertEqual(exported["configuration_status"], "applied")


if __name__ == "__main__":
    unittest.main()
