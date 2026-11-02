"""Bounded history of actual Schema parameter measurements, independent of events."""

import asyncio
import json
import math
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timezone


DEFAULT_HISTORY_SETTINGS = {"enabled": True, "retention_days": 30, "interval_seconds": 30}
HISTORY_MAX_SAMPLES = 250_000
HISTORY_RANGES = {"hour": 3600, "day": 86400, "week": 604800}
HISTORY_TYPES = {"number", "integer", "boolean"}


def valid_history_definition(definition):
    """An omitted flag keeps existing numeric and boolean firmware compatible."""
    option = definition.get("history", None)
    if option is None or option is False:
        return "history" not in definition or option is False
    if definition.get("type") not in HISTORY_TYPES:
        return False
    if option is True:
        return True
    return (
        isinstance(option, dict)
        and bool(option)
        and set(option).issubset({"enabled", "interval_seconds"})
        and ("enabled" not in option or type(option["enabled"]) is bool)
        and (
            "interval_seconds" not in option
            or type(option["interval_seconds"]) is int and 1 <= option["interval_seconds"] <= 3600
        )
    )


class ParameterHistory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.recent = OrderedDict()
        self.last_cleanup = 0
        self.inserts_since_cleanup = 0
        self.sample_count = 0
        self.cached_settings = None
        self.query_slots = asyncio.Semaphore(2)

    def initialize(self):
        with self.gateway.database_connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS parameter_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    parameter_id TEXT NOT NULL,
                    sampled_at REAL NOT NULL,
                    value REAL NOT NULL,
                    value_type TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_parameter_history_series_time
                    ON parameter_history(device_id, parameter_id, sampled_at);
                CREATE INDEX IF NOT EXISTS idx_parameter_history_time
                    ON parameter_history(sampled_at);
            """)
            connection.execute(
                "INSERT OR IGNORE INTO gateway_settings VALUES (?, ?)",
                ("parameter_history", json.dumps(DEFAULT_HISTORY_SETTINGS)),
            )
            self.sample_count = connection.execute("SELECT COUNT(*) FROM parameter_history").fetchone()[0]
        self.cleanup(force=True)

    def settings(self):
        if self.cached_settings is not None:
            return dict(self.cached_settings)
        with self.gateway.database_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM gateway_settings WHERE setting_key = 'parameter_history'"
            ).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            value = {}
        settings = dict(DEFAULT_HISTORY_SETTINGS)
        if isinstance(value, dict):
            if type(value.get("enabled")) is bool:
                settings["enabled"] = value["enabled"]
            for key, maximum in (("retention_days", 365), ("interval_seconds", 3600)):
                if type(value.get(key)) is int and 1 <= value[key] <= maximum:
                    settings[key] = value[key]
        self.cached_settings = {**settings, "max_samples": HISTORY_MAX_SAMPLES}
        return dict(self.cached_settings)

    def save_settings(self, message):
        settings = {key: message.get(key) for key in DEFAULT_HISTORY_SETTINGS}
        if (
            type(settings["enabled"]) is not bool
            or type(settings["retention_days"]) is not int
            or not 1 <= settings["retention_days"] <= 365
            or type(settings["interval_seconds"]) is not int
            or not 1 <= settings["interval_seconds"] <= 3600
        ):
            raise ValueError("History requires enabled=true/false, retention_days=1..365 and interval_seconds=1..3600.")
        with self.gateway.database_connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO gateway_settings VALUES ('parameter_history', ?)",
                (json.dumps(settings),),
            )
        self.recent.clear()
        self.cached_settings = None
        self.cleanup(force=True)
        return self.settings()

    def policy(self, definition, settings):
        option = definition.get("history", True)
        if not settings["enabled"] or option is False or definition.get("type") not in HISTORY_TYPES:
            return None
        if isinstance(option, dict):
            if option.get("enabled", True) is False:
                return None
            return option.get("interval_seconds", settings["interval_seconds"])
        return settings["interval_seconds"]

    def record(self, device_id, parameters, *, sampled_at=None):
        if not parameters or self.gateway.database_restore_in_progress:
            return 0
        metadata = self.gateway.device_metadata.get(device_id) or self.gateway.load_device_metadata(device_id)
        if not metadata:
            return 0
        settings = self.settings()
        if not settings["enabled"]:
            return 0
        now = time.time() if sampled_at is None else sampled_at
        if type(now) not in (int, float) or not math.isfinite(now):
            return 0
        definitions = metadata.get("schema", {}).get("parameters", {})
        inserted = 0
        with self.gateway.database_connection() as connection:
            for parameter_id, value in parameters.items():
                definition = definitions.get(parameter_id, {})
                interval = self.policy(definition, settings)
                if interval is None or not self.gateway.is_schema_value(value, definition):
                    continue
                if not self.gateway.value_within_schema_limits(value, definition):
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(number):
                    continue
                value_type = definition["type"]
                unit = definition.get("unit", "")
                key = (device_id, parameter_id, value_type, unit)
                previous = self.recent.get(key)
                if previous is None:
                    previous = connection.execute("""
                        SELECT sampled_at, value FROM parameter_history
                        WHERE device_id = ? AND parameter_id = ? AND value_type = ? AND unit = ?
                        ORDER BY sampled_at DESC, id DESC LIMIT 1
                    """, key).fetchone()
                # Boolean edges must survive a short pulse. Numeric series are
                # sampled at their declared interval to bound storage and writes.
                changed_boolean = value_type == "boolean" and previous is not None and previous[1] != number
                if previous is not None and 0 <= now - previous[0] < interval and not changed_boolean:
                    continue
                connection.execute("""
                    INSERT INTO parameter_history(device_id, parameter_id, sampled_at, value, value_type, unit)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (device_id, parameter_id, now, number, value_type, unit))
                self.recent[key] = (now, number)
                self.recent.move_to_end(key)
                while len(self.recent) > 4096:
                    self.recent.popitem(last=False)
                inserted += 1
        self.inserts_since_cleanup += inserted
        self.sample_count += inserted
        self.cleanup()
        return inserted

    def cleanup(self, force=False):
        now = time.time()
        if self.gateway.database_restore_in_progress or (
            not force and now - self.last_cleanup < 60 and self.inserts_since_cleanup < 500
            and self.sample_count <= HISTORY_MAX_SAMPLES
        ):
            return 0
        cutoff = now - self.settings()["retention_days"] * 86400
        with self.gateway.database_connection() as connection:
            expired = connection.execute("DELETE FROM parameter_history WHERE sampled_at < ?", (cutoff,)).rowcount
            remaining = connection.execute("SELECT COUNT(*) FROM parameter_history").fetchone()[0] if force else max(0, self.sample_count - expired)
            excess = max(0, remaining - HISTORY_MAX_SAMPLES)
            # Delete the small overflow from the start of the primary-key index,
            # instead of rescanning all 250k entries on every sample at capacity.
            capped = connection.execute("""
                DELETE FROM parameter_history
                WHERE id IN (SELECT id FROM parameter_history ORDER BY id LIMIT ?)
            """, (excess,)).rowcount if excess else 0
            self.sample_count = remaining - capped
        self.last_cleanup = now
        self.inserts_since_cleanup = 0
        return expired + capped

    def query(self, message, *, settings=None):
        device_id, parameter_id = message.get("device"), message.get("parameter")
        period, maximum = message.get("range", "day"), message.get("max_points", 240)
        if (
            not isinstance(device_id, str) or not 1 <= len(device_id) <= 64
            or not isinstance(parameter_id, str) or not 1 <= len(parameter_id) <= 64
            or not isinstance(period, str) or period not in HISTORY_RANGES
            or type(maximum) is not int or not 10 <= maximum <= 500
        ):
            raise ValueError("Choose a device, parameter, range hour/day/week and max_points=10..500.")
        metadata = self.gateway.device_metadata.get(device_id) or self.gateway.load_device_metadata(device_id)
        definition = (metadata or {}).get("schema", {}).get("parameters", {}).get(parameter_id)
        if not definition or definition.get("type") not in HISTORY_TYPES:
            raise ValueError("The current Schema has no numeric or boolean parameter with this ID.")
        end = time.time()
        start = end - HISTORY_RANGES[period]
        settings = dict(settings) if settings is not None else self.settings()
        query_start = max(start, end - settings["retention_days"] * 86400)
        bucket = HISTORY_RANGES[period] / maximum
        unit, value_type = definition.get("unit", ""), definition["type"]
        with self.gateway.database_connection() as connection:
            rows = connection.execute("""
                SELECT bins.sampled_at, bins.average_value, bins.minimum, bins.maximum,
                       bins.samples, last.value, bins.bucket_id
                FROM (
                    SELECT MAX(sampled_at) AS sampled_at, AVG(value) AS average_value,
                           MIN(value) AS minimum, MAX(value) AS maximum,
                           COUNT(*) AS samples, MAX(id) AS last_id,
                           MIN(?, CAST((sampled_at - ?) / ? AS INTEGER)) AS bucket_id
                    FROM parameter_history
                    WHERE device_id = ? AND parameter_id = ? AND sampled_at >= ? AND sampled_at <= ?
                      AND value_type = ? AND unit = ?
                    GROUP BY bucket_id
                ) bins JOIN parameter_history last ON last.id = bins.last_id
                ORDER BY bins.sampled_at
            """, (maximum - 1, start, bucket, device_id, parameter_id, query_start, end, value_type, unit)).fetchall()
            points = []
            for row in rows:
                value = row[5] if value_type == "boolean" else row[1]
                if not math.isfinite(value):
                    # SQLite's AVG can overflow its running sum even when all
                    # measurements and the mathematical mean are finite.
                    values = [item[0] for item in connection.execute("""
                        SELECT value FROM parameter_history
                        WHERE device_id = ? AND parameter_id = ? AND sampled_at >= ? AND sampled_at <= ?
                          AND value_type = ? AND unit = ?
                          AND MIN(?, CAST((sampled_at - ?) / ? AS INTEGER)) = ?
                    """, (device_id, parameter_id, query_start, end, value_type, unit,
                          maximum - 1, start, bucket, row[6]))]
                    scale = max(abs(item) for item in values)
                    value = sum(item / scale / len(values) for item in values) * scale
                    value = max(row[2], min(row[3], value))
                points.append({"timestamp": self.iso(row[0]), "value": value,
                               "min": row[2], "max": row[3], "count": row[4]})
        return {
            "device": device_id, "parameter": parameter_id, "range": period,
            "unit": unit, "value_type": value_type,
            "from": self.iso(start), "to": self.iso(end),
            "retention_days": settings["retention_days"],
            "recording_enabled": self.policy(definition, settings) is not None,
            "points": points,
        }

    @staticmethod
    def iso(timestamp):
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")

    async def handle_browser_message(self, websocket, message):
        action = message.get("action")
        if not isinstance(action, str) or action not in {"get_parameter_history", "get_history_settings", "set_history_settings"}:
            return False
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or len(request_id) > 128:
            request_id = None
        result_type = "parameter_history" if action == "get_parameter_history" else (
            "history_settings_result" if action == "set_history_settings" else "history_settings"
        )
        result = {"type": result_type, "request_id": request_id, "success": True}
        try:
            if action == "get_parameter_history":
                # A dense week can contain 250k samples. Keep aggregation off
                # the WebSocket/command event loop; each query owns its connection.
                async with self.query_slots:
                    if self.gateway.database_restore_in_progress or self is not self.gateway.history:
                        raise ValueError("The database is being restored. Refresh the chart shortly.")
                    settings = self.settings()
                    data = await asyncio.to_thread(self.query, dict(message), settings=settings)
                    if self.gateway.database_restore_in_progress or self is not self.gateway.history:
                        raise ValueError("The database changed while loading history. Refresh the chart.")
                result.update(data)
            elif action == "set_history_settings":
                result.update(self.save_settings(message))
                await self.gateway.broadcast({"type": "history_settings", **self.settings()})
            else:
                result.update(self.settings())
        except (ValueError, sqlite3.Error) as error:
            result.update(success=False, message=str(error))
        except Exception as error:
            # Return the request ID even when an unexpected internal failure
            # occurs, so the browser can stop its loading state immediately.
            print(f"[ИСТОРИЯ] Ошибка обработки {action}: {type(error).__name__}")
            result.update(
                success=False,
                code="history_internal_error",
                message="Не удалось обработать историю показаний. Проверьте журнал Gateway и повторите запрос."
            )
        await self.gateway.send_json(websocket, result)
        return True
