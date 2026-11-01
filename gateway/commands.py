"""Command dispatch, durable delivery attempts and command results."""

import asyncio
import json
import sqlite3
import uuid


class CommandsMixin:
    async def send_command_queue_state(self, websocket=None):
        commands = [
            {
                "command_id": pending["command_id"],
                "device": pending["device"],
                "operation": pending["operation"],
                "status": pending.get("status", "queued"),
                "attempts": pending.get("attempts", 0)
            }
            for pending in self.pending_commands.values()
        ]
        message = {"type": "command_queue", "commands": commands}
        if websocket is not None:
            await self.send_json(websocket, message)
        else:
            await self.broadcast(message)


    async def notify_command_queued(self, pending):
        if pending["operation"].get("transient"):
            removed = self.remove_pending_command(pending["command_id"])
            if removed is not None:
                await self.send_pending_command_result(
                    removed, "unavailable", removed["operation"].get("value"),
                    "Общая команда отменена: устройство недоступно. Она не будет выполнена после переподключения."
                )
            return
        pending["status"] = "queued"
        self.update_command_queue_record(pending, "queued")
        if pending.get("browser") is not None:
            try:
                await self.send_json(pending["browser"], {
                    "type": "command_queued",
                    "command_id": pending["command_id"],
                    "device": pending["device"],
                    "attempts": pending.get("attempts", 0)
                })
            except Exception:
                pending["browser"] = None
        await self.send_command_queue_state()


    async def deliver_pending_command(self, pending):
        if pending.get("sending"):
            return False
        device_id = pending["device"]
        websocket = self.devices.get(device_id)
        if websocket is None or device_id in self.pending_device_schemas or self.device_configuration_is_pending(device_id):
            await self.notify_command_queued(pending)
            return False

        operation = pending["operation"]
        schema = self.device_metadata.get(device_id, {}).get("schema", {})
        if operation["kind"] == "parameter":
            definition = schema.get("parameters", {}).get(operation["name"])
            matches = (
                definition is not None and definition.get("access") == "read_write"
                and self.is_schema_value(operation.get("value"), definition)
                and self.value_within_schema_limits(operation.get("value"), definition)
            )
        else:
            definition = schema.get("actions", {}).get(operation["name"])
            matches = definition is not None and self.validate_action_arguments(definition, operation.get("arguments", {}))
        if not matches:
            removed = self.remove_pending_command(pending["command_id"])
            if removed is not None:
                await self.send_pending_command_result(
                    removed, "failed", operation.get("value"),
                    "Команда больше не соответствует принятой Schema устройства."
                )
            return False

        pending["sending"] = True
        try:
            pending["attempts"] = pending.get("attempts", 0) + 1
            pending["status"] = "sent"
            self.update_command_queue_record(pending, "sent")
            payload = dict(pending["payload"])
            payload["device"] = self.firmware_identity_for(device_id)
            await self.send_json(websocket, payload)
        except Exception:
            await self.notify_command_queued(pending)
            return False
        finally:
            pending["sending"] = False

        if self.pending_commands.get(pending["command_id"]) is not pending:
            return True

        old_task = pending.get("timeout_task")
        if old_task is not None and old_task is not asyncio.current_task():
            old_task.cancel()
        pending["timeout_task"] = asyncio.create_task(
            self.expire_pending_command(pending["command_id"])
        )
        if pending.get("browser") is not None:
            try:
                await self.send_json(pending["browser"], {
                    "type": "command_sent",
                    "device": device_id,
                    "command_id": pending["command_id"],
                    "status": "sent",
                    "attempts": pending["attempts"]
                })
            except Exception:
                pending["browser"] = None
        await self.send_command_queue_state()
        return True


    async def deliver_queued_commands(self, device_id):
        for pending in list(self.pending_commands.values()):
            if pending["device"] == device_id and pending.get("status") == "queued":
                if (
                    pending.get("attempts", 0) > 0
                    and not self.device_supports_command_dedup(device_id)
                ):
                    pending = self.remove_pending_command(pending["command_id"])
                    if pending is not None:
                        await self.send_pending_command_result(
                            pending,
                            "unavailable",
                            pending["operation"].get("value"),
                            "Повтор отключён: прошивка не объявила защиту от повторного command_id."
                        )
                    continue
                await self.deliver_pending_command(pending)


    def remove_pending_command(self, command_id):
        pending = self.pending_commands.pop(command_id, None)

        if pending is not None:
            timeout_task = pending.get("timeout_task")

            if (
                timeout_task is not None
                and timeout_task is not asyncio.current_task()
            ):
                timeout_task.cancel()

        return pending


    async def send_pending_command_result(self, pending, status, value, error):
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
                await self.send_json(browser, message)
            except Exception:
                pending["browser"] = None

        self.update_command_queue_record(pending, status, result_record, error)
        result_future = pending.get("result_future")
        if result_future is not None and not result_future.done():
            result_future.set_result({"status": status, "value": value, "error": error})
        automation_state_change = pending.pop("automation_state_change", None)
        if (
            automation_state_change is None
            and self.is_automation_command(pending["command_id"], pending.get("automation_id"))
            and status in ("succeeded", "failed")
            and operation["kind"] == "parameter"
            and value is not None
            and any(
                state_event["parameter"] == operation["name"]
                and self.schema_values_match(state_event["value"], value)
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
                and self.schema_values_match(state_event["value"], value)
            )
            if is_confirmed_command_state:
                continue
            try:
                self.save_device_event(
                    pending["device"],
                    state_event["event_type"],
                    state_event["event_value"],
                    timestamp=state_event["timestamp"]
                )
            except Exception as log_error:
                print(f"[ОШИБКА] Не удалось сохранить изменение состояния: {log_error}")

        if status in ("succeeded", "failed") and automation_state_change is not None:
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
                self.save_device_event(
                    pending["device"],
                    state_event_type,
                    state_event_value
                )
            except Exception as log_error:
                print(f"[ОШИБКА] Не удалось записать изменение состояния сценария: {log_error}")

        try:
            if status != "succeeded" or not self.is_automation_command(
                pending["command_id"], pending.get("automation_id")
            ):
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
                self.save_device_event(pending["device"], "command", json.dumps(command_event, ensure_ascii=False))
            await self.send_event_history()
        except Exception as log_error:
            print(f"[ОШИБКА] Не удалось сохранить результат команды: {log_error}")

        if pending["browser"] is None:
            await self.send_command_queue_state()
            print(
                f"[КОМАНДА] {pending['command_id']} ({pending['device']}): "
                f"{status}; {error or value}"
            )
            return

        await self.send_command_queue_state()


    async def expire_pending_command(self, command_id):
        await asyncio.sleep(self.COMMAND_TIMEOUT_SECONDS)
        pending = self.pending_commands.get(command_id)
        if pending is None:
            return

        pending["timeout_task"] = None
        if not self.device_supports_command_dedup(pending["device"]):
            pending = self.remove_pending_command(command_id)
            if pending is not None:
                await self.send_pending_command_result(
                    pending,
                    "timeout",
                    pending["operation"].get("value"),
                    "Устройство не подтвердило команду. Повтор отключён, так как прошивка не объявила защиту от дублей."
                )
            return
        if pending.get("attempts", 0) < self.COMMAND_RETRY_LIMIT:
            if await self.deliver_pending_command(pending):
                return
        if pending.get("automation_id") or pending["operation"].get("transient"):
            pending = self.remove_pending_command(command_id)
            if pending is not None:
                await self.send_pending_command_result(
                    pending,
                    "timeout",
                    pending["operation"].get("value"),
                    "Устройство не подтвердило действие после повторных попыток; отложенное выполнение отменено."
                )
            return
        await self.notify_command_queued(pending)


    async def finish_pending_commands_for_device(
        self,
        device_id, status, error, retry=True
    ):
        command_ids = [
            command_id
            for command_id, pending in self.pending_commands.items()
            if pending["device"] == device_id
        ]

        for command_id in command_ids:
            pending = self.pending_commands.get(command_id)
            if pending is None:
                continue
            if pending.get("automation_id") or pending["operation"].get("transient"):
                pending = self.remove_pending_command(command_id)
                if pending is not None:
                    await self.send_pending_command_result(
                        pending,
                        status,
                        pending["operation"].get("value"),
                        error
                    )
                continue
            if retry:
                if not self.device_supports_command_dedup(device_id):
                    pending = self.remove_pending_command(command_id)
                    if pending is not None:
                        await self.send_pending_command_result(
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
                await self.notify_command_queued(pending)
                continue

            pending = self.remove_pending_command(command_id)
            if pending is not None:
                await self.send_pending_command_result(
                    pending, status, pending["operation"].get("value"), error
                )


    def detach_pending_commands_for_browser(self, websocket):
        # Keep tracking a command after its page closes. Its result or timeout is
        # still written to the event log, even though there is no page to notify.
        for pending in self.pending_commands.values():
            if pending["browser"] is websocket:
                pending["browser"] = None


    async def dispatch_device_command(
        self,
        device_id,
        parameter,
        value,
        browser=None,
        requested_command_id=None,
        wait_for_result=False,
        automation_id=None,
        automation_run_id=None,
        transient=False
    ):
        async def reject(message, command_id=requested_command_id):
            if browser is None:
                print(message)
            else:
                await self.send_json(browser, {
                    "type": "error",
                    "command_id": command_id,
                    "message": message
                })

        if not isinstance(device_id, str) or device_id not in self.devices:
            await reject(f"Устройство {device_id} не подключено.")
            return False

        if device_id in self.pending_device_schemas:
            await reject("Сначала рассмотрите предложенное изменение Schema устройства.")
            return False

        if self.device_configuration_is_pending(device_id):
            await reject("Дождитесь подтверждения аппаратной конфигурации устройства.")
            return False

        if not isinstance(parameter, str):
            await reject("Некорректный идентификатор параметра.")
            return False

        schema = self.device_metadata.get(
            device_id, {"schema": self.default_legacy_schema()}
        )["schema"]
        definition = schema.get("parameters", {}).get(parameter)
        if (
            definition is None
            or definition.get("access") != "read_write"
            or not self.is_schema_value(value, definition)
            or not self.value_within_schema_limits(value, definition)
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
            for pending in self.pending_commands.values()
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

        if command_id in self.pending_commands:
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
        if transient:
            pending["operation"]["transient"] = True
        try:
            self.create_command_queue_record(pending)
        except sqlite3.IntegrityError:
            await reject("Команда с таким command_id уже использовалась.", command_id)
            return False
        self.pending_commands[command_id] = pending
        await self.deliver_pending_command(pending)

        print(f"[КОМАНДА] {device_id}: {parameter} = {value} ({command_id})")
        return pending["result_future"] if wait_for_result else True


    async def dispatch_device_action(
        self,
        device_id,
        action,
        arguments=None,
        browser=None,
        requested_command_id=None,
        wait_for_result=False,
        automation_id=None,
        automation_run_id=None,
        transient=False
    ):
        async def reject(message, command_id=requested_command_id):
            if browser is None:
                print(message)
            else:
                await self.send_json(browser, {
                    "type": "error",
                    "command_id": command_id,
                    "message": message
                })

        if not isinstance(device_id, str) or device_id not in self.devices:
            await reject(f"Устройство {device_id} не подключено.")
            return False

        if device_id in self.pending_device_schemas:
            await reject("Сначала рассмотрите предложенное изменение Schema устройства.")
            return False
        if self.device_configuration_is_pending(device_id):
            await reject("Дождитесь подтверждения аппаратной конфигурации устройства.")
            return False
        if not isinstance(action, str):
            await reject("Некорректный идентификатор действия.")
            return False
        metadata = self.device_metadata.get(device_id, {})
        definition = metadata.get("schema", {}).get("actions", {}).get(action)
        if definition is None:
            await reject(f"Действие {action!r} не объявлено в Schema устройства.")
            return False
        if arguments is None:
            arguments = {}
        if not self.validate_action_arguments(definition, arguments):
            await reject("Аргументы действия не соответствуют его Schema.")
            return False

        if any(
            pending["device"] == device_id
            and pending["operation"]["kind"] == "action"
            and pending["operation"]["name"] == action
            for pending in self.pending_commands.values()
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
        if command_id in self.pending_commands:
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
        if transient:
            pending["operation"]["transient"] = True
        try:
            self.create_command_queue_record(pending)
        except sqlite3.IntegrityError:
            await reject("Команда с таким command_id уже использовалась.", command_id)
            return False
        self.pending_commands[command_id] = pending
        await self.deliver_pending_command(pending)
        if browser is None:
            print(f"Действие отправлено: {device_id} — {definition.get('label', action)}")
        return pending["result_future"] if wait_for_result else True
