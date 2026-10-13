
import asyncio
import json
from itertools import count

import websockets
from websockets.exceptions import ConnectionClosed


# Подключённые устройства: device_id -> WebSocket
devices = {}
command_ids = count(1)


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

            message_type = data.get("type")

            if message_type == "hello":
                device_id = data.get("device")

                if not device_id:
                    print("Ошибка: устройство не указало ID")
                    continue

                devices[device_id] = websocket

                welcome = {
                    "type": "welcome",
                    "id": "welcome-001",
                    "device": device_id,
                    "data": {"message": "Gateway is ready"},
                }

                await websocket.send(json.dumps(welcome))
                print(f"Устройство зарегистрировано: {device_id}")

            elif message_type == "state":
                parameters = data.get("data", {}).get(
                    "parameters", {}
                )
                print(f"Состояние {device_id}: {parameters}")

            elif message_type == "error":
                print(f"Ошибка устройства: {data}")

    except ConnectionClosed:
        print(f"Соединение закрыто: {device_id}")

    finally:
        # Не удаляем новое соединение, если устройство
        # успело переподключиться со старым ID.
        if device_id and devices.get(device_id) is websocket:
            del devices[device_id]
            print(f"Устройство отключено: {device_id}")


async def console():
    while True:
        command = await asyncio.to_thread(
            input, "Команда (on/off/exit): "
        )
        command = command.strip().lower()

        if command == "exit":
            print("Завершение работы Gateway...")
            return

        if command not in ("on", "off"):
            print("Введите on, off или exit")
            continue

        device_id = "esp32-01"
        websocket = devices.get(device_id)

        if websocket is None:
            print("ESP32 не подключена")
            continue

        command_id = f"cmd-{next(command_ids):03d}"

        message = {
            "type": "command",
            "id": command_id,
            "device": device_id,
            "data": {
                "command": "set",
                "command_id": command_id,
                "parameter": "led",
                "value": command == "on",
            },
        }

        try:
            await websocket.send(json.dumps(message))
            print(
                "Отправлена команда:",
                "включить LED" if command == "on"
                else "выключить LED",
            )
        except ConnectionClosed:
            print("Не удалось отправить команду: соединение закрыто")


async def main():
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
