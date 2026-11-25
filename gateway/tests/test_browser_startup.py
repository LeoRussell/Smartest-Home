"""DevTools readiness retries use a bounded budget without real Chrome or HTTP."""

import asyncio
import http.client
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("browser_startup_runner", ROOT / "web/tests/browser_smoke.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

PAGE = {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:18765/devtools/page/test"}


class BrowserSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def executable(self, name):
        path = self.directory / name
        path.write_bytes(b"synthetic browser fixture; never executed")
        return path

    def test_explicit_browser_has_priority_over_environment_and_path(self):
        explicit = self.executable("explicit-chrome.exe")
        environment = self.executable("environment-chrome.exe")
        fallback = self.executable("path-chrome.exe")
        with (
            patch.dict(runner.os.environ, {"CHROME_PATH": str(environment)}),
            patch.object(runner.shutil, "which", return_value=str(fallback)),
        ):
            self.assertEqual(runner.find_browser(str(explicit)), str(explicit.resolve()))

    def test_environment_browser_has_priority_over_path(self):
        environment = self.executable("environment-chrome.exe")
        fallback = self.executable("path-chrome.exe")
        with (
            patch.dict(runner.os.environ, {"CHROME_PATH": str(environment)}),
            patch.object(runner.shutil, "which", return_value=str(fallback)),
        ):
            self.assertEqual(runner.find_browser(None), str(environment.resolve()))

    def test_invalid_explicit_or_environment_browser_does_not_fall_back(self):
        valid = self.executable("available-chrome.exe")
        missing = self.directory / "missing-chrome.exe"
        for explicit, environment in ((str(missing), str(valid)),
                                      (str(self.directory), str(valid)),
                                      (None, str(missing))):
            with (
                self.subTest(explicit=explicit, environment=environment),
                patch.dict(runner.os.environ, {"CHROME_PATH": environment}),
                patch.object(runner.shutil, "which", return_value=str(valid)),
            ):
                with self.assertRaises(RuntimeError):
                    runner.find_browser(explicit)

    def test_windows_chrome_executable_can_be_discovered_on_path(self):
        chrome = self.executable("chrome.exe")
        with (
            patch.dict(runner.os.environ),
            patch.object(runner.shutil, "which", side_effect=lambda name: str(chrome) if name == "chrome.exe" else None),
        ):
            runner.os.environ.pop("CHROME_PATH", None)
            self.assertEqual(runner.find_browser(None), str(chrome.resolve()))


class BrowserStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.process = Mock()
        self.process.poll.return_value = None
        self.profile = Path("unused-browser-profile")

    async def test_endpoint_timeout_then_success(self):
        with (
            patch.object(runner, "read_browser_page", side_effect=[TimeoutError("endpoint warming up"), PAGE]) as probe,
            patch.object(runner.asyncio, "sleep", return_value=None),
        ):
            self.assertEqual(await runner.wait_for_browser(self.process, self.profile), PAGE)
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertEqual(call.args[0], self.profile)
            self.assertGreater(call.args[1], 0)
            self.assertLessEqual(call.args[1], 2)

    async def test_transient_port_json_target_and_http_errors_then_success(self):
        problems = [
            FileNotFoundError("DevToolsActivePort not created"),
            ValueError("partial port"),
            json.JSONDecodeError("partial JSON", "[", 1),
            ValueError("no debuggable page target"),
            urllib.error.URLError("connection refused"),
            http.client.HTTPException("incomplete response"),
        ]
        with (
            patch.object(runner, "read_browser_page", side_effect=[*problems, PAGE]) as probe,
            patch.object(runner.asyncio, "sleep", return_value=None),
        ):
            self.assertEqual(await runner.wait_for_browser(self.process, self.profile), PAGE)
        self.assertEqual(probe.call_count, len(problems) + 1)

    async def test_deadline_caps_request_budget_and_reports_last_problem(self):
        clock = {"now": 10.0}
        budgets = []
        deadline = clock["now"] + 0.35

        def fail_probe(profile, request_timeout):
            budgets.append((request_timeout, deadline - clock["now"]))
            clock["now"] += 0.08
            raise TimeoutError("last synthetic endpoint failure")

        async def advance_clock(delay):
            clock["now"] += delay

        async def run_probe(function, *args):
            # The separate liveness test exercises real worker threads. Keep
            # this artificial deadline independent of thread scheduling in CI.
            return function(*args)

        with (
            patch.object(runner, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(runner, "read_browser_page", side_effect=fail_probe),
            patch.object(runner.asyncio, "to_thread", side_effect=run_probe),
            patch.object(runner.asyncio, "sleep", side_effect=advance_clock),
        ):
            with self.assertRaisesRegex(RuntimeError, "last synthetic endpoint failure"):
                await runner.wait_for_browser(self.process, self.profile, timeout=0.35)
        self.assertGreaterEqual(len(budgets), 2)
        self.assertLess(budgets[-1][0], budgets[0][0])
        for actual, remaining in budgets:
            self.assertGreater(actual, 0)
            self.assertLessEqual(actual, 2)
            self.assertLessEqual(actual, remaining + 1e-9)

    async def test_exited_browser_fails_without_probing_including_zero_exit(self):
        for returncode in (0, 17):
            with self.subTest(returncode=returncode):
                self.process.poll.return_value = returncode
                with patch.object(runner, "read_browser_page") as probe:
                    with self.assertRaisesRegex(RuntimeError, str(returncode)):
                        await runner.wait_for_browser(self.process, self.profile)
                probe.assert_not_called()

    async def test_browser_exit_after_probe_failure_is_reported(self):
        def fail_probe(profile, request_timeout):
            self.process.poll.return_value = 23
            raise OSError("endpoint closed while browser exited")

        with (
            patch.object(runner, "read_browser_page", side_effect=fail_probe) as probe,
            patch.object(runner.asyncio, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "23"):
                await runner.wait_for_browser(self.process, self.profile)
        self.assertEqual(probe.call_count, 1)

    async def test_http_probe_does_not_block_the_event_loop(self):
        entered, release = threading.Event(), threading.Event()

        def blocking_probe(profile, request_timeout):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("The synchronous probe blocked the event loop")
            return PAGE

        async def release_from_event_loop():
            while not entered.is_set():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()

        heartbeat = asyncio.create_task(release_from_event_loop())
        try:
            with patch.object(runner, "read_browser_page", side_effect=blocking_probe):
                self.assertEqual(await asyncio.wait_for(
                    runner.wait_for_browser(self.process, self.profile, timeout=3), timeout=4
                ), PAGE)
            await heartbeat
        finally:
            release.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


class BrowserInterfaceReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_interface_can_become_ready_after_the_old_five_second_poll_limit(self):
        browser = runner.Browser(Mock())
        clock = {"now": 100.0, "probes": 0}

        async def evaluate(expression):
            clock["probes"] += 1
            clock["now"] += 0.1
            return clock["probes"] >= 120

        async def advance_clock(delay):
            clock["now"] += delay

        with (
            patch.object(runner, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(browser, "evaluate", side_effect=evaluate),
            patch.object(runner.asyncio, "sleep", side_effect=advance_clock),
        ):
            await browser.ready(timeout=30)
        self.assertEqual(clock["probes"], 120)
        self.assertGreater(clock["now"] - 100, 5)
        self.assertLess(clock["now"] - 100, 30)

    async def test_interface_deadline_reports_a_useful_diagnostic_snapshot(self):
        browser = runner.Browser(Mock())
        clock = {"now": 100.0, "probes": 0, "diagnostics": 0}
        deadline = clock["now"] + 30
        snapshot = {
            "readyState": "interactive",
            "location": "http://127.0.0.1:8080/pending-interface",
            "renderDevices": "undefined",
            "errors": ["synthetic interface startup failure"],
            "sockets": [{"readyState": 0}],
            "body": "synthetic pending interface body",
        }

        async def evaluate(expression):
            if clock["now"] >= deadline:
                clock["diagnostics"] += 1
                return snapshot
            clock["probes"] += 1
            clock["now"] = min(deadline, clock["now"] + 8)
            return False

        async def advance_clock(delay):
            clock["now"] += delay

        with (
            patch.object(runner, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(browser, "evaluate", side_effect=evaluate),
            patch.object(runner.asyncio, "sleep", side_effect=advance_clock),
        ):
            with self.assertRaises(RuntimeError) as failure:
                await browser.ready(timeout=30)
        message = str(failure.exception)
        for detail in ("interactive", "pending-interface", "undefined",
                       "synthetic interface startup failure", "synthetic pending interface body"):
            self.assertIn(detail, message)
        self.assertEqual(clock["diagnostics"], 1)
        self.assertLessEqual(clock["probes"], 4)
        self.assertLessEqual(clock["now"], deadline + 0.1)


if __name__ == "__main__":
    unittest.main()
