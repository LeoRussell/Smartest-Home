"""Validation and Schema generation for the supported ESP32-S3 profile."""

from copy import deepcopy
import json
import re
import unicodedata


# Conservative ESP32-S3 DevKitC-1 pin set. GPIO0/3/45/46 are boot
# strapping pins, GPIO19/20 are native USB, flash/PSRAM pins are omitted, and
# GPIO38/48 are reserved for components because the built-in RGB LED pin differs
# by board revision. They may be selected separately for the identify indicator.
GPIO_PINS = [1, 2, *range(4, 19), 21]
ADC_PINS = [1, 2, *range(4, 11)]

DRIVERS = {
    "gpio_output": {
        "id": "gpio_output",
        "label": "Цифровой выход",
        "category": "actuator",
        "description": "Включает или выключает цифровой выход.",
        "parameter_type": "boolean",
        "access": "read_write",
        "pins": GPIO_PINS,
        "fields": ["active_high", "initial_value"]
    },
    "digital_input": {
        "id": "digital_input",
        "label": "Цифровой вход",
        "category": "input",
        "description": "Считывает логический уровень на входе.",
        "parameter_type": "boolean",
        "access": "read_only",
        "pins": GPIO_PINS,
        "fields": ["pull", "interval_ms"]
    },
    "analog_input": {
        "id": "analog_input",
        "label": "Аналоговый вход (ADC)",
        "category": "sensor",
        "description": "Считывает ADC и линейно преобразует настроенный raw-диапазон в заданные значения.",
        "parameter_type": "integer",
        "access": "read_only",
        "pins": ADC_PINS,
        "fields": [
            "interval_ms", "unit", "raw_min", "raw_max",
            "value_min", "value_max"
        ]
    },
    "pwm_output": {
        "id": "pwm_output",
        "label": "PWM-выход",
        "category": "actuator",
        "description": "Устанавливает заполнение PWM в заданном диапазоне.",
        "parameter_type": "integer",
        "access": "read_write",
        "pins": GPIO_PINS,
        "fields": ["initial_value", "min_value", "max_value"]
    }
}

DEVICE_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
ALLOWED_PULLS = {"none", "up", "down"}
MAX_COMPONENTS = 8
MAX_CONFIGURATION_BYTES = 3000


class ConfigurationValidationError(str):
    """Keep the existing text error contract while identifying its form field."""

    def __new__(cls, message, path):
        instance = super().__new__(cls, message)
        instance.path = path
        return instance


def configuration_error(path, message):
    return None, None, ConfigurationValidationError(message, path)


def public_driver_catalog(driver_ids):
    """Return only known drivers actually advertised by this firmware."""
    if not isinstance(driver_ids, list):
        return []
    result = []
    seen = set()
    for driver_id in driver_ids:
        if isinstance(driver_id, str) and driver_id in DRIVERS and driver_id not in seen:
            result.append(deepcopy(DRIVERS[driver_id]))
            seen.add(driver_id)
    return result


