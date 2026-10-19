import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed


# =========================
# НАСТРОЙКИ
# =========================

HOST = "0.0.0.0"
PORT = 8765

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gateway.db"

# Активные ESP32: device_id -> WebSocket
devices = {}

# Подключённые браузеры
browser_clients = set()

# Последние известные состояния устройств
device_states = {}

command_counter = 0


# =========================
# ВРЕМЯ
# =========================

def current_time():
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


# =========================
# SQLITE
# =========================

def init_db():
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                led INTEGER,
                connected INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_value TEXT,
                timestamp TEXT NOT NULL
            )
        """)

        conn.commit()

    print(f"База данных: {DB_PATH}")


def save_event_in_connection(
    conn, device_id, event_type, event_value, timestamp
):
    conn.execute("""
        INSERT INTO device_events (
            device_id,
            event_type,
            event_value,
            timestamp
        )
        VALUES (?, ?, ?, ?)
    """, (
        device_id,
        event_type,
        str(event_value),
        timestamp
    ))


def save_device_event(device_id, event_type, event_value):
    timestamp = current_time()

    with sqlite3.connect(DB_PATH) as conn:
        save_event_in_connection(
            conn,
            device_id,
            event_type,
            event_value,
            timestamp
        )
        conn.commit()

    return timestamp


def save_device_seen(device_id, connected):
    timestamp = current_time()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO devices (
                device_id,
                last_seen,
                connected
            )
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                connected = excluded.connected
        """, (
            device_id,
            timestamp,
            int(connected)
        ))

        conn.commit()

    return timestamp


def save_device_state(device_id, parameters):
    """
    Сохраняет состояние устройства.

    Событие LED создаётся только если:
    1. Предыдущее значение LED уже известно.
    2. Новое значение отличается от предыдущего.
    """

    timestamp = current_time()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT led
            FROM devices
            WHERE device_id = ?
        """, (device_id,)).fetchone()

        if row is None:
            conn.execute("""
                INSERT INTO devices (
                    device_id,
                    last_seen,
                    connected
                )
                VALUES (?, ?, 1)
            """, (
                device_id,
                timestamp
            ))

            previous_led = None
        else:
            previous_led = row[0]

        new_led = None

        if "led" in parameters:
            raw_led = parameters["led"]

            if isinstance(raw_led, bool):
                new_led = int(raw_led)
            elif isinstance(raw_led, int) and raw_led in (0, 1):
                new_led = raw_led

        if new_led is not None:
            # Сохраняем новое состояние LED.
            conn.execute("""
                UPDATE devices
                SET led = ?,
                    last_seen = ?,
                    connected = 1
                WHERE device_id = ?
            """, (
                new_led,
                timestamp,
                device_id
            ))

            # Записываем событие только при реальном изменении.
            if (
                previous_led is not None
                and int(previous_led) != new_led
            ):
                save_event_in_connection(
                    conn,
                    device_id,
                    "led",
                    str(bool(new_led)).lower(),
                    timestamp
                )

                print(
                    f"[СОБЫТИЕ] {device_id}: "
                    f"LED {'включён' if new_led else 'выключен'}"
                )

        else:
            conn.execute("""
                UPDATE devices
                SET last_seen = ?,
                    connected = 1
                WHERE device_id = ?
            """, (
                timestamp,
                device_id
            ))

        conn.commit()

    return timestamp


def get_recent_events(limit=30):
    """
    Возвращает последние значимые события.

    Старые повторяющиеся подряд значения LED скрываются
    в выдаче, даже если они уже сохранены в SQLite.

    Записи в самой базе при этом не удаляются.
    """

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT
                id,
                device_id,
                event_type,
                event_value,
                timestamp
            FROM device_events
            ORDER BY id ASC
        """).fetchall()

    filtered_events = []

    # Последнее встреченное значение LED для каждого устройства.
    last_led_values = {}

    for row in rows:
        event = dict(row)

        if event["event_type"] == "led":
            device_id = event["device_id"]
            value = str(event["event_value"]).lower()

            previous_value = last_led_values.get(device_id)

            # Пропускаем повтор одного и того же состояния.
            if previous_value == value:
                continue

            last_led_values[device_id] = value

        filtered_events.append({
            "device_id": event["device_id"],
            "event_type": event["event_type"],
            "event_value": event["event_value"],
            "timestamp": event["timestamp"]
        })

    # Сначала оставляем последние значимые события,
    # затем отображаем их от новых к старым.
    return list(reversed(filtered_events[-limit:]))


