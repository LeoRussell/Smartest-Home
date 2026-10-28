import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from main import validate_schema


class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "format_version": 1,
            "components": {
                "status_led": {
                    "label": "Встроенный светодиод",
                    "category": "actuator",
                    "description": "Индикатор платы."
                }
            },
            "parameters": {
                "led": {
                    "label": "Светодиод",
                    "description": "Включает индикатор платы.",
                    "component": "status_led",
                    "type": "boolean",
                    "access": "read_write",
                    "control": "toggle"
                }
            },
            "actions": {}
        }

    def test_accepts_versioned_component_schema(self):
        self.assertTrue(validate_schema(self.schema))

    def test_accepts_existing_schema_without_new_fields(self):
        self.assertTrue(validate_schema({
            "parameters": {
                "led": {
                    "label": "LED",
                    "type": "boolean",
                    "access": "read_write"
                }
            },
            "actions": {}
        }))

    def test_rejects_parameter_referencing_unknown_component(self):
        schema = copy.deepcopy(self.schema)
        schema["parameters"]["led"]["component"] = "missing"
        self.assertFalse(validate_schema(schema))

    def test_rejects_unknown_component_category(self):
        schema = copy.deepcopy(self.schema)
        schema["components"]["status_led"]["category"] = "arbitrary_gpio"
        self.assertFalse(validate_schema(schema))

    def test_rejects_malformed_component_category_without_raising(self):
        schema = copy.deepcopy(self.schema)
        schema["components"]["status_led"]["category"] = []
        self.assertFalse(validate_schema(schema))

    def test_rejects_unsupported_schema_format_version(self):
        schema = copy.deepcopy(self.schema)
        schema["format_version"] = 2
        self.assertFalse(validate_schema(schema))

    def test_rejects_overlong_description(self):
        schema = copy.deepcopy(self.schema)
        schema["parameters"]["led"]["description"] = "x" * 241
        self.assertFalse(validate_schema(schema))


if __name__ == "__main__":
    unittest.main()
