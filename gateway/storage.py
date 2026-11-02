"""SQLite persistence, snapshots, event queries and database backups."""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from device_configuration import public_driver_catalog
from settings import EVENTS_PAGE_SIZE
from history import ParameterHistory


class StorageMixin:
    @contextmanager
    def database_connection(self):
        """Commit or roll back the transaction, then always release its file handle."""
        with closing(sqlite3.connect(self.DB_PATH)) as connection:
            with connection:
                yield connection


    def init_db(self):


        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        with self.database_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    last_seen TEXT,
                    last_telemetry_at TEXT,
                    led INTEGER,
                    connected INTEGER DEFAULT 0
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS parameter_ui_preferences (
                    device_id TEXT NOT NULL,
                    parameter_id TEXT NOT NULL,
                    control TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, parameter_id)
                )
            """)
            conn.execute("""
                UPDATE parameter_ui_preferences
                SET control = 'list'
                WHERE control IN ('boolean_words', 'boolean_binary')
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_value TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_device_events_device_id_id
                ON device_events (device_id, id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_schema_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    schema_version INTEGER,
                    firmware_version TEXT,
                    schema_json TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    source TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_device_schema_history_device
                ON device_schema_history (device_id, id DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_configurations (
                    device_id TEXT PRIMARY KEY,
                    config_version INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_configuration_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    config_version INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            configuration_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(device_configurations)")
            }
            if "applied_version" not in configuration_columns:
                conn.execute(
                    "ALTER TABLE device_configurations ADD COLUMN applied_version INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute("""
                    UPDATE device_configurations SET applied_version = config_version
                    WHERE status = 'applied' AND applied_at IS NOT NULL
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_device_configuration_history_device
                ON device_configuration_history (device_id, id DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_queue (
                    command_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    operation_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gateway_runtime (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    total_seconds REAL NOT NULL DEFAULT 0,
                    checkpoint_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automations (
                    automation_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_triggered_at TEXT,
                    run_status TEXT NOT NULL DEFAULT 'idle',
                    last_run_status TEXT,
                    last_error TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL,
                    automation_name TEXT NOT NULL,
                    trigger_json TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_automation_runs_history
                ON automation_runs (automation_id, started_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_run_steps (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    step_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    UNIQUE (run_id, sequence)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_automation_run_steps_order
                ON automation_run_steps (run_id, sequence)
            """)
            automation_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(automations)").fetchall()
            }
            for column, column_type in {
                "run_status": "TEXT NOT NULL DEFAULT 'idle'",
                "last_run_status": "TEXT",
                "last_error": "TEXT"
            }.items():
                if column not in automation_columns:
                    conn.execute(f"ALTER TABLE automations ADD COLUMN {column} {column_type}")
            interrupted_at = self.current_time()
            conn.execute("""
                UPDATE automation_runs
                SET status = 'interrupted', finished_at = ?,
                    error = COALESCE(error, 'Gateway перезапустился во время выполнения.')
                WHERE status = 'running'
            """, (interrupted_at,))
            conn.execute("""
                UPDATE automation_run_steps
                SET status = 'interrupted', finished_at = ?,
                    error = COALESCE(error, 'Gateway перезапустился во время выполнения.')
                WHERE status = 'running'
            """, (interrupted_at,))
            conn.execute("""
                UPDATE automations
                SET run_status = 'idle', last_run_status = 'interrupted',
                    last_error = COALESCE(last_error, 'Gateway перезапустился во время выполнения.')
                WHERE run_status = 'running'
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_command_queue_device_status
                ON command_queue (device_id, status)
            """)

            existing_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(devices)").fetchall()
            }
            metadata_columns = {
                "name": "TEXT",
                "display_name": "TEXT",
                "firmware_id": "TEXT",
                "protocol_version": "INTEGER",
                "firmware_version": "TEXT",
                "command_dedup": "INTEGER DEFAULT 0",
                "schema_version": "INTEGER",
                "schema_json": "TEXT",
                "state_json": "TEXT",
                "last_telemetry_at": "TEXT",
                "trust_policy": "TEXT DEFAULT 'allow'",
                "device_profile": "TEXT",
                "board_profile": "TEXT",
                "driver_catalog_json": "TEXT"
            }

            for column, column_type in metadata_columns.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE devices ADD COLUMN {column} {column_type}"
                    )

            conn.execute(
                "UPDATE devices SET firmware_id = device_id WHERE firmware_id IS NULL"
            )
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_firmware_id
                ON devices (firmware_id)
                WHERE firmware_id IS NOT NULL
            """)

            conn.execute("UPDATE devices SET connected = 0")

            # Existing enrolled devices retain access across this migration.
            # New devices receive a trust policy only after an explicit decision.
            rows = conn.execute("""
                SELECT device_id, schema_version, firmware_version, schema_json, last_seen
                FROM devices
                WHERE schema_json IS NOT NULL
            """).fetchall()
            for device_id, schema_version, firmware_version, schema_json, last_seen in rows:
                try:
                    schema = json.loads(schema_json)
                    canonical = json.dumps(
                        schema, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")
                    ).encode("utf-8")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                schema_hash = hashlib.sha256(canonical).hexdigest()
                existing_snapshot = conn.execute("""
                    SELECT 1 FROM device_schema_history
                    WHERE device_id = ? AND schema_hash = ?
                      AND schema_version IS ? AND firmware_version IS ?
                    LIMIT 1
                """, (
                    device_id, schema_hash, schema_version, firmware_version
                )).fetchone()
                if existing_snapshot:
                    continue
                conn.execute("""
                    INSERT INTO device_schema_history (
                        device_id, schema_version, firmware_version, schema_json,
                        schema_hash, accepted_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?, 'migration')
                """, (
                    device_id, schema_version, firmware_version, schema_json,
                    schema_hash, last_seen or self.current_time()
                ))

            # A command sent just before a Gateway restart may have reached the
            # device without its result reaching SQLite. Re-delivery keeps the
            # same command ID so compatible devices can safely replay their ACK.
            conn.execute("""
                UPDATE command_queue SET status = 'queued', updated_at = ?
                WHERE status = 'sent'
            """, (self.current_time(),))
            # A configuration may have reached the device immediately before a
            # Gateway restart. Reapply the saved desired configuration on reconnect.
            conn.execute("""
                UPDATE device_configurations
                SET status = 'pending', updated_at = ?
                WHERE status = 'applying'
            """, (self.current_time(),))
            conn.execute("""
                UPDATE device_configuration_history
                SET status = 'pending'
                WHERE (device_id, config_version) IN (
                    SELECT device_id, config_version FROM device_configurations
                    WHERE status = 'pending'
                )
            """)

            runtime_checkpoint_at = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT OR IGNORE INTO gateway_runtime (
                    singleton_id, total_seconds, checkpoint_at
                ) VALUES (1, 0, ?)
            """, (runtime_checkpoint_at,))
            # Count only persisted runtime checkpoints, never offline time.
            conn.execute("""
                UPDATE gateway_runtime SET checkpoint_at = ?
                WHERE singleton_id = 1
            """, (runtime_checkpoint_at,))

        print(f"База данных: {self.DB_PATH}")

        self.GATEWAY_SESSION_STARTED_AT = datetime.now(timezone.utc)
        self.management.initialize()
        self.history = ParameterHistory(self)
        self.history.initialize()


    def checkpoint_gateway_runtime(self, final=False):
        self.history.cleanup()
        now = datetime.now(timezone.utc)
        with self.database_connection() as conn:
            row = conn.execute("""
                SELECT total_seconds, checkpoint_at
                FROM gateway_runtime WHERE singleton_id = 1
            """).fetchone()
            if row is None:
                return

            total_seconds = max(0.0, float(row[0] or 0))
            if row[1]:
                try:
                    previous_checkpoint = datetime.fromisoformat(row[1])
                    if previous_checkpoint.tzinfo is None:
                        previous_checkpoint = previous_checkpoint.replace(tzinfo=timezone.utc)
                    total_seconds += max(
                        0.0,
                        (now - previous_checkpoint.astimezone(timezone.utc)).total_seconds()
                    )
                except ValueError:
                    pass

            conn.execute("""
                UPDATE gateway_runtime
                SET total_seconds = ?, checkpoint_at = ?
                WHERE singleton_id = 1
            """, (
                total_seconds,
                None if final else now.isoformat(timespec="microseconds")
            ))


    def get_database_stats(self):
        now = datetime.now(timezone.utc)
        with self.database_connection() as conn:
            device_count = conn.execute(
                "SELECT COUNT(*) FROM devices"
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM device_events"
            ).fetchone()[0]
            ever_connected_device_count = conn.execute("""
                SELECT COUNT(DISTINCT device_id)
                FROM device_events WHERE event_type = 'connection'
            """).fetchone()[0]
            schema_snapshot_count = conn.execute(
                "SELECT COUNT(*) FROM device_schema_history"
            ).fetchone()[0]
            device_configuration_count = conn.execute(
                "SELECT COUNT(*) FROM device_configurations"
            ).fetchone()[0]
            device_configuration_history_count = conn.execute(
                "SELECT COUNT(*) FROM device_configuration_history"
            ).fetchone()[0]
            command_record_count = conn.execute(
                "SELECT COUNT(*) FROM command_queue"
            ).fetchone()[0]
            parameter_ui_preference_count = conn.execute(
                "SELECT COUNT(*) FROM parameter_ui_preferences"
            ).fetchone()[0]
            automation_count = conn.execute(
                "SELECT COUNT(*) FROM automations"
            ).fetchone()[0]
            automation_run_count = conn.execute(
                "SELECT COUNT(*) FROM automation_runs"
            ).fetchone()[0]
            automation_run_step_count = conn.execute(
                "SELECT COUNT(*) FROM automation_run_steps"
            ).fetchone()[0]
            history_sample_count = conn.execute(
                "SELECT COUNT(*) FROM parameter_history"
            ).fetchone()[0]
            runtime = conn.execute("""
                SELECT total_seconds, checkpoint_at
                FROM gateway_runtime WHERE singleton_id = 1
            """).fetchone()

        total_uptime_seconds = max(0.0, float(runtime[0] or 0)) if runtime else 0.0
        if runtime and runtime[1]:
            try:
                checkpoint_at = datetime.fromisoformat(runtime[1])
                if checkpoint_at.tzinfo is None:
                    checkpoint_at = checkpoint_at.replace(tzinfo=timezone.utc)
                total_uptime_seconds += max(
                    0.0,
                    (now - checkpoint_at.astimezone(timezone.utc)).total_seconds()
                )
            except ValueError:
                pass

        session_uptime_seconds = (
            max(0.0, (now - self.GATEWAY_SESSION_STARTED_AT).total_seconds())
            if self.GATEWAY_SESSION_STARTED_AT
            else 0.0
        )
        database_size_bytes = 0
        for path in (self.DB_PATH, Path(f"{self.DB_PATH}-wal"), Path(f"{self.DB_PATH}-shm")):
            try:
                database_size_bytes += path.stat().st_size
            except OSError:
                pass

        return {
            "database_size_bytes": database_size_bytes,
            "record_count": (
                device_count + event_count + schema_snapshot_count + command_record_count
                + device_configuration_count + device_configuration_history_count
                + parameter_ui_preference_count + automation_count
                + automation_run_count + automation_run_step_count
                + history_sample_count
            ),
            "automation_count": automation_count,
            "automation_run_count": automation_run_count,
            "history_sample_count": history_sample_count,
            "history_enabled": self.history.settings()["enabled"],
            "device_count": device_count,
            "ever_connected_device_count": ever_connected_device_count,
            "connected_device_count": len(self.devices),
            "event_count": event_count,
            "schema_snapshot_count": schema_snapshot_count,
            "device_configuration_count": device_configuration_count,
            "device_configuration_history_count": device_configuration_history_count,
            "command_record_count": command_record_count,
            "session_uptime_seconds": session_uptime_seconds,
            "total_uptime_seconds": total_uptime_seconds,
            "sampled_at": now.timestamp()
        }


    def database_backup_metadata(self, path):
        stat = path.stat()
        return {
            "backup_id": path.name,
            "created_at": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).astimezone().isoformat(timespec="seconds"),
            "size_bytes": stat.st_size
        }


    def list_database_backups(self):
        if not self.DATABASE_BACKUP_DIR.is_dir():
            return []
        backups = [
            path for path in self.DATABASE_BACKUP_DIR.iterdir()
            if path.is_file() and self.DATABASE_BACKUP_ID_PATTERN.fullmatch(path.name)
        ]
        backups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [self.database_backup_metadata(path) for path in backups[:self.DATABASE_BACKUP_LIMIT]]


    def create_database_backup(self, prune=True, preserve_ids=()):
        if not self.DB_PATH.is_file():
            raise FileNotFoundError("Gateway database does not exist.")

        self.DATABASE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_id = f"gateway-{stamp}-{uuid.uuid4().hex}.sqlite3"
        backup_path = self.DATABASE_BACKUP_DIR / backup_id
        temporary_path = self.DATABASE_BACKUP_DIR / f".{backup_id}.tmp"
        try:
            with closing(sqlite3.connect(self.DB_PATH, timeout=30)) as source:
                with closing(sqlite3.connect(temporary_path, timeout=30)) as destination:
                    source.backup(destination, pages=256, sleep=0.01)
                    integrity = destination.execute("PRAGMA integrity_check").fetchone()
                    if not integrity or integrity[0] != "ok":
                        raise sqlite3.DatabaseError("Backup integrity check failed.")
            os.replace(temporary_path, backup_path)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        if prune:
            backups = [
                path for path in self.DATABASE_BACKUP_DIR.iterdir()
                if path.is_file() and self.DATABASE_BACKUP_ID_PATTERN.fullmatch(path.name)
            ]
            backups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            preserved_ids = set(preserve_ids)
            remaining_count = len(backups)
            for old_path in reversed(backups):
                if remaining_count <= self.DATABASE_BACKUP_LIMIT:
                    break
                if old_path.name in preserved_ids:
                    continue
                try:
                    old_path.unlink()
                    remaining_count -= 1
                except OSError as error:
                    print(f"[BACKUP] Could not prune {old_path.name}: {error}")
        return self.database_backup_metadata(backup_path)


    def validate_database_backup(self, path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Backup file is missing or empty.")
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("Backup integrity check failed.")
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        if not {"devices", "device_events"}.issubset(tables):
            raise ValueError("The file isn't an IoT Gateway database backup.")


    def restore_database_file(self, path):
        self.validate_database_backup(path)
        with closing(sqlite3.connect(path, timeout=30)) as source:
            with closing(sqlite3.connect(self.DB_PATH, timeout=30)) as destination:
                source.backup(destination, pages=256, sleep=0.01)
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise sqlite3.DatabaseError("Restored database integrity check failed.")


    def apply_parameter_ui_preferences(self, conn, device_id, preferences):
        timestamp = self.current_time()
        for parameter_id, control in preferences.items():
            if control == "schema":
                conn.execute(
                    "DELETE FROM parameter_ui_preferences WHERE device_id = ? AND parameter_id = ?",
                    (device_id, parameter_id)
                )
            else:
                conn.execute("""
                    INSERT INTO parameter_ui_preferences (
                        device_id, parameter_id, control, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(device_id, parameter_id) DO UPDATE SET
                        control = excluded.control,
                        updated_at = excluded.updated_at
                """, (device_id, parameter_id, control, timestamp))


    def get_device_parameter_ui_preferences(self, device_id):
        with self.database_connection() as conn:
            rows = conn.execute(
                "SELECT parameter_id, control FROM parameter_ui_preferences WHERE device_id = ?",
                (device_id,)
            ).fetchall()
        return {parameter_id: control for parameter_id, control in rows}


    def prune_parameter_ui_preferences(self, conn, device_id, schema):
        parameters = schema.get("parameters", {}) if isinstance(schema, dict) else {}
        if not isinstance(parameters, dict):
            parameters = {}
        rows = conn.execute(
            "SELECT parameter_id, control FROM parameter_ui_preferences WHERE device_id = ?",
            (device_id,)
        ).fetchall()
        stale = [
            parameter_id
            for parameter_id, control in rows
            if not self.is_parameter_ui_preference_supported(
                control, parameters.get(parameter_id)
            )
        ]
        if stale:
            conn.executemany(
                "DELETE FROM parameter_ui_preferences WHERE device_id = ? AND parameter_id = ?",
                [(device_id, parameter_id) for parameter_id in stale]
            )


    def get_parameter_ui_preferences(self):
        with self.database_connection() as conn:
            rows = conn.execute(
                "SELECT device_id, parameter_id, control FROM parameter_ui_preferences"
            ).fetchall()

        preferences = {}
        for device_id, parameter_id, control in rows:
            preferences.setdefault(device_id, {})[parameter_id] = control
        return preferences


    def save_parameter_ui_preference(self, device_id, parameter_id, control):
        if (
            not isinstance(device_id, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", device_id)
            or not isinstance(parameter_id, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", parameter_id)
            or not isinstance(control, str)
        ):
            return False, "Некорректный идентификатор устройства, параметра или вид элемента."

        with self.database_connection() as conn:
            row = conn.execute(
                "SELECT schema_version, schema_json FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return False, "Для устройства ещё нет принятой Schema."
            try:
                schema = json.loads(row[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                return False, "Не удалось прочитать Schema устройства."
            if not isinstance(schema, dict) or not isinstance(schema.get("parameters"), dict):
                return False, "Schema устройства имеет неверный формат."

            definition = schema.get("parameters", {}).get(parameter_id)
            if not isinstance(definition, dict) or definition.get("access") != "read_write":
                return False, "Параметр не найден в принятой Schema или доступен только для чтения."

            if control == "schema":
                conn.execute(
                    "DELETE FROM parameter_ui_preferences WHERE device_id = ? AND parameter_id = ?",
                    (device_id, parameter_id)
                )
                result_message = "Используется вид элемента из Schema."
            else:
                if not self.is_parameter_ui_preference_supported(control, definition):
                    return False, "Этот вид элемента несовместим с параметром в текущей Schema."

                conn.execute("""
                    INSERT INTO parameter_ui_preferences (device_id, parameter_id, control, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(device_id, parameter_id) DO UPDATE SET
                        control = excluded.control,
                        updated_at = excluded.updated_at
                """, (device_id, parameter_id, control, self.current_time()))
                result_message = "Вид элемента сохранён."
        self.write_device_configuration_export(device_id)
        return True, result_message


    def save_device_metadata(self, device_id, metadata):
        schema_json = json.dumps(metadata["schema"], ensure_ascii=False)
        timestamp = self.current_time()

        with self.database_connection() as conn:
            previous = conn.execute(
                "SELECT schema_json, schema_version, firmware_version, display_name, firmware_id, device_profile, board_profile, driver_catalog_json FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
            if previous is not None:
                if metadata.get("display_name") is None:
                    metadata["display_name"] = previous[3]
                if metadata.get("firmware_id") is None:
                    metadata["firmware_id"] = previous[4]
                metadata.setdefault("device_profile", previous[5])
                metadata.setdefault("board_profile", previous[6])
                if "driver_ids" not in metadata:
                    try:
                        metadata["driver_ids"] = json.loads(previous[7] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        metadata["driver_ids"] = []
            driver_ids = metadata.get("driver_ids", [])
            if not isinstance(driver_ids, list):
                driver_ids = []
            driver_catalog_json = json.dumps(driver_ids, ensure_ascii=True)
            conn.execute("""
                INSERT INTO devices (
                    device_id, last_seen, connected, name, display_name, firmware_id,
                    protocol_version, firmware_version, command_dedup,
                    schema_version, schema_json,
                    trust_policy, device_profile, board_profile, driver_catalog_json
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    connected = 1,
                    name = excluded.name,
                    display_name = COALESCE(excluded.display_name, devices.display_name),
                    firmware_id = COALESCE(devices.firmware_id, excluded.firmware_id),
                    protocol_version = excluded.protocol_version,
                    firmware_version = excluded.firmware_version,
                    command_dedup = excluded.command_dedup,
                    schema_version = excluded.schema_version,
                    schema_json = excluded.schema_json,
                    trust_policy = excluded.trust_policy,
                    device_profile = COALESCE(excluded.device_profile, devices.device_profile),
                    board_profile = COALESCE(excluded.board_profile, devices.board_profile),
                    driver_catalog_json = COALESCE(excluded.driver_catalog_json, devices.driver_catalog_json)
            """, (
                device_id,
                timestamp,
                metadata.get("name"),
                metadata.get("display_name"),
                metadata.get("firmware_id"),
                metadata.get("protocol_version"),
                metadata.get("firmware_version"),
                int(metadata.get("command_dedup") is True),
                metadata.get("schema_version"),
                schema_json,
                metadata.get("trust_policy"),
                metadata.get("device_profile"),
                metadata.get("board_profile"),
                driver_catalog_json
            ))

            previous_schema = None
            if previous is not None and previous[0]:
                try:
                    previous_schema = json.loads(previous[0])
                except (TypeError, json.JSONDecodeError):
                    previous_schema = None
            snapshot_changed = (
                previous_schema is None
                or not self.schemas_match(previous_schema, metadata["schema"])
                or previous[1] != metadata.get("schema_version")
                or previous[2] != metadata.get("firmware_version")
            ) if previous is not None else True
            if snapshot_changed:
                self.record_schema_snapshot_in_connection(
                    conn, device_id, metadata, timestamp, "accepted"
                )
            self.prune_parameter_ui_preferences(conn, device_id, metadata["schema"])

        self.device_metadata[device_id] = metadata
        self.write_device_configuration_export(device_id)


    def load_device_metadata(self, device_id):
        metadata = self.device_metadata.get(device_id)
        if metadata is not None:
            return dict(metadata)

        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT name, display_name, firmware_id, protocol_version, firmware_version,
                       command_dedup, schema_version, schema_json, trust_policy,
                       device_profile, board_profile, driver_catalog_json
                FROM devices WHERE device_id = ?
                """,
                (device_id,)
            ).fetchone()

        if row is None:
            return None

        try:
            schema = json.loads(row["schema_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            schema = self.default_legacy_schema()
        if not self.validate_schema(schema):
            schema = self.default_legacy_schema()

        try:
            driver_ids = json.loads(row["driver_catalog_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            driver_ids = []
        if not isinstance(driver_ids, list):
            driver_ids = []

        return {
            "name": row["name"],
            "display_name": row["display_name"],
            "firmware_id": row["firmware_id"],
            "protocol_version": row["protocol_version"],
            "firmware_version": row["firmware_version"],
            "command_dedup": bool(row["command_dedup"]),
            "schema_version": row["schema_version"],
            "schema": schema,
            "trust_policy": row["trust_policy"],
            "device_profile": row["device_profile"],
            "board_profile": row["board_profile"],
            "driver_ids": driver_ids
        }


    def load_device_configuration(self, device_id):
        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT config_version, config_json, config_hash, status,
                       last_error, updated_at, applied_at, applied_version
                FROM device_configurations WHERE device_id = ?
                """,
                (device_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            configuration = json.loads(row["config_json"])
        except (TypeError, json.JSONDecodeError):
            configuration = None
        if not isinstance(configuration, dict):
            return None
        return {
            "config_version": row["config_version"],
            "config": configuration,
            "config_hash": row["config_hash"],
            "status": row["status"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
            "applied_at": row["applied_at"],
            "applied_version": row["applied_version"]
        }


    def save_device_configuration(self, device_id, configuration, ui_preferences=None):
        canonical = json.dumps(
            configuration, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False
        )
        config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        timestamp = self.current_time()
        with self.database_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT config_version, config_hash FROM device_configurations WHERE device_id = ?",
                (device_id,)
            ).fetchone()
            if row is not None and row[1] == config_hash:
                self.apply_parameter_ui_preferences(conn, device_id, ui_preferences or {})
                return {
                    "config_version": row[0], "config_hash": config_hash,
                    "unchanged": True
                }
            version = (row[0] if row else 0) + 1
            if version > self.MAX_CONFIGURATION_VERSION:
                raise ValueError("Достигнут предел версий аппаратной конфигурации устройства.")
            status = "pending"
            conn.execute("""
                INSERT INTO device_configurations (
                    device_id, config_version, config_json, config_hash,
                    status, last_error, updated_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    config_version = excluded.config_version,
                    config_json = excluded.config_json,
                    config_hash = excluded.config_hash,
                    status = excluded.status,
                    last_error = NULL,
                    updated_at = excluded.updated_at
            """, (device_id, version, canonical, config_hash, status, timestamp))
            conn.execute("""
                INSERT INTO device_configuration_history (
                    device_id, config_version, config_json, config_hash,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (device_id, version, canonical, config_hash, timestamp, status))
            conn.execute("""
                DELETE FROM device_configuration_history
                WHERE device_id = ? AND id NOT IN (
                    SELECT id FROM device_configuration_history
                    WHERE device_id = ? ORDER BY id DESC LIMIT 50
                )
            """, (device_id, device_id))
            self.apply_parameter_ui_preferences(conn, device_id, ui_preferences or {})
        return {"config_version": version, "config_hash": config_hash, "unchanged": False}


    def update_device_configuration_status(
        self,
        device_id, status, error=None, applied_at=None
    ):
        with self.database_connection() as conn:
            conn.execute("""
                UPDATE device_configurations
                SET status = ?, last_error = ?,
                    applied_at = COALESCE(?, applied_at),
                    applied_version = CASE WHEN ? = 'applied' THEN config_version ELSE applied_version END
                WHERE device_id = ?
            """, (status, error, applied_at, status, device_id))
            conn.execute("""
                UPDATE device_configuration_history
                SET status = ?
                WHERE device_id = ? AND config_version = (
                    SELECT config_version FROM device_configurations WHERE device_id = ?
                )
            """, (status, device_id, device_id))


    def device_configuration_export_document(self, device_id):
        metadata = self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
        configuration = self.load_device_configuration(device_id)
        if metadata is None and configuration is None:
            return None
        document = {
            "export_format": "smartest-home-device",
            "export_version": 1,
            "exported_at": self.current_time(),
            "device_id": device_id,
            "schema_version": metadata.get("schema_version") if metadata else None,
            "firmware_version": metadata.get("firmware_version") if metadata else None,
            "schema": metadata.get("schema") if metadata else None,
            "device_profile": metadata.get("device_profile") if metadata else None,
            "board_profile": metadata.get("board_profile") if metadata else None,
            "hardware_configuration": configuration["config"] if configuration else None,
            "ui_preferences": self.get_device_parameter_ui_preferences(device_id),
            "configuration_version": configuration["config_version"] if configuration else None,
            "configuration_hash": configuration["config_hash"] if configuration else None,
            "configuration_status": configuration["status"] if configuration else None,
            "configuration_applied_version": configuration["applied_version"] if configuration else 0,
            "configuration_updated_at": configuration["updated_at"] if configuration else None,
            "configuration_applied_at": configuration["applied_at"] if configuration else None
        }
        return document


    def write_device_configuration_export(self, device_id):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", device_id or ""):
            return False
        document = self.device_configuration_export_document(device_id)
        if document is None:
            return False
        export_dir = self.DEVICE_CONFIGURATION_EXPORT_DIR
        destination = export_dir / f"{device_id}.json"
        temporary = export_dir / f".{device_id}.{uuid.uuid4().hex}.tmp"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8"
            )
            os.replace(temporary, destination)
            return True
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"[CONFIG] Could not update export for {device_id}: {error}")
            return False


    def sync_device_configuration_exports(self):
        """Rebuild portable JSON views from SQLite, the source of truth."""
        with self.database_connection() as conn:
            device_ids = [row[0] for row in conn.execute(
                "SELECT device_id FROM devices ORDER BY device_id"
            ).fetchall()]
        failed = [
            device_id for device_id in device_ids
            if not self.write_device_configuration_export(device_id)
        ]
        if failed:
            print(f"[CONFIG] Could not refresh {len(failed)} device JSON export(s).")
        return len(device_ids) - len(failed)


    def resolve_device_identity(self, firmware_id):
        with self.database_connection() as conn:
            row = conn.execute(
                "SELECT device_id FROM devices WHERE firmware_id = ?",
                (firmware_id,)
            ).fetchone()
            if row is not None:
                return row[0]

            row = conn.execute(
                "SELECT firmware_id FROM devices WHERE device_id = ?",
                (firmware_id,)
            ).fetchone()
            if row is not None and row[0] != firmware_id:
                conn.execute(
                    "UPDATE devices SET firmware_id = ? WHERE device_id = ?",
                    (firmware_id, firmware_id)
                )
        return firmware_id


    def firmware_identity_for(self, device_id):
        if device_id in self.device_wire_ids:
            return self.device_wire_ids[device_id]
        with self.database_connection() as conn:
            row = conn.execute(
                "SELECT firmware_id FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
        return row[0] if row and row[0] else device_id


    def replace_device_references(self, value, old_id, new_id):
        changed = False
        if isinstance(value, list):
            for item in value:
                changed = self.replace_device_references(item, old_id, new_id) or changed
        elif isinstance(value, dict):
            fields = value.get("fields")
            if isinstance(fields, dict) and fields.get("DEVICE") == old_id:
                fields["DEVICE"] = new_id
                changed = True
            if value.get("name") == "DEVICE" and value.get("value") == old_id:
                value["value"] = new_id
                changed = True
            for key, item in value.items():
                if key == "device" and item == old_id:
                    value[key] = new_id
                    changed = True
                elif isinstance(item, (dict, list)):
                    changed = self.replace_device_references(item, old_id, new_id) or changed
        return changed


    def update_device_identity(self, device_id, new_device_id, display_name):
        if not isinstance(device_id, str) or not isinstance(new_device_id, str):
            return False, "Некорректный ID устройства."
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", new_device_id):
            return False, "ID должен начинаться с английской буквы и содержать только английские буквы, цифры, дефис или подчёркивание."
        if display_name is not None and (
            not isinstance(display_name, str) or len(display_name) > 60
        ):
            return False, "Имя устройства должно быть не длиннее 60 символов."

        display_name = display_name.strip() if isinstance(display_name, str) else ""
        display_name = display_name or None
        changing_id = device_id != new_device_id
        if changing_id and device_id in self.devices:
            return False, "Чтобы изменить ID, сначала отключите устройство."
        if changing_id and device_id in self.pending_device_connections:
            return False, "Сначала обработайте ожидающий запрос на подключение устройства."
        if changing_id and any(task is not None and not task.done() for task in self.automation_tasks.values()):
            return False, "Сначала дождитесь завершения или остановите выполняющиеся сценарии."
        if changing_id and any(
            pending_id != device_id and pending_id.casefold() == new_device_id.casefold()
            for pending_id in self.pending_device_connections
        ):
            return False, "Такой ID уже запрашивает подключение."

        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
            if existing is None:
                return False, "Устройство не найдено."

            if changing_id:
                collision = conn.execute("""
                    SELECT 1 FROM devices
                    WHERE (lower(device_id) = lower(?) OR lower(firmware_id) = lower(?))
                      AND device_id != ?
                    LIMIT 1
                """, (new_device_id, new_device_id, device_id)).fetchone()
                if collision:
                    return False, "Такой ID уже занят другим устройством."

            timestamp = self.current_time()
            if changing_id:
                conn.execute(
                    "UPDATE devices SET device_id = ?, display_name = ? WHERE device_id = ?",
                    (new_device_id, display_name, device_id)
                )
                for table in (
                    "device_events", "device_schema_history", "command_queue",
                    "parameter_ui_preferences", "device_configurations",
                    "device_configuration_history", "device_group_membership",
                    "device_common_commands", "device_health", "parameter_history"
                ):
                    conn.execute(
                        f"UPDATE {table} SET device_id = ? WHERE device_id = ?",
                        (new_device_id, device_id)
                    )
                automation_rows = conn.execute(
                    "SELECT automation_id, config_json FROM automations"
                ).fetchall()
                for row in automation_rows:
                    try:
                        config = json.loads(row["config_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if self.replace_device_references(config, device_id, new_device_id):
                        conn.execute("""
                            UPDATE automations
                            SET config_json = ?, updated_at = ?, last_error = NULL
                            WHERE automation_id = ?
                        """, (
                            json.dumps(config, ensure_ascii=False, allow_nan=False),
                            timestamp,
                            row["automation_id"]
                        ))
            else:
                conn.execute(
                    "UPDATE devices SET display_name = ? WHERE device_id = ?",
                    (display_name, device_id)
                )

        metadata = self.device_metadata.pop(device_id, None)
        if metadata is not None:
            metadata["display_name"] = display_name
            self.device_metadata[new_device_id] = metadata
        old_export = self.DEVICE_CONFIGURATION_EXPORT_DIR / f"{device_id}.json"
        if changing_id:
            self.history.recent.clear()
            try:
                old_export.unlink(missing_ok=True)
            except OSError:
                pass
        self.write_device_configuration_export(new_device_id)
        for state_map in (
            self.device_states, self.last_device_heartbeat, self.fresh_device_parameters,
            self.pending_device_schemas, self.deferred_device_schemas, self.last_device_telemetry,
            self.last_parameter_telemetry, self.last_device_probe
        ):
            if device_id in state_map:
                state_map[new_device_id] = state_map.pop(device_id)
        if device_id in self.deferred_connection_automations:
            self.deferred_connection_automations.discard(device_id)
            self.deferred_connection_automations.add(new_device_id)
        for pending in self.pending_commands.values():
            if pending.get("device") == device_id:
                pending["device"] = new_device_id

        return True, f"Имя и ID устройства обновлены. В Blockly используется ID «{new_device_id}»."


    def device_supports_command_dedup(self, device_id):
        metadata = self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
        return metadata is not None and metadata.get("command_dedup") is True


    def record_schema_snapshot_in_connection(self, conn, device_id, metadata, timestamp, source):
        schema_json = json.dumps(metadata["schema"], ensure_ascii=False)
        canonical = json.dumps(
            metadata["schema"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")
        exists = conn.execute("""
            SELECT 1 FROM device_schema_history
            WHERE device_id = ? AND schema_hash = ?
              AND schema_version IS ? AND firmware_version IS ?
            LIMIT 1
        """, (
            device_id, hashlib.sha256(canonical).hexdigest(),
            metadata.get("schema_version"), metadata.get("firmware_version")
        )).fetchone()
        if exists:
            return

        conn.execute("""
            INSERT INTO device_schema_history (
                device_id, schema_version, firmware_version, schema_json,
                schema_hash, accepted_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            device_id, metadata.get("schema_version"),
            metadata.get("firmware_version"), schema_json,
            hashlib.sha256(canonical).hexdigest(), timestamp, source
        ))


    def get_schema_history(self, device_id):
        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, schema_version, firmware_version, schema_json,
                       accepted_at, source
                FROM device_schema_history
                WHERE device_id = ?
                ORDER BY id DESC
            """, (device_id,)).fetchall()

        history = []
        for row in rows:
            try:
                schema = json.loads(row["schema_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not self.validate_schema(schema):
                continue
            history.append({
                "id": row["id"],
                "schema_version": row["schema_version"],
                "firmware_version": row["firmware_version"],
                "accepted_at": row["accepted_at"],
                "source": row["source"],
                "schema": schema
            })
        return history


    def delete_schema_history_item(self, device_id, history_id):
        with self.database_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM device_schema_history WHERE id = ? AND device_id = ?",
                (history_id, device_id)
            )
            return cursor.rowcount > 0


    def restore_device_schema(self, device_id, history_id):
        if device_id in self.devices:
            return False, "Отключите устройство перед восстановлением Schema."

        history = next(
            (item for item in self.get_schema_history(device_id) if item["id"] == history_id),
            None
        )
        if history is None:
            return False, "Снимок Schema не найден."

        with self.database_connection() as conn:
            cursor = conn.execute("""
                UPDATE devices
                SET schema_version = ?, firmware_version = ?, schema_json = ?,
                    connected = 0
                WHERE device_id = ?
            """, (
                history.get("schema_version"), history.get("firmware_version"),
                json.dumps(history["schema"], ensure_ascii=False), device_id
            ))
            if cursor.rowcount == 0:
                return False, "Устройство не найдено."
            self.prune_parameter_ui_preferences(conn, device_id, history["schema"])

        self.device_metadata.pop(device_id, None)
        self.pending_device_schemas.pop(device_id, None)
        self.deferred_device_schemas.pop(device_id, None)
        self.write_device_configuration_export(device_id)
        self.save_device_event(
            device_id,
            "schema",
            f"Восстановлена Schema из истории (снимок {history_id})"
        )
        return True, "Schema Gateway восстановлена. При следующем подключении она будет сравнена с Schema устройства."


    def get_device_trust_policy(self, device_id):
        with self.database_connection() as conn:
            row = conn.execute(
                "SELECT trust_policy FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
        return row[0] if row else None


    def set_device_trust_policy(self, device_id, policy, name=None):
        with self.database_connection() as conn:
            conn.execute("""
                INSERT INTO devices (device_id, connected, trust_policy, name)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    trust_policy = excluded.trust_policy,
                    name = COALESCE(excluded.name, devices.name)
            """, (device_id, policy, name))
        if device_id in self.device_metadata:
            self.device_metadata[device_id]["trust_policy"] = policy


    def get_blocked_devices(self):
        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT device_id, COALESCE(display_name, name) AS name, last_seen
                FROM devices
                WHERE trust_policy = 'deny'
                ORDER BY device_id
            """).fetchall()
        return [dict(row) for row in rows]


    def get_pending_device_requests(self):
        return [
            {
                "request_id": request["request_id"],
                "device": device_id,
                "name": request["metadata"].get("name"),
                "firmware_version": request["metadata"].get("firmware_version"),
                "protocol_version": request["metadata"].get("protocol_version")
            }
            for device_id, request in self.pending_device_connections.items()
            if not request["decision_event"].is_set()
        ]


    def load_pending_commands(self):
        with self.database_connection() as conn:
            rows = conn.execute("""
                SELECT command_id, device_id, operation_json, payload_json,
                       status, attempts
                FROM command_queue
                WHERE status IN ('queued', 'sent')
                ORDER BY created_at
            """).fetchall()

        for command_id, device_id, operation_json, payload_json, status, attempts in rows:
            try:
                operation = json.loads(operation_json)
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                print(f"[WARN] Skipping unreadable queued command {command_id}.")
                continue
            if not isinstance(operation, dict) or not isinstance(payload, dict):
                continue
            if operation.get("transient"):
                with self.database_connection() as conn:
                    conn.execute("""
                        UPDATE command_queue SET status = 'unavailable',
                            updated_at = ?, error = ? WHERE command_id = ?
                    """, (
                        self.current_time(),
                        "Общая команда отменена при перезапуске Gateway; отложенное выполнение запрещено.",
                        command_id
                    ))
                continue
            self.pending_commands[command_id] = {
                "command_id": command_id,
                "device": device_id,
                "browser": None,
                "operation": operation,
                "payload": payload,
                "attempts": attempts,
                "status": "queued",
                "timeout_task": None,
                "sending": False,
                "automation_id": operation.get("automation_id"),
                "automation_run_id": operation.get("automation_run_id")
            }


    def create_command_queue_record(self, pending):
        timestamp = self.current_time()
        with self.database_connection() as conn:
            conn.execute("""
                INSERT INTO command_queue (
                    command_id, device_id, operation_json, payload_json,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)
            """, (
                pending["command_id"], pending["device"],
                json.dumps(pending["operation"], ensure_ascii=False),
                json.dumps(pending["payload"], ensure_ascii=False),
                timestamp, timestamp
            ))


    def update_command_queue_record(self, pending, status, result=None, error=None):
        with self.database_connection() as conn:
            conn.execute("""
                UPDATE command_queue
                SET status = ?, attempts = ?, updated_at = ?,
                    result_json = ?, error = ?
                WHERE command_id = ?
            """, (
                status, pending.get("attempts", 0), self.current_time(),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error, pending["command_id"]
            ))


    def save_event_in_connection(
        self,
        conn, device_id, event_type, event_value, timestamp
    ):
        conn.execute("""
            INSERT INTO device_events (
                device_id,
                event_type,
                event_value,
                timestamp
            )
            VALUES (?, ?, ?, ?)
        """, (
            device_id,
            event_type,
            str(event_value),
            timestamp
        ))


    def save_device_event(self, device_id, event_type, event_value, timestamp=None):
        timestamp = timestamp or self.current_time()

        with self.database_connection() as conn:
            self.save_event_in_connection(
                conn,
                device_id,
                event_type,
                event_value,
                timestamp
            )

        return timestamp


    def save_device_seen(self, device_id, connected):
        timestamp = self.current_time()

        with self.database_connection() as conn:
            conn.execute("""
                INSERT INTO devices (
                    device_id,
                    last_seen,
                    connected
                )
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    connected = excluded.connected
            """, (
                device_id,
                timestamp,
                int(connected)
            ))

        return timestamp


    def save_device_state(
        self,
        device_id,
        parameters,
        record_changes=True,
        defer_parameters=None,
        record_telemetry=False
    ):
        timestamp = self.current_time()
        has_changes = False
        deferred_events = []
        defer_parameters = defer_parameters or set()

        with self.database_connection() as conn:
            row = conn.execute("""
                SELECT led, state_json, last_telemetry_at
                FROM devices
                WHERE device_id = ?
            """, (device_id,)).fetchone()

            if row is None:
                conn.execute("""
                    INSERT INTO devices (
                        device_id,
                        last_seen,
                        last_telemetry_at,
                        connected
                    )
                    VALUES (?, ?, ?, 1)
                """, (
                    device_id,
                    timestamp,
                    timestamp if record_telemetry else None
                ))

                previous_led = None
                saved_parameters = {}
                last_telemetry_at = timestamp if record_telemetry else None
            else:
                previous_led = row[0]
                last_telemetry_at = row[2]
                if record_telemetry:
                    last_telemetry_at = timestamp
                try:
                    saved_parameters = json.loads(row[1]) if row[1] else {}
                except json.JSONDecodeError:
                    saved_parameters = {}

                if not isinstance(saved_parameters, dict):
                    saved_parameters = {}

                if previous_led is not None:
                    saved_parameters.setdefault("led", bool(previous_led))

            for parameter_id, value in parameters.items():
                if (
                    parameter_id not in saved_parameters
                    or self.schema_values_match(saved_parameters[parameter_id], value)
                ):
                    continue

                event_type = "parameter"
                event_value = json.dumps({
                    "parameter": parameter_id,
                    "value": value
                }, ensure_ascii=False)

                if record_changes and parameter_id in defer_parameters:
                    deferred_events.append({
                        "parameter": parameter_id,
                        "value": value,
                        "event_type": event_type,
                        "event_value": event_value,
                        "timestamp": timestamp
                    })
                elif record_changes:
                    self.save_event_in_connection(
                        conn,
                        device_id,
                        event_type,
                        event_value,
                        timestamp
                    )
                has_changes = True

                if (
                    record_changes
                    and parameter_id == "led"
                    and parameter_id not in defer_parameters
                ):
                    print(
                        f"[СОБЫТИЕ] {device_id}: "
                        f"LED {'включён' if value else 'выключен'}"
                    )

            saved_parameters.update(parameters)
            state_json = json.dumps(saved_parameters, ensure_ascii=False)

            new_led = None

            if "led" in parameters:
                raw_led = parameters["led"]

                if isinstance(raw_led, bool):
                    new_led = int(raw_led)
                elif isinstance(raw_led, int) and raw_led in (0, 1):
                    new_led = raw_led

            if new_led is not None:
                # Сохраняем новое состояние LED.
                conn.execute("""
                    UPDATE devices
                    SET led = ?,
                        last_seen = ?,
                        connected = 1,
                        state_json = ?,
                        last_telemetry_at = CASE
                            WHEN ? THEN ? ELSE last_telemetry_at
                        END
                    WHERE device_id = ?
                """, (
                    new_led,
                    timestamp,
                    state_json,
                    int(record_telemetry),
                    timestamp,
                    device_id
                ))

            else:
                conn.execute("""
                    UPDATE devices
                    SET last_seen = ?,
                        state_json = ?,
                        connected = 1,
                        last_telemetry_at = CASE
                            WHEN ? THEN ? ELSE last_telemetry_at
                        END
                    WHERE device_id = ?
                """, (
                    timestamp,
                    state_json,
                    int(record_telemetry),
                    timestamp,
                    device_id
                ))

        return timestamp, has_changes, deferred_events, last_telemetry_at


    def get_recent_events(self, limit=30):
        """
        Возвращает последние значимые события.

        Старые повторяющиеся подряд значения LED скрываются
        в выдаче, даже если они уже сохранены в SQLite.

        Записи в самой базе при этом не удаляются.
        """

        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT
                    id,
                    device_id,
                    event_type,
                    event_value,
                    timestamp
                FROM device_events
                ORDER BY id ASC
            """).fetchall()

        filtered_events = []

        # Последнее встреченное значение LED для каждого устройства.
        last_led_values = {}

        for row in rows:
            event = dict(row)
            if not self.event_is_visible(event["event_type"], event["event_value"]):
                continue

            if event["event_type"] == "led":
                device_id = event["device_id"]
                value = str(event["event_value"]).lower()

                previous_value = last_led_values.get(device_id)

                # Пропускаем повтор одного и того же состояния.
                if previous_value == value:
                    continue

                last_led_values[device_id] = value

            filtered_events.append({
                "device_id": event["device_id"],
                "event_type": event["event_type"],
                "event_value": event["event_value"],
                "timestamp": event["timestamp"]
            })

        # Сначала оставляем последние значимые события,
        # затем отображаем их от новых к старым.
        return list(reversed(filtered_events[-limit:]))


    def sqlite_casefold(self, value):
        return value.casefold() if isinstance(value, str) else ""


    def is_automation_command(self, command_id, automation_id=None):
        return (isinstance(automation_id, str) and bool(automation_id.strip())) or (
            isinstance(command_id, str) and command_id.startswith("auto-")
        )


    def event_is_visible(self, event_type, event_value):
        """Keep successful scenario operations in run history, without duplicate events."""
        if event_type != "command":
            return True
        try:
            command = json.loads(event_value)
        except (TypeError, ValueError, RecursionError):
            return True
        return not (
            isinstance(command, dict)
            and command.get("status") == "succeeded"
            and self.is_automation_command(command.get("command_id"), command.get("automation_id"))
        )


    def get_event_page(self, limit=EVENTS_PAGE_SIZE, device_id=None, before_id=None, search=""):
        conditions = ["event_is_visible(event_type, event_value) = 1"]
        parameters = []

        if device_id is not None:
            conditions.append("device_id = ?")
            parameters.append(device_id)

        if search:
            escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("(casefold(device_id) LIKE casefold(?) ESCAPE '\\' OR casefold(event_type) LIKE casefold(?) ESCAPE '\\' OR casefold(event_value) LIKE casefold(?) ESCAPE '\\' OR casefold(timestamp) LIKE casefold(?) ESCAPE '\\' OR EXISTS (SELECT 1 FROM devices AS matching_device WHERE matching_device.device_id = device_events.device_id AND (casefold(COALESCE(matching_device.display_name, '')) LIKE casefold(?) ESCAPE '\\' OR casefold(COALESCE(matching_device.name, '')) LIKE casefold(?) ESCAPE '\\' OR casefold(COALESCE(matching_device.schema_json, '')) LIKE casefold(?) ESCAPE '\\')))")
            search_pattern = f"%{escaped_search}%"
            parameters.extend([search_pattern] * 7)

        matching_where = (
            f"WHERE {' AND '.join(conditions)}"
            if conditions
            else ""
        )
        matching_parameters = tuple(parameters)
        if before_id is not None:
            conditions.append("id < ?")
            parameters.append(before_id)

        where_clause = (
            f"WHERE {' AND '.join(conditions)}"
            if conditions
            else ""
        )

        with self.database_connection() as conn:
            conn.create_function("casefold", 1, self.sqlite_casefold)
            conn.create_function("event_is_visible", 2, self.event_is_visible, deterministic=True)
            conn.row_factory = sqlite3.Row
            matching_count = conn.execute(
                f"SELECT COUNT(*) FROM device_events {matching_where}",
                matching_parameters
            ).fetchone()[0]
            rows = conn.execute(f"""
                SELECT id, device_id, event_type, event_value, timestamp
                FROM device_events
                {where_clause}
                ORDER BY id DESC
                LIMIT ?
            """, (*parameters, limit + 1)).fetchall()

        has_more = len(rows) > limit
        page = rows[:limit]
        events = [dict(row) for row in page]
        next_before_id = page[-1]["id"] if has_more and page else None

        return events, has_more, next_before_id, matching_count


    def get_all_devices(self):
        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT
                    device_id, last_seen, last_telemetry_at, led, connected, name, display_name, state_json,
                    protocol_version, firmware_version, command_dedup,
                    schema_version, schema_json,
                    trust_policy, device_profile, board_profile, driver_catalog_json
                FROM devices
                WHERE trust_policy IS NULL
                   OR trust_policy NOT IN ('deny', 'forgotten')
                ORDER BY device_id
            """).fetchall()
            configuration_rows = conn.execute("""
                SELECT device_id, config_version, config_json, config_hash,
                       status, last_error, updated_at, applied_at, applied_version
                FROM device_configurations
            """).fetchall()

        configurations = {}
        for configuration_row in configuration_rows:
            try:
                config_value = json.loads(configuration_row["config_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(config_value, dict):
                configurations[configuration_row["device_id"]] = {
                    "version": configuration_row["config_version"],
                    "config": config_value,
                    "status": configuration_row["status"],
                    "error": configuration_row["last_error"],
                    "applied_version": configuration_row["applied_version"],
                    "updated_at": configuration_row["updated_at"],
                    "applied_at": configuration_row["applied_at"]
                }

        result = []

        for row in rows:
            device_id = row["device_id"]

            parameters = {}

            if row["led"] is not None:
                parameters["led"] = bool(row["led"])

            try:
                saved_parameters = json.loads(row["state_json"] or "{}")
                if isinstance(saved_parameters, dict):
                    parameters.update(saved_parameters)
            except json.JSONDecodeError:
                pass

            parameters.update(self.device_states.get(device_id, {}))

            metadata = self.device_metadata.get(device_id)
            if metadata is None:
                try:
                    stored_schema = json.loads(row["schema_json"])
                except (TypeError, json.JSONDecodeError):
                    stored_schema = self.default_legacy_schema()
                if not self.validate_schema(stored_schema):
                    stored_schema = self.default_legacy_schema()

                # Rebuild inferred, read-only definitions for offline legacy devices
                # from their last saved values, without treating them as writable.
                if row["schema_version"] is None:
                    stored_schema.setdefault("parameters", {})
                    for definition in stored_schema["parameters"].values():
                        if isinstance(definition, dict):
                            definition["access"] = "read_only"
                    for key, value in parameters.items():
                        if key not in stored_schema["parameters"]:
                            definition = self.inferred_definition(key, value)
                            if definition is not None:
                                stored_schema["parameters"][key] = definition

                metadata = {
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "firmware_id": row["device_id"],
                    "protocol_version": row["protocol_version"],
                    "firmware_version": row["firmware_version"],
                    "command_dedup": bool(row["command_dedup"]),
                    "schema_version": row["schema_version"],
                    "schema": stored_schema,
                    "trust_policy": row["trust_policy"],
                    "device_profile": row["device_profile"],
                    "board_profile": row["board_profile"]
                }
                try:
                    metadata["driver_ids"] = json.loads(row["driver_catalog_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    metadata["driver_ids"] = []

            result.append({
                "device": device_id,
                "name": metadata.get("name"),
                "display_name": metadata.get("display_name"),
                "connected": device_id in self.devices,
                "last_seen": row["last_seen"],
                "last_telemetry_at": row["last_telemetry_at"],
                "telemetry_stale_after_seconds": self.DEVICE_STALE_SECONDS,
                "parameters": parameters,
                "schema": metadata["schema"],
                "protocol_version": metadata.get("protocol_version"),
                "firmware_version": metadata.get("firmware_version"),
                "command_dedup": metadata.get("command_dedup", False),
                "schema_version": metadata.get("schema_version"),
                "schema_source": (
                    "device"
                    if metadata.get("schema_version") is not None
                    else "inferred"
                    if metadata["schema"].get("parameters")
                    else "missing"
                ),
                "trust_policy": metadata.get("trust_policy"),
                "pending_schema": self.public_schema_proposal(device_id),
                "device_profile": metadata.get("device_profile"),
                "board_profile": metadata.get("board_profile"),
                "driver_catalog": public_driver_catalog(metadata.get("driver_ids", [])),
                "hardware_configuration": configurations.get(device_id, {}).get("config"),
                "configuration_version": configurations.get(device_id, {}).get("version", 0),
                "configuration_applied_version": configurations.get(device_id, {}).get("applied_version", 0),
                "configuration_updated_at": configurations.get(device_id, {}).get("updated_at"),
                "configuration_applied_at": configurations.get(device_id, {}).get("applied_at"),
                "configuration_sync_pending": device_id in self.deferred_connection_automations and metadata.get("device_profile") == "universal",
                "configuration_status": configurations.get(device_id, {}).get(
                    "status", "not_configured" if metadata.get("device_profile") == "universal" else None
                ),
                "configuration_error": configurations.get(device_id, {}).get("error")
            })

        known_ids = {item["device"] for item in result}

        for device_id in self.devices:
            if device_id not in known_ids:
                metadata = self.device_metadata.get(
                    device_id, {"schema": self.default_legacy_schema()}
                )
                result.append({
                    "device": device_id,
                    "name": metadata.get("name"),
                    "display_name": metadata.get("display_name"),
                    "connected": True,
                    "last_seen": None,
                    "last_telemetry_at": None,
                    "telemetry_stale_after_seconds": self.DEVICE_STALE_SECONDS,
                    "parameters": self.device_states.get(device_id, {}),
                    "schema": metadata["schema"],
                    "protocol_version": metadata.get("protocol_version"),
                    "firmware_version": metadata.get("firmware_version"),
                    "command_dedup": metadata.get("command_dedup", False),
                    "schema_version": metadata.get("schema_version"),
                    "schema_source": (
                        "device"
                        if metadata.get("schema_version") is not None
                        else "inferred"
                        if metadata["schema"].get("parameters")
                        else "missing"
                    ),
                    "trust_policy": metadata.get("trust_policy"),
                    "pending_schema": self.public_schema_proposal(device_id),
                    "device_profile": metadata.get("device_profile"),
                    "board_profile": metadata.get("board_profile"),
                    "driver_catalog": public_driver_catalog(metadata.get("driver_ids", [])),
                    "hardware_configuration": configurations.get(device_id, {}).get("config"),
                    "configuration_version": configurations.get(device_id, {}).get("version", 0),
                    "configuration_applied_version": configurations.get(device_id, {}).get("applied_version", 0),
                    "configuration_updated_at": configurations.get(device_id, {}).get("updated_at"),
                    "configuration_applied_at": configurations.get(device_id, {}).get("applied_at"),
                    "configuration_sync_pending": device_id in self.deferred_connection_automations and metadata.get("device_profile") == "universal",
                    "configuration_status": configurations.get(device_id, {}).get(
                        "status", "not_configured" if metadata.get("device_profile") == "universal" else None
                    ),
                    "configuration_error": configurations.get(device_id, {}).get("error")
                })

        return self.management.decorate_devices(result)


    def finalize_restored_database(self):
        self.management.last_cleanup_at = None
        self.management.last_cleanup_deleted = 0
        self.init_db()
        self.sync_device_configuration_exports()
        timestamp = self.current_time()
        with self.database_connection() as conn:
            conn.execute("UPDATE devices SET connected = 0")
            conn.execute("""
                UPDATE command_queue
                SET status = 'unavailable', updated_at = ?,
                    error = 'Command queue cleared during database restore.'
                WHERE status IN ('queued', 'sent')
            """, (timestamp,))


    async def restore_database_backup(self, backup_id):


        if self.database_restore_in_progress:
            return False, "A database restore is already in progress."
        if (
            not isinstance(backup_id, str)
            or not self.DATABASE_BACKUP_ID_PATTERN.fullmatch(backup_id)
        ):
            return False, "Invalid backup identifier."

        backup_path = self.DATABASE_BACKUP_DIR / backup_id
        try:
            self.validate_database_backup(backup_path)
            safety_backup = self.create_database_backup(preserve_ids={backup_id})
        except (OSError, sqlite3.Error, ValueError) as error:
            return False, f"Could not prepare the restore: {error}"

        safety_path = self.DATABASE_BACKUP_DIR / safety_backup["backup_id"]
        self.database_restore_in_progress = True
        restored = False
        restore_started = False
        recovery_message = None
        try:
            await self.management.shutdown()
            applying_tasks = [
                self.cancel_configuration_apply(device_id, pending=True)
                for device_id in list(self.configuration_apply_tasks)
            ]
            if applying_tasks:
                await asyncio.gather(*applying_tasks, return_exceptions=True)
            trigger_tasks = list(self.automation_trigger_tasks.values())
            for automation_id in list(self.automation_trigger_tasks):
                self.cancel_pending_automation_trigger(automation_id)
            if trigger_tasks:
                await asyncio.gather(*trigger_tasks, return_exceptions=True)
            self.automation_trigger_tasks.clear()
            self.automation_trigger_devices.clear()

            running_automations = list(self.automation_tasks.values())
            self.automation_stop_requests.update(self.automation_tasks)
            for task in running_automations:
                task.cancel()
            if running_automations:
                await asyncio.gather(*running_automations, return_exceptions=True)
            self.automation_tasks.clear()
            self.automation_stop_requests.clear()
            self.automation_waiting_tasks.clear()
            self.automation_restart_tasks.clear()

            for request in list(self.pending_device_connections.values()):
                request["decision"] = "deny"
                request["remember"] = False
                decision_event = request.get("decision_event")
                if decision_event is not None:
                    decision_event.set()

            for pending in list(self.pending_commands.values()):
                timeout_task = pending.get("timeout_task")
                if timeout_task is not None:
                    timeout_task.cancel()
                result_future = pending.get("result_future")
                if result_future is not None and not result_future.done():
                    result_future.set_result({
                        "status": "unavailable",
                        "error": "Gateway database was restored."
                    })
            self.pending_commands.clear()

            device_sockets = list(self.devices.values())
            active_device_tasks = list(self.device_handler_tasks)
            self.devices.clear()
            self.configuration_confirmed_devices.clear()
            self.device_wire_ids.clear()
            self.last_device_heartbeat.clear()
            self.last_device_telemetry.clear()
            self.last_parameter_telemetry.clear()
            self.last_device_probe.clear()
            self.fresh_device_parameters.clear()
            self.device_states.clear()
            self.device_metadata.clear()
            self.pending_device_schemas.clear()
            self.deferred_device_schemas.clear()
            self.deferred_connection_automations.clear()

            async def close_device(websocket):
                try:
                    await asyncio.wait_for(
                        websocket.close(code=1012, reason="Gateway database restored"),
                        timeout=3
                    )
                except Exception:
                    pass

            if device_sockets:
                await asyncio.gather(
                    *(close_device(websocket) for websocket in device_sockets),
                    return_exceptions=True
                )
            if active_device_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*active_device_tasks, return_exceptions=True),
                    timeout=10
                )

            restore_started = True
            self.restore_database_file(backup_path)
            self.finalize_restored_database()
            restored = True
        except Exception as error:
            if restore_started:
                try:
                    self.restore_database_file(safety_path)
                    self.finalize_restored_database()
                except Exception as recovery_error:
                    print(f"[BACKUP] Safety restore failed: {recovery_error}")
                    return False, f"Restore failed and safety recovery also failed: {recovery_error}"
                recovery_message = f"Restore failed; the previous database was recovered: {error}"
            else:
                return False, f"Restore could not start: {error}"
        finally:
            self.database_restore_in_progress = False

        if restored or recovery_message is not None:
            await self.send_device_list()
            await self.send_parameter_ui_preferences()
            await self.send_device_access_state()
            await self.send_command_queue_state()
            await self.send_event_history()
            await self.publish_automation_list()
            await self.send_database_backups()
            await self.management.publish()
            await self.broadcast({"type": "gateway_settings", **self.management.settings()})
            await self.broadcast({"type": "history_settings", **self.history.settings()})
        if restored:
            return True, "Database restored. Devices have been disconnected and can reconnect now."
        if recovery_message is not None:
            return False, recovery_message
        return False, "Database restore failed."
