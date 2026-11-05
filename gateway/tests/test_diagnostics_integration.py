"""Diagnostics and safe previews through real, isolated loopback WebSockets."""

import asyncio
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import Gateway
import test_management_protocol as management_helpers


class DiagnosticsIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        # The final hello response is a barrier for all startup work.
        await self.browser.receive("history_settings")

    async def asyncTearDown(self):
        await self.gateway.management.shutdown()
        await asyncio.gather(*(peer.close() for peer in self.peers), return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()

    async def open_peer(self, on_message=None):
        peer = management_helpers.JsonPeer(
            await websockets.connect(self.address, ping_interval=None), on_message,
        )
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
        return await management_helpers.ManagementProtocolTests.connect_device(
            self, device_id, approved=approved,
        )

    def snapshot(self):
        with self.gateway.database_connection() as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            records = {
                table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in tables
            }
        return (
            records, deepcopy(self.gateway.device_states),
            deepcopy(self.gateway.device_metadata),
            dict(self.gateway.devices), dict(self.gateway.pending_commands),
            dict(self.gateway.automation_tasks),
            dict(self.gateway.automation_trigger_tasks),
        )

    @contextmanager
    def prohibit_simulation_side_effects(self):
        """Keep reads available while making every real effect fail loudly."""
        original_send = self.gateway.send_json
        device_sockets = tuple(self.gateway.devices.values())

        async def browser_only_send(socket, message):
            if any(socket is device for device in device_sockets):
                raise AssertionError("Simulation sent a message to a physical-device socket")
            return await original_send(socket, message)

        @contextmanager
        def read_only_database():
            uri = self.gateway.DB_PATH.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.execute("PRAGMA query_only = ON")
                yield connection

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                self.gateway, "database_connection", read_only_database,
            ))
            send = stack.enter_context(patch.object(
                self.gateway, "send_json", side_effect=browser_only_send,
            ))
            forbidden = []
            for name in ("dispatch_device_command", "dispatch_device_action"):
                forbidden.append(stack.enter_context(patch.object(
                    self.gateway, name,
                    new=AsyncMock(side_effect=AssertionError(f"Physical dispatch: {name}")),
                )))
            for name in (
                "start_automation_run", "create_automation_run", "save_device_event",
                "save_device_state", "create_command_queue_record",
            ):
                forbidden.append(stack.enter_context(patch.object(
                    self.gateway, name, side_effect=AssertionError(f"Persistent effect: {name}"),
                )))
            yield
            self.assertGreater(send.call_count, 0)
            for operation in forbidden:
                operation.assert_not_called()

    @staticmethod
    def preview_config():
        enabled = {"type": "parameter", "device": "lamp", "parameter": "enabled"}
        return {
            "version": 2, "name": "Loopback preview", "workspace": {"blocks": {}},
            "program": {"version": 1, "trigger": {"type": "manual"}, "body": [
                {"type": "if", "block_id": "initial-state", "branches": [{
                    "condition": enabled, "then": [
                        {"type": "wait", "seconds": 3600, "block_id": "virtual-delay"},
                        {"type": "set_parameter", "device": "lamp", "parameter": "enabled",
                         "value": {"type": "constant", "value": False}, "block_id": "virtual-off"},
                    ],
                }], "else": []},
                {"type": "if", "block_id": "updated-state", "branches": [{
                    "condition": {"type": "not", "operand": enabled},
                    "then": [{"type": "wait", "seconds": 2, "block_id": "after-off"}],
                }], "else": []},
            ]},
        }

    async def test_capabilities_and_session_lifecycle_reach_browser_and_database(self):
        capabilities = [message for message in self.browser.received
                        if message.get("type") == "gateway_capabilities"]
        self.assertEqual(len(capabilities), 1)
        self.assertEqual(capabilities[0]["protocol_version"], 1)
        self.assertTrue({"connection_history", "automation_simulation"}.issubset(
            capabilities[0]["features"],
        ))

        lamp = await self.connect_device()
        active = await self.action(
            "get_device_connections", "device_connections", device="lamp",
        )
        self.assertTrue(active["success"], active)
        self.assertEqual(active["summary"]["session_count"], 1)
        self.assertTrue(active["summary"]["active"])
        session = active["sessions"][0]
        first_session = session["session_id"]
        self.assertEqual(session["status"], "active")
        self.assertEqual((session["wifi_rssi_start"], session["uptime_start"]), (-61, 41))
        self.assertEqual(session["reset_reason"], "power_on")
        self.assertIsNotNone(session["last_data_at"])

        lamp.health.update(wifi_rssi=-78, uptime_seconds=77,
                           last_disconnect_reason="wifi: BEACON_TIMEOUT (200)")
        lamp.value = False
        await lamp.state()
        await self.browser.receive(
            "device_health", lambda message: message.get("device") == "lamp"
            and message["health"].get("uptime_seconds") == 77,
        )
        await self.browser.receive(
            "device_state", lambda message: message.get("device") == "lamp"
            and message["parameters"].get("enabled") is False,
        )
        await lamp.peer.close(code=1001, reason="integration link closed")
        await management_helpers.ManagementProtocolTests.wait_offline(self, "lamp")
        closed = await self.action(
            "get_device_connections", "device_connections", device="lamp",
        )
        self.assertTrue(closed["success"], closed)
        self.assertFalse(closed["summary"]["active"])
        session = closed["sessions"][0]
        self.assertEqual(session["session_id"], first_session)
        self.assertEqual((session["status"], session["category"]), ("closed", "closed"))
        self.assertEqual(session["close_code"], 1001)
        self.assertEqual(session["close_reason"], "integration link closed")
        self.assertEqual((session["wifi_rssi_last"], session["wifi_rssi_min"]), (-78, -78))
        self.assertEqual(session["uptime_last"], 77)
        self.assertEqual(session["device_disconnect_reason"], "wifi: BEACON_TIMEOUT (200)")
        self.assertGreaterEqual(session["duration_seconds"], 0)
        self.assertIsNotNone(session["ended_at"])
        with self.gateway.database_connection() as connection:
            persisted = connection.execute(
                "SELECT status, close_code, close_reason FROM device_connection_sessions WHERE id = ?",
                (first_session,),
            ).fetchone()
        self.assertEqual(persisted, ("closed", 1001, "integration link closed"))

        await self.connect_device(approved=True)
        latest = await self.action(
            "get_device_connections", "device_connections", device="lamp", limit=1,
        )
        self.assertEqual(latest["summary"]["session_count"], 2)
        self.assertTrue(latest["summary"]["active"])
        self.assertTrue(latest["has_more"])
        self.assertNotEqual(latest["sessions"][0]["session_id"], first_session)
        older = await self.action(
            "get_device_connections", "device_connections", device="lamp", limit=1,
            before_id=latest["next_before_id"],
        )
        self.assertFalse(older["has_more"])
        self.assertEqual(older["sessions"][0]["session_id"], first_session)

    async def test_draft_simulation_uses_live_input_without_dispatch_or_database_writes(self):
        lamp = await self.connect_device()
        before = self.snapshot()
        with self.prohibit_simulation_side_effects():
            result = await self.action(
                "simulate_automation", "automation_simulation", config=self.preview_config(),
            )
        self.assertTrue(result["success"], result)
        self.assertTrue(result["simulated"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["elapsed_seconds"], 3602)
        self.assertEqual(result["inputs"], [{
            "device": "lamp", "parameter": "enabled", "value": True, "source": "live",
        }])
        off = next(step for step in result["steps"] if step["type"] == "set_parameter")
        self.assertEqual((off["block_id"], off["at_seconds"]), ("virtual-off", 3600))
        self.assertIs(off["details"]["value"], False)
        self.assertEqual(result["final_parameters"], [{
            "device": "lamp", "parameter": "enabled", "value": False,
        }])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(lamp.value)
        self.assertFalse(lamp.commands)

    async def test_saved_simulation_and_invalid_override_keep_browser_and_real_status_intact(self):
        lamp = await self.connect_device()
        saved, error = self.gateway.save_automation(
            "preview-only", self.preview_config(), enabled=False,
        )
        self.assertTrue(saved, error)
        before = self.snapshot()
        with self.prohibit_simulation_side_effects():
            rejected = await self.action(
                "simulate_automation", "automation_simulation", automation_id="preview-only",
                parameters=[{"device": "lamp", "parameter": "missing", "value": True}],
            )
            self.assertFalse(rejected["success"])
            result = await self.action(
                "simulate_automation", "automation_simulation", automation_id="preview-only",
                parameters=[{"device": "lamp", "parameter": "enabled", "value": False}],
                connections=[{"device": "lamp", "connected": True}],
            )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["automation_id"], "preview-only")
        self.assertEqual(result["elapsed_seconds"], 2)
        self.assertEqual(result["inputs"][0]["source"], "override")
        self.assertNotIn("set_parameter", [step["type"] for step in result["steps"]])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(lamp.value)
        self.assertFalse(lamp.commands)


if __name__ == "__main__":
    unittest.main()
