import asyncio
import json
import sqlite3
from pathlib import Path
from itertools import count
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed


# Пути и база данных
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gateway.db"

# Активные WebSocket-соединения
devices = {}

# Последние известные состояния в памяти
device_states = {}

# Счётчик ID команд
command_ids = count(1)


def now():
    """Текущее время в UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    """Создаёт таблицу, если её ещё нет."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                led INTEGER,
                connected INTEGER NOT NULL DEFAULT 0
            )
        """)


def save_device_seen(device_id):
    """Сохраняет факт получения сообщения от устройства."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO devices (device_id, last_seen, connected)
            VALUES (?, ?, 1)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                connected = 1
        """, (device_id, now()))


def save_device_state(device_id, parameters):
    """Сохраняет известные параметры устройства."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO devices (device_id, last_seen, led, connected)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                led = COALESCE(excluded.led, devices.led),
                connected = 1
        """, (
            device_id,
            now(),
            int(parameters["led"]) if isinstance(parameters.get("led"), bool) else None,
        ))


def mark_disconnected(device_id):
    """Отмечает устройство как отключённое."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            UPDATE devices
            SET connected = 0
            WHERE device_id = ?
        """, (device_id,))


def get_device_from_db(device_id):
    """Читает сохранённую запись устройства."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
            SELECT device_id, last_seen, led, connected
            FROM devices
            WHERE device_id = ?
        """, (device_id,)).fetchone()

        return dict(row) if row else None


def get_all_devices_from_db():
    """Читает все сохранённые устройства."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT device_id, last_seen, led, connected
            FROM devices
            ORDER BY device_id
        """).fetchall()

        return [dict(row) for row in rows]


async def handle_client(websocket):
    device_id = None

    print(f"Новое подключение: {websocket.remote_address}")

    try:
        async for message in websocket:
            print(f"Получено: {message}")

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("Ошибка: некорректный JSON")
                continue

            if not isinstance(data, dict):
                print("Ошибка: ожидался JSON-объект")
                continue

            message_type = data.get("type")

            # Регистрация устройства
            if message_type == "hello":
                new_device_id = data.get("device")

                if (
                    not isinstance(new_device_id, str)
                    or not new_device_id.strip()
                ):
                    print("Ошибка: некорректный ID устройства")
                    continue

                device_id = new_device_id

                devices[device_id] = websocket
                device_states.setdefault(device_id, {})

                save_device_seen(device_id)

                welcome = {
                    "type": "welcome",
                    "id": "welcome-001",
                    "device": device_id,
                    "data": {"message": "Gateway is ready"},
                }

                await websocket.send(json.dumps(welcome))
                print(f"Устройство зарегистрировано: {device_id}")

            # Получение состояния
            elif message_type == "state":
                if device_id is None:
                    print("Ошибка: устройство ещё не зарегистрировано")
                    continue

                payload = data.get("data", {})

                if not isinstance(payload, dict):
                    print("Ошибка: некорректные данные состояния")
                    continue

                parameters = payload.get("parameters", {})

                if not isinstance(parameters, dict):
                    print("Ошибка: parameters должен быть объектом")
                    continue

                device_states.setdefault(device_id, {}).update(parameters)

                save_device_state(device_id, parameters)

                print(
                    f"Состояние {device_id}: "
                    f"{device_states[device_id]}"
                )

            elif message_type == "error":
                print(f"Ошибка устройства {device_id}: {data}")

            else:
                print(f"Неизвестный тип сообщения: {message_type}")

    except ConnectionClosed:
        print(f"Соединение закрыто: {device_id}")

    finally:
        if device_id and devices.get(device_id) is websocket:
            del devices[device_id]
            mark_disconnected(device_id)
            print(f"Устройство отключено: {device_id}")


async def console():
    selected_device = None

    while True:
        raw_command = await asyncio.to_thread(
            input,
            "Команда (on/off/info/status/devices/history/use/exit): ",
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

            device_id = parts[1]

            if device_id not in devices:
                print(f"Устройство {device_id} не подключено")
                continue

            selected_device = device_id
            print(f"Выбрано устройство: {selected_device}")

        elif action == "info":
            if selected_device is None:
                print("Сначала выбери устройство: use <device_id>")
                continue

            state = get_device_from_db(selected_device)

            if state is None:
                print("Информация об устройстве не найдена")
                continue

            print(f"Устройство: {state['device_id']}")
            print(
                "Подключение: "
                + ("активно" if selected_device in devices else "отсутствует")
            )
            print(f"Последнее сообщение: {state['last_seen']}")

            if state["led"] is None:
                print("LED: состояние неизвестно")
            else:
                print("LED: " + ("включён" if state["led"] else "выключен"))

        elif action == "history":
            saved_devices = get_all_devices_from_db()

            if not saved_devices:
                print("В базе пока нет устройств.")
                continue

            print("Устройства, сохранённые в базе:")

            for state in saved_devices:
                if state["led"] is None:
                    led_status = "неизвестно"
                else:
                    led_status = "включён" if state["led"] else "выключен"

                connection_status = (
                    "подключено"
                    if state["device_id"] in devices
                    else "не подключено"
                )

                print(
                    f"- {state['device_id']} | "
                    f"LED: {led_status} | "
                    f"{connection_status} | "
                    f"последнее сообщение: {state['last_seen']}"
                )

        elif action in ("on", "off"):
            if selected_device is None:
                print("Сначала выбери устройство: use <device_id>")
                continue

            websocket = devices.get(selected_device)

            if websocket is None:
                print(f"Устройство {selected_device} отключено")
                selected_device = None
                continue

            command_id = f"cmd-{next(command_ids):03d}"

            message = {
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
                await websocket.send(json.dumps(message))
                print(
                    f"Команда {'включения' if action == 'on' else 'выключения'} "
                    f"LED отправлена → {selected_device}"
                )

            except ConnectionClosed:
                print(f"Соединение с {selected_device} закрыто")

        else:
            print(
                "Доступно: on, off, info, status, devices, "
                "history, use <device_id>, exit"
            )


async def main():
    init_db()

    print(f"База данных: {DB_PATH}")

    async with websockets.serve(
        handle_client,
        "0.0.0.0",
        8765,
    ):
        print("Gateway запущен на порту 8765")
        print("Ожидаем подключения ESP32...")

        await console()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway остановлен")