import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.exceptions import ConnectionClosed

from device_configuration import (
    DRIVERS,
    public_driver_catalog,
    validate_device_configuration
)

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# py -m http.server 8080 --directory web

# =========================
# НАСТРОЙКИ
# =========================

BASE_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0").strip()
try:
    PORT = int(os.environ.get("GATEWAY_PORT", "8765"))
except ValueError as error:
    raise RuntimeError("GATEWAY_PORT must be an integer.") from error

if not HOST or not 1 <= PORT <= 65535:
    raise RuntimeError("Configure GATEWAY_HOST and GATEWAY_PORT correctly.")

DB_PATH_VALUE = os.environ.get("GATEWAY_DB_PATH", str(BASE_DIR / "gateway.db"))
if not DB_PATH_VALUE.strip():
    raise RuntimeError("GATEWAY_DB_PATH must not be empty.")
DB_PATH = Path(DB_PATH_VALUE).expanduser()
DATABASE_BACKUP_DIR = DB_PATH.parent / "backups"
DEVICE_CONFIGURATION_EXPORT_DIR = DB_PATH.parent / "schemas"
DATABASE_BACKUP_LIMIT = 20
DATABASE_BACKUP_DOWNLOAD_LIMIT = 512 * 1024 * 1024
DEVICE_CONFIGURATION_EXPORT_DOWNLOAD_LIMIT = 1024 * 1024
DATABASE_BACKUP_ID_PATTERN = re.compile(
    r"gateway-\d{8}-\d{6}-[a-f0-9]{32}\.sqlite3"
)

# Активные ESP32: device_id -> WebSocket
devices = {}
device_wire_ids = {}
device_handler_tasks = set()

# Подключённые браузеры
browser_clients = set()

# Последние известные состояния устройств
device_states = {}
device_metadata = {}
last_device_heartbeat = {}
fresh_device_parameters = {}
deferred_connection_automations = set()
pending_device_schemas = {}
deferred_device_schemas = {}
pending_device_connections = {}

# Команды, ожидающие результата: command_id -> контекст команды.
pending_commands = {}
automation_tasks = {}
automation_trigger_tasks = {}
automation_trigger_devices = {}
database_restore_in_progress = False
automation_stop_requests = set()
automation_waiting_tasks = set()
automation_restart_tasks = set()
COMMAND_TIMEOUT_SECONDS = 5
EVENTS_PAGE_SIZE = 30
DEVICE_STALE_SECONDS = 45
MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 60 * 1024
DEVICE_APPROVAL_TIMEOUT_SECONDS = 180
COMMAND_RETRY_LIMIT = 3

# =========================
# ВРЕМЯ
# =========================

def current_time():
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


# =========================
# SQLITE
# =========================

GATEWAY_SESSION_STARTED_AT = None
GATEWAY_RUNTIME_CHECKPOINT_SECONDS = 30