def validate_device_configuration(configuration, driver_ids):
    """Return (normalized_config, generated_schema, error); reject unsafe data."""
    if (
        not isinstance(configuration, dict)
        or not {"format_version", "components"}.issubset(configuration)
        or set(configuration) - {"format_version", "components", "identify_indicator"}
    ):
        return configuration_error("configuration", "Конфигурация должна содержать format_version и components.")
    if type(configuration.get("format_version")) is not int or configuration["format_version"] != 1:
        return configuration_error("format_version", "Поддерживается только версия конфигурации 1.")

    components = configuration.get("components")
    if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
        return configuration_error("components", f"Можно настроить от 0 до {MAX_COMPONENTS} компонентов.")

    advertised = {
        driver_id for driver_id in driver_ids
        if isinstance(driver_id, str)
    } if isinstance(driver_ids, list) else set()
    component_ids = set()
    parameter_ids = set()
    used_pins = set()
    normalized = []
    identify_indicator = configuration.get("identify_indicator")
    normalized_indicator = None
    if identify_indicator is not None:
        if not isinstance(identify_indicator, dict):
            return configuration_error("identify_indicator", "Identify indicator must be an object.")
        indicator_driver = identify_indicator.get("driver")
        indicator_pin = identify_indicator.get("pin")
        built_in = identify_indicator.get("built_in", indicator_driver == "neopixel")
        if type(built_in) is not bool:
            return configuration_error("identify_indicator.built_in", "Identify indicator built_in must be boolean.")
        if indicator_driver == "gpio":
            if (
                set(identify_indicator) - {"driver", "pin", "active_high", "built_in"}
                or not {"driver", "pin", "active_high"}.issubset(identify_indicator)
                or type(indicator_pin) is not int or indicator_pin not in GPIO_PINS
                or type(identify_indicator.get("active_high")) is not bool
            ):
                path = "identify_indicator.pin" if type(indicator_pin) is not int or indicator_pin not in GPIO_PINS else "identify_indicator.active_high"
                return configuration_error(path, "GPIO identify indicator requires a supported GPIO and boolean active_high.")
            normalized_indicator = {
                "driver": "gpio", "pin": indicator_pin,
                "active_high": identify_indicator["active_high"],
                "built_in": built_in
            }
        elif indicator_driver == "neopixel":
            if (
                set(identify_indicator) - {"driver", "pin", "brightness", "built_in"}
                or not {"driver", "pin", "brightness"}.issubset(identify_indicator)
                or type(indicator_pin) is not int or indicator_pin not in (38, 48)
                or type(identify_indicator.get("brightness")) is not int
                or not 1 <= identify_indicator["brightness"] <= 255
            ):
                path = "identify_indicator.pin" if type(indicator_pin) is not int or indicator_pin not in (38, 48) else "identify_indicator.brightness"
                return configuration_error(path, "NeoPixel identify indicator requires GPIO 38 or 48 and brightness 1..255.")
            normalized_indicator = {
                "driver": "neopixel", "pin": indicator_pin,
                "brightness": identify_indicator["brightness"],
                "built_in": built_in
            }
        else:
            return configuration_error("identify_indicator.driver", "Identify indicator driver must be gpio or neopixel.")
        if indicator_pin in used_pins:
            return configuration_error("identify_indicator.pin", f"GPIO {indicator_pin} is already assigned to another component.")
        used_pins.add(indicator_pin)
    schema = {
        "format_version": 1,
        "components": {},
        "parameters": {},
        "actions": {},
        "capabilities": {"universal_profile": True}
    }

    for index, raw in enumerate(components, start=1):
        prefix = f"components.{index - 1}"
        if not isinstance(raw, dict):
            return configuration_error(prefix, f"Компонент {index}: ожидается объект.")
        driver_id = raw.get("driver")
        if not isinstance(driver_id, str):
            return configuration_error(f"{prefix}.driver", f"Компонент {index}: укажите ID драйвера строкой.")
        driver = DRIVERS.get(driver_id)
        if driver is None or driver_id not in advertised:
            return configuration_error(f"{prefix}.driver", f"Компонент {index}: прошивка не объявила драйвер {driver_id!r}.")

        allowed_keys = {
            "id", "driver", "label", "parameter_id", "parameter_label", "pin"
        } | set(driver["fields"])
        if set(raw) - allowed_keys:
            return configuration_error(prefix, f"Компонент {index}: найдены неподдерживаемые поля.")

        component_id = raw.get("id")
        parameter_id = raw.get("parameter_id")
        label = raw.get("label")
        parameter_label = raw.get("parameter_label")
        if not isinstance(component_id, str) or not DEVICE_ID_PATTERN.fullmatch(component_id):
            return configuration_error(f"{prefix}.id", f"Компонент {index}: некорректный ID.")
        if component_id in component_ids:
            return configuration_error(f"{prefix}.id", f"ID компонента {component_id!r} повторяется.")
        if not isinstance(parameter_id, str) or not DEVICE_ID_PATTERN.fullmatch(parameter_id):
            return configuration_error(f"{prefix}.parameter_id", f"Компонент {component_id}: некорректный ID параметра.")
        if parameter_id in parameter_ids:
            return configuration_error(f"{prefix}.parameter_id", f"ID параметра {parameter_id!r} повторяется.")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            return configuration_error(f"{prefix}.label", f"Компонент {component_id}: укажите название до 80 символов.")
        if not isinstance(parameter_label, str) or not 1 <= len(parameter_label.strip()) <= 80:
            return configuration_error(f"{prefix}.parameter_label", f"Компонент {component_id}: укажите название параметра до 80 символов.")
        try:
            label_bytes = len(label.strip().encode("utf-8"))
            parameter_label_bytes = len(parameter_label.strip().encode("utf-8"))
        except UnicodeEncodeError:
            return configuration_error(prefix, f"Компонент {component_id}: названия должны содержать корректный UTF-8 текст.")
        if any(
            unicodedata.category(character) == "Cc"
            for character in label + parameter_label
        ):
            return configuration_error(prefix, f"Компонент {component_id}: в названиях нельзя использовать управляющие символы.")
        if label_bytes > 80 or parameter_label_bytes > 80:
            return configuration_error(prefix, f"Компонент {component_id}: каждое название должно занимать не более 80 байт UTF-8.")

        pin = raw.get("pin")
        if type(pin) is not int or pin not in driver["pins"]:
            return configuration_error(f"{prefix}.pin", f"Компонент {component_id}: GPIO не поддерживается драйвером {driver_id}.")
        if pin in used_pins:
            return configuration_error(f"{prefix}.pin", f"GPIO {pin} назначен нескольким компонентам.")

        component = {
            "id": component_id,
            "driver": driver_id,
            "label": label.strip(),
            "parameter_id": parameter_id,
            "parameter_label": parameter_label.strip(),
            "pin": pin
        }
        if driver_id == "gpio_output":
            if type(raw.get("active_high")) is not bool or type(raw.get("initial_value")) is not bool:
                return configuration_error(f"{prefix}.active_high" if type(raw.get("active_high")) is not bool else f"{prefix}.initial_value", f"Компонент {component_id}: active_high и initial_value должны быть boolean.")
            component.update({
                "active_high": raw["active_high"],
                "initial_value": raw["initial_value"]
            })
        elif driver_id == "digital_input":
            pull = raw.get("pull")
            interval = raw.get("interval_ms")
            if not isinstance(pull, str) or pull not in ALLOWED_PULLS:
                return configuration_error(f"{prefix}.pull", f"Компонент {component_id}: pull должен быть none, up или down.")
            if type(interval) is not int or not 100 <= interval <= 60000:
                return configuration_error(f"{prefix}.interval_ms", f"Компонент {component_id}: интервал должен быть от 100 до 60000 мс.")
            component.update({"pull": pull, "interval_ms": interval})
        elif driver_id == "analog_input":
            interval = raw.get("interval_ms")
            unit = raw.get("unit", "raw")
            raw_min = raw.get("raw_min", 0)
            raw_max = raw.get("raw_max", 4095)
            value_min = raw.get("value_min", 0)
            value_max = raw.get("value_max", 4095)
            if type(interval) is not int or not 250 <= interval <= 60000:
                return configuration_error(f"{prefix}.interval_ms", f"Компонент {component_id}: интервал должен быть от 250 до 60000 мс.")
            if (
                type(raw_min) is not int or type(raw_max) is not int
                or not 0 <= raw_min < raw_max <= 4095
                or type(value_min) is not int or type(value_max) is not int
                or not -1_000_000 <= value_min < value_max <= 1_000_000
            ):
                if type(raw_min) is not int or not 0 <= raw_min <= 4095:
                    field = "raw_min"
                elif type(raw_max) is not int or not raw_min < raw_max <= 4095:
                    field = "raw_max"
                elif type(value_min) is not int or not -1_000_000 <= value_min <= 1_000_000:
                    field = "value_min"
                else:
                    field = "value_max"
                return configuration_error(f"{prefix}.{field}", f"Компонент {component_id}: укажите упорядоченные raw и итоговые диапазоны.")
            if not isinstance(unit, str):
                return configuration_error(f"{prefix}.unit", f"Компонент {component_id}: единица измерения должна быть текстом.")
            try:
                unit_bytes = len(unit.strip().encode("utf-8"))
            except UnicodeEncodeError:
                return configuration_error(f"{prefix}.unit", f"Компонент {component_id}: единица измерения должна содержать корректный UTF-8 текст.")
            if any(unicodedata.category(character) == "Cc" for character in unit):
                return configuration_error(f"{prefix}.unit", f"Компонент {component_id}: в единице измерения нельзя использовать управляющие символы.")
            if unit_bytes > 24:
                return configuration_error(f"{prefix}.unit", f"Компонент {component_id}: единица измерения должна занимать не более 24 байт UTF-8.")
            component.update({
                "interval_ms": interval,
                "unit": unit.strip() or "raw",
                "raw_min": raw_min,
                "raw_max": raw_max,
                "value_min": value_min,
                "value_max": value_max
            })
        elif driver_id == "pwm_output":
            initial_value = raw.get("initial_value")
            minimum = raw.get("min_value", 0)
            maximum = raw.get("max_value", 255)
            if (
                type(minimum) is not int or type(maximum) is not int
                or not 0 <= minimum < maximum <= 255
                or type(initial_value) is not int
                or not minimum <= initial_value <= maximum
            ):
                if type(minimum) is not int or not 0 <= minimum < 255:
                    field = "min_value"
                elif type(maximum) is not int or not minimum < maximum <= 255:
                    field = "max_value"
                else:
                    field = "initial_value"
                return configuration_error(f"{prefix}.{field}", f"Компонент {component_id}: PWM диапазон должен быть внутри 0–255, а начальное значение — внутри выбранного диапазона.")
            component.update({
                "initial_value": initial_value,
                "min_value": minimum,
                "max_value": maximum
            })

        component_ids.add(component_id)
        parameter_ids.add(parameter_id)
        used_pins.add(pin)
        normalized.append(component)
        schema["components"][component_id] = {
            "label": component["label"],
            "category": driver["category"],
            "description": driver["description"]
        }
        definition = {
            "label": component["parameter_label"],
            "component": component_id,
            "type": driver["parameter_type"],
            "access": driver["access"]
        }
        if driver_id == "gpio_output":
            definition["control"] = "toggle"
        elif driver_id == "analog_input":
            definition.update({
                "min": component["value_min"],
                "max": component["value_max"],
                "step": 1,
                "precision": 0,
                "unit": component["unit"]
            })
        elif driver_id == "pwm_output":
            definition.update({
                "min": component["min_value"],
                "max": component["max_value"],
                "step": 1,
                "precision": 0,
                "unit": "raw",
                "control": "slider"
            })
        schema["parameters"][parameter_id] = definition

    normalized_configuration = {"format_version": 1, "components": normalized}
    if normalized_indicator is not None:
        normalized_configuration["identify_indicator"] = normalized_indicator
        schema["capabilities"]["identify"] = True
        if normalized_indicator["built_in"]:
            schema["capabilities"]["built_in_indicator"] = True
        schema["actions"]["identify"] = {
            "label": "Найти устройство",
            "description": "Мигает индикатором, выбранным для этого устройства."
        }
    if len(json.dumps(
        normalized_configuration, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False
    ).encode("utf-8")) > MAX_CONFIGURATION_BYTES:
        return configuration_error("configuration", f"Размер конфигурации не должен превышать {MAX_CONFIGURATION_BYTES} байт.")
    return normalized_configuration, schema, None
