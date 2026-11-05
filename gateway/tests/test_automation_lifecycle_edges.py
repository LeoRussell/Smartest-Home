"""Delayed triggers must never execute an edited-away program snapshot."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class AutomationLifecycleEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        metadata = {"schema": {"parameters": {
            "reading": {"type": "number", "access": "read_only"},
        }, "actions": {}}, "schema_version": 1}
        self.gateway.save_device_metadata("sensor", metadata)
        self.gateway.device_metadata["sensor"] = metadata
        self.config = {
            "version": 2, "name": "Delayed sensor", "workspace": {"blocks": {}},
            "program": {"version": 1,
                        "trigger": {"type": "parameter_changed", "device": "sensor", "parameter": "reading"},
                        "body": [{"type": "wait", "seconds": 1}]},
        }
        self.assertTrue(self.gateway.save_automation("delayed", self.config)[0])

    async def asyncTearDown(self):
        tasks = list(self.gateway.automation_trigger_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def prepare_source(self):
        self.gateway.devices["sensor"] = object()
        self.gateway.device_states["sensor"] = {"reading": 10}
        self.gateway.record_device_telemetry("sensor", {"reading": 10})

    async def test_edit_cancels_delayed_trigger_before_awaiting_browser_reply(self):
        self.prepare_source()
        gate = asyncio.Event()
        original = self.gateway.wait_for_stable_automation_trigger

        async def delayed(*args):
            await gate.wait()
            await original(*args)

        class Browser:
            async def send(self, document):
                if json.loads(document).get("type") == "automation_save_result":
                    gate.set()
                    # A slow browser send lets the old stability/cooldown task run.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)

        revised = deepcopy(self.config)
        revised["program"]["body"][0]["seconds"] = 9
        with patch.object(self.gateway, "wait_for_stable_automation_trigger", delayed), \
                patch.object(self.gateway, "start_automation_run") as start:
            self.gateway.schedule_parameter_automation_trigger("delayed", self.config, "sensor", "reading", 10)
            pending = self.gateway.automation_trigger_tasks["delayed"]
            await self.gateway.handle_browser_message(Browser(), {
                "action": "save_automation", "automation_id": "delayed", "config": revised,
            })
            await asyncio.gather(pending, return_exceptions=True)
            start.assert_not_called()
        self.assertNotIn("delayed", self.gateway.automation_trigger_tasks)
        self.assertEqual(self.gateway.get_automations()[0]["config"], revised)

    async def test_delayed_trigger_rejects_changed_body_even_when_trigger_is_equal(self):
        self.prepare_source()
        revised = deepcopy(self.config)
        revised["program"]["body"][0]["seconds"] = 9
        with self.gateway.database_connection() as connection:
            connection.execute("UPDATE automations SET config_json = ? WHERE automation_id = ?",
                               (json.dumps(revised), "delayed"))
        with patch.object(self.gateway, "start_automation_run") as start:
            self.gateway.schedule_parameter_automation_trigger("delayed", self.config, "sensor", "reading", 10)
            await self.gateway.automation_trigger_tasks["delayed"]
            start.assert_not_called()

    async def test_failed_edit_keeps_pending_trigger_and_its_valid_program(self):
        self.prepare_source()
        with patch.object(self.gateway, "start_automation_run") as start:
            self.gateway.schedule_parameter_automation_trigger("delayed", self.config, "sensor", "reading", 10)
            pending = self.gateway.automation_trigger_tasks["delayed"]
            self.assertFalse(self.gateway.save_automation("delayed", {"name": "Invalid"})[0])
            self.assertIs(self.gateway.automation_trigger_tasks["delayed"], pending)
            await pending
            start.assert_called_once_with("delayed", self.config, "parameter_changed")


if __name__ == "__main__":
    unittest.main()
