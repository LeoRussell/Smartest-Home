"""A missing optional thermometer must not lock unrelated, confirmed outputs."""

import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from device_configuration import DRIVERS, validate_device_configuration
from runtime import Gateway
from test_ds18b20_configuration import C3, Socket, configuration, sensor
from test_universal_lifecycle import MemorySocket


DEVICE = "sensor-lifecycle"


def output():
    return {
        "id": "lamp", "driver": "gpio_output", "label": "Lamp",
        "parameter_id": "enabled", "parameter_label": "Enabled", "pin": 1,
        "active_high": True, "initial_value": False,
    }


class DS18B20LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.gateway.set_device_trust_policy(DEVICE, "allow")
        self.gateway.fire_automation_event = Mock()
        self.sockets = []
        self.handlers = []
        _, self.empty_schema, _ = validate_device_configuration(configuration(), list(DRIVERS), C3)

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        for handler in self.handlers:
            await asyncio.wait_for(handler, timeout=3)
        tasks = list(self.gateway.configuration_apply_tasks.values())
        tasks += [pending["timeout_task"] for pending in self.gateway.pending_commands.values()
                  if pending.get("timeout_task") is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0)

    async def connect(self, schema=None, version=1):
        socket = MemorySocket()
        self.sockets.append(socket)
        self.handlers.append(asyncio.create_task(self.gateway.handle_client(socket)))
        socket.feed({
            "type": "hello", "device": DEVICE, "name": "Sensor lifecycle",
            "protocol_version": 2, "schema_version": version,
            "schema": schema or self.empty_schema, "firmware_version": "test",
            "command_dedup": True, "command_replay_guard": True,
            "device_profile": "universal", "board_profile": C3,
            "driver_catalog": list(DRIVERS),
        })
        await socket.receive("hello_ack")
        return socket

    async def prepare(self, *components):
        socket = await self.connect()
        value, schema, error = validate_device_configuration(configuration(*components), list(DRIVERS), C3)
        self.assertIsNone(error)
        browser = Socket()
        await self.gateway.handle_browser_message(browser, {
            "action": "save_device_configuration", "device": DEVICE,
            "configuration": value,
        })
        result = next(m for m in browser.messages if m["type"] == "device_configuration_save_result")
        self.assertTrue(result["success"], result)
        applied = await socket.receive("device_config_apply")
        return socket, schema, applied

    async def acknowledge(self, socket, schema, applied):
        socket.feed({
            "type": "device_config_result", "device": DEVICE,
            "config_version": applied["config_version"], "config_hash": applied["config_hash"],
            "schema_version": applied["config_version"], "schema": schema, "success": True,
        })
        await self.until(lambda: self.gateway.load_device_configuration(DEVICE)["status"] == "applied")

    def state(self, socket, **values):
        socket.feed({"type": "state", "device": DEVICE, "data": {"parameters": values}})

    def public_pending(self):
        return next(d for d in self.gateway.get_all_devices() if d["device"] == DEVICE)["configuration_sync_pending"]

    async def test_mixed_device_unlocks_only_after_ack_and_first_output_value(self):
        socket, schema, applied = await self.prepare(output(), sensor())
        self.assertTrue(self.gateway.device_configuration_is_pending(DEVICE))
        self.assertTrue(self.public_pending())
        self.assertFalse(await self.gateway.dispatch_device_command(DEVICE, "enabled", True))
        await self.acknowledge(socket, schema, applied)
        self.assertTrue(self.gateway.device_configuration_is_pending(DEVICE))
        self.assertTrue(self.public_pending())
        self.assertFalse(await self.gateway.dispatch_device_command(DEVICE, "enabled", True))
        self.state(socket, enabled=False)
        await self.until(lambda: not self.gateway.device_configuration_is_pending(DEVICE))
        self.assertFalse(self.public_pending())
        self.assertFalse(self.gateway.parameter_value_is_fresh(DEVICE, "temperature_c"))
        self.assertNotIn("temperature_c", self.gateway.device_states[DEVICE])
        self.assertIn(DEVICE, self.gateway.deferred_connection_automations)
        self.gateway.fire_automation_event.assert_not_called()
        # A simultaneous Schema review is still an independent command gate.
        self.gateway.pending_device_schemas[DEVICE] = {"schema": schema}
        self.assertFalse(await self.gateway.dispatch_device_command(DEVICE, "enabled", True))
        self.gateway.pending_device_schemas.pop(DEVICE)
        self.assertTrue(await self.gateway.dispatch_device_command(DEVICE, "enabled", True, requested_command_id="output-command"))
        command = await socket.receive("command")
        self.assertEqual(command["data"]["command_id"], "output-command")
        socket.feed({
            "type": "command_result", "device": DEVICE, "command_id": "output-command",
            "success": True, "data": {"parameter": "enabled", "value": True},
        })
        await self.until(lambda: not self.gateway.pending_commands)
        self.state(socket, enabled=True, temperature_c=23.4375)
        await self.until(lambda: DEVICE not in self.gateway.deferred_connection_automations)
        self.gateway.fire_automation_event.assert_called_once_with("device_connected", DEVICE)
        self.state(socket, enabled=True, temperature_c=23.4375)
        await asyncio.sleep(0)
        self.assertEqual(self.gateway.fire_automation_event.call_count, 1)

    async def test_missing_sensor_only_configuration_has_no_fabricated_reading(self):
        socket, schema, applied = await self.prepare(sensor())
        self.assertTrue(self.public_pending())
        await self.acknowledge(socket, schema, applied)
        self.assertFalse(self.gateway.device_configuration_is_pending(DEVICE))
        self.assertFalse(self.public_pending())
        self.assertIn(DEVICE, self.gateway.deferred_connection_automations)
        self.assertFalse(self.gateway.parameter_value_is_fresh(DEVICE, "temperature_c"))
        self.assertNotIn("temperature_c", self.gateway.device_states.get(DEVICE, {}))
        self.assertNotIn("temperature_c", next(d for d in self.gateway.get_all_devices() if d["device"] == DEVICE)["parameters"])
        self.assertFalse(await self.gateway.dispatch_device_command(DEVICE, "temperature_c", 0))
        self.gateway.fire_automation_event.assert_not_called()

    async def test_legacy_drivers_still_wait_for_all_initial_values(self):
        button = {
            "id": "button", "driver": "digital_input", "label": "Button",
            "parameter_id": "pressed", "parameter_label": "Pressed", "pin": 4,
            "interval_ms": 1000, "pull": "none",
        }
        socket, schema, applied = await self.prepare(output(), button)
        await self.acknowledge(socket, schema, applied)
        self.state(socket, enabled=False)
        await self.until(lambda: self.gateway.parameter_value_is_fresh(DEVICE, "enabled"))
        self.assertTrue(self.gateway.device_configuration_is_pending(DEVICE))
        self.assertTrue(self.public_pending())
        self.assertFalse(await self.gateway.dispatch_device_command(DEVICE, "enabled", True))
        self.gateway.fire_automation_event.assert_not_called()
        self.state(socket, pressed=False)
        await self.until(lambda: DEVICE not in self.gateway.deferred_connection_automations)
        self.assertFalse(self.gateway.device_configuration_is_pending(DEVICE))
        self.assertFalse(self.public_pending())
        self.gateway.fire_automation_event.assert_called_once_with("device_connected", DEVICE)

    async def test_recovered_manual_command_does_not_wait_forever_for_sensor(self):
        socket, schema, applied = await self.prepare(output(), sensor())
        await self.acknowledge(socket, schema, applied)
        self.state(socket, enabled=False)
        await self.until(lambda: not self.gateway.device_configuration_is_pending(DEVICE))
        self.assertTrue(await self.gateway.dispatch_device_command(DEVICE, "enabled", True, requested_command_id="recover-output"))
        first_command = await socket.receive("command")
        await socket.close()
        await self.handlers[-1]
        self.assertEqual(self.gateway.pending_commands["recover-output"]["status"], "queued")
        self.gateway.fire_automation_event.reset_mock()
        reconnect = await self.connect(schema, version=applied["config_version"])
        reapplied = await reconnect.receive("device_config_apply")
        await self.acknowledge(reconnect, schema, reapplied)
        self.state(reconnect, enabled=True)
        recovered = await reconnect.receive("command")
        self.assertEqual(recovered["data"]["command_id"], first_command["data"]["command_id"])
        self.assertTrue(recovered["data"]["require_cached_result"])
        self.assertFalse(self.gateway.parameter_value_is_fresh(DEVICE, "temperature_c"))
        self.assertIn(DEVICE, self.gateway.deferred_connection_automations)
        self.assertFalse(self.public_pending())
        self.gateway.fire_automation_event.assert_not_called()
        reconnect.feed({
            "type": "command_result", "device": DEVICE, "command_id": "recover-output",
            "success": True, "replayed": True,
            "data": {"parameter": "enabled", "value": True},
        })
        await self.until(lambda: not self.gateway.pending_commands)


if __name__ == "__main__":
    unittest.main()
