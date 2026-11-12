"""The endurance runner exercises real sockets and cleans up on every exit path."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import soak_test


class SoakRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def assert_no_new_tasks(self, baseline):
        await asyncio.sleep(0)
        unfinished = asyncio.all_tasks() - baseline
        self.assertFalse(unfinished, f"Runner leaked tasks: {unfinished}")

    async def test_real_loopback_cycles_cover_replay_timeout_and_restart(self):
        baseline = asyncio.all_tasks()
        checkpoints = []
        report = await soak_test.run_soak(
            cycles=2, interval=0,
            checkpoint=lambda item: checkpoints.append(deepcopy(item)),
        )
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(report["scope"], "software_loopback")
        self.assertFalse(report["physical_device_tested"])
        self.assertEqual(report["cycles_completed"], 2)
        self.assertEqual(report["errors"], [])
        self.assertGreater(report["database_bytes"], 0)
        counts = report["counts"]
        self.assertEqual(counts["gateway_starts"], 3)
        for key in ("confirmed_commands", "duplicate_acks", "cached_replays", "guarded_cache_misses",
                    "expected_timeouts", "recovered_manual_commands", "interrupted_scenarios"):
            self.assertEqual(counts[key], 2, key)
        self.assertEqual(counts["network_reconnections"], 0)
        self.assertEqual(report["simulated_side_effects"], 8)
        self.assertEqual(checkpoints[0]["status"], "running")
        self.assertTrue(any(item["cycles_completed"] == 1 for item in checkpoints))
        self.assertEqual(checkpoints[-1]["status"], "passed")
        self.assertLessEqual(len(report["gateway_log_tail"]), 80)
        await self.assert_no_new_tasks(baseline)

    async def test_one_gateway_survives_repeated_network_reconnections(self):
        baseline = asyncio.all_tasks()
        report = await soak_test.run_soak(cycles=2, interval=0, restart_every=0)
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(report["counts"]["gateway_starts"], 1)
        self.assertEqual(report["counts"]["network_reconnections"], 2)
        self.assertEqual(report["counts"]["interrupted_scenarios"], 0)
        self.assertEqual(report["counts"]["recovered_manual_commands"], 2)
        self.assertEqual(report["counts"]["guarded_cache_misses"], 2)
        await self.assert_no_new_tasks(baseline)

    async def test_failure_is_reported_and_gateway_tasks_are_closed(self):
        baseline = asyncio.all_tasks()
        with patch.object(soak_test.SoakRun, "cycle", side_effect=RuntimeError("injected failure")):
            report = await soak_test.run_soak(cycles=1, interval=0)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cycles_completed"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["cycle"], 1)
        self.assertEqual(report["errors"][0]["message"], "injected failure")
        await self.assert_no_new_tasks(baseline)

    async def test_cancellation_keeps_partial_report_and_closes_gateway(self):
        baseline = asyncio.all_tasks()
        first_cycle = asyncio.Event()
        task = asyncio.create_task(soak_test.run_soak(
            cycles=100, interval=10, progress=lambda *_: first_cycle.set(),
        ))
        await asyncio.wait_for(first_cycle.wait(), 10)
        task.cancel()
        report = await asyncio.wait_for(task, 10)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["cycles_completed"], 1)
        self.assertEqual(report["errors"], [])
        await self.assert_no_new_tasks(baseline)

    async def test_duration_always_completes_at_least_one_cycle(self):
        report = await soak_test.run_soak(duration=0.001, interval=0)
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(report["cycles_completed"], 1)
        self.assertGreater(report["elapsed_seconds"], 0.001)

    async def test_invalid_limits_do_not_start_a_gateway(self):
        for options in ({"cycles": 0}, {"cycles": True}, {"cycles": 1, "duration": 1},
                        {"duration": float("nan")}, {"interval": -1},
                        {"command_timeout": float("inf")}, {"restart_every": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await soak_test.run_soak(**options)


class SoakReportTests(unittest.TestCase):
    def test_database_and_unrelated_json_are_never_overwritten(self):
        with TemporaryDirectory() as temporary:
            for filename, content in (("gateway.db", b"SQLite fixture"),
                                      ("device.json", b'{"device":"lamp"}'),
                                      ("broken.json", b"invalid json")):
                path = Path(temporary) / filename
                path.write_bytes(content)
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    soak_test.validate_report_path(path)
                self.assertEqual(path.read_bytes(), content)

    def test_atomic_updates_replace_only_a_recognized_report(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reports" / "soak.json"
            self.assertEqual(soak_test.validate_report_path(path), path.resolve())
            report = {"format": "smartest-home-soak-report", "status": "running"}
            soak_test.write_report(path, report)
            self.assertEqual(soak_test.validate_report_path(path), path.resolve())
            report["status"] = "passed"
            soak_test.write_report(path, report)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            self.assertEqual(list(path.parent.glob(".soak-report-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
