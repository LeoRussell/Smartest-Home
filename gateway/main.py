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

# Команды, ожидающие результата: command_id -> контекст команды.
pending_commands = {}
COMMAND_TIMEOUT_SECONDS = 5
EVENTS_PAGE_SIZE = 30

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


def get_event_page(limit=EVENTS_PAGE_SIZE, device_id=None, before_id=None):
    conditions = []
    parameters = []

    if device_id is not None:
        conditions.append("device_id = ?")
        parameters.append(device_id)

    if before_id is not None:
        conditions.append("id < ?")
        parameters.append(before_id)

    where_clause = (
        f"WHERE {' AND '.join(conditions)}"
        if conditions
        else ""
    )

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT id, device_id, event_type, event_value, timestamp
            FROM device_events
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """, (*parameters, limit + 1)).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    events = [dict(row) for row in page]
    next_before_id = page[-1]["id"] if has_more and page else None

    return events, has_more, next_before_id


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


async def send_event_history(
    websocket=None,
    device_id=None,
    before_id=None,
    request_id=None
):
    events, has_more, next_before_id = get_event_page(
        device_id=device_id,
        before_id=before_id
    )

    message = {
        "type": "event_history",
        "events": events,
        "has_more": has_more,
        "next_before_id": next_before_id,
        "device_filter": device_id or "all"
    }

    if request_id is not None:
        message["request_id"] = request_id

    if websocket is not None:
        await send_json(websocket, message)
    else:
        await broadcast(message)


def remove_pending_command(command_id):
    pending = pending_commands.pop(command_id, None)

    if pending is not None:
        timeout_task = pending.get("timeout_task")

        if (
            timeout_task is not None
            and timeout_task is not asyncio.current_task()
        ):
            timeout_task.cancel()

    return pending


async def send_pending_command_result(pending, status, value, error):
    if pending["browser"] is None:
        print(
            f"[КОМАНДА] {pending['command_id']} ({pending['device']}): "
            f"{status}; {error or value}"
        )
        return

    message = {
        "type": "command_result",
        "command_id": pending["command_id"],
        "device": pending["device"],
        "success": status == "succeeded",
        "status": status,
        "parameter": pending["parameter"],
        "value": value
    }

    if error:
        message["error"] = error

    try:
        await send_json(pending["browser"], message)
    except Exception:
        pass


async def expire_pending_command(command_id):
    await asyncio.sleep(COMMAND_TIMEOUT_SECONDS)
    pending = remove_pending_command(command_id)

    if pending is not None:
        await send_pending_command_result(
            pending,
            "timeout",
            pending["value"],
            "Устройство не подтвердило выполнение за отведённое время."
        )


async def finish_pending_commands_for_device(device_id, status, error):
    command_ids = [
        command_id
        for command_id, pending in pending_commands.items()
        if pending["device"] == device_id
    ]

    for command_id in command_ids:
        pending = remove_pending_command(command_id)

        if pending is not None:
            await send_pending_command_result(
                pending,
                status,
                pending["value"],
                error
            )


def remove_pending_commands_for_browser(websocket):
    command_ids = [
        command_id
        for command_id, pending in pending_commands.items()
        if pending["browser"] is websocket
    ]

    for command_id in command_ids:
        remove_pending_command(command_id)


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
        device_id = message.get("device", "all")
        before_id = message.get("before_id")
        request_id = message.get("request_id")

        if device_id == "all":
            device_id = None
        elif not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
            await send_json(websocket, {
                "type": "error",
                "request_id": request_id,
                "message": "Некорректный фильтр устройства истории."
            })
            return

        if before_id is not None and (type(before_id) is not int or before_id < 1):
            await send_json(websocket, {
                "type": "error",
                "request_id": request_id,
                "message": "Некорректный курсор истории."
            })
            return

        if request_id is not None and (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 80
        ):
            await send_json(websocket, {
                "type": "error",
                "message": "Некорректный ID запроса истории."
            })
            return

        await send_event_history(
            websocket,
            device_id=device_id,
            before_id=before_id,
            request_id=request_id
        )
        return

    if action == "command":
        device_id = message.get("device")
        requested_command_id = message.get("command_id")
        parameter = message.get("parameter")
        value = message.get("value")

        if not device_id or device_id not in devices:
            await send_json(websocket, {
                "type": "error",
                "command_id": requested_command_id,
                "message": f"Устройство {device_id} не подключено."
            })
            return

        if parameter != "led" or not isinstance(value, bool):
            await send_json(websocket, {
                "type": "error",
                "command_id": requested_command_id,
                "message": "Некорректная команда."
            })
            return

        if requested_command_id is None:
            command_counter += 1
            command_id = f"cmd-{command_counter:06d}"
        elif (
            isinstance(requested_command_id, str)
            and 1 <= len(requested_command_id) <= 80
        ):
            command_id = requested_command_id
        else:
            await send_json(websocket, {
                "type": "error",
                "command_id": requested_command_id,
                "message": "Некорректный command_id."
            })
            return

        if command_id in pending_commands:
            await send_json(websocket, {
                "type": "error",
                "command_id": command_id,
                "message": "Команда с таким command_id уже ожидает результат."
            })
            return

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

        pending = {
            "command_id": command_id,
            "device": device_id,
            "browser": websocket,
            "parameter": parameter,
            "value": value,
            "timeout_task": None
        }
        pending_commands[command_id] = pending

        try:
            await send_json(devices[device_id], command_payload)
        except Exception as error:
            remove_pending_command(command_id)
            await send_json(websocket, {
                "type": "error",
                "command_id": command_id,
                "message": f"Не удалось отправить команду: {error}"
            })
            return

        pending["timeout_task"] = asyncio.create_task(
            expire_pending_command(command_id)
        )

        try:
            await send_json(websocket, {
                "type": "command_sent",
                "device": device_id,
                "command_id": command_id,
                "status": "sent"
            })
        except Exception:
            return

        print(
            f"[КОМАНДА] {device_id}: "
            f"{parameter} = {value} ({command_id})"
        )

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
                    await finish_pending_commands_for_device(
                        device_id,
                        "unavailable",
                        "Соединение устройства было заменено до получения результата."
                    )

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

            if client_type == "device" and message_type == "command_result":
                if message.get("device") != device_id:
                    continue

                command_id = message.get("command_id")
                success = message.get("success")
                data = message.get("data", {})

                if (
                    not isinstance(command_id, str)
                    or not 1 <= len(command_id) <= 80
                    or type(success) is not bool
                    or not isinstance(data, dict)
                ):
                    continue

                parameter = data.get("parameter")
                value = data.get("value")
                error_message = data.get("error")

                if (
                    not isinstance(parameter, str)
                    or not parameter
                    or not isinstance(value, bool)
                    or (
                        error_message is not None
                        and not isinstance(error_message, str)
                    )
                ):
                    continue

                pending = pending_commands.get(command_id)

                if pending is None or pending["device"] != device_id:
                    print(
                        f"[ПРЕДУПРЕЖДЕНИЕ] Игнорируется неизвестный "
                        f"результат команды {command_id} от {device_id}."
                    )
                    continue

                if (
                    success
                    and parameter == pending["parameter"]
                    and value == pending["value"]
                ):
                    status = "succeeded"
                    result_error = None
                else:
                    status = "failed"
                    result_error = error_message or (
                        "Устройство вернуло результат, не совпадающий "
                        "с отправленной командой."
                    )

                pending = remove_pending_command(command_id)
                await send_pending_command_result(
                    pending,
                    status,
                    value,
                    result_error
                )
                continue

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

                state_message = {
                    "type": "device_state",
                    "device": device_id,
                    "parameters": device_states[device_id],
                    "last_seen": last_seen
                }

                await broadcast(state_message)

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

            await finish_pending_commands_for_device(
                device_id,
                "unavailable",
                "Устройство отключилось до получения результата команды."
            )

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
            remove_pending_commands_for_browser(websocket)
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

            pending = {
                "command_id": command_id,
                "device": selected_device,
                "browser": None,
                "parameter": "led",
                "value": value,
                "timeout_task": None
            }
            pending_commands[command_id] = pending

            try:
                await send_json(
                    devices[selected_device],
                    payload
                )

                pending["timeout_task"] = asyncio.create_task(
                    expire_pending_command(command_id)
                )

                print(f"Команда отправлена: LED = {value}")

            except Exception as error:
                remove_pending_command(command_id)
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
