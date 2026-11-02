"""Device/browser WebSocket protocol, broadcasts and download routes."""

import asyncio
import json
import re
import sqlite3
import uuid
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.exceptions import ConnectionClosed
from device_configuration import DRIVERS, validate_device_configuration


class ProtocolMixin:
    def handle_http_request(self, connection, request):
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
            schema_path = self.DEVICE_CONFIGURATION_EXPORT_DIR / f"{device_id}.json"
            try:
                if not schema_path.is_file():
                    status_code, reason, body = 404, "Not Found", b"Schema export not found"
                elif schema_path.stat().st_size > self.DEVICE_CONFIGURATION_EXPORT_DOWNLOAD_LIMIT:
                    status_code, reason, body = 413, "Content Too Large", b"Schema export is too large"
                else:
                    body = schema_path.read_bytes()
                    status_code, reason = 200, "OK"
            except OSError:
                status_code, reason, body = 404, "Not Found", b"Schema export not found"
        else:
            backup_path = self.DATABASE_BACKUP_DIR / backup_match.group(1)
            try:
                if not backup_path.is_file():
                    status_code, reason, body = 404, "Not Found", b"Backup not found"
                elif backup_path.stat().st_size > self.DATABASE_BACKUP_DOWNLOAD_LIMIT:
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


    async def send_device_access_state(self, websocket=None):
        messages = (
            {
                "type": "device_access_requests",
                "requests": self.get_pending_device_requests()
            },
            {
                "type": "blocked_devices",
                "devices": self.get_blocked_devices()
            }
        )
        targets = [websocket] if websocket is not None else list(self.browser_clients)
        for target in targets:
            for message in messages:
                try:
                    await self.send_json(target, message)
                except Exception:
                    if target in self.browser_clients:
                        self.browser_clients.discard(target)


    async def send_json(self, websocket, data):
        await websocket.send(
            json.dumps(data, ensure_ascii=False)
        )


    async def broadcast(self, data):
        disconnected_clients = set()

        for websocket in list(self.browser_clients):
            try:
                await self.send_json(websocket, data)
            except Exception:
                disconnected_clients.add(websocket)

        self.browser_clients.difference_update(disconnected_clients)


    async def send_device_list(self, websocket=None):
        message = {
            "type": "device_list",
            "devices": self.get_all_devices()
        }

        if websocket is not None:
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)


    async def send_parameter_ui_preferences(self, websocket=None):
        message = {
            "type": "parameter_ui_preferences",
            "preferences": self.get_parameter_ui_preferences()
        }
        if websocket is not None:
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)


    async def send_database_stats(self, websocket=None):
        message = {
            "type": "database_stats",
            **self.get_database_stats()
        }
        if websocket is not None:
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)


    async def send_database_backups(self, websocket=None):
        message = {
            "type": "database_backups",
            "backups": self.list_database_backups(),
            "retention_limit": self.DATABASE_BACKUP_LIMIT
        }
        if websocket is not None:
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)


    async def send_event_history(
        self,
        websocket=None,
        device_id=None,
        before_id=None,
        request_id=None,
        search=""
    ):
        events, has_more, next_before_id, matching_count = self.get_event_page(
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
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)

        await self.send_database_stats(websocket)


    async def handle_browser_message(self, websocket, message):

        action = message.get("action")
        if action == "run_automation":
            request_id = message.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 80):
                success, text = False, "Некорректный ID запроса."
            else:
                success, text = self.run_manual_automation(message.get("automation_id"))
            await self.send_json(websocket, {
                "type": "automation_run_result", "automation_id": message.get("automation_id"),
                "request_id": request_id, "success": success, "message": text,
            })
            return
        if self.database_restore_in_progress:
            await self.send_json(websocket, {
                "type": "error",
                "code": "database_restoring",
                "request_id": message.get("request_id") if isinstance(message.get("request_id"), str) and len(message["request_id"]) <= 128 else None,
                "action": action if isinstance(action, str) and len(action) <= 80 else None,
                "message": "Gateway database is being restored. Try again shortly."
            })
            return

        if await self.management.handle_browser_message(websocket, message):
            return

        if await self.history.handle_browser_message(websocket, message):
            return

        if action == "check_automation":
            automation_id = message.get("automation_id")
            success = True
            if "config" in message:
                config = message.get("config")
            elif isinstance(automation_id, str):
                with self.database_connection() as conn:
                    row = conn.execute(
                        "SELECT config_json FROM automations WHERE automation_id = ?",
                        (automation_id,)
                    ).fetchone()
                try:
                    config = json.loads(row[0]) if row else None
                except (TypeError, json.JSONDecodeError):
                    config = None
                success = row is not None
            else:
                config = None
                success = False
            issues, ready = self.automation_diagnostics(config)
            if not success:
                issues = [{
                    "severity": "error", "code": "automation_missing",
                    "message": "Сценарий не найден.", "message_en": "Scenario was not found."
                }]
                ready = False
            await self.send_json(websocket, {
                "type": "automation_diagnostics", "request_id": message.get("request_id"),
                "automation_id": automation_id, "success": success,
                "issues": issues, "ready": ready, "checked_at": self.current_time()
            })
            return

        if action == "get_automations":
            await self.send_json(websocket, {
                "type": "automation_list", "automations": self.get_automations()
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
                with self.database_connection() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM automations WHERE automation_id = ?",
                        (automation_id,)
                    ).fetchone() is not None
            await self.send_json(websocket, {
                "type": "automation_run_history",
                "automation_id": automation_id,
                "success": exists,
                "runs": self.get_automation_run_history(automation_id) if exists else [],
                "run_count": self.get_automation_run_count(automation_id) if exists else 0,
                "completed_count": self.get_automation_run_count(automation_id, completed_only=True) if exists else 0,
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
                with self.database_connection() as conn:
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
            await self.send_json(websocket, {
                "type": "automation_run_delete_result",
                "success": success,
                "automation_id": automation_id,
                "run_id": run_id,
                "message": result_message
            })
            if success:
                await self.broadcast({
                    "type": "automation_run_history",
                    "automation_id": automation_id,
                    "success": True,
                    "runs": self.get_automation_run_history(automation_id),
                    "run_count": self.get_automation_run_count(automation_id),
                    "completed_count": self.get_automation_run_count(automation_id, completed_only=True)
                })
                await self.send_database_stats()
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
                with self.database_connection() as conn:
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
            await self.send_json(websocket, {
                "type": "automation_run_clear_result",
                "success": success,
                "automation_id": automation_id,
                "deleted_count": deleted_count,
                "message": result_message
            })
            if success:
                await self.broadcast({
                    "type": "automation_run_history",
                    "automation_id": automation_id,
                    "success": True,
                    "runs": self.get_automation_run_history(automation_id),
                    "run_count": self.get_automation_run_count(automation_id),
                    "completed_count": self.get_automation_run_count(automation_id, completed_only=True)
                })
                await self.send_database_stats()
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
                success, message_text = self.save_automation(
                    automation_id, config, message.get("enabled", True) is True
                )
            await self.send_json(websocket, {
                "type": "automation_save_result", "success": success,
                "automation_id": automation_id, "message": message_text
            })
            if success:
                self.cancel_pending_automation_trigger(automation_id)
                await self.publish_automation_list()
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
                with self.database_connection() as conn:
                    cursor = conn.execute(
                        "UPDATE automations SET enabled = ?, updated_at = ? WHERE automation_id = ?",
                        (int(enabled), self.current_time(), automation_id)
                    )
                    success = cursor.rowcount == 1
            await self.send_json(websocket, {
                "type": "automation_update_result", "success": success,
                "automation_id": automation_id,
                "message": "Сценарий обновлён." if success else "Сценарий не найден."
            })
            if success:
                if not enabled:
                    self.cancel_pending_automation_trigger(automation_id)
                await self.publish_automation_list()
            return

        if action == "stop_automation":
            automation_id = message.get("automation_id")
            task = self.automation_tasks.get(automation_id) if isinstance(automation_id, str) else None
            success = task is not None and not task.done()
            if success:
                self.automation_stop_requests.add(automation_id)
            await self.send_json(websocket, {
                "type": "automation_stop_result", "success": success,
                "automation_id": automation_id,
                "message": (
                    "Остановка запрошена; текущее действие завершится первым."
                    if success else "Сценарий сейчас не выполняется."
                )
            })
            if success:
                await self.publish_automation_list()
            return

        if action == "delete_automation":
            automation_id = message.get("automation_id")
            success = False
            active_task = self.automation_tasks.get(automation_id) if isinstance(automation_id, str) else None
            if active_task is not None and not active_task.done():
                success = False
            elif isinstance(automation_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id):
                with self.database_connection() as conn:
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
            await self.send_json(websocket, {
                "type": "automation_delete_result", "success": success,
                "automation_id": automation_id,
                "message": (
                    "Сценарий удалён." if success else
                    "Сначала остановите выполняющийся сценарий." if active_task is not None and not active_task.done() else
                    "Сценарий не найден."
                )
            })
            if success:
                self.cancel_pending_automation_trigger(automation_id)
                await self.publish_automation_list()
            return

        if action == "device_access_decision":
            device_id = message.get("device")
            request = self.pending_device_connections.get(device_id)
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
                await self.send_json(websocket, {
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
                self.set_device_trust_policy(
                    device_id, "deny", request["metadata"].get("name")
                )
                self.cancel_pending_automation_triggers_for_device(device_id)
                self.cancel_configuration_apply(device_id, pending=True)
                active_socket = self.devices.pop(device_id, None)
                if active_socket is not None:
                    self.management.record_disconnect(device_id, 4003, "Device blocked by user")
                self.device_wire_ids.pop(device_id, None)
                self.last_device_heartbeat.pop(device_id, None)
                self.last_device_telemetry.pop(device_id, None)
                self.last_parameter_telemetry.pop(device_id, None)
                self.last_device_probe.pop(device_id, None)
                self.pending_device_schemas.pop(device_id, None)
                self.deferred_device_schemas.pop(device_id, None)
                await self.finish_pending_commands_for_device(
                    device_id,
                    "unavailable",
                    "Подключение устройства запрещено.",
                    retry=False
                )
                with self.database_connection() as conn:
                    conn.execute(
                        "UPDATE devices SET connected = 0 WHERE device_id = ?",
                        (device_id,)
                    )
                self.save_device_event(
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
            await self.send_json(websocket, {
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
            await self.send_device_access_state()
            if decision == "deny" and remember:
                await self.send_device_list()
                await self.send_event_history()
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
                await self.send_json(websocket, {
                    "type": "device_forget_result",
                    "success": False,
                    "device": device_id,
                    "message": "Некорректный идентификатор устройства."
                })
                return

            with self.database_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM devices WHERE device_id = ?",
                    (device_id,)
                ).fetchone()
            if exists is None:
                await self.send_json(websocket, {
                    "type": "device_forget_result",
                    "success": False,
                    "device": device_id,
                    "message": "Устройство не найдено."
                })
                return

            self.set_device_trust_policy(device_id, "forgotten")
            self.cancel_pending_automation_triggers_for_device(device_id)
            self.cancel_configuration_apply(device_id, pending=True)
            active_socket = self.devices.pop(device_id, None)
            if active_socket is not None:
                self.management.record_disconnect(device_id, 4002, "Device forgotten by user")
            self.device_wire_ids.pop(device_id, None)
            self.last_device_heartbeat.pop(device_id, None)
            self.last_device_telemetry.pop(device_id, None)
            self.last_parameter_telemetry.pop(device_id, None)
            self.last_device_probe.pop(device_id, None)
            self.pending_device_schemas.pop(device_id, None)
            self.deferred_device_schemas.pop(device_id, None)
            await self.finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Доступ к устройству отозван.",
                retry=False
            )
            with self.database_connection() as conn:
                conn.execute(
                    "UPDATE devices SET connected = 0 WHERE device_id = ?",
                    (device_id,)
                )
            self.save_device_event(
                device_id,
                "connection",
                "Устройство забыто; при следующем подключении потребуется подтверждение"
            )
            if active_socket is not None:
                try:
                    await active_socket.close(code=4002, reason="Device forgotten")
                except Exception:
                    pass
            await self.send_json(websocket, {
                "type": "device_forget_result",
                "success": True,
                "device": device_id,
                "message": "Устройство отключено. При следующем подключении Gateway снова запросит разрешение."
            })
            await self.send_device_list()
            await self.send_event_history()
            await self.send_device_access_state()
            return

        if action == "update_device_identity":
            device_id = message.get("device")
            new_device_id = message.get("new_device_id")
            display_name = message.get("display_name")
            success, message_text = self.update_device_identity(
                device_id, new_device_id, display_name
            )
            await self.send_json(websocket, {
                "type": "device_identity_result",
                "success": success,
                "device": device_id,
                "new_device_id": new_device_id if success else device_id,
                "message": message_text
            })
            if success:
                await self.send_device_list()
                await self.send_event_history()
                await self.send_command_queue_state()
                await self.send_parameter_ui_preferences()
                await self.send_database_stats()
                await self.send_json(websocket, {
                    "type": "automation_list",
                    "automations": self.get_automations()
                })
            return

        if action == "clear_device_block":
            device_id = message.get("device")
            if not isinstance(device_id, str) or self.get_device_trust_policy(device_id) != "deny":
                await self.send_json(websocket, {
                    "type": "blocked_device_result",
                    "success": False,
                    "device": device_id,
                    "message": "Для устройства не найдена постоянная блокировка."
                })
                return
            self.set_device_trust_policy(device_id, "forgotten")
            self.save_device_event(
                device_id, "connection", "Постоянная блокировка подключения снята"
            )
            await self.send_json(websocket, {
                "type": "blocked_device_result",
                "success": True,
                "device": device_id,
                "message": "Блокировка снята. При следующем подключении появится запрос разрешения."
            })
            await self.send_device_list()
            await self.send_device_access_state()
            await self.send_event_history()
            return

        if action == "get_schema_history":
            device_id = message.get("device")
            request_id = message.get("request_id")
            if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
                await self.send_json(websocket, {
                    "type": "schema_history",
                    "device": device_id,
                    "request_id": request_id,
                    "history": []
                })
                return
            await self.send_json(websocket, {
                "type": "schema_history",
                "device": device_id,
                "request_id": request_id,
                "history": self.get_schema_history(device_id)
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
                success, message_text = self.restore_device_schema(device_id, history_id)
            await self.send_json(websocket, {
                "type": "schema_restore_result",
                "success": success,
                "device": device_id,
                "history_id": history_id,
                "message": message_text
            })
            if success:
                await self.send_device_list()
                await self.send_event_history()
                await self.send_parameter_ui_preferences()
                await self.send_database_stats()
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
                success = self.delete_schema_history_item(device_id, history_id)
                message_text = (
                    "Снимок Schema удалён из истории. Текущая Schema устройства не изменена."
                    if success
                    else "Снимок Schema не найден; история не изменена."
                )
            if success:
                self.save_device_event(
                    device_id,
                    "schema",
                    f"Удалён снимок Schema из истории (ID {history_id})"
                )
            await self.send_json(websocket, {
                "type": "schema_history_delete_result",
                "success": success,
                "device": device_id,
                "history_id": history_id,
                "message": message_text
            })
            if success:
                await self.send_event_history()
            return

        if action == "set_parameter_ui_preference":
            device_id = message.get("device")
            parameter_id = message.get("parameter")
            control = message.get("control")
            success, message_text = self.save_parameter_ui_preference(
                device_id, parameter_id, control
            )
            await self.send_json(websocket, {
                "type": "parameter_ui_preference_result",
                "success": success,
                "device": device_id,
                "parameter": parameter_id,
                "control": control,
                "message": message_text
            })
            if success:
                await self.send_parameter_ui_preferences()
                await self.send_database_stats()
            return

        if action == "get_parameter_ui_preferences":
            await self.send_parameter_ui_preferences(websocket)
            return

        if action == "get_devices":
            await self.send_device_list(websocket)
            return

        if action == "validate_device_configuration":
            device_id = message.get("device")
            normalized, schema, preferences, errors = self.validate_device_configuration_request(
                device_id, message.get("configuration"), message.get("ui_preferences")
            )
            response = {
                "type": "device_configuration_validation", "device": device_id,
                "request_id": message.get("request_id"), "success": not errors,
                "errors": errors
            }
            if not errors:
                response["schema"] = schema
                response["summary"] = {
                    "component_count": len(normalized["components"]),
                    "parameter_count": len(schema["parameters"]),
                    "writable_parameter_count": sum(
                        definition["access"] == "read_write" for definition in schema["parameters"].values()
                    ),
                    "has_identify": "identify" in schema["actions"],
                    "configuration_bytes": len(json.dumps(
                        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8"))
                }
            await self.send_json(websocket, response)
            return

        if action == "retry_device_configuration":
            device_id = message.get("device")
            metadata = self.automation_device_metadata(device_id)
            record = self.load_device_configuration(device_id) if metadata is not None else None
            success = False
            status = None
            result_message = "Сохранённая аппаратная конфигурация устройства не найдена."
            if metadata is not None and metadata.get("device_profile") == "universal" and record is not None:
                if device_id in self.devices:
                    await self.send_device_configuration(device_id)
                else:
                    self.cancel_configuration_apply(device_id)
                    self.update_device_configuration_status(device_id, "pending")
                status = self.load_device_configuration(device_id)["status"]
                self.write_device_configuration_export(device_id)
                success = True
                result_message = (
                    "Сохранённая конфигурация повторно отправлена устройству."
                    if status in {"applying", "applied"} else "Конфигурация будет отправлена при подключении устройства."
                )
            await self.send_json(websocket, {
                "type": "device_configuration_retry_result", "device": device_id,
                "request_id": message.get("request_id"), "success": success,
                "status": status, "config_version": record["config_version"] if record else None,
                "message": result_message
            })
            if success:
                await self.send_device_list()
            return

        if action == "save_device_configuration":
            device_id = message.get("device")
            configuration = message.get("configuration")
            requested_ui_preferences = message.get("ui_preferences")
            metadata = (
                self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
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
                    normalized_ui_preferences, validation_error = self.validate_parameter_ui_preferences(
                        requested_ui_preferences, generated_schema
                    )
                if validation_error:
                    result_message = validation_error
                else:
                    try:
                        saved = self.save_device_configuration(
                            device_id, normalized, normalized_ui_preferences
                        )
                        version = saved["config_version"]
                        status = "pending"
                        if saved["unchanged"]:
                            self.update_device_configuration_status(device_id, status)
                        if device_id in self.devices:
                            await self.send_device_configuration(device_id)
                            status = self.load_device_configuration(device_id)["status"]
                        export_written = self.write_device_configuration_export(device_id)
                        success = True
                        result_message = (
                            "Конфигурация сохранена и подтверждена устройством."
                            if status == "applied"
                            else
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
                            self.save_device_event(
                                device_id,
                                "configuration",
                                f"Сохранена аппаратная конфигурация версии {version}; статус: {status}."
                            )
                    except (OSError, sqlite3.Error, ValueError) as error:
                        result_message = f"Не удалось сохранить конфигурацию: {error}"
            elif metadata is not None and metadata.get("device_profile") == "universal":
                result_message = "Для этой версии конструктора поддерживается профиль ESP32-S3 DevKitC-1."

            await self.send_json(websocket, {
                "type": "device_configuration_save_result",
                "request_id": message.get("request_id"),
                "success": success,
                "device": device_id,
                "config_version": version,
                "status": status,
                "message": result_message
            })
            if success:
                await self.send_device_list()
                await self.send_parameter_ui_preferences()
                await self.send_event_history()
                await self.send_database_stats()
            return

        if action == "get_database_stats":
            await self.send_database_stats(websocket)
            return

        if action == "get_database_backups":
            await self.send_database_backups(websocket)
            return

        if action == "create_database_backup":
            try:
                backup = self.create_database_backup()
                await self.send_json(websocket, {
                    "type": "database_backup_result",
                    "success": True,
                    "backup": backup,
                    "message": "Backup created."
                })
                await self.send_database_backups()
            except Exception as error:
                print(f"[BACKUP] Could not create a database backup: {error}")
                await self.send_json(websocket, {
                    "type": "database_backup_result",
                    "success": False,
                    "message": f"Could not create a backup: {error}"
                })
            return

        if action == "restore_database_backup":
            success, message_text = await self.restore_database_backup(
                message.get("backup_id")
            )
            await self.send_json(websocket, {
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
                await self.send_json(websocket, {
                    "type": "error",
                    "request_id": request_id,
                    "message": "Поисковый запрос должен содержать не более 120 символов."
                })
                return
            search = search.strip()

            if device_id == "all":
                device_id = None
            elif not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
                await self.send_json(websocket, {
                    "type": "error",
                    "request_id": request_id,
                    "message": "Некорректный фильтр устройства истории."
                })
                return

            if before_id is not None and (type(before_id) is not int or before_id < 1):
                await self.send_json(websocket, {
                    "type": "error",
                    "request_id": request_id,
                    "message": "Некорректный курсор истории."
                })
                return

            if request_id is not None and (
                not isinstance(request_id, str)
                or not 1 <= len(request_id) <= 80
            ):
                await self.send_json(websocket, {
                    "type": "error",
                    "message": "Некорректный ID запроса истории."
                })
                return

            await self.send_event_history(
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
                self.pending_device_schemas.get(device_id)
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
                await self.send_json(websocket, {
                    "type": "schema_decision_result",
                    "success": False,
                    "device": device_id,
                    "proposal_id": proposal_id,
                    "message": "Предложение Schema уже изменилось или недоступно."
                })
                await self.send_device_list(websocket)
                return

            version = proposal.get("schema_version")
            if decision == "accept":
                accepted_metadata = dict(proposal["metadata"])
                self.save_device_metadata(device_id, accepted_metadata)
                self.pending_device_schemas.pop(device_id, None)
                self.deferred_device_schemas.pop(device_id, None)
                self.save_device_event(
                    device_id,
                    "schema",
                    f"Принята версия Schema {version or 'без номера'}"
                )
                message_text = f"Новая Schema устройства {device_id} принята."
            else:
                self.pending_device_schemas.pop(device_id, None)
                self.deferred_device_schemas[device_id] = proposal["schema"]
                self.save_device_event(
                    device_id,
                    "schema",
                    f"Оставлена текущая Schema; предложение версии {version or 'без номера'} отклонено"
                )
                message_text = f"Текущая Schema устройства {device_id} оставлена. При следующем подключении Gateway снова предложит это изменение, если Schema всё ещё отличается."

            await self.send_json(websocket, {
                "type": "schema_decision_result",
                "success": True,
                "device": device_id,
                "proposal_id": proposal_id,
                "message": message_text
            })
            await self.send_device_list()
            await self.send_parameter_ui_preferences()
            await self.send_database_stats()
            await self.send_event_history()
            await self.send_command_queue_state()
            should_fire_connected = device_id in self.deferred_connection_automations
            if should_fire_connected and self.device_metadata.get(device_id, {}).get("device_profile") == "universal":
                await self.send_device_configuration(device_id)
            else:
                await self.deliver_queued_commands(device_id)
                self.deferred_connection_automations.discard(device_id)
                if should_fire_connected and self.device_is_connected(device_id):
                    self.fire_automation_event("device_connected", device_id)
            return

        if action == "command":
            await self.dispatch_device_command(
                message.get("device"),
                message.get("parameter"),
                message.get("value"),
                browser=websocket,
                requested_command_id=message.get("command_id")
            )
            return

        if action == "invoke_action":
            await self.dispatch_device_action(
                message.get("device"),
                message.get("action_id"),
                arguments=message.get("arguments", {}),
                browser=websocket,
                requested_command_id=message.get("command_id")
            )
            return

        await self.send_json(websocket, {
            "type": "error",
            "code": "unsupported_action",
            "request_id": message.get("request_id") if isinstance(message.get("request_id"), str) and len(message["request_id"]) <= 128 else None,
            "action": action if isinstance(action, str) and len(action) <= 80 else None,
            "message": f"Неизвестное действие: {action}"
        })


    async def handle_client(self, websocket):
        client_type = None
        device_id = None
        wire_device_id = None
        connection_started_at = asyncio.get_running_loop().time()
        close_error = None

        remote = getattr(websocket, "remote_address", None)
        print(f"[ПОДКЛЮЧЕНИЕ] Новое соединение: {remote}")

        try:
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except (ValueError, RecursionError):
                    await self.send_json(websocket, {
                        "type": "error",
                        "message": "Получен некорректный JSON."
                    })
                    continue

                if not isinstance(message, dict):
                    await self.send_json(websocket, {
                        "type": "error",
                        "message": "Корневое значение JSON должно быть объектом."
                    })
                    continue

                # A second connection with the same device ID replaces the first.
                # Do not accept queued messages from the superseded socket.
                if client_type == "device" and self.devices.get(device_id) is not websocket:
                    await websocket.close(
                        code=4001,
                        reason="Replaced by a newer connection"
                    )
                    break

                message_type = message.get("type")
                if client_type == "device":
                    self.last_device_heartbeat[device_id] = asyncio.get_running_loop().time()

                if self.database_restore_in_progress and client_type != "browser":
                    await websocket.close(
                        code=1012,
                        reason="Gateway database is being restored"
                    )
                    break

                # -------------------------
                # Регистрация браузера
                # -------------------------

                if message_type == "browser_hello" and client_type == "device":
                    await self.send_json(websocket, {
                        "type": "error",
                        "message": "Тип подключения устройства нельзя изменить."
                    })
                    continue

                if message_type == "browser_hello":
                    client_type = "browser"
                    self.browser_clients.add(websocket)

                    print("[БРАУЗЕР] Подключён")

                    await self.send_json(websocket, {
                        "type": "gateway_capabilities",
                        "protocol_version": 1,
                        "features": ["parameter_history", "automation_progress"]
                    })
                    await self.send_device_list(websocket)
                    await self.send_parameter_ui_preferences(websocket)
                    await self.send_device_access_state(websocket)
                    await self.send_command_queue_state(websocket)
                    await self.send_database_stats(websocket)
                    await self.send_database_backups(websocket)
                    await self.management.publish(websocket)
                    await self.send_json(websocket, {
                        "type": "gateway_settings", **self.management.settings()
                    })
                    await self.send_json(websocket, {
                        "type": "history_settings", **self.history.settings()
                    })
                    continue

                if client_type == "browser":
                    if message_type == "browser_message":
                        await self.handle_browser_message(websocket, message)
                    else:
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Неизвестное сообщение браузера."
                        })

                    continue

                # -------------------------
                # Регистрация ESP32
                # -------------------------

                if message_type == "hello":
                    if client_type is not None:
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Устройство уже зарегистрировано в этом подключении."
                        })
                        continue

                    incoming_id = message.get("device")

                    if (
                        not isinstance(incoming_id, str)
                        or not 1 <= len(incoming_id) <= 64
                    ):
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "В сообщении hello отсутствует device."
                        })
                        await websocket.close(code=4002, reason="Invalid device ID")
                        break

                    wire_device_id = incoming_id
                    device_id = self.resolve_device_identity(wire_device_id)

                    legacy_data = message.get("data", {})
                    if not isinstance(legacy_data, dict):
                        legacy_data = {}
                    schema = message.get("schema")
                    is_legacy = schema is None
                    if is_legacy:
                        schema = self.default_legacy_schema()
                    elif not self.validate_schema(schema):
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
                        await self.send_json(websocket, {
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
                        await self.send_json(websocket, {
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
                            await self.send_json(websocket, {
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

                    trust_policy = self.get_device_trust_policy(device_id)
                    if trust_policy == "deny":
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Для этого устройства сохранён запрет подключения."
                        })
                        await websocket.close(code=4003, reason="Device access denied")
                        break

                    if trust_policy != "allow":
                        if device_id in self.pending_device_connections:
                            await self.send_json(websocket, {
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
                        self.pending_device_connections[device_id] = request
                        await self.send_device_access_state()
                        approval_wait = asyncio.create_task(
                            request["decision_event"].wait()
                        )
                        closed_wait = asyncio.create_task(websocket.wait_closed())
                        done, waiting = await asyncio.wait(
                            {approval_wait, closed_wait},
                            timeout=self.DEVICE_APPROVAL_TIMEOUT_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in waiting:
                            task.cancel()
                        if waiting:
                            await asyncio.gather(*waiting, return_exceptions=True)

                        if closed_wait in done:
                            if self.pending_device_connections.get(device_id) is request:
                                self.pending_device_connections.pop(device_id, None)
                            await self.send_device_access_state()
                            break

                        if approval_wait not in done:
                            if self.pending_device_connections.get(device_id) is request:
                                self.pending_device_connections.pop(device_id, None)
                            await self.send_device_access_state()
                            try:
                                await websocket.close(
                                    code=4000, reason="Device approval timed out"
                                )
                            except Exception:
                                pass
                            break

                        if self.pending_device_connections.get(device_id) is request:
                            self.pending_device_connections.pop(device_id, None)
                        await self.send_device_access_state()
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

                    if self.database_restore_in_progress:
                        await websocket.close(
                            code=1012,
                            reason="Gateway database is being restored"
                        )
                        break

                    client_type = "device"
                    current_device_task = asyncio.current_task()
                    if current_device_task is not None:
                        self.device_handler_tasks.add(current_device_task)

                    old_connection = self.devices.get(device_id)
                    self.configuration_confirmed_devices.discard(device_id)
                    if old_connection is not None:
                        self.cancel_configuration_apply(device_id, pending=True)

                    if device_id not in self.device_states:
                        with self.database_connection() as conn:
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
                                self.device_states[device_id] = restored_state

                    self.devices[device_id] = websocket
                    self.device_wire_ids[device_id] = wire_device_id
                    self.last_device_heartbeat[device_id] = asyncio.get_running_loop().time()
                    self.last_device_telemetry[device_id] = None
                    self.last_parameter_telemetry[device_id] = {}
                    self.last_device_probe[device_id] = self.last_device_heartbeat[device_id]
                    self.fresh_device_parameters[device_id] = set()
                    previous_metadata = self.load_device_metadata(device_id)
                    if (
                        metadata.get("device_profile") == "universal"
                        and previous_metadata is not None
                        and previous_metadata.get("device_profile") == "universal"
                    ):
                        accepted = dict(metadata)
                        accepted["schema"] = previous_metadata["schema"]
                        accepted["schema_version"] = previous_metadata.get("schema_version")
                        schema_status = "preserved"
                        self.save_device_metadata(device_id, accepted)
                    else:
                        schema_status = self.offer_device_schema(
                            device_id, metadata, is_legacy=is_legacy
                        )
                    self.save_device_seen(device_id, True)
                    if schema_status == "pending":
                        print(
                            f"[Schema] Для {device_id} ожидает подтверждения "
                            f"версия {schema_version}."
                        )

                    # Логируем подключение в историю.
                    self.save_device_event(
                        device_id,
                        "connection",
                        "Подключено"
                    )

                    print(f"[ESP32] Подключено устройство: {device_id}")
                    if old_connection is not None and old_connection is not websocket:
                        self.management.record_disconnect(
                            device_id, 1000, "Connection replaced by a new session with the same device ID"
                        )
                    self.management.clear_live_health(device_id)
                    self.management.record_health(device_id, message.get("health"))

                    # Закрываем предыдущее соединение с тем же ID.
                    if (
                        old_connection is not None
                        and old_connection is not websocket
                    ):
                        await self.finish_pending_commands_for_device(
                            device_id,
                            "unavailable",
                            "Соединение устройства было заменено до получения результата."
                        )

                        try:
                            await old_connection.close()
                        except Exception:
                            pass

                    await self.send_json(websocket, {
                        "type": "hello_ack",
                        "device": device_id,
                        "schema_status": schema_status
                    })
                    await self.send_device_list()
                    await self.send_event_history()
                    if schema_status != "pending":
                        if metadata.get("device_profile") == "universal":
                            self.deferred_connection_automations.add(device_id)
                            await self.send_device_configuration(device_id, websocket)
                        else:
                            await self.deliver_queued_commands(device_id)
                            self.fire_automation_event("device_connected", device_id)
                    else:
                        self.deferred_connection_automations.add(device_id)
                    await self.send_command_queue_state()
                    continue

                if client_type == "device" and message_type == "device_config_result":
                    await self.handle_device_configuration_result(
                        device_id, wire_device_id, message
                    )
                    continue

                if client_type == "device" and message_type == "device_config_report":
                    await self.handle_device_configuration_report(
                        device_id, wire_device_id, message
                    )
                    continue

                if client_type == "device" and message_type == "schema_update":
                    schema = message.get("schema")
                    schema_version = message.get("schema_version")
                    if (
                        not self.validate_schema(schema)
                        or type(schema_version) is not int
                        or schema_version < 1
                    ):
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Некорректное обновление Schema."
                        })
                        continue

                    metadata = self.load_device_metadata(device_id) or {
                        "name": None,
                        "protocol_version": None,
                        "firmware_version": None
                    }
                    metadata = dict(metadata)
                    metadata.update({"schema": schema, "schema_version": schema_version})
                    schema_status = self.offer_device_schema(device_id, metadata)
                    if schema_status == "pending":
                        print(
                            f"[Schema] Для {device_id} ожидает подтверждения "
                            f"версия {schema_version}."
                        )
                    await self.send_device_list()
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

                    pending = self.pending_commands.get(command_id)

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
                        definition = self.device_metadata.get(
                            device_id, {"schema": self.default_legacy_schema()}
                        )["schema"].get("parameters", {}).get(parameter)
                        if (
                            definition is None
                            or not self.is_schema_value(value, definition)
                            or not self.value_within_schema_limits(value, definition)
                        ):
                            status = "failed"
                            result_error = "Устройство вернуло значение, не соответствующее Schema."
                            value = None
                        elif success and not self.schema_values_match(value, operation["value"]):
                            status = "failed"
                            result_error = "Устройство подтвердило значение, отличающееся от запрошенного."
                        elif success:
                            status = "succeeded"
                            result_error = None
                        else:
                            status = "failed"
                            if (
                                definition is None
                                or not self.is_schema_value(value, definition)
                                or not self.value_within_schema_limits(value, definition)
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
                        previous_parameters = dict(self.device_states.get(device_id, {}))
                        self.device_states[device_id] = {
                            **previous_parameters,
                            parameter: value
                        }
                        self.record_device_telemetry(device_id, {parameter: value})
                        last_seen, _, _, _ = self.save_device_state(
                            device_id,
                            {parameter: value},
                            record_changes=False
                        )
                        self.history.record(device_id, {parameter: value})
                        await self.broadcast({
                            "type": "device_state",
                            "device": device_id,
                            "parameters": self.device_states[device_id],
                            "last_seen": last_seen
                        })
                        if parameter not in previous_parameters or not self.schema_values_match(previous_parameters[parameter], value):
                            automation_state_change = (
                                previous_parameters,
                                dict(self.device_states[device_id])
                            )
                            if self.is_automation_command(command_id, pending.get("automation_id")) and status in ("succeeded", "failed"):
                                pending["automation_state_change"] = {
                                    "parameter": parameter,
                                    "value": value
                                }

                    pending = self.remove_pending_command(command_id)
                    await self.send_pending_command_result(
                        pending,
                        status,
                        value,
                        result_error
                    )
                    if automation_state_change is not None:
                        await self.evaluate_automations(
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
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Device telemetry parameters must be an object."
                        })
                        continue

                    metadata = self.device_metadata.get(
                        device_id, {"schema": self.default_legacy_schema()}
                    )
                    schema = metadata["schema"]
                    allow_unknown = metadata.get("schema_version") is None
                    pending_schema = self.pending_device_schemas.get(device_id)
                    deferred_schema = self.deferred_device_schemas.get(device_id)
                    review_schema = (
                        pending_schema["schema"]
                        if pending_schema is not None
                        else deferred_schema
                    )
                    if review_schema is not None:
                        accepted_parameters = self.accepted_parameters_during_schema_review(
                            schema, review_schema, parameters
                        )
                        state_is_valid = accepted_parameters is not None
                    else:
                        state_is_valid = self.validate_parameters(
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
                        await self.send_json(websocket, {
                            "type": "error",
                            "message": "Состояние устройства не соответствует Schema."
                        })
                        continue

                    if self.management.record_health(device_id, message.get("health")):
                        await self.broadcast({
                            "type": "device_health", "device": device_id,
                            "health": self.management.health_for_device(device_id)
                        })

                    if allow_unknown:
                        updated_schema = json.loads(json.dumps(schema))
                        updated_schema.setdefault("parameters", {})
                        schema_changed = False
                        for key, value in accepted_parameters.items():
                            if key not in updated_schema["parameters"]:
                                updated_schema["parameters"][key] = self.inferred_definition(key, value)
                                schema_changed = True
                        if schema_changed:
                            if len(updated_schema["parameters"]) > 64:
                                await self.send_json(websocket, {
                                    "type": "error",
                                    "message": "В Schema превышен лимит параметров."
                                })
                                continue
                            metadata["schema"] = updated_schema
                            self.save_device_metadata(device_id, metadata)
                            await self.send_device_list()

                    previous_parameters = dict(self.device_states.get(device_id, {}))
                    telemetry_changed = any(
                        key not in previous_parameters or not self.schema_values_match(previous_parameters[key], value)
                        for key, value in accepted_parameters.items()
                    )
                    self.device_states[device_id] = {
                        **self.device_states.get(device_id, {}),
                        **accepted_parameters
                    }
                    self.record_device_telemetry(device_id, accepted_parameters)

                    pending_state_echoes = {}
                    for pending in self.pending_commands.values():
                        operation = pending["operation"]
                        if (
                            pending["device"] == device_id
                            and operation["kind"] == "parameter"
                            and operation["name"] in accepted_parameters
                            and self.schema_values_match(accepted_parameters[operation["name"]], operation["value"])
                        ):
                            pending_state_echoes[operation["name"]] = pending

                    last_seen, state_changed, deferred_events, last_telemetry_at = self.save_device_state(
                        device_id,
                        accepted_parameters,
                        defer_parameters=set(pending_state_echoes),
                        record_telemetry=bool(accepted_parameters)
                    )
                    self.history.record(device_id, accepted_parameters)
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
                        "parameters": self.device_states[device_id],
                        "last_seen": last_seen,
                        "last_telemetry_at": last_telemetry_at
                    }

                    await self.broadcast(state_message)

                    if (
                        device_id in self.deferred_connection_automations
                        and self.device_metadata.get(device_id, {}).get("device_profile") == "universal"
                    ):
                        await self.resume_device_configuration(device_id)

                    if telemetry_changed:
                        await self.evaluate_automations(
                            device_id, previous_parameters, self.device_states[device_id]
                        )

                    if state_changed:
                        await self.send_event_history()
                    continue

                if client_type is None:
                    print(
                        f"[ПРЕДУПРЕЖДЕНИЕ] Сообщение до регистрации hello "
                        f"от {remote}: {message_type}"
                    )
                    await self.send_json(websocket, {
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

        except ConnectionClosed as error:
            close_error = error

        except Exception as error:
            print(f"[ОШИБКА] Обработка соединения: {error}")

        finally:
            self.browser_clients.discard(websocket)

            if device_id:
                close_frame = (
                    getattr(close_error, "rcvd", None)
                    or getattr(close_error, "sent", None)
                )
                code = getattr(websocket, "close_code", None) or getattr(close_frame, "code", None)
                reason = getattr(websocket, "close_reason", None) or getattr(close_frame, "reason", "")
                now = asyncio.get_running_loop().time()
                current_connection = self.devices.get(device_id) is websocket
                activity = self.last_device_heartbeat.get(device_id) if current_connection else None
                telemetry = self.last_device_telemetry.get(device_id) if current_connection else None
                activity_age = f"{max(0, now - activity):.1f}s" if activity is not None else "unknown"
                telemetry_age = f"{max(0, now - telemetry):.1f}s" if telemetry is not None else "none"
                print(
                    f"[CLOSE] {device_id}: code={code or 'unknown'} reason={reason!r}; "
                    f"session={now - connection_started_at:.1f}s, "
                    f"last_message_age={activity_age}, last_data_age={telemetry_age}"
                )

            if device_id:
                pending_access = self.pending_device_connections.get(device_id)
                if (
                    pending_access is not None
                    and pending_access.get("websocket") is websocket
                ):
                    self.pending_device_connections.pop(device_id, None)
                    await self.send_device_access_state()

            # Обрабатываем отключение только текущего соединения.
            # Это не даст старому соединению ошибочно отметить
            # новое соединение того же устройства отключённым.
            if (
                device_id
                and self.devices.get(device_id) is websocket
            ):
                self.cancel_pending_automation_triggers_for_device(device_id)
                self.cancel_configuration_apply(device_id, pending=True)
                self.management.record_disconnect(device_id, code, reason)
                del self.devices[device_id]
                self.device_wire_ids.pop(device_id, None)
                self.last_device_heartbeat.pop(device_id, None)
                self.last_device_telemetry.pop(device_id, None)
                self.last_parameter_telemetry.pop(device_id, None)
                self.last_device_probe.pop(device_id, None)
                self.fresh_device_parameters.pop(device_id, None)
                self.deferred_connection_automations.discard(device_id)
                self.deferred_device_schemas.pop(device_id, None)

                await self.finish_pending_commands_for_device(
                    device_id,
                    "unavailable",
                    "Устройство отключилось до получения результата команды."
                )

                with self.database_connection() as conn:
                    conn.execute("""
                        UPDATE devices
                        SET connected = 0
                        WHERE device_id = ?
                    """, (device_id,))

                self.save_device_event(
                    device_id,
                    "connection",
                    "Отключено"
                )

                print(f"[ESP32] Отключено устройство: {device_id}")

                await self.send_device_list()
                await self.send_event_history()
                await self.send_command_queue_state()
                self.fire_automation_event("device_disconnected", device_id)

            elif client_type == "browser":
                self.detach_pending_commands_for_browser(websocket)
                print("[БРАУЗЕР] Отключён")

            current_task = asyncio.current_task()
            if current_task is not None:
                self.device_handler_tasks.discard(current_task)
