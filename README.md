# Smartest-Home

Локальный IoT Gateway для ESP32 и других устройств с поддерживаемым WebSocket-протоколом. Gateway принимает Schema и телеметрию устройств, сохраняет их в SQLite и предоставляет веб-интерфейс для просмотра параметров и вызова объявленных действий.

## Структура проекта

- `gateway/` — WebSocket-сервер на Python и SQLite база `gateway.db`.
- `web/` — статический интерфейс на HTML, CSS и JavaScript.
- `esp32/` — прошивка Arduino/PlatformIO для тестовой Adafruit ESP32-S3 Feather.

Gateway слушает WebSocket на порту `8765`. Веб-файлы раздаются отдельным HTTP-сервером на порту `8080`.

Адрес, порт и путь к базе Gateway можно переопределить переменными `GATEWAY_HOST`, `GATEWAY_PORT` и `GATEWAY_DB_PATH`. Без них используются `0.0.0.0:8765` и `gateway/gateway.db`.

## Запуск Gateway

Из корня репозитория в PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt
.\.venv\Scripts\python.exe gateway\main.py
```

В отдельном окне PowerShell запусти веб-страницу:

```powershell
py -m http.server 8080 --directory web
```

Открой `http://localhost:8080`. Для доступа с другого устройства в локальной сети используй IP компьютера с Gateway вместо `localhost`.

## Настройка ESP32

Скопируй `esp32/include/secrets.example.h` в `esp32/include/secrets.h` и укажи Wi-Fi и IP компьютера, где запущен Gateway. `secrets.h` исключён из Git. Прошивка собирается PlatformIO из каталога `esp32`.

Текущая конфигурация в `esp32/include/device_config.h` управляет выходом LED на GPIO 48 и включает действие `identify`, которое мигает настроенным индикатором. Для платы без такого индикатора установи `DEVICE_HAS_IDENTIFY_INDICATOR` в `0`: действие исчезнет из Schema и не появится в браузере. GPIO остаётся внутренней настройкой прошивки, а Schema сообщает Gateway только о поддерживаемой возможности.

Для подключения нескольких плат задай каждой уникальный `DEVICE_ID` в этом файле перед прошивкой. `DEVICE_NAME` и `DEVICE_MODEL` можно настроить отдельно; имя и состояние каждой платы Gateway хранит по её ID.

## Schema и база данных

При каждом подключении устройство отправляет ID и Schema в `hello`. Для первого подключения Gateway сохраняет эту Schema. При следующих подключениях он сравнивает её с принятой версией по содержимому, независимо от `schema_version`: если Schema не менялась, соединение продолжается; если изменилась, веб-интерфейс показывает список добавленных, удалённых и изменённых параметров и действий. До решения остаётся активна прежняя Schema, а команды устройства временно блокируются. Принятие сохраняет новую Schema в `devices.schema_json` и версию в `devices.schema_version`. Если оставить текущую Schema, она остаётся активной; при следующем подключении Gateway снова предложит изменение, если оно всё ещё есть.

Последнее состояние параметров хранится отдельно в `devices.state_json`. Действия являются частью Schema, поэтому добавление `identify` не требует отдельной SQLite-колонки.

Устройство без Schema отображается с автоматически выведенными параметрами только для чтения. Управляемые параметры и действия должны быть явно объявлены устройством. Действие может объявить типизированные `arguments`; Gateway проверяет обязательность, тип и ограничения, а браузер строит для них форму.

Например, действие калибровки может принимать числовое смещение и необязательный режим:

```json
{
  "actions": {
    "calibrate": {
      "label": "Калибровка",
      "arguments": {
        "offset": {"label": "Смещение", "type": "number", "min": -5, "max": 5},
        "mode": {
          "label": "Режим",
          "type": "enum",
          "required": false,
          "options": [
            {"label": "Быстро", "value": "fast"},
            {"label": "Точно", "value": "fine"}
          ]
        }
      }
    }
  }
}
```

## Консоль Gateway

- `devices`, `status` — список устройств и состояние Gateway.
- `use <device_id>` — выбрать устройство.
- `schema [device_id]` — посмотреть сохранённую Schema.
- `set <parameter> <json_value>` — установить объявленный параметр.
- `call <action_id> [json_arguments]` — вызвать объявленное действие; аргументы проверяются по Schema.
- `events`, `history` — посмотреть журнал и сохранённые устройства.
- `exit` — корректно остановить Gateway.

## Linux и автозапуск

В репозитории есть unit-файл `deploy/smartest-home-gateway.service` для `systemd`. Он рассчитан на установку проекта в `/opt/smartest-home`, отдельного системного пользователя `smartest-home` и базы в `/var/lib/smartest-home/gateway.db`.

Сначала склонируй или скопируй репозиторий в `/opt/smartest-home`. Затем создай системного пользователя и каталог базы, передай файлы проекта этому пользователю и установи зависимости:

```sh
sudo useradd --system --user-group --home /opt/smartest-home --shell /usr/sbin/nologin smartest-home
sudo install -d -o smartest-home -g smartest-home /var/lib/smartest-home
sudo chown -R smartest-home:smartest-home /opt/smartest-home
sudo -u smartest-home python3 -m venv /opt/smartest-home/.venv
sudo -u smartest-home /opt/smartest-home/.venv/bin/pip install -r /opt/smartest-home/gateway/requirements.txt
sudo cp /opt/smartest-home/deploy/smartest-home-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartest-home-gateway
```

Проверяй состояние через `systemctl status smartest-home-gateway`, а журналы — через `journalctl -u smartest-home-gateway -f`. В служебном режиме Gateway не ожидает ввода из консоли и остаётся запущенным до остановки systemd. Для веб-интерфейса по-прежнему нужен HTTP-сервер на каталоге `web`; открой только необходимые порты в доверенной локальной сети.
