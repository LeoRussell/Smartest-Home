"""Bounded automation preview over isolated values; never dispatches device work."""

from copy import deepcopy
import json
import sqlite3

from automation import AutomationMixin
from errors import AutomationExecutionError


MAX_STEPS = 128
MAX_REQUEST_BYTES = 48 * 1024
MAX_TRACE_BYTES = 40 * 1024
MAX_REPORT_BYTES = 60 * 1024


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _program(config):
    if config.get("version") == 2:
        return deepcopy(config["program"])
    trigger, target = config["trigger"], config["target"]
    operator = {"above": "gt", "below": "lt", "at_least": "ge", "at_most": "le"}[trigger["operator"]]
    action = (
        {"type": "set_parameter", "device": target["device"], "parameter": target["name"],
         "value": {"type": "constant", "value": target["value"]}}
        if target["kind"] == "parameter" else
        {"type": "invoke_action", "device": target["device"], "action": target["name"], "arguments": target["arguments"]}
    )
    return {"version": 1,
            "trigger": {"type": "parameter_changed", "device": trigger["device"], "parameter": trigger["parameter"]},
            "body": [{"type": "if", "branches": [{"condition": {
                "type": "compare", "operator": operator,
                "left": {"type": "parameter", "device": trigger["device"], "parameter": trigger["parameter"]},
                "right": {"type": "constant", "value": trigger["threshold"]}}, "then": [action]}], "else": []}]}


def _references(program):
    devices, parameters = set(), set()

    def expression(value):
        kind = value["type"]
        if kind == "parameter":
            devices.add(value["device"])
            key = (value["device"], value["parameter"])
            parameters.add(key)
        elif kind == "device_connected":
            devices.add(value["device"])
        elif kind == "not":
            expression(value["operand"])
        elif kind in {"and", "or"}:
            for item in value["operands"]:
                expression(item)
        elif kind in {"compare", "arithmetic"}:
            expression(value["left"])
            expression(value["right"])

    def statements(body):
        for item in body:
            if item["type"] == "set_parameter":
                devices.add(item["device"])
                parameters.add((item["device"], item["parameter"]))
                expression(item["value"])
            elif item["type"] == "invoke_action":
                devices.add(item["device"])
            elif item["type"] == "if":
                for branch in item["branches"]:
                    expression(branch["condition"])
                    statements(branch["then"])
                statements(item["else"])

    trigger = program["trigger"]
    if "device" in trigger:
        devices.add(trigger["device"])
    if trigger["type"] == "parameter_changed":
        key = (trigger["device"], trigger["parameter"])
        parameters.add(key)
    statements(program["body"])
    return devices, parameters


