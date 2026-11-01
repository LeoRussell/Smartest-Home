"""Logical Schema validation, review and control compatibility."""

import hashlib
import json
import math
import re
from device_management import validate_schema_common_commands
from device_configuration import validate_device_configuration


class SchemaMixin:
    def schema_values_match(self, actual, expected):
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


    def is_schema_value(self, value, parameter_schema):
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


    def value_within_schema_limits(self, value, definition):
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


    def validate_action_argument_definition(self, key, definition):
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


    def validate_action_arguments(self, action_definition, arguments):
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
            if not self.is_schema_value(value, definition):
                return False
            if not self.value_within_schema_limits(value, definition):
                return False

        return True


    def validate_schema(self, schema):
        try:
            serialized_size = len(
                json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError):
            return False

        if serialized_size > self.MAX_SCHEMA_BYTES:
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
                not self.validate_action_argument_definition(argument_id, argument_definition)
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
                    or not self.is_schema_value(mode["status_value"], status_definition)
                ):
                    return False

            conflicts = mode.get("conflicts_with", [])
            if (
                not isinstance(conflicts, list) or len(conflicts) > 8
                or any(not isinstance(item, str) or item == mode_id or item not in mode_ids for item in conflicts)
                or len(set(conflicts)) != len(conflicts)
            ):
                return False

        return validate_schema_common_commands(self, schema)


    def default_legacy_schema(self):
        # Без Schema Gateway не знает, безопасно ли менять параметр.
        # Состояние старых устройств добавится как read_only при первом отчёте.
        return {"parameters": {}, "actions": {}}


    def inferred_definition(self, key, value):
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


    def validate_parameters(self, schema, parameters, allow_unknown=False):
        if not isinstance(parameters, dict) or len(parameters) > 64:
            return False

        definitions = schema.get("parameters", {})
        for key, value in parameters.items():
            definition = definitions.get(key)
            if definition is None:
                if allow_unknown and self.inferred_definition(key, value) is not None:
                    continue
                return False

            if (
                not self.is_schema_value(value, definition)
                or not self.value_within_schema_limits(value, definition)
            ):
                return False

        return True


    def validate_parameters_during_schema_review(
        self,
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
                    self.is_schema_value(value, definition)
                    and self.value_within_schema_limits(value, definition)
                ):
                    valid_under_one_schema = True
                    break
            if not valid_under_one_schema:
                return False

        return True


    def accepted_parameters_during_schema_review(
        self,
        accepted_schema, proposed_schema, parameters
    ):
        if not self.validate_parameters_during_schema_review(
            accepted_schema, proposed_schema, parameters
        ):
            return None

        definitions = accepted_schema.get("parameters", {})
        return {
            key: value
            for key, value in parameters.items()
            if key in definitions
            and self.is_schema_value(value, definitions[key])
            and self.value_within_schema_limits(value, definitions[key])
        }


    def parameter_has_zero_one_values(self, definition):
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


    def is_parameter_ui_preference_supported(self, control, definition):
        if not isinstance(definition, dict) or definition.get("access") != "read_write":
            return False

        parameter_type = definition.get("type")
        if control == "toggle":
            return parameter_type == "boolean"
        if control == "list":
            return self.parameter_has_zero_one_values(definition)
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


    def validate_parameter_ui_preferences(self, preferences, schema):
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
            if control != "schema" and not self.is_parameter_ui_preference_supported(control, definition):
                return None, f"Вид элемента {control!r} несовместим с параметром {parameter_id!r}."
            normalized[parameter_id] = control
        return normalized, None


    def validate_device_configuration_request(self, device_id, configuration, ui_preferences=None):
        metadata = self.automation_device_metadata(device_id)
        if metadata is None or metadata.get("device_profile") != "universal":
            return None, None, None, [{
                "path": "device", "message": "Устройство не поддерживает универсальную конфигурацию."
            }]
        if metadata.get("board_profile") != "esp32-s3-devkitc-1":
            return None, None, None, [{
                "path": "device", "message": "Для этой версии конструктора поддерживается профиль ESP32-S3 DevKitC-1."
            }]
        normalized, schema, error = validate_device_configuration(
            configuration, metadata.get("driver_ids", [])
        )
        if error is not None:
            return None, None, None, [{
                "path": getattr(error, "path", "configuration"), "message": str(error)
            }]
        preferences, error = self.validate_parameter_ui_preferences(ui_preferences, schema)
        if error is not None:
            path = "ui_preferences"
            if isinstance(ui_preferences, dict):
                for parameter_id, control in ui_preferences.items():
                    if self.validate_parameter_ui_preferences({parameter_id: control}, schema)[1]:
                        path += f".{parameter_id}"
                        break
            return None, None, None, [{"path": path, "message": error}]
        return normalized, schema, preferences, []


    def schemas_match(self, left, right):
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


    def schema_proposal_id(self, schema, schema_version):
        payload = json.dumps(
            {"schema": schema, "schema_version": schema_version},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:24]


    def offer_device_schema(self, device_id, offered_metadata, is_legacy=False):
        self.deferred_device_schemas.pop(device_id, None)
        previous = self.load_device_metadata(device_id)
        if previous is None:
            self.save_device_metadata(device_id, offered_metadata)
            self.pending_device_schemas.pop(device_id, None)
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
            self.save_device_metadata(device_id, active_metadata)
            return "preserved"

        has_previous_schema = previous.get("schema_version") is not None
        same_schema = self.schemas_match(previous["schema"], offered_metadata["schema"])

        if not has_previous_schema or same_schema:
            accepted_metadata = dict(offered_metadata)
            if same_schema:
                previous_version = previous.get("schema_version") or 0
                offered_version = offered_metadata.get("schema_version") or 0
                accepted_metadata["schema_version"] = max(
                    previous_version, offered_version
                ) or None
            self.save_device_metadata(device_id, accepted_metadata)
            self.pending_device_schemas.pop(device_id, None)
            return "saved"

        proposal_id = self.schema_proposal_id(
            offered_metadata["schema"], offered_metadata.get("schema_version")
        )
        active_metadata = dict(previous)
        for key in (
            "name", "protocol_version", "firmware_version",
            "command_dedup", "trust_policy"
        ):
            active_metadata[key] = offered_metadata.get(key)
        self.save_device_metadata(device_id, active_metadata)

        self.pending_device_schemas[device_id] = {
            "proposal_id": proposal_id,
            "schema": offered_metadata["schema"],
            "schema_version": offered_metadata.get("schema_version"),
            "metadata": offered_metadata
        }
        return "pending"


    def public_schema_proposal(self, device_id):
        proposal = self.pending_device_schemas.get(device_id)
        if proposal is None:
            return None
        return {
            "proposal_id": proposal["proposal_id"],
            "schema": proposal["schema"],
            "schema_version": proposal.get("schema_version")
        }
