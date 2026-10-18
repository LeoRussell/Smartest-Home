import asyncio
import json
import sqlite3
from pathlib import Path
from itertools import count
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gateway.db"

# Активные ESP32 и браузерные подключения
devices = {}
browser_clients = set()

# Последние известные состояния в памяти
device_states = {}

command_ids = count(1)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                led INTEGER,
                connected INTEGER NOT NULL DEFAULT 0
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_value TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)


def save_device_seen(device_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO devices (device_id, last_seen, connected)
            VALUES (?, ?, 1)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                connected = 1
        """, (device_id, now()))


def save_device_state(device_id, parameters):
    led_value = parameters.get("led")
    led_db_value = int(led_value) if isinstance(led_value, bool) else None

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO devices (device_id, last_seen, led, connected)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                led = COALESCE(excluded.led, devices.led),
                connected = 1
        """, (device_id, now(), led_db_value))


def save_device_event(device_id, event_type, event_value):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO device_events (
                device_id, event_type, event_value, timestamp
            )
            VALUES (?, ?, ?, ?)
        """, (device_id, event_type, str(event_value), now()))


def mark_disconnected(device_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            UPDATE devices SET connected = 0
            WHERE device_id = ?
        """, (device_id,))


def get_device_from_db(device_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
            SELECT device_id, last_seen, led, connected
            FROM devices WHERE device_id = ?
        """, (device_id,)).fetchone()
        return dict(row) if row else None


def get_all_devices_from_db():
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT device_id, last_seen, led, connected
            FROM devices ORDER BY device_id
        """).fetchall()
        return [dict(row) for row in rows]


def get_device_events(device_id, limit=10):
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT event_type, event_value, timestamp
            FROM device_events
            WHERE device_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (device_id, limit)).fetchall()
        return [dict(row) for row in rows]


async def send_json(websocket, message):
    await websocket.send(json.dumps(message))


async def broadcast(message):
    """Отправляет сообщение всем подключённым браузерам."""
    if not browser_clients:
        return

    results = await asyncio.gather(
        *(send_json(client, message) for client in list(browser_clients)),
        return_exceptions=True,
    )

    for client, result in zip(list(browser_clients), results):
        if isinstance(result, Exception):
            browser_clients.discard(client)


def make_device_list():
    """Формирует снимок устройств для браузера."""
    result = []

    for saved in get_all_devices_from_db():
        device_id = saved["device_id"]
        result.append({
            "device": device_id,
            "connected": device_id in devices,
            "parameters": device_states.get(device_id, {}),
            "last_seen": saved["last_seen"],
        })

    # Учитываем устройства, которые подключены, но ещё не сохранены.
    known_ids = {item["device"] for item in result}

    for device_id in devices:
        if device_id not in known_ids:
            result.append({
                "device": device_id,
                "connected": True,
                "parameters": device_states.get(device_id, {}),
                "last_seen": None,
            })

    return result


async def send_device_list(websocket):
    await send_json(websocket, {
        "type": "device_list",
        "devices": make_device_list(),
    })


async def handle_browser_message(websocket, data):
    action = data.get("action")

    if action == "get_devices":
        await send_device_list(websocket)
        return

    if action == "command":
        device_id = data.get("device")
        parameter = data.get("parameter")
        value = data.get("value")

        if not isinstance(device_id, str):
            await send_json(websocket, {
                "type": "error",
                "message": "Некорректный ID устройства",
            })
            return

        if parameter != "led" or not isinstance(value, bool):
            await send_json(websocket, {
                "type": "error",
                "message": "Поддерживается только команда управления LED",
            })
            return

        device_ws = devices.get(device_id)

        if device_ws is None:
            await send_json(websocket, {
                "type": "error",
                "message": f"Устройство {device_id} не подключено",
            })
            return

        command_id = f"cmd-{next(command_ids):03d}"

        command = {
            "type": "command",
            "id": command_id,
            "device": device_id,
            "data": {
                "command": "set",
                "command_id": command_id,
                "parameter": parameter,
                "value": value,
            },
        }

        try:
            await send_json(device_ws, command)
            await send_json(websocket, {
                "type": "command_sent",
                "device": device_id,
                "parameter": parameter,
                "value": value,
                "command_id": command_id,
            })
        except ConnectionClosed:
            await send_json(websocket, {
                "type": "error",
                "message": f"Соединение с {device_id} закрыто",
            })