class _Sandbox:
    """Only the pure expression evaluator is shared with the real interpreter."""

    evaluate_automation_expression = AutomationMixin.evaluate_automation_expression

    def __init__(self, gateway, program, message):
        referenced, parameters = _references(program)
        self.device_metadata = {device: deepcopy(gateway.automation_device_metadata(device)) for device in referenced}
        self.device_states = {device: {} for device in referenced}
        self.connected = {device: True for device in referenced}
        if program["trigger"]["type"] == "device_disconnected":
            self.connected[program["trigger"]["device"]] = False
        self.is_schema_value = gateway.is_schema_value
        self.value_within_schema_limits = gateway.value_within_schema_limits
        self.schema_values_match = gateway.schema_values_match
        self.sources = {}
        self.initial_values = {}
        self.changed = set()
        self.unknown_devices = set()
        self.steps = []
        self.trace_bytes = 0
        self.elapsed = 0
        self.warnings = [
            "Соединения моделируются: устройства по умолчанию подключены, кроме источника события отключения. Фактическая доступность не проверяется.",
            "Событие запуска считается поступившим. Заданные показания остаются постоянными во время виртуальных пауз.",
            "Команды установки в модели считаются успешными; доставка и подтверждения устройств не проверяются.",
        ]
        for device, parameter in sorted(parameters):
            definition = self.definition(device, parameter)
            if gateway.parameter_value_is_fresh(device, parameter) and not gateway.device_configuration_is_pending(device):
                value = gateway.device_states.get(device, {}).get(parameter)
                if self.is_schema_value(value, definition) and self.value_within_schema_limits(value, definition):
                    self.device_states[device][parameter] = deepcopy(value)
                    self.sources[(device, parameter)] = "live"
        overrides = message.get("parameters", [])
        connections = message.get("connections", [])
        if not isinstance(overrides, list) or len(overrides) > 200:
            raise ValueError("Задайте не больше 200 виртуальных показаний.")
        if not isinstance(connections, list) or len(connections) > 100:
            raise ValueError("Задайте не больше 100 состояний соединения.")
        seen = set()
        for item in overrides:
            if not isinstance(item, dict) or set(item) != {"device", "parameter", "value"}:
                raise ValueError("Некорректное виртуальное показание.")
            device, parameter = item["device"], item["parameter"]
            if not isinstance(device, str) or not isinstance(parameter, str) or (device, parameter) not in parameters:
                raise ValueError("Виртуальное показание ссылается на параметр вне сценария.")
            key = (device, parameter)
            if key in seen:
                raise ValueError("Виртуальное показание параметра задано дважды.")
            seen.add(key)
            definition = self.definition(device, parameter)
            if not self.is_schema_value(item["value"], definition) or not self.value_within_schema_limits(item["value"], definition):
                raise ValueError(f"Значение {device}.{parameter} не соответствует Schema.")
            self.device_states[device][parameter] = deepcopy(item["value"])
            self.sources[key] = "override"
        seen.clear()
        for item in connections:
            if (
                not isinstance(item, dict) or set(item) != {"device", "connected"}
                or not isinstance(item["device"], str) or item["device"] not in referenced
                or type(item["connected"]) is not bool
            ):
                raise ValueError("Некорректное виртуальное состояние соединения.")
            if item["device"] in seen:
                raise ValueError("Состояние соединения устройства задано дважды.")
            seen.add(item["device"])
            self.connected[item["device"]] = item["connected"]
        self.initial_values = deepcopy(self.device_states)

    def load_device_metadata(self, device):
        return self.device_metadata.get(device)

    def device_is_connected(self, device):
        return self.connected.get(device, False)

    def device_configuration_is_pending(self, device):
        return False

    def parameter_value_is_fresh(self, device, parameter):
        if parameter not in self.device_states.get(device, {}) and device in self.unknown_devices:
            raise AutomationExecutionError(f"Значение {device}.{parameter} неизвестно после вызова действия прошивки.")
        return parameter in self.device_states.get(device, {})

    def definition(self, device, parameter):
        return self.device_metadata[device]["schema"]["parameters"][parameter]

    def record(self, kind, details=None, block_id=None, status="succeeded", error=None):
        if len(self.steps) >= MAX_STEPS:
            raise AutomationExecutionError("Достигнут предел тестового запуска: 128 шагов.")
        step = {"sequence": len(self.steps) + 1, "type": kind, "status": status,
                "at_seconds": self.elapsed, "block_id": block_id, "details": deepcopy(details or {})}
        if error:
            step["error"] = error
        size = _json_size(step)
        if self.trace_bytes + size > MAX_TRACE_BYTES:
            raise AutomationExecutionError("Достигнут предел размера отчёта тестового запуска.")
        self.trace_bytes += size
        self.steps.append(step)

    def trigger(self, trigger):
        details = {"trigger": deepcopy(trigger), "assumed_event": True, "matched": True}
        kind = trigger["type"]
        if kind == "parameter_changed":
            device, parameter = trigger["device"], trigger["parameter"]
            if not self.device_is_connected(device):
                details["matched"] = False
                details["reason"] = "Виртуальное устройство отключено."
            else:
                value = self.evaluate_automation_expression({"type": "parameter", "device": device, "parameter": parameter})
                details["value"] = value
                if "value" in trigger:
                    details["matched"] = self.schema_values_match(value, trigger["value"])
            if trigger.get("cooldown_seconds"):
                self.warnings.append("Пауза между реальными запусками не моделируется: тест не использует историю выполнений.")
        elif kind in {"device_connected", "device_disconnected"}:
            details["matched"] = self.device_is_connected(trigger["device"]) == (kind == "device_connected")
        self.record("trigger", details, status="succeeded" if details["matched"] else "skipped")
        if details["matched"] and trigger.get("for_seconds"):
            self.record("wait", {"seconds": trigger["for_seconds"], "source": "trigger_stability"})
            self.elapsed += trigger["for_seconds"]
        return details["matched"]

    def execute(self, statements):
        for statement in statements:
            kind, block = statement["type"], statement.get("block_id")
            try:
                if kind == "if":
                    matched = False
                    for index, branch in enumerate(statement["branches"], 1):
                        result = bool(self.evaluate_automation_expression(branch["condition"]))
                        self.record("condition", {"branch": index, "result": result}, block)
                        if result:
                            self.execute(branch["then"])
                            matched = True
                            break
                    if not matched:
                        if statement["else"]:
                            self.record("otherwise", {}, block)
                        self.execute(statement["else"])
                elif kind == "wait":
                    self.record("wait", {"seconds": statement["seconds"]}, block)
                    self.elapsed += statement["seconds"]
                elif kind == "set_parameter":
                    device, parameter = statement["device"], statement["parameter"]
                    if not self.device_is_connected(device):
                        raise AutomationExecutionError(f"Виртуальное устройство {device} отключено; команда не моделируется.")
                    value = self.evaluate_automation_expression(statement["value"])
                    definition = self.definition(device, parameter)
                    if not self.is_schema_value(value, definition) or not self.value_within_schema_limits(value, definition):
                        raise AutomationExecutionError(f"Результат вычисления {device}.{parameter} не соответствует Schema.")
                    details = {"device": device, "parameter": parameter, "value": deepcopy(value), "simulated": True}
                    if parameter in self.device_states[device]:
                        details["previous_value"] = deepcopy(self.device_states[device][parameter])
                    self.record(kind, details, block)
                    self.device_states[device][parameter] = deepcopy(value)
                    self.changed.add((device, parameter))
                elif kind == "invoke_action":
                    device, action = statement["device"], statement["action"]
                    if not self.device_is_connected(device):
                        raise AutomationExecutionError(f"Виртуальное устройство {device} отключено; действие не моделируется.")
                    self.record(kind, {"device": device, "action": action,
                                      "arguments": statement["arguments"], "simulated": True, "effects": "unknown"}, block)
                    warning = "Действия прошивки только перечисляются: их внутренние эффекты и изменения показаний неизвестны модели."
                    if warning not in self.warnings:
                        self.warnings.append(warning)
                    self.device_states[device].clear()
                    self.unknown_devices.add(device)
                else:
                    raise AutomationExecutionError("В тестовом запуске встретился неподдерживаемый блок.")
            except (AutomationExecutionError, ArithmeticError, TypeError, ValueError) as error:
                # Do not duplicate a failure propagated from a nested branch.
                if not self.steps or self.steps[-1]["status"] != "failed":
                    try:
                        self.record(kind, {}, block, "failed", str(error))
                    except AutomationExecutionError:
                        pass
                raise


def simulate_automation(gateway, message):
    """Return a complete preview. The real Gateway is a read-only input source."""
    request_id = message.get("request_id")
    request_id = request_id if isinstance(request_id, str) and 1 <= len(request_id) <= 128 else None
    result = {"type": "automation_simulation", "request_id": request_id, "success": False,
              "status": "failed", "simulated": True, "elapsed_seconds": 0,
              "steps": [], "inputs": [], "connections": [], "warnings": [], "final_parameters": []}
    sandbox = None
    try:
        if message.get("request_id") is not None and request_id is None:
            raise ValueError("Некорректный идентификатор тестового запроса.")
        if gateway.database_restore_in_progress:
            raise ValueError("База Gateway восстанавливается. Повторите тест после восстановления.")
        if _json_size(message) > MAX_REQUEST_BYTES:
            raise ValueError("Тестовый запрос слишком большой (лимит 48 КБ).")
        if ("config" in message) == ("automation_id" in message):
            raise ValueError("Выберите сохранённый сценарий или передайте черновик.")
        if "config" in message:
            config = deepcopy(message["config"])
        else:
            identity = message["automation_id"]
            if not isinstance(identity, str) or not 1 <= len(identity) <= 100:
                raise ValueError("Некорректный идентификатор сценария.")
            with gateway.database_connection() as connection:
                row = connection.execute("SELECT config_json FROM automations WHERE automation_id = ?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Сохранённый сценарий не найден.")
            config = json.loads(row[0])
            result["automation_id"] = identity
        validation = gateway.validate_automation_config(config)
        if validation:
            raise ValueError(validation)
        program = _program(config)
        result.update(scenario_name=config["name"], program_tag=gateway.automation_program_tag(program))
        sandbox = _Sandbox(gateway, program, message)
        if sandbox.trigger(program["trigger"]):
            sandbox.execute(program["body"])
            result.update(success=True, status="completed")
        else:
            result.update(success=True, status="not_triggered")
    except (AutomationExecutionError, ArithmeticError, TypeError, ValueError, KeyError, RecursionError, sqlite3.Error) as error:
        result["message"] = str(error) or "Не удалось выполнить тестовый запуск."
    if sandbox is not None:
        result.update(steps=sandbox.steps, elapsed_seconds=sandbox.elapsed, warnings=sandbox.warnings,
                      inputs=[{"device": device, "parameter": parameter, "value": value,
                               "source": sandbox.sources[(device, parameter)]}
                              for device, values in sorted(sandbox.initial_values.items())
                              for parameter, value in sorted(values.items())],
                      connections=[{"device": device, "connected": value} for device, value in sorted(sandbox.connected.items())],
                      final_parameters=[{"device": device, "parameter": parameter,
                                         "value": sandbox.device_states[device][parameter]}
                                        for device, parameter in sorted(sandbox.changed)
                                        if parameter in sandbox.device_states.get(device, {})])
    if _json_size(result) > MAX_REPORT_BYTES:
        result.update(success=False, status="failed", message="Отчёт теста слишком большой; уменьшите число входных значений или действий.", inputs=[], final_parameters=[])
        if _json_size(result) > MAX_REPORT_BYTES:
            result["connections"] = []
    return result


async def handle_simulation_message(gateway, websocket, message):
    if not isinstance(message, dict) or message.get("action") != "simulate_automation":
        return False
    try:
        result = simulate_automation(gateway, message)
    except Exception as error:
        print(f"[SIMULATION] Preview failed: {type(error).__name__}")
        request_id = message.get("request_id")
        result = {"type": "automation_simulation", "simulated": True, "success": False,
                  "status": "failed", "steps": [], "elapsed_seconds": 0,
                  "request_id": request_id if isinstance(request_id, str) and len(request_id) <= 128 else None,
                  "message": "Не удалось выполнить тестовый запуск. Проверьте сценарий и повторите запрос."}
    await gateway.send_json(websocket, result)
    return True