def get_all_devices():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT device_id, last_seen, led, connected
            FROM devices
            ORDER BY device_id
        """).fetchall()

    result = []

    for row in rows:
        device_id = row["device_id"]

        parameters = {}

        if row["led"] is not None:
            parameters["led"] = bool(row["led"])

        parameters.update(device_states.get(device_id, {}))

        result.append({
            "device": device_id,
            "connected": device_id in devices,
            "last_seen": row["last_seen"],
            "parameters": parameters
        })

    known_ids = {item["device"] for item in result}

    for device_id in devices:
        if device_id not in known_ids:
            result.append({
                "device": device_id,
                "connected": True,
                "last_seen": None,
                "parameters": device_states.get(device_id, {})
            })

    return result


# =========================
# WEBSOCKET
# =========================

async def send_json(websocket, data):
    await websocket.send(
        json.dumps(data, ensure_ascii=False)
    )


async def broadcast(data):
    disconnected_clients = set()

    for websocket in list(browser_clients):
        try:
            await send_json(websocket, data)
        except Exception:
            disconnected_clients.add(websocket)

    browser_clients.difference_update(disconnected_clients)


async def send_device_list(websocket=None):
    message = {
        "type": "device_list",
        "devices": get_all_devices()
    }

    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


async def send_event_history(websocket=None):
    message = {
        "type": "event_history",
        "events": get_recent_events()
    }

    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


# =========================
# КОМАНДЫ ИЗ БРАУЗЕРА
# =========================

async def handle_browser_message(websocket, message):
    global command_counter

    action = message.get("action")

    if action == "get_devices":
        await send_device_list(websocket)
        return

    if action == "get_events":
        await send_event_history(websocket)
        return

    if action == "command":
        device_id = message.get("device")
        parameter = message.get("parameter")
        value = message.get("value")

        if not device_id or device_id not in devices:
            await send_json(websocket, {
                "type": "error",
                "message": f"Устройство {device_id} не подключено."
            })
            return

        if parameter != "led" or not isinstance(value, bool):
            await send_json(websocket, {
                "type": "error",
                "message": "Некорректная команда."
            })
            return

        command_counter += 1
        command_id = f"cmd-{command_counter:06d}"

        command_payload = {
            "type": "command",
            "id": command_id,
            "device": device_id,
            "data": {
                "command": "set",
                "command_id": command_id,
                "parameter": parameter,
                "value": value
            }
        }

        try:
            await send_json(devices[device_id], command_payload)

            await send_json(websocket, {
                "type": "command_sent",
                "device": device_id,
                "command_id": command_id
            })

            print(
                f"[КОМАНДА] {device_id}: "
                f"{parameter} = {value}"
            )

        except Exception as error:
            await send_json(websocket, {
                "type": "error",
                "message": f"Не удалось отправить команду: {error}"
            })

        return

    await send_json(websocket, {
        "type": "error",
        "message": f"Неизвестное действие: {action}"
    })


# =========================
# ОБРАБОТКА ПОДКЛЮЧЕНИЙ
# =========================

async def handle_client(websocket):
    client_type = None
    device_id = None

    remote = getattr(websocket, "remote_address", None)
    print(f"[ПОДКЛЮЧЕНИЕ] Новое соединение: {remote}")

    try:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await send_json(websocket, {
                    "type": "error",
                    "message": "Получен некорректный JSON."
                })
                continue

            message_type = message.get("type")

            # -------------------------
            # Регистрация браузера
            # -------------------------

            if message_type == "browser_hello":
                client_type = "browser"
                browser_clients.add(websocket)

                print("[БРАУЗЕР] Подключён")

                await send_device_list(websocket)
                await send_event_history(websocket)
                continue

            if client_type == "browser":
                if message_type == "browser_message":
                    await handle_browser_message(websocket, message)
                else:
                    await send_json(websocket, {
                        "type": "error",
                        "message": "Неизвестное сообщение браузера."
                    })

                continue

            # -------------------------
            # Регистрация ESP32
            # -------------------------

            if message_type == "hello":
                incoming_id = message.get("device")

                if not incoming_id:
                    await send_json(websocket, {
                        "type": "error",
                        "message": "В сообщении hello отсутствует device."
                    })
                    continue

                device_id = str(incoming_id)
                client_type = "device"

                old_connection = devices.get(device_id)

                devices[device_id] = websocket
                save_device_seen(device_id, True)

                # Логируем подключение в историю.
                save_device_event(
                    device_id,
                    "connection",
                    "Подключено"
                )

                print(f"[ESP32] Подключено устройство: {device_id}")

                # Закрываем предыдущее соединение с тем же ID.
                if (
                    old_connection is not None
                    and old_connection is not websocket
                ):
                    try:
                        await old_connection.close()
                    except Exception:
                        pass

                await send_device_list()
                await send_event_history()
                continue

            # -------------------------
            # Состояние от ESP32
            # -------------------------

            if client_type == "device" and message_type == "state":
                if message.get("device") != device_id:
                    continue

                data = message.get("data", {})
                parameters = data.get("parameters", {})

                if not isinstance(parameters, dict):
                    continue

                device_states[device_id] = {
                    **device_states.get(device_id, {}),
                    **parameters
                }

                last_seen = save_device_state(
                    device_id,
                    parameters
                )

                print(
                    f"[СОСТОЯНИЕ] {device_id}: {parameters}"
                )

                await broadcast({
                    "type": "device_state",
                    "device": device_id,
                    "parameters": device_states[device_id],
                    "last_seen": last_seen
                })

                # История обновляется в браузере, но новые
                # LED-события добавляются только при изменении.
                await send_event_history()
                continue

            print(f"[ПРЕДУПРЕЖДЕНИЕ] Неизвестное сообщение: {message}")

    except ConnectionClosed:
        pass

    except Exception as error:
        print(f"[ОШИБКА] Обработка соединения: {error}")

    finally:
        browser_clients.discard(websocket)

        # Обрабатываем отключение только текущего соединения.
        # Это не даст старому соединению ошибочно отметить
        # новое соединение того же устройства отключённым.
        if (
            device_id
            and devices.get(device_id) is websocket
        ):
            del devices[device_id]

            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    UPDATE devices
                    SET connected = 0
                    WHERE device_id = ?
                """, (device_id,))
                conn.commit()

            save_device_event(
                device_id,
                "connection",
                "Отключено"
            )

            print(f"[ESP32] Отключено устройство: {device_id}")

            await send_device_list()
            await send_event_history()

        elif client_type == "browser":
            print("[БРАУЗЕР] Отключён")


