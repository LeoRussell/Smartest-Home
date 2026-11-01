"""Device groups, explicit shared commands, health and event retention.

The service receives a Gateway runtime; it never opens the production database
at import time. Shared commands use the normal validated command/ACK pipeline.
"""

import asyncio
import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone


DEFAULT_EVENT_RETENTION_DAYS = 90
MAX_EVENT_RETENTION_DAYS = 3650
GROUP_ID_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
SUPPORTED_COMMON_COMMANDS = {"off"}


def operation_error(gateway, schema, operation):
    """Return a useful validation error, or None for a declared operation."""
    if not isinstance(operation, dict):
        return "Нужно выбрать параметр или действие устройства."
    kind = operation.get("kind")
    name = operation.get("name")
    if not isinstance(name, str):
        return "Не указан ID параметра или действия."
    if kind == "parameter":
        definition = schema.get("parameters", {}).get(name)
        if set(operation) != {"kind", "name", "value"}:
            return "Некорректное назначение параметра."
        if not definition or definition.get("access") != "read_write":
            return "Параметр отсутствует в Schema или доступен только для чтения."
        value = operation["value"]
        if not gateway.is_schema_value(value, definition) or not gateway.value_within_schema_limits(value, definition):
            return "Значение общей команды не соответствует типу или диапазону Schema."
    elif kind == "action":
        definition = schema.get("actions", {}).get(name)
        if set(operation) != {"kind", "name", "arguments"}:
            return "Некорректное назначение действия."
        if not definition or not gateway.validate_action_arguments(definition, operation["arguments"]):
            return "Действие или его аргументы не соответствуют Schema."
    else:
        return "Общая команда должна устанавливать параметр или вызывать действие."
    return None


def validate_schema_common_commands(gateway, schema):
    definitions = schema.get("common_commands", {})
    return (
        isinstance(definitions, dict)
        and set(definitions).issubset(SUPPORTED_COMMON_COMMANDS)
        and all(operation_error(gateway, schema, value) is None for value in definitions.values())
    )


