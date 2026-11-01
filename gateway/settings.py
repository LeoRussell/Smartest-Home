"""Environment settings and default runtime limits."""

import os
import re
from pathlib import Path

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

COMMAND_TIMEOUT_SECONDS = 5

EVENTS_PAGE_SIZE = 30

DEVICE_STALE_SECONDS = 45

DEVICE_TRANSPORT_IDLE_SECONDS = 45

DEVICE_TRANSPORT_PROBE_TIMEOUT_SECONDS = 20

MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024

MAX_SCHEMA_BYTES = 60 * 1024

DEVICE_APPROVAL_TIMEOUT_SECONDS = 180

COMMAND_RETRY_LIMIT = 3

CONFIGURATION_APPLY_TIMEOUT_SECONDS = 20

MAX_CONFIGURATION_VERSION = 0xFFFFFFFF

GATEWAY_RUNTIME_CHECKPOINT_SECONDS = 30

AUTOMATION_RUN_HISTORY_LIMIT = 50
