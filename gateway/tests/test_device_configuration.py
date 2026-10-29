import unittest
import sys
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from websockets.http11 import Headers, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from device_configuration import (
    DRIVERS,
    public_driver_catalog,
    validate_device_configuration,
)
from main import handle_http_request, validate_parameter_ui_preferences, validate_schema


DRIVER_IDS = list(DRIVERS)


class DeviceConfigurationTests(unittest.TestCase):
    def validate(self, components):
        return validate_device_configuration(
            {"format_version": 1, "components": components}, DRIVER_IDS
        )

    def test_empty_configuration_is_valid_and_emits_universal_schema(self):
        normalized, schema, error = self.validate([])

        self.assertIsNone(error)
        self.assertEqual(normalized, {"format_version": 1, "components": []})
        self.assertTrue(validate_schema(schema))
        self.assertTrue(schema["capabilities"]["universal_profile"])

    def test_gateway_driver_descriptions_match_universal_firmware(self):
        firmware_path = (
            Path(__file__).resolve().parents[2]
            / "esp32" / "universal" / "src" / "UniversalDevice.cpp"
        )
        source = firmware_path.read_text(encoding="utf-8")
        schema_builder = source[
            source.index("void UniversalDevice::fillSchema"):
            source.index("void UniversalDevice::sendState")
        ]
        driver_cases = {
            "gpio_output": "GpioOutput",
            "digital_input": "DigitalInput",
            "analog_input": "AnalogInput",
            "pwm_output": "PwmOutput",
        }
        for driver_id, case_name in driver_cases.items():
            case_start = schema_builder.index(f"case Driver::{case_name}:")
            case = schema_builder[case_start:].split("break;", 1)[0]
            match = re.search(
                r'componentSchema\["description"\]\s*=\s*"([^"]*)"',
                case,
            )
            self.assertIsNotNone(match, driver_id)
            self.assertEqual(DRIVERS[driver_id]["description"], match.group(1))

    def test_drivers_generate_schema_from_logical_ids(self):
        components = [
            {
                "id": "lamp", "driver": "gpio_output", "label": "Лампа",
                "parameter_id": "enabled", "parameter_label": "Включена",
                "pin": 4, "active_high": True, "initial_value": False,
            },
            {
                "id": "light_sensor", "driver": "analog_input",
                "label": "Датчик света", "parameter_id": "lux_raw",
                "parameter_label": "Освещённость", "pin": 5,
                "interval_ms": 1000, "unit": "lx",
            },
        ]

        normalized, schema, error = self.validate(components)

        self.assertIsNone(error)
        self.assertEqual(normalized["components"][0]["pin"], 4)
        self.assertEqual(schema["parameters"]["enabled"]["type"], "boolean")
        self.assertEqual(schema["parameters"]["lux_raw"]["unit"], "lx")
        self.assertTrue(validate_schema(schema))

    def test_digital_input_and_pwm_schema_are_valid(self):
        components = [
            {
                "id": "button", "driver": "digital_input", "label": "Кнопка",
                "parameter_id": "pressed", "parameter_label": "Нажата",
                "pin": 4, "pull": "up", "interval_ms": 250,
            },
            {
                "id": "dimmer", "driver": "pwm_output", "label": "Диммер",
                "parameter_id": "duty", "parameter_label": "Заполнение PWM",
                "pin": 5, "initial_value": 64, "min_value": 16, "max_value": 200,
            },
        ]

        _, schema, error = self.validate(components)

        self.assertIsNone(error)
        self.assertEqual(schema["components"]["button"]["category"], "input")
        self.assertEqual(schema["parameters"]["pressed"]["access"], "read_only")
        self.assertEqual(schema["parameters"]["duty"]["min"], 16)
        self.assertEqual(schema["parameters"]["duty"]["max"], 200)
        self.assertEqual(schema["parameters"]["duty"]["unit"], "raw")
        self.assertTrue(validate_schema(schema))

    def test_adc_calibration_generates_scaled_value_schema(self):
        component = {
            "id": "light_sensor", "driver": "analog_input",
            "label": "Датчик света", "parameter_id": "illuminance",
            "parameter_label": "Освещённость", "pin": 5,
            "interval_ms": 1000, "unit": "lx", "raw_min": 200,
            "raw_max": 3900, "value_min": 0, "value_max": 1200,
        }

        normalized, schema, error = self.validate([component])

        self.assertIsNone(error)
        self.assertEqual(normalized["components"][0]["raw_min"], 200)
        self.assertEqual(schema["parameters"]["illuminance"]["min"], 0)
        self.assertEqual(schema["parameters"]["illuminance"]["max"], 1200)
        self.assertEqual(schema["parameters"]["illuminance"]["unit"], "lx")

    def test_identify_indicator_is_optional_and_declares_action_when_set(self):
        config = {
            "format_version": 1,
            "components": [],
            "identify_indicator": {
                "driver": "neopixel", "pin": 48, "brightness": 24,
            },
        }

        normalized, schema, error = validate_device_configuration(config, DRIVER_IDS)

        self.assertIsNone(error)
        self.assertEqual(normalized["identify_indicator"]["pin"], 48)
        self.assertTrue(schema["capabilities"]["identify"])
        self.assertIn("identify", schema["actions"])
        self.assertTrue(validate_schema(schema))

    def test_identify_indicator_rejects_pin_conflicts_and_invalid_hardware(self):
        component = {
            "id": "lamp", "driver": "gpio_output", "label": "Лампа",
            "parameter_id": "enabled", "parameter_label": "Включена",
            "pin": 4, "active_high": True, "initial_value": False,
        }
        config = {
            "format_version": 1,
            "components": [component],
            "identify_indicator": {
                "driver": "gpio", "pin": 4, "active_high": True,
            },
        }
        _, _, error = validate_device_configuration(config, DRIVER_IDS)
        self.assertIn("GPIO 4", error)

        config["components"] = []
        config["identify_indicator"] = {
            "driver": "neopixel", "pin": 48, "brightness": 0,
        }
        _, _, error = validate_device_configuration(config, DRIVER_IDS)
        self.assertIn("NeoPixel", error)

    def test_saved_schema_export_route_serves_only_safe_device_ids(self):
        with TemporaryDirectory() as directory:
            export_path = Path(directory) / "esp32-01.json"
            export_path.write_text('{"schema":{}}', encoding="utf-8")
            with patch("main.DEVICE_CONFIGURATION_EXPORT_DIR", Path(directory)):
                response = handle_http_request(
                    None,
                    Request("/gateway-schemas/esp32-01.json", Headers())
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.body, b'{"schema":{}}')
                self.assertIn("application/json", response.headers["Content-Type"])

                rejected = handle_http_request(
                    None,
                    Request("/gateway-schemas/../gateway.db.json", Headers())
                )
                self.assertIsNone(rejected)

    def test_pwm_range_rejects_invalid_bounds_and_initial_value(self):
        component = {
            "id": "dimmer", "driver": "pwm_output", "label": "Диммер",
            "parameter_id": "duty", "parameter_label": "PWM",
            "pin": 5, "initial_value": 255, "min_value": 16, "max_value": 200,
        }
        _, _, error = self.validate([component])
        self.assertIn("начальное значение", error)

        component["initial_value"] = 80
        component["max_value"] = 256
        _, _, error = self.validate([component])
        self.assertIn("диапазон", error)

    def test_duplicate_ids_and_pins_are_rejected(self):
        base = {
            "id": "lamp", "driver": "gpio_output", "label": "Лампа",
            "parameter_id": "enabled", "parameter_label": "Включена",
            "pin": 4, "active_high": True, "initial_value": False,
        }
        duplicate_component = dict(base, parameter_id="enabled2")
        _, _, error = self.validate([base, duplicate_component])
        self.assertIn("ID компонента", error)

        duplicate_pin = dict(base, id="lamp2", parameter_id="enabled2")
        _, _, error = self.validate([base, duplicate_pin])
        self.assertIn("GPIO 4", error)

    def test_unsafe_or_driver_incompatible_pins_are_rejected(self):
        component = {
            "id": "button", "driver": "digital_input", "label": "Кнопка",
            "parameter_id": "pressed", "parameter_label": "Нажата",
            "pin": 3, "pull": "up", "interval_ms": 500,
        }
        _, _, error = self.validate([component])
        self.assertIn("GPIO", error)

        component["pin"] = 48
        _, _, error = self.validate([component])
        self.assertIn("GPIO", error)

    def test_unknown_advertised_driver_and_non_string_entries_are_safe(self):
        self.assertEqual(public_driver_catalog([{}, "unknown", "gpio_output"])[0]["id"], "gpio_output")
        config = {"format_version": 1, "components": [{
            "id": "lamp", "driver": "unknown", "label": "Лампа",
            "parameter_id": "enabled", "parameter_label": "Включена",
            "pin": 4,
        }]}
        _, _, error = validate_device_configuration(config, [None, "unknown"])
        self.assertIn("не объявила драйвер", error)

    def test_limits_and_value_types_are_checked(self):
        components = [{
            "id": "relay", "driver": "gpio_output", "label": "Реле",
            "parameter_id": "enabled", "parameter_label": "Включено",
            "pin": 4, "active_high": 1, "initial_value": False,
        }]
        _, _, error = self.validate(components)
        self.assertIn("boolean", error)

        components[0]["active_high"] = True
        components[0]["initial_value"] = False
        components[0]["interval_ms"] = 1000
        _, _, error = self.validate(components)
        self.assertIn("неподдерживаемые поля", error)

        _, _, error = self.validate([components[0]] * 9)
        self.assertIn("0 до 8", error)

    def test_utf8_labels_and_units_fit_firmware_buffers(self):
        component = {
            "id": "sensor", "driver": "analog_input", "label": "Я" * 41,
            "parameter_id": "reading", "parameter_label": "Значение",
            "pin": 4, "interval_ms": 1000, "unit": "lx",
        }
        _, _, error = self.validate([component])
        self.assertIn("80 байт UTF-8", error)

        component["label"] = "Датчик"
        component["unit"] = "°" * 13
        _, _, error = self.validate([component])
        self.assertIn("24 байт UTF-8", error)

        component["unit"] = "lx"
        component["parameter_label"] = "Значение\u0000raw"
        _, _, error = self.validate([component])
        self.assertIn("управляющие символы", error)

    def test_ui_preferences_are_checked_against_generated_schema(self):
        schema = {
            "parameters": {
                "enabled": {"type": "boolean", "access": "read_write"},
                "duty": {
                    "type": "integer", "access": "read_write",
                    "min": 10, "max": 200, "step": 1,
                },
            }
        }
        preferences, error = validate_parameter_ui_preferences(
            {"enabled": "list", "duty": "input"}, schema
        )
        self.assertIsNone(error)
        self.assertEqual(preferences["enabled"], "list")
        self.assertEqual(preferences["duty"], "input")

        _, error = validate_parameter_ui_preferences({"enabled": "slider"}, schema)
        self.assertIn("несовместим", error)


if __name__ == "__main__":
    unittest.main()
