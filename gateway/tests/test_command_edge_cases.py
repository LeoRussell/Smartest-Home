"""Retries never execute evicted commands; stale delivery work cannot revive them."""

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class DeviceSocket(Socket):
    remote_address = ("isolated-command-edge", 0)
    close_code = None
    close_reason = ""

    def __init__(self, hello):
        super().__init__()
        self.hello = hello
        self.closed = []

    def __aiter__(self):
        async def messages():
            yield json.dumps(self.hello)
        return messages()

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class CommandEdgeCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.schema = {
            "parameters": {"power": {"type": "boolean", "access": "read_write"}},
            "actions": {"identify": {"label": "Identify", "arguments": {}}},
        }
        self.gateway.save_device_metadata("lamp", {
            "schema": self.schema, "schema_version": 1, "firmware_id": "wire-lamp",
            "command_dedup": True, "command_replay_guard": True,
        })
        self.socket = Socket()
        self.gateway.devices["lamp"] = self.socket

    async def asyncTearDown(self):
        tasks = []
        for pending in self.gateway.pending_commands.values():
            task = pending.get("timeout_task")
            if task:
                task.cancel()
                tasks.append(task)
            future = pending.get("result_future")
            if future and not future.done():
                future.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stored(self, command_id):
        with self.gateway.database_connection() as connection:
            return connection.execute(
                "SELECT status, attempts FROM command_queue WHERE command_id = ?", (command_id,),
            ).fetchone()

    async def dispatch(self, command_id="edge-command", **options):
        return await self.gateway.dispatch_device_command(
            "lamp", "power", True, requested_command_id=command_id, **options,
        )

    async def finish(self, command_id, status="succeeded"):
        pending = self.gateway.remove_pending_command(command_id)
        await self.gateway.send_pending_command_result(pending, status, True, None)
        return pending

    async def test_detached_delivery_and_queue_notification_keep_final_result(self):
        await self.dispatch()
        pending = await self.finish("edge-command")
        self.assertFalse(await self.gateway.deliver_pending_command(pending))
        await self.gateway.notify_command_queued(pending)
        self.assertEqual(len(self.socket.messages), 1)
        self.assertEqual(self.stored("edge-command"), ("succeeded", 1))
        self.assertFalse(self.gateway.pending_commands)

    async def test_ack_claimed_during_failed_send_is_never_requeued(self):
        async def acknowledged_then_transport_failed(payload):
            message = json.loads(payload)
            await self.finish(message["data"]["command_id"])
            raise ConnectionError("socket failed after ACK was processed")

        self.socket.send = acknowledged_then_transport_failed
        future = await self.dispatch(wait_for_result=True)
        self.assertEqual(future.result()["status"], "succeeded")
        self.assertEqual(self.stored("edge-command"), ("succeeded", 1))
        self.assertFalse(self.gateway.pending_commands)

    async def test_retry_requires_cached_result_and_preserves_original_payload(self):
        await self.dispatch()
        pending = self.gateway.pending_commands["edge-command"]
        self.assertNotIn("require_cached_result", self.socket.messages[0]["data"])
        self.assertTrue(await self.gateway.deliver_pending_command(pending))
        retry = self.socket.messages[1]
        self.assertIs(retry["data"]["require_cached_result"], True)
        self.assertEqual(retry["data"]["command_id"], "edge-command")
        self.assertEqual(retry["device"], "wire-lamp")
        self.assertNotIn("require_cached_result", pending["payload"]["data"])
        self.assertEqual(self.stored("edge-command"), ("sent", 2))

    async def test_restarted_gateway_retains_guard_and_retries_as_replay_only(self):
        await self.dispatch()
        old = self.gateway
        timer = old.pending_commands["edge-command"]["timeout_task"]
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        self.gateway = Gateway(old.DB_PATH)
        self.gateway.init_db()
        self.gateway.load_pending_commands()
        self.gateway.device_metadata["lamp"] = self.gateway.load_device_metadata("lamp")
        self.gateway.devices["lamp"] = self.socket
        self.assertTrue(self.gateway.device_supports_command_replay_guard("lamp"))
        await self.gateway.deliver_queued_commands("lamp")
        self.assertIs(self.socket.messages[-1]["data"]["require_cached_result"], True)
        self.assertEqual(self.stored("edge-command"), ("sent", 2))

    async def test_legacy_cache_without_replay_guard_cannot_retry_on_timeout(self):
        self.gateway.device_metadata["lamp"]["command_replay_guard"] = False
        future = await self.dispatch(wait_for_result=True)
        timer = self.gateway.pending_commands["edge-command"]["timeout_task"]
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        with patch.object(self.gateway, "COMMAND_TIMEOUT_SECONDS", 0):
            await self.gateway.expire_pending_command("edge-command")
        self.assertEqual(future.result()["status"], "timeout")
        self.assertEqual(len(self.socket.messages), 1)
        self.assertFalse(self.gateway.pending_commands)

    async def test_legacy_reconnection_finishes_unknown_result_without_resend(self):
        await self.dispatch()
        self.gateway.device_metadata["lamp"]["command_replay_guard"] = False
        pending = self.gateway.pending_commands["edge-command"]
        pending["status"] = "queued"
        await self.gateway.deliver_queued_commands("lamp")
        self.assertEqual(self.stored("edge-command"), ("unavailable", 1))
        self.assertEqual(len(self.socket.messages), 1)
        self.assertFalse(self.gateway.pending_commands)

    async def test_invalid_string_ids_never_reach_database_or_device(self):
        for invalid in ("", "a" * 81, "embedded\0nul", "\ud800", "\udfff"):
            for kind in ("parameter", "action"):
                with self.subTest(invalid=repr(invalid), kind=kind):
                    if kind == "parameter":
                        accepted = await self.dispatch(invalid)
                    else:
                        accepted = await self.gateway.dispatch_device_action(
                            "lamp", "identify", requested_command_id=invalid,
                        )
                    self.assertIs(accepted, False)
        self.assertEqual(self.socket.messages, [])
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM command_queue").fetchone()[0], 0)

    async def test_full_length_unicode_id_still_matches_firmware_utf8_limit(self):
        command_id = "\U0001f4a1" * 80
        self.assertTrue(await self.dispatch(command_id))
        self.assertEqual(self.socket.messages[0]["data"]["command_id"], command_id)
        self.assertEqual(len(command_id.encode("utf-8")), 320)

    async def test_schema_proposal_also_updates_current_connection_guard(self):
        offered = {
            "schema": {"parameters": {}, "actions": {}}, "schema_version": 2,
            "command_dedup": True, "command_replay_guard": False,
        }
        self.assertEqual(self.gateway.offer_device_schema("lamp", offered), "pending")
        self.assertFalse(self.gateway.device_supports_command_replay_guard("lamp"))
        self.assertIn("power", self.gateway.device_metadata["lamp"]["schema"]["parameters"])

    async def test_hello_negotiates_guard_and_legacy_connection_clears_it(self):
        metadata = self.gateway.device_metadata["lamp"]
        self.gateway.save_device_metadata("lamp", {**metadata, "trust_policy": "allow"})
        for capabilities in ({"command_replay_guard": True}, {}):
            with self.subTest(capabilities=capabilities):
                socket = DeviceSocket({
                    "type": "hello", "device": "lamp", "schema": self.schema,
                    "schema_version": 1, "command_dedup": True, **capabilities,
                })
                await self.gateway.handle_client(socket)
                self.assertEqual(self.gateway.device_supports_command_replay_guard("lamp"), bool(capabilities))
                self.assertTrue(any(message["type"] == "hello_ack" for message in socket.messages))

    async def test_hello_rejects_non_boolean_guard_instead_of_enabling_retries(self):
        for invalid in (1, "true", None):
            with self.subTest(invalid=invalid):
                socket = DeviceSocket({
                    "type": "hello", "device": "lamp", "schema": self.schema,
                    "command_dedup": True, "command_replay_guard": invalid,
                })
                await self.gateway.handle_client(socket)
                self.assertEqual(socket.closed[0][0], 4002)
                self.assertFalse(any(message["type"] == "hello_ack" for message in socket.messages))


if __name__ == "__main__":
    unittest.main()