@contextmanager
def database_connection():
    """Commit or roll back the transaction, then always release its file handle."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        with connection:
            yield connection


def init_db():
    global GATEWAY_SESSION_STARTED_AT

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with database_connection() as conn:
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
        interrupted_at = current_time()
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
                schema_hash, last_seen or current_time()
            ))

        # A command sent just before a Gateway restart may have reached the
        # device without its result reaching SQLite. Re-delivery keeps the
        # same command ID so compatible devices can safely replay their ACK.
        conn.execute("""
            UPDATE command_queue SET status = 'queued', updated_at = ?
            WHERE status = 'sent'
        """, (current_time(),))
        # A configuration may have reached the device immediately before a
        # Gateway restart. Reapply the saved desired configuration on reconnect.
        conn.execute("""
            UPDATE device_configurations
            SET status = 'pending', updated_at = ?
            WHERE status = 'applying'
        """, (current_time(),))
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

    print(f"База данных: {DB_PATH}")

    GATEWAY_SESSION_STARTED_AT = datetime.now(timezone.utc)


def checkpoint_gateway_runtime(final=False):
    now = datetime.now(timezone.utc)
    with database_connection() as conn:
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


def get_database_stats():
    now = datetime.now(timezone.utc)
    with database_connection() as conn:
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
        max(0.0, (now - GATEWAY_SESSION_STARTED_AT).total_seconds())
        if GATEWAY_SESSION_STARTED_AT
        else 0.0
    )
    database_size_bytes = 0
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
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
        ),
        "automation_count": automation_count,
        "automation_run_count": automation_run_count,
        "device_count": device_count,
        "ever_connected_device_count": ever_connected_device_count,
        "connected_device_count": len(devices),
        "event_count": event_count,
        "schema_snapshot_count": schema_snapshot_count,
        "device_configuration_count": device_configuration_count,
        "device_configuration_history_count": device_configuration_history_count,
        "command_record_count": command_record_count,
        "session_uptime_seconds": session_uptime_seconds,
        "total_uptime_seconds": total_uptime_seconds,
        "sampled_at": now.timestamp()
    }


def database_backup_metadata(path):
    stat = path.stat()
    return {
        "backup_id": path.name,
        "created_at": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).astimezone().isoformat(timespec="seconds"),
        "size_bytes": stat.st_size
    }


def list_database_backups():
    if not DATABASE_BACKUP_DIR.is_dir():
        return []
    backups = [
        path for path in DATABASE_BACKUP_DIR.iterdir()
        if path.is_file() and DATABASE_BACKUP_ID_PATTERN.fullmatch(path.name)
    ]
    backups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [database_backup_metadata(path) for path in backups[:DATABASE_BACKUP_LIMIT]]


def create_database_backup(prune=True, preserve_ids=()):
    if not DB_PATH.is_file():
        raise FileNotFoundError("Gateway database does not exist.")

    DATABASE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_id = f"gateway-{stamp}-{uuid.uuid4().hex}.sqlite3"
    backup_path = DATABASE_BACKUP_DIR / backup_id
    temporary_path = DATABASE_BACKUP_DIR / f".{backup_id}.tmp"
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as source:
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
            path for path in DATABASE_BACKUP_DIR.iterdir()
            if path.is_file() and DATABASE_BACKUP_ID_PATTERN.fullmatch(path.name)
        ]
        backups.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        preserved_ids = set(preserve_ids)
        remaining_count = len(backups)
        for old_path in reversed(backups):
            if remaining_count <= DATABASE_BACKUP_LIMIT:
                break
            if old_path.name in preserved_ids:
                continue
            try:
                old_path.unlink()
                remaining_count -= 1
            except OSError as error:
                print(f"[BACKUP] Could not prune {old_path.name}: {error}")
    return database_backup_metadata(backup_path)


def validate_database_backup(path):
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


def restore_database_file(path):
    validate_database_backup(path)
    with closing(sqlite3.connect(path, timeout=30)) as source:
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as destination:
            source.backup(destination, pages=256, sleep=0.01)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("Restored database integrity check failed.")


def handle_http_request(connection, request):
    backup_match = re.fullmatch(
        r"/gateway-backups/(gateway-\d{8}-\d{6}-[a-f0-9]{32}\.sqlite3)",
        request.path
    )
    schema_match = re.fullmatch(
        r"/gateway-schemas/([A-Za-z][A-Za-z0-9_.-]{0,63})\.json",
        request.path
    )
    if backup_match is None and schema_match is None:
        return None
    if request.method != "GET":
        status_code, reason, body = 405, "Method Not Allowed", b"Method not allowed"
    elif schema_match is not None:
        device_id = schema_match.group(1)
        schema_path = DEVICE_CONFIGURATION_EXPORT_DIR / f"{device_id}.json"
        try:
            if not schema_path.is_file():
                status_code, reason, body = 404, "Not Found", b"Schema export not found"
            elif schema_path.stat().st_size > DEVICE_CONFIGURATION_EXPORT_DOWNLOAD_LIMIT:
                status_code, reason, body = 413, "Content Too Large", b"Schema export is too large"
            else:
                body = schema_path.read_bytes()
                status_code, reason = 200, "OK"
        except OSError:
            status_code, reason, body = 404, "Not Found", b"Schema export not found"
    else:
        backup_path = DATABASE_BACKUP_DIR / backup_match.group(1)
        try:
            if not backup_path.is_file():
                status_code, reason, body = 404, "Not Found", b"Backup not found"
            elif backup_path.stat().st_size > DATABASE_BACKUP_DOWNLOAD_LIMIT:
                status_code, reason, body = 413, "Content Too Large", b"Backup is too large to download"
            else:
                body = backup_path.read_bytes()
                status_code, reason = 200, "OK"
        except OSError:
            status_code, reason, body = 404, "Not Found", b"Backup not found"

    headers = Headers()
    headers["Content-Type"] = (
        "application/json; charset=utf-8"
        if schema_match is not None else "application/octet-stream"
    )
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store"
    headers["X-Content-Type-Options"] = "nosniff"
    if status_code == 200:
        filename = (
            f"{schema_match.group(1)}.json"
            if schema_match is not None else backup_path.name
        )
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    headers["Connection"] = "close"
    return Response(status_code, reason, headers, body)


async def gateway_runtime_checkpoint_loop():
    while True:
        await asyncio.sleep(GATEWAY_RUNTIME_CHECKPOINT_SECONDS)
        checkpoint_gateway_runtime()


def schema_values_match(actual, expected):
    """Compare JSON values without treating booleans as the numbers 0 and 1."""
    pairs = [(actual, expected)]
    while pairs:
        left, right = pairs.pop()
        if type(left) in (int, float) and type(right) in (int, float):
            if left != right:
                return False
            continue
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            pairs.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pairs.extend(zip(left, right))
        elif left != right:
            return False
    return True


def is_schema_value(value, parameter_schema):
    parameter_type = parameter_schema["type"]

    if parameter_type == "boolean":
        return type(value) is bool
    if parameter_type == "integer":
        return type(value) is int
    if parameter_type == "number":
        return type(value) is int or (
            type(value) is float and math.isfinite(value)
        )
    if parameter_type == "string":
        return isinstance(value, str)
    if parameter_type == "json":
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
            return True
        except (TypeError, ValueError):
            return False
    if parameter_type == "enum":
        return any(
            type(value) is type(option["value"])
            and value == option["value"]
            for option in parameter_schema["options"]
        )

    return False


def value_within_schema_limits(value, definition):
    if "min" in definition and value < definition["min"]:
        return False
    if "max" in definition and value > definition["max"]:
        return False
    if "step" in definition:
        base = definition.get("min", 0)
        try:
            step_position = (value - base) / definition["step"]
        except (OverflowError, ZeroDivisionError):
            return False
        if not math.isfinite(step_position):
            return False
        if not math.isclose(
            step_position,
            round(step_position),
            rel_tol=0.0,
            abs_tol=1e-9
        ):
            return False
    return True


def validate_action_argument_definition(key, definition):
    allowed_fields = {
        "label", "type", "unit", "precision", "min", "max", "step",
        "options", "required"
    }
    supported_types = {"boolean", "integer", "number", "string", "enum"}
    argument_type = definition.get("type") if isinstance(definition, dict) else None
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key)
        or not isinstance(definition, dict)
        or not set(definition).issubset(allowed_fields)
        or not isinstance(argument_type, str)
        or argument_type not in supported_types
        or ("required" in definition and type(definition["required"]) is not bool)
    ):
        return False

    label = definition.get("label", key)
    if not isinstance(label, str) or not 1 <= len(label) <= 80:
        return False
    if "unit" in definition and (
        not isinstance(definition["unit"], str)
        or len(definition["unit"]) > 24
    ):
        return False
    if "precision" in definition and (
        type(definition["precision"]) is not int
        or not 0 <= definition["precision"] <= 8
    ):
        return False

    for bound in ("min", "max", "step"):
        if bound in definition and (
            type(definition[bound]) not in (int, float)
            or (
                type(definition[bound]) is float
                and not math.isfinite(definition[bound])
            )
        ):
            return False

    if argument_type == "integer" and any(
        bound in definition and type(definition[bound]) is not int
        for bound in ("min", "max", "step")
    ):
        return False

    if (
        any(bound in definition for bound in ("min", "max", "step"))
        and definition["type"] not in {"integer", "number"}
    ):
        return False
    if "step" in definition and definition["step"] <= 0:
        return False
    if (
        "min" in definition
        and "max" in definition
        and definition["min"] > definition["max"]
    ):
        return False

    if definition["type"] == "enum":
        options = definition.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= 32:
            return False
        if any(
            not isinstance(option, dict)
            or set(option) != {"label", "value"}
            or not isinstance(option["label"], str)
            or not 1 <= len(option["label"]) <= 80
            or type(option["value"]) not in (str, int, bool)
            for option in options
        ):
            return False
        if len({json.dumps(option["value"]) for option in options}) != len(options):
            return False
    elif "options" in definition:
        return False

    return True


def validate_action_arguments(action_definition, arguments):
    if not isinstance(arguments, dict) or len(arguments) > 32:
        return False

    definitions = action_definition.get("arguments", {})
    if any(argument_id not in definitions for argument_id in arguments):
        return False

    for argument_id, definition in definitions.items():
        if argument_id not in arguments:
            if definition.get("required", True):
                return False
            continue

        value = arguments[argument_id]
        if not is_schema_value(value, definition):
            return False
        if not value_within_schema_limits(value, definition):
            return False

    return True


def validate_schema(schema):
    try:
        serialized_size = len(
            json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError):
        return False

    if serialized_size > MAX_SCHEMA_BYTES:
        return False

    if not isinstance(schema, dict):
        return False

    format_version = schema.get("format_version")
    if format_version is not None and (
        type(format_version) is not int or format_version != 1
    ):
        return False

    actions = schema.get("actions", {})
    parameters = schema.get("parameters", {})
    components = schema.get("components", {})
    if (
        not isinstance(actions, dict)
        or not isinstance(parameters, dict)
        or not isinstance(components, dict)
        or len(components) > 64
    ):
        return False

    supported_component_categories = {
        "sensor", "actuator", "input", "output", "other"
    }
    for component_id, component in components.items():
        if (
            not isinstance(component_id, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", component_id)
            or not isinstance(component, dict)
            or not set(component).issubset({"label", "category", "description"})
            or not isinstance(component.get("label"), str)
            or not 1 <= len(component["label"]) <= 80
            or not isinstance(component.get("category"), str)
            or component.get("category") not in supported_component_categories
            or (
                "description" in component
                and (
                    not isinstance(component["description"], str)
                    or len(component["description"]) > 240
                )
            )
        ):
            return False

    capabilities = schema.get("capabilities", {})
    if (
        not isinstance(capabilities, dict)
        or len(capabilities) > 16
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key)
            or type(enabled) is not bool
            for key, enabled in capabilities.items()
        )
        or (
            capabilities.get("identify") is True
            and "identify" not in actions
        )
    ):
        return False

    if len(actions) > 32:
        return False
    if len(parameters) > 64:
        return False

    for key, definition in actions.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key)
            or not isinstance(definition, dict)
            or not set(definition).issubset({"label", "description", "arguments"})
            or not isinstance(definition.get("label", key), str)
            or not 1 <= len(definition.get("label", key)) <= 80
            or (
                "description" in definition
                and (
                    not isinstance(definition["description"], str)
                    or len(definition["description"]) > 240
                )
            )
        ):
            return False
        arguments = definition.get("arguments", {})
        if not isinstance(arguments, dict) or len(arguments) > 32:
            return False
        if any(
            not validate_action_argument_definition(argument_id, argument_definition)
            for argument_id, argument_definition in arguments.items()
        ):
            return False

    supported_types = {"boolean", "integer", "number", "string", "enum", "json"}
    allowed_parameter_fields = {
        "label", "description", "component", "type", "access", "unit",
        "precision", "min", "max", "step", "options", "control"
    }
    for key, definition in parameters.items():
        parameter_type = definition.get("type") if isinstance(definition, dict) else None
        access = definition.get("access") if isinstance(definition, dict) else None
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key)
            or not isinstance(definition, dict)
            or not set(definition).issubset(allowed_parameter_fields)
            or not isinstance(parameter_type, str)
            or parameter_type not in supported_types
            or not isinstance(access, str)
            or access not in {"read_only", "read_write"}
        ):
            return False

        if parameter_type == "json" and access != "read_only":
            return False

        if "control" in definition:
            control = definition["control"]
            if not isinstance(control, str):
                return False
            if control == "toggle":
                if parameter_type != "boolean" or access != "read_write":
                    return False
            elif control == "slider":
                if (
                    parameter_type not in {"integer", "number"}
                    or access != "read_write"
                    or "min" not in definition
                    or "max" not in definition
                    or type(definition["min"]) not in (int, float)
                    or type(definition["max"]) not in (int, float)
                    or definition["min"] >= definition["max"]
                ):
                    return False
            elif control == "select":
                if parameter_type != "enum" or access != "read_write":
                    return False
            elif control == "input":
                if (
                    parameter_type not in {"integer", "number", "string"}
                    or access != "read_write"
                ):
                    return False
            else:
                return False

        label = definition.get("label", key)
        if not isinstance(label, str) or not 1 <= len(label) <= 80:
            return False

        if "description" in definition and (
            not isinstance(definition["description"], str)
            or len(definition["description"]) > 240
        ):
            return False

        if "component" in definition and (
            not isinstance(definition["component"], str)
            or definition["component"] not in components
        ):
            return False

        if "unit" in definition and (
            not isinstance(definition["unit"], str)
            or len(definition["unit"]) > 24
        ):
            return False

        if "precision" in definition and (
            type(definition["precision"]) is not int
            or not 0 <= definition["precision"] <= 8
        ):
            return False

        for bound in ("min", "max", "step"):
            if bound in definition and (
                type(definition[bound]) not in (int, float)
                or (
                    type(definition[bound]) is float
                    and not math.isfinite(definition[bound])
                )
            ):
                return False

        if parameter_type == "integer" and any(
            bound in definition and type(definition[bound]) is not int
            for bound in ("min", "max", "step")
        ):
            return False

        if (
            any(bound in definition for bound in ("min", "max", "step"))
            and definition["type"] not in {"integer", "number"}
        ):
            return False
        if "step" in definition and definition["step"] <= 0:
            return False

        if (
            "min" in definition
            and "max" in definition
            and definition["min"] > definition["max"]
        ):
            return False

        if parameter_type == "enum":
            options = definition.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 32:
                return False
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("label"), str)
                or not 1 <= len(option["label"]) <= 80
                or type(option.get("value")) not in (str, int, bool)
                for option in options
            ):
                return False
            if len({json.dumps(option["value"]) for option in options}) != len(options):
                return False
        elif "options" in definition:
            return False

    modes = schema.get("modes", {})
    if not isinstance(modes, dict) or len(modes) > 8:
        return False
    if not (parameters or actions or modes or capabilities):
        return False
    mode_ids = set(modes)
    for mode_id, mode in modes.items():
        if (
            not isinstance(mode_id, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,47}", mode_id)
            or not isinstance(mode, dict)
            or not set(mode).issubset({
                "label", "description", "operations", "status_parameter",
                "status_value", "conflicts_with"
            })
        ):
            return False
        label = mode.get("label", mode_id)
        description = mode.get("description", "")
        operations = mode.get("operations")
        if (
            not isinstance(label, str) or not 1 <= len(label) <= 80
            or not isinstance(description, str) or len(description) > 240
            or not isinstance(operations, dict) or not operations
            or len(operations) > 3
            or not set(operations).issubset({"select", "enable", "disable"})
        ):
            return False
        for action_id in operations.values():
            if not isinstance(action_id, str) or action_id not in actions:
                return False

        has_status_parameter = "status_parameter" in mode
        has_status_value = "status_value" in mode
        if has_status_parameter != has_status_value:
            return False
        if has_status_parameter:
            status_parameter = mode["status_parameter"]
            status_definition = parameters.get(status_parameter) if isinstance(status_parameter, str) else None
            if (
                not status_definition
                or status_definition.get("access") not in {"read_only", "read_write"}
                or not is_schema_value(mode["status_value"], status_definition)
            ):
                return False

        conflicts = mode.get("conflicts_with", [])
        if (
            not isinstance(conflicts, list) or len(conflicts) > 8
            or any(not isinstance(item, str) or item == mode_id or item not in mode_ids for item in conflicts)
            or len(set(conflicts)) != len(conflicts)
        ):
            return False

    return True


def default_legacy_schema():
    # Без Schema Gateway не знает, безопасно ли менять параметр.
    # Состояние старых устройств добавится как read_only при первом отчёте.
    return {"parameters": {}, "actions": {}}


def inferred_definition(key, value):
    if not isinstance(key, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key
    ):
        return None

    if type(value) is bool:
        parameter_type = "boolean"
    elif type(value) is int:
        parameter_type = "integer"
    elif type(value) is float:
        if not math.isfinite(value):
            return None
        parameter_type = "number"
    elif isinstance(value, str):
        parameter_type = "string"
    else:
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return None
        parameter_type = "json"

    label = " ".join(part.capitalize() for part in re.split(r"[_-]+", key))
    return {
        "label": label or "Параметр",
        "type": parameter_type,
        "access": "read_only"
    }


def validate_parameters(schema, parameters, allow_unknown=False):
    if not isinstance(parameters, dict) or len(parameters) > 64:
        return False

    definitions = schema.get("parameters", {})
    for key, value in parameters.items():
        definition = definitions.get(key)
        if definition is None:
            if allow_unknown and inferred_definition(key, value) is not None:
                continue
            return False

        if (
            not is_schema_value(value, definition)
            or not value_within_schema_limits(value, definition)
        ):
            return False

    return True


def validate_parameters_during_schema_review(
    accepted_schema, proposed_schema, parameters
):
    if not isinstance(parameters, dict) or len(parameters) > 64:
        return False

    accepted = accepted_schema.get("parameters", {})
    proposed = proposed_schema.get("parameters", {})
    for key, value in parameters.items():
        valid_under_one_schema = False
        for definition in (accepted.get(key), proposed.get(key)):
            if definition is not None and (
                is_schema_value(value, definition)
                and value_within_schema_limits(value, definition)
            ):
                valid_under_one_schema = True
                break
        if not valid_under_one_schema:
            return False

    return True


def accepted_parameters_during_schema_review(
    accepted_schema, proposed_schema, parameters
):
    if not validate_parameters_during_schema_review(
        accepted_schema, proposed_schema, parameters
    ):
        return None

    definitions = accepted_schema.get("parameters", {})
    return {
        key: value
        for key, value in parameters.items()
        if key in definitions
        and is_schema_value(value, definitions[key])
        and value_within_schema_limits(value, definitions[key])
    }


def parameter_has_zero_one_values(definition):
    if not isinstance(definition, dict):
        return False
    parameter_type = definition.get("type")
    if parameter_type == "boolean":
        return True
    if parameter_type in {"integer", "number"}:
        return (
            definition.get("min") == 0
            and definition.get("max") == 1
            and (
                (parameter_type == "integer" and definition.get("step", 1) == 1)
                or (parameter_type == "number" and definition.get("step") == 1)
            )
        )
    if parameter_type == "enum":
        values = [option.get("value") for option in definition.get("options", [])]
        numeric_values = len(values) == 2 and all(type(value) is int for value in values)
        string_values = len(values) == 2 and all(isinstance(value, str) for value in values)
        return (
            numeric_values and 0 in values and 1 in values
        ) or (
            string_values and "0" in values and "1" in values
        )
    return False


def is_parameter_ui_preference_supported(control, definition):
    if not isinstance(definition, dict) or definition.get("access") != "read_write":
        return False

    parameter_type = definition.get("type")
    if control == "toggle":
        return parameter_type == "boolean"
    if control == "list":
        return parameter_has_zero_one_values(definition)
    if control == "input":
        return parameter_type in {"integer", "number"}
    if control == "slider":
        return (
            parameter_type in {"integer", "number"}
            and type(definition.get("min")) in (int, float)
            and type(definition.get("max")) in (int, float)
            and definition["min"] < definition["max"]
        )
    return False


def validate_parameter_ui_preferences(preferences, schema):
    if preferences is None:
        return {}, None
    if not isinstance(preferences, dict) or len(preferences) > 8:
        return None, "Можно задать отображение не более чем для восьми параметров."
    parameters = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    normalized = {}
    for parameter_id, control in preferences.items():
        definition = parameters.get(parameter_id)
        if (
            not isinstance(parameter_id, str)
            or not isinstance(control, str)
            or not isinstance(definition, dict)
            or definition.get("access") != "read_write"
        ):
            return None, "Настройка отображения ссылается на неизвестный или недоступный параметр."
        if control != "schema" and not is_parameter_ui_preference_supported(control, definition):
            return None, f"Вид элемента {control!r} несовместим с параметром {parameter_id!r}."
        normalized[parameter_id] = control
    return normalized, None


def apply_parameter_ui_preferences(conn, device_id, preferences):
    timestamp = current_time()
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


def get_device_parameter_ui_preferences(device_id):
    with database_connection() as conn:
        rows = conn.execute(
            "SELECT parameter_id, control FROM parameter_ui_preferences WHERE device_id = ?",
            (device_id,)
        ).fetchall()
    return {parameter_id: control for parameter_id, control in rows}


def prune_parameter_ui_preferences(conn, device_id, schema):
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
        if not is_parameter_ui_preference_supported(
            control, parameters.get(parameter_id)
        )
    ]
    if stale:
        conn.executemany(
            "DELETE FROM parameter_ui_preferences WHERE device_id = ? AND parameter_id = ?",
            [(device_id, parameter_id) for parameter_id in stale]
        )


def get_parameter_ui_preferences():
    with database_connection() as conn:
        rows = conn.execute(
            "SELECT device_id, parameter_id, control FROM parameter_ui_preferences"
        ).fetchall()

    preferences = {}
    for device_id, parameter_id, control in rows:
        preferences.setdefault(device_id, {})[parameter_id] = control
    return preferences


def save_parameter_ui_preference(device_id, parameter_id, control):
    if (
        not isinstance(device_id, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", device_id)
        or not isinstance(parameter_id, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", parameter_id)
        or not isinstance(control, str)
    ):
        return False, "Некорректный идентификатор устройства, параметра или вид элемента."

    with database_connection() as conn:
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
            if not is_parameter_ui_preference_supported(control, definition):
                return False, "Этот вид элемента несовместим с параметром в текущей Schema."

            conn.execute("""
                INSERT INTO parameter_ui_preferences (device_id, parameter_id, control, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id, parameter_id) DO UPDATE SET
                    control = excluded.control,
                    updated_at = excluded.updated_at
            """, (device_id, parameter_id, control, current_time()))
            result_message = "Вид элемента сохранён."
    write_device_configuration_export(device_id)
    return True, result_message


def save_device_metadata(device_id, metadata):
    schema_json = json.dumps(metadata["schema"], ensure_ascii=False)
    timestamp = current_time()

    with database_connection() as conn:
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
            or not schemas_match(previous_schema, metadata["schema"])
            or previous[1] != metadata.get("schema_version")
            or previous[2] != metadata.get("firmware_version")
        ) if previous is not None else True
        if snapshot_changed:
            record_schema_snapshot_in_connection(
                conn, device_id, metadata, timestamp, "accepted"
            )
        prune_parameter_ui_preferences(conn, device_id, metadata["schema"])

    device_metadata[device_id] = metadata
    write_device_configuration_export(device_id)


def load_device_metadata(device_id):
    metadata = device_metadata.get(device_id)
    if metadata is not None:
        return dict(metadata)

    with database_connection() as conn:
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
        schema = default_legacy_schema()
    if not validate_schema(schema):
        schema = default_legacy_schema()

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


def load_device_configuration(device_id):
    with database_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT config_version, config_json, config_hash, status,
                   last_error, updated_at, applied_at
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
        "applied_at": row["applied_at"]
    }


def save_device_configuration(device_id, configuration, ui_preferences=None):
    canonical = json.dumps(
        configuration, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False
    )
    config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    timestamp = current_time()
    with database_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT config_version, config_hash FROM device_configurations WHERE device_id = ?",
            (device_id,)
        ).fetchone()
        if row is not None and row[1] == config_hash:
            apply_parameter_ui_preferences(conn, device_id, ui_preferences or {})
            return {
                "config_version": row[0], "config_hash": config_hash,
                "unchanged": True
            }
        version = (row[0] if row else 0) + 1
        status = "applying" if device_id in devices else "pending"
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
                updated_at = excluded.updated_at,
                applied_at = NULL
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
        apply_parameter_ui_preferences(conn, device_id, ui_preferences or {})
    return {"config_version": version, "config_hash": config_hash, "unchanged": False}


def update_device_configuration_status(
    device_id, status, error=None, applied_at=None
):
    with database_connection() as conn:
        conn.execute("""
            UPDATE device_configurations
            SET status = ?, last_error = ?,
                applied_at = COALESCE(?, applied_at), updated_at = ?
            WHERE device_id = ?
        """, (status, error, applied_at, current_time(), device_id))
        conn.execute("""
            UPDATE device_configuration_history
            SET status = ?
            WHERE device_id = ? AND config_version = (
                SELECT config_version FROM device_configurations WHERE device_id = ?
            )
        """, (status, device_id, device_id))


def device_configuration_export_document(device_id):
    metadata = device_metadata.get(device_id) or load_device_metadata(device_id)
    configuration = load_device_configuration(device_id)
    if metadata is None and configuration is None:
        return None
    document = {
        "export_format": "smartest-home-device",
        "export_version": 1,
        "exported_at": current_time(),
        "device_id": device_id,
        "schema_version": metadata.get("schema_version") if metadata else None,
        "firmware_version": metadata.get("firmware_version") if metadata else None,
        "schema": metadata.get("schema") if metadata else None,
        "device_profile": metadata.get("device_profile") if metadata else None,
        "board_profile": metadata.get("board_profile") if metadata else None,
        "hardware_configuration": configuration["config"] if configuration else None,
        "ui_preferences": get_device_parameter_ui_preferences(device_id),
        "configuration_version": configuration["config_version"] if configuration else None,
        "configuration_hash": configuration["config_hash"] if configuration else None,
        "configuration_status": configuration["status"] if configuration else None
    }
    return document


def write_device_configuration_export(device_id):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", device_id or ""):
        return False
    document = device_configuration_export_document(device_id)
    if document is None:
        return False
    export_dir = DEVICE_CONFIGURATION_EXPORT_DIR
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


def sync_device_configuration_exports():
    """Rebuild portable JSON views from SQLite, the source of truth."""
    with database_connection() as conn:
        device_ids = [row[0] for row in conn.execute(
            "SELECT device_id FROM devices ORDER BY device_id"
        ).fetchall()]
    failed = [
        device_id for device_id in device_ids
        if not write_device_configuration_export(device_id)
    ]
    if failed:
        print(f"[CONFIG] Could not refresh {len(failed)} device JSON export(s).")
    return len(device_ids) - len(failed)


async def send_device_configuration(device_id, websocket=None):
    metadata = device_metadata.get(device_id) or load_device_metadata(device_id)
    if metadata is None or metadata.get("device_profile") != "universal":
        return False
    target = websocket or devices.get(device_id)
    if target is None:
        return False
    record = load_device_configuration(device_id)
    if record is None:
        await send_json(target, {"type": "device_config_request"})
        return False
    update_device_configuration_status(device_id, "applying")
    await send_device_list()
    try:
        await send_json(target, {
            "type": "device_config_apply",
            "device": firmware_identity_for(device_id),
            "config_version": record["config_version"],
            "config_hash": record["config_hash"],
            "configuration": record["config"]
        })
    except ConnectionClosed:
        update_device_configuration_status(device_id, "pending")
        await send_device_list()
        return False
    return True


async def handle_device_configuration_result(device_id, wire_device_id, message):
    if message.get("device") != wire_device_id:
        return
    version = message.get("config_version")
    config_hash = message.get("config_hash")
    success = message.get("success")
    error = message.get("error")
    metadata = device_metadata.get(device_id) or load_device_metadata(device_id)
    record = load_device_configuration(device_id)

    if (
        metadata is None
        or metadata.get("device_profile") != "universal"
        or type(version) is not int
        or type(success) is not bool
        or not isinstance(config_hash, str)
        or not isinstance(error, (str, type(None)))
        or (isinstance(error, str) and len(error) > 240)
    ):
        return

    if version == 0 and record is None:
        _, expected_schema, validation_error = validate_device_configuration(
            {"format_version": 1, "components": []}, metadata.get("driver_ids", [])
        )
        schema = message.get("schema")
        if (
            not success
            or config_hash != ""
            or validation_error
            or type(message.get("schema_version")) is not int
            or message.get("schema_version") != 1
            or not validate_schema(schema)
            or not schemas_match(expected_schema, schema)
        ):
            print(f"[CONFIG] {device_id}: invalid empty configuration report.")
            return
        metadata = dict(metadata)
        metadata.update({"schema": expected_schema, "schema_version": 1})
        save_device_metadata(device_id, metadata)
        update_device_configuration_status(device_id, "not_configured")
        write_device_configuration_export(device_id)
        await send_device_list()
        return

    if (
        record is None
        or version != record["config_version"]
        or config_hash != record["config_hash"]
    ):
        return

    normalized, expected_schema, validation_error = validate_device_configuration(
        record["config"], metadata.get("driver_ids", [])
    )
    reported_schema = message.get("schema")
    if success and (
        validation_error
        or not validate_schema(reported_schema)
        or not schemas_match(expected_schema, reported_schema)
        or type(message.get("schema_version")) is not int
        or message.get("schema_version") != version
    ):
        success = False
        error = validation_error or "Schema устройства не соответствует принятой конфигурации."

    if success:
        metadata = dict(metadata)
        metadata.update({
            "schema": expected_schema,
            "schema_version": version,
            "device_profile": "universal"
        })
        save_device_metadata(device_id, metadata)
        update_device_configuration_status(
            device_id, "applied", applied_at=current_time()
        )
        event = f"Аппаратная конфигурация версии {version} применена устройством."
        print(f"[CONFIG] {device_id}: applied configuration v{version}.")
    else:
        error = error or "Устройство отклонило аппаратную конфигурацию."
        update_device_configuration_status(device_id, "failed", error=error)
        event = f"Ошибка применения аппаратной конфигурации версии {version}: {error}"
        print(f"[CONFIG] {device_id}: configuration v{version} rejected: {error}")

    write_device_configuration_export(device_id)
    save_device_event(device_id, "configuration", event)
    await send_device_list()
    await send_event_history()
    await send_database_stats()
    await broadcast({
        "type": "device_configuration_apply_result",
        "success": success,
        "device": device_id,
        "config_version": version,
        "status": "applied" if success else "failed",
        "message": "Конфигурация применена." if success else error
    })


async def handle_device_configuration_report(device_id, wire_device_id, message):
    """Reconcile a configuration already stored in ESP32 NVS after DB loss."""
    metadata = device_metadata.get(device_id) or load_device_metadata(device_id)
    if (
        metadata is None
        or metadata.get("device_profile") != "universal"
        or message.get("device") != wire_device_id
        or type(message.get("config_version")) is not int
        or type(message.get("schema_version")) is not int
        or not isinstance(message.get("config_hash"), str)
    ):
        return

    version = message["config_version"]
    if version == 0:
        await handle_device_configuration_result(device_id, wire_device_id, {
            "device": wire_device_id,
            "config_version": 0,
            "config_hash": message["config_hash"],
            "success": True,
            "schema_version": message["schema_version"],
            "schema": message.get("schema")
        })
        return
    if version < 1:
        return

    # A saved Gateway configuration is authoritative. The report is only used
    # to recover an empty Gateway configuration table.
    if load_device_configuration(device_id) is not None:
        await send_device_configuration(device_id)
        return

    normalized, expected_schema, validation_error = validate_device_configuration(
        message.get("configuration"), metadata.get("driver_ids", [])
    )
    reported_schema = message.get("schema")
    config_hash = message["config_hash"]
    if validation_error:
        error = validation_error
    else:
        canonical = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        error = None
        if config_hash != expected_hash:
            error = "Хэш аппаратной конфигурации не совпадает с её содержимым."
        elif (
            message["schema_version"] != version
            or not validate_schema(reported_schema)
            or not schemas_match(expected_schema, reported_schema)
        ):
            error = "Schema устройства не соответствует сохранённой конфигурации."
    if error:
        print(f"[CONFIG] {device_id}: stored configuration report rejected: {error}")
        save_device_event(
            device_id,
            "configuration",
            f"Gateway не восстановил конфигурацию из ESP32: {error}"
        )
        await send_event_history()
        await broadcast({
            "type": "device_configuration_apply_result",
            "success": False,
            "device": device_id,
            "config_version": version,
            "status": "failed",
            "message": error
        })
        return

    timestamp = current_time()
    with database_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO device_configurations (
                device_id, config_version, config_json, config_hash,
                status, last_error, updated_at, applied_at
            ) VALUES (?, ?, ?, ?, 'applied', NULL, ?, ?)
        """, (device_id, version, canonical, config_hash, timestamp, timestamp))
        conn.execute("""
            INSERT INTO device_configuration_history (
                device_id, config_version, config_json, config_hash,
                created_at, status
            ) VALUES (?, ?, ?, ?, ?, 'applied')
        """, (device_id, version, canonical, config_hash, timestamp))

    metadata = dict(metadata)
    metadata.update({"schema": expected_schema, "schema_version": version})
    save_device_metadata(device_id, metadata)
    write_device_configuration_export(device_id)
    save_device_event(
        device_id,
        "configuration",
        f"Gateway восстановил аппаратную конфигурацию версии {version} из ESP32."
    )
    await send_device_list()
    await send_event_history()
    await send_database_stats()
    await broadcast({
        "type": "device_configuration_apply_result",
        "success": True,
        "device": device_id,
        "config_version": version,
        "status": "applied",
        "message": f"Конфигурация версии {version} восстановлена из устройства."
    })


def resolve_device_identity(firmware_id):
    with database_connection() as conn:
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


def firmware_identity_for(device_id):
    if device_id in device_wire_ids:
        return device_wire_ids[device_id]
    with database_connection() as conn:
        row = conn.execute(
            "SELECT firmware_id FROM devices WHERE device_id = ?",
            (device_id,)
        ).fetchone()
    return row[0] if row and row[0] else device_id


def replace_device_references(value, old_id, new_id):
    changed = False
    if isinstance(value, list):
        for item in value:
            changed = replace_device_references(item, old_id, new_id) or changed
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
                changed = replace_device_references(item, old_id, new_id) or changed
    return changed


