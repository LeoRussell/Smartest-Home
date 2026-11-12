"""Software endurance checks over isolated loopback peers, never a real ESP32.

Example: python gateway/soak_test.py --duration 86400 --interval 5 --report soak.json
"""

import argparse
import asyncio
from collections import OrderedDict, deque
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import socket
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkstemp
import time
import traceback

import websockets
from websockets.exceptions import ConnectionClosed

from runtime import Gateway


DEVICE = "soak-test-device"
SCHEMA = {
    "parameters": {"power": {"type": "boolean", "access": "read_write", "label": "Test power"}},
    "actions": {"soak_action": {"label": "Test action", "arguments": {}}},
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _LogTail(io.TextIOBase):
    """Long runs retain only bounded diagnostic output, not an endless log file."""

    def __init__(self):
        self.lines = deque(maxlen=80)
        self.partial = ""

    def write(self, text):
        for piece in (self.partial + text).splitlines(keepends=True):
            if piece.endswith(("\n", "\r")):
                self.lines.append(piece.rstrip()[-1000:])
                self.partial = ""
            else:
                self.partial = piece[-1000:]
        return len(text)

    def flush(self):
        pass


class _Peer:
    def __init__(self, socket, callback=None):
        self.socket = socket
        self.callback = callback
        self.mailbox = deque(maxlen=256)
        self.changed = asyncio.Event()
        self.failure = None
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for payload in self.socket:
                message = json.loads(payload)
                self.mailbox.append(message)
                self.changed.set()
                if self.callback:
                    await self.callback(message)
        except ConnectionClosed:
            pass
        except Exception as error:
            self.failure = error
        finally:
            self.changed.set()

    async def send(self, message):
        await self.socket.send(json.dumps(message))

    async def wait(self, kind, predicate=None, timeout=5):
        async with asyncio.timeout(timeout):
            while True:
                for message in self.mailbox:
                    if message.get("type") == kind and (predicate is None or predicate(message)):
                        self.mailbox.remove(message)
                        return message
                if self.failure:
                    raise self.failure
                if self.reader.done():
                    raise RuntimeError(f"Peer closed while waiting for {kind}")
                self.changed.clear()
                await self.changed.wait()

    async def close(self, reason="soak peer closed"):
        await self.socket.close(code=1001, reason=reason)
        await self.reader
        if self.failure:
            raise self.failure


class _Device:
    """Independent state machine with a bounded, simulated command-ID cache."""

    def __init__(self):
        self.peer = None
        self.value = False
        self.mode = "normal"
        self.cache = OrderedDict()
        self.deliveries = OrderedDict()
        self.effects = OrderedDict()
        self.total_deliveries = 0
        self.total_effects = 0
        self.sequence = 0

    @staticmethod
    def increment(counts, command_id):
        counts[command_id] = counts.get(command_id, 0) + 1
        while len(counts) > 64:
            counts.popitem(last=False)

    async def on_message(self, message):
        if message.get("type") != "command":
            return
        operation = message["data"]
        command_id = operation["command_id"]
        self.increment(self.deliveries, command_id)
        self.total_deliveries += 1
        if self.mode == "ignore":
            return
        cached = self.cache.get(command_id)
        if cached:
            await self.peer.send({**cached, "replayed": True})
            await self.state()
            return
        if operation.get("require_cached_result") is True:
            await self.peer.send({
                "type": "command_result", "device": DEVICE, "command_id": command_id,
                "success": False, "data": {"parameter": operation.get("parameter"),
                                          "action": operation.get("action"),
                                          "error": "Cached result is unavailable; execution was not repeated"},
            })
            await self.state()
            return
        require(operation.get("command") == "set" and operation.get("parameter") == "power", "Unexpected side effect requested from simulated device")
        self.value = operation["value"]
        self.increment(self.effects, command_id)
        self.total_effects += 1
        result = {"type": "command_result", "device": DEVICE, "command_id": command_id,
                  "success": True, "data": {"parameter": "power", "value": self.value}}
        self.cache[command_id] = result
        while len(self.cache) > 16:
            self.cache.popitem(last=False)
        if self.mode in ("lose_first_ack", "lose_first_ack_and_evict"):
            # Another source changes state after execution but before retry.
            # Replayed confirmation must not restore the historical True value.
            if self.mode == "lose_first_ack_and_evict":
                self.cache.pop(command_id)
            self.value = False
            await self.state()
            return
        await self.peer.send(result)
        await self.state()

    async def state(self):
        self.sequence += 1
        await self.peer.send({
            "type": "state", "device": DEVICE,
            "data": {"parameters": {"power": self.value}},
            "health": {"wifi_rssi": -60 - self.sequence % 15,
                       "uptime_seconds": 1000 + self.sequence, "reset_reason": "software_soak"},
        })
        return 1000 + self.sequence


class _HeadlessGateway(Gateway):
    async def console_loop(self):
        # An interactive terminal must not start a competing console input task.
        await asyncio.Event().wait()


class SoakRun:
    def __init__(self, database, timeout=0.08, restart_every=1):
        self.database = Path(database)
        self.timeout = timeout
        self.restart_every = restart_every
        self.gateway = None
        self.task = None
        self.address = None
        self.browser = None
        self.peers = []
        self.device = _Device()
        self.phase = "initialization"
        self.counts = {"gateway_starts": 0, "device_connections": 0, "browser_connections": 0,
                       "confirmed_commands": 0, "duplicate_acks": 0, "cached_replays": 0,
                       "expected_timeouts": 0, "guarded_cache_misses": 0, "recovered_manual_commands": 0,
                       "interrupted_scenarios": 0, "network_reconnections": 0}

    def setup(self):
        gateway = _HeadlessGateway(self.database)
        gateway.init_db()
        gateway.save_device_metadata(DEVICE, {
            "name": "Software soak peer", "schema": SCHEMA, "schema_version": 1,
            "firmware_id": DEVICE, "protocol_version": 2, "command_dedup": True,
            "command_replay_guard": True,
            "trust_policy": "allow",
        })
        config = {"version": 2, "name": "Soak timeout scenario", "workspace": {"blocks": {}},
                  "program": {"version": 1, "trigger": {"type": "manual"},
                              "body": [{"type": "invoke_action", "device": DEVICE,
                                        "action": "soak_action", "arguments": {}}]}}
        require(gateway.save_automation("soak-timeout", config)[0], "Could not create test scenario")
        require(gateway.save_automation("soak-interrupted", config)[0], "Could not create interruption scenario")

    async def start(self):
        self.phase = "gateway_start"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        gateway = _HeadlessGateway(self.database)
        gateway.HOST, gateway.PORT = "127.0.0.1", port
        gateway.COMMAND_TIMEOUT_SECONDS = self.timeout
        gateway.COMMAND_RETRY_LIMIT = 2
        self.gateway = gateway
        self.address = f"ws://127.0.0.1:{port}"
        self.task = asyncio.create_task(gateway.run())
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            if self.task.done():
                await self.task
                raise RuntimeError("Gateway exited during startup")
            try:
                self.browser = await self.open_peer()
                break
            except (OSError, TimeoutError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Isolated Gateway did not become ready")
                await asyncio.sleep(0.01)
        await self.register_browser(self.browser)
        self.counts["gateway_starts"] += 1

    async def open_peer(self, callback=None):
        # Closed browser/device objects must not accumulate in a 24-hour run.
        for peer in self.peers:
            if peer.reader.done() and peer.failure:
                raise peer.failure
        self.peers = [peer for peer in self.peers if not peer.reader.done()]
        peer = _Peer(await websockets.connect(self.address, open_timeout=0.5, close_timeout=1, ping_interval=None), callback)
        self.peers.append(peer)
        return peer

    async def register_browser(self, peer):
        await peer.send({"type": "browser_hello"})
        await peer.wait("history_settings")  # Barrier: final initial browser response.
        self.counts["browser_connections"] += 1

    async def connect_device(self):
        self.phase = "device_connect"
        self.device.peer = await self.open_peer(self.device.on_message)
        await self.device.peer.send({
            "type": "hello", "device": DEVICE, "name": "Software soak peer",
            "protocol_version": 2, "firmware_version": "software-soak",
            "schema_version": 1, "command_dedup": True, "command_replay_guard": True, "schema": SCHEMA,
        })
        response = await self.device.peer.wait("hello_ack")
        require(response.get("schema_status") == "saved", "Schema unexpectedly required operator review")
        await self.state_barrier()
        self.counts["device_connections"] += 1

    async def state_barrier(self):
        sequence = await self.device.state()
        await self.browser.wait("device_health", lambda message: message.get("device") == DEVICE and message.get("health", {}).get("uptime_seconds") == sequence)

    async def wait_until(self, predicate, timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if self.device.peer and self.device.peer.failure:
                raise self.device.peer.failure
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Invariant wait timed out during {self.phase}")
            await asyncio.sleep(0.005)

    async def command(self, command_id, value):
        await self.browser.send({"type": "browser_message", "action": "command", "device": DEVICE,
                                 "parameter": "power", "value": value, "command_id": command_id})

    def stored_result(self, command_id):
        with self.gateway.database_connection() as connection:
            row = connection.execute("SELECT status, result_json FROM command_queue WHERE command_id = ?", (command_id,)).fetchone()
        require(row is not None, f"Missing command record: {command_id}")
        return row[0], json.loads(row[1]) if row[1] else None

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        peers, self.peers = self.peers, []
        failures = await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)
        for failure in failures:
            if isinstance(failure, Exception):
                raise failure
        if self.gateway is not None:
            require(not self.gateway.automation_tasks and not self.gateway.automation_trigger_tasks, "Gateway shutdown left automation tasks")
            require(not any(pending.get("timeout_task") and not pending["timeout_task"].done() for pending in self.gateway.pending_commands.values()), "Gateway shutdown left command timers")

    async def cycle(self, number):
        device, gateway = self.device, self.gateway
        self.phase = "confirmed_command"
        device.mode = "normal"
        command_id = f"soak-{number}-confirmed"
        await self.command(command_id, True)
        result = await self.browser.wait("command_result", lambda message: message.get("command_id") == command_id)
        require(result.get("success") is True, "Normal command was not confirmed")
        await self.state_barrier()
        require(gateway.device_states[DEVICE]["power"] is True, "Confirmed state was lost")
        require(device.effects.get(command_id) == 1, "Command executed more than once")
        self.counts["confirmed_commands"] += 1

        self.phase = "duplicate_ack"
        await device.peer.send(device.cache[command_id])
        await self.state_barrier()
        require(self.stored_result(command_id)[0] == "succeeded", "Duplicate ACK changed final command status")
        require(command_id not in gateway.pending_commands, "Duplicate ACK recreated a command")
        self.counts["duplicate_acks"] += 1

        self.phase = "cached_replay"
        device.value = False
        await self.state_barrier()
        device.mode = "lose_first_ack"
        command_id = f"soak-{number}-replayed"
        await self.command(command_id, True)
        result = await self.browser.wait("command_result", lambda message: message.get("command_id") == command_id)
        require(result.get("success") is True and result.get("replayed") is True, "Lost ACK did not recover through the cached result")
        await self.state_barrier()
        require(device.deliveries.get(command_id) == 2 and device.effects.get(command_id) == 1, "A retry repeated the simulated side effect")
        require(gateway.device_states[DEVICE]["power"] is False, "Historical ACK replaced current telemetry")
        require(self.stored_result(command_id)[1].get("replayed") is True, "Replay was not stored transparently")
        self.counts["cached_replays"] += 1

        self.phase = "evicted_result_guard"
        device.mode = "lose_first_ack_and_evict"
        command_id = f"soak-{number}-evicted"
        await self.command(command_id, True)
        result = await self.browser.wait("command_result", lambda message: message.get("command_id") == command_id)
        require(result.get("success") is False and "Cached result is unavailable" in result.get("error", ""), "An evicted result was retried as a new execution")
        await self.state_barrier()
        require(device.deliveries.get(command_id) == 2 and device.effects.get(command_id) == 1, "Cache eviction repeated the simulated side effect")
        require(gateway.device_states[DEVICE]["power"] is False, "An unknown command result replaced live telemetry")
        require(self.stored_result(command_id)[0] == "failed", "Guard rejection was not stored as a failed result")
        self.counts["guarded_cache_misses"] += 1

        self.phase = "scenario_timeout"
        device.mode = "ignore"
        request_id = f"soak-{number}-timeout"
        await self.browser.send({"type": "browser_message", "action": "run_automation", "automation_id": "soak-timeout", "request_id": request_id})
        response = await self.browser.wait("automation_run_result", lambda message: message.get("request_id") == request_id)
        require(response.get("success") is True, "Timeout test scenario could not start")
        await self.wait_until(lambda: "soak-timeout" not in gateway.automation_tasks)
        history = gateway.get_automation_run_history("soak-timeout")
        require(history and history[0]["status"] == "failed", "Unacknowledged scenario did not finish as failed")
        with gateway.database_connection() as connection:
            timed_out = connection.execute("SELECT COUNT(*) FROM command_queue WHERE status = 'timeout' AND operation_json LIKE '%soak-timeout%'").fetchone()[0]
        require(timed_out >= number, "Scenario timeout was not recorded")
        self.counts["expected_timeouts"] += 1

        self.phase = "browser_reconnect"
        await self.browser.close()
        self.browser = await self.open_peer()
        await self.register_browser(self.browser)
        listing = await self.browser.wait("device_list")
        require(any(item["device"] == DEVICE and item["connected"] for item in listing["devices"]), "Reconnected browser did not see the live device")

        self.phase = "gateway_restart"
        # This stage tests interruption, not a race with the deliberately tiny
        # ACK deadline used above. Both outstanding operations are cancelled by
        # the planned stop/disconnect instead of expiring under a busy CI host.
        gateway.COMMAND_TIMEOUT_SECONDS = max(self.timeout, 5.0)
        manual_id = f"soak-{number}-recover"
        device.mode = "lose_first_ack"
        await self.command(manual_id, True)
        # Only a retained result may be replayed after a lost first response.
        # If execution itself never reached the device, the new replay guard
        # must reject recovery rather than perform a delayed side effect.
        await self.wait_until(lambda: manual_id in device.cache)
        device.mode = "ignore"
        before = set(device.deliveries)
        request_id = f"soak-{number}-interrupt"
        await self.browser.send({"type": "browser_message", "action": "run_automation", "automation_id": "soak-interrupted", "request_id": request_id})
        response = await self.browser.wait("automation_run_result", lambda message: message.get("request_id") == request_id)
        require(response.get("success") is True, "Interruption scenario could not start")
        await self.wait_until(lambda: any(identity not in before for identity in device.deliveries))
        restart = bool(self.restart_every and number % self.restart_every == 0)
        if restart:
            await self.stop()
            await self.start()
            gateway = self.gateway
            interrupted = gateway.get_automation_run_history("soak-interrupted")
            require(interrupted and interrupted[0]["status"] == "interrupted", "A running scenario survived Gateway restart")
            self.counts["interrupted_scenarios"] += 1
        else:
            self.phase = "network_disconnect"
            await device.peer.close(reason="simulated network loss")
            await self.wait_until(lambda: DEVICE not in gateway.devices and not gateway.automation_tasks)
            failed = gateway.get_automation_run_history("soak-interrupted")
            require(failed and failed[0]["status"] == "failed", "A scenario survived its device disconnection")
            self.counts["network_reconnections"] += 1
        require(set(gateway.pending_commands) == {manual_id}, "Recovery kept commands other than the one manual delivery")
        require(not gateway.automation_tasks, "An aborted scenario restarted automatically")
        device.mode = "normal"
        await self.connect_device()
        await self.wait_until(lambda: manual_id not in gateway.pending_commands)
        self.phase = "manual_recovery_confirmation"
        # Claiming an ACK removes pending before the asynchronous browser
        # broadcasts and SQLite completion finish. Observe committed state too.
        await self.wait_until(lambda: self.stored_result(manual_id)[0] == "succeeded")
        await self.state_barrier()
        require(self.stored_result(manual_id)[0] == "succeeded", "Manual queued command did not recover after restart")
        require(device.effects.get(manual_id) == 1, "Recovery repeated the queued command side effect")
        require(not gateway.pending_commands, "Cycle left an unexpected pending command")
        gateway.COMMAND_TIMEOUT_SECONDS = self.timeout
        self.counts["recovered_manual_commands"] += 1


async def run_soak(*, cycles=None, duration=None, interval=5.0, command_timeout=0.08, restart_every=1, progress=None, checkpoint=None):
    """Return a JSON-compatible report. All Gateway writes stay in a temporary DB."""
    if cycles is None and duration is None:
        cycles = 10
    if cycles is not None and duration is not None:
        raise ValueError("Choose cycles or duration, not both")
    if cycles is not None and (type(cycles) is not int or cycles < 1):
        raise ValueError("cycles must be a positive integer")
    if duration is not None and (type(duration) not in (int, float) or duration <= 0 or not math.isfinite(duration)):
        raise ValueError("duration must be finite and positive")
    if interval < 0 or not math.isfinite(interval) or not 0.01 <= command_timeout <= 30:
        raise ValueError("interval or command_timeout is out of range")
    if type(restart_every) is not int or restart_every < 0:
        raise ValueError("restart_every must be a nonnegative integer")
    if duration is None and cycles is None:
        raise ValueError("Specify cycles or duration")
    started = time.monotonic()
    report = {"format": "smartest-home-soak-report", "version": 1,
              "scope": "software_loopback", "physical_device_tested": False,
              "started_at": utc_now(), "status": "passed", "cycles_completed": 0,
              "requested_cycles": cycles, "requested_duration_seconds": duration,
              "interval_seconds": interval, "restart_every": restart_every, "errors": []}
    log = _LogTail()
    runner = None
    try:
        with TemporaryDirectory(prefix="smartest-home-soak-") as temporary, redirect_stdout(log):
            runner = SoakRun(Path(temporary) / "gateway.db", timeout=command_timeout, restart_every=restart_every)
            try:
                runner.setup()
                if checkpoint:
                    checkpoint({**report, "status": "running", "updated_at": utc_now(), "elapsed_seconds": round(time.monotonic() - started, 3)})
                await runner.start()
                await runner.connect_device()
                last_progress = started
                while (cycles is None or report["cycles_completed"] < cycles) and (duration is None or report["cycles_completed"] == 0 or time.monotonic() - started < duration):
                    await runner.cycle(report["cycles_completed"] + 1)
                    report["cycles_completed"] += 1
                    now = time.monotonic()
                    if report["cycles_completed"] == 1 or now - last_progress >= 30:
                        if progress:
                            progress(report["cycles_completed"], now - started)
                        if checkpoint:
                            checkpoint({**report, "status": "running", "updated_at": utc_now(),
                                        "elapsed_seconds": round(now - started, 3), "counts": runner.counts,
                                        "gateway_log_tail": list(log.lines)})
                        last_progress = now
                    remaining = duration - (time.monotonic() - started) if duration is not None else interval
                    if interval and remaining > 0 and (cycles is None or report["cycles_completed"] < cycles):
                        await asyncio.sleep(min(interval, remaining))
            except asyncio.CancelledError:
                report["status"] = "interrupted"
            except Exception as error:
                report["status"] = "failed"
                report["errors"].append({"phase": runner.phase, "cycle": report["cycles_completed"] + 1,
                                         "type": type(error).__name__, "message": str(error),
                                         "traceback": traceback.format_exc(limit=8)})
            finally:
                try:
                    await runner.stop()
                except Exception as error:
                    report["status"] = "failed"
                    report["errors"].append({"phase": "cleanup", "type": type(error).__name__, "message": str(error)})
                report["counts"] = runner.counts
                report["simulated_deliveries"] = runner.device.total_deliveries
                report["simulated_side_effects"] = runner.device.total_effects
                report["database_bytes"] = runner.database.stat().st_size if runner.database.exists() else 0
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at"] = utc_now()
        report["gateway_log_tail"] = list(log.lines)
    if checkpoint:
        checkpoint(report)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--cycles", type=int, help="Number of complete cycles (default: 10).")
    limit.add_argument("--duration", type=float, help="Run for at least this many seconds, completing the current cycle.")
    parser.add_argument("--interval", type=float, default=5.0, help="Pause between cycles in seconds (default: 5).")
    parser.add_argument("--command-timeout", type=float, default=0.08, help="Simulated ACK deadline, 0.01..30 seconds (default: 0.08).")
    parser.add_argument("--restart-every", type=int, default=1, help="Restart Gateway every N cycles; 0 keeps one Gateway instance running (default: 1).")
    parser.add_argument("--report", type=Path, help="JSON report path; otherwise a new temporary report file is used.")
    options = parser.parse_args(argv)
    if options.cycles is not None and options.cycles < 1:
        parser.error("--cycles must be positive")
    if options.duration is not None and (options.duration <= 0 or not math.isfinite(options.duration)):
        parser.error("--duration must be finite and positive")
    if options.interval < 0 or not math.isfinite(options.interval) or not 0.01 <= options.command_timeout <= 30:
        parser.error("--interval or --command-timeout is out of range")
    if options.restart_every < 0:
        parser.error("--restart-every must be nonnegative")
    if options.report is not None:
        try:
            validate_report_path(options.report)
        except ValueError as error:
            parser.error(str(error))
    return options


def validate_report_path(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".json":
        raise ValueError("--report must be a .json file, never a database path")
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as error:
            raise ValueError("Refusing to replace an existing file that is not a soak report") from error
        if not isinstance(previous, dict) or previous.get("format") != "smartest-home-soak-report":
            raise ValueError("Refusing to replace an unrelated JSON file")
    return path


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".soak-report-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    options = arguments(argv)
    console = sys.stdout
    if options.report is None:
        descriptor, filename = mkstemp(prefix="smartest-home-soak-", suffix=".json")
        os.close(descriptor)
        destination = Path(filename)
    else:
        destination = options.report.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(run_soak(cycles=options.cycles,
                                     duration=options.duration, interval=options.interval,
                                     command_timeout=options.command_timeout,
                                     restart_every=options.restart_every,
                                     progress=lambda cycles, elapsed: print(f"Completed {cycles} cycles in {elapsed:.1f}s", file=console, flush=True),
                                     checkpoint=lambda report: write_report(destination, report)))
    except (OSError, ValueError) as error:
        print(f"Could not run or write soak report: {error}", file=sys.stderr)
        return 1
    print(f"Result: {report['status']}; cycles={report['cycles_completed']}; report={destination}")
    if report["errors"]:
        print(json.dumps(report["errors"], ensure_ascii=False), file=sys.stderr)
    return 0 if report["status"] == "passed" else 130 if report["status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