class DeviceManagement:
    def __init__(self, gateway):
        self.gateway = gateway
        self.last_cleanup_at = None
        self.last_cleanup_deleted = 0
        self.bulk_tasks = set()
        self.bulk_busy_devices = set()

    def initialize(self):
        with self.gateway.database_connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS gateway_settings (
                    setting_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_device_events_timestamp_jd
                    ON device_events(julianday(timestamp));
                CREATE TABLE IF NOT EXISTS device_groups (
                    group_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_group_membership (
                    device_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_device_group_membership_group
                    ON device_group_membership(group_id);
                CREATE TABLE IF NOT EXISTS device_common_commands (
                    device_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    operation_json TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, command)
                );
                CREATE TABLE IF NOT EXISTS device_health (
                    device_id TEXT PRIMARY KEY,
                    health_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT,
                    last_disconnect_reason TEXT,
                    last_disconnect_at TEXT
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO gateway_settings VALUES (?, ?)",
                ("event_retention_days", json.dumps(DEFAULT_EVENT_RETENTION_DAYS)),
            )

    def settings(self):
        with self.gateway.database_connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM gateway_settings WHERE setting_key = ?",
                ("event_retention_days",),
            ).fetchone()
        try:
            days = json.loads(row[0]) if row else DEFAULT_EVENT_RETENTION_DAYS
        except (TypeError, ValueError, RecursionError):
            days = DEFAULT_EVENT_RETENTION_DAYS
        if type(days) is not int or not 0 <= days <= MAX_EVENT_RETENTION_DAYS:
            days = DEFAULT_EVENT_RETENTION_DAYS
        return {
            "event_retention_days": days,
            "last_cleanup_at": self.last_cleanup_at,
            "deleted_events": self.last_cleanup_deleted,
        }

    def cleanup_events(self):
        days = self.settings()["event_retention_days"]
        deleted = 0
        if days and not self.gateway.database_restore_in_progress:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            with self.gateway.database_connection() as connection:
                cursor = connection.execute(
                    "DELETE FROM device_events WHERE julianday(timestamp) < julianday(?)",
                    (cutoff,),
                )
                deleted = cursor.rowcount
        self.last_cleanup_at = self.gateway.current_time()
        self.last_cleanup_deleted = deleted
        return deleted

    async def maintenance_loop(self):
        while True:
            try:
                if not self.gateway.database_restore_in_progress:
                    deleted = self.cleanup_events()
                    if deleted:
                        await self.gateway.send_event_history()
                        await self.gateway.send_database_stats()
            except (OSError, sqlite3.Error) as error:
                print(f"[RETENTION] Cleanup failed: {error}")
            await asyncio.sleep(300)

    def record_health(self, device_id, health):
        if not isinstance(health, dict) or len(health) > 16:
            return False
        accepted = {}
        rssi = health.get("wifi_rssi")
        uptime = health.get("uptime_seconds")
        if type(rssi) is int and -127 <= rssi <= 0:
            accepted["wifi_rssi"] = rssi
        if type(uptime) is int and 0 <= uptime <= 0x7FFFFFFFFFFFFFFF:
            accepted["uptime_seconds"] = uptime
        for source, target in (("reset_reason", "reset_reason"), ("last_disconnect_reason", "device_disconnect_reason")):
            value = health.get(source)
            if isinstance(value, str) and 0 < len(value) <= 240:
                try:
                    value.encode("utf-8")
                except UnicodeError:
                    continue
                if any(unicodedata.category(character) == "Cc" for character in value):
                    continue
                accepted[target] = value
            elif source in health and value is None:
                accepted[target] = None
        if not accepted:
            return False
        timestamp = self.gateway.current_time()
        # A new report is a snapshot. Never carry a previous boot's RSSI or
        # uptime into a report that did not provide those fields.
        with self.gateway.database_connection() as connection:
            connection.execute("""
                INSERT INTO device_health (device_id, health_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    health_json = excluded.health_json, updated_at = excluded.updated_at
            """, (device_id, json.dumps(accepted, ensure_ascii=False), timestamp))
        return True

    def record_disconnect(self, device_id, code=None, reason=None):
        description = f"WebSocket {code if code is not None else 'closed'}"
        if isinstance(reason, str) and reason.strip():
            description += f": {reason.strip()[:200]}"
        with self.gateway.database_connection() as connection:
            connection.execute("""
                INSERT INTO device_health (device_id, last_disconnect_reason, last_disconnect_at)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_disconnect_reason = excluded.last_disconnect_reason,
                    last_disconnect_at = excluded.last_disconnect_at
            """, (device_id, description, self.gateway.current_time()))

    def clear_live_health(self, device_id):
        """A legacy firmware must not appear to send health from a previous boot."""
        with self.gateway.database_connection() as connection:
            connection.execute(
                "UPDATE device_health SET health_json = '{}', updated_at = NULL WHERE device_id = ?",
                (device_id,),
            )

    def groups(self):
        with self.gateway.database_connection() as connection:
            rows = connection.execute("""
                SELECT g.group_id, g.name, COUNT(d.device_id)
                FROM device_groups AS g
                LEFT JOIN device_group_membership AS m ON m.group_id = g.group_id
                LEFT JOIN devices AS d ON d.device_id = m.device_id
                    AND COALESCE(d.trust_policy, '') NOT IN ('deny', 'forgotten')
                GROUP BY g.group_id, g.name ORDER BY g.name COLLATE NOCASE, g.group_id
            """).fetchall()
        return [{"group_id": row[0], "name": row[1], "device_count": row[2]} for row in rows]

    @staticmethod
    def _public_health(row):
        if row is None:
            return {}
        _, document, timestamp, reason, disconnected_at = row
        try:
            health = json.loads(document)
        except (TypeError, ValueError, RecursionError):
            health = {}
        if not isinstance(health, dict):
            health = {}
        device_reason = health.get("device_disconnect_reason")
        health["updated_at"] = timestamp
        health["last_disconnect_at"] = disconnected_at
        health["last_disconnect_reason"] = (
            f"{reason}; {device_reason}" if reason and device_reason
            else reason or device_reason
        )
        return health

    def health_for_device(self, device_id):
        with self.gateway.database_connection() as connection:
            row = connection.execute(
                "SELECT device_id, health_json, updated_at, last_disconnect_reason, last_disconnect_at "
                "FROM device_health WHERE device_id = ?", (device_id,),
            ).fetchone()
        return self._public_health(row)

    def decorate_devices(self, devices):
        with self.gateway.database_connection() as connection:
            membership = {row[0]: (row[1], row[2]) for row in connection.execute("""
                SELECT m.device_id, g.group_id, g.name FROM device_group_membership m
                JOIN device_groups g ON g.group_id = m.group_id
            """)}
            overrides = {}
            for device_id, command, document in connection.execute(
                "SELECT device_id, command, operation_json FROM device_common_commands"
            ):
                try:
                    operation = json.loads(document) if document is not None else None
                except (TypeError, ValueError, RecursionError):
                    operation = None
                overrides.setdefault(device_id, {})[command] = operation
            health_rows = connection.execute(
                "SELECT device_id, health_json, updated_at, last_disconnect_reason, last_disconnect_at FROM device_health"
            ).fetchall()
        health_by_device = {}
        for row in health_rows:
            health_by_device[row[0]] = self._public_health(row)
        for device in devices:
            device_id = device["device"]
            group = membership.get(device_id)
            device["group_id"] = group[0] if group else None
            device["group_name"] = group[1] if group else None
            defaults = device.get("schema", {}).get("common_commands", {})
            defaults = defaults if isinstance(defaults, dict) else {}
            custom = overrides.get(device_id, {})
            effective = dict(defaults)
            effective.update(custom)
            device["common_commands"] = effective
            device["common_command_defaults"] = defaults
            device["common_command_overrides"] = custom
            device["common_command_sources"] = {
                command: ("disabled" if custom[command] is None else "custom") if command in custom
                else "schema" if command in defaults else "none"
                for command in SUPPORTED_COMMON_COMMANDS
            }
            device["health"] = health_by_device.get(device_id, {})
        return devices

    def _known_device(self, device_id):
        if not isinstance(device_id, str):
            raise ValueError("Не указан ID устройства.")
        metadata = self.gateway.load_device_metadata(device_id)
        if metadata is None or metadata.get("trust_policy") in {"deny", "forgotten"}:
            raise ValueError("Устройство не найдено или недоступно.")
        return metadata

    def save_group(self, group_id, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Название группы должно содержать от 1 до 60 символов.")
        name = name.strip()
        if any(ord(character) < 32 for character in name):
            raise ValueError("В названии группы недопустимы управляющие символы.")
        if group_id is not None and (not isinstance(group_id, str) or not GROUP_ID_PATTERN.fullmatch(group_id)):
            raise ValueError("Некорректный ID группы.")
        timestamp = self.gateway.current_time()
        with self.gateway.database_connection() as connection:
            existing = connection.execute("SELECT group_id, name FROM device_groups").fetchall()
            if any(old_id != group_id and old_name.casefold() == name.casefold() for old_id, old_name in existing):
                raise ValueError("Группа с таким названием уже существует.")
            if group_id is None:
                if len(existing) >= 100:
                    raise ValueError("Можно создать не более 100 групп.")
                group_id = uuid.uuid4().hex
                connection.execute("INSERT INTO device_groups VALUES (?, ?, ?, ?)", (group_id, name, timestamp, timestamp))
            elif not any(old_id == group_id for old_id, _ in existing):
                raise ValueError("Группа не найдена.")
            else:
                connection.execute("UPDATE device_groups SET name = ?, updated_at = ? WHERE group_id = ?", (name, timestamp, group_id))
        return group_id

    def delete_group(self, group_id):
        if not isinstance(group_id, str):
            raise ValueError("Не указан ID группы.")
        with self.gateway.database_connection() as connection:
            cursor = connection.execute("DELETE FROM device_groups WHERE group_id = ?", (group_id,))
            if not cursor.rowcount:
                raise ValueError("Группа не найдена.")
            connection.execute("DELETE FROM device_group_membership WHERE group_id = ?", (group_id,))

    def assign_group(self, device_id, group_id):
        self._known_device(device_id)
        if group_id is not None and not isinstance(group_id, str):
            raise ValueError("Некорректный ID группы.")
        with self.gateway.database_connection() as connection:
            if group_id is None:
                connection.execute("DELETE FROM device_group_membership WHERE device_id = ?", (device_id,))
            else:
                if connection.execute("SELECT 1 FROM device_groups WHERE group_id = ?", (group_id,)).fetchone() is None:
                    raise ValueError("Группа не найдена.")
                connection.execute("INSERT INTO device_group_membership VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET group_id = excluded.group_id", (device_id, group_id))

    def save_common_command(self, device_id, command, operation, use_schema=False):
        metadata = self._known_device(device_id)
        if not isinstance(command, str) or command not in SUPPORTED_COMMON_COMMANDS:
            raise ValueError("Общая команда не поддерживается.")
        if type(use_schema) is not bool:
            raise ValueError("Некорректный выбор источника команды.")
        if not use_schema and operation is not None:
            error = operation_error(self.gateway, metadata["schema"], operation)
            if error:
                raise ValueError(error)
            if len(json.dumps(operation, ensure_ascii=False).encode("utf-8")) > 8192:
                raise ValueError("Назначение общей команды слишком большое.")
        with self.gateway.database_connection() as connection:
            if use_schema:
                connection.execute("DELETE FROM device_common_commands WHERE device_id = ? AND command = ?", (device_id, command))
            else:
                connection.execute("""
                    INSERT INTO device_common_commands VALUES (?, ?, ?, ?)
                    ON CONFLICT(device_id, command) DO UPDATE SET
                        operation_json = excluded.operation_json, updated_at = excluded.updated_at
                """, (device_id, command, json.dumps(operation, ensure_ascii=False) if operation is not None else None, self.gateway.current_time()))

    async def publish(self, websocket=None):
        message = {"type": "device_groups", "groups": self.groups()}
        if websocket is None:
            await self.gateway.broadcast(message)
        else:
            await self.gateway.send_json(websocket, message)

    async def shutdown(self):
        tasks = list(self.bulk_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.bulk_busy_devices.clear()

    async def _execute_one(self, device, operation, expected_socket, semaphore):
        gateway = self.gateway
        device_id = device["device"]
        async with semaphore:
            if gateway.devices.get(device_id) is not expected_socket or gateway.database_restore_in_progress:
                return {"device": device_id, "status": "skipped", "message": "Соединение устройства изменилось или Gateway восстанавливается."}
            if device_id in gateway.pending_device_schemas or gateway.device_configuration_is_pending(device_id):
                return {"device": device_id, "status": "skipped", "message": "Ожидается подтверждение Schema или конфигурации."}
            metadata = gateway.device_metadata.get(device_id) or gateway.load_device_metadata(device_id)
            error = operation_error(gateway, metadata.get("schema", {}) if metadata else {}, operation)
            if error:
                return {"device": device_id, "status": "skipped", "message": error}
            command_id = f"bulk-{uuid.uuid4().hex}"
            try:
                if operation["kind"] == "parameter":
                    future = await gateway.dispatch_device_command(device_id, operation["name"], operation["value"], requested_command_id=command_id, wait_for_result=True, transient=True)
                else:
                    future = await gateway.dispatch_device_action(device_id, operation["name"], operation["arguments"], requested_command_id=command_id, wait_for_result=True, transient=True)
                if future is False:
                    return {"device": device_id, "status": "failed", "message": "Команда отклонена: устройство занято или недоступно."}
                result = await asyncio.wait_for(asyncio.shield(future), gateway.COMMAND_TIMEOUT_SECONDS * (gateway.COMMAND_RETRY_LIMIT + 2))
                success = result.get("status") == "succeeded"
                return {"device": device_id, "status": "succeeded" if success else "failed", "message": "Выполнено" if success else result.get("error") or "Команда не подтверждена."}
            except asyncio.TimeoutError:
                return {"device": device_id, "status": "failed", "message": "Истекло время подтверждения. Фактический результат неизвестен."}
            except Exception as error:
                return {"device": device_id, "status": "failed", "message": str(error)[:240]}
            finally:
                pending = gateway.remove_pending_command(command_id)
                if pending is not None:
                    await gateway.send_pending_command_result(pending, "unavailable", None, "Групповая команда завершена без подтверждения; отложенный повтор отменён.")

    async def execute_common(self, websocket, message):
        if len(self.bulk_tasks) >= 16:
            raise ValueError("Слишком много общих команд выполняется одновременно. Дождитесь результата.")
        command = message.get("command")
        group_id = message.get("group_id")
        request_id = message.get("request_id")
        if not isinstance(command, str) or command not in SUPPORTED_COMMON_COMMANDS:
            raise ValueError("Общая команда не поддерживается.")
        if group_id is not None and (not isinstance(group_id, str) or not any(group["group_id"] == group_id for group in self.groups())):
            raise ValueError("Группа не найдена.")
        devices = self.gateway.get_all_devices()
        if group_id is not None:
            devices = [device for device in devices if device.get("group_id") == group_id]
        reserved = set()
        jobs = []
        semaphore = asyncio.Semaphore(6)
        for device in devices:
            device_id = device["device"]
            operation = device.get("common_commands", {}).get(command)
            if device_id in self.bulk_busy_devices:
                jobs.append({"device": device_id, "status": "skipped", "message": "Общая команда для устройства уже выполняется."})
            elif operation is None:
                jobs.append({"device": device_id, "status": "skipped", "message": "Общая команда не назначена."})
            elif device_id not in self.gateway.devices:
                jobs.append({"device": device_id, "status": "skipped", "message": "Устройство было отключено при запуске команды."})
            else:
                self.bulk_busy_devices.add(device_id)
                reserved.add(device_id)
                jobs.append((device, operation, self.gateway.devices[device_id]))

        async def execute():
            try:
                async def result_for(job):
                    if isinstance(job, dict):
                        return job
                    try:
                        return await self._execute_one(*job, semaphore)
                    except Exception as error:
                        return {"device": job[0]["device"], "status": "failed", "message": str(error)[:240]}
                results = await asyncio.gather(*(result_for(job) for job in jobs))
                counts = {status: sum(item["status"] == status for item in results) for status in ("succeeded", "failed", "skipped")}
                await self.gateway.send_json(websocket, {
                    "type": "common_command_result", "request_id": request_id,
                    "command": command, "group_id": group_id,
                    "success": bool(results) and counts["succeeded"] > 0 and counts["failed"] == 0,
                    "results": results, "counts": counts,
                })
            except asyncio.CancelledError:
                results = [job if isinstance(job, dict) else {
                    "device": job[0]["device"], "status": "failed",
                    "message": "Общее управление прервано. Проверьте фактическое состояние устройства.",
                } for job in jobs]
                try:
                    await self.gateway.send_json(websocket, {
                        "type": "common_command_result", "request_id": request_id,
                        "command": command, "group_id": group_id,
                        "success": False, "cancelled": True, "results": results,
                        "message": "Общее управление остановлено. Некоторые команды могли быть выполнены.",
                    })
                except Exception:
                    pass
                raise
            finally:
                self.bulk_busy_devices.difference_update(reserved)

        task = asyncio.create_task(execute())
        self.bulk_tasks.add(task)
        def complete(done):
            self.bulk_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                print(f"[GROUP] Result could not be delivered: {done.exception()}")
        task.add_done_callback(complete)
        # Enter execute() before returning, so immediate shutdown still runs
        # its cancellation response and reservation cleanup.
        await asyncio.sleep(0)

    async def handle_browser_message(self, websocket, message):
        action = message.get("action")
        if not isinstance(action, str) or action not in {
            "get_device_groups", "save_device_group", "delete_device_group", "set_device_group",
            "save_common_command", "execute_common_command", "get_gateway_settings", "set_gateway_settings",
        }:
            return False
        response_type = "group_update_result"
        if action == "save_common_command":
            response_type = "common_command_save_result"
        elif action == "execute_common_command":
            response_type = "common_command_result"
        elif action in {"get_gateway_settings", "set_gateway_settings"}:
            response_type = "gateway_settings_result"
        try:
            if action == "get_device_groups":
                await self.publish(websocket)
                return True
            if action == "get_gateway_settings":
                await self.gateway.send_json(websocket, {"type": "gateway_settings", **self.settings()})
                return True
            if action == "execute_common_command":
                await self.execute_common(websocket, message)
                return True
            if action == "save_device_group":
                group_id = self.save_group(message.get("group_id"), message.get("name"))
            elif action == "delete_device_group":
                self.delete_group(message.get("group_id"))
                group_id = message.get("group_id")
            elif action == "set_device_group":
                self.assign_group(message.get("device"), message.get("group_id"))
                group_id = message.get("group_id")
            elif action == "save_common_command":
                if "operation" not in message and message.get("use_schema") is not True:
                    raise ValueError("Не выбрано назначение общей команды.")
                self.save_common_command(message.get("device"), message.get("command"), message.get("operation"), message.get("use_schema", False))
                group_id = None
            else:
                days = message.get("event_retention_days")
                if type(days) is not int or not 0 <= days <= MAX_EVENT_RETENTION_DAYS:
                    raise ValueError("Срок хранения должен быть целым числом от 0 до 3650 дней.")
                with self.gateway.database_connection() as connection:
                    connection.execute("INSERT INTO gateway_settings VALUES (?, ?) ON CONFLICT(setting_key) DO UPDATE SET value_json = excluded.value_json", ("event_retention_days", json.dumps(days)))
                self.cleanup_events()
                await self.gateway.broadcast({"type": "gateway_settings", **self.settings()})
                await self.gateway.send_event_history()
                await self.gateway.send_database_stats()
                group_id = None
            await self.gateway.send_json(websocket, {
                "type": response_type, "success": True, "message": "Сохранено.",
                "device": message.get("device"), "group_id": group_id,
                "request_id": message.get("request_id"),
            })
            if action != "set_gateway_settings":
                await self.publish()
                await self.gateway.send_device_list()
        except (ValueError, TypeError, sqlite3.Error) as error:
            await self.gateway.send_json(websocket, {
                "type": response_type, "success": False, "message": str(error),
                "device": message.get("device"), "request_id": message.get("request_id"),
                **({"results": []} if action == "execute_common_command" else {}),
            })
        return True