def update_device_identity(device_id, new_device_id, display_name):
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
    if changing_id and device_id in devices:
        return False, "Чтобы изменить ID, сначала отключите устройство."
    if changing_id and device_id in pending_device_connections:
        return False, "Сначала обработайте ожидающий запрос на подключение устройства."
    if changing_id and any(task is not None and not task.done() for task in automation_tasks.values()):
        return False, "Сначала дождитесь завершения или остановите выполняющиеся сценарии."
    if changing_id and any(
        pending_id != device_id and pending_id.casefold() == new_device_id.casefold()
        for pending_id in pending_device_connections
    ):
        return False, "Такой ID уже запрашивает подключение."

    with database_connection() as conn:
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

        timestamp = current_time()
        if changing_id:
            conn.execute(
                "UPDATE devices SET device_id = ?, display_name = ? WHERE device_id = ?",
                (new_device_id, display_name, device_id)
            )
            for table in (
                "device_events", "device_schema_history", "command_queue",
                "parameter_ui_preferences", "device_configurations",
                "device_configuration_history"
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
                if replace_device_references(config, device_id, new_device_id):
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

    metadata = device_metadata.pop(device_id, None)
    if metadata is not None:
        metadata["display_name"] = display_name
        device_metadata[new_device_id] = metadata
    old_export = DEVICE_CONFIGURATION_EXPORT_DIR / f"{device_id}.json"
    if changing_id:
        try:
            old_export.unlink(missing_ok=True)
        except OSError:
            pass
    write_device_configuration_export(new_device_id)
    for state_map in (
        device_states, last_device_heartbeat, fresh_device_parameters,
        pending_device_schemas, deferred_device_schemas
    ):
        if device_id in state_map:
            state_map[new_device_id] = state_map.pop(device_id)
    if device_id in deferred_connection_automations:
        deferred_connection_automations.discard(device_id)
        deferred_connection_automations.add(new_device_id)
    for pending in pending_commands.values():
        if pending.get("device") == device_id:
            pending["device"] = new_device_id

    return True, f"Имя и ID устройства обновлены. В Blockly используется ID «{new_device_id}»."


def device_supports_command_dedup(device_id):
    metadata = device_metadata.get(device_id) or load_device_metadata(device_id)
    return metadata is not None and metadata.get("command_dedup") is True


def record_schema_snapshot_in_connection(conn, device_id, metadata, timestamp, source):
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


def get_schema_history(device_id):
    with database_connection() as conn:
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
        if not validate_schema(schema):
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


def delete_schema_history_item(device_id, history_id):
    with database_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM device_schema_history WHERE id = ? AND device_id = ?",
            (history_id, device_id)
        )
        return cursor.rowcount > 0


def restore_device_schema(device_id, history_id):
    if device_id in devices:
        return False, "Отключите устройство перед восстановлением Schema."

    history = next(
        (item for item in get_schema_history(device_id) if item["id"] == history_id),
        None
    )
    if history is None:
        return False, "Снимок Schema не найден."

    with database_connection() as conn:
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
        prune_parameter_ui_preferences(conn, device_id, history["schema"])

    device_metadata.pop(device_id, None)
    pending_device_schemas.pop(device_id, None)
    deferred_device_schemas.pop(device_id, None)
    write_device_configuration_export(device_id)
    save_device_event(
        device_id,
        "schema",
        f"Восстановлена Schema из истории (снимок {history_id})"
    )
    return True, "Schema Gateway восстановлена. При следующем подключении она будет сравнена с Schema устройства."


def get_device_trust_policy(device_id):
    with database_connection() as conn:
        row = conn.execute(
            "SELECT trust_policy FROM devices WHERE device_id = ?",
            (device_id,)
        ).fetchone()
    return row[0] if row else None


def set_device_trust_policy(device_id, policy, name=None):
    with database_connection() as conn:
        conn.execute("""
            INSERT INTO devices (device_id, connected, trust_policy, name)
            VALUES (?, 0, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                trust_policy = excluded.trust_policy,
                name = COALESCE(excluded.name, devices.name)
        """, (device_id, policy, name))
    if device_id in device_metadata:
        device_metadata[device_id]["trust_policy"] = policy


