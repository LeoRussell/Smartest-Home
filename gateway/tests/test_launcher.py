"""The combined launcher must own and stop only its own web server."""

import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import websockets

spec = importlib.util.spec_from_file_location("project_launcher", Path(__file__).resolve().parents[2] / "run.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_gateway_and_interface_share_one_cancellable_lifecycle(self):
        with TemporaryDirectory() as temporary:
            with socket.socket() as http_reservation, socket.socket() as gateway_reservation:
                http_reservation.bind(("127.0.0.1", 0))
                gateway_reservation.bind(("127.0.0.1", 0))
                http_port = http_reservation.getsockname()[1]
                gateway_port = gateway_reservation.getsockname()[1]
            options = SimpleNamespace(host="127.0.0.1", http_port=http_port, gateway_port=gateway_port, db=Path(temporary) / "integration.db")
            with patch("sys.stdin", io.StringIO()), redirect_stdout(io.StringIO()):
                task = asyncio.create_task(launcher.run_services(options))
                try:
                    for _ in range(100):
                        if task.done():
                            await task
                        try:
                            peer = await websockets.connect(f"ws://127.0.0.1:{gateway_port}", open_timeout=0.1)
                            break
                        except OSError:
                            await asyncio.sleep(0.02)
                    else:
                        self.fail("The combined Gateway did not become ready")
                    try:
                        await peer.send('{"type":"browser_hello"}')
                        async with asyncio.timeout(2):
                            while True:
                                message = json.loads(await peer.recv())
                                if message.get("type") == "gateway_capabilities":
                                    self.assertIn("automation_simulation", message["features"])
                                    break
                        def fetch_interface():
                            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/", timeout=2) as response:
                                return response.read()
                        body = await asyncio.to_thread(fetch_interface)
                        self.assertIn(b"gateway-connection.js", body)
                        self.assertTrue(options.db.is_file())
                    finally:
                        await peer.close()
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            with socket.socket() as client:
                client.settimeout(1)
                self.assertNotEqual(client.connect_ex(("127.0.0.1", http_port)), 0)

    async def test_serves_web_only_and_closes_listener_on_gateway_failure(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            web = directory / "web"
            web.mkdir()
            (web / "index.html").write_text("test interface", encoding="utf-8")
            (directory / "private.txt").write_text("outside web", encoding="utf-8")
            # Reserve a test-only port; the actual server takes ownership below.
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            options = SimpleNamespace(host="127.0.0.1", http_port=port, gateway_port=8765, db=directory / "test.db")
            observed = []

            class TestGateway:
                def __init__(self, database):
                    observed.append(database)

                async def run(self):
                    def fetch():
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                            self_test.assertEqual(response.read(), b"test interface")
                        with self_test.assertRaises(urllib.error.HTTPError) as failure:
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/../private.txt", timeout=2)
                        self_test.assertEqual(failure.exception.code, 404)
                    await asyncio.to_thread(fetch)
                    raise RuntimeError("test gateway stopped")

            self_test = self
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "test gateway stopped"):
                await launcher.run_services(options, gateway_factory=TestGateway, web_directory=web)
            self.assertEqual(observed, [options.db])
            self.assertFalse(options.db.exists())
            with socket.socket() as client:
                client.settimeout(1)
                self.assertNotEqual(client.connect_ex(("127.0.0.1", port)), 0)

    async def test_rejects_same_ports_before_starting_gateway(self):
        options = SimpleNamespace(host="127.0.0.1", http_port=8765, gateway_port=8765, db=None)
        with self.assertRaisesRegex(ValueError, "different"):
            await launcher.run_services(options)

    def test_cli_validates_ports_and_preserves_explicit_database(self):
        options = launcher.arguments(["--http-port", "8099", "--gateway-port", "8777", "--db", "test-only.db"])
        self.assertEqual((options.http_port, options.gateway_port, options.db), (8099, 8777, Path("test-only.db")))
        for value in ("0", "65536", "-1"):
            with self.assertRaises(Exception):
                launcher.port_number(value)

    def test_project_environment_preserves_failed_start_exit_code(self):
        result = subprocess.run([
            sys._base_executable, str(launcher.ROOT / "run.py"),
            "--http-port", "32129", "--gateway-port", "32129",
        ], capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ports must be different", result.stderr)