# =========================
# КОНСОЛЬНЫЕ КОМАНДЫ
# =========================

async def console_loop():
    print()
    print("Команды консоли:")
    print("  devices          — список устройств")
    print("  status           — статус Gateway")
    print("  on               — включить LED выбранного устройства")
    print("  off              — выключить LED выбранного устройства")
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

        parts = command_line.strip().split()

        if not parts:
            continue

        command = parts[0].lower()

        if command == "exit":
            print("Останавливаем Gateway...")
            return

        elif command == "status":
            print(f"WebSocket: ws://0.0.0.0:{PORT}")
            print(f"Подключено ESP32: {len(devices)}")
            print(f"Подключено браузеров: {len(browser_clients)}")

        elif command == "devices":
            all_devices = get_all_devices()

            for item in all_devices:
                status = (
                    "онлайн"
                    if item["connected"]
                    else "офлайн"
                )

                print(
                    f'{item["device"]}: {status}, '
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

        elif command in ("on", "off"):
            if not selected_device:
                print("Сначала выбери устройство: use esp32-01")
                continue

            if selected_device not in devices:
                print(
                    f"Устройство {selected_device} "
                    "не подключено."
                )
                continue

            value = command == "on"
            command_counter += 1
            command_id = f"console-{command_counter:06d}"

            payload = {
                "type": "command",
                "id": command_id,
                "device": selected_device,
                "data": {
                    "command": "set",
                    "command_id": command_id,
                    "parameter": "led",
                    "value": value
                }
            }

            try:
                await send_json(
                    devices[selected_device],
                    payload
                )

                print(f"Команда отправлена: LED = {value}")

            except Exception as error:
                print(f"Не удалось отправить команду: {error}")

        elif command == "events":
            for event in get_recent_events(10):
                print(
                    f'{event["timestamp"]} | '
                    f'{event["device_id"]} | '
                    f'{event["event_type"]}: '
                    f'{event["event_value"]}'
                )

        elif command == "history":
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute("""
                    SELECT device_id, last_seen, led, connected
                    FROM devices
                    ORDER BY device_id
                """).fetchall()

            for row in rows:
                print(
                    f"ID: {row[0]}, "
                    f"последнее сообщение: {row[1]}, "
                    f"LED: {row[2]}, "
                    f"подключено: {bool(row[3])}"
                )

            if not rows:
                print("История устройств пока пуста.")

        else:
            print(
                "Неизвестная команда. "
                "Доступно: devices, status, on, off, "
                "use, events, history, exit."
            )


# =========================
# ЗАПУСК GATEWAY
# =========================

async def main():
    init_db()

    print("=" * 48)
    print("        IoT Gateway запускается")
    print("=" * 48)

    async with websockets.serve(handle_client, HOST, PORT):
        print(f"WebSocket-сервер запущен на порту {PORT}")
        print("Ожидание подключений ESP32 и браузера...")

        await console_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway остановлен.")