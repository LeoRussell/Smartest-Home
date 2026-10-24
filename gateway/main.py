import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


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

# Активные ESP32: device_id -> WebSocket
devices = {}

# Подключённые браузеры
browser_clients = set()

# Последние известные состояния устройств
device_states = {}
device_metadata = {}
last_device_heartbeat = {}
pending_device_schemas = {}
deferred_device_schemas = {}
pending_device_connections = {}

# Команды, ожидающие результата: command_id -> контекст команды.
pending_commands = {}
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


def init_db():
    global GATEWAY_SESSION_STARTED_AT

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                led INTEGER,
                connected INTEGER DEFAULT 0
            )
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
            CREATE INDEX IF NOT EXISTS idx_command_queue_device_status
            ON command_queue (device_id, status)
        """)

        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(devices)").fetchall()
        }
        metadata_columns = {
            "name": "TEXT",
            "protocol_version": "INTEGER",
            "firmware_version": "TEXT",
            "command_dedup": "INTEGER DEFAULT 0",
            "schema_version": "INTEGER",
            "schema_json": "TEXT",
            "state_json": "TEXT",
            "trust_policy": "TEXT DEFAULT 'allow'"
        }

        for column, column_type in metadata_columns.items():
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE devices ADD COLUMN {column} {column_type}"
                )

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

        conn.commit()

    print(f"База данных: {DB_PATH}")


    GATEWAY_SESSION_STARTED_AT = datetime.now(timezone.utc)


def checkpoint_gateway_runtime(final=False):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()


def get_database_stats():
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB_PATH) as conn:
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
        command_record_count = conn.execute(
            "SELECT COUNT(*) FROM command_queue"
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
        ),
        "device_count": device_count,
        "ever_connected_device_count": ever_connected_device_count,
        "connected_device_count": len(devices),
        "event_count": event_count,
        "schema_snapshot_count": schema_snapshot_count,
        "command_record_count": command_record_count,
        "session_uptime_seconds": session_uptime_seconds,
        "total_uptime_seconds": total_uptime_seconds,
        "sampled_at": now.timestamp()
    }


async def gateway_runtime_checkpoint_loop():
    while True:
        await asyncio.sleep(GATEWAY_RUNTIME_CHECKPOINT_SECONDS)
        checkpoint_gateway_runtime()


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

    if not isinstance(schema, dict) or not isinstance(schema.get("parameters", {}), dict):
        return False

    actions = schema.get("actions", {})
    if not isinstance(actions, dict) or len(actions) > 32:
        return False
    parameters = schema.get("parameters", {})
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
        "label", "type", "access", "unit", "precision", "min", "max",
        "step", "options"
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


def save_device_metadata(device_id, metadata):
    schema_json = json.dumps(metadata["schema"], ensure_ascii=False)
    timestamp = current_time()

    with sqlite3.connect(DB_PATH) as conn:
        previous = conn.execute(
            "SELECT schema_json, schema_version, firmware_version FROM devices WHERE device_id = ?",
            (device_id,)
        ).fetchone()
        conn.execute("""
            INSERT INTO devices (
                device_id, last_seen, connected, name,
                protocol_version, firmware_version, command_dedup,
                schema_version, schema_json,
                trust_policy
            )
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                connected = 1,
                name = excluded.name,
                protocol_version = excluded.protocol_version,
                firmware_version = excluded.firmware_version,
                command_dedup = excluded.command_dedup,
                schema_version = excluded.schema_version,
                schema_json = excluded.schema_json,
                trust_policy = excluded.trust_policy
        """, (
            device_id,
            timestamp,
            metadata.get("name"),
            metadata.get("protocol_version"),
            metadata.get("firmware_version"),
            int(metadata.get("command_dedup") is True),
            metadata.get("schema_version"),
            schema_json,
            metadata.get("trust_policy")
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
        conn.commit()

    device_metadata[device_id] = metadata


def load_device_metadata(device_id):
    metadata = device_metadata.get(device_id)
    if metadata is not None:
        return dict(metadata)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT name, protocol_version, firmware_version,
                   command_dedup, schema_version, schema_json, trust_policy
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

    return {
        "name": row["name"],
        "protocol_version": row["protocol_version"],
        "firmware_version": row["firmware_version"],
        "command_dedup": bool(row["command_dedup"]),
        "schema_version": row["schema_version"],
        "schema": schema,
        "trust_policy": row["trust_policy"]
    }


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
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM device_schema_history WHERE id = ? AND device_id = ?",
            (history_id, device_id)
        )
        conn.commit()
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

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("""
            UPDATE devices
            SET schema_version = ?, firmware_version = ?, schema_json = ?,
                connected = 0
            WHERE device_id = ?
        """, (
            history.get("schema_version"), history.get("firmware_version"),
            json.dumps(history["schema"], ensure_ascii=False), device_id
        ))
        conn.commit()
        if cursor.rowcount == 0:
            return False, "Устройство не найдено."

    device_metadata.pop(device_id, None)
    pending_device_schemas.pop(device_id, None)
    deferred_device_schemas.pop(device_id, None)
    save_device_event(
        device_id,
        "schema",
        f"Восстановлена Schema из истории (снимок {history_id})"
    )
    return True, "Schema Gateway восстановлена. При следующем подключении она будет сравнена с Schema устройства."


def get_device_trust_policy(device_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT trust_policy FROM devices WHERE device_id = ?",
            (device_id,)
        ).fetchone()
    return row[0] if row else None


def set_device_trust_policy(device_id, policy, name=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO devices (device_id, connected, trust_policy, name)
            VALUES (?, 0, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                trust_policy = excluded.trust_policy,
                name = COALESCE(excluded.name, devices.name)
        """, (device_id, policy, name))
        conn.commit()
    if device_id in device_metadata:
        device_metadata[device_id]["trust_policy"] = policy


def get_blocked_devices():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT device_id, name, last_seen
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
    with sqlite3.connect(DB_PATH) as conn:
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
            "sending": False
        }


def create_command_queue_record(pending):
    timestamp = current_time()
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()


def update_command_queue_record(pending, status, result=None, error=None):
    with sqlite3.connect(DB_PATH) as conn:
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
        conn.commit()


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
        await send_json(websocket, pending["payload"])
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

    with sqlite3.connect(DB_PATH) as conn:
        save_event_in_connection(
            conn,
            device_id,
            event_type,
            event_value,
            timestamp
        )
        conn.commit()

    return timestamp


def save_device_seen(device_id, connected):
    timestamp = current_time()

    with sqlite3.connect(DB_PATH) as conn:
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

        conn.commit()

    return timestamp


def save_device_state(
    device_id,
    parameters,
    record_changes=True,
    defer_parameters=None
):
    timestamp = current_time()
    has_changes = False
    deferred_events = []
    defer_parameters = defer_parameters or set()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT led, state_json
            FROM devices
            WHERE device_id = ?
        """, (device_id,)).fetchone()

        if row is None:
            conn.execute("""
                INSERT INTO devices (
                    device_id,
                    last_seen,
                    connected
                )
                VALUES (?, ?, 1)
            """, (
                device_id,
                timestamp
            ))

            previous_led = None
            saved_parameters = {}
        else:
            previous_led = row[0]
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
                or saved_parameters[parameter_id] == value
            ):
                continue

            if parameter_id == "led" and type(value) is bool:
                event_type = "led"
                event_value = str(value).lower()
            else:
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
                    state_json = ?
                WHERE device_id = ?
            """, (
                new_led,
                timestamp,
                state_json,
                device_id
            ))

        else:
            conn.execute("""
                UPDATE devices
                SET last_seen = ?,
                    state_json = ?,
                    connected = 1
                WHERE device_id = ?
            """, (
                timestamp,
                state_json,
                device_id
            ))

        conn.commit()

    return timestamp, has_changes, deferred_events


def get_recent_events(limit=30):
    """
    Возвращает последние значимые события.

    Старые повторяющиеся подряд значения LED скрываются
    в выдаче, даже если они уже сохранены в SQLite.

    Записи в самой базе при этом не удаляются.
    """

    with sqlite3.connect(DB_PATH) as conn:
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


def get_event_page(limit=EVENTS_PAGE_SIZE, device_id=None, before_id=None):
    conditions = []
    parameters = []

    if device_id is not None:
        conditions.append("device_id = ?")
        parameters.append(device_id)

    if before_id is not None:
        conditions.append("id < ?")
        parameters.append(before_id)

    where_clause = (
        f"WHERE {' AND '.join(conditions)}"
        if conditions
        else ""
    )

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
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

    return events, has_more, next_before_id


def get_all_devices():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT
                device_id, last_seen, led, connected, name, state_json,
                protocol_version, firmware_version, command_dedup,
                schema_version, schema_json,
                trust_policy
            FROM devices
            WHERE trust_policy IS NULL
               OR trust_policy NOT IN ('deny', 'forgotten')
            ORDER BY device_id
        """).fetchall()

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
                "protocol_version": row["protocol_version"],
                "firmware_version": row["firmware_version"],
                "command_dedup": bool(row["command_dedup"]),
                "schema_version": row["schema_version"],
                "schema": stored_schema,
                "trust_policy": row["trust_policy"]
            }

        result.append({
            "device": device_id,
            "name": metadata.get("name"),
            "connected": device_id in devices,
            "last_seen": row["last_seen"],
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
            "pending_schema": public_schema_proposal(device_id)
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
                "connected": True,
                "last_seen": None,
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
                "pending_schema": public_schema_proposal(device_id)
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


async def send_database_stats(websocket=None):
    message = {
        "type": "database_stats",
        **get_database_stats()
    }
    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_event_history(
    websocket=None,
    device_id=None,
    before_id=None,
    request_id=None
):
    events, has_more, next_before_id = get_event_page(
        device_id=device_id,
        before_id=before_id
    )

    message = {
        "type": "event_history",
        "events": events,
        "has_more": has_more,
        "next_before_id": next_before_id,
        "device_filter": device_id or "all"
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
    update_command_queue_record(pending, status, result_record, error)
    deferred_state_events = pending.pop("deferred_state_events", [])
    for state_event in deferred_state_events:
        is_confirmed_command_state = (
            status in ("succeeded", "failed")
            and value is not None
            and operation["kind"] == "parameter"
            and state_event["parameter"] == operation["name"]
            and state_event["value"] == value
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

    event_value = json.dumps({
        "command_id": pending["command_id"],
        operation["kind"]: operation["name"],
        "requested_value": operation.get("value"),
        "arguments": operation.get("arguments", {}),
        "result_value": value if status in ("succeeded", "failed") else None,
        "status": status,
        "error": error
    }, ensure_ascii=False)
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
        await send_json(pending["browser"], message)
    except Exception:
        pass
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
    requested_command_id=None
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
        "operation": {"kind": "parameter", "name": parameter, "value": value},
        "payload": payload,
        "attempts": 0,
        "status": "queued",
        "timeout_task": None,
        "sending": False
    }
    try:
        create_command_queue_record(pending)
    except sqlite3.IntegrityError:
        await reject("Команда с таким command_id уже использовалась.", command_id)
        return False
    pending_commands[command_id] = pending
    await deliver_pending_command(pending)

    print(f"[КОМАНДА] {device_id}: {parameter} = {value} ({command_id})")
    return True


async def dispatch_device_action(
    device_id,
    action,
    arguments=None,
    browser=None,
    requested_command_id=None
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
            "arguments": arguments
        },
        "payload": payload,
        "attempts": 0,
        "status": "queued",
        "timeout_task": None,
        "sending": False
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
    return True


async def handle_browser_message(websocket, message):

    action = message.get("action")

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
            active_socket = devices.pop(device_id, None)
            last_device_heartbeat.pop(device_id, None)
            pending_device_schemas.pop(device_id, None)
            deferred_device_schemas.pop(device_id, None)
            await finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Подключение устройства запрещено.",
                retry=False
            )
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE devices SET connected = 0 WHERE device_id = ?",
                    (device_id,)
                )
                conn.commit()
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

        with sqlite3.connect(DB_PATH) as conn:
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
        active_socket = devices.pop(device_id, None)
        last_device_heartbeat.pop(device_id, None)
        pending_device_schemas.pop(device_id, None)
        deferred_device_schemas.pop(device_id, None)
        await finish_pending_commands_for_device(
            device_id,
            "unavailable",
            "Доступ к устройству отозван.",
            retry=False
        )
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE devices SET connected = 0 WHERE device_id = ?",
                (device_id,)
            )
            conn.commit()
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

    if action == "get_devices":
        await send_device_list(websocket)
        return

    if action == "get_database_stats":
        await send_database_stats(websocket)
        return

    if action == "get_events":
        device_id = message.get("device", "all")
        before_id = message.get("before_id")
        request_id = message.get("request_id")

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
            request_id=request_id
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
        await send_event_history()
        await send_command_queue_state()
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
                await send_device_access_state(websocket)
                await send_command_queue_state(websocket)
                await send_database_stats(websocket)
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
                    continue

                legacy_data = message.get("data", {})
                if not isinstance(legacy_data, dict):
                    legacy_data = {}
                schema = message.get("schema")
                is_legacy = schema is None
                if is_legacy:
                    schema = default_legacy_schema()
                elif not validate_schema(schema):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Schema устройства имеет неверный формат."
                    })
                    continue

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
                ):
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Некорректные метаданные устройства."
                    })
                    continue

                metadata = {
                    "name": name,
                    "protocol_version": protocol_version,
                    "firmware_version": firmware_version,
                    "command_dedup": command_dedup,
                    "schema_version": schema_version,
                    "schema": schema
                }

                device_id = str(incoming_id)
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

                client_type = "device"

                old_connection = devices.get(device_id)

                devices[device_id] = websocket
                last_device_heartbeat[device_id] = asyncio.get_running_loop().time()
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

                await send_device_list()
                await send_event_history()
                if schema_status != "pending":
                    await deliver_queued_commands(device_id)
                await send_command_queue_state()
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
                if message.get("device") != device_id:
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
                    elif success and value != operation["value"]:
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
                if operation["kind"] == "parameter" and value is not None:
                    device_states[device_id] = {
                        **device_states.get(device_id, {}),
                        parameter: value
                    }
                    last_seen, _, _ = save_device_state(
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

                pending = remove_pending_command(command_id)
                await send_pending_command_result(
                    pending,
                    status,
                    value,
                    result_error
                )
                continue

            if client_type == "device" and message_type == "state":
                if message.get("device") != device_id:
                    continue

                data = message.get("data", {})
                if not isinstance(data, dict):
                    continue
                parameters = data.get("parameters", {})

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

                device_states[device_id] = {
                    **device_states.get(device_id, {}),
                    **accepted_parameters
                }

                pending_state_echoes = {}
                for pending in pending_commands.values():
                    operation = pending["operation"]
                    if (
                        pending["device"] == device_id
                        and operation["kind"] == "parameter"
                        and operation["name"] in accepted_parameters
                        and accepted_parameters[operation["name"]] == operation["value"]
                    ):
                        pending_state_echoes[operation["name"]] = pending

                last_seen, state_changed, deferred_events = save_device_state(
                    device_id,
                    accepted_parameters,
                    defer_parameters=set(pending_state_echoes)
                )
                for state_event in deferred_events:
                    pending = pending_state_echoes.get(state_event["parameter"])
                    if pending is not None:
                        pending.setdefault("deferred_state_events", []).append(
                            state_event
                        )
                last_device_heartbeat[device_id] = asyncio.get_running_loop().time()

                print(
                    f"[СОСТОЯНИЕ] {device_id}: {accepted_parameters}"
                )

                state_message = {
                    "type": "device_state",
                    "device": device_id,
                    "parameters": device_states[device_id],
                    "last_seen": last_seen
                }

                await broadcast(state_message)

                if state_changed:
                    await send_event_history()
                continue

            print(f"[ПРЕДУПРЕЖДЕНИЕ] Неизвестное сообщение: {message}")

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
            del devices[device_id]
            last_device_heartbeat.pop(device_id, None)
            deferred_device_schemas.pop(device_id, None)

            await finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Устройство отключилось до получения результата команды."
            )

            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    UPDATE devices
                    SET connected = 0
                    WHERE device_id = ?
                """, (device_id,))
                conn.commit()

            save_device_event(
                device_id,
                "connection",
                "Отключено"
            )

            print(f"[ESP32] Отключено устройство: {device_id}")

            await send_device_list()
            await send_event_history()
            await send_command_queue_state()

        elif client_type == "browser":
            detach_pending_commands_for_browser(websocket)
            print("[БРАУЗЕР] Отключён")


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
            with sqlite3.connect(DB_PATH) as conn:
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

    print("=" * 48)
    print("        IoT Gateway запускается")
    print("=" * 48)

    async with websockets.serve(
        handle_client,
        HOST,
        PORT,
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