async def handle_client(websocket):
    client_type = None
    device_id = None

    print(f"Новое подключение: {websocket.remote_address}")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await send_json(websocket, {
                    "type": "error",
                    "message": "Некорректный JSON",
                })
                continue

            if not isinstance(data, dict):
                continue

            message_type = data.get("type")

            # Браузер регистрируется отдельным сообщением.
            if message_type == "browser_hello":
                client_type = "browser"
                browser_clients.add(websocket)
                print("Подключён веб-браузер")
                await send_device_list(websocket)
                continue

            # Остальные сообщения браузера.
            if client_type == "browser":
                await handle_browser_message(websocket, data)
                continue

            # Регистрация ESP32.
            if message_type == "hello":
                new_device_id = data.get("device")

                if (
                    not isinstance(new_device_id, str)
                    or not new_device_id.strip()
                ):
                    print("Ошибка: некорректный ID устройства")
                    continue

                client_type = "device"
                device_id = new_device_id

                devices[device_id] = websocket
                device_states.setdefault(device_id, {})
                save_device_seen(device_id)

                await send_json(websocket, {
                    "type": "welcome",
                    "id": "welcome-001",
                    "device": device_id,
                    "data": {"message": "Gateway is ready"},
                })

                print(f"Устройство зарегистрировано: {device_id}")
                await broadcast({
                    "type": "device_list",
                    "devices": make_device_list(),
                })
                continue

            if client_type != "device" or device_id is None:
                print("Получено сообщение от незарегистрированного клиента")
                continue

            if message_type == "state":
                payload = data.get("data", {})
                if not isinstance(payload, dict):
                    continue

                parameters = payload.get("parameters", {})
                if not isinstance(parameters, dict):
                    continue

                device_states.setdefault(device_id, {}).update(parameters)
                save_device_state(device_id, parameters)

                for parameter, value in parameters.items():
                    save_device_event(device_id, parameter, value)

                print(f"Состояние {device_id}: {device_states[device_id]}")

                await broadcast({
                    "type": "device_state",
                    "device": device_id,
                    "parameters": device_states[device_id],
                    "last_seen": now(),
                })

            elif message_type == "error":
                print(f"Ошибка устройства {device_id}: {data}")

            else:
                print(f"Неизвестный тип сообщения: {message_type}")

    except ConnectionClosed:
        pass

    finally:
        if client_type == "browser":
            browser_clients.discard(websocket)
            print("Веб-браузер отключён")

        elif (
            client_type == "device"
            and device_id
            and devices.get(device_id) is websocket
        ):
            del devices[device_id]
            mark_disconnected(device_id)
            print(f"Устройство отключено: {device_id}")

            await broadcast({
                "type": "device_list",
                "devices": make_device_list(),
            })


async def console():
    selected_device = None

    while True:
        raw_command = await asyncio.to_thread(
            input,
            "Команда (on/off/info/events/status/devices/history/use/exit): ",
        )
        parts = raw_command.strip().split()

        if not parts:
            continue

        action = parts[0].lower()

        if action == "exit":
            print("Завершение работы Gateway...")
            return

        elif action == "status":
            print(f"Подключено устройств: {len(devices)}")

        elif action == "devices":
            if devices:
                print("Подключённые устройства:")
                for device_id in devices:
                    marker = " (выбрано)" if device_id == selected_device else ""
                    print(f"- {device_id}{marker}")
            else:
                print("Подключённые устройства отсутствуют.")
            print(f"Всего подключено: {len(devices)}")

        elif action == "use":
            if len(parts) != 2:
                print("Использование: use <device_id>")
                continue

            if parts[1] not in devices:
                print(f"Устройство {parts[1]} не подключено")
                continue

            selected_device = parts[1]
            print(f"Выбрано устройство: {selected_device}")

        elif action == "info":
            if selected_device is None:
                print("Сначала выбери устройство: use <device_id>")
                continue

            state = get_device_from_db(selected_device)
            if state is None:
                print("Информация об устройстве не найдена")
                continue

            print(f"Устройство: {selected_device}")
            print("Подключение:", "активно" if selected_device in devices else "отсутствует")
            print(f"Последнее сообщение: {state['last_seen']}")

            if state["led"] is None:
                print("LED: состояние неизвестно")
            else:
                print("LED:", "включён" if state["led"] else "выключен")

        elif action == "events":
            if selected_device is None:
                print("Сначала выбери устройство: use <device_id>")
                continue

            events = get_device_events(selected_device)
            if not events:
                print("История событий пока пуста.")
                continue

            print(f"Последние события: {selected_device}")
            for event in reversed(events):
                value = event["event_value"]

                if event["event_type"] == "led":
                    if value.lower() == "true":
                        value = "включён"
                    elif value.lower() == "false":
                        value = "выключен"

                print(
                    f"{event['timestamp']} | "
                    f"{event['event_type']}: {value}"
                )

        elif action == "history":
            saved_devices = get_all_devices_from_db()
            if not saved_devices:
                print("В базе пока нет устройств.")
                continue

            for state in saved_devices:
                led = (
                    "неизвестно" if state["led"] is None
                    else "включён" if state["led"]
                    else "выключен"
                )
                connection = (
                    "подключено"
                    if state["device_id"] in devices
                    else "не подключено"
                )
                print(
                    f"{state['device_id']} | LED: {led} | {connection} | "
                    f"последнее сообщение: {state['last_seen']}"
                )

        elif action in ("on", "off"):
            if selected_device is None:
                print("Сначала выбери устройство: use <device_id>")
                continue

            device_ws = devices.get(selected_device)
            if device_ws is None:
                print(f"Устройство {selected_device} отключено")
                selected_device = None
                continue

            command_id = f"cmd-{next(command_ids):03d}"
            command = {
                "type": "command",
                "id": command_id,
                "device": selected_device,
                "data": {
                    "command": "set",
                    "command_id": command_id,
                    "parameter": "led",
                    "value": action == "on",
                },
            }

            try:
                await send_json(device_ws, command)
                print(f"Команда {action} отправлена → {selected_device}")
            except ConnectionClosed:
                print(f"Соединение с {selected_device} закрыто")

        else:
            print(
                "Доступно: on, off, info, events, status, devices, "
                "history, use <device_id>, exit"
            )


async def main():
    init_db()

    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        print(f"База данных: {DB_PATH}")
        print("Gateway запущен на порту 8765")
        print("Ожидаем подключения ESP32 и браузера...")
        await console()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway остановлен")