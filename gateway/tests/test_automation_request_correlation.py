"""Saving a scenario must return the caller's request ID without changing legacy replies."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class Browser:
    def __init__(self):
        self.messages = []

    async def send(self, document):
        self.messages.append(json.loads(document))


class AutomationRequestCorrelationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.browser = Browser()
        self.config = {
            "version": 2, "name": "Correlated draft", "workspace": {"blocks": {}},
            "program": {"version": 1, "trigger": {"type": "manual"}, "body": [{"type": "wait", "seconds": 1}]},
        }

    async def test_success_and_validation_failure_keep_request_id(self):
        await self.gateway.handle_browser_message(self.browser, {
            "action": "save_automation", "request_id": "save-current-draft", "config": self.config,
        })
        result = self.browser.messages[0]
        self.assertEqual(result["type"], "automation_save_result")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["request_id"], "save-current-draft")
        await self.gateway.handle_browser_message(self.browser, {
            "action": "save_automation", "request_id": "save-invalid-draft", "config": {},
        })
        result = self.browser.messages[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["request_id"], "save-invalid-draft")

    async def test_invalid_request_id_never_creates_a_scenario(self):
        for request_id in (True, {}, "", "x" * 129):
            await self.gateway.handle_browser_message(self.browser, {
                "action": "save_automation", "request_id": request_id, "config": self.config,
            })
            self.assertFalse(self.browser.messages[-1]["success"])
        self.assertEqual(self.gateway.get_automations(), [])

    async def test_legacy_save_still_works_without_request_id(self):
        await self.gateway.handle_browser_message(self.browser, {
            "action": "save_automation", "config": self.config,
        })
        result = self.browser.messages[0]
        self.assertTrue(result["success"], result)
        self.assertNotIn("request_id", result)
        self.assertEqual(len(self.gateway.get_automations()), 1)


if __name__ == "__main__":
    unittest.main()
