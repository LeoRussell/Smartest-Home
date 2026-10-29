# Прошивки ESP32

Здесь находятся два самостоятельных проекта PlatformIO и общие для них
сетевые настройки и библиотека связи с Gateway.

| Проект | Когда использовать | Где задавать устройство |
| --- | --- | --- |
| `universal/` | GPIO, ADC и PWM настраиваются в интерфейсе Gateway; сценарии собираются в Blockly | `universal/include/user_config.h` |
| `examples/led/` | Пользователь пишет свою Schema, обработчики команд и сложные режимы на C++ | `examples/led/include/device_config.h` и `examples/led/src/` |

В корне `esp32` нет отдельной прошивки. Для сборки выбирайте каталог одного
из проектов через `pio run -d ...`.

Для работы в VS Code откройте [`Smartest-Home.code-workspace`](../Smartest-Home.code-workspace).
Оба проекта будут доступны в PlatformIO со своими профилями сборки и настройками редактора.

## Расположение файлов

```text
esp32/
├── shared/
│   ├── include/
│   │   ├── secrets.example.h       Пример параметров Wi-Fi и адреса Gateway
│   │   └── secrets.h               Локальные параметры подключения
│   └── lib/GatewayClient/          Общий код Wi-Fi, WebSocket и JSON-транспорта
├── universal/
│   ├── platformio.ini              Профиль платы и зависимости
│   ├── include/user_config.h       ID и имя устройства
│   └── src/
│       ├── main.cpp                Запуск и сообщения подключения
│       └── UniversalDevice.*       Драйверы, конфигурация, Schema и телеметрия
└── examples/led/
    ├── platformio.ini              Профили GPIO- и NeoPixel-индикаторов
    ├── include/device_config.h     ID, имя и настройки устройства
    └── src/
        ├── main.cpp                Команды, телеметрия и действие «Найти устройство»
        ├── DeviceSchema.*          Параметры и возможности устройства
        └── DeviceModes.*           Реестр пользовательских режимов
```

`shared/lib/GatewayClient` содержит транспорт, который используют оба проекта.
Поведение конкретного устройства, его Schema и режимы находятся в выбранном
проекте. Для собственного устройства можно скопировать `examples/led` в
соседний каталог и изменить его ID, Schema и обработчики.

## Подготовка и загрузка

Скопируйте `shared/include/secrets.example.h` в `shared/include/secrets.h`,
укажите Wi-Fi и IP компьютера с Gateway. Файл `secrets.h` исключён из Git.
Назначьте каждой плате свой уникальный ID в конфигурационном файле выбранного
проекта.

Все команды ниже выполняются из корня репозитория.

Универсальная прошивка:

```powershell
pio run -d esp32/universal
pio run -d esp32/universal -t upload
pio device monitor -d esp32/universal
```

Пользовательская LED-прошивка:

```powershell
pio run -d esp32/examples/led
pio run -d esp32/examples/led -t upload
pio device monitor -d esp32/examples/led
```

В LED-проекте профиль по умолчанию управляет обычным светодиодом на GPIO 48.
Остальные профили в его `platformio.ini` выбираются через `-e`:

- `esp32-s3-devkitc-1` — обычный GPIO-светодиод на GPIO 48;
- `esp32-s3-devkitc-1-v1_0-neopixel` — адресный RGB-индикатор на GPIO 48;
- `esp32-s3-devkitc-1-v1_1` — адресный RGB-индикатор на GPIO 38;
- `adafruit_feather_esp32s3` — GPIO-светодиод Feather на GPIO 13.

## Сгенерированные файлы

При сборке PlatformIO создаёт `.pio/` внутри выбранного проекта. В
`.pio/build/` находятся объектные файлы и готовые образы прошивки, а в
`.pio/libdeps/` — скачанные зависимости. Эти каталоги исключены из Git и
восстанавливаются при сборке; исходники проекта редактируются в `src/` и
`include/`.

Необязательный файл `universal/include/device_configuration.generated.h`
создаётся через окно конфигурации Gateway. Он позволяет включить начальную
аппаратную конфигурацию в прошивку. При обычном использовании Gateway
передаёт конфигурацию по WebSocket, и повторная прошивка платы не требуется.

Подробные руководства: [универсальная прошивка](../docs/UNIVERSAL_FIRMWARE.md),
[пользовательская прошивка и протокол](../docs/ADVANCED_FIRMWARE.md),
[Schema устройства](../docs/DEVICE_SCHEMA.md).
