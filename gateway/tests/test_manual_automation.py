import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Gateway


class Browser:
    def __init__(self):
        self.messages = []

    async def send(self, document):
        self.messages.append(json.loads(document))


class ManualAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.browser = Browser()
        self.config = {
            "version": 2, "name": "Night mode", "workspace": {"blocks": {}},
            "program": {"version": 1, "trigger": {"type": "manual", "show_on_dashboard": True},
                        "body": [{"type": "wait", "seconds": 0}]},
        }

    async def asyncTearDown(self):
        tasks = list(self.gateway.automation_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    async def launch(self, automation_id="manual-test", **extra):
        await self.gateway.handle_browser_message(self.browser, {
            "action": "run_automation", "automation_id": automation_id,
            "request_id": "manual-request", **extra,
        })
        return self.browser.messages[-1]

    def test_manual_trigger_validation_and_persistence(self):
        for enabled in (True, False):
            config = copy.deepcopy(self.config)
            config["program"]["trigger"]["show_on_dashboard"] = enabled
            self.assertIsNone(self.gateway.validate_automation_config(config))
        for invalid in (1, "true", None):
            config = copy.deepcopy(self.config)
            config["program"]["trigger"]["show_on_dashboard"] = invalid
            self.assertIsNotNone(self.gateway.validate_automation_config(config))
        config = copy.deepcopy(self.config)
        config["program"]["trigger"]["device"] = "lamp"
        self.assertIsNotNone(self.gateway.validate_automation_config(config))
        self.assertTrue(self.gateway.save_automation("manual-test", self.config)[0])
        reopened = Gateway(self.gateway.DB_PATH)
        reopened.init_db()
        self.assertTrue(reopened.get_automations()[0]["config"]["program"]["trigger"]["show_on_dashboard"])

    async def test_launch_uses_normal_command_pipeline_and_run_history(self):
        schema = {"parameters": {"enabled": {"type": "boolean", "access": "read_write"}}, "actions": {}}
        metadata = {"schema": schema, "schema_version": 1}
        self.gateway.save_device_metadata("lamp", metadata)
        self.gateway.device_metadata["lamp"] = metadata
        self.gateway.devices["lamp"] = Browser()
        self.config["program"]["body"] = [{
            "type": "set_parameter", "device": "lamp", "parameter": "enabled",
            "value": {"type": "constant", "value": False},
        }]
        self.assertTrue(self.gateway.save_automation("manual-test", self.config)[0])
        result = asyncio.get_running_loop().create_future()
        result.set_result({"status": "succeeded", "value": False})
        dispatcher = AsyncMock(return_value=result)
        with patch.object(self.gateway, "dispatch_device_command", dispatcher):
            response = await self.launch()
            self.assertTrue(response["success"])
            self.assertEqual(response["type"], "automation_run_result")
            self.assertEqual(response["request_id"], "manual-request")
            await self.gateway.automation_tasks["manual-test"]
        self.assertEqual(dispatcher.await_args.args[:3], ("lamp", "enabled", False))
        self.assertEqual(dispatcher.await_args.kwargs["automation_id"], "manual-test")
        history = self.gateway.get_automation_run_history("manual-test")
        self.assertEqual(history[0]["status"], "succeeded")
        self.assertEqual(history[0]["trigger_reason"], "manual")

    async def test_manual_launch_does_not_restart_a_waiting_run(self):
        self.config["program"]["body"] = [{"type": "wait", "seconds": 60}]
        self.gateway.save_automation("manual-test", self.config)
        self.assertTrue((await self.launch())["success"])
        task = self.gateway.automation_tasks["manual-test"]
        await asyncio.sleep(0)
        self.assertIn(task, self.gateway.automation_waiting_tasks)
        self.assertFalse((await self.launch())["success"])
        self.assertIs(self.gateway.automation_tasks["manual-test"], task)
        self.assertEqual(self.gateway.get_automation_run_count("manual-test"), 1)

    async def test_invalid_disabled_automatic_and_restore_requests_are_rejected(self):
        self.gateway.save_automation("manual-test", self.config, enabled=False)
        self.assertFalse((await self.launch())["success"])
        automatic = copy.deepcopy(self.config)
        automatic["program"]["trigger"] = {"type": "gateway_started"}
        self.gateway.save_automation("automatic", automatic)
        for identity in ("automatic", "missing", "../test", [], None):
            with self.subTest(identity=identity):
                self.assertFalse((await self.launch(identity))["success"])
        self.gateway.save_automation("manual-test", self.config)
        self.gateway.database_restore_in_progress = True
        self.assertFalse((await self.launch())["success"])
        self.gateway.database_restore_in_progress = False
        self.assertFalse((await self.launch(request_id=[]))["success"])
        self.assertEqual(self.gateway.get_automation_run_count("manual-test"), 0)

    async def test_manual_scenario_never_runs_from_automatic_events(self):
        self.gateway.save_automation("manual-test", self.config)
        for event in ("gateway_started", "device_connected", "parameter_changed"):
            self.gateway.fire_automation_event(event, "lamp", "enabled", True)
        await asyncio.sleep(0)
        self.assertEqual(self.gateway.automation_tasks, {})
        self.assertEqual(self.gateway.get_automation_run_count("manual-test"), 0)


if __name__ == "__main__":
    unittest.main()
