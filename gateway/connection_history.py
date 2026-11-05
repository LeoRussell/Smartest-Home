"""Bounded connection sessions and measured transport diagnostics.

Only observed facts are recorded here. A missed Pong, low RSSI or event-loop
delay is diagnostic evidence; none alone establishes a physical Wi-Fi cause.
"""

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone


SESSION_RETENTION_DAYS = 90
SESSION_LIMIT = 10_000
DEVICE_SESSION_LIMIT = 1_000
METRIC_FLUSH_SECONDS = 10
DEVICE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


class ConnectionHistory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.active = {}
        self.last_cleanup = 0

    @staticmethod
    def iso(timestamp):
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            return None
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")
        except (ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _document(raw):
        try:
            value = json.loads(raw or "{}")
        except (ValueError, TypeError, RecursionError):
            return {}
        return value if isinstance(value, dict) else {}

    def initialize(self):
        with self.gateway.database_connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS device_connection_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    close_code INTEGER,
                    close_reason TEXT,
                    category TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_device_connection_sessions_device
                    ON device_connection_sessions(device_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_device_connection_sessions_ended
                    ON device_connection_sessions(ended_at);
            """)
            # A prior process cannot tell when the cable/network actually stopped.
            # Recover at the last persisted observation rather than adding downtime.
            restored = self.gateway.database_restore_in_progress
            connection.execute("""
                UPDATE device_connection_sessions
                SET ended_at = updated_at, status = 'interrupted',
                    category = ?, close_reason = ?
                WHERE status = 'active'
            """, ("database_restore" if restored else "gateway_restart",
                  "Open session recovered from a restored database" if restored else "Gateway restarted before the session was closed"))
        self.active.clear()
        self.cleanup(force=True)

    def start(self, device_id, websocket, health=None):
        previous = self.active.get(device_id)
        if previous is not None:
            if previous["socket"] is websocket:
                return previous["session_id"]
            self.finish(device_id, previous["socket"], reason="Connection replaced by a new session", category="replaced")
        now, monotonic = time.time(), time.monotonic()
        metrics = {"last_message_epoch": now, "failed_probes": 0, "gateway_loop_lag_ms": 0}
        with self.gateway.database_connection() as connection:
            cursor = connection.execute("""
                INSERT INTO device_connection_sessions(device_id, started_at, status, metrics_json, updated_at)
                VALUES (?, ?, 'active', ?, ?)
            """, (device_id, now, json.dumps(metrics), now))
            session_id = cursor.lastrowid
        self.active[device_id] = {
            "session_id": session_id, "socket": websocket, "started_at": now,
            "monotonic": monotonic, "last_flush": monotonic, "metrics": metrics,
        }
        if health is not None:
            self.record_health(device_id, health)
        return session_id

    def _session(self, device_id, websocket=None):
        session = self.active.get(device_id)
        return session if session is not None and (websocket is None or session["socket"] is websocket) else None

    def _flush(self, device_id, force=False):
        session = self.active.get(device_id)
        if session is None:
            return
        now = time.monotonic()
        if not force and now - session["last_flush"] < METRIC_FLUSH_SECONDS:
            return
        with self.gateway.database_connection() as connection:
            connection.execute("""
                UPDATE device_connection_sessions
                SET duration_seconds = ?, metrics_json = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
            """, (max(0, now - session["monotonic"]), json.dumps(session["metrics"], ensure_ascii=False),
                  time.time(), session["session_id"]))
        session["last_flush"] = now

    def touch(self, device_id, websocket=None, *, data=False):
        session = self._session(device_id, websocket)
        if session is None:
            return False
        now = time.time()
        session["metrics"]["last_message_epoch"] = now
        if data:
            session["metrics"]["last_data_epoch"] = now
        self._flush(device_id)
        return True

    def record_health(self, device_id, health):
        session = self._session(device_id)
        if session is None or not isinstance(health, dict):
            return False
        metrics = session["metrics"]
        previous = dict(metrics)
        rssi = health.get("wifi_rssi")
        if type(rssi) is int and -127 <= rssi <= 0:
            metrics.setdefault("wifi_rssi_start", rssi)
            metrics["wifi_rssi_last"] = rssi
            metrics["wifi_rssi_min"] = min(rssi, metrics.get("wifi_rssi_min", rssi))
            metrics["wifi_rssi_max"] = max(rssi, metrics.get("wifi_rssi_max", rssi))
        uptime = health.get("uptime_seconds")
        if type(uptime) is int and 0 <= uptime <= 0x7FFFFFFFFFFFFFFF:
            metrics.setdefault("uptime_start", uptime)
            metrics["uptime_last"] = uptime
        for source, target in (("reset_reason", "reset_reason"), ("device_disconnect_reason", "device_disconnect_reason"),
                               ("last_disconnect_reason", "device_disconnect_reason")):
            value = health.get(source)
            if isinstance(value, str) and 0 < len(value) <= 240 and not any(ord(character) < 32 for character in value):
                try:
                    value.encode("utf-8")
                except UnicodeError:
                    continue
                metrics[target] = value
        if metrics == previous:
            return False
        first_report = not session.get("health_received", False)
        session["health_received"] = True
        self._flush(device_id, force=first_report)
        return True

    def probe(self, device_id, websocket=None, *, rtt_seconds=None, failed=False):
        session = self._session(device_id, websocket)
        if session is None:
            return False
        metrics = session["metrics"]
        metrics["last_probe_epoch"] = time.time()
        if type(rtt_seconds) in (int, float) and math.isfinite(rtt_seconds) and 0 <= rtt_seconds <= 3600:
            metrics["probe_rtt_ms"] = round(rtt_seconds * 1000, 2)
        if failed:
            metrics["failed_probes"] = metrics.get("failed_probes", 0) + 1
        self._flush(device_id, force=True)
        return True

    def record_loop_lag(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            return
        milliseconds = round(seconds * 1000, 2)
        for session in self.active.values():
            session["metrics"]["gateway_loop_lag_ms"] = max(milliseconds, session["metrics"].get("gateway_loop_lag_ms", 0))

    @staticmethod
    def category(code, reason):
        text = (reason or "").lower()
        if "replac" in text:
            return "replaced"
        if "forgot" in text:
            return "forgotten"
        if code == 4003 or "block" in text or "denied" in text:
            return "user_blocked"
        if code == 1012 or "restor" in text:
            return "database_restore"
        if "ping" in text and ("timeout" in text or "did not answer" in text):
            return "transport_timeout"
        if code == 1006:
            return "network_lost"
        if code == 1011:
            return "server_error"
        return "closed"

    def finish(self, device_id, websocket=None, *, code=None, reason="", category=None):
        session = self._session(device_id, websocket)
        if session is None:
            return False
        now = time.time()
        reason = reason[:240] if isinstance(reason, str) else ""
        code = code if type(code) is int and 1000 <= code <= 4999 else None
        category = category if category in {"closed", "replaced", "forgotten", "user_blocked", "database_restore",
                                           "gateway_shutdown", "gateway_restart", "transport_timeout", "network_lost", "server_error"} else self.category(code, reason)
        with self.gateway.database_connection() as connection:
            connection.execute("""
                UPDATE device_connection_sessions SET ended_at = ?, status = 'closed',
                    duration_seconds = ?, close_code = ?, close_reason = ?, category = ?,
                    metrics_json = ?, updated_at = ? WHERE id = ? AND status = 'active'
            """, (now, max(0, time.monotonic() - session["monotonic"]), code, reason, category,
                  json.dumps(session["metrics"], ensure_ascii=False), now, session["session_id"]))
        self.active.pop(device_id, None)
        self.cleanup(device_id=device_id, force=True)
        return True

    def close_all(self, reason="gateway_shutdown"):
        for device_id, session in list(self.active.items()):
            self.finish(device_id, session["socket"], reason=reason, category=reason)

    def cleanup(self, *, device_id=None, force=False):
        now = time.time()
        if not force and now - self.last_cleanup < 60:
            return
        with self.gateway.database_connection() as connection:
            connection.execute("DELETE FROM device_connection_sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
                               (now - SESSION_RETENTION_DAYS * 86400,))
            connection.execute("""
                DELETE FROM device_connection_sessions WHERE id IN (
                    SELECT id FROM device_connection_sessions WHERE status != 'active'
                    ORDER BY id DESC LIMIT -1 OFFSET ?
                )
            """, (SESSION_LIMIT,))
            if device_id is not None:
                connection.execute("""
                    DELETE FROM device_connection_sessions WHERE id IN (
                        SELECT id FROM device_connection_sessions WHERE device_id = ? AND status != 'active'
                        ORDER BY id DESC LIMIT -1 OFFSET ?
                    )
                """, (device_id, DEVICE_SESSION_LIMIT))
        self.last_cleanup = now

    def rename_device(self, old_id, new_id):
        with self.gateway.database_connection() as connection:
            connection.execute("UPDATE device_connection_sessions SET device_id = ? WHERE device_id = ?", (new_id, old_id))
        if old_id in self.active:
            self.active[new_id] = self.active.pop(old_id)

    def _public(self, row):
        session_id, device_id, started, ended, status, duration, code, reason, category, document, updated = row
        metrics = self._document(document)
        active = self.active.get(device_id)
        if active is not None and active["session_id"] == session_id:
            metrics = dict(active["metrics"])
            duration = max(0, time.monotonic() - active["monotonic"])
        result = {
            "session_id": session_id, "started_at": self.iso(started), "ended_at": self.iso(ended),
            "status": status, "duration_seconds": duration, "close_code": code,
            "close_reason": reason, "category": category, "updated_at": self.iso(updated),
        }
        for source, target in (("last_message_epoch", "last_message_at"), ("last_data_epoch", "last_data_at"),
                               ("last_probe_epoch", "last_probe_at")):
            value = metrics.pop(source, None)
            result[target] = self.iso(value) if type(value) in (int, float) and math.isfinite(value) else None
        for key in ("wifi_rssi_start", "wifi_rssi_last", "wifi_rssi_min", "wifi_rssi_max", "uptime_start", "uptime_last",
                    "reset_reason", "device_disconnect_reason", "probe_rtt_ms", "failed_probes", "gateway_loop_lag_ms"):
            result[key] = metrics.get(key)
        return result

    def query(self, message):
        device_id = message.get("device")
        limit, before = message.get("limit", 20), message.get("before_id")
        if (not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id)
                or type(limit) is not int or not 1 <= limit <= 50
                or before is not None and (type(before) is not int or before <= 0)):
            raise ValueError("Choose a device, limit=1..50 and a positive before_id.")
        with self.gateway.database_connection() as connection:
            if not connection.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
                raise ValueError("Unknown device.")
            rows = connection.execute("""
                SELECT * FROM device_connection_sessions WHERE device_id = ? AND (? IS NULL OR id < ?)
                ORDER BY id DESC LIMIT ?
            """, (device_id, before, before, limit + 1)).fetchall()
            totals = connection.execute("""
                SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0),
                    COALESCE(SUM(status = 'active'), 0)
                FROM device_connection_sessions WHERE device_id = ?
            """, (device_id,)).fetchone()
        sessions = [self._public(row) for row in rows[:limit]]
        active = self.active.get(device_id)
        extra_active_duration = 0
        if active is not None:
            with self.gateway.database_connection() as connection:
                saved = connection.execute("SELECT duration_seconds FROM device_connection_sessions WHERE id = ?", (active["session_id"],)).fetchone()
            if saved:
                extra_active_duration = max(0, time.monotonic() - active["monotonic"] - saved[0])
        return {
            "device": device_id, "sessions": sessions, "has_more": len(rows) > limit,
            "next_before_id": sessions[-1]["session_id"] if len(rows) > limit else None,
            "retention_days": SESSION_RETENTION_DAYS,
            "summary": {"session_count": totals[0], "active": bool(totals[2]),
                        "completed_count": totals[0] - totals[2],
                        "total_connected_seconds": totals[1] + extra_active_duration},
        }

    async def handle_browser_message(self, websocket, message):
        if message.get("action") != "get_device_connections":
            return False
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            request_id = None
        result = {"type": "device_connections", "request_id": request_id, "success": True,
                  "device": message.get("device") if isinstance(message.get("device"), str) else None}
        try:
            result.update(self.query(message))
        except (ValueError, sqlite3.Error) as error:
            result.update(success=False, message=str(error))
        await self.gateway.send_json(websocket, result)
        return True
