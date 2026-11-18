"""C3 hardware limits must hold through validation, storage and the protocol."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from device_configuration import (
    DRIVERS, public_configuration_limits, public_driver_catalog,
    validate_device_configuration,
)
from runtime import Gateway


C3 = "esp32-c3-supermini"
S3 = "esp32-s3-devkitc-1"
DRIVER_IDS = list(DRIVERS)


def component(pin=0, driver="gpio_output", index=0):
    item = {"id": f"part_{index}", "parameter_id": f"value_{index}",
            "label": f"Part {index}", "parameter_label": f"Value {index}",
            "driver": driver, "pin": pin}
    if driver == "gpio_output":
        item.update(active_high=True, initial_value=False)
    elif driver == "pwm_output":
        item.update(initial_value=0, min_value=0, max_value=255)
    elif driver == "analog_input":
        item.update(interval_ms=1000, unit="raw")
    else:
        item.update(pull="none", interval_ms=1000)
    return item


def configuration(items=None, **fields):
    return {"format_version": 1, "components": items or [], **fields}


class Socket:
    remote_address = ("isolated-c3-test", 0)
    close_code = None
    close_reason = ""

    def __init__(self, hello=None):
        self.hello = hello
        self.messages = []
        self.closed = []

    def __aiter__(self):
        async def messages():
            if self.hello is not None:
                yield json.dumps(self.hello)
        return messages()

    async def send(self, payload):
        self.messages.append(json.loads(payload))

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class C3ValidationTests(unittest.TestCase):
    def validate(self, value):
        return validate_device_configuration(value, DRIVER_IDS, C3)

    def test_c3_catalog_and_public_limits_do_not_mutate_other_boards(self):
        catalog = {driver["id"]: driver for driver in public_driver_catalog(DRIVER_IDS, C3)}
        self.assertEqual(catalog["gpio_output"]["pins"], [0, 1, 3, 4, 5, 6, 7, 10])
        self.assertEqual(catalog["analog_input"]["pins"], [0, 1, 3, 4])
        limits = public_configuration_limits(C3)
        self.assertEqual(limits, {"max_components": 8, "max_pwm_channels": 6,
                                 "identify_gpio_pins": [0, 1, 3, 4, 5, 6, 7, 8, 10],
                                 "identify_neopixel_pins": []})
        catalog["gpio_output"]["pins"].append(48)
        limits["identify_gpio_pins"].clear()
        self.assertNotIn(48, public_driver_catalog(DRIVER_IDS, C3)[0]["pins"])
        self.assertIn(8, public_configuration_limits(C3)["identify_gpio_pins"])
        self.assertEqual(public_driver_catalog(DRIVER_IDS), public_driver_catalog(DRIVER_IDS, S3))
        self.assertIn(18, public_driver_catalog(DRIVER_IDS, S3)[0]["pins"])

    def test_c3_zero_pin_is_valid_and_unsafe_or_s3_only_pins_are_rejected(self):
        normalized, schema, error = self.validate(configuration([component(0)]))
        self.assertIsNone(error)
        self.assertEqual(normalized["components"][0]["pin"], 0)
        self.assertEqual(schema["parameters"]["value_0"]["access"], "read_write")
        for pin in (2, 8, 9, 11, 12, 17, 18, 19, 20, 21, 38, 48, False):
            with self.subTest(pin=pin):
                self.assertIsNotNone(self.validate(configuration([component(pin)]))[2])

    def test_c3_adc_is_limited_to_supported_adc1_pins(self):
        for pin in (0, 1, 3, 4):
            self.assertIsNone(self.validate(configuration([component(pin, "analog_input")]))[2])
        for pin in (2, 5, 6, 7, 10):
            self.assertIsNotNone(self.validate(configuration([component(pin, "analog_input")]))[2])

    def test_gpio8_is_an_explicit_gpio_indicator_and_never_a_neopixel(self):
        indicator = {"driver": "gpio", "pin": 8, "active_high": False, "built_in": True}
        normalized, schema, error = self.validate(configuration(identify_indicator=indicator))
        self.assertIsNone(error)
        self.assertEqual(normalized["identify_indicator"], indicator)
        self.assertTrue(schema["capabilities"]["identify"])
        self.assertIn("identify", schema["actions"])
        for pin in (8, 38, 48):
            invalid = configuration(identify_indicator={"driver": "neopixel", "pin": pin, "brightness": 32})
            self.assertIsNotNone(self.validate(invalid)[2])

    def test_eight_components_are_supported_but_only_six_can_be_pwm(self):
        pins = [0, 1, 3, 4, 5, 6, 7, 10]
        sparse = [component(pin, "digital_input" if i < 2 else "pwm_output", i) for i, pin in enumerate(pins)]
        self.assertIsNone(self.validate(configuration(sparse))[2])
        excessive = [component(pin, "pwm_output", i) for i, pin in enumerate(pins[:7])]
        error = self.validate(configuration(excessive))[2]
        self.assertIn("6 PWM", error)
        self.assertEqual(error.path, "components.6.driver")

    def test_unknown_or_malformed_board_profile_never_inherits_s3_pins(self):
        for board in ("unknown", None, [], {}):
            with self.subTest(board=board):
                self.assertIsNotNone(validate_device_configuration(configuration(), DRIVER_IDS, board)[2])
                self.assertEqual(public_driver_catalog(DRIVER_IDS, board), [])
                self.assertEqual(public_configuration_limits(board), {})


class C3ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.empty_schema = validate_device_configuration(configuration(), DRIVER_IDS, C3)[1]
        self.metadata = {
            "name": "C3 SuperMini", "device_profile": "universal", "board_profile": C3,
            "driver_ids": DRIVER_IDS, "schema": self.empty_schema, "schema_version": 1,
            "firmware_id": "c3-test", "protocol_version": 2, "trust_policy": "allow",
        }
        self.gateway.save_device_metadata("c3-test", deepcopy(self.metadata))

    async def asyncTearDown(self):
        tasks = list(self.gateway.configuration_apply_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_c3_hello_is_accepted_but_unknown_board_hello_is_rejected(self):
        hello = {"type": "hello", "device": "c3-test", "name": "C3 SuperMini",
                 "protocol_version": 2, "schema_version": 1, "device_profile": "universal",
                 "board_profile": C3, "driver_catalog": DRIVER_IDS, "schema": self.empty_schema}
        socket = Socket(hello)
        await self.gateway.handle_client(socket)
        self.assertTrue(any(message["type"] == "hello_ack" for message in socket.messages))
        self.assertFalse(socket.closed)
        self.assertEqual(self.gateway.load_device_metadata("c3-test")["board_profile"], C3)
        socket = Socket({**hello, "board_profile": "unknown-c3-board"})
        await self.gateway.handle_client(socket)
        self.assertTrue(any(code == 4002 for code, _ in socket.closed))
        self.assertFalse(any(message["type"] == "hello_ack" for message in socket.messages))

    async def test_save_export_and_browser_catalog_use_c3_profile(self):
        browser = Socket()
        await self.gateway.handle_browser_message(browser, {
            "action": "save_device_configuration", "device": "c3-test",
            "configuration": configuration([component(0)]), "request_id": "save-c3",
        })
        result = next(item for item in browser.messages if item["type"] == "device_configuration_save_result")
        self.assertTrue(result["success"], result)
        self.assertEqual(self.gateway.load_device_configuration("c3-test")["config"]["components"][0]["pin"], 0)
        exported = self.gateway.device_configuration_export_document("c3-test")
        self.assertEqual(exported["board_profile"], C3)
        self.assertEqual(exported["hardware_configuration"]["components"][0]["pin"], 0)
        listing = self.gateway.get_all_devices()[0]
        self.assertEqual(listing["configuration_limits"]["max_pwm_channels"], 6)
        self.assertEqual(listing["driver_catalog"][0]["pins"], [0, 1, 3, 4, 5, 6, 7, 10])
        await self.gateway.handle_browser_message(browser, {
            "action": "save_device_configuration", "device": "c3-test",
            "configuration": configuration([component(18)]), "request_id": "save-s3-pin",
        })
        self.assertFalse(browser.messages[-1]["success"])
        self.assertEqual(self.gateway.load_device_configuration("c3-test")["config_version"], 1)

    async def test_stored_s3_assignments_are_never_transmitted_to_c3(self):
        normalized, _, error = validate_device_configuration(configuration([component(18)]), DRIVER_IDS, S3)
        self.assertIsNone(error)
        self.gateway.save_device_configuration("c3-test", normalized)
        socket = Socket()
        self.gateway.devices["c3-test"] = socket
        pending = {"command_id": "old-board-command", "device": "c3-test", "browser": None,
                   "operation": {"kind": "parameter", "name": "value_0", "value": True},
                   "payload": {}, "status": "queued", "attempts": 1, "timeout_task": None}
        self.gateway.create_command_queue_record(pending)
        self.gateway.pending_commands[pending["command_id"]] = pending
        self.assertFalse(await self.gateway.send_device_configuration("c3-test"))
        self.assertFalse(any(item["type"] == "device_config_apply" for item in socket.messages))
        saved = self.gateway.load_device_configuration("c3-test")
        self.assertEqual(saved["status"], "failed")
        self.assertIn("несовместима", saved["last_error"])
        self.assertFalse(self.gateway.pending_commands)
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM command_queue").fetchone()[0], "unavailable")

    async def test_device_report_restoration_validates_current_board_before_import(self):
        for pin, expected in ((5, False), (0, True)):
            candidate = configuration([component(pin, "analog_input")])
            # GPIO5 is a legal S3 ADC, but cannot be restored to a C3 device.
            normalized, schema, error = validate_device_configuration(candidate, DRIVER_IDS, S3 if pin == 5 else C3)
            self.assertIsNone(error)
            canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            await self.gateway.handle_device_configuration_report("c3-test", "c3-test", {
                "device": "c3-test", "config_version": 4, "schema_version": 4,
                "config_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "configuration": normalized, "schema": schema,
            })
            saved = self.gateway.load_device_configuration("c3-test")
            self.assertEqual(saved is not None, expected)
        self.assertEqual(saved["status"], "applied")
        self.assertEqual(saved["config"]["components"][0]["pin"], 0)


if __name__ == "__main__":
    unittest.main()
