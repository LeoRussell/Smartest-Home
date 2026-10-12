
import asyncio
import json
import websockets


async def handle_client(websocket):
    print(f"Подключилось устройство: {websocket.remote_address}")

    try:
        async for message in websocket:
            print(f"Получено: {message}")

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("Ошибка: получено некорректное JSON-сообщение")
                continue

            if data.get("type") == "hello":
                response = {
                    "type": "welcome",
                    "id": "welcome-001",
                    "data": {
                        "message": "Gateway is ready"
                    }
                }

                await websocket.send(
                    json.dumps(response, ensure_ascii=False)
                )
                print("Отправлено приветствие ESP32")

    except websockets.exceptions.ConnectionClosed:
        print("Устройство отключилось")


async def main():
    async with websockets.serve(
        handle_client,
        "0.0.0.0",
        8765
    ):
        print("Gateway запущен на порту 8765")
        print("Ожидаем подключения ESP32...")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
