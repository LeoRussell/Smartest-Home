import asyncio
import copy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Gateway


class AutomationProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.messages = []

        async def broadcast(message):
            self.messages.append(copy.deepcopy(message))

        self.gateway.broadcast = AsyncMock(side_effect=broadcast)
        self.config = {
            "version": 2, "name": "Progress test", "workspace": {"blocks": {}},
            "program": {"version": 1, "trigger": {"type": "manual"},
                        "body": [{"type": "wait", "seconds": 60, "block_id": "wait-source"}]}
        }

    async def asyncTearDown(self):
        tasks = list(self.gateway.automation_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    def save(self):
        success, message = self.gateway.save_automation("progress", self.config)
        self.assertTrue(success, message)

    async def wait_for_stage(self, stage):
        for _ in range(30):
            snapshot = self.gateway.automation_progress_snapshot("progress")
            if snapshot and snapshot["stage"] == stage:
                return snapshot
            await asyncio.sleep(0)
        self.fail(f"Progress did not reach {stage}")

    def test_source_ids_are_optional_bounded_metadata(self):
        self.assertIsNone(self.gateway.validate_automation_config(self.config))
        for invalid in (None, "", 1, [], "x" * 129, "bad\nsource"):
            with self.subTest(invalid=invalid):
                self.config["program"]["body"][0]["block_id"] = invalid
                self.assertIsNotNone(self.gateway.validate_automation_config(self.config))
        self.config["program"]["body"][0].pop("block_id")
        self.assertIsNone(self.gateway.validate_automation_config(self.config))

    async def test_wait_snapshot_counts_down_without_database_or_broadcast_ticks(self):
        self.save()
        self.assertTrue(self.gateway.start_automation_run("progress", self.config, "manual"))
        initial = await self.wait_for_stage("wait")
        self.assertEqual(initial["block_id"], "wait-source")
        self.assertEqual(initial["step_number"], 1)
        before = len(self.messages)
        await asyncio.sleep(0.025)
        after = self.gateway.get_automations()[0]["active_progress"]
        self.assertLess(after["remaining_seconds"], initial["remaining_seconds"])
        self.assertEqual(before, len(self.messages))
        self.assertNotIn("_wait_deadline", after)
        self.assertEqual(len(self.gateway.get_automation_run_history("progress")[0]["steps"]), 1)
        task = self.gateway.automation_tasks["progress"]
        self.gateway.automation_stop_requests.add("progress")
        await task
        self.assertIsNone(self.gateway.automation_progress_snapshot("progress"))
        terminal = [item for item in self.messages if item.get("type") == "automation_progress"][-1]
        self.assertEqual(terminal["status"], "stopped")

    async def test_acknowledgement_is_visible_until_actual_device_result(self):
        metadata = {"schema": {"parameters": {"power": {"type": "boolean", "access": "read_write"}}, "actions": {}}}
        self.gateway.save_device_metadata("lamp", metadata)
        self.gateway.device_metadata["lamp"] = metadata
        self.gateway.devices["lamp"] = object()
        self.config["program"]["body"] = [{
            "type": "set_parameter", "device": "lamp", "parameter": "power",
            "value": {"type": "constant", "value": False}, "block_id": "power-source"
        }]
        self.save()
        future = asyncio.get_running_loop().create_future()
        with patch.object(self.gateway, "dispatch_device_command", AsyncMock(return_value=future)):
            self.gateway.start_automation_run("progress", self.config, "manual")
            task = self.gateway.automation_tasks["progress"]
            waiting = await self.wait_for_stage("acknowledgment")
            self.assertEqual(waiting["details"]["parameter"], "power")
            self.assertEqual(waiting["block_id"], "power-source")
            self.assertFalse(task.done())
            future.set_result({"status": "succeeded", "value": False})
            await task
        stages = [item["stage"] for item in self.messages if item.get("type") == "automation_progress"]
        self.assertEqual(stages, ["starting", "sending", "acknowledgment", "finished"])
        self.assertIsNone(self.gateway.get_automations()[0]["active_progress"])

    async def test_cancelled_old_run_cannot_clear_or_overwrite_new_progress(self):
        self.save()
        self.gateway.start_automation_run("progress", self.config, "manual")
        old_snapshot = await self.wait_for_stage("wait")
        old_task = self.gateway.automation_tasks["progress"]
        self.assertTrue(self.gateway.start_automation_run("progress", self.config, "changed"))
        await asyncio.gather(old_task, return_exceptions=True)
        new_snapshot = await self.wait_for_stage("wait")
        self.assertNotEqual(old_snapshot["run_id"], new_snapshot["run_id"])
        self.assertGreater(new_snapshot["remaining_seconds"], 59)
        before = len(self.messages)
        await self.gateway.publish_automation_progress("progress", old_snapshot["run_id"], "finished", status="interrupted")
        self.assertEqual(len(self.messages), before)
        self.assertEqual(self.gateway.get_automations()[0]["active_progress"]["run_id"], new_snapshot["run_id"])


if __name__ == "__main__":
    unittest.main()
