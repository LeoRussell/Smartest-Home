"""Management flows over real loopback WebSockets and isolated SQLite files."""

import asyncio
import json
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import websockets
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Gateway


class JsonPeer:
    def __init__(self, socket, on_message=None):
        self.socket = socket
        self.on_message = on_message
        self.incoming = asyncio.Queue()
        self.received = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for payload in self.socket:
                message = json.loads(payload)
                self.received.append(message)
                if self.on_message is not None:
                    await self.on_message(message)
                self.incoming.put_nowait(message)
        except ConnectionClosed:
            pass

    async def send(self, message):
        await self.socket.send(json.dumps(message))

    async def receive(self, kind, predicate=None):
        async def matching():
            while True:
                message = await self.incoming.get()
                if message.get("type") == kind and (predicate is None or predicate(message)):
                    return message
        return await asyncio.wait_for(matching(), timeout=5)

    async def close(self, code=1000, reason=""):
        await self.socket.close(code=code, reason=reason)
        await self.reader


class SimulatedDevice:
    def __init__(self, device_id, parameter, value, schema):
        self.device_id = device_id
        self.parameter = parameter
        self.value = value
        self.schema = schema
        self.health = {
            "wifi_rssi": -61,
            "uptime_seconds": 41,
            "reset_reason": "power_on",
            "last_disconnect_reason": None,
        }
        self.commands = []
        self.ack_commands = True
        self.peer = None

    async def on_message(self, message):
        if message.get("type") != "command":
            return
        self.commands.append(message)
        if not self.ack_commands:
            return
        operation = message["data"]
        if operation["command"] != "set" or operation["parameter"] != self.parameter:
            raise AssertionError(f"Unexpected simulated command: {operation}")
        self.value = operation["value"]
        await self.peer.send({
            "type": "command_result", "device": self.device_id,
            "command_id": operation["command_id"], "success": True,
            "data": {"parameter": self.parameter, "value": self.value},
        })
        await self.state()

    async def state(self):
        await self.peer.send({
            "type": "state", "device": self.device_id,
            "data": {"parameters": {self.parameter: self.value}},
            "health": self.health,
        })


class ManagementProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.server = await websockets.serve(
            self.gateway.handle_client, "127.0.0.1", 0,
            max_size=64 * 1024, ping_interval=None,
        )
        port = self.server.sockets[0].getsockname()[1]
        self.address = f"ws://127.0.0.1:{port}"
        self.peers = []
        self.connection_sequence = 0
        self.browser = await self.open_peer()
        await self.browser.send({"type": "browser_hello"})
        await self.browser.receive("gateway_settings")

    async def asyncTearDown(self):
        await self.gateway.management.shutdown()
        await asyncio.gather(*(peer.close() for peer in self.peers), return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        timeouts = [pending["timeout_task"] for pending in self.gateway.pending_commands.values()
                    if pending.get("timeout_task") is not None]
        for task in timeouts:
            task.cancel()
        if timeouts:
            await asyncio.gather(*timeouts, return_exceptions=True)

    async def open_peer(self, on_message=None):
        peer = JsonPeer(await websockets.connect(self.address), on_message)
        self.peers.append(peer)
        return peer

    async def action(self, action, response_type, **fields):
        request_id = uuid.uuid4().hex
        await self.browser.send({
            "type": "browser_message", "action": action,
            "request_id": request_id, **fields,
        })
        return await self.browser.receive(
            response_type, lambda message: message.get("request_id") == request_id,
        )

    async def connect_device(self, device_id="lamp", *, approved=False):
        is_fan = device_id == "fan"
        parameter = "speed" if is_fan else "enabled"
        definition = {"type": "integer", "access": "read_write", "min": 0, "max": 100} if is_fan else {
            "type": "boolean", "access": "read_write",
        }
        schema = {"parameters": {parameter: definition}, "actions": {}}
        if not is_fan:
            schema["common_commands"] = {
                "off": {"kind": "parameter", "name": parameter, "value": False},
            }
        device = SimulatedDevice(device_id, parameter, 60 if is_fan else True, schema)
        device.health["uptime_seconds"] += self.connection_sequence
        self.connection_sequence += 1
        device.peer = await self.open_peer(device.on_message)
        await device.peer.send({
            "type": "hello", "device": device_id, "name": device_id,
            "protocol_version": 2, "firmware_version": "integration-test",
            "schema_version": 1, "command_dedup": False,
            "schema": schema, "health": device.health,
        })
        if not approved:
            offered = await self.browser.receive(
                "device_access_requests",
                lambda message: any(request["device"] == device_id for request in message["requests"]),
            )
            request = next(request for request in offered["requests"] if request["device"] == device_id)
            await self.browser.send({
                "type": "browser_message", "action": "device_access_decision",
                "device": device_id, "request_id": request["request_id"],
                "decision": "allow", "remember": True,
            })
            decision = await self.browser.receive(
                "device_access_decision_result", lambda message: message.get("device") == device_id,
            )
            self.assertTrue(decision["success"])
        registration = await device.peer.receive("hello_ack")
        self.assertEqual(registration["schema_status"], "saved")
        # At this point the fake device has not sent state: this proves hello
        # diagnostics were persisted after admission, independently of telemetry.
        self.assertEqual(
            self.gateway.management.health_for_device(device_id)["uptime_seconds"],
            device.health["uptime_seconds"],
        )
        await device.state()
        # A unique uptime per connection excludes buffered messages from an
        # earlier connection when this helper verifies post-restore reconnects.
        await self.browser.receive(
            "device_health", lambda message: message.get("device") == device_id
            and message["health"].get("uptime_seconds") == device.health["uptime_seconds"],
        )
        await self.browser.receive("device_state", lambda message: message.get("device") == device_id)
        return device

    async def make_group(self, name, *devices):
        saved = await self.action("save_device_group", "group_update_result", name=name)
        self.assertTrue(saved["success"])
        group_id = saved["group_id"]
        for device in devices:
            result = await self.action("set_device_group", "group_update_result", device=device, group_id=group_id)
            self.assertTrue(result["success"])
        return group_id

    async def map_fan_off(self):
        result = await self.action(
            "save_common_command", "common_command_save_result", device="fan", command="off",
            operation={"kind": "parameter", "name": "speed", "value": 0},
        )
        self.assertTrue(result["success"])

    async def wait_offline(self, device_id):
        await self.browser.receive(
            "device_list", lambda message: any(
                device["device"] == device_id and not device["connected"]
                for device in message["devices"]
            ),
        )

    async def test_health_hello_live_state_and_persisted_close_reason(self):
        lamp = await self.connect_device()
        initial = self.gateway.management.health_for_device("lamp")
        self.assertEqual((initial["wifi_rssi"], initial["uptime_seconds"]), (-61, 41))
        self.assertEqual(initial["reset_reason"], "power_on")
        lamp.health.update({
            "wifi_rssi": -76, "uptime_seconds": 77,
            "last_disconnect_reason": "wifi: BEACON_TIMEOUT (200)",
        })
        await lamp.state()
        update = await self.browser.receive(
            "device_health", lambda message: message.get("device") == "lamp"
            and message["health"].get("uptime_seconds") == 77,
        )
        self.assertEqual(update["health"]["wifi_rssi"], -76)
        self.assertIn("BEACON_TIMEOUT", update["health"]["last_disconnect_reason"])
        self.assertEqual(set(self.gateway.device_states["lamp"]), {"enabled"})
        self.assertTrue(self.gateway.parameter_value_is_fresh("lamp", "enabled"))
        await lamp.peer.close(code=1001, reason="integration link closed")
        await self.wait_offline("lamp")
        with self.gateway.database_connection() as connection:
            persisted = connection.execute(
                "SELECT last_disconnect_reason, last_disconnect_at FROM device_health WHERE device_id = 'lamp'"
            ).fetchone()
        self.assertIn("1001", persisted[0])
        self.assertIn("integration link closed", persisted[0])
        self.assertIsNotNone(persisted[1])
        displayed = self.gateway.get_all_devices()[0]
        self.assertFalse(displayed["connected"])
        self.assertIn("BEACON_TIMEOUT", displayed["health"]["last_disconnect_reason"])

    async def test_group_scope_real_ack_types_and_offline_summary(self):
        lamp = await self.connect_device()
        fan = await self.connect_device("fan")
        await self.map_fan_off()
        group_id = await self.make_group("Only lamp", "lamp")
        scoped = await self.action(
            "execute_common_command", "common_command_result", command="off", group_id=group_id,
        )
        self.assertEqual(scoped["counts"], {"succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual([item["device"] for item in scoped["results"]], ["lamp"])
        self.assertEqual(fan.commands, [])
        all_devices = await self.action("execute_common_command", "common_command_result", command="off")
        self.assertEqual(all_devices["counts"], {"succeeded": 2, "failed": 0, "skipped": 0})
        self.assertIs(lamp.commands[-1]["data"]["value"], False)
        self.assertIs(type(fan.commands[-1]["data"]["value"]), int)
        self.assertEqual(fan.commands[-1]["data"]["value"], 0)
        with self.gateway.database_connection() as connection:
            statuses = connection.execute("SELECT status FROM command_queue").fetchall()
        self.assertEqual(statuses, [("succeeded",)] * 3)
        await fan.peer.close()
        await self.wait_offline("fan")
        partial = await self.action("execute_common_command", "common_command_result", command="off")
        self.assertEqual(partial["counts"], {"succeeded": 1, "failed": 0, "skipped": 1})
        by_device = {item["device"]: item["status"] for item in partial["results"]}
        self.assertEqual(by_device, {"lamp": "succeeded", "fan": "skipped"})
        self.assertEqual(len(fan.commands), 1)
        self.assertEqual(self.gateway.pending_commands, {})

    async def test_backup_restore_preserves_management_and_never_replays_bulk_command(self):
        lamp = await self.connect_device()
        await self.connect_device("fan")
        await self.map_fan_off()
        group_id = await self.make_group("Saved room", "lamp", "fan")
        setting = await self.action(
            "set_gateway_settings", "gateway_settings_result", event_retention_days=27,
        )
        self.assertTrue(setting["success"])
        lamp.ack_commands = False
        await self.browser.send({
            "type": "browser_message", "action": "execute_common_command",
            "command": "off", "group_id": group_id, "request_id": "pending-before-backup",
        })
        pending_message = await lamp.peer.receive("command")
        command_id = pending_message["data"]["command_id"]
        self.assertTrue(self.gateway.pending_commands[command_id]["operation"]["transient"])
        await self.browser.send({"type": "browser_message", "action": "create_database_backup"})
        backup = await self.browser.receive("database_backup_result")
        self.assertTrue(backup["success"])
        backup_id = backup["backup"]["backup_id"]
        self.assertTrue((self.gateway.DATABASE_BACKUP_DIR / backup_id).is_file())
        await self.action("save_device_group", "group_update_result", group_id=group_id, name="Changed")
        await self.action("save_common_command", "common_command_save_result", device="lamp", command="off", operation=None)
        await self.action("set_gateway_settings", "gateway_settings_result", event_retention_days=0)
        lamp.health["uptime_seconds"] = 999
        await lamp.state()
        await self.browser.receive("device_health", lambda message: message["health"].get("uptime_seconds") == 999)
        restore_message_start = len(self.browser.received)
        await self.browser.send({
            "type": "browser_message", "action": "restore_database_backup", "backup_id": backup_id,
        })
        restored = await self.browser.receive("database_backup_restore_result")
        self.assertTrue(restored["success"], restored.get("message"))
        restore_updates = self.browser.received[restore_message_start:]
        self.assertTrue(any(
            message.get("type") == "device_groups" and any(
                group["group_id"] == group_id and group["name"] == "Saved room"
                for group in message["groups"]
            ) for message in restore_updates
        ), "The connected browser did not receive restored group names")
        self.assertTrue(any(
            message.get("type") == "gateway_settings" and message.get("event_retention_days") == 27
            for message in restore_updates
        ), "The connected browser did not receive restored retention settings")
        self.assertEqual(self.gateway.management.settings()["event_retention_days"], 27)
        groups = self.gateway.management.groups()
        self.assertEqual([(group["name"], group["device_count"]) for group in groups], [("Saved room", 2)])
        devices = {device["device"]: device for device in self.gateway.get_all_devices()}
        self.assertEqual(devices["lamp"]["health"]["uptime_seconds"], 41)
        self.assertEqual(devices["lamp"]["group_id"], group_id)
        self.assertEqual(devices["lamp"]["common_command_sources"]["off"], "schema")
        self.assertEqual(devices["fan"]["common_commands"]["off"]["value"], 0)
        self.assertEqual(devices["fan"]["common_command_sources"]["off"], "custom")
        self.assertEqual(self.gateway.pending_commands, {})
        self.assertEqual(self.gateway.management.bulk_busy_devices, set())
        with self.gateway.database_connection() as connection:
            status = connection.execute("SELECT status FROM command_queue WHERE command_id = ?", (command_id,)).fetchone()[0]
        self.assertEqual(status, "unavailable")
        new_lamp = await self.connect_device(approved=True)
        new_fan = await self.connect_device("fan", approved=True)
        self.assertEqual(new_lamp.commands, [])
        self.assertEqual(new_fan.commands, [])
        self.assertEqual(self.gateway.pending_commands, {})


if __name__ == "__main__":
    unittest.main()
