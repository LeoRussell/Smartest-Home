import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Gateway


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, document):
        self.messages.append(json.loads(document))


class DeviceManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.management = self.gateway.management
        self.browser = Socket()
        self.schema = {
            "parameters": {
                "enabled": {"type": "boolean", "access": "read_write"},
                "speed": {"type": "integer", "access": "read_write", "min": 0, "max": 100},
                "sensor": {"type": "number", "access": "read_only"},
            },
            "actions": {"stop": {"arguments": {"force": {"type": "boolean"}}}},
            "common_commands": {"off": {"kind": "parameter", "name": "enabled", "value": False}},
        }
        for device_id in ("lamp", "fan", "offline"):
            metadata = {
                "schema": self.schema, "schema_version": 1, "protocol_version": 2,
                "firmware_version": "test", "name": device_id, "trust_policy": "allow",
                "command_dedup": False,
            }
            self.gateway.save_device_metadata(device_id, metadata)
            self.gateway.device_metadata[device_id] = metadata
        self.gateway.devices.update({"lamp": Socket(), "fan": Socket()})

    async def asyncTearDown(self):
        await self.management.shutdown()
        tasks = [item["timeout_task"] for item in self.gateway.pending_commands.values() if item.get("timeout_task")]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def device(self, name):
        return next(device for device in self.gateway.get_all_devices() if device["device"] == name)

    async def complete_bulk(self, group_id=None):
        await self.management.execute_common(self.browser, {
            "command": "off", "group_id": group_id, "request_id": "request-test",
        })
        await asyncio.gather(*list(self.management.bulk_tasks))
        return [message for message in self.browser.messages if message["type"] == "common_command_result"][-1]

    def completed_future(self, status="succeeded"):
        future = asyncio.get_running_loop().create_future()
        future.set_result({"status": status, "value": False, "error": None})
        return future

    def test_group_crud_and_membership_survive_restart(self):
        group_id = self.management.save_group(None, "Комната")
        self.management.assign_group("lamp", group_id)
        self.assertEqual(self.device("lamp")["group_name"], "Комната")
        self.assertEqual(self.management.groups()[0]["device_count"], 1)
        with self.assertRaises(ValueError):
            self.management.save_group(None, "комната")
        self.management.save_group(group_id, "Bedroom")
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.assertEqual(reopened.management.groups()[0]["name"], "Bedroom")
        self.management.delete_group(group_id)
        self.assertIsNone(self.device("lamp")["group_id"])
        self.assertEqual(len(self.gateway.get_all_devices()), 3)

    def test_common_command_defaults_override_disable_and_reset(self):
        self.assertEqual(self.device("lamp")["common_command_sources"]["off"], "schema")
        operation = {"kind": "parameter", "name": "speed", "value": 0}
        self.management.save_common_command("fan", "off", operation)
        self.assertEqual(self.device("fan")["common_commands"]["off"], operation)
        self.management.save_common_command("lamp", "off", None)
        self.assertIsNone(self.device("lamp")["common_commands"]["off"])
        self.assertEqual(self.device("lamp")["common_command_sources"]["off"], "disabled")
        self.management.save_common_command("lamp", "off", None, use_schema=True)
        self.assertEqual(self.device("lamp")["common_command_sources"]["off"], "schema")

    def test_common_commands_reject_readonly_types_arguments_and_invalid_schema(self):
        for operation in (
            {"kind": "parameter", "name": "enabled", "value": 0},
            {"kind": "parameter", "name": "speed", "value": True},
            {"kind": "parameter", "name": "speed", "value": -1},
            {"kind": "parameter", "name": "sensor", "value": 0},
            {"kind": "action", "name": "stop", "arguments": {}},
            {"kind": "action", "name": "stop", "arguments": {"force": "yes"}},
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.management.save_common_command("lamp", "off", operation)
            invalid_schema = {**self.schema, "common_commands": {"off": operation}}
            self.assertFalse(self.gateway.validate_schema(invalid_schema))
        self.assertTrue(self.gateway.validate_schema(self.schema))
        self.management.save_common_command("fan", "off", {"kind": "action", "name": "stop", "arguments": {"force": True}})

    async def test_group_commands_use_explicit_mapping_and_report_offline(self):
        group_id = self.management.save_group(None, "All test devices")
        for device_id in ("lamp", "fan", "offline"):
            self.management.assign_group(device_id, group_id)
        self.management.save_common_command("fan", "off", {"kind": "parameter", "name": "speed", "value": 0})
        dispatcher = AsyncMock(side_effect=lambda *args, **kwargs: self.completed_future())
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            result = await self.complete_bulk(group_id)
        self.assertEqual(result["counts"], {"succeeded": 2, "failed": 0, "skipped": 1})
        calls = dispatcher.call_args_list
        self.assertEqual({(call.args[0], call.args[1], call.args[2]) for call in calls}, {("lamp", "enabled", False), ("fan", "speed", 0)})
        self.assertTrue(all(call.kwargs["transient"] for call in calls))

    async def test_concurrent_bulk_requests_never_double_dispatch(self):
        group_id = self.management.save_group(None, "Single")
        self.management.assign_group("lamp", group_id)
        future = asyncio.get_running_loop().create_future()
        dispatcher = AsyncMock(return_value=future)
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            await self.management.execute_common(self.browser, {"command": "off", "group_id": group_id, "request_id": "first"})
            await asyncio.sleep(0)
            await self.management.execute_common(self.browser, {"command": "off", "group_id": group_id, "request_id": "second"})
            await asyncio.sleep(0)
            future.set_result({"status": "succeeded"})
            await asyncio.gather(*list(self.management.bulk_tasks))
        self.assertEqual(dispatcher.await_count, 1)
        second = next(message for message in self.browser.messages if message.get("request_id") == "second")
        self.assertEqual(second["results"][0]["status"], "skipped")

    async def test_transient_command_timeout_is_never_replayed(self):
        group_id = self.management.save_group(None, "Single")
        self.management.assign_group("lamp", group_id)
        self.gateway.COMMAND_TIMEOUT_SECONDS = 0.001
        self.gateway.COMMAND_RETRY_LIMIT = 0
        result = await self.complete_bulk(group_id)
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(self.gateway.pending_commands, {})
        with self.gateway.database_connection() as connection:
            queued = connection.execute("SELECT COUNT(*) FROM command_queue WHERE status IN ('queued', 'sent')").fetchone()[0]
        self.assertEqual(queued, 0)
        self.gateway.load_pending_commands()
        self.assertEqual(self.gateway.pending_commands, {})

    async def test_offline_device_does_not_join_bulk_after_reconnecting(self):
        dispatcher = AsyncMock(side_effect=lambda *args, **kwargs: self.completed_future())
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            await self.management.execute_common(self.browser, {"command": "off"})
            self.gateway.devices["offline"] = Socket()
            await asyncio.gather(*list(self.management.bulk_tasks))
        self.assertEqual(dispatcher.await_count, 2)
        result = self.browser.messages[-1]
        offline = next(item for item in result["results"] if item["device"] == "offline")
        self.assertEqual(offline["status"], "skipped")

    async def test_changed_session_while_waiting_for_slot_is_skipped(self):
        semaphore = asyncio.Semaphore(0)
        device = self.device("lamp")
        socket = self.gateway.devices["lamp"]
        task = asyncio.create_task(self.management._execute_one(device, device["common_commands"]["off"], socket, semaphore))
        await asyncio.sleep(0)
        self.gateway.devices["lamp"] = Socket()
        dispatcher = AsyncMock()
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            semaphore.release()
            result = await task
        self.assertEqual(result["status"], "skipped")
        dispatcher.assert_not_awaited()

    async def test_shutdown_reports_uncertain_bulk_result_to_connected_browser(self):
        future = asyncio.get_running_loop().create_future()
        with patch.object(self.gateway, "dispatch_device_command", AsyncMock(return_value=future)):
            await self.management.execute_common(self.browser, {"command": "off", "request_id": "cancelled"})
            await self.management.shutdown()
        message = next(item for item in self.browser.messages if item.get("request_id") == "cancelled")
        self.assertTrue(message["cancelled"])
        self.assertFalse(message["success"])
        self.assertEqual(self.management.bulk_busy_devices, set())
        self.assertFalse(any(item["status"] == "succeeded" for item in message["results"]))

    async def test_stale_mapping_is_skipped_after_schema_changes(self):
        self.management.save_common_command("fan", "off", {"kind": "parameter", "name": "speed", "value": 0})
        self.gateway.device_metadata["fan"] = {**self.gateway.device_metadata["fan"], "schema": {"parameters": {}, "actions": {"stop": {}}}}
        dispatcher = AsyncMock(side_effect=lambda *args, **kwargs: self.completed_future())
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            result = await self.complete_bulk()
        fan = next(item for item in result["results"] if item["device"] == "fan")
        self.assertEqual(fan["status"], "skipped")
        self.assertEqual(dispatcher.await_count, 1)

    def test_health_is_separate_from_parameters_and_does_not_refresh_them(self):
        self.assertTrue(self.management.record_health("lamp", {"wifi_rssi": -61, "uptime_seconds": 100, "reset_reason": "power_on", "last_disconnect_reason": "wifi: lost_ip"}))
        self.management.record_disconnect("lamp", 1011, "keepalive ping timeout")
        health = self.device("lamp")["health"]
        self.assertEqual(health["wifi_rssi"], -61)
        self.assertIn("1011", health["last_disconnect_reason"])
        self.assertIn("lost_ip", health["last_disconnect_reason"])
        self.assertEqual(self.gateway.last_parameter_telemetry, {})
        self.assertEqual(self.gateway.device_states, {})
        self.management.clear_live_health("lamp")
        self.assertNotIn("uptime_seconds", self.device("lamp")["health"])
        self.assertIsNotNone(self.device("lamp")["health"]["last_disconnect_at"])

    def test_invalid_health_does_not_spoof_uptime_or_signal(self):
        self.assertFalse(self.management.record_health("lamp", {"wifi_rssi": True, "uptime_seconds": -1}))
        self.assertFalse(self.management.record_health("lamp", []))
        self.assertEqual(self.management.health_for_device("lamp"), {})

    def test_malformed_optional_health_text_preserves_valid_signal(self):
        for reason in ("\ud800", "bad\nreason", "bad\x00reason"):
            with self.subTest(reason=repr(reason)):
                self.assertTrue(self.management.record_health("lamp", {
                    "wifi_rssi": -55, "reset_reason": reason, "last_disconnect_reason": reason,
                }))
                health = self.management.health_for_device("lamp")
                self.assertEqual(health["wifi_rssi"], -55)
                self.assertNotIn("reset_reason", health)
                self.assertNotIn("device_disconnect_reason", health)

    async def test_retention_deletes_only_old_events_and_handles_timezones(self):
        old = (datetime.now(timezone.utc) - timedelta(days=100)).astimezone(timezone(timedelta(hours=3))).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for stamp in (old, recent, "invalid timestamp"):
            self.gateway.save_device_event("lamp", "connection", "test", timestamp=stamp)
        self.assertEqual(self.management.settings()["event_retention_days"], 90)
        self.assertEqual(self.management.cleanup_events(), 1)
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_events").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 3)
        await self.management.handle_browser_message(self.browser, {"action": "set_gateway_settings", "event_retention_days": 0})
        self.gateway.save_device_event("lamp", "connection", "old retained", timestamp=old)
        self.assertEqual(self.management.cleanup_events(), 0)
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.assertEqual(reopened.management.settings()["event_retention_days"], 0)

    async def test_invalid_requests_leave_settings_and_database_intact(self):
        for days in (True, -1, 3651, "90", None):
            await self.management.handle_browser_message(self.browser, {"action": "set_gateway_settings", "event_retention_days": days})
            self.assertFalse(self.browser.messages[-1]["success"])
        self.assertEqual(self.management.settings()["event_retention_days"], 90)
        self.assertFalse(await self.management.handle_browser_message(self.browser, {"action": []}))
        await self.management.handle_browser_message(self.browser, {"action": "execute_common_command", "command": "off", "group_id": "missing"})
        self.assertFalse(self.browser.messages[-1]["success"])

    def test_rename_moves_membership_mapping_and_health_together(self):
        group_id = self.management.save_group(None, "Room")
        self.management.assign_group("offline", group_id)
        self.management.save_common_command("offline", "off", None)
        self.management.record_health("offline", {"uptime_seconds": 20})
        success, _ = self.gateway.update_device_identity("offline", "renamed", "New name")
        self.assertTrue(success)
        device = self.device("renamed")
        self.assertEqual(device["group_id"], group_id)
        self.assertEqual(device["common_command_sources"]["off"], "disabled")
        self.assertEqual(device["health"]["uptime_seconds"], 20)


if __name__ == "__main__":
    unittest.main()