def get_blocked_devices():
    with database_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT device_id, COALESCE(display_name, name) AS name, last_seen
            FROM devices
            WHERE trust_policy = 'deny'
            ORDER BY device_id
        """).fetchall()
    return [dict(row) for row in rows]


def get_pending_device_requests():
    return [
        {
            "request_id": request["request_id"],
            "device": device_id,
            "name": request["metadata"].get("name"),
            "firmware_version": request["metadata"].get("firmware_version"),
            "protocol_version": request["metadata"].get("protocol_version")
        }
        for device_id, request in pending_device_connections.items()
        if not request["decision_event"].is_set()
    ]


async def send_device_access_state(websocket=None):
    messages = (
        {
            "type": "device_access_requests",
            "requests": get_pending_device_requests()
        },
        {
            "type": "blocked_devices",
            "devices": get_blocked_devices()
        }
    )
    targets = [websocket] if websocket is not None else list(browser_clients)
    for target in targets:
        for message in messages:
            try:
                await send_json(target, message)
            except Exception:
                if target in browser_clients:
                    browser_clients.discard(target)


def load_pending_commands():
    with database_connection() as conn:
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
        pending_commands[command_id] = {
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


def create_command_queue_record(pending):
    timestamp = current_time()
    with database_connection() as conn:
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


def update_command_queue_record(pending, status, result=None, error=None):
    with database_connection() as conn:
        conn.execute("""
            UPDATE command_queue
            SET status = ?, attempts = ?, updated_at = ?,
                result_json = ?, error = ?
            WHERE command_id = ?
        """, (
            status, pending.get("attempts", 0), current_time(),
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            error, pending["command_id"]
        ))


async def send_command_queue_state(websocket=None):
    commands = [
        {
            "command_id": pending["command_id"],
            "device": pending["device"],
            "operation": pending["operation"],
            "status": pending.get("status", "queued"),
            "attempts": pending.get("attempts", 0)
        }
        for pending in pending_commands.values()
    ]
    message = {"type": "command_queue", "commands": commands}
    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def notify_command_queued(pending):
    pending["status"] = "queued"
    update_command_queue_record(pending, "queued")
    if pending.get("browser") is not None:
        try:
            await send_json(pending["browser"], {
                "type": "command_queued",
                "command_id": pending["command_id"],
                "device": pending["device"],
                "attempts": pending.get("attempts", 0)
            })
        except Exception:
            pending["browser"] = None
    await send_command_queue_state()


async def deliver_pending_command(pending):
    if pending.get("sending"):
        return False
    device_id = pending["device"]
    websocket = devices.get(device_id)
    if websocket is None or device_id in pending_device_schemas:
        await notify_command_queued(pending)
        return False

    pending["sending"] = True
    try:
        pending["attempts"] = pending.get("attempts", 0) + 1
        pending["status"] = "sent"
        update_command_queue_record(pending, "sent")
        payload = dict(pending["payload"])
        payload["device"] = firmware_identity_for(device_id)
        await send_json(websocket, payload)
    except Exception:
        await notify_command_queued(pending)
        return False
    finally:
        pending["sending"] = False

    if pending_commands.get(pending["command_id"]) is not pending:
        return True

    old_task = pending.get("timeout_task")
    if old_task is not None and old_task is not asyncio.current_task():
        old_task.cancel()
    pending["timeout_task"] = asyncio.create_task(
        expire_pending_command(pending["command_id"])
    )
    if pending.get("browser") is not None:
        try:
            await send_json(pending["browser"], {
                "type": "command_sent",
                "device": device_id,
                "command_id": pending["command_id"],
                "status": "sent",
                "attempts": pending["attempts"]
            })
        except Exception:
            pending["browser"] = None
    await send_command_queue_state()
    return True


async def deliver_queued_commands(device_id):
    for pending in list(pending_commands.values()):
        if pending["device"] == device_id and pending.get("status") == "queued":
            if (
                pending.get("attempts", 0) > 0
                and not device_supports_command_dedup(device_id)
            ):
                pending = remove_pending_command(pending["command_id"])
                if pending is not None:
                    await send_pending_command_result(
                        pending,
                        "unavailable",
                        pending["operation"].get("value"),
                        "Повтор отключён: прошивка не объявила защиту от повторного command_id."
                    )
                continue
            await deliver_pending_command(pending)


def schemas_match(left, right):
    return json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def schema_proposal_id(schema, schema_version):
    payload = json.dumps(
        {"schema": schema, "schema_version": schema_version},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def offer_device_schema(device_id, offered_metadata, is_legacy=False):
    deferred_device_schemas.pop(device_id, None)
    previous = load_device_metadata(device_id)
    if previous is None:
        save_device_metadata(device_id, offered_metadata)
        pending_device_schemas.pop(device_id, None)
        return "saved"

    # У старой прошивки нет объявленной Schema. Не позволяем ей затереть
    # уже принятую Schema современного устройства.
    if is_legacy and previous.get("schema_version") is not None:
        active_metadata = dict(previous)
        for key in (
            "name", "protocol_version", "firmware_version",
            "command_dedup", "trust_policy"
        ):
            active_metadata[key] = offered_metadata.get(key)
        save_device_metadata(device_id, active_metadata)
        return "preserved"

    has_previous_schema = previous.get("schema_version") is not None
    same_schema = schemas_match(previous["schema"], offered_metadata["schema"])

    if not has_previous_schema or same_schema:
        accepted_metadata = dict(offered_metadata)
        if same_schema:
            previous_version = previous.get("schema_version") or 0
            offered_version = offered_metadata.get("schema_version") or 0
            accepted_metadata["schema_version"] = max(
                previous_version, offered_version
            ) or None
        save_device_metadata(device_id, accepted_metadata)
        pending_device_schemas.pop(device_id, None)
        return "saved"

    proposal_id = schema_proposal_id(
        offered_metadata["schema"], offered_metadata.get("schema_version")
    )
    active_metadata = dict(previous)
    for key in (
        "name", "protocol_version", "firmware_version",
        "command_dedup", "trust_policy"
    ):
        active_metadata[key] = offered_metadata.get(key)
    save_device_metadata(device_id, active_metadata)

    pending_device_schemas[device_id] = {
        "proposal_id": proposal_id,
        "schema": offered_metadata["schema"],
        "schema_version": offered_metadata.get("schema_version"),
        "metadata": offered_metadata
    }
    return "pending"


def save_event_in_connection(
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


def save_device_event(device_id, event_type, event_value, timestamp=None):
    timestamp = timestamp or current_time()

    with database_connection() as conn:
        save_event_in_connection(
            conn,
            device_id,
            event_type,
            event_value,
            timestamp
        )

    return timestamp


def save_device_seen(device_id, connected):
    timestamp = current_time()

    with database_connection() as conn:
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
    device_id,
    parameters,
    record_changes=True,
    defer_parameters=None,
    record_telemetry=False
):
    timestamp = current_time()
    has_changes = False
    deferred_events = []
    defer_parameters = defer_parameters or set()

    with database_connection() as conn:
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
                or schema_values_match(saved_parameters[parameter_id], value)
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
                save_event_in_connection(
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


def get_recent_events(limit=30):
    """
    Возвращает последние значимые события.

    Старые повторяющиеся подряд значения LED скрываются
    в выдаче, даже если они уже сохранены в SQLite.

    Записи в самой базе при этом не удаляются.
    """

    with database_connection() as conn:
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


def sqlite_casefold(value):
    return value.casefold() if isinstance(value, str) else ""


def get_event_page(limit=EVENTS_PAGE_SIZE, device_id=None, before_id=None, search=""):
    conditions = []
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

    with database_connection() as conn:
        conn.create_function("casefold", 1, sqlite_casefold)
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


def get_all_devices():
    with database_connection() as conn:
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
                   status, last_error, updated_at, applied_at
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
                "error": configuration_row["last_error"]
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

        parameters.update(device_states.get(device_id, {}))

        metadata = device_metadata.get(device_id)
        if metadata is None:
            try:
                stored_schema = json.loads(row["schema_json"])
            except (TypeError, json.JSONDecodeError):
                stored_schema = default_legacy_schema()
            if not validate_schema(stored_schema):
                stored_schema = default_legacy_schema()

            # Rebuild inferred, read-only definitions for offline legacy devices
            # from their last saved values, without treating them as writable.
            if row["schema_version"] is None:
                stored_schema.setdefault("parameters", {})
                for definition in stored_schema["parameters"].values():
                    if isinstance(definition, dict):
                        definition["access"] = "read_only"
                for key, value in parameters.items():
                    if key not in stored_schema["parameters"]:
                        definition = inferred_definition(key, value)
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
            "connected": device_id in devices,
            "last_seen": row["last_seen"],
            "last_telemetry_at": row["last_telemetry_at"],
            "telemetry_stale_after_seconds": DEVICE_STALE_SECONDS,
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
            "pending_schema": public_schema_proposal(device_id),
            "device_profile": metadata.get("device_profile"),
            "board_profile": metadata.get("board_profile"),
            "driver_catalog": public_driver_catalog(metadata.get("driver_ids", [])),
            "hardware_configuration": configurations.get(device_id, {}).get("config"),
            "configuration_version": configurations.get(device_id, {}).get("version", 0),
            "configuration_status": configurations.get(device_id, {}).get(
                "status", "not_configured" if metadata.get("device_profile") == "universal" else None
            ),
            "configuration_error": configurations.get(device_id, {}).get("error")
        })

    known_ids = {item["device"] for item in result}

    for device_id in devices:
        if device_id not in known_ids:
            metadata = device_metadata.get(
                device_id, {"schema": default_legacy_schema()}
            )
            result.append({
                "device": device_id,
                "name": metadata.get("name"),
                "display_name": metadata.get("display_name"),
                "connected": True,
                "last_seen": None,
                "last_telemetry_at": None,
                "telemetry_stale_after_seconds": DEVICE_STALE_SECONDS,
                "parameters": device_states.get(device_id, {}),
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
                "pending_schema": public_schema_proposal(device_id),
                "device_profile": metadata.get("device_profile"),
                "board_profile": metadata.get("board_profile"),
                "driver_catalog": public_driver_catalog(metadata.get("driver_ids", [])),
                "hardware_configuration": configurations.get(device_id, {}).get("config"),
                "configuration_version": configurations.get(device_id, {}).get("version", 0),
                "configuration_status": configurations.get(device_id, {}).get(
                    "status", "not_configured" if metadata.get("device_profile") == "universal" else None
                ),
                "configuration_error": configurations.get(device_id, {}).get("error")
            })

    return result


def public_schema_proposal(device_id):
    proposal = pending_device_schemas.get(device_id)
    if proposal is None:
        return None
    return {
        "proposal_id": proposal["proposal_id"],
        "schema": proposal["schema"],
        "schema_version": proposal.get("schema_version")
    }


# =========================
# WEBSOCKET
# =========================

async def send_json(websocket, data):
    await websocket.send(
        json.dumps(data, ensure_ascii=False)
    )


async def broadcast(data):
    disconnected_clients = set()

    for websocket in list(browser_clients):
        try:
            await send_json(websocket, data)
        except Exception:
            disconnected_clients.add(websocket)

    browser_clients.difference_update(disconnected_clients)


async def send_device_list(websocket=None):
    message = {
        "type": "device_list",
        "devices": get_all_devices()
    }

    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_parameter_ui_preferences(websocket=None):
    message = {
        "type": "parameter_ui_preferences",
        "preferences": get_parameter_ui_preferences()
    }
    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_database_stats(websocket=None):
    message = {
        "type": "database_stats",
        **get_database_stats()
    }
    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_database_backups(websocket=None):
    message = {
        "type": "database_backups",
        "backups": list_database_backups(),
        "retention_limit": DATABASE_BACKUP_LIMIT
    }
    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_event_history(
    websocket=None,
    device_id=None,
    before_id=None,
    request_id=None,
    search=""
):
    events, has_more, next_before_id, matching_count = get_event_page(
        device_id=device_id,
        before_id=before_id,
        search=search
    )

    message = {
        "type": "event_history",
        "events": events,
        "has_more": has_more,
        "next_before_id": next_before_id,
        "device_filter": device_id or "all",
        "search": search,
        "total_count": matching_count
    }

    if request_id is not None:
        message["request_id"] = request_id

    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)

    await send_database_stats(websocket)


def remove_pending_command(command_id):
    pending = pending_commands.pop(command_id, None)

    if pending is not None:
        timeout_task = pending.get("timeout_task")

        if (
            timeout_task is not None
            and timeout_task is not asyncio.current_task()
        ):
            timeout_task.cancel()

    return pending


async def send_pending_command_result(pending, status, value, error):
    operation = pending["operation"]
    result_record = {"status": status, "value": value, "error": error}
    pending["status"] = status

    # Notify the browser as soon as the device result is known. History and
    # queue bookkeeping can require several SQLite operations and broadcasts.
    browser = pending.get("browser")
    if browser is not None:
        message = {
            "type": "command_result",
            "command_id": pending["command_id"],
            "device": pending["device"],
            "success": status == "succeeded",
            "status": status,
            **{operation["kind"]: operation["name"]},
            "value": value
        }
        if error:
            message["error"] = error
        try:
            await send_json(browser, message)
        except Exception:
            pending["browser"] = None

    update_command_queue_record(pending, status, result_record, error)
    result_future = pending.get("result_future")
    if result_future is not None and not result_future.done():
        result_future.set_result({"status": status, "value": value, "error": error})
    automation_state_change = pending.pop("automation_state_change", None)
    if (
        automation_state_change is None
        and pending.get("automation_id")
        and status == "succeeded"
        and operation["kind"] == "parameter"
        and value is not None
        and any(
            state_event["parameter"] == operation["name"]
            and schema_values_match(state_event["value"], value)
            for state_event in pending.get("deferred_state_events", [])
        )
    ):
        automation_state_change = {
            "parameter": operation["name"],
            "value": value
        }
    deferred_state_events = pending.pop("deferred_state_events", [])
    for state_event in deferred_state_events:
        is_confirmed_command_state = (
            status in ("succeeded", "failed")
            and value is not None
            and operation["kind"] == "parameter"
            and state_event["parameter"] == operation["name"]
            and schema_values_match(state_event["value"], value)
        )
        if is_confirmed_command_state:
            continue
        try:
            save_device_event(
                pending["device"],
                state_event["event_type"],
                state_event["event_value"],
                timestamp=state_event["timestamp"]
            )
        except Exception as log_error:
            print(f"[ОШИБКА] Не удалось сохранить изменение состояния: {log_error}")

    if status == "succeeded" and automation_state_change is not None:
        parameter = automation_state_change["parameter"]
        state_value = automation_state_change["value"]
        state_event_type = "parameter"
        state_event_value = json.dumps({
            "parameter": parameter,
            "value": state_value,
            "source": "automation",
            "command_id": pending["command_id"],
            "automation_id": pending.get("automation_id"),
            "run_id": pending.get("automation_run_id")
        }, ensure_ascii=False)
        try:
            save_device_event(
                pending["device"],
                state_event_type,
                state_event_value
            )
        except Exception as log_error:
            print(f"[ОШИБКА] Не удалось записать изменение состояния сценария: {log_error}")

    command_event = {
        "command_id": pending["command_id"],
        operation["kind"]: operation["name"],
        "requested_value": operation.get("value"),
        "arguments": operation.get("arguments", {}),
        "result_value": value if status in ("succeeded", "failed") else None,
        "status": status,
        "error": error
    }
    if pending.get("automation_id"):
        command_event["automation_id"] = pending["automation_id"]
        command_event["run_id"] = pending.get("automation_run_id")
    event_value = json.dumps(command_event, ensure_ascii=False)
    try:
        save_device_event(pending["device"], "command", event_value)
        await send_event_history()
    except Exception as log_error:
        print(f"[ОШИБКА] Не удалось сохранить результат команды: {log_error}")

    if pending["browser"] is None:
        await send_command_queue_state()
        print(
            f"[КОМАНДА] {pending['command_id']} ({pending['device']}): "
            f"{status}; {error or value}"
        )
        return

    await send_command_queue_state()


async def expire_pending_command(command_id):
    await asyncio.sleep(COMMAND_TIMEOUT_SECONDS)
    pending = pending_commands.get(command_id)
    if pending is None:
        return

    pending["timeout_task"] = None
    if not device_supports_command_dedup(pending["device"]):
        pending = remove_pending_command(command_id)
        if pending is not None:
            await send_pending_command_result(
                pending,
                "timeout",
                pending["operation"].get("value"),
                "Устройство не подтвердило команду. Повтор отключён, так как прошивка не объявила защиту от дублей."
            )
        return
    if pending.get("attempts", 0) < COMMAND_RETRY_LIMIT:
        if await deliver_pending_command(pending):
            return
    if pending.get("automation_id"):
        pending = remove_pending_command(command_id)
        if pending is not None:
            await send_pending_command_result(
                pending,
                "timeout",
                pending["operation"].get("value"),
                "Автоматизация остановлена: устройство не подтвердило действие после повторных попыток."
            )
        return
    await notify_command_queued(pending)


async def finish_pending_commands_for_device(
    device_id, status, error, retry=True
):
    command_ids = [
        command_id
        for command_id, pending in pending_commands.items()
        if pending["device"] == device_id
    ]

    for command_id in command_ids:
        pending = pending_commands.get(command_id)
        if pending is None:
            continue
        if pending.get("automation_id"):
            pending = remove_pending_command(command_id)
            if pending is not None:
                await send_pending_command_result(
                    pending,
                    status,
                    pending["operation"].get("value"),
                    error
                )
            continue
        if retry:
            if not device_supports_command_dedup(device_id):
                pending = remove_pending_command(command_id)
                if pending is not None:
                    await send_pending_command_result(
                        pending,
                        status,
                        pending["operation"].get("value"),
                        "Результат команды неизвестен после разрыва. Автоповтор отключён для прошивки без защиты от дублей."
                    )
                continue
            task = pending.get("timeout_task")
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            pending["timeout_task"] = None
            await notify_command_queued(pending)
            continue

        pending = remove_pending_command(command_id)
        if pending is not None:
            await send_pending_command_result(
                pending, status, pending["operation"].get("value"), error
            )


def detach_pending_commands_for_browser(websocket):
    # Keep tracking a command after its page closes. Its result or timeout is
    # still written to the event log, even though there is no page to notify.
    for pending in pending_commands.values():
        if pending["browser"] is websocket:
            pending["browser"] = None


# =========================
# КОМАНДЫ ИЗ БРАУЗЕРА
# =========================

async def dispatch_device_command(
    device_id,
    parameter,
    value,
    browser=None,
    requested_command_id=None,
    wait_for_result=False,
    automation_id=None,
    automation_run_id=None
):
    async def reject(message, command_id=requested_command_id):
        if browser is None:
            print(message)
        else:
            await send_json(browser, {
                "type": "error",
                "command_id": command_id,
                "message": message
            })

    if not isinstance(device_id, str) or device_id not in devices:
        await reject(f"Устройство {device_id} не подключено.")
        return False

    if device_id in pending_device_schemas:
        await reject("Сначала рассмотрите предложенное изменение Schema устройства.")
        return False

    if not isinstance(parameter, str):
        await reject("Некорректный идентификатор параметра.")
        return False

    schema = device_metadata.get(
        device_id, {"schema": default_legacy_schema()}
    )["schema"]
    definition = schema.get("parameters", {}).get(parameter)
    if (
        definition is None
        or definition.get("access") != "read_write"
        or not is_schema_value(value, definition)
        or not value_within_schema_limits(value, definition)
    ):
        await reject(
            "Параметр неизвестен, доступен только для чтения "
            "или содержит недопустимое значение."
        )
        return False

    if any(
        pending["device"] == device_id
        and pending["operation"]["kind"] == "parameter"
        and pending["operation"]["name"] == parameter
        for pending in pending_commands.values()
    ):
        await reject(
            f"Для параметра {parameter} уже ожидается результат команды."
        )
        return False

    if requested_command_id is None:
        prefix = "cmd" if browser is not None else "console"
        command_id = f"{prefix}-{uuid.uuid4().hex}"
    elif isinstance(requested_command_id, str) and 1 <= len(requested_command_id) <= 80:
        command_id = requested_command_id
    else:
        await reject("Некорректный command_id.")
        return False

    if command_id in pending_commands:
        await reject(
            "Команда с таким command_id уже ожидает результат.", command_id
        )
        return False

    payload = {
        "type": "command",
        "id": command_id,
        "device": device_id,
        "data": {
            "command": "set",
            "command_id": command_id,
            "parameter": parameter,
            "value": value
        }
    }
    pending = {
        "command_id": command_id,
        "device": device_id,
        "browser": browser,
        "operation": {
            "kind": "parameter",
            "name": parameter,
            "value": value,
            "automation_id": automation_id,
            "automation_run_id": automation_run_id
        },
        "payload": payload,
        "attempts": 0,
        "status": "queued",
        "timeout_task": None,
        "sending": False,
        "automation_id": automation_id,
        "automation_run_id": automation_run_id,
        "result_future": asyncio.get_running_loop().create_future() if wait_for_result else None
    }
    try:
        create_command_queue_record(pending)
    except sqlite3.IntegrityError:
        await reject("Команда с таким command_id уже использовалась.", command_id)
        return False
    pending_commands[command_id] = pending
    await deliver_pending_command(pending)

    print(f"[КОМАНДА] {device_id}: {parameter} = {value} ({command_id})")
    return pending["result_future"] if wait_for_result else True


async def dispatch_device_action(
    device_id,
    action,
    arguments=None,
    browser=None,
    requested_command_id=None,
    wait_for_result=False,
    automation_id=None,
    automation_run_id=None
):
    async def reject(message, command_id=requested_command_id):
        if browser is None:
            print(message)
        else:
            await send_json(browser, {
                "type": "error",
                "command_id": command_id,
                "message": message
            })

    if not isinstance(device_id, str) or device_id not in devices:
        await reject(f"Устройство {device_id} не подключено.")
        return False

    if device_id in pending_device_schemas:
        await reject("Сначала рассмотрите предложенное изменение Schema устройства.")
        return False
    if not isinstance(action, str):
        await reject("Некорректный идентификатор действия.")
        return False
    metadata = device_metadata.get(device_id, {})
    definition = metadata.get("schema", {}).get("actions", {}).get(action)
    if definition is None:
        await reject(f"Действие {action!r} не объявлено в Schema устройства.")
        return False
    if arguments is None:
        arguments = {}
    if not validate_action_arguments(definition, arguments):
        await reject("Аргументы действия не соответствуют его Schema.")
        return False

    if any(
        pending["device"] == device_id
        and pending["operation"]["kind"] == "action"
        and pending["operation"]["name"] == action
        for pending in pending_commands.values()
    ):
        await reject(f"Действие {action!r} уже выполняется на устройстве.")
        return False

    if requested_command_id is None:
        prefix = "cmd" if browser is not None else "console"
        command_id = f"{prefix}-{uuid.uuid4().hex}"
    elif isinstance(requested_command_id, str) and 1 <= len(requested_command_id) <= 80:
        command_id = requested_command_id
    else:
        await reject("Некорректный command_id.")
        return False
    if command_id in pending_commands:
        await reject("Команда с таким command_id уже ожидает результат.", command_id)
        return False

    payload = {
        "type": "command",
        "id": command_id,
        "device": device_id,
        "data": {
            "command": "invoke",
            "command_id": command_id,
            "action": action,
            "arguments": arguments
        }
    }
    pending = {
        "command_id": command_id,
        "device": device_id,
        "browser": browser,
        "operation": {
            "kind": "action",
            "name": action,
            "arguments": arguments,
            "automation_id": automation_id,
            "automation_run_id": automation_run_id
        },
        "payload": payload,
        "attempts": 0,
        "status": "queued",
        "timeout_task": None,
        "sending": False,
        "automation_id": automation_id,
        "automation_run_id": automation_run_id,
        "result_future": asyncio.get_running_loop().create_future() if wait_for_result else None
    }
    try:
        create_command_queue_record(pending)
    except sqlite3.IntegrityError:
        await reject("Команда с таким command_id уже использовалась.", command_id)
        return False
    pending_commands[command_id] = pending
    await deliver_pending_command(pending)
    if browser is None:
        print(f"Действие отправлено: {device_id} — {definition.get('label', action)}")
    return pending["result_future"] if wait_for_result else True


def get_automations():
    with database_connection() as conn:
        rows = conn.execute("""
            SELECT automation_id, config_json, enabled, created_at,
                   updated_at, last_triggered_at, run_status,
                   last_run_status, last_error
            FROM automations ORDER BY created_at, automation_id
        """).fetchall()
    result = []
    for automation_id, config_json, enabled, created, updated, triggered, run_status, last_run_status, last_error in rows:
        try:
            config = json.loads(config_json)
        except (TypeError, json.JSONDecodeError):
            continue
        result.append({
            "automation_id": automation_id,
            "config": config,
            "enabled": bool(enabled),
            "created_at": created,
            "updated_at": updated,
            "last_triggered_at": triggered,
            "run_status": run_status,
            "last_run_status": last_run_status,
            "last_error": last_error
        })
    return result


AUTOMATION_RUN_HISTORY_LIMIT = 50


def get_automation_run_count(automation_id, completed_only=False):
    where_clause = "automation_id = ?"
    if completed_only:
        where_clause += " AND status != 'running'"
    with database_connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM automation_runs WHERE {where_clause}",
            (automation_id,)
        ).fetchone()
    return row[0] if row else 0


def get_automation_run_history(automation_id):
    with database_connection() as conn:
        conn.row_factory = sqlite3.Row
        runs = conn.execute("""
            SELECT run_id, automation_name, trigger_json, trigger_reason,
                   started_at, finished_at, status, error
            FROM automation_runs
            WHERE automation_id = ?
            ORDER BY started_at DESC, rowid DESC
            LIMIT ?
        """, (automation_id, AUTOMATION_RUN_HISTORY_LIMIT)).fetchall()

        result = []
        for run in runs:
            try:
                trigger_data = json.loads(run["trigger_json"])
                if not isinstance(trigger_data, dict):
                    trigger_data = {}
            except (TypeError, json.JSONDecodeError):
                trigger_data = {}

            steps = conn.execute("""
                SELECT step_type, status, started_at, finished_at,
                       details_json, error
                FROM automation_run_steps
                WHERE run_id = ?
                ORDER BY sequence
            """, (run["run_id"],)).fetchall()
            parsed_steps = []
            for step in steps:
                try:
                    details = json.loads(step["details_json"] or "{}")
                    if not isinstance(details, dict):
                        details = {}
                except (TypeError, json.JSONDecodeError):
                    details = {}
                parsed_steps.append({
                    "type": step["step_type"],
                    "status": step["status"],
                    "started_at": step["started_at"],
                    "finished_at": step["finished_at"],
                    "details": details,
                    "error": step["error"]
                })

            result.append({
                "run_id": run["run_id"],
                "name": run["automation_name"],
                "trigger": trigger_data.get("trigger", {}),
                "trigger_reason": run["trigger_reason"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "status": run["status"],
                "error": run["error"],
                "steps": parsed_steps
            })
        return result


def create_automation_run(automation_id, config, reason):
    run_id = uuid.uuid4().hex
    started_at = current_time()
    program = config.get("program")
    trigger = (
        program.get("trigger", {})
        if isinstance(program, dict)
        else config.get("trigger", {})
    )
    trigger_data = {
        "trigger": trigger,
        "reason": reason
    }
    with database_connection() as conn:
        cursor = conn.execute("""
            UPDATE automations SET run_status = 'running', last_error = NULL,
                   last_triggered_at = ? WHERE automation_id = ? AND enabled = 1
        """, (started_at, automation_id))
        if cursor.rowcount != 1:
            return None
        conn.execute("""
            INSERT INTO automation_runs (
                run_id, automation_id, automation_name, trigger_json,
                trigger_reason, started_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'running')
        """, (
            run_id, automation_id, config.get("name", automation_id),
            json.dumps(trigger_data, ensure_ascii=False, allow_nan=False),
            str(reason), started_at
        ))

        old_runs = conn.execute("""
            SELECT run_id FROM automation_runs
            WHERE automation_id = ?
            ORDER BY started_at DESC, rowid DESC
            LIMIT -1 OFFSET ?
        """, (automation_id, AUTOMATION_RUN_HISTORY_LIMIT)).fetchall()
        old_run_ids = [row[0] for row in old_runs]
        if old_run_ids:
            placeholders = ",".join("?" for _ in old_run_ids)
            conn.execute(
                f"DELETE FROM automation_run_steps WHERE run_id IN ({placeholders})",
                old_run_ids
            )
            conn.execute(
                f"DELETE FROM automation_runs WHERE run_id IN ({placeholders})",
                old_run_ids
            )
    return run_id


def start_automation_run_step(run_id, step_type, details=None):
    try:
        with database_connection() as conn:
            sequence = conn.execute("""
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM automation_run_steps WHERE run_id = ?
            """, (run_id,)).fetchone()[0]
            cursor = conn.execute("""
                INSERT INTO automation_run_steps (
                    run_id, sequence, step_type, status, started_at, details_json
                ) VALUES (?, ?, ?, 'running', ?, ?)
            """, (
                run_id, sequence, step_type, current_time(),
                json.dumps(details or {}, ensure_ascii=False, allow_nan=False)
            ))
            return cursor.lastrowid
    except (sqlite3.Error, TypeError, ValueError) as error:
        print(f"[АВТОМАТИЗАЦИЯ] Не удалось записать шаг запуска: {error}")
        return None


def finish_automation_run_step(step_id, status, details=None, error=None):
    if step_id is None:
        return
    try:
        with database_connection() as conn:
            row = conn.execute(
                "SELECT details_json FROM automation_run_steps WHERE step_id = ?",
                (step_id,)
            ).fetchone()
            if row is None:
                return
            try:
                saved_details = json.loads(row[0] or "{}")
            except (TypeError, json.JSONDecodeError):
                saved_details = {}
            if not isinstance(saved_details, dict):
                saved_details = {}
            if isinstance(details, dict):
                saved_details.update(details)
            conn.execute("""
                UPDATE automation_run_steps
                SET status = ?, finished_at = ?, details_json = ?, error = ?
                WHERE step_id = ?
            """, (
                status, current_time(),
                json.dumps(saved_details, ensure_ascii=False, allow_nan=False),
                error, step_id
            ))
    except (sqlite3.Error, TypeError, ValueError) as log_error:
        print(f"[АВТОМАТИЗАЦИЯ] Не удалось сохранить результат шага: {log_error}")


def finish_automation_run(run_id, status, error=None):
    finished_at = current_time()
    with database_connection() as conn:
        if status in {"failed", "stopped", "interrupted"}:
            conn.execute("""
                UPDATE automation_run_steps
                SET status = ?, finished_at = ?, error = COALESCE(error, ?)
                WHERE run_id = ? AND status = 'running'
            """, (status, finished_at, error, run_id))
        conn.execute("""
            UPDATE automation_runs
            SET status = ?, finished_at = ?, error = ?
            WHERE run_id = ?
        """, (status, finished_at, error, run_id))


def automation_device_metadata(device_id):
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
        return None
    return device_metadata.get(device_id) or load_device_metadata(device_id)


def validate_automation_program(program):
    if (
        not isinstance(program, dict)
        or set(program) != {"version", "trigger", "body"}
        or type(program.get("version")) is not int
        or program["version"] != 1
    ):
        return "Некорректный формат программы автоматизации."
    trigger = program.get("trigger")
    body = program.get("body")
    if not isinstance(trigger, dict) or not isinstance(body, list) or not body:
        return "В сценарии нужен блок запуска и хотя бы одно действие."
    trigger_type = trigger.get("type")
    if not isinstance(trigger_type, str):
        return "Неизвестный тип триггера сценария."
    if trigger_type == "parameter_changed":
        trigger_fields = set(trigger)
        required_fields = {"type", "device", "parameter"}
        optional_fields = {"value", "for_seconds", "cooldown_seconds"}
        if not required_fields.issubset(trigger_fields) or trigger_fields - required_fields - optional_fields:
            return "Некорректный триггер изменения параметра."
        for field, maximum in (("for_seconds", 3600), ("cooldown_seconds", 86400)):
            if field in trigger and (
                type(trigger[field]) is not int
                or not 0 <= trigger[field] <= maximum
            ):
                return "Время устойчивости и пауза запуска должны быть целыми секундами в допустимых пределах."
        device_id = trigger.get("device")
        parameter_id = trigger.get("parameter")
        if not isinstance(device_id, str) or not isinstance(parameter_id, str):
            return "Триггер ссылается на неизвестный параметр устройства."
        metadata = automation_device_metadata(device_id)
        parameter = metadata.get("schema", {}).get("parameters", {}).get(parameter_id) if metadata else None
        if parameter is None or parameter.get("access") not in {"read_only", "read_write"} or parameter.get("type") == "json":
            return "Триггер ссылается на неизвестный параметр устройства."
        if "value" in trigger and (
            not is_schema_value(trigger["value"], parameter)
            or not value_within_schema_limits(trigger["value"], parameter)
        ):
            return "Значение фильтра запуска не соответствует Schema параметра."
    elif trigger_type in {"device_connected", "device_disconnected"}:
        if set(trigger) != {"type", "device"} or automation_device_metadata(trigger.get("device")) is None:
            return "Триггер ссылается на неизвестное устройство."
    elif trigger_type == "gateway_started":
        if set(trigger) != {"type"}:
            return "Некорректный триггер запуска Gateway."
    else:
        return "Неизвестный тип триггера сценария."

    node_count = [0]

    def count_node(depth):
        node_count[0] += 1
        if depth > 20 or node_count[0] > 200:
            raise ValueError("Сценарий слишком большой или глубоко вложенный.")

    def expression_type(expression, depth=0):
        count_node(depth)
        if not isinstance(expression, dict):
            raise ValueError("Некорректное выражение условия.")
        kind = expression.get("type")
        if not isinstance(kind, str):
            raise ValueError("Неизвестный тип выражения.")
        if kind == "constant":
            if set(expression) != {"type", "value"}:
                raise ValueError("Некорректное значение в условии.")
            value = expression["value"]
            if value is None:
                return "null"
            if type(value) is bool:
                return "boolean"
            if type(value) is int:
                return "integer"
            if type(value) is float and math.isfinite(value):
                return "number"
            if isinstance(value, str) and len(value) <= 1024:
                return "string"
            raise ValueError("Значение в условии имеет неподдерживаемый тип.")
        if kind == "parameter":
            if set(expression) != {"type", "device", "parameter"}:
                raise ValueError("Некорректная ссылка на параметр.")
            device_id = expression.get("device")
            parameter_id = expression.get("parameter")
            if not isinstance(device_id, str) or not isinstance(parameter_id, str):
                raise ValueError("Выражение ссылается на недоступный параметр.")
            metadata = automation_device_metadata(device_id)
            definition = metadata.get("schema", {}).get("parameters", {}).get(parameter_id) if metadata else None
            if definition is None or definition.get("access") not in {"read_only", "read_write"} or definition.get("type") == "json":
                raise ValueError("Выражение ссылается на недоступный параметр.")
            return definition.get("type")
        if kind == "device_connected":
            if set(expression) != {"type", "device"} or automation_device_metadata(expression.get("device")) is None:
                raise ValueError("Проверка соединения ссылается на неизвестное устройство.")
            return "boolean"
        if kind == "arithmetic":
            operator = expression.get("operator")
            if set(expression) != {"type", "operator", "left", "right"} or not isinstance(operator, str) or operator not in {"add", "subtract", "multiply", "divide", "power"}:
                raise ValueError("Некорректный блок вычисления.")
            left_type = expression_type(expression.get("left"), depth + 1)
            right_type = expression_type(expression.get("right"), depth + 1)
            if left_type not in {"integer", "number"} or right_type not in {"integer", "number"}:
                raise ValueError("Вычисление принимает только числа.")
            return "number"
        if kind == "not":
            if set(expression) != {"type", "operand"} or expression_type(expression.get("operand"), depth + 1) != "boolean":
                raise ValueError("Блок НЕ должен получать логическое значение.")
            return "boolean"
        if kind in {"and", "or"}:
            operands = expression.get("operands")
            if set(expression) != {"type", "operands"} or not isinstance(operands, list) or not 2 <= len(operands) <= 16:
                raise ValueError("Блок И/ИЛИ должен содержать от 2 до 16 условий.")
            if any(expression_type(operand, depth + 1) != "boolean" for operand in operands):
                raise ValueError("Блок И/ИЛИ принимает только логические значения.")
            return "boolean"
        if kind == "compare":
            operator = expression.get("operator")
            if set(expression) != {"type", "operator", "left", "right"} or not isinstance(operator, str) or operator not in {"eq", "ne", "lt", "le", "gt", "ge"}:
                raise ValueError("Некорректный блок сравнения.")
            left_type = expression_type(expression.get("left"), depth + 1)
            right_type = expression_type(expression.get("right"), depth + 1)
            comparable = (
                left_type == right_type
                or {left_type, right_type} <= {"integer", "number"}
                or "enum" in {left_type, right_type} and (left_type in {"enum", "string", "integer", "number", "boolean"} and right_type in {"enum", "string", "integer", "number", "boolean"})
            )
            if not comparable:
                raise ValueError("Нельзя сравнить значения разных типов.")
            if (
                expression["operator"] not in {"eq", "ne"}
                and {left_type, right_type} & {"boolean", "enum", "null"}
            ):
                raise ValueError("Для этого типа допустима только проверка равенства.")
            return "boolean"
        raise ValueError("Неизвестный тип выражения.")

    def validate_statements(statements, depth=0):
        if not isinstance(statements, list) or len(statements) > 100:
            raise ValueError("Слишком много действий в одной ветке.")
        for statement in statements:
            count_node(depth)
            if not isinstance(statement, dict):
                raise ValueError("Некорректный блок действия.")
            kind = statement.get("type")
            if kind == "set_parameter":
                if set(statement) != {"type", "device", "parameter", "value"}:
                    raise ValueError("Некорректный блок установки параметра.")
                metadata = automation_device_metadata(statement.get("device"))
                definition = metadata.get("schema", {}).get("parameters", {}).get(statement.get("parameter")) if metadata else None
                if not definition or definition.get("access") != "read_write":
                    raise ValueError("Действие ссылается на неизвестный или доступный только для чтения параметр.")
                value = statement.get("value")
                value_type = expression_type(value, depth + 1)
                if value.get("type") == "constant":
                    raw_value = value.get("value")
                    if not is_schema_value(raw_value, definition) or not value_within_schema_limits(raw_value, definition):
                        raise ValueError("Значение команды не соответствует Schema параметра.")
                elif value_type not in {definition.get("type"), "integer" if definition.get("type") == "number" else ""}:
                    if definition.get("type") != "enum" or value_type not in {"enum", "string", "integer", "number", "boolean"}:
                        raise ValueError("Тип значения команды не совпадает со Schema параметра.")
            elif kind == "invoke_action":
                if set(statement) != {"type", "device", "action", "arguments"}:
                    raise ValueError("Некорректный блок вызова действия.")
                metadata = automation_device_metadata(statement.get("device"))
                definition = metadata.get("schema", {}).get("actions", {}).get(statement.get("action")) if metadata else None
                arguments = statement.get("arguments")
                if not definition or not isinstance(arguments, dict) or not validate_action_arguments(definition, arguments):
                    raise ValueError("Вызов действия или его аргументы не соответствуют Schema.")
            elif kind == "wait":
                seconds = statement.get("seconds")
                if (
                    set(statement) != {"type", "seconds"}
                    or type(seconds) not in (int, float)
                    or not 0 <= seconds <= 3600
                    or not math.isfinite(seconds)
                ):
                    raise ValueError("Пауза должна быть числом от 0 до 3600 секунд.")
            elif kind == "if":
                if set(statement) != {"type", "branches", "else"}:
                    raise ValueError("Некорректный блок ЕСЛИ.")
                branches = statement.get("branches")
                if not isinstance(branches, list) or not 1 <= len(branches) <= 6:
                    raise ValueError("Блок ЕСЛИ должен содержать от 1 до 6 ветвей.")
                for branch in branches:
                    count_node(depth + 1)
                    if not isinstance(branch, dict) or set(branch) != {"condition", "then"} or expression_type(branch.get("condition"), depth + 2) != "boolean":
                        raise ValueError("Условие ветки ЕСЛИ должно возвращать логическое значение.")
                    validate_statements(branch.get("then"), depth + 2)
                validate_statements(statement.get("else"), depth + 1)
            else:
                raise ValueError("Неизвестный тип действия в сценарии.")

    try:
        if not 1 <= len(body) <= 100:
            return "В программе должно быть от 1 до 100 блоков действий."
        validate_statements(body)
    except (TypeError, ValueError, KeyError, OverflowError) as error:
        return str(error)
    return None


def validate_automation_config(config):
    if isinstance(config, dict) and config.get("version") == 2:
        if (
            type(config["version"]) is not int
            or set(config) != {"version", "name", "workspace", "program"}
        ):
            return "Некорректная структура сценария."
        name = config.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            return "Укажите название сценария длиной от 1 до 80 символов."
        try:
            serialized = json.dumps(config, ensure_ascii=False, allow_nan=False)
            serialized_size = len(serialized.encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return "Сценарий содержит некорректные данные."
        if serialized_size > 48 * 1024:
            return "Сценарий слишком большой (лимит 48 КБ)."
        workspace_state = config.get("workspace")
        if not isinstance(workspace_state, dict) or not isinstance(workspace_state.get("blocks"), dict):
            return "Не удалось сохранить блоки редактора."
        return validate_automation_program(config.get("program"))
    if not isinstance(config, dict) or set(config) != {"name", "trigger", "target"}:
        return "Некорректная структура сценария."
    name = config.get("name")
    trigger = config.get("trigger")
    target = config.get("target")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        return "Укажите название сценария длиной от 1 до 80 символов."
    if (
        not isinstance(trigger, dict)
        or set(trigger) != {"device", "parameter", "operator", "threshold"}
        or not isinstance(target, dict)
    ):
        return "Добавьте блок условия и блок действия."

    source_id = trigger.get("device")
    parameter = trigger.get("parameter")
    operator = trigger.get("operator")
    threshold = trigger.get("threshold")
    source_metadata = (
        device_metadata.get(source_id) or load_device_metadata(source_id)
        if isinstance(source_id, str) else None
    )
    if (
        not isinstance(source_id, str) or source_metadata is None
        or not isinstance(parameter, str)
        or not isinstance(operator, str)
        or operator not in {"above", "at_least", "below", "at_most"}
        or type(threshold) not in (int, float)
        or (type(threshold) is float and not math.isfinite(threshold))
    ):
        return "Проверьте устройство, датчик и порог в блоке условия."
    source_schema = source_metadata.get("schema", {})
    sensor = source_schema.get("parameters", {}).get(parameter)
    if not sensor or sensor.get("access") != "read_only" or sensor.get("type") not in {"integer", "number"}:
        return "Условие можно строить только по числовому датчику, доступному для чтения."
    if (
        (sensor.get("type") == "integer" and type(threshold) is not int)
        or ("min" in sensor and threshold < sensor["min"])
        or ("max" in sensor and threshold > sensor["max"])
    ):
        return "Порог не соответствует типу и диапазону датчика."

    target_id = target.get("device")
    kind = target.get("kind")
    operation_name = target.get("name")
    if not isinstance(operation_name, str):
        return "Выберите параметр или режим в блоке действия."
    target_metadata = (
        device_metadata.get(target_id) or load_device_metadata(target_id)
        if isinstance(target_id, str) else None
    )
    if not isinstance(target_id, str) or target_metadata is None:
        return "Выберите известное Gateway целевое устройство."
    if target_id in pending_device_schemas:
        return "Сначала примите или отклоните ожидающее изменение Schema целевого устройства."
    schema = target_metadata.get("schema", {})
    if kind == "parameter":
        definition = schema.get("parameters", {}).get(operation_name)
        value = target.get("value")
        if (
            not definition or definition.get("access") != "read_write"
            or not is_schema_value(value, definition)
            or not value_within_schema_limits(value, definition)
            or set(target) != {"device", "kind", "name", "value"}
        ):
            return "Значение блока действия не соответствует доступным параметрам Schema."
    elif kind == "action":
        definition = schema.get("actions", {}).get(operation_name)
        arguments = target.get("arguments", {})
        if (
            not definition or not isinstance(arguments, dict)
            or not validate_action_arguments(definition, arguments)
            or set(target) != {"device", "kind", "name", "arguments"}
        ):
            return "Параметры режима не соответствуют его Schema."
    else:
        return "Неизвестный тип блока действия."
    return None


def save_automation(automation_id, config, enabled=True):
    error = validate_automation_config(config)
    if error:
        return False, error
    now = current_time()
    with database_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM automations WHERE automation_id = ?", (automation_id,)
        ).fetchone()
        if exists:
            conn.execute("""
                UPDATE automations SET config_json = ?, enabled = ?, updated_at = ?,
                       last_run_status = NULL, last_error = NULL
                WHERE automation_id = ?
            """, (json.dumps(config, ensure_ascii=False, allow_nan=False),
                  int(enabled), now, automation_id))
        else:
            conn.execute("""
                INSERT INTO automations
                    (automation_id, config_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (automation_id, json.dumps(config, ensure_ascii=False, allow_nan=False),
                  int(enabled), now, now))
    return True, "Сценарий сохранён."


