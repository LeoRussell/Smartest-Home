"""Gateway instance state, console and application lifecycle."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import websockets
from websockets.exceptions import ConnectionClosed
import settings
from storage import StorageMixin
from schema import SchemaMixin
from configuration import ConfigurationMixin
from commands import CommandsMixin
from automation import AutomationMixin
from protocol import ProtocolMixin
from device_management import DeviceManagement


class Gateway(StorageMixin, SchemaMixin, ConfigurationMixin, CommandsMixin, AutomationMixin, ProtocolMixin):
    def __init__(self, database_path=None):
        self.AUTOMATION_RUN_HISTORY_LIMIT = settings.AUTOMATION_RUN_HISTORY_LIMIT
        self.BASE_DIR = settings.BASE_DIR
        self.COMMAND_RETRY_LIMIT = settings.COMMAND_RETRY_LIMIT
        self.COMMAND_TIMEOUT_SECONDS = settings.COMMAND_TIMEOUT_SECONDS
        self.CONFIGURATION_APPLY_TIMEOUT_SECONDS = settings.CONFIGURATION_APPLY_TIMEOUT_SECONDS
        self.DATABASE_BACKUP_DIR = settings.DATABASE_BACKUP_DIR
        self.DATABASE_BACKUP_DOWNLOAD_LIMIT = settings.DATABASE_BACKUP_DOWNLOAD_LIMIT
        self.DATABASE_BACKUP_ID_PATTERN = settings.DATABASE_BACKUP_ID_PATTERN
        self.DATABASE_BACKUP_LIMIT = settings.DATABASE_BACKUP_LIMIT
        self.DB_PATH = settings.DB_PATH
        self.DB_PATH_VALUE = settings.DB_PATH_VALUE
        self.DEVICE_APPROVAL_TIMEOUT_SECONDS = settings.DEVICE_APPROVAL_TIMEOUT_SECONDS
        self.DEVICE_CONFIGURATION_EXPORT_DIR = settings.DEVICE_CONFIGURATION_EXPORT_DIR
        self.DEVICE_CONFIGURATION_EXPORT_DOWNLOAD_LIMIT = settings.DEVICE_CONFIGURATION_EXPORT_DOWNLOAD_LIMIT
        self.DEVICE_STALE_SECONDS = settings.DEVICE_STALE_SECONDS
        self.DEVICE_TRANSPORT_IDLE_SECONDS = settings.DEVICE_TRANSPORT_IDLE_SECONDS
        self.DEVICE_TRANSPORT_PROBE_TIMEOUT_SECONDS = settings.DEVICE_TRANSPORT_PROBE_TIMEOUT_SECONDS
        self.EVENTS_PAGE_SIZE = settings.EVENTS_PAGE_SIZE
        self.GATEWAY_RUNTIME_CHECKPOINT_SECONDS = settings.GATEWAY_RUNTIME_CHECKPOINT_SECONDS
        self.HOST = settings.HOST
        self.MAX_CONFIGURATION_VERSION = settings.MAX_CONFIGURATION_VERSION
        self.MAX_SCHEMA_BYTES = settings.MAX_SCHEMA_BYTES
        self.MAX_WEBSOCKET_MESSAGE_BYTES = settings.MAX_WEBSOCKET_MESSAGE_BYTES
        self.PORT = settings.PORT
        self.GATEWAY_SESSION_STARTED_AT = None
        self.devices = {}
        self.device_wire_ids = {}
        self.device_handler_tasks = set()
        self.browser_clients = set()
        self.device_states = {}
        self.device_metadata = {}
        self.last_device_heartbeat = {}
        self.last_device_telemetry = {}
        self.last_parameter_telemetry = {}
        self.last_device_probe = {}
        self.fresh_device_parameters = {}
        self.deferred_connection_automations = set()
        self.pending_device_schemas = {}
        self.deferred_device_schemas = {}
        self.pending_device_connections = {}
        self.configuration_apply_tasks = {}
        self.configuration_confirmed_devices = set()
        self.pending_commands = {}
        self.automation_tasks = {}
        self.automation_trigger_tasks = {}
        self.automation_trigger_devices = {}
        self.database_restore_in_progress = False
        self.automation_stop_requests = set()
        self.automation_waiting_tasks = set()
        self.automation_restart_tasks = set()
        if database_path is not None:
            self.DB_PATH = Path(database_path).expanduser()
            self.DATABASE_BACKUP_DIR = self.DB_PATH.parent / "backups"
            self.DEVICE_CONFIGURATION_EXPORT_DIR = self.DB_PATH.parent / "schemas"
        self.management = DeviceManagement(self)


    def current_time(self):
        return datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )


    async def gateway_runtime_checkpoint_loop(self):
        while True:
            await asyncio.sleep(self.GATEWAY_RUNTIME_CHECKPOINT_SECONDS)
            self.checkpoint_gateway_runtime()


    async def monitor_gateway_loop(self):
        loop = asyncio.get_running_loop()
        expected = loop.time() + 1
        while True:
            await asyncio.sleep(max(0, expected - loop.time()))
            now = loop.time()
            self.connections.record_loop_lag(max(0, now - expected))
            expected = now + 1


    def device_is_connected(self, device_id):
        return device_id in self.devices


    def record_device_telemetry(self, device_id, parameters):
        if not parameters:
            return
        now = asyncio.get_running_loop().time()
        self.last_device_telemetry[device_id] = now
        timestamps = self.last_parameter_telemetry.setdefault(device_id, {})
        for parameter in parameters:
            timestamps[parameter] = now
        self.fresh_device_parameters.setdefault(device_id, set()).update(parameters)


    def parameter_value_is_fresh(self, device_id, parameter):
        timestamp = self.last_parameter_telemetry.get(device_id, {}).get(parameter)
        if (
            not self.device_is_connected(device_id)
            or parameter not in self.fresh_device_parameters.get(device_id, set())
            or timestamp is None
        ):
            return False
        return 0 <= asyncio.get_running_loop().time() - timestamp <= self.DEVICE_STALE_SECONDS


    async def console_loop(self):
        print()
        print("Команды консоли:")
        print("  devices          — список устройств")
        print("  status           — статус Gateway")
        print("  schema [device]  — показать Schema устройства")
        print("  set <param> <json> — установить значение параметра")
        print("  call <action_id> [json_arguments] — вызвать действие Schema")
        print("  use <device_id>  — выбрать устройство")
        print("  events           — последние события")
        print("  history          — устройства в базе данных")
        print("  exit             — остановить Gateway")
        print()

        selected_device = None

        while True:
            try:
                command_line = await asyncio.to_thread(
                    input,
                    "Gateway> "
                )
            except (EOFError, KeyboardInterrupt):
                return

            parts = command_line.strip().split(maxsplit=2)

            if not parts:
                continue

            command = parts[0].lower()

            if command == "exit":
                print("Останавливаем Gateway...")
                return

            elif command == "status":
                print(f"WebSocket: ws://{self.HOST}:{self.PORT}")
                print(f"Подключено ESP32: {len(self.devices)}")
                print(f"Подключено браузеров: {len(self.browser_clients)}")

            elif command == "devices":
                all_devices = self.get_all_devices()

                for item in all_devices:
                    status = (
                        "онлайн"
                        if item["connected"]
                        else "офлайн"
                    )

                    print(
                        f'{item["device"]} ({item.get("name") or "без имени"}): '
                        f'{status}, firmware={item.get("firmware_version") or "—"}, '
                        f'schema={item.get("schema_version") or "—"}, '
                        f'{item["parameters"]}'
                    )

                if not all_devices:
                    print("Устройств пока нет.")

            elif command == "use":
                if len(parts) < 2:
                    print("Использование: use <device_id>")
                else:
                    selected_device = parts[1]
                    print(f"Выбрано устройство: {selected_device}")

            elif command == "schema":
                target_device = parts[1] if len(parts) > 1 else selected_device
                if not target_device:
                    print("Укажи устройство: schema <device_id> или выбери его через use.")
                    continue
                item = next(
                    (entry for entry in self.get_all_devices() if entry["device"] == target_device),
                    None
                )
                if item is None:
                    print(f"Устройство {target_device} не найдено.")
                else:
                    print(json.dumps(item["schema"], ensure_ascii=False, indent=2))

            elif command == "set":
                if not selected_device:
                    print("Сначала выбери устройство: use <device_id>")
                    continue
                if len(parts) < 3:
                    print('Использование: set <parameter> <json_value> (например: set led true)')
                    continue
                try:
                    value = json.loads(parts[2])
                except (ValueError, RecursionError) as error:
                    print(f"Значение должно быть корректным JSON: {error}")
                    continue
                await self.dispatch_device_command(selected_device, parts[1], value)

            elif command == "call":
                if not selected_device:
                    print("Сначала выбери устройство: use <device_id>")
                    continue
                if len(parts) < 2:
                    print("Использование: call <action_id> [json_arguments]")
                    continue
                arguments = {}
                if len(parts) > 2:
                    try:
                        arguments = json.loads(parts[2])
                    except (ValueError, RecursionError) as error:
                        print(f"Аргументы должны быть корректным JSON: {error}")
                        continue
                if not isinstance(arguments, dict):
                    print("Аргументы действия должны быть JSON-объектом.")
                    continue
                await self.dispatch_device_action(
                    selected_device,
                    parts[1],
                    arguments=arguments
                )

            elif command == "events":
                for event in self.get_recent_events(10):
                    print(
                        f'{event["timestamp"]} | '
                        f'{event["device_id"]} | '
                        f'{event["event_type"]}: '
                        f'{event["event_value"]}'
                    )

            elif command == "history":
                with self.database_connection() as conn:
                    rows = conn.execute("""
                        SELECT device_id, name, last_seen, state_json, led, connected,
                               firmware_version, schema_version
                        FROM devices
                        ORDER BY device_id
                    """).fetchall()

                for row in rows:
                    try:
                        state = json.loads(row[3] or "{}")
                    except (TypeError, ValueError, RecursionError):
                        state = {}
                    if not isinstance(state, dict):
                        state = {}
                    if row[4] is not None:
                        state.setdefault("led", bool(row[4]))
                    print(
                        f"ID: {row[0]} ({row[1] or 'без имени'}), "
                        f"последнее сообщение: {row[2]}, "
                        f"состояние: {state}, "
                        f"firmware: {row[6] or '—'}, schema: {row[7] or '—'}, "
                        f"подключено сейчас: {row[0] in self.devices}"
                    )

                if not rows:
                    print("История устройств пока пуста.")

            else:
                print(
                    "Неизвестная команда. Доступно: devices, status, schema, set, "
                    "call, use, events, history, exit."
                )


    async def configure_device_keepalive(self, websocket):
        """Use the activity-aware watchdog for devices, keeping browser defaults.

        websockets starts its keepalive before the peer identifies itself. Its
        strict Pong deadline must not race the device watchdog or close a link
        that is still delivering application messages.
        """
        websocket.ping_timeout = None
        keepalive = getattr(websocket, "keepalive_task", None)
        if keepalive is not None:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)


    def device_transport_probe_due(self, device_id, now):
        last_activity = self.last_device_heartbeat.get(device_id, now)
        last_probe = self.last_device_probe.get(device_id, last_activity)
        return now - max(last_activity, last_probe) > self.DEVICE_TRANSPORT_IDLE_SECONDS


    async def probe_device_transport(self, device_id, websocket):
        now = asyncio.get_running_loop().time()
        if self.devices.get(device_id) is not websocket or not self.device_transport_probe_due(device_id, now):
            return
        activity_before_probe = self.last_device_heartbeat.get(device_id)
        self.last_device_probe[device_id] = now
        try:
            pong = await websocket.ping()
            latency = await asyncio.wait_for(pong, timeout=self.DEVICE_TRANSPORT_PROBE_TIMEOUT_SECONDS)
            self.connections.probe(device_id, websocket, rtt_seconds=latency)
            print(f"[PRESENCE] {device_id}: WebSocket ping confirmed; application silence is not a disconnect.")
            return
        except ConnectionClosed:
            return
        except (asyncio.TimeoutError, OSError) as error:
            # A buffered application message can arrive alongside the timeout,
            # for example after the host resumes. Let its handler run first.
            await asyncio.sleep(0)
            if self.devices.get(device_id) is not websocket:
                return
            self.connections.probe(device_id, websocket, failed=True)
            last_activity = self.last_device_heartbeat.get(device_id)
            if (
                last_activity is not None
                and last_activity != activity_before_probe
                and asyncio.get_running_loop().time() - last_activity <= self.DEVICE_TRANSPORT_IDLE_SECONDS
            ):
                print(f"[PRESENCE] {device_id}: application data resumed during the probe; connection kept.")
                return
            last_data = self.last_device_telemetry.get(device_id)
            data_age = f"{now - last_data:.1f}s" if last_data is not None else "none"
            print(
                f"[ПРЕДУПРЕЖДЕНИЕ] {device_id}: transport ping failed "
                f"({type(error).__name__}); probe_elapsed={asyncio.get_running_loop().time() - now:.1f}s, "
                f"last_data_age={data_age}; closing code=1011."
            )
            try:
                await websocket.close(code=1011, reason="Device did not answer a WebSocket ping")
            except (ConnectionClosed, OSError):
                pass


    async def device_presence_watchdog(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(5)
            now = loop.time()
            probes = [
                self.probe_device_transport(device_id, websocket)
                for device_id, websocket in list(self.devices.items())
                if self.device_transport_probe_due(device_id, now)
            ]
            if probes:
                results = await asyncio.gather(*probes, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        print(f"[PRESENCE] Could not probe device transport: {result}")


    async def run(self):
        self.init_db()
        self.load_pending_commands()
        self.sync_device_configuration_exports()

        print("=" * 48)
        print("        IoT Gateway запускается")
        print("=" * 48)

        async with websockets.serve(
            self.handle_client,
            self.HOST,
            self.PORT,
            process_request=self.handle_http_request,
            max_size=self.MAX_WEBSOCKET_MESSAGE_BYTES,
            max_queue=16,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5
        ):
            print(f"WebSocket-сервер запущен на порту {self.PORT}")
            print("Ожидание подключений ESP32 и браузера...")
            watchdog_task = asyncio.create_task(self.device_presence_watchdog())
            runtime_task = asyncio.create_task(self.gateway_runtime_checkpoint_loop())
            maintenance_task = asyncio.create_task(self.management.maintenance_loop())
            lag_task = asyncio.create_task(self.monitor_gateway_loop())
            self.fire_automation_event("gateway_started")
            try:
                if sys.stdin.isatty():
                    await self.console_loop()
                else:
                    print("Интерактивная консоль отключена; Gateway работает в служебном режиме.")
                    await asyncio.Event().wait()
            finally:
                self.connections.close_all("gateway_shutdown")
                await self.management.shutdown()
                applying_tasks = [
                    self.cancel_configuration_apply(device_id, pending=True)
                    for device_id in list(self.configuration_apply_tasks)
                ]
                if applying_tasks:
                    await asyncio.gather(*applying_tasks, return_exceptions=True)
                watchdog_task.cancel()
                runtime_task.cancel()
                maintenance_task.cancel()
                lag_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                try:
                    await runtime_task
                except asyncio.CancelledError:
                    pass
                try:
                    await maintenance_task
                except asyncio.CancelledError:
                    pass
                try:
                    await lag_task
                except asyncio.CancelledError:
                    pass
                self.checkpoint_gateway_runtime(final=True)
