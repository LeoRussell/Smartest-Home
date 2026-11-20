"""DS18B20 is a typed 1-Wire sensor, never an ADC calibration preset."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from device_configuration import DRIVERS, public_driver_catalog, validate_device_configuration
from runtime import Gateway
from schema import SchemaMixin
from settings import MAX_SCHEMA_BYTES


C3 = "esp32-c3-supermini"
S3 = "esp32-s3-devkitc-1"
DRIVER_IDS = list(DRIVERS)
LEGACY_DRIVERS = [driver for driver in DRIVER_IDS if driver != "ds18b20"]


def sensor(**fields):
    return {
        "id": "temperature", "driver": "ds18b20", "label": "Температура",
        "parameter_id": "temperature_c", "parameter_label": "Температура",
        "pin": 4, **fields,
    }


def configuration(*components):
    return {"format_version": 1, "components": list(components)}


class SchemaValidator(SchemaMixin):
    MAX_SCHEMA_BYTES = MAX_SCHEMA_BYTES


class DS18B20ValidationTests(unittest.TestCase):
    def validate(self, component=None, *, board=C3, drivers=DRIVER_IDS):
        return validate_device_configuration(
            configuration(sensor() if component is None else component), drivers, board
        )

    def test_catalog_gates_driver_by_firmware_and_uses_digital_gpio(self):
        self.assertNotIn("ds18b20", {d["id"] for d in public_driver_catalog(LEGACY_DRIVERS, C3)})
        catalog = {d["id"]: d for d in public_driver_catalog(DRIVER_IDS, C3)}
        self.assertEqual(catalog["ds18b20"]["pins"], [0, 1, 3, 4, 5, 6, 7, 10])
        self.assertEqual(catalog["ds18b20"]["fields"], ["interval_ms", "unit"])
        self.assertNotIn("requires_initial_sample", catalog["ds18b20"])
        catalog["ds18b20"]["pins"].append(48)
        self.assertNotIn(48, next(d for d in public_driver_catalog(DRIVER_IDS, C3) if d["id"] == "ds18b20")["pins"])
        normalized, schema, error = self.validate(drivers=LEGACY_DRIVERS)
        self.assertIsNone(normalized)
        self.assertIsNone(schema)
        self.assertEqual(error.path, "components.0.driver")

    def test_defaults_generate_celsius_fractional_read_only_schema(self):
        normalized, schema, error = self.validate()
        self.assertIsNone(error)
        self.assertEqual(normalized["components"][0], sensor(interval_ms=2000, unit="°C"))
        self.assertEqual(schema["parameters"]["temperature_c"], {
            "label": "Температура", "component": "temperature", "type": "number",
            "access": "read_only", "min": -55, "max": 125, "step": 0.0625,
            "precision": 4, "unit": "°C",
        })
        self.assertTrue(SchemaValidator().validate_schema(schema))
        self.assertNotIn("control", schema["parameters"]["temperature_c"])

    def test_optional_defaults_and_explicit_values_have_same_canonical_document(self):
        implicit, schema, error = self.validate()
        explicit, explicit_schema, explicit_error = self.validate(sensor(interval_ms=2000, unit="°C"))
        self.assertIsNone(error)
        self.assertIsNone(explicit_error)
        self.assertEqual(implicit, explicit)
        self.assertEqual(schema, explicit_schema)
        # Revalidation of an exported document must not change its normalized hash.
        reloaded, reloaded_schema, reloaded_error = validate_device_configuration(
            json.loads(json.dumps(implicit)), DRIVER_IDS, C3
        )
        self.assertIsNone(reloaded_error)
        self.assertEqual(implicit, reloaded)
        self.assertEqual(schema, reloaded_schema)

    def test_interval_bounds_and_integer_type(self):
        for value in (1000, 2000, 3_600_000):
            with self.subTest(valid=value):
                normalized, _, error = self.validate(sensor(interval_ms=value))
                self.assertIsNone(error)
                self.assertEqual(normalized["components"][0]["interval_ms"], value)
        for value in (0, 500, 999, 3_600_001, True, 1000.0, None, "2000"):
            with self.subTest(invalid=value):
                error = self.validate(sensor(interval_ms=value))[2]
                self.assertIsNotNone(error)
                self.assertEqual(error.path, "components.0.interval_ms")

    def test_unit_is_fixed_and_cannot_relabel_celsius_as_other_quantity(self):
        for unit in ("raw", "°F", "K", "", None, 1, [], {}):
            with self.subTest(unit=unit):
                error = self.validate(sensor(unit=unit))[2]
                self.assertIsNotNone(error)
                self.assertEqual(error.path, "components.0.unit")

    def test_sensor_rejects_adc_calibration_output_and_bus_address_fields(self):
        for key, value in {
            "raw_min": 0, "raw_max": 4095, "value_min": 0, "value_max": 100,
            "pull": "up", "active_high": True, "initial_value": 0,
            "min_value": -55, "max_value": 125, "address": "28AABBCCDDEEFF00",
        }.items():
            with self.subTest(field=key):
                self.assertIsNotNone(self.validate(sensor(**{key: value}))[2])

    def test_sensor_uses_supported_gpio_on_both_boards(self):
        for board, pins in ((C3, (0, 4, 5, 10)), (S3, (1, 4, 18, 21))):
            for pin in pins:
                with self.subTest(board=board, valid=pin):
                    self.assertIsNone(self.validate(sensor(pin=pin), board=board)[2])
        for board, pins in ((C3, (2, 8, 9, 18, 19, 38, 48, False)), (S3, (0, 3, 19, 20, 38, 48, False))):
            for pin in pins:
                with self.subTest(board=board, invalid=pin):
                    error = self.validate(sensor(pin=pin), board=board)[2]
                    self.assertIsNotNone(error)
                    self.assertEqual(error.path, "components.0.pin")

    def test_bus_is_exclusive_with_other_components_and_identify(self):
        other = {
            "id": "lamp", "driver": "gpio_output", "label": "Lamp",
            "parameter_id": "enabled", "parameter_label": "Enabled",
            "pin": 4, "active_high": True, "initial_value": False,
        }
        error = validate_device_configuration(configuration(sensor(), other), DRIVER_IDS, C3)[2]
        self.assertIsNotNone(error)
        self.assertEqual(error.path, "components.1.pin")
        value = configuration(sensor())
        value["identify_indicator"] = {"driver": "gpio", "pin": 4, "active_high": False}
        self.assertIsNotNone(validate_device_configuration(value, DRIVER_IDS, C3)[2])

    def test_temperature_constraints_preserve_fraction_and_reject_fault_sentinel(self):
        _, schema, error = self.validate()
        self.assertIsNone(error)
        validator = SchemaValidator()
        for temperature in (-55, -10.0625, 0, 23.4375, 85, 125):
            with self.subTest(valid=temperature):
                self.assertTrue(validator.validate_parameters(schema, {"temperature_c": temperature}))
        for temperature in (-127, -55.0625, 125.0625, 23.1, True, None, "23.4375", float("nan"), float("inf")):
            with self.subTest(invalid=temperature):
                self.assertFalse(validator.validate_parameters(schema, {"temperature_c": temperature}))


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class DS18B20ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.empty_schema = validate_device_configuration(configuration(), DRIVER_IDS, C3)[1]
        self.metadata = {
            "name": "C3", "device_profile": "universal", "board_profile": C3,
            "driver_ids": DRIVER_IDS, "schema": self.empty_schema, "schema_version": 1,
            "firmware_id": "c3-sensor", "protocol_version": 2, "trust_policy": "allow",
        }
        self.gateway.save_device_metadata("c3-sensor", deepcopy(self.metadata))

    async def asyncTearDown(self):
        tasks = list(self.gateway.configuration_apply_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def save(self, component):
        browser = Socket()
        await self.gateway.handle_browser_message(browser, {
            "action": "save_device_configuration", "device": "c3-sensor",
            "configuration": configuration(component), "request_id": "sensor-save",
        })
        return next(m for m in browser.messages if m["type"] == "device_configuration_save_result")

    async def test_normalized_sensor_survives_save_export_delivery_and_ack(self):
        result = await self.save(sensor())
        self.assertTrue(result["success"], result)
        saved = self.gateway.load_device_configuration("c3-sensor")
        canonical = json.dumps(saved["config"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(saved["config_hash"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(saved["config"]["components"][0]["interval_ms"], 2000)
        exported = self.gateway.device_configuration_export_document("c3-sensor")
        self.assertEqual(exported["hardware_configuration"], saved["config"])
        self.assertEqual(exported["board_profile"], C3)
        device = Socket()
        self.gateway.devices["c3-sensor"] = device
        self.assertTrue(await self.gateway.send_device_configuration("c3-sensor"))
        delivery = next(m for m in device.messages if m["type"] == "device_config_apply")
        self.assertEqual(delivery["configuration"], saved["config"])
        _, schema, error = validate_device_configuration(delivery["configuration"], DRIVER_IDS, C3)
        self.assertIsNone(error)
        await self.gateway.handle_device_configuration_result("c3-sensor", "c3-sensor", {
            "device": "c3-sensor", "config_version": saved["config_version"],
            "config_hash": saved["config_hash"], "schema_version": saved["config_version"],
            "success": True, "schema": schema,
        })
        self.assertEqual(self.gateway.load_device_configuration("c3-sensor")["status"], "applied")
        accepted = self.gateway.load_device_metadata("c3-sensor")["schema"]
        self.assertEqual(accepted, schema)
        self.assertTrue(self.gateway.validate_parameters(accepted, {"temperature_c": 23.4375}))

    async def test_invalid_or_unsupported_sensor_does_not_replace_saved_configuration(self):
        self.assertTrue((await self.save(sensor()))["success"])
        saved = self.gateway.load_device_configuration("c3-sensor")
        self.assertFalse((await self.save(sensor(raw_min=0)))["success"])
        self.assertEqual(self.gateway.load_device_configuration("c3-sensor")["config_hash"], saved["config_hash"])
        metadata = self.gateway.load_device_metadata("c3-sensor")
        metadata["driver_ids"] = LEGACY_DRIVERS
        self.gateway.save_device_metadata("c3-sensor", metadata)
        self.assertFalse((await self.save(sensor(interval_ms=3000)))["success"])
        device = Socket()
        self.gateway.devices["c3-sensor"] = device
        self.assertFalse(await self.gateway.send_device_configuration("c3-sensor"))
        self.assertFalse(any(m["type"] == "device_config_apply" for m in device.messages))
        self.assertEqual(self.gateway.load_device_configuration("c3-sensor")["config_version"], saved["config_version"])


if __name__ == "__main__":
    unittest.main()