class AutomationExecutionError(Exception):
    pass


class AutomationStoppedError(AutomationExecutionError):
    pass


def device_is_connected(device_id):
    heartbeat = last_device_heartbeat.get(device_id)
    return (
        device_id in devices
        and heartbeat is not None
        and asyncio.get_running_loop().time() - heartbeat <= DEVICE_STALE_SECONDS
    )


def evaluate_automation_expression(expression):
    kind = expression["type"]
    if kind == "constant":
        return expression["value"]
    if kind == "device_connected":
        return device_is_connected(expression["device"])
    if kind == "parameter":
        device_id = expression["device"]
        if not device_is_connected(device_id):
            raise AutomationExecutionError(f"Устройство {device_id} отключено.")
        parameter = expression["parameter"]
        if parameter not in fresh_device_parameters.get(device_id, set()):
            raise AutomationExecutionError(f"Значение {device_id}.{parameter} ещё не обновлялось в этой сессии Gateway.")
        if parameter not in device_states.get(device_id, {}):
            raise AutomationExecutionError(f"Нет актуального значения параметра {device_id}.{parameter}.")
        value = device_states[device_id][parameter]
        definition = (device_metadata.get(device_id) or load_device_metadata(device_id) or {}).get("schema", {}).get("parameters", {}).get(parameter)
        if not definition or not is_schema_value(value, definition) or not value_within_schema_limits(value, definition):
            raise AutomationExecutionError(f"Текущее значение {device_id}.{parameter} не соответствует Schema.")
        return value
    if kind == "not":
        return not evaluate_automation_expression(expression["operand"])
    if kind == "and":
        return all(evaluate_automation_expression(item) for item in expression["operands"])
    if kind == "or":
        return any(evaluate_automation_expression(item) for item in expression["operands"])
    if kind == "compare":
        left = evaluate_automation_expression(expression["left"])
        right = evaluate_automation_expression(expression["right"])
        operator = expression["operator"]
        if type(left) is not type(right) and not (
            type(left) in (int, float) and type(right) in (int, float)
        ):
            return operator == "ne"
        if operator == "eq": return left == right
        if operator == "ne": return left != right
        if operator == "lt": return left < right
        if operator == "le": return left <= right
        if operator == "gt": return left > right
        if operator == "ge": return left >= right
    if kind == "arithmetic":
        operator = expression["operator"]
        try:
            left = float(evaluate_automation_expression(expression["left"]))
            right = float(evaluate_automation_expression(expression["right"]))
            if operator == "add": result = left + right
            elif operator == "subtract": result = left - right
            elif operator == "multiply": result = left * right
            elif operator == "divide": result = left / right
            elif operator == "power": result = left ** right
            else: raise AutomationExecutionError("Неизвестная арифметическая операция.")
        except (OverflowError, ZeroDivisionError, ValueError, TypeError) as error:
            raise AutomationExecutionError("Не удалось вычислить числовое выражение.") from error
        if type(result) not in (int, float) or not math.isfinite(result) or abs(result) > 1e100:
            raise AutomationExecutionError("Результат вычисления выходит за допустимый диапазон.")
        return result
    raise AutomationExecutionError("В программе встретилось неподдерживаемое выражение.")


async def execute_automation_statements(automation_id, statements, run_id):
    for statement in statements:
        if automation_id in automation_stop_requests:
            raise AutomationStoppedError("Остановлено пользователем.")
        if statement["type"] == "if":
            branch_taken = False
            for index, branch in enumerate(statement["branches"], start=1):
                step_id = start_automation_run_step(
                    run_id, "condition", {"branch": index}
                )
                try:
                    condition_result = bool(
                        evaluate_automation_expression(branch["condition"])
                    )
                except Exception as error:
                    finish_automation_run_step(
                        step_id, "failed", error=str(error)
                    )
                    raise
                finish_automation_run_step(
                    step_id, "succeeded", {"result": condition_result}
                )
                if condition_result:
                    await execute_automation_statements(
                        automation_id, branch["then"], run_id
                    )
                    branch_taken = True
                    break
            if not branch_taken:
                otherwise = statement["else"]
                if otherwise:
                    step_id = start_automation_run_step(run_id, "otherwise")
                    try:
                        await execute_automation_statements(
                            automation_id, otherwise, run_id
                        )
                    except AutomationStoppedError as error:
                        finish_automation_run_step(
                            step_id, "stopped", error=str(error)
                        )
                        raise
                    except Exception as error:
                        finish_automation_run_step(
                            step_id, "failed", error=str(error)
                        )
                        raise
                    finish_automation_run_step(step_id, "succeeded")
            continue

        if statement["type"] == "wait":
            seconds = statement["seconds"]
            step_id = start_automation_run_step(
                run_id, "wait", {"seconds": seconds}
            )
            current_task = asyncio.current_task()
            if current_task is not None:
                automation_waiting_tasks.add(current_task)
            try:
                deadline = asyncio.get_running_loop().time() + seconds
                while True:
                    if automation_id in automation_stop_requests:
                        raise AutomationStoppedError(
                            "Остановлено пользователем во время паузы."
                        )
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(remaining, 0.25))
            except AutomationStoppedError as error:
                finish_automation_run_step(
                    step_id, "stopped", error=str(error)
                )
                raise
            except Exception as error:
                finish_automation_run_step(
                    step_id, "failed", error=str(error)
                )
                raise
            finally:
                if current_task is not None:
                    automation_waiting_tasks.discard(current_task)
            finish_automation_run_step(step_id, "succeeded")
            continue

        device_id = statement["device"]
        command_id = f"auto-{automation_id[:24]}-{uuid.uuid4().hex[:16]}"
        step_type = statement["type"]
        step_details = {"device": device_id, "command_id": command_id}
        if step_type == "set_parameter":
            step_details["parameter"] = statement["parameter"]
        elif step_type == "invoke_action":
            step_details["action"] = statement["action"]
            step_details["arguments"] = statement["arguments"]
        step_id = start_automation_run_step(run_id, step_type, step_details)
        try:
            if not device_is_connected(device_id):
                raise AutomationExecutionError(
                    f"Целевое устройство {device_id} недоступно."
                )
            if step_type == "set_parameter":
                value = evaluate_automation_expression(statement["value"])
                step_details["requested_value"] = value
                result_future = await dispatch_device_command(
                    device_id, statement["parameter"], value,
                    requested_command_id=command_id,
                    wait_for_result=True,
                    automation_id=automation_id,
                    automation_run_id=run_id
                )
            elif step_type == "invoke_action":
                result_future = await dispatch_device_action(
                    device_id, statement["action"], statement["arguments"],
                    requested_command_id=command_id,
                    wait_for_result=True,
                    automation_id=automation_id,
                    automation_run_id=run_id
                )
            else:
                raise AutomationExecutionError("Неизвестный блок действия.")
            if result_future is False:
                raise AutomationExecutionError(
                    f"Gateway не смог отправить команду устройству {device_id}."
                )
            result = await asyncio.wait_for(
                result_future,
                timeout=COMMAND_TIMEOUT_SECONDS * (COMMAND_RETRY_LIMIT + 2)
            )
            if isinstance(result, dict) and "value" in result:
                step_details["result_value"] = result["value"]
            if result.get("status") != "succeeded":
                raise AutomationExecutionError(
                    result.get("error")
                    or f"Устройство {device_id} не выполнило действие."
                )
        except asyncio.TimeoutError as error:
            message = f"Gateway не дождался результата от {device_id}."
            finish_automation_run_step(
                step_id, "failed", details=step_details, error=message
            )
            raise AutomationExecutionError(message) from error
        except AutomationStoppedError as error:
            finish_automation_run_step(
                step_id, "stopped", details=step_details, error=str(error)
            )
            raise
        except Exception as error:
            finish_automation_run_step(
                step_id, "failed", details=step_details, error=str(error)
            )
            raise
        finish_automation_run_step(
            step_id, "succeeded", details=step_details
        )
        if automation_id in automation_stop_requests:
            raise AutomationStoppedError("Остановлено пользователем после текущего действия.")


async def publish_automation_list():
    await broadcast({"type": "automation_list", "automations": get_automations()})
    await send_database_stats()


def start_automation_run(automation_id, config, reason):
    if database_restore_in_progress:
        return False
    existing_task = automation_tasks.get(automation_id)
    task_is_active = existing_task is not None and not existing_task.done()
    if task_is_active and existing_task not in automation_waiting_tasks:
        return False
    run_id = create_automation_run(automation_id, config, reason)
    if run_id is None:
        return False

    if task_is_active:
        # Повторный триггер во время паузы начинает её отсчёт заново.
        automation_restart_tasks.add(existing_task)
        automation_stop_requests.discard(automation_id)
        existing_task.cancel()

    new_task = asyncio.create_task(
        execute_automation_run(automation_id, config, reason, run_id)
    )
    automation_tasks[automation_id] = new_task
    asyncio.create_task(publish_automation_list())
    return True


async def execute_automation_run(automation_id, config, reason, run_id):
    error_text = None
    status = "succeeded"
    try:
        validation_error = validate_automation_config(config)
        if validation_error:
            raise AutomationExecutionError(f"Schema изменилась: {validation_error}")
        if config.get("version") == 2:
            statements = config["program"]["body"]
        else:
            target = config["target"]
            if target["kind"] == "parameter":
                statements = [{
                    "type": "set_parameter",
                    "device": target["device"],
                    "parameter": target["name"],
                    "value": {"type": "constant", "value": target["value"]}
                }]
            else:
                statements = [{
                    "type": "invoke_action",
                    "device": target["device"],
                    "action": target["name"],
                    "arguments": target.get("arguments", {})
                }]
        await execute_automation_statements(
            automation_id, statements, run_id
        )
        print(f"[АВТОМАТИЗАЦИЯ] {config['name']} выполнена ({reason})")
    except AutomationStoppedError as error:
        status = "stopped"
        error_text = str(error)
    except asyncio.CancelledError:
        status = "interrupted"
        current_task = asyncio.current_task()
        error_text = (
            "Отсчёт перезапущен новым срабатыванием сценария."
            if current_task in automation_restart_tasks
            else "Gateway остановил выполнение сценария."
        )
        raise
    except Exception as error:
        status = "failed"
        error_text = str(error)
        print(f"[АВТОМАТИЗАЦИЯ] Ошибка в {config.get('name', automation_id)}: {error_text}")
    finally:
        current_task = asyncio.current_task()
        finish_automation_run(run_id, status, error_text)
        automation_waiting_tasks.discard(current_task)
        automation_restart_tasks.discard(current_task)
        # Сценарий могли уже перезапустить. Завершение старого запуска не
        # должно сбрасывать состояние нового запуска или его запрос остановки.
        if automation_tasks.get(automation_id) is current_task:
            with database_connection() as conn:
                conn.execute("""
                    UPDATE automations SET run_status = 'idle', last_run_status = ?,
                           last_error = ? WHERE automation_id = ?
                """, (status, error_text, automation_id))
            automation_tasks.pop(automation_id, None)
            automation_stop_requests.discard(automation_id)
        try:
            await publish_automation_list()
        except asyncio.CancelledError:
            pass


