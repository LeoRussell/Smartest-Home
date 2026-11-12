"""Slow browser I/O must not corrupt confirmed device commands or other peers."""

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class Peer:
    remote_address = ("isolated-lifecycle-test", 0)
    close_code = None
    close_reason = ""

    def __init__(self, messages=()):
        self.messages = []
        self.incoming = messages
        self.closed = []
        self.sent = asyncio.Event()

    def __aiter__(self):
        async def frames():
            for message in self.incoming:
                yield json.dumps(message)
        return frames()

    async def send(self, message):
        self.messages.append(json.loads(message))
        self.sent.set()

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class TransportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()

    async def test_valid_ack_claims_command_before_broadcast_can_race_its_timeout(self):
        gateway = self.gateway
        schema = {"parameters": {"power": {"type": "boolean", "access": "read_write"}}, "actions": {}}
        gateway.save_device_metadata("lamp", {"schema": schema, "schema_version": 1, "trust_policy": "allow"})
        pending = {
            "command_id": "ack-before-timeout", "device": "lamp", "browser": None,
            "operation": {"kind": "parameter", "name": "power", "value": True},
            "payload": {}, "attempts": 1, "status": "sent", "timeout_task": None,
        }
        gateway.create_command_queue_record(pending)
        gateway.pending_commands[pending["command_id"]] = pending
        peer = Peer([
            {"type": "hello", "device": "lamp", "schema": schema, "schema_version": 1},
            {"type": "command_result", "device": "lamp", "command_id": pending["command_id"],
             "success": True, "data": {"parameter": "power", "value": True}},
            {"type": "state", "device": "lamp", "data": {"parameters": {"power": False}}},
        ])
        raced = []

        async def slow_state_broadcast(message):
            if message.get("type") == "device_state" and message.get("parameters", {}).get("power") is True:
                # Reproduce a deadline firing while the ACK path yields to a browser.
                task = asyncio.create_task(gateway.expire_pending_command(pending["command_id"]))
                await task
                raced.append(True)

        with patch.object(gateway, "broadcast", side_effect=slow_state_broadcast), patch.object(gateway, "COMMAND_TIMEOUT_SECONDS", 0):
            await gateway.handle_client(peer)
        self.assertEqual(raced, [True])
        self.assertIs(gateway.device_states["lamp"]["power"], False, "The handler must still process the state after ACK")
        with gateway.database_connection() as connection:
            row = connection.execute("SELECT status FROM command_queue WHERE command_id = ?", (pending["command_id"],)).fetchone()
        self.assertEqual(row[0], "succeeded")

    async def test_broadcast_delivers_to_healthy_peer_before_slow_peer_recovers(self):
        gateway = self.gateway
        blocked = asyncio.Event()

        class Slow(Peer):
            async def send(self, message):
                await blocked.wait()

        slow, healthy = Slow(), Peer()
        # Fixed order makes the old sequential implementation fail deterministically.
        class OrderedSet(set):
            def __iter__(self):
                return iter((slow, healthy))
        gateway.browser_clients = OrderedSet((slow, healthy))
        task = asyncio.create_task(gateway.broadcast({"type": "test"}))
        try:
            await asyncio.wait_for(healthy.sent.wait(), timeout=0.2)
            self.assertEqual(healthy.messages, [{"type": "test"}])
        finally:
            blocked.set()
            await task

    async def test_stalled_browser_send_is_bounded_and_other_clients_remain(self):
        gateway = self.gateway
        cancelled = asyncio.Event()

        class Stalled(Peer):
            async def send(self, message):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        slow, healthy = Stalled(), Peer()
        gateway.browser_clients.update((slow, healthy))
        with patch.object(gateway, "BROWSER_SEND_TIMEOUT_SECONDS", 0.01, create=True):
            await asyncio.wait_for(gateway.broadcast({"type": "test"}), timeout=0.2)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(slow, gateway.browser_clients)
        self.assertIn(healthy, gateway.browser_clients)
        self.assertEqual(healthy.messages, [{"type": "test"}])

    async def test_replayed_ack_finishes_delivery_without_replacing_current_state(self):
        gateway = self.gateway
        schema = {"parameters": {"power": {"type": "boolean", "access": "read_write"}}, "actions": {}}
        gateway.save_device_metadata("lamp", {"schema": schema, "schema_version": 1, "trust_policy": "allow"})
        browser = Peer()
        future = asyncio.get_running_loop().create_future()
        pending = {
            "command_id": "old-applied-set", "device": "lamp", "browser": browser,
            "operation": {"kind": "parameter", "name": "power", "value": True},
            "payload": {}, "attempts": 2, "status": "sent", "timeout_task": None,
            "automation_id": "scenario", "automation_run_id": "old-run", "result_future": future,
        }
        gateway.create_command_queue_record(pending)
        gateway.pending_commands[pending["command_id"]] = pending
        peer = Peer([
            {"type": "hello", "device": "lamp", "schema": schema, "schema_version": 1},
            {"type": "state", "device": "lamp", "data": {"parameters": {"power": False}}},
            {"type": "command_result", "device": "lamp", "command_id": pending["command_id"],
             "success": True, "replayed": True, "data": {"parameter": "power", "value": True}},
        ])
        changes = []

        async def observe_changes(device, previous, current):
            changes.append(dict(current))

        with patch.object(gateway, "evaluate_automations", side_effect=observe_changes), patch.object(gateway, "record_device_telemetry", wraps=gateway.record_device_telemetry) as telemetry:
            await gateway.handle_client(peer)
        telemetry.assert_called_once_with("lamp", {"power": False})
        self.assertIs(gateway.device_states["lamp"]["power"], False)
        self.assertTrue(all(change["power"] is False for change in changes))
        reply = next(message for message in browser.messages if message["type"] == "command_result")
        self.assertTrue(reply["success"], reply)
        self.assertTrue(reply["replayed"])
        self.assertTrue(future.result()["replayed"])
        with gateway.database_connection() as connection:
            samples = connection.execute("SELECT value FROM parameter_history").fetchall()
            stored = connection.execute("SELECT status, result_json FROM command_queue WHERE command_id = ?", (pending["command_id"],)).fetchone()
        self.assertEqual(samples, [(0.0,)])
        self.assertEqual(stored[0], "succeeded")
        self.assertTrue(json.loads(stored[1])["replayed"])
        for event in gateway.get_event_page()[0]:
            if event["event_type"] == "parameter":
                self.assertIsNot(json.loads(event["event_value"])["value"], True)

    async def test_failed_ack_without_a_reading_preserves_device_error(self):
        gateway = self.gateway
        schema = {"parameters": {"power": {"type": "boolean", "access": "read_write"}}, "actions": {}}
        gateway.save_device_metadata("lamp", {"schema": schema, "schema_version": 1, "trust_policy": "allow"})
        for index, extra in enumerate(({}, {"value": None})):
            with self.subTest(extra=extra):
                browser = Peer()
                pending = {
                    "command_id": f"failed-without-value-{index}", "device": "lamp", "browser": browser,
                    "operation": {"kind": "parameter", "name": "power", "value": True},
                    "payload": {}, "attempts": 1, "status": "sent", "timeout_task": None,
                }
                gateway.create_command_queue_record(pending)
                gateway.pending_commands[pending["command_id"]] = pending
                peer = Peer([
                    {"type": "hello", "device": "lamp", "schema": schema, "schema_version": 1},
                    {"type": "state", "device": "lamp", "data": {"parameters": {"power": False}}},
                    {"type": "command_result", "device": "lamp", "command_id": pending["command_id"],
                     "success": False, "data": {"parameter": "power", "error": "Actuator reported a fault", **extra}},
                ])
                with patch.object(gateway, "record_device_telemetry", wraps=gateway.record_device_telemetry) as telemetry:
                    await gateway.handle_client(peer)
                telemetry.assert_called_once_with("lamp", {"power": False})
                result = next(message for message in browser.messages if message["type"] == "command_result")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "Actuator reported a fault")
                self.assertIsNone(result["value"])
                self.assertIs(gateway.device_states["lamp"]["power"], False)


if __name__ == "__main__":
    unittest.main()
