"""Automation validation, diagnostics, interpretation and run history."""

import asyncio
import json
import math
import re
import sqlite3
import struct
import time
import uuid
from datetime import datetime, timezone
from errors import AutomationExecutionError, AutomationStoppedError


class AutomationMixin:
    @staticmethod
    def automation_program_tag(program):
        """Correlate a live run with the editor's program, without trusting block IDs alone."""
        def canonical(value):
            if value is None:
                return "z"
            if isinstance(value, bool):
                return "t" if value else "f"
            if isinstance(value, (int, float)):
                if isinstance(value, int) and abs(value) > 9007199254740991:
                    return f"i{value};"
                if not math.isfinite(value):
                    raise ValueError("Non-finite program value")
                # IEEE bytes avoid Python/JavaScript differences in 1e-7 formatting.
                return "n" + struct.pack(">d", value).hex()
            if isinstance(value, str):
                return "s" + json.dumps(value, ensure_ascii=False)
            if isinstance(value, list):
                return "a[" + ",".join(canonical(item) for item in value) + "]"
            if isinstance(value, dict):
                return "o{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + canonical(value[key]) for key in sorted(value)) + "}"
            raise TypeError("Unsupported program value")

        try:
            encoded = canonical(program).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, OverflowError):
            return None
        # A small UI correlation tag, never an authorisation or integrity check.
        result = 2166136261
        for byte in encoded:
            result = ((result ^ byte) * 16777619) & 0xffffffff
        return f"{result:08x}"

    def automation_progress_snapshot(self, automation_id):
        state = getattr(self, "_automation_progress", {}).get(automation_id)
        if state is None:
            return None
        snapshot = {key: value for key, value in state.items() if not key.startswith("_")}
        snapshot["server_time"] = self.current_time()
        if state.get("stage") == "wait":
            snapshot["remaining_seconds"] = max(0, state["_wait_deadline"] - time.monotonic())
        return snapshot

    async def publish_automation_progress(self, automation_id, run_id, stage, *,
                                          step_type=None, details=None, block_id=None,
                                          status="running", error=None, wait_deadline=None):
        states = getattr(self, "_automation_progress", {})
        previous = states.get(automation_id)
        # A cancelled old run can finish after its replacement has started.
        if previous is None or previous["run_id"] != run_id:
            return
        state = dict(previous)
        state.update(stage=stage, status=status, step_type=step_type,
                     details=dict(details or {}), block_id=block_id, error=error)
        state["sequence"] = previous.get("sequence", 0) + 1
        if stage not in {"starting", "acknowledgment", "finished"}:
            state["step_number"] += 1
        if stage == "wait":
            state["_wait_deadline"] = wait_deadline if wait_deadline is not None else time.monotonic() + details["seconds"]
        else:
            state.pop("_wait_deadline", None)
        states[automation_id] = state
        try:
            await self.broadcast({"type": "automation_progress", **self.automation_progress_snapshot(automation_id)})
        except Exception as progress_error:
            # UI telemetry must not interrupt a device command or its acknowledgement.
            print(f"[АВТОМАТИЗАЦИЯ] Не удалось отправить ход запуска: {progress_error}")
        finally:
            if status != "running" and states.get(automation_id) is state:
                states.pop(automation_id, None)

    def get_automations(self):
        with self.database_connection() as conn:
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
                "last_error": last_error,
                "active_progress": self.automation_progress_snapshot(automation_id)
            })
        return result


    def get_automation_run_count(self, automation_id, completed_only=False):
        where_clause = "automation_id = ?"
        if completed_only:
            where_clause += " AND status != 'running'"
        with self.database_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM automation_runs WHERE {where_clause}",
                (automation_id,)
            ).fetchone()
        return row[0] if row else 0


    def get_automation_run_history(self, automation_id):
        with self.database_connection() as conn:
            conn.row_factory = sqlite3.Row
            runs = conn.execute("""
                SELECT run_id, automation_name, trigger_json, trigger_reason,
                       started_at, finished_at, status, error
                FROM automation_runs
                WHERE automation_id = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT ?
            """, (automation_id, self.AUTOMATION_RUN_HISTORY_LIMIT)).fetchall()

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


    def create_automation_run(self, automation_id, config, reason):
        run_id = uuid.uuid4().hex
        started_at = self.current_time()
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
        with self.database_connection() as conn:
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
            """, (automation_id, self.AUTOMATION_RUN_HISTORY_LIMIT)).fetchall()
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


    def start_automation_run_step(self, run_id, step_type, details=None):
        try:
            with self.database_connection() as conn:
                sequence = conn.execute("""
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM automation_run_steps WHERE run_id = ?
                """, (run_id,)).fetchone()[0]
                cursor = conn.execute("""
                    INSERT INTO automation_run_steps (
                        run_id, sequence, step_type, status, started_at, details_json
                    ) VALUES (?, ?, ?, 'running', ?, ?)
                """, (
                    run_id, sequence, step_type, self.current_time(),
                    json.dumps(details or {}, ensure_ascii=False, allow_nan=False)
                ))
                return cursor.lastrowid
        except (sqlite3.Error, TypeError, ValueError) as error:
            print(f"[АВТОМАТИЗАЦИЯ] Не удалось записать шаг запуска: {error}")
            return None


    def finish_automation_run_step(self, step_id, status, details=None, error=None):
        if step_id is None:
            return
        try:
            with self.database_connection() as conn:
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
                    status, self.current_time(),
                    json.dumps(saved_details, ensure_ascii=False, allow_nan=False),
                    error, step_id
                ))
        except (sqlite3.Error, TypeError, ValueError) as log_error:
            print(f"[АВТОМАТИЗАЦИЯ] Не удалось сохранить результат шага: {log_error}")


    def finish_automation_run(self, run_id, status, error=None):
        finished_at = self.current_time()
        with self.database_connection() as conn:
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


    def automation_device_metadata(self, device_id):
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
            return None
        return self.device_metadata.get(device_id) or self.load_device_metadata(device_id)


    def validate_automation_program(self, program):
        metadata_cache = {}

        def metadata_for(device_id):
            if not isinstance(device_id, str):
                return None
            if device_id not in metadata_cache:
                metadata_cache[device_id] = self.automation_device_metadata(device_id)
            return metadata_cache[device_id]

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
            metadata = metadata_for(device_id)
            parameter = metadata.get("schema", {}).get("parameters", {}).get(parameter_id) if metadata else None
            if parameter is None or parameter.get("access") not in {"read_only", "read_write"} or parameter.get("type") == "json":
                return "Триггер ссылается на неизвестный параметр устройства."
            if "value" in trigger and (
                not self.is_schema_value(trigger["value"], parameter)
                or not self.value_within_schema_limits(trigger["value"], parameter)
            ):
                return "Значение фильтра запуска не соответствует Schema параметра."
        elif trigger_type in {"device_connected", "device_disconnected"}:
            if set(trigger) != {"type", "device"} or metadata_for(trigger.get("device")) is None:
                return "Триггер ссылается на неизвестное устройство."
        elif trigger_type == "gateway_started":
            if set(trigger) != {"type"}:
                return "Некорректный триггер запуска Gateway."
        elif trigger_type == "manual":
            if (
                set(trigger) - {"type", "show_on_dashboard"}
                or ("show_on_dashboard" in trigger and type(trigger["show_on_dashboard"]) is not bool)
            ):
                return "Некорректные настройки запуска по кнопке."
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
                metadata = metadata_for(device_id)
                definition = metadata.get("schema", {}).get("parameters", {}).get(parameter_id) if metadata else None
                if definition is None or definition.get("access") not in {"read_only", "read_write"} or definition.get("type") == "json":
                    raise ValueError("Выражение ссылается на недоступный параметр.")
                return definition.get("type")
            if kind == "device_connected":
                if set(expression) != {"type", "device"} or metadata_for(expression.get("device")) is None:
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
                if "block_id" in statement and (
                    not isinstance(statement["block_id"], str)
                    or not 1 <= len(statement["block_id"]) <= 128
                    or any(ord(character) < 32 for character in statement["block_id"])
                ):
                    raise ValueError("Некорректный идентификатор блока.")
                # Source IDs identify the corresponding Blockly block. They never
                # affect evaluation or authorise any additional device operation.
                statement_keys = set(statement) - {"block_id"}
                kind = statement.get("type")
                if kind == "set_parameter":
                    if statement_keys != {"type", "device", "parameter", "value"}:
                        raise ValueError("Некорректный блок установки параметра.")
                    metadata = metadata_for(statement.get("device"))
                    definition = metadata.get("schema", {}).get("parameters", {}).get(statement.get("parameter")) if metadata else None
                    if not definition or definition.get("access") != "read_write":
                        raise ValueError("Действие ссылается на неизвестный или доступный только для чтения параметр.")
                    value = statement.get("value")
                    value_type = expression_type(value, depth + 1)
                    if value.get("type") == "constant":
                        raw_value = value.get("value")
                        if not self.is_schema_value(raw_value, definition) or not self.value_within_schema_limits(raw_value, definition):
                            raise ValueError("Значение команды не соответствует Schema параметра.")
                    elif value_type not in {definition.get("type"), "integer" if definition.get("type") == "number" else ""}:
                        if definition.get("type") != "enum" or value_type not in {"enum", "string", "integer", "number", "boolean"}:
                            raise ValueError("Тип значения команды не совпадает со Schema параметра.")
                elif kind == "invoke_action":
                    if statement_keys != {"type", "device", "action", "arguments"}:
                        raise ValueError("Некорректный блок вызова действия.")
                    metadata = metadata_for(statement.get("device"))
                    definition = metadata.get("schema", {}).get("actions", {}).get(statement.get("action")) if metadata else None
                    arguments = statement.get("arguments")
                    if not definition or not isinstance(arguments, dict) or not self.validate_action_arguments(definition, arguments):
                        raise ValueError("Вызов действия или его аргументы не соответствуют Schema.")
                elif kind == "wait":
                    seconds = statement.get("seconds")
                    if (
                        statement_keys != {"type", "seconds"}
                        or type(seconds) not in (int, float)
                        or not 0 <= seconds <= 3600
                        or not math.isfinite(seconds)
                    ):
                        raise ValueError("Пауза должна быть числом от 0 до 3600 секунд.")
                elif kind == "if":
                    if statement_keys != {"type", "branches", "else"}:
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


    def validate_automation_config(self, config):
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
            return self.validate_automation_program(config.get("program"))
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
            self.device_metadata.get(source_id) or self.load_device_metadata(source_id)
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
            self.device_metadata.get(target_id) or self.load_device_metadata(target_id)
            if isinstance(target_id, str) else None
        )
        if not isinstance(target_id, str) or target_metadata is None:
            return "Выберите известное Gateway целевое устройство."
        if target_id in self.pending_device_schemas:
            return "Сначала примите или отклоните ожидающее изменение Schema целевого устройства."
        schema = target_metadata.get("schema", {})
        if kind == "parameter":
            definition = schema.get("parameters", {}).get(operation_name)
            value = target.get("value")
            if (
                not definition or definition.get("access") != "read_write"
                or not self.is_schema_value(value, definition)
                or not self.value_within_schema_limits(value, definition)
                or set(target) != {"device", "kind", "name", "value"}
            ):
                return "Значение блока действия не соответствует доступным параметрам Schema."
        elif kind == "action":
            definition = schema.get("actions", {}).get(operation_name)
            arguments = target.get("arguments", {})
            if (
                not definition or not isinstance(arguments, dict)
                or not self.validate_action_arguments(definition, arguments)
                or set(target) != {"device", "kind", "name", "arguments"}
            ):
                return "Параметры режима не соответствуют его Schema."
        else:
            return "Неизвестный тип блока действия."
        return None


    def automation_diagnostics(self, config):
        """Inspect a scenario without executing commands or changing saved state."""
        issues = []
        seen = set()

        def issue(severity, code, message, message_en, device=None, parameter=None):
            key = (code, device, parameter)
            if key in seen:
                return
            seen.add(key)
            entry = {
                "severity": severity, "code": code, "message": message,
                "message_en": message_en
            }
            if device is not None:
                entry["device"] = device
            if parameter is not None:
                entry["parameter"] = parameter
            issues.append(entry)

        try:
            validation_error = self.validate_automation_config(config)
        except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
            validation_error = "Сценарий содержит некорректные данные."
        if validation_error:
            issue("error", "invalid_program", str(validation_error),
                  "Scenario validation failed. Review its blocks and device schema.")
        if not isinstance(config, dict):
            return issues, False

        command_targets = set()
        parameter_reads = set()
        schema_references = set()
        if config.get("version") == 2:
            program = config.get("program")
            trigger = program.get("trigger") if isinstance(program, dict) else None
            if isinstance(trigger, dict) and isinstance(trigger.get("device"), str):
                schema_references.add(trigger["device"])
            stack = [program]
            visited = 0
            while stack and visited < 1000:
                node = stack.pop()
                visited += 1
                if isinstance(node, list):
                    stack.extend(node)
                    continue
                if not isinstance(node, dict):
                    continue
                kind = node.get("type")
                device_id = node.get("device")
                if isinstance(device_id, str):
                    if kind in ("set_parameter", "invoke_action"):
                        command_targets.add(device_id)
                    elif kind == "parameter" and isinstance(node.get("parameter"), str):
                        parameter_reads.add((device_id, node["parameter"]))
                stack.extend(node.values())
        else:
            target = config.get("target")
            if isinstance(target, dict) and isinstance(target.get("device"), str):
                command_targets.add(target["device"])
            trigger = config.get("trigger")
            if isinstance(trigger, dict) and isinstance(trigger.get("device"), str):
                schema_references.add(trigger["device"])

        referenced = schema_references | command_targets | {device for device, _ in parameter_reads}
        metadata_by_id = {device: self.automation_device_metadata(device) for device in referenced}
        valid_schema_devices = set()
        for device_id, metadata in sorted(metadata_by_id.items()):
            if metadata is None or not self.validate_schema(metadata.get("schema")):
                issue("error", "invalid_device_schema",
                      f"Нет принятой корректной Schema устройства {device_id}.",
                      f"Device {device_id} has no valid accepted schema.", device_id)
            else:
                valid_schema_devices.add(device_id)
            if device_id in self.pending_device_schemas:
                issue("warning", "pending_schema",
                      f"Рассмотрите изменение Schema устройства {device_id} перед запуском.",
                      f"Review the pending schema for {device_id} before running.", device_id)

        for device_id in sorted(command_targets):
            if self.device_configuration_is_pending(device_id):
                issue("warning", "configuration_applying",
                      f"Дождитесь подтверждения аппаратной конфигурации устройства {device_id}.",
                      f"Wait for {device_id} to confirm its hardware configuration.", device_id)
            if not self.device_is_connected(device_id):
                issue("warning", "target_offline",
                      f"Целевое устройство {device_id} отключено.",
                      f"Target device {device_id} is offline.", device_id)

        for device_id, parameter in sorted(parameter_reads):
            if device_id not in valid_schema_devices:
                continue
            if self.device_configuration_is_pending(device_id):
                issue("warning", "configuration_applying",
                      f"Дождитесь подтверждения аппаратной конфигурации устройства {device_id}.",
                      f"Wait for {device_id} to confirm its hardware configuration.", device_id)
            metadata = metadata_by_id.get(device_id) or {}
            definition = metadata.get("schema", {}).get("parameters", {}).get(parameter)
            if definition is None:
                issue("error", "parameter_missing",
                      f"Параметр {device_id}.{parameter} отсутствует в принятой Schema.",
                      f"Parameter {device_id}.{parameter} is missing from the accepted schema.",
                      device_id, parameter)
                continue
            values = self.device_states.get(device_id, {})
            current = values.get(parameter)
            if (
                not self.parameter_value_is_fresh(device_id, parameter)
                or parameter not in values
                or not self.is_schema_value(current, definition)
                or not self.value_within_schema_limits(current, definition)
            ):
                issue("warning", "parameter_stale",
                      f"Нет актуального значения {device_id}.{parameter} для вычисления условия.",
                      f"No current value of {device_id}.{parameter} is available for this expression.",
                      device_id, parameter)
        return issues, not issues


    def save_automation(self, automation_id, config, enabled=True):
        error = self.validate_automation_config(config)
        if error:
            return False, error
        now = self.current_time()
        with self.database_connection() as conn:
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
        # Invalidate the previous stability/cooldown wait before any caller can
        # yield while acknowledging the save to a slow browser.
        self.cancel_pending_automation_trigger(automation_id)
        return True, "Сценарий сохранён."


    def evaluate_automation_expression(self, expression):
        kind = expression["type"]
        if kind == "constant":
            return expression["value"]
        if kind == "device_connected":
            return self.device_is_connected(expression["device"])
        if kind == "parameter":
            device_id = expression["device"]
            if not self.device_is_connected(device_id):
                raise AutomationExecutionError(f"Устройство {device_id} отключено.")
            if self.device_configuration_is_pending(device_id):
                raise AutomationExecutionError(f"Ожидается подтверждение аппаратной конфигурации {device_id}.")
            parameter = expression["parameter"]
            if not self.parameter_value_is_fresh(device_id, parameter):
                raise AutomationExecutionError(f"Значение {device_id}.{parameter} устарело или ещё не обновлялось в этой сессии Gateway.")
            if parameter not in self.device_states.get(device_id, {}):
                raise AutomationExecutionError(f"Нет актуального значения параметра {device_id}.{parameter}.")
            value = self.device_states[device_id][parameter]
            definition = (self.device_metadata.get(device_id) or self.load_device_metadata(device_id) or {}).get("schema", {}).get("parameters", {}).get(parameter)
            if not definition or not self.is_schema_value(value, definition) or not self.value_within_schema_limits(value, definition):
                raise AutomationExecutionError(f"Текущее значение {device_id}.{parameter} не соответствует Schema.")
            return value
        if kind == "not":
            return not self.evaluate_automation_expression(expression["operand"])
        if kind == "and":
            return all(self.evaluate_automation_expression(item) for item in expression["operands"])
        if kind == "or":
            return any(self.evaluate_automation_expression(item) for item in expression["operands"])
        if kind == "compare":
            left = self.evaluate_automation_expression(expression["left"])
            right = self.evaluate_automation_expression(expression["right"])
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
                left = float(self.evaluate_automation_expression(expression["left"]))
                right = float(self.evaluate_automation_expression(expression["right"]))
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


    async def execute_automation_statements(self, automation_id, statements, run_id):
        for statement in statements:
            if automation_id in self.automation_stop_requests:
                raise AutomationStoppedError("Остановлено пользователем.")
            if statement["type"] == "if":
                branch_taken = False
                for index, branch in enumerate(statement["branches"], start=1):
                    await self.publish_automation_progress(
                        automation_id, run_id, "condition", step_type="condition",
                        details={"branch": index}, block_id=statement.get("block_id")
                    )
                    step_id = self.start_automation_run_step(
                        run_id, "condition", {"branch": index}
                    )
                    try:
                        condition_result = bool(
                            self.evaluate_automation_expression(branch["condition"])
                        )
                    except Exception as error:
                        self.finish_automation_run_step(
                            step_id, "failed", error=str(error)
                        )
                        raise
                    self.finish_automation_run_step(
                        step_id, "succeeded", {"result": condition_result}
                    )
                    if condition_result:
                        await self.execute_automation_statements(
                            automation_id, branch["then"], run_id
                        )
                        branch_taken = True
                        break
                if not branch_taken:
                    otherwise = statement["else"]
                    if otherwise:
                        await self.publish_automation_progress(
                            automation_id, run_id, "otherwise", step_type="otherwise",
                            block_id=statement.get("block_id")
                        )
                        step_id = self.start_automation_run_step(run_id, "otherwise")
                        try:
                            await self.execute_automation_statements(
                                automation_id, otherwise, run_id
                            )
                        except AutomationStoppedError as error:
                            self.finish_automation_run_step(
                                step_id, "stopped", error=str(error)
                            )
                            raise
                        except Exception as error:
                            self.finish_automation_run_step(
                                step_id, "failed", error=str(error)
                            )
                            raise
                        self.finish_automation_run_step(step_id, "succeeded")
                continue

            if statement["type"] == "wait":
                seconds = statement["seconds"]
                deadline = time.monotonic() + seconds
                await self.publish_automation_progress(
                    automation_id, run_id, "wait", step_type="wait",
                    details={"seconds": seconds}, block_id=statement.get("block_id"),
                    wait_deadline=deadline
                )
                step_id = self.start_automation_run_step(
                    run_id, "wait", {"seconds": seconds}
                )
                current_task = asyncio.current_task()
                if current_task is not None:
                    self.automation_waiting_tasks.add(current_task)
                try:
                    while True:
                        if automation_id in self.automation_stop_requests:
                            raise AutomationStoppedError(
                                "Остановлено пользователем во время паузы."
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        await asyncio.sleep(min(remaining, 0.25))
                except AutomationStoppedError as error:
                    self.finish_automation_run_step(
                        step_id, "stopped", error=str(error)
                    )
                    raise
                except Exception as error:
                    self.finish_automation_run_step(
                        step_id, "failed", error=str(error)
                    )
                    raise
                finally:
                    if current_task is not None:
                        self.automation_waiting_tasks.discard(current_task)
                self.finish_automation_run_step(step_id, "succeeded")
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
            await self.publish_automation_progress(
                automation_id, run_id, "sending", step_type=step_type,
                details=step_details, block_id=statement.get("block_id")
            )
            step_id = self.start_automation_run_step(run_id, step_type, step_details)
            try:
                if not self.device_is_connected(device_id):
                    raise AutomationExecutionError(
                        f"Целевое устройство {device_id} недоступно."
                    )
                if step_type == "set_parameter":
                    value = self.evaluate_automation_expression(statement["value"])
                    step_details["requested_value"] = value
                    result_future = await self.dispatch_device_command(
                        device_id, statement["parameter"], value,
                        requested_command_id=command_id,
                        wait_for_result=True,
                        automation_id=automation_id,
                        automation_run_id=run_id
                    )
                elif step_type == "invoke_action":
                    result_future = await self.dispatch_device_action(
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
                await self.publish_automation_progress(
                    automation_id, run_id, "acknowledgment", step_type=step_type,
                    details=step_details, block_id=statement.get("block_id")
                )
                result = await asyncio.wait_for(
                    result_future,
                    timeout=self.COMMAND_TIMEOUT_SECONDS * (self.COMMAND_RETRY_LIMIT + 2)
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
                self.finish_automation_run_step(
                    step_id, "failed", details=step_details, error=message
                )
                raise AutomationExecutionError(message) from error
            except AutomationStoppedError as error:
                self.finish_automation_run_step(
                    step_id, "stopped", details=step_details, error=str(error)
                )
                raise
            except Exception as error:
                self.finish_automation_run_step(
                    step_id, "failed", details=step_details, error=str(error)
                )
                raise
            self.finish_automation_run_step(
                step_id, "succeeded", details=step_details
            )
            if automation_id in self.automation_stop_requests:
                raise AutomationStoppedError("Остановлено пользователем после текущего действия.")


    async def publish_automation_list(self):
        await self.broadcast({"type": "automation_list", "automations": self.get_automations()})
        await self.send_database_stats()


    def run_manual_automation(self, automation_id):
        if self.database_restore_in_progress:
            return False, "Дождитесь завершения восстановления базы Gateway."
        if not isinstance(automation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", automation_id):
            return False, "Некорректный ID сценария."
        with self.database_connection() as connection:
            row = connection.execute(
                "SELECT config_json, enabled, run_status FROM automations WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
        if row is None:
            return False, "Сценарий не найден."
        if not row[1]:
            return False, "Сначала включите сценарий."
        task = self.automation_tasks.get(automation_id)
        if row[2] == "running" or (task is not None and not task.done()):
            return False, "Сценарий уже выполняется."
        try:
            config = json.loads(row[0])
            error = self.validate_automation_config(config)
        except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
            return False, "Не удалось прочитать программу сценария."
        if error:
            return False, error
        if config.get("version") != 2 or config["program"]["trigger"].get("type") != "manual":
            return False, "Для ручного запуска выберите в Blockly событие «По нажатию кнопки»."
        if not self.start_automation_run(automation_id, config, "manual"):
            return False, "Не удалось запустить сценарий."
        return True, "Сценарий запущен. Результат появится в истории запусков."


    def start_automation_run(self, automation_id, config, reason):
        if self.database_restore_in_progress:
            return False
        existing_task = self.automation_tasks.get(automation_id)
        task_is_active = existing_task is not None and not existing_task.done()
        if task_is_active and existing_task not in self.automation_waiting_tasks:
            return False
        run_id = self.create_automation_run(automation_id, config, reason)
        if run_id is None:
            return False

        if task_is_active:
            # Повторный триггер во время паузы начинает её отсчёт заново.
            self.automation_restart_tasks.add(existing_task)
            self.automation_stop_requests.discard(automation_id)
            existing_task.cancel()

        if not hasattr(self, "_automation_progress"):
            self._automation_progress = {}
        self._automation_progress[automation_id] = {
            "automation_id": automation_id, "run_id": run_id, "status": "running",
            "stage": "starting", "step_number": 0, "step_type": None,
            "details": {}, "block_id": None, "sequence": 0,
            "started_at": self.current_time(),
            "program_tag": self.automation_program_tag(config.get("program"))
        }
        new_task = asyncio.create_task(
            self.execute_automation_run(automation_id, config, reason, run_id)
        )
        self.automation_tasks[automation_id] = new_task
        asyncio.create_task(self.publish_automation_list())
        return True


    async def execute_automation_run(self, automation_id, config, reason, run_id):
        error_text = None
        status = "succeeded"
        try:
            await self.publish_automation_progress(automation_id, run_id, "starting")
            validation_error = self.validate_automation_config(config)
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
            await self.execute_automation_statements(
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
                if current_task in self.automation_restart_tasks
                else "Gateway остановил выполнение сценария."
            )
            raise
        except Exception as error:
            status = "failed"
            error_text = str(error)
            print(f"[АВТОМАТИЗАЦИЯ] Ошибка в {config.get('name', automation_id)}: {error_text}")
        finally:
            current_task = asyncio.current_task()
            self.finish_automation_run(run_id, status, error_text)
            self.automation_waiting_tasks.discard(current_task)
            self.automation_restart_tasks.discard(current_task)
            # Сценарий могли уже перезапустить. Завершение старого запуска не
            # должно сбрасывать состояние нового запуска или его запрос остановки.
            if self.automation_tasks.get(automation_id) is current_task:
                with self.database_connection() as conn:
                    conn.execute("""
                        UPDATE automations SET run_status = 'idle', last_run_status = ?,
                               last_error = ? WHERE automation_id = ?
                    """, (status, error_text, automation_id))
                self.automation_tasks.pop(automation_id, None)
                self.automation_stop_requests.discard(automation_id)
            try:
                await self.publish_automation_progress(
                    automation_id, run_id, "finished", status=status, error=error_text
                )
                await self.publish_automation_list()
            except asyncio.CancelledError:
                pass


    def fire_automation_event(self, event_type, device_id=None, parameter=None, value=None):
        if self.database_restore_in_progress:
            return
        with self.database_connection() as conn:
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
                            or self.schema_values_match(value, trigger.get("value"))
                        )
                    )
                elif event_type in {"device_connected", "device_disconnected"}:
                    matches = matches and trigger.get("device") == device_id
                if matches:
                    self.start_automation_run(automation_id, config, event_type)
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                print(f"[АВТОМАТИЗАЦИЯ] Не удалось запустить сценарий {automation_id}: {error}")


    async def evaluate_automations(self, device_id, previous_parameters, current_parameters):
        if self.database_restore_in_progress or self.device_configuration_is_pending(device_id):
            return
        changed_parameters = {
            key for key, value in current_parameters.items()
            if key not in previous_parameters or not self.schema_values_match(previous_parameters[key], value)
        }
        if not changed_parameters:
            return
        with self.database_connection() as conn:
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
                            or self.schema_values_match(value, trigger["value"])
                        ):
                            self.schedule_parameter_automation_trigger(
                                automation_id, config, device_id, parameter, value
                            )
                        else:
                            self.cancel_pending_automation_trigger(automation_id)
                    continue
                invalid_reason = self.validate_automation_config(config)
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
                self.start_automation_run(automation_id, config, "threshold_crossed")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(f"[ОШИБКА] Пропуск сценария {automation_id}: {error}")


    def cancel_pending_automation_trigger(self, automation_id):
        task = self.automation_trigger_tasks.pop(automation_id, None)
        self.automation_trigger_devices.pop(automation_id, None)
        if task is not None and not task.done():
            task.cancel()


    def cancel_pending_automation_triggers_for_device(self, device_id):
        for automation_id, trigger_device in list(self.automation_trigger_devices.items()):
            if trigger_device == device_id:
                self.cancel_pending_automation_trigger(automation_id)


    def automation_cooldown_remaining(self, automation_id, cooldown_seconds):
        if cooldown_seconds <= 0:
            return 0
        with self.database_connection() as conn:
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
        self,
        automation_id, config, device_id, parameter, value
    ):
        self.cancel_pending_automation_trigger(automation_id)
        task = asyncio.create_task(
            self.wait_for_stable_automation_trigger(
                automation_id, config, device_id, parameter, value
            )
        )
        self.automation_trigger_tasks[automation_id] = task
        self.automation_trigger_devices[automation_id] = device_id


    async def wait_for_stable_automation_trigger(
        self,
        automation_id, config, device_id, parameter, value
    ):
        task = asyncio.current_task()
        trigger = config["program"]["trigger"]
        try:
            stable_seconds = trigger.get("for_seconds", 0)
            if stable_seconds:
                await asyncio.sleep(stable_seconds)

            while self.automation_trigger_tasks.get(automation_id) is task:
                with self.database_connection() as conn:
                    row = conn.execute(
                        "SELECT enabled, config_json FROM automations WHERE automation_id = ?",
                        (automation_id,)
                    ).fetchone()
                if row is None or not row[0]:
                    return
                try:
                    saved_config = json.loads(row[1])
                except (TypeError, json.JSONDecodeError):
                    return
                if saved_config != config or not self.parameter_value_is_fresh(device_id, parameter):
                    return

                current_parameters = self.device_states.get(device_id, {})
                if parameter not in current_parameters:
                    return
                current_value = current_parameters[parameter]
                if not self.schema_values_match(current_value, value):
                    return
                if (
                    "value" in trigger
                    and not self.schema_values_match(current_value, trigger["value"])
                ):
                    return

                remaining = self.automation_cooldown_remaining(
                    automation_id, trigger.get("cooldown_seconds", 0)
                )
                if remaining is None:
                    return
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue

                self.start_automation_run(automation_id, config, "parameter_changed")
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[АВТОМАТИЗАЦИЯ] Не удалось проверить триггер {automation_id}: {error}")
        finally:
            if self.automation_trigger_tasks.get(automation_id) is task:
                self.automation_trigger_tasks.pop(automation_id, None)
                self.automation_trigger_devices.pop(automation_id, None)
