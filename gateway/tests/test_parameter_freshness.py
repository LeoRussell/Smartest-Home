"""Published parameter ages must describe samples, not unrelated device traffic."""

import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway
from test_ds18b20_configuration import Socket
from test_universal_lifecycle import MemorySocket


DEVICE = "freshness-sensor"
SCHEMA = {
    "parameters": {
        "temperature": {"type": "number", "access": "read_only", "unit": "°C"},
        "power": {"type": "boolean", "access": "read_write"},
    },
    "actions": {},
}


class SynchronousParameterFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.gateway.save_device_metadata(DEVICE, {
            "schema": SCHEMA, "schema_version": 1, "trust_policy": "allow",
        })

    def test_no_current_sample_needs_no_event_loop_and_never_uses_saved_dates(self):
        self.gateway.save_device_state(DEVICE, {"temperature": 23.5, "power": True})
        self.assertEqual(self.gateway.parameter_ages_seconds(DEVICE), {"temperature": None, "power": None})
        # Offline device_list can load its Schema from SQLite, even with no
        # runtime metadata. The saved reading is intentionally not a fresh one.
        self.gateway.device_metadata.clear()
        item = next(d for d in self.gateway.get_all_devices() if d["device"] == DEVICE)
        self.assertEqual(item["parameters"]["temperature"], 23.5)
        self.assertEqual(item["parameter_ages_seconds"], {"temperature": None, "power": None})

    def test_synchronous_caller_does_not_interpret_timestamps_from_another_loop(self):
        self.gateway.devices[DEVICE] = Socket()
        self.gateway.fresh_device_parameters[DEVICE] = {"temperature", "power"}
        self.gateway.last_parameter_telemetry[DEVICE] = {"temperature": 1.0, "power": 2.0}
        self.assertEqual(self.gateway.parameter_ages_seconds(DEVICE), {"temperature": None, "power": None})


class ParameterFreshnessProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.gateway.save_device_metadata(DEVICE, {
            "schema": SCHEMA, "schema_version": 1, "trust_policy": "allow",
            "command_dedup": True, "command_replay_guard": True,
        })
        self.browser = Socket()
        self.gateway.browser_clients.add(self.browser)
        self.sockets = []
        self.handlers = []

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        for handler in self.handlers:
            await asyncio.wait_for(handler, timeout=3)
        tasks = [pending["timeout_task"] for pending in self.gateway.pending_commands.values()
                 if pending.get("timeout_task") is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0)

    async def connect(self):
        socket = MemorySocket()
        self.sockets.append(socket)
        self.handlers.append(asyncio.create_task(self.gateway.handle_client(socket)))
        socket.feed({
            "type": "hello", "device": DEVICE, "schema": SCHEMA, "schema_version": 1,
            "protocol_version": 2, "command_dedup": True, "command_replay_guard": True,
        })
        await socket.receive("hello_ack")
        return socket

    def state_messages(self):
        return [m for m in self.browser.messages if m["type"] == "device_state" and m["device"] == DEVICE]

    async def state(self, socket, **parameters):
        before = len(self.state_messages())
        socket.feed({"type": "state", "device": DEVICE, "data": {"parameters": parameters}})
        await self.until(lambda: len(self.state_messages()) > before)
        return self.state_messages()[-1]

    def device_item(self):
        return next(d for d in self.gateway.get_all_devices() if d["device"] == DEVICE)

    def assert_list_matches_state(self, message):
        published = message["parameter_ages_seconds"]
        listed = self.device_item()["parameter_ages_seconds"]
        self.assertEqual(set(published), set(SCHEMA["parameters"]))
        self.assertEqual(set(listed), set(published))
        for key, value in published.items():
            if value is None:
                self.assertIsNone(listed[key])
            else:
                self.assertGreaterEqual(listed[key], value)
                self.assertLess(listed[key] - value, 3)

    async def test_partial_state_and_empty_heartbeat_do_not_refresh_temperature(self):
        socket = await self.connect()
        self.assertEqual(self.device_item()["parameter_ages_seconds"], {"temperature": None, "power": None})
        await self.state(socket, temperature=23.5, power=False)
        old = asyncio.get_running_loop().time() - 60
        self.gateway.last_parameter_telemetry[DEVICE]["temperature"] = old
        partial = await self.state(socket, power=True)
        self.assertEqual(partial["parameters"], {"temperature": 23.5, "power": True})
        self.assertGreaterEqual(partial["parameter_ages_seconds"]["temperature"], 60)
        self.assertLess(partial["parameter_ages_seconds"]["power"], self.gateway.DEVICE_STALE_SECONDS)
        self.assertFalse(self.gateway.parameter_value_is_fresh(DEVICE, "temperature"))
        self.assertTrue(self.gateway.parameter_value_is_fresh(DEVICE, "power"))
        self.assertEqual(self.gateway.last_parameter_telemetry[DEVICE]["temperature"], old)
        self.assert_list_matches_state(partial)
        timestamps = dict(self.gateway.last_parameter_telemetry[DEVICE])
        heartbeat = await self.state(socket)
        self.assertEqual(self.gateway.last_parameter_telemetry[DEVICE], timestamps)
        self.assertGreaterEqual(heartbeat["parameter_ages_seconds"]["temperature"], partial["parameter_ages_seconds"]["temperature"])
        self.assert_list_matches_state(heartbeat)

    async def test_reconnect_keeps_cached_reading_but_resets_sample_ages(self):
        socket = await self.connect()
        await self.state(socket, temperature=23.5, power=False)
        await socket.close()
        await self.handlers[-1]
        self.assertEqual(self.gateway.parameter_ages_seconds(DEVICE), {"temperature": None, "power": None})
        reconnect = await self.connect()
        item = self.device_item()
        self.assertEqual(item["parameters"]["temperature"], 23.5)
        self.assertEqual(item["parameter_ages_seconds"], {"temperature": None, "power": None})
        partial = await self.state(reconnect, power=True)
        self.assertIsNone(partial["parameter_ages_seconds"]["temperature"])
        self.assert_list_matches_state(partial)
        converted = await self.state(reconnect, temperature=24.0625)
        self.assertLess(converted["parameter_ages_seconds"]["temperature"], self.gateway.DEVICE_STALE_SECONDS)
        self.assert_list_matches_state(converted)

    async def test_replayed_ack_does_not_make_a_cached_result_new_telemetry(self):
        socket = await self.connect()
        await self.state(socket, temperature=23.5, power=False)
        now = asyncio.get_running_loop().time()
        self.gateway.last_parameter_telemetry[DEVICE] = {"temperature": now - 60, "power": now - 10}
        timestamps = dict(self.gateway.last_parameter_telemetry[DEVICE])
        self.assertTrue(await self.gateway.dispatch_device_command(DEVICE, "power", True, requested_command_id="cached-power"))
        await socket.receive("command")
        before = len(self.state_messages())
        socket.feed({
            "type": "command_result", "device": DEVICE, "command_id": "cached-power",
            "success": True, "replayed": True, "data": {"parameter": "power", "value": True},
        })
        await self.until(lambda: not self.gateway.pending_commands)
        self.assertEqual(self.gateway.last_parameter_telemetry[DEVICE], timestamps)
        self.assertEqual(len(self.state_messages()), before)
        self.assertIs(self.gateway.device_states[DEVICE]["power"], False)
        heartbeat = await self.state(socket)
        self.assertGreaterEqual(heartbeat["parameter_ages_seconds"]["temperature"], 60)
        self.assertGreaterEqual(heartbeat["parameter_ages_seconds"]["power"], 10)
        self.assert_list_matches_state(heartbeat)

    async def test_non_replayed_ack_refreshes_only_its_parameter_age(self):
        socket = await self.connect()
        await self.state(socket, temperature=23.5, power=False)
        old = asyncio.get_running_loop().time() - 60
        self.gateway.last_parameter_telemetry[DEVICE]["temperature"] = old
        self.assertTrue(await self.gateway.dispatch_device_command(DEVICE, "power", True, requested_command_id="new-power"))
        await socket.receive("command")
        before = len(self.state_messages())
        socket.feed({
            "type": "command_result", "device": DEVICE, "command_id": "new-power",
            "success": True, "data": {"parameter": "power", "value": True},
        })
        await self.until(lambda: len(self.state_messages()) > before)
        message = self.state_messages()[-1]
        self.assertTrue(message["parameters"]["power"])
        self.assertEqual(self.gateway.last_parameter_telemetry[DEVICE]["temperature"], old)
        self.assertGreaterEqual(message["parameter_ages_seconds"]["temperature"], 60)
        self.assertLess(message["parameter_ages_seconds"]["power"], self.gateway.DEVICE_STALE_SECONDS)
        self.assert_list_matches_state(message)


if __name__ == "__main__":
    unittest.main()