def fire_automation_event(event_type, device_id=None, parameter=None, value=None):
    if database_restore_in_progress:
        return
    with database_connection() as conn:
        rows = conn.execute("""
            SELECT automation_id, config_json FROM automations WHERE enabled = 1
        """).fetchall()
    for automation_id, config_json in rows:
        try:
            config = json.loads(config_json)
            if config.get("version") != 2:
                continue
            trigger = config["program"]["trigger"]
            matches = trigger.get("type") == event_type
            if event_type == "parameter_changed":
                matches = (
                    matches and trigger.get("device") == device_id
                    and trigger.get("parameter") == parameter
                    and (
                        "value" not in trigger
                        or schema_values_match(value, trigger.get("value"))
                    )
                )
            elif event_type in {"device_connected", "device_disconnected"}:
                matches = matches and trigger.get("device") == device_id
            if matches:
                start_automation_run(automation_id, config, event_type)
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            print(f"[АВТОМАТИЗАЦИЯ] Не удалось запустить сценарий {automation_id}: {error}")


async def evaluate_automations(device_id, previous_parameters, current_parameters):
    if database_restore_in_progress:
        return
    changed_parameters = {
        key for key, value in current_parameters.items()
        if key not in previous_parameters or not schema_values_match(previous_parameters[key], value)
    }
    if not changed_parameters:
        return
    with database_connection() as conn:
        rows = conn.execute("""
            SELECT automation_id, config_json, last_triggered_at
            FROM automations WHERE enabled = 1
        """).fetchall()
    now = datetime.now(timezone.utc)
    for automation_id, config_json, last_triggered in rows:
        try:
            config = json.loads(config_json)
            if config.get("version") == 2:
                trigger = config["program"]["trigger"]
                parameter = trigger.get("parameter")
                if (
                    trigger.get("type") == "parameter_changed"
                    and trigger.get("device") == device_id
                    and parameter in changed_parameters
                ):
                    value = current_parameters.get(parameter)
                    if (
                        "value" not in trigger
                        or schema_values_match(value, trigger["value"])
                    ):
                        schedule_parameter_automation_trigger(
                            automation_id, config, device_id, parameter, value
                        )
                    else:
                        cancel_pending_automation_trigger(automation_id)
                continue
            invalid_reason = validate_automation_config(config)
            if invalid_reason:
                continue
            trigger = config["trigger"]
            before = previous_parameters.get(trigger["parameter"])
            after = current_parameters.get(trigger["parameter"])
            if trigger["device"] != device_id or trigger["parameter"] not in changed_parameters or type(before) not in (int, float) or type(after) not in (int, float):
                continue
            threshold = trigger["threshold"]
            crossed = {
                "above": before <= threshold and after > threshold,
                "at_least": before < threshold and after >= threshold,
                "below": before >= threshold and after < threshold,
                "at_most": before > threshold and after <= threshold
            }[trigger["operator"]]
            if not crossed:
                continue
            if last_triggered and (now - datetime.fromisoformat(last_triggered).astimezone(timezone.utc)).total_seconds() < 30:
                continue
            start_automation_run(automation_id, config, "threshold_crossed")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"[ОШИБКА] Пропуск сценария {automation_id}: {error}")


def cancel_pending_automation_trigger(automation_id):
    task = automation_trigger_tasks.pop(automation_id, None)
    automation_trigger_devices.pop(automation_id, None)
    if task is not None and not task.done():
        task.cancel()


def cancel_pending_automation_triggers_for_device(device_id):
    for automation_id, trigger_device in list(automation_trigger_devices.items()):
        if trigger_device == device_id:
            cancel_pending_automation_trigger(automation_id)


def automation_cooldown_remaining(automation_id, cooldown_seconds):
    if cooldown_seconds <= 0:
        return 0
    with database_connection() as conn:
        row = conn.execute(
            "SELECT enabled, last_triggered_at FROM automations WHERE automation_id = ?",
            (automation_id,)
        ).fetchone()
    if row is None or not row[0]:
        return None
    if not row[1]:
        return 0
    try:
        last_triggered = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
        if last_triggered.tzinfo is None:
            last_triggered = last_triggered.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_triggered.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return 0
    return max(0, cooldown_seconds - elapsed)


def schedule_parameter_automation_trigger(
    automation_id, config, device_id, parameter, value
):
    cancel_pending_automation_trigger(automation_id)
    task = asyncio.create_task(
        wait_for_stable_automation_trigger(
            automation_id, config, device_id, parameter, value
        )
    )
    automation_trigger_tasks[automation_id] = task
    automation_trigger_devices[automation_id] = device_id


async def wait_for_stable_automation_trigger(
    automation_id, config, device_id, parameter, value
):
    task = asyncio.current_task()
    trigger = config["program"]["trigger"]
    try:
        stable_seconds = trigger.get("for_seconds", 0)
        if stable_seconds:
            await asyncio.sleep(stable_seconds)

        while automation_trigger_tasks.get(automation_id) is task:
            with database_connection() as conn:
                row = conn.execute(
                    "SELECT enabled, config_json FROM automations WHERE automation_id = ?",
                    (automation_id,)
                ).fetchone()
            if row is None or not row[0]:
                return
            try:
                saved_config = json.loads(row[1])
                current_trigger = saved_config["program"]["trigger"]
            except (TypeError, KeyError, json.JSONDecodeError):
                return
            if current_trigger != trigger or not device_is_connected(device_id):
                return

            current_parameters = device_states.get(device_id, {})
            if parameter not in current_parameters:
                return
            current_value = current_parameters[parameter]
            if not schema_values_match(current_value, value):
                return
            if (
                "value" in trigger
                and not schema_values_match(current_value, trigger["value"])
            ):
                return

            remaining = automation_cooldown_remaining(
                automation_id, trigger.get("cooldown_seconds", 0)
            )
            if remaining is None:
                return
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue

            start_automation_run(automation_id, config, "parameter_changed")
            return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[АВТОМАТИЗАЦИЯ] Не удалось проверить триггер {automation_id}: {error}")
    finally:
        if automation_trigger_tasks.get(automation_id) is task:
            automation_trigger_tasks.pop(automation_id, None)
            automation_trigger_devices.pop(automation_id, None)


def finalize_restored_database():
    init_db()
    sync_device_configuration_exports()
    timestamp = current_time()
    with database_connection() as conn:
        conn.execute("UPDATE devices SET connected = 0")
        conn.execute("""
            UPDATE command_queue
            SET status = 'unavailable', updated_at = ?,
                error = 'Command queue cleared during database restore.'
            WHERE status IN ('queued', 'sent')
        """, (timestamp,))


async def restore_database_backup(backup_id):
    global database_restore_in_progress

    if database_restore_in_progress:
        return False, "A database restore is already in progress."
    if (
        not isinstance(backup_id, str)
        or not DATABASE_BACKUP_ID_PATTERN.fullmatch(backup_id)
    ):
        return False, "Invalid backup identifier."

    backup_path = DATABASE_BACKUP_DIR / backup_id
    try:
        validate_database_backup(backup_path)
        safety_backup = create_database_backup(preserve_ids={backup_id})
    except (OSError, sqlite3.Error, ValueError) as error:
        return False, f"Could not prepare the restore: {error}"

    safety_path = DATABASE_BACKUP_DIR / safety_backup["backup_id"]
    database_restore_in_progress = True
    restored = False
    restore_started = False
    try:
        trigger_tasks = list(automation_trigger_tasks.values())
        for automation_id in list(automation_trigger_tasks):
            cancel_pending_automation_trigger(automation_id)
        if trigger_tasks:
            await asyncio.gather(*trigger_tasks, return_exceptions=True)
        automation_trigger_tasks.clear()
        automation_trigger_devices.clear()

        running_automations = list(automation_tasks.values())
        automation_stop_requests.update(automation_tasks)
        for task in running_automations:
            task.cancel()
        if running_automations:
            await asyncio.gather(*running_automations, return_exceptions=True)
        automation_tasks.clear()
        automation_stop_requests.clear()
        automation_waiting_tasks.clear()
        automation_restart_tasks.clear()

        for request in list(pending_device_connections.values()):
            request["decision"] = "deny"
            request["remember"] = False
            decision_event = request.get("decision_event")
            if decision_event is not None:
                decision_event.set()

        for pending in list(pending_commands.values()):
            timeout_task = pending.get("timeout_task")
            if timeout_task is not None:
                timeout_task.cancel()
            result_future = pending.get("result_future")
            if result_future is not None and not result_future.done():
                result_future.set_result({
                    "status": "unavailable",
                    "error": "Gateway database was restored."
                })
        pending_commands.clear()

        device_sockets = list(devices.values())
        active_device_tasks = list(device_handler_tasks)
        devices.clear()
        device_wire_ids.clear()
        last_device_heartbeat.clear()
        fresh_device_parameters.clear()
        device_states.clear()
        device_metadata.clear()
        pending_device_schemas.clear()
        deferred_device_schemas.clear()
        deferred_connection_automations.clear()

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
        restore_database_file(backup_path)
        finalize_restored_database()
        restored = True
    except Exception as error:
        if restore_started:
            try:
                restore_database_file(safety_path)
                finalize_restored_database()
            except Exception as recovery_error:
                print(f"[BACKUP] Safety restore failed: {recovery_error}")
                return False, f"Restore failed and safety recovery also failed: {recovery_error}"
            return False, f"Restore failed; the previous database was recovered: {error}"
        return False, f"Restore could not start: {error}"
    finally:
        database_restore_in_progress = False

    if restored:
        await send_device_list()
        await send_parameter_ui_preferences()
        await send_device_access_state()
        await send_command_queue_state()
        await send_event_history()
        await publish_automation_list()
        await send_database_backups()
        return True, "Database restored. Devices have been disconnected and can reconnect now."
    return False, "Database restore failed."


