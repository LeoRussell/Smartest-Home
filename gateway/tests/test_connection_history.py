import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway
from connection_history import ConnectionHistory


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, value):
        self.messages.append(json.loads(value))


class ConnectionHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.wall, self.monotonic = 1_800_000_000.0, 100.0
        clock = SimpleNamespace(time=lambda: self.wall, monotonic=lambda: self.monotonic)
        patcher = patch("connection_history.time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = ConnectionHistory(self.gateway)
        self.sessions.initialize()
        self.gateway.connections = self.sessions
        self.peer = Socket()
        for device_id in ("lamp", "fan"):
            self.gateway.save_device_metadata(device_id, {
                "name": device_id, "schema_version": 1,
                "schema": {"parameters": {}, "actions": {}},
            })

    def advance(self, seconds, wall_seconds=None):
        self.monotonic += seconds
        self.wall += seconds if wall_seconds is None else wall_seconds

    def query(self, **extra):
        return self.sessions.query({"device": "lamp", **extra})

    def test_active_session_metrics_and_monotonic_duration(self):
        session_id = self.sessions.start("lamp", self.peer, {"wifi_rssi": -60, "uptime_seconds": 100, "reset_reason": "power_on"})
        self.assertEqual(self.sessions.start("lamp", self.peer), session_id)
        self.advance(12)
        self.sessions.touch("lamp", self.peer, data=True)
        self.sessions.record_health("lamp", {"wifi_rssi": -72, "uptime_seconds": 112})
        self.sessions.record_loop_lag(0.123)
        self.sessions.probe("lamp", self.peer, rtt_seconds=0.045)
        self.advance(3, wall_seconds=-500)
        result = self.query()
        session = result["sessions"][0]
        self.assertEqual(result["summary"]["session_count"], 1)
        self.assertTrue(result["summary"]["active"])
        self.assertEqual(result["summary"]["total_connected_seconds"], 15)
        self.assertEqual(session["duration_seconds"], 15)
        self.assertEqual((session["wifi_rssi_start"], session["wifi_rssi_last"], session["wifi_rssi_min"], session["wifi_rssi_max"]), (-60, -72, -72, -60))
        self.assertEqual((session["uptime_start"], session["uptime_last"]), (100, 112))
        self.assertEqual(session["gateway_loop_lag_ms"], 123)
        self.assertEqual(session["probe_rtt_ms"], 45)
        self.assertTrue(session["last_data_at"])
        json.dumps(result, allow_nan=False)

    def test_finish_is_idempotent_and_replacement_socket_guarded(self):
        first = self.sessions.start("lamp", self.peer)
        self.advance(4)
        second_peer = Socket()
        second = self.sessions.start("lamp", second_peer)
        self.assertNotEqual(first, second)
        self.assertFalse(self.sessions.finish("lamp", self.peer, code=1006))
        self.advance(7)
        self.sessions.probe("lamp", second_peer, failed=True)
        self.assertTrue(self.sessions.finish("lamp", second_peer, code=1011, reason="Device did not answer a WebSocket ping"))
        self.assertFalse(self.sessions.finish("lamp", second_peer, code=1000))
        result = self.query()
        self.assertEqual(result["summary"]["session_count"], 2)
        self.assertFalse(result["summary"]["active"])
        self.assertEqual([item["category"] for item in result["sessions"]], ["transport_timeout", "replaced"])
        self.assertEqual([item["duration_seconds"] for item in result["sessions"]], [7, 4])
        self.assertEqual(result["sessions"][0]["failed_probes"], 1)

    def test_restart_recovery_excludes_unobserved_downtime(self):
        self.sessions.start("lamp", self.peer)
        self.advance(12)
        self.sessions.touch("lamp", self.peer, data=True)
        last_observed = self.sessions.iso(self.wall)
        self.advance(3600)
        reopened = ConnectionHistory(self.gateway)
        reopened.initialize()
        result = reopened.query({"device": "lamp"})
        session = result["sessions"][0]
        self.assertEqual(session["status"], "interrupted")
        self.assertEqual(session["category"], "gateway_restart")
        self.assertEqual(session["ended_at"], last_observed)
        self.assertEqual(session["duration_seconds"], 12)

    def test_restore_recovery_does_not_claim_device_reboot(self):
        self.sessions.start("lamp", self.peer)
        self.advance(11)
        self.sessions.touch("lamp", self.peer)
        self.gateway.database_restore_in_progress = True
        try:
            reopened = ConnectionHistory(self.gateway)
            reopened.initialize()
        finally:
            self.gateway.database_restore_in_progress = False
        session = reopened.query({"device": "lamp"})["sessions"][0]
        self.assertEqual(session["category"], "database_restore")
        self.assertEqual(session["status"], "interrupted")
        self.assertEqual(session["duration_seconds"], 11)

    def test_pagination_summary_and_validation(self):
        for _ in range(3):
            self.sessions.start("lamp", Socket())
            self.advance(1)
            self.sessions.finish("lamp", code=1000)
        self.sessions.start("lamp", self.peer)
        self.advance(2)
        first = self.query(limit=2)
        second = self.query(limit=2, before_id=first["next_before_id"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(first["summary"]["session_count"], 4)
        self.assertEqual(first["summary"]["total_connected_seconds"], 5)
        self.assertFalse(set(item["session_id"] for item in first["sessions"]) & set(item["session_id"] for item in second["sessions"]))
        for invalid in ({"limit": 0}, {"limit": True}, {"limit": 51}, {"before_id": 0}, {"before_id": True}, {"device": "missing"}, {"device": []}):
            with self.assertRaises(ValueError, msg=invalid):
                self.query(**invalid)

    def test_limits_expiry_and_active_sessions_survive_cleanup(self):
        with patch("connection_history.SESSION_LIMIT", 3), patch("connection_history.DEVICE_SESSION_LIMIT", 2):
            for device_id in ("lamp", "lamp", "lamp", "fan", "fan"):
                self.sessions.start(device_id, Socket())
                self.advance(1)
                self.sessions.finish(device_id, code=1000)
            with self.gateway.database_connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_connection_sessions").fetchone()[0], 3)
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM device_connection_sessions WHERE device_id = 'lamp'").fetchone()[0], 2)
            self.sessions.start("lamp", self.peer)
            self.advance(91 * 86400)
            self.sessions.cleanup(force=True)
            result = self.query()
            self.assertEqual(len(result["sessions"]), 1)
            self.assertEqual(result["sessions"][0]["status"], "active")

    def test_sanitized_metrics_and_no_old_socket_updates(self):
        self.sessions.start("lamp", self.peer)
        self.sessions.record_health("lamp", {"wifi_rssi": True, "uptime_seconds": -2, "reset_reason": "bad\nvalue"})
        self.sessions.record_loop_lag(float("nan"))
        self.sessions.probe("lamp", self.peer, rtt_seconds=float("inf"))
        self.assertFalse(self.sessions.touch("lamp", Socket(), data=True))
        self.assertFalse(self.sessions.probe("lamp", Socket(), failed=True))
        row = self.query()["sessions"][0]
        self.assertIsNone(row["wifi_rssi_start"])
        self.assertIsNone(row["uptime_start"])
        self.assertIsNone(row["reset_reason"])
        self.assertIsNone(row["probe_rtt_ms"])
        self.assertEqual(row["gateway_loop_lag_ms"], 0)
        self.assertEqual(row["failed_probes"], 0)
        json.dumps(row, allow_nan=False)

    def test_rename_moves_history_and_active_owner(self):
        self.sessions.start("lamp", self.peer)
        self.advance(5)
        with self.gateway.database_connection() as connection:
            connection.execute("UPDATE devices SET device_id = 'renamed' WHERE device_id = 'lamp'")
        self.sessions.rename_device("lamp", "renamed")
        self.assertTrue(self.sessions.touch("renamed", self.peer, data=True))
        self.sessions.finish("renamed", self.peer, reason="Device forgotten by user")
        result = self.sessions.query({"device": "renamed"})
        self.assertEqual(result["summary"]["session_count"], 1)
        self.assertEqual(result["sessions"][0]["category"], "forgotten")

    async def test_browser_replies_include_request_identity(self):
        browser = Socket()
        self.sessions.start("lamp", self.peer)
        handled = await self.sessions.handle_browser_message(browser, {"action": "get_device_connections", "device": "lamp", "request_id": "sessions-1"})
        self.assertTrue(handled)
        self.assertEqual(browser.messages[-1]["type"], "device_connections")
        self.assertEqual(browser.messages[-1]["request_id"], "sessions-1")
        self.assertTrue(browser.messages[-1]["success"])
        await self.sessions.handle_browser_message(browser, {"action": "get_device_connections", "device": "lamp", "limit": True, "request_id": "bad"})
        self.assertFalse(browser.messages[-1]["success"])
        self.assertEqual(browser.messages[-1]["request_id"], "bad")
        self.assertFalse(await self.sessions.handle_browser_message(browser, {"action": "other"}))


if __name__ == "__main__":
    unittest.main()