async def handle_browser_message(websocket, message):

    action = message.get("action")
    if database_restore_in_progress:
        await send_json(websocket, {
            "type": "error",
            "message": "Gateway database is being restored. Try again shortly."
        })
        return

    if action == "get_automations":
        await send_json(websocket, {
            "type": "automation_list", "automations": get_automations()
        })
        return

    if action == "get_automation_run_history":
        automation_id = message.get("automation_id")
        valid_id = (
            isinstance(automation_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id)
        )
        exists = False
        if valid_id:
            with database_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM automations WHERE automation_id = ?",
                    (automation_id,)
                ).fetchone() is not None
        await send_json(websocket, {
            "type": "automation_run_history",
            "automation_id": automation_id,
            "success": exists,
            "runs": get_automation_run_history(automation_id) if exists else [],
            "run_count": get_automation_run_count(automation_id) if exists else 0,
            "completed_count": get_automation_run_count(automation_id, completed_only=True) if exists else 0,
            "message": None if exists else "Сценарий не найден."
        })
        return

    if action == "delete_automation_run":
        automation_id = message.get("automation_id")
        run_id = message.get("run_id")
        valid = (
            isinstance(automation_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id)
            and isinstance(run_id, str)
            and re.fullmatch(r"[A-Fa-f0-9-]{1,64}", run_id)
        )
        success = False
        result_message = "Запуск не найден."
        if valid:
            with database_connection() as conn:
                row = conn.execute(
                    "SELECT status FROM automation_runs WHERE run_id = ? AND automation_id = ?",
                    (run_id, automation_id)
                ).fetchone()
                if row is not None and row[0] == "running":
                    result_message = "Нельзя удалить выполняющийся запуск."
                elif row is not None:
                    conn.execute(
                        "DELETE FROM automation_run_steps WHERE run_id = ?",
                        (run_id,)
                    )
                    cursor = conn.execute(
                        "DELETE FROM automation_runs WHERE run_id = ? AND automation_id = ?",
                        (run_id, automation_id)
                    )
                    success = cursor.rowcount == 1
                    result_message = "Запуск удалён." if success else "Запуск не найден."
        await send_json(websocket, {
            "type": "automation_run_delete_result",
            "success": success,
            "automation_id": automation_id,
            "run_id": run_id,
            "message": result_message
        })
        if success:
            await broadcast({
                "type": "automation_run_history",
                "automation_id": automation_id,
                "success": True,
                "runs": get_automation_run_history(automation_id),
                "run_count": get_automation_run_count(automation_id),
                "completed_count": get_automation_run_count(automation_id, completed_only=True)
            })
            await send_database_stats()
        return

    if action == "clear_automation_run_history":
        automation_id = message.get("automation_id")
        valid = (
            isinstance(automation_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id)
        )
        success = False
        deleted_count = 0
        result_message = "Сценарий не найден."
        if valid:
            with database_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                exists = conn.execute(
                    "SELECT 1 FROM automations WHERE automation_id = ?",
                    (automation_id,)
                ).fetchone() is not None
                if exists:
                    conn.execute(
                        "DELETE FROM automation_run_steps WHERE run_id IN ("
                        "SELECT run_id FROM automation_runs "
                        "WHERE automation_id = ? AND status != 'running')",
                        (automation_id,)
                    )
                    cursor = conn.execute(
                        "DELETE FROM automation_runs "
                        "WHERE automation_id = ? AND status != 'running'",
                        (automation_id,)
                    )
                    deleted_count = cursor.rowcount
                    success = True
                    result_message = (
                        f"Удалено записей запусков: {deleted_count}."
                        if deleted_count
                        else "Завершённых записей для удаления нет."
                    )
        await send_json(websocket, {
            "type": "automation_run_clear_result",
            "success": success,
            "automation_id": automation_id,
            "deleted_count": deleted_count,
            "message": result_message
        })
        if success:
            await broadcast({
                "type": "automation_run_history",
                "automation_id": automation_id,
                "success": True,
                "runs": get_automation_run_history(automation_id),
                "run_count": get_automation_run_count(automation_id),
                "completed_count": get_automation_run_count(automation_id, completed_only=True)
            })
            await send_database_stats()
        return

    if action == "save_automation":
        automation_id = message.get("automation_id")
        config = message.get("config")
        is_new = automation_id in (None, "")
        if is_new:
            automation_id = uuid.uuid4().hex
        if not isinstance(automation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id):
            success, message_text = False, "Некорректный ID сценария."
        else:
            success, message_text = save_automation(
                automation_id, config, message.get("enabled", True) is True
            )
        await send_json(websocket, {
            "type": "automation_save_result", "success": success,
            "automation_id": automation_id, "message": message_text
        })
        if success:
            cancel_pending_automation_trigger(automation_id)
            await publish_automation_list()
        return

    if action == "set_automation_enabled":
        automation_id = message.get("automation_id")
        enabled = message.get("enabled")
        valid = (
            isinstance(automation_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id)
            and type(enabled) is bool
        )
        success = False
        if valid:
            with database_connection() as conn:
                cursor = conn.execute(
                    "UPDATE automations SET enabled = ?, updated_at = ? WHERE automation_id = ?",
                    (int(enabled), current_time(), automation_id)
                )
                success = cursor.rowcount == 1
        await send_json(websocket, {
            "type": "automation_update_result", "success": success,
            "automation_id": automation_id,
            "message": "Сценарий обновлён." if success else "Сценарий не найден."
        })
        if success:
            if not enabled:
                cancel_pending_automation_trigger(automation_id)
            await publish_automation_list()
        return

    if action == "stop_automation":
        automation_id = message.get("automation_id")
        task = automation_tasks.get(automation_id) if isinstance(automation_id, str) else None
        success = task is not None and not task.done()
        if success:
            automation_stop_requests.add(automation_id)
        await send_json(websocket, {
            "type": "automation_stop_result", "success": success,
            "automation_id": automation_id,
            "message": (
                "Остановка запрошена; текущее действие завершится первым."
                if success else "Сценарий сейчас не выполняется."
            )
        })
        if success:
            await publish_automation_list()
        return

    if action == "delete_automation":
        automation_id = message.get("automation_id")
        success = False
        active_task = automation_tasks.get(automation_id) if isinstance(automation_id, str) else None
        if active_task is not None and not active_task.done():
            success = False
        elif isinstance(automation_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id):
            with database_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM automations WHERE automation_id = ?", (automation_id,)
                )
                success = cursor.rowcount == 1
                if success:
                    conn.execute("""
                        DELETE FROM automation_run_steps
                        WHERE run_id IN (
                            SELECT run_id FROM automation_runs WHERE automation_id = ?
                        )
                    """, (automation_id,))
                    conn.execute(
                        "DELETE FROM automation_runs WHERE automation_id = ?",
                        (automation_id,)
                    )
        await send_json(websocket, {
            "type": "automation_delete_result", "success": success,
            "automation_id": automation_id,
            "message": (
                "Сценарий удалён." if success else
                "Сначала остановите выполняющийся сценарий." if active_task is not None and not active_task.done() else
                "Сценарий не найден."
            )
        })
        if success:
            cancel_pending_automation_trigger(automation_id)
            await publish_automation_list()
        return

    if action == "device_access_decision":
        device_id = message.get("device")
        request = pending_device_connections.get(device_id)
        decision = message.get("decision")
        remember = message.get("remember")
        request_id = message.get("request_id")
        valid = (
            isinstance(device_id, str)
            and request is not None
            and request["request_id"] == request_id
            and isinstance(decision, str)
            and decision in {"allow", "deny"}
            and type(remember) is bool
            and not request["decision_event"].is_set()
        )
        if not valid:
            await send_json(websocket, {
                "type": "device_access_decision_result",
                "success": False,
                "device": device_id,
                "request_id": request_id,
                "message": "Запрос подключения уже обработан или истёк."
            })
            return

        request["decision"] = decision
        request["remember"] = remember
        request["decision_event"].set()
        if decision == "deny" and remember:
            set_device_trust_policy(
                device_id, "deny", request["metadata"].get("name")
            )
            cancel_pending_automation_triggers_for_device(device_id)
            active_socket = devices.pop(device_id, None)
            device_wire_ids.pop(device_id, None)
            last_device_heartbeat.pop(device_id, None)
            pending_device_schemas.pop(device_id, None)
            deferred_device_schemas.pop(device_id, None)
            await finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Подключение устройства запрещено.",
                retry=False
            )
            with database_connection() as conn:
                conn.execute(
                    "UPDATE devices SET connected = 0 WHERE device_id = ?",
                    (device_id,)
                )
            save_device_event(
                device_id,
                "connection",
                "Для устройства сохранён постоянный запрет подключения"
            )
            if active_socket is not None:
                try:
                    await active_socket.close(
                        code=4003, reason="Device access denied"
                    )
                except Exception:
                    pass
        await send_json(websocket, {
            "type": "device_access_decision_result",
            "success": True,
            "device": device_id,
            "request_id": request_id,
            "message": (
                "Подключение устройства разрешено."
                if decision == "allow"
                else "Подключение устройства отклонено."
            )
        })
        await send_device_access_state()
        if decision == "deny" and remember:
            await send_device_list()
            await send_event_history()
        if decision == "deny":
            try:
                await request["websocket"].close(
                    code=4003, reason="Device access denied"
                )
            except Exception:
                pass
        return

    if action == "forget_device":
        device_id = message.get("device")
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
            await send_json(websocket, {
                "type": "device_forget_result",
                "success": False,
                "device": device_id,
                "message": "Некорректный идентификатор устройства."
            })
            return

        with database_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM devices WHERE device_id = ?",
                (device_id,)
            ).fetchone()
        if exists is None:
            await send_json(websocket, {
                "type": "device_forget_result",
                "success": False,
                "device": device_id,
                "message": "Устройство не найдено."
            })
            return

        set_device_trust_policy(device_id, "forgotten")
        cancel_pending_automation_triggers_for_device(device_id)
        active_socket = devices.pop(device_id, None)
        device_wire_ids.pop(device_id, None)
        last_device_heartbeat.pop(device_id, None)
        pending_device_schemas.pop(device_id, None)
        deferred_device_schemas.pop(device_id, None)
        await finish_pending_commands_for_device(
            device_id,
            "unavailable",
            "Доступ к устройству отозван.",
            retry=False
        )
        with database_connection() as conn:
            conn.execute(
                "UPDATE devices SET connected = 0 WHERE device_id = ?",
                (device_id,)
            )
        save_device_event(
            device_id,
            "connection",
            "Устройство забыто; при следующем подключении потребуется подтверждение"
        )
        if active_socket is not None:
            try:
                await active_socket.close(code=4002, reason="Device forgotten")
            except Exception:
                pass
        await send_json(websocket, {
            "type": "device_forget_result",
            "success": True,
            "device": device_id,
            "message": "Устройство отключено. При следующем подключении Gateway снова запросит разрешение."
        })
        await send_device_list()
        await send_event_history()
        await send_device_access_state()
        return

    if action == "update_device_identity":
        device_id = message.get("device")
        new_device_id = message.get("new_device_id")
        display_name = message.get("display_name")
        success, message_text = update_device_identity(
            device_id, new_device_id, display_name
        )
        await send_json(websocket, {
            "type": "device_identity_result",
            "success": success,
            "device": device_id,
            "new_device_id": new_device_id if success else device_id,
            "message": message_text
        })
        if success:
            await send_device_list()
            await send_event_history()
            await send_command_queue_state()
            await send_parameter_ui_preferences()
            await send_database_stats()
            await send_json(websocket, {
                "type": "automation_list",
                "automations": get_automations()
            })
        return

    if action == "clear_device_block":
        device_id = message.get("device")
        if not isinstance(device_id, str) or get_device_trust_policy(device_id) != "deny":
            await send_json(websocket, {
                "type": "blocked_device_result",
                "success": False,
                "device": device_id,
                "message": "Для устройства не найдена постоянная блокировка."
            })
            return
        set_device_trust_policy(device_id, "forgotten")
        save_device_event(
            device_id, "connection", "Постоянная блокировка подключения снята"
        )
        await send_json(websocket, {
            "type": "blocked_device_result",
            "success": True,
            "device": device_id,
            "message": "Блокировка снята. При следующем подключении появится запрос разрешения."
        })
        await send_device_list()
        await send_device_access_state()
        await send_event_history()
        return

    if action == "get_schema_history":
        device_id = message.get("device")
        request_id = message.get("request_id")
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
            await send_json(websocket, {
                "type": "schema_history",
                "device": device_id,
                "request_id": request_id,
                "history": []
            })
            return
        await send_json(websocket, {
            "type": "schema_history",
            "device": device_id,
            "request_id": request_id,
            "history": get_schema_history(device_id)
        })
        return

    if action == "restore_schema":
        device_id = message.get("device")
        history_id = message.get("history_id")
        if (
            not isinstance(device_id, str)
            or type(history_id) is not int
            or history_id < 1
        ):
            success, message_text = False, "Некорректный снимок Schema."
        else:
            success, message_text = restore_device_schema(device_id, history_id)
        await send_json(websocket, {
            "type": "schema_restore_result",
            "success": success,
            "device": device_id,
            "history_id": history_id,
            "message": message_text
        })
        if success:
            await send_device_list()
            await send_event_history()
            await send_parameter_ui_preferences()
            await send_database_stats()
        return

    if action == "delete_schema_history":
        device_id = message.get("device")
        history_id = message.get("history_id")
        if (
            not isinstance(device_id, str)
            or not 1 <= len(device_id) <= 64
            or type(history_id) is not int
            or history_id < 1
        ):
            success = False
            message_text = "Некорректный снимок Schema."
        else:
            success = delete_schema_history_item(device_id, history_id)
            message_text = (
                "Снимок Schema удалён из истории. Текущая Schema устройства не изменена."
                if success
                else "Снимок Schema не найден; история не изменена."
            )
        if success:
            save_device_event(
                device_id,
                "schema",
                f"Удалён снимок Schema из истории (ID {history_id})"
            )
        await send_json(websocket, {
            "type": "schema_history_delete_result",
            "success": success,
            "device": device_id,
            "history_id": history_id,
            "message": message_text
        })
        if success:
            await send_event_history()
        return

    if action == "set_parameter_ui_preference":
        device_id = message.get("device")
        parameter_id = message.get("parameter")
        control = message.get("control")
        success, message_text = save_parameter_ui_preference(
            device_id, parameter_id, control
        )
        await send_json(websocket, {
            "type": "parameter_ui_preference_result",
            "success": success,
            "device": device_id,
            "parameter": parameter_id,
            "control": control,
            "message": message_text
        })
        if success:
            await send_parameter_ui_preferences()
            await send_database_stats()
        return

    if action == "get_parameter_ui_preferences":
        await send_parameter_ui_preferences(websocket)
        return

    if action == "get_devices":
        await send_device_list(websocket)
        return

    if action == "save_device_configuration":
        device_id = message.get("device")
        configuration = message.get("configuration")
        requested_ui_preferences = message.get("ui_preferences")
        metadata = (
            device_metadata.get(device_id) or load_device_metadata(device_id)
            if isinstance(device_id, str)
            else None
        )
        success = False
        version = None
        status = None
        result_message = "Устройство не поддерживает универсальную конфигурацию."
        if (
            metadata is not None
            and metadata.get("device_profile") == "universal"
            and metadata.get("board_profile") == "esp32-s3-devkitc-1"
        ):
            normalized, generated_schema, validation_error = validate_device_configuration(
                configuration, metadata.get("driver_ids", [])
            )
            normalized_ui_preferences = {}
            if validation_error is None:
                normalized_ui_preferences, validation_error = validate_parameter_ui_preferences(
                    requested_ui_preferences, generated_schema
                )
            if validation_error:
                result_message = validation_error
            else:
                try:
                    saved = save_device_configuration(
                        device_id, normalized, normalized_ui_preferences
                    )
                    version = saved["config_version"]
                    status = "pending"
                    if saved["unchanged"]:
                        update_device_configuration_status(device_id, status)
                    if device_id in devices:
                        status = "applying" if await send_device_configuration(device_id) else "pending"
                    export_written = write_device_configuration_export(device_id)
                    success = True
                    result_message = (
                        "Конфигурация уже была сохранена; Gateway повторно отправляет её устройству."
                        if saved["unchanged"] and status == "applying"
                        else "Конфигурация сохранена и отправлена устройству."
                        if status == "applying"
                        else "Конфигурация сохранена; она будет отправлена при подключении устройства."
                    )
                    if not export_written:
                        result_message += (
                            " SQLite обновлена, но JSON-файл не удалось обновить; "
                            "Gateway повторит экспорт при следующем запуске."
                        )
                    if not saved["unchanged"]:
                        save_device_event(
                            device_id,
                            "configuration",
                            f"Сохранена аппаратная конфигурация версии {version}; статус: {status}."
                        )
                except (OSError, sqlite3.Error, ValueError) as error:
                    result_message = f"Не удалось сохранить конфигурацию: {error}"
        elif metadata is not None and metadata.get("device_profile") == "universal":
            result_message = "Для этой версии конструктора поддерживается профиль ESP32-S3 DevKitC-1."

        await send_json(websocket, {
            "type": "device_configuration_save_result",
            "success": success,
            "device": device_id,
            "config_version": version,
            "status": status,
            "message": result_message
        })
        if success:
            await send_device_list()
            await send_parameter_ui_preferences()
            await send_event_history()
            await send_database_stats()
        return

    if action == "get_database_stats":
        await send_database_stats(websocket)
        return

    if action == "get_database_backups":
        await send_database_backups(websocket)
        return

    if action == "create_database_backup":
        try:
            backup = create_database_backup()
            await send_json(websocket, {
                "type": "database_backup_result",
                "success": True,
                "backup": backup,
                "message": "Backup created."
            })
            await send_database_backups()
        except Exception as error:
            print(f"[BACKUP] Could not create a database backup: {error}")
            await send_json(websocket, {
                "type": "database_backup_result",
                "success": False,
                "message": f"Could not create a backup: {error}"
            })
        return

    if action == "restore_database_backup":
        success, message_text = await restore_database_backup(
            message.get("backup_id")
        )
        await send_json(websocket, {
            "type": "database_backup_restore_result",
            "success": success,
            "backup_id": message.get("backup_id"),
            "message": message_text
        })
        return

    if action == "get_events":
        device_id = message.get("device", "all")
        before_id = message.get("before_id")
        request_id = message.get("request_id")
        search = message.get("search", "")

        if not isinstance(search, str) or len(search) > 120:
            await send_json(websocket, {
                "type": "error",
                "request_id": request_id,
                "message": "Поисковый запрос должен содержать не более 120 символов."
            })
            return
        search = search.strip()

        if device_id == "all":
            device_id = None
        elif not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
            await send_json(websocket, {
                "type": "error",
                "request_id": request_id,
                "message": "Некорректный фильтр устройства истории."
            })
            return

        if before_id is not None and (type(before_id) is not int or before_id < 1):
            await send_json(websocket, {
                "type": "error",
                "request_id": request_id,
                "message": "Некорректный курсор истории."
            })
            return

        if request_id is not None and (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 80
        ):
            await send_json(websocket, {
                "type": "error",
                "message": "Некорректный ID запроса истории."
            })
            return

        await send_event_history(
            websocket,
            device_id=device_id,
            before_id=before_id,
            request_id=request_id,
            search=search
        )
        return

    if action == "decide_schema":
        device_id = message.get("device")
        proposal_id = message.get("proposal_id")
        decision = message.get("decision")
        proposal = (
            pending_device_schemas.get(device_id)
            if isinstance(device_id, str)
            else None
        )

        if (
            not isinstance(device_id, str)
            or not 1 <= len(device_id) <= 64
            or not isinstance(proposal_id, str)
            or not isinstance(decision, str)
            or decision not in {"accept", "reject"}
            or proposal is None
            or proposal["proposal_id"] != proposal_id
        ):
            await send_json(websocket, {
                "type": "schema_decision_result",
                "success": False,
                "device": device_id,
                "proposal_id": proposal_id,
                "message": "Предложение Schema уже изменилось или недоступно."
            })
            await send_device_list(websocket)
            return

        version = proposal.get("schema_version")
        if decision == "accept":
            accepted_metadata = dict(proposal["metadata"])
            save_device_metadata(device_id, accepted_metadata)
            pending_device_schemas.pop(device_id, None)
            deferred_device_schemas.pop(device_id, None)
            save_device_event(
                device_id,
                "schema",
                f"Принята версия Schema {version or 'без номера'}"
            )
            await deliver_queued_commands(device_id)
            message_text = f"Новая Schema устройства {device_id} принята."
        else:
            pending_device_schemas.pop(device_id, None)
            deferred_device_schemas[device_id] = proposal["schema"]
            save_device_event(
                device_id,
                "schema",
                f"Оставлена текущая Schema; предложение версии {version or 'без номера'} отклонено"
            )
            await deliver_queued_commands(device_id)
            message_text = f"Текущая Schema устройства {device_id} оставлена. При следующем подключении Gateway снова предложит это изменение, если Schema всё ещё отличается."

        await send_json(websocket, {
            "type": "schema_decision_result",
            "success": True,
            "device": device_id,
            "proposal_id": proposal_id,
            "message": message_text
        })
        await send_device_list()
        await send_parameter_ui_preferences()
        await send_database_stats()
        await send_event_history()
        await send_command_queue_state()
        should_fire_connected = device_id in deferred_connection_automations
        deferred_connection_automations.discard(device_id)
        if should_fire_connected and device_is_connected(device_id):
            fire_automation_event("device_connected", device_id)
        return

    if action == "command":
        await dispatch_device_command(
            message.get("device"),
            message.get("parameter"),
            message.get("value"),
            browser=websocket,
            requested_command_id=message.get("command_id")
        )
        return

    if action == "invoke_action":
        await dispatch_device_action(
            message.get("device"),
            message.get("action_id"),
            arguments=message.get("arguments", {}),
            browser=websocket,
            requested_command_id=message.get("command_id")
        )
        return

    await send_json(websocket, {
        "type": "error",
        "message": f"Неизвестное действие: {action}"
    })


# =========================
# ОБРАБОТКА ПОДКЛЮЧЕНИЙ
# =========================

async def handle_client(websocket):
    client_type = None
    device_id = None
    wire_device_id = None

    remote = getattr(websocket, "remote_address", None)
    print(f"[ПОДКЛЮЧЕНИЕ] Новое соединение: {remote}")

    try:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await send_json(websocket, {
                    "type": "error",
                    "message": "Получен некорректный JSON."
                })
                continue

            if not isinstance(message, dict):
                await send_json(websocket, {
                    "type": "error",
                    "message": "Корневое значение JSON должно быть объектом."
                })
                continue

            # A second connection with the same device ID replaces the first.
            # Do not accept queued messages from the superseded socket.
            if client_type == "device" and devices.get(device_id) is not websocket:
                await websocket.close(
                    code=4001,
                    reason="Replaced by a newer connection"
                )
                break

            message_type = message.get("type")

            if database_restore_in_progress and client_type != "browser":
                await websocket.close(
                    code=1012,
                    reason="Gateway database is being restored"
                )
                break

            # -------------------------
            # Регистрация браузера
            # -------------------------

            if message_type == "browser_hello" and client_type == "device":
                await send_json(websocket, {
                    "type": "error",
                    "message": "Тип подключения устройства нельзя изменить."
                })
                continue

            if message_type == "browser_hello":
                client_type = "browser"
                browser_clients.add(websocket)

                print("[БРАУЗЕР] Подключён")

                await send_device_list(websocket)
                await send_parameter_ui_preferences(websocket)
                await send_device_access_state(websocket)
                await send_command_queue_state(websocket)
                await send_database_stats(websocket)
                await send_database_backups(websocket)
                continue

            if client_type == "browser":
                if message_type == "browser_message":
                    await handle_browser_message(websocket, message)
                else:
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Неизвестное сообщение браузера."
                    })

                continue

            # -------------------------
            # Регистрация ESP32
            # -------------------------

            if message_type == "hello":
                if client_type is not None:
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Устройство уже зарегистрировано в этом подключении."
                    })
                    continue

                incoming_id = message.get("device")

                if (
                    not isinstance(incoming_id, str)
                    or not 1 <= len(incoming_id) <= 64
                ):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "В сообщении hello отсутствует device."
                    })
                    await websocket.close(code=4002, reason="Invalid device ID")
                    break

                wire_device_id = incoming_id
                device_id = resolve_device_identity(wire_device_id)

                legacy_data = message.get("data", {})
                if not isinstance(legacy_data, dict):
                    legacy_data = {}
                schema = message.get("schema")
                is_legacy = schema is None
                if is_legacy:
                    schema = default_legacy_schema()
                elif not validate_schema(schema):
                    schema_shape = {
                        "type": type(schema).__name__,
                        "keys": list(schema) if isinstance(schema, dict) else [],
                        "parameters": list(schema.get("parameters", {}))
                        if isinstance(schema, dict)
                        and isinstance(schema.get("parameters", {}), dict)
                        else [],
                        "actions": list(schema.get("actions", {}))
                        if isinstance(schema, dict)
                        and isinstance(schema.get("actions", {}), dict)
                        else []
                    }
                    print(
                        f"[Schema rejected] Некорректная Schema от {wire_device_id}: "
                        f"{schema_shape}"
                    )
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Schema устройства имеет неверный формат."
                    })
                    await websocket.close(code=4002, reason="Invalid device schema")
                    break

                protocol_version = message.get(
                    "protocol_version", legacy_data.get("protocol")
                )
                firmware_version = message.get(
                    "firmware_version", legacy_data.get("firmware")
                )
                name = message.get("name", legacy_data.get("model"))
                schema_version = message.get(
                    "schema_version", None if is_legacy else 1
                )
                command_dedup = message.get("command_dedup", False)
                device_profile = message.get("device_profile")
                board_profile = message.get("board_profile")
                driver_ids = message.get("driver_catalog", [])
                if not is_legacy and schema_version is None:
                    schema_version = 1

                if (
                    type(command_dedup) is not bool
                ) or (
                    protocol_version is not None
                    and (type(protocol_version) is not int or protocol_version < 1)
                ) or (
                    schema_version is not None
                    and (type(schema_version) is not int or schema_version < 1)
                ) or any(
                    value is not None
                    and (not isinstance(value, str) or len(value) > 100)
                    for value in (name, firmware_version)
                ) or (
                    device_profile is not None
                    and (
                        not isinstance(device_profile, str)
                        or len(device_profile) > 40
                    )
                ):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Некорректные метаданные устройства."
                    })
                    await websocket.close(code=4002, reason="Invalid device metadata")
                    break

                if device_profile == "universal":
                    if (
                        board_profile != "esp32-s3-devkitc-1"
                        or not isinstance(driver_ids, list)
                        or not 1 <= len(driver_ids) <= 16
                        or any(
                            not isinstance(driver_id, str)
                            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", driver_id)
                            for driver_id in driver_ids
                        )
                        or any(driver_id not in DRIVERS for driver_id in driver_ids)
                        or len(set(driver_ids)) != len(driver_ids)
                        or schema.get("capabilities", {}).get("universal_profile") is not True
                    ):
                        await send_json(websocket, {
                            "type": "error",
                            "message": "Универсальная прошивка сообщила некорректный профиль или каталог драйверов."
                        })
                        await websocket.close(code=4002, reason="Invalid device capabilities")
                        break
                else:
                    device_profile = "custom" if device_profile is not None else None
                    board_profile = None
                    driver_ids = []

                metadata = {
                    "name": name,
                    "firmware_id": wire_device_id,
                    "protocol_version": protocol_version,
                    "firmware_version": firmware_version,
                    "command_dedup": command_dedup,
                    "schema_version": schema_version,
                    "schema": schema,
                    "device_profile": device_profile,
                    "board_profile": board_profile,
                    "driver_ids": driver_ids
                }

                trust_policy = get_device_trust_policy(device_id)
                if trust_policy == "deny":
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Для этого устройства сохранён запрет подключения."
                    })
                    await websocket.close(code=4003, reason="Device access denied")
                    break

                if trust_policy != "allow":
                    if device_id in pending_device_connections:
                        await send_json(websocket, {
                            "type": "error",
                            "message": "Для этого устройства уже ожидается решение."
                        })
                        await websocket.close(code=4004, reason="Approval already pending")
                        break

                    request = {
                        "request_id": uuid.uuid4().hex,
                        "websocket": websocket,
                        "metadata": metadata,
                        "decision": None,
                        "remember": False,
                        "decision_event": asyncio.Event()
                    }
                    pending_device_connections[device_id] = request
                    await send_device_access_state()
                    approval_wait = asyncio.create_task(
                        request["decision_event"].wait()
                    )
                    closed_wait = asyncio.create_task(websocket.wait_closed())
                    done, waiting = await asyncio.wait(
                        {approval_wait, closed_wait},
                        timeout=DEVICE_APPROVAL_TIMEOUT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in waiting:
                        task.cancel()
                    if waiting:
                        await asyncio.gather(*waiting, return_exceptions=True)

                    if closed_wait in done:
                        if pending_device_connections.get(device_id) is request:
                            pending_device_connections.pop(device_id, None)
                        await send_device_access_state()
                        break

                    if approval_wait not in done:
                        if pending_device_connections.get(device_id) is request:
                            pending_device_connections.pop(device_id, None)
                        await send_device_access_state()
                        try:
                            await websocket.close(
                                code=4000, reason="Device approval timed out"
                            )
                        except Exception:
                            pass
                        break

                    if pending_device_connections.get(device_id) is request:
                        pending_device_connections.pop(device_id, None)
                    await send_device_access_state()
                    if request["decision"] != "allow":
                        try:
                            await websocket.close(
                                code=4003, reason="Device access denied"
                            )
                        except Exception:
                            pass
                        break

                    metadata["trust_policy"] = (
                        "allow" if request["remember"] else None
                    )
                else:
                    metadata["trust_policy"] = "allow"

                if database_restore_in_progress:
                    await websocket.close(
                        code=1012,
                        reason="Gateway database is being restored"
                    )
                    break

                client_type = "device"
                current_device_task = asyncio.current_task()
                if current_device_task is not None:
                    device_handler_tasks.add(current_device_task)

                old_connection = devices.get(device_id)

                if device_id not in device_states:
                    with database_connection() as conn:
                        saved_state_row = conn.execute(
                            "SELECT state_json, led FROM devices WHERE device_id = ?",
                            (device_id,)
                        ).fetchone()
                    if saved_state_row is not None:
                        try:
                            restored_state = json.loads(saved_state_row[0] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            restored_state = {}
                        if isinstance(restored_state, dict):
                            if saved_state_row[1] is not None:
                                restored_state.setdefault("led", bool(saved_state_row[1]))
                            device_states[device_id] = restored_state

                devices[device_id] = websocket
                device_wire_ids[device_id] = wire_device_id
                last_device_heartbeat[device_id] = asyncio.get_running_loop().time()
                fresh_device_parameters[device_id] = set()
                previous_metadata = load_device_metadata(device_id)
                if (
                    metadata.get("device_profile") == "universal"
                    and previous_metadata is not None
                    and previous_metadata.get("device_profile") == "universal"
                ):
                    accepted = dict(metadata)
                    accepted["schema"] = previous_metadata["schema"]
                    accepted["schema_version"] = previous_metadata.get("schema_version")
                    schema_status = "preserved"
                    save_device_metadata(device_id, accepted)
                else:
                    schema_status = offer_device_schema(
                        device_id, metadata, is_legacy=is_legacy
                    )
                save_device_seen(device_id, True)
                if schema_status == "pending":
                    print(
                        f"[Schema] Для {device_id} ожидает подтверждения "
                        f"версия {schema_version}."
                    )

                # Логируем подключение в историю.
                save_device_event(
                    device_id,
                    "connection",
                    "Подключено"
                )

                print(f"[ESP32] Подключено устройство: {device_id}")

                # Закрываем предыдущее соединение с тем же ID.
                if (
                    old_connection is not None
                    and old_connection is not websocket
                ):
                    await finish_pending_commands_for_device(
                        device_id,
                        "unavailable",
                        "Соединение устройства было заменено до получения результата."
                    )

                    try:
                        await old_connection.close()
                    except Exception:
                        pass

                await send_json(websocket, {
                    "type": "hello_ack",
                    "device": device_id,
                    "schema_status": schema_status
                })
                await send_device_list()
                await send_event_history()
                if schema_status != "pending":
                    if metadata.get("device_profile") == "universal":
                        await send_device_configuration(device_id, websocket)
                    await deliver_queued_commands(device_id)
                    fire_automation_event("device_connected", device_id)
                else:
                    deferred_connection_automations.add(device_id)
                await send_command_queue_state()
                continue

            if client_type == "device" and message_type == "device_config_result":
                await handle_device_configuration_result(
                    device_id, wire_device_id, message
                )
                continue

            if client_type == "device" and message_type == "device_config_report":
                await handle_device_configuration_report(
                    device_id, wire_device_id, message
                )
                continue

            if client_type == "device" and message_type == "schema_update":
                schema = message.get("schema")
                schema_version = message.get("schema_version")
                if (
                    not validate_schema(schema)
                    or type(schema_version) is not int
                    or schema_version < 1
                ):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Некорректное обновление Schema."
                    })
                    continue

                metadata = load_device_metadata(device_id) or {
                    "name": None,
                    "protocol_version": None,
                    "firmware_version": None
                }
                metadata = dict(metadata)
                metadata.update({"schema": schema, "schema_version": schema_version})
                schema_status = offer_device_schema(device_id, metadata)
                if schema_status == "pending":
                    print(
                        f"[Schema] Для {device_id} ожидает подтверждения "
                        f"версия {schema_version}."
                    )
                await send_device_list()
                continue

            # -------------------------
            # Состояние от ESP32
            # -------------------------

            if client_type == "device" and message_type == "command_result":
                if message.get("device") != wire_device_id:
                    continue

                command_id = message.get("command_id")
                success = message.get("success")
                data = message.get("data", {})

                if (
                    not isinstance(command_id, str)
                    or not 1 <= len(command_id) <= 80
                    or type(success) is not bool
                    or not isinstance(data, dict)
                ):
                    continue

                parameter = data.get("parameter")
                action = data.get("action")
                value = data.get("value")
                error_message = data.get("error")

                if (
                    not isinstance(parameter or action, str)
                    or not (parameter or action)
                    or (
                        error_message is not None
                        and not isinstance(error_message, str)
                    )
                ):
                    continue

                pending = pending_commands.get(command_id)

                if pending is None or pending["device"] != device_id:
                    print(
                        f"[ПРЕДУПРЕЖДЕНИЕ] Игнорируется неизвестный "
                        f"результат команды {command_id} от {device_id}."
                    )
                    continue

                operation = pending["operation"]
                operation_name = parameter if operation["kind"] == "parameter" else action
                if operation_name != operation["name"]:
                    continue

                if operation["kind"] == "parameter":
                    definition = device_metadata.get(
                        device_id, {"schema": default_legacy_schema()}
                    )["schema"].get("parameters", {}).get(parameter)
                    if (
                        definition is None
                        or not is_schema_value(value, definition)
                        or not value_within_schema_limits(value, definition)
                    ):
                        status = "failed"
                        result_error = "Устройство вернуло значение, не соответствующее Schema."
                        value = None
                    elif success and not schema_values_match(value, operation["value"]):
                        status = "failed"
                        result_error = "Устройство подтвердило значение, отличающееся от запрошенного."
                    elif success:
                        status = "succeeded"
                        result_error = None
                    else:
                        status = "failed"
                        if (
                            definition is None
                            or not is_schema_value(value, definition)
                            or not value_within_schema_limits(value, definition)
                        ):
                            value = None
                        result_error = error_message or "Устройство отклонило команду."
                else:
                    status = "succeeded" if success else "failed"
                    result_error = None if success else error_message or "Действие отклонено устройством."

                # A parameter command result includes the device's actual value.
                # Persist and broadcast it here so the state update does not
                # create a second, duplicate history entry for the same action.
                automation_state_change = None
                if operation["kind"] == "parameter" and value is not None:
                    previous_parameters = dict(device_states.get(device_id, {}))
                    device_states[device_id] = {
                        **previous_parameters,
                        parameter: value
                    }
                    fresh_device_parameters.setdefault(device_id, set()).add(parameter)
                    last_seen, _, _, _ = save_device_state(
                        device_id,
                        {parameter: value},
                        record_changes=False
                    )
                    last_device_heartbeat[device_id] = (
                        asyncio.get_running_loop().time()
                    )
                    await broadcast({
                        "type": "device_state",
                        "device": device_id,
                        "parameters": device_states[device_id],
                        "last_seen": last_seen
                    })
                    if parameter not in previous_parameters or not schema_values_match(previous_parameters[parameter], value):
                        automation_state_change = (
                            previous_parameters,
                            dict(device_states[device_id])
                        )
                        if pending.get("automation_id") and status == "succeeded":
                            pending["automation_state_change"] = {
                                "parameter": parameter,
                                "value": value
                            }

                pending = remove_pending_command(command_id)
                await send_pending_command_result(
                    pending,
                    status,
                    value,
                    result_error
                )
                if automation_state_change is not None:
                    await evaluate_automations(
                        device_id,
                        automation_state_change[0],
                        automation_state_change[1]
                    )
                continue

            if client_type == "device" and message_type == "state":
                if message.get("device") != wire_device_id:
                    continue

                data = message.get("data", {})
                if not isinstance(data, dict):
                    continue
                parameters = data.get("parameters", {})
                if not isinstance(parameters, dict):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Device telemetry parameters must be an object."
                    })
                    continue

                # A schema mismatch must not be mistaken for a dead WebSocket.
                # Track valid device traffic separately from accepted state values.
                last_device_heartbeat[device_id] = asyncio.get_running_loop().time()

                metadata = device_metadata.get(
                    device_id, {"schema": default_legacy_schema()}
                )
                schema = metadata["schema"]
                allow_unknown = metadata.get("schema_version") is None
                pending_schema = pending_device_schemas.get(device_id)
                deferred_schema = deferred_device_schemas.get(device_id)
                review_schema = (
                    pending_schema["schema"]
                    if pending_schema is not None
                    else deferred_schema
                )
                if review_schema is not None:
                    accepted_parameters = accepted_parameters_during_schema_review(
                        schema, review_schema, parameters
                    )
                    state_is_valid = accepted_parameters is not None
                else:
                    state_is_valid = validate_parameters(
                        schema, parameters, allow_unknown
                    )
                    accepted_parameters = parameters
                if not state_is_valid:
                    accepted_parameter_names = sorted(
                        schema.get("parameters", {}).keys()
                    )
                    offered_parameter_names = sorted(parameters.keys())
                    print(
                        f"[STATE REJECTED] {device_id}: offered={offered_parameter_names}, "
                        f"accepted={accepted_parameter_names}, "
                        f"schema_review={review_schema is not None}"
                    )
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Состояние устройства не соответствует Schema."
                    })
                    continue

                if allow_unknown:
                    updated_schema = json.loads(json.dumps(schema))
                    updated_schema.setdefault("parameters", {})
                    schema_changed = False
                    for key, value in accepted_parameters.items():
                        if key not in updated_schema["parameters"]:
                            updated_schema["parameters"][key] = inferred_definition(key, value)
                            schema_changed = True
                    if schema_changed:
                        if len(updated_schema["parameters"]) > 64:
                            await send_json(websocket, {
                                "type": "error",
                                "message": "В Schema превышен лимит параметров."
                            })
                            continue
                        metadata["schema"] = updated_schema
                        save_device_metadata(device_id, metadata)
                        await send_device_list()

                previous_parameters = dict(device_states.get(device_id, {}))
                telemetry_changed = any(
                    key not in previous_parameters or not schema_values_match(previous_parameters[key], value)
                    for key, value in accepted_parameters.items()
                )
                device_states[device_id] = {
                    **device_states.get(device_id, {}),
                    **accepted_parameters
                }
                fresh_device_parameters.setdefault(device_id, set()).update(accepted_parameters)

                pending_state_echoes = {}
                for pending in pending_commands.values():
                    operation = pending["operation"]
                    if (
                        pending["device"] == device_id
                        and operation["kind"] == "parameter"
                        and operation["name"] in accepted_parameters
                        and schema_values_match(accepted_parameters[operation["name"]], operation["value"])
                    ):
                        pending_state_echoes[operation["name"]] = pending

                last_seen, state_changed, deferred_events, last_telemetry_at = save_device_state(
                    device_id,
                    accepted_parameters,
                    defer_parameters=set(pending_state_echoes),
                    record_telemetry=bool(accepted_parameters)
                )
                for state_event in deferred_events:
                    pending = pending_state_echoes.get(state_event["parameter"])
                    if pending is not None:
                        pending.setdefault("deferred_state_events", []).append(
                            state_event
                        )
                print(
                    f"[ТЕЛЕМЕТРИЯ] {device_id}: получено={parameters}; "
                    f"принято текущей Schema={accepted_parameters}"
                )

                state_message = {
                    "type": "device_state",
                    "device": device_id,
                    "parameters": device_states[device_id],
                    "last_seen": last_seen,
                    "last_telemetry_at": last_telemetry_at
                }

                await broadcast(state_message)

                if telemetry_changed:
                    await evaluate_automations(
                        device_id, previous_parameters, device_states[device_id]
                    )

                if state_changed:
                    await send_event_history()
                continue

            if client_type is None:
                print(
                    f"[ПРЕДУПРЕЖДЕНИЕ] Сообщение до регистрации hello "
                    f"от {remote}: {message_type}"
                )
                await send_json(websocket, {
                    "type": "error",
                    "message": "Сначала устройство должно отправить корректное hello."
                })
                await websocket.close(
                    code=4002,
                    reason="Device must register with hello first"
                )
                break

            print(
                f"[ПРЕДУПРЕЖДЕНИЕ] Неизвестное сообщение от {client_type}: "
                f"{message}"
            )

    except ConnectionClosed:
        pass

    except Exception as error:
        print(f"[ОШИБКА] Обработка соединения: {error}")

    finally:
        browser_clients.discard(websocket)

        if device_id:
            pending_access = pending_device_connections.get(device_id)
            if (
                pending_access is not None
                and pending_access.get("websocket") is websocket
            ):
                pending_device_connections.pop(device_id, None)
                await send_device_access_state()

        # Обрабатываем отключение только текущего соединения.
        # Это не даст старому соединению ошибочно отметить
        # новое соединение того же устройства отключённым.
        if (
            device_id
            and devices.get(device_id) is websocket
        ):
            cancel_pending_automation_triggers_for_device(device_id)
            del devices[device_id]
            device_wire_ids.pop(device_id, None)
            last_device_heartbeat.pop(device_id, None)
            fresh_device_parameters.pop(device_id, None)
            deferred_connection_automations.discard(device_id)
            deferred_device_schemas.pop(device_id, None)

            await finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Устройство отключилось до получения результата команды."
            )

            with database_connection() as conn:
                conn.execute("""
                    UPDATE devices
                    SET connected = 0
                    WHERE device_id = ?
                """, (device_id,))

            save_device_event(
                device_id,
                "connection",
                "Отключено"
            )

            print(f"[ESP32] Отключено устройство: {device_id}")

            await send_device_list()
            await send_event_history()
            await send_command_queue_state()
            fire_automation_event("device_disconnected", device_id)

        elif client_type == "browser":
            detach_pending_commands_for_browser(websocket)
            print("[БРАУЗЕР] Отключён")

        current_task = asyncio.current_task()
        if current_task is not None:
            device_handler_tasks.discard(current_task)


# =========================
# КОНСОЛЬНЫЕ КОМАНДЫ
# =========================

async def console_loop():
    print()
    print("Команды консоли:")
    print("  devices          — список устройств")
    print("  status           — статус Gateway")
    print("  schema [device]  — показать Schema устройства")
    print("  set <param> <json> — установить значение параметра")
    print("  call <action_id> [json_arguments] — вызвать действие Schema")
    print("  use <device_id>  — выбрать устройство")
    print("  events           — последние события")
    print("  history          — устройства в базе данных")
    print("  exit             — остановить Gateway")
    print()

    selected_device = None

    while True:
        try:
            command_line = await asyncio.to_thread(
                input,
                "Gateway> "
            )
        except (EOFError, KeyboardInterrupt):
            return

        parts = command_line.strip().split(maxsplit=2)

        if not parts:
            continue

        command = parts[0].lower()

        if command == "exit":
            print("Останавливаем Gateway...")
            return

        elif command == "status":
            print(f"WebSocket: ws://0.0.0.0:{PORT}")
            print(f"Подключено ESP32: {len(devices)}")
            print(f"Подключено браузеров: {len(browser_clients)}")

        elif command == "devices":
            all_devices = get_all_devices()

            for item in all_devices:
                status = (
                    "онлайн"
                    if item["connected"]
                    else "офлайн"
                )

                print(
                    f'{item["device"]} ({item.get("name") or "без имени"}): '
                    f'{status}, firmware={item.get("firmware_version") or "—"}, '
                    f'schema={item.get("schema_version") or "—"}, '
                    f'{item["parameters"]}'
                )

            if not all_devices:
                print("Устройств пока нет.")

        elif command == "use":
            if len(parts) < 2:
                print("Использование: use <device_id>")
            else:
                selected_device = parts[1]
                print(f"Выбрано устройство: {selected_device}")

        elif command == "schema":
            target_device = parts[1] if len(parts) > 1 else selected_device
            if not target_device:
                print("Укажи устройство: schema <device_id> или выбери его через use.")
                continue
            item = next(
                (entry for entry in get_all_devices() if entry["device"] == target_device),
                None
            )
            if item is None:
                print(f"Устройство {target_device} не найдено.")
            else:
                print(json.dumps(item["schema"], ensure_ascii=False, indent=2))

        elif command == "set":
            if not selected_device:
                print("Сначала выбери устройство: use <device_id>")
                continue
            if len(parts) < 3:
                print('Использование: set <parameter> <json_value> (например: set led true)')
                continue
            try:
                value = json.loads(parts[2])
            except json.JSONDecodeError as error:
                print(f"Значение должно быть корректным JSON: {error}")
                continue
            await dispatch_device_command(selected_device, parts[1], value)

        elif command == "call":
            if not selected_device:
                print("Сначала выбери устройство: use <device_id>")
                continue
            if len(parts) < 2:
                print("Использование: call <action_id> [json_arguments]")
                continue
            arguments = {}
            if len(parts) > 2:
                try:
                    arguments = json.loads(parts[2])
                except json.JSONDecodeError as error:
                    print(f"Аргументы должны быть корректным JSON: {error}")
                    continue
            if not isinstance(arguments, dict):
                print("Аргументы действия должны быть JSON-объектом.")
                continue
            await dispatch_device_action(
                selected_device,
                parts[1],
                arguments=arguments
            )

        elif command == "events":
            for event in get_recent_events(10):
                print(
                    f'{event["timestamp"]} | '
                    f'{event["device_id"]} | '
                    f'{event["event_type"]}: '
                    f'{event["event_value"]}'
                )

        elif command == "history":
            with database_connection() as conn:
                rows = conn.execute("""
                    SELECT device_id, name, last_seen, state_json, led, connected,
                           firmware_version, schema_version
                    FROM devices
                    ORDER BY device_id
                """).fetchall()

            for row in rows:
                try:
                    state = json.loads(row[3] or "{}")
                except json.JSONDecodeError:
                    state = {}
                if row[4] is not None:
                    state.setdefault("led", bool(row[4]))
                print(
                    f"ID: {row[0]} ({row[1] or 'без имени'}), "
                    f"последнее сообщение: {row[2]}, "
                    f"состояние: {state}, "
                    f"firmware: {row[6] or '—'}, schema: {row[7] or '—'}, "
                    f"подключено сейчас: {row[0] in devices}"
                )

            if not rows:
                print("История устройств пока пуста.")

        else:
            print(
                "Неизвестная команда. Доступно: devices, status, schema, set, "
                "call, use, events, history, exit."
            )


# =========================
# ЗАПУСК GATEWAY
# =========================

async def device_presence_watchdog():
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(5)
        now = loop.time()

        for device_id, last_seen in list(last_device_heartbeat.items()):
            if now - last_seen <= DEVICE_STALE_SECONDS:
                continue

            # Старые прошивки без Schema не отправляют периодическое state;
            # для них остаётся контроль WebSocket ping/pong.
            if device_metadata.get(device_id, {}).get("schema_version") is None:
                continue

            websocket = devices.get(device_id)
            if websocket is None:
                continue

            print(
                f"[ПРЕДУПРЕЖДЕНИЕ] Нет телеметрии от {device_id} "
                f"более {DEVICE_STALE_SECONDS} секунд; закрываем соединение."
            )
            try:
                await websocket.close(
                    code=1011,
                    reason="Device telemetry timed out"
                )
            except Exception:
                pass

async def main():
    init_db()
    load_pending_commands()
    sync_device_configuration_exports()

    print("=" * 48)
    print("        IoT Gateway запускается")
    print("=" * 48)

    async with websockets.serve(
        handle_client,
        HOST,
        PORT,
        process_request=handle_http_request,
        max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        max_queue=16,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5
    ):
        print(f"WebSocket-сервер запущен на порту {PORT}")
        print("Ожидание подключений ESP32 и браузера...")
        watchdog_task = asyncio.create_task(device_presence_watchdog())
        runtime_task = asyncio.create_task(gateway_runtime_checkpoint_loop())
        fire_automation_event("gateway_started")
        try:
            if sys.stdin.isatty():
                await console_loop()
            else:
                print("Интерактивная консоль отключена; Gateway работает в служебном режиме.")
                await asyncio.Event().wait()
        finally:
            watchdog_task.cancel()
            runtime_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
            try:
                await runtime_task
            except asyncio.CancelledError:
                pass
            checkpoint_gateway_runtime(final=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway остановлен.")
