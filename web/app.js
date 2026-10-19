
const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const refreshButton = document.getElementById("refresh-button");
const activityCard = document.querySelector(".activity-card");

let socket = null;
let reconnectTimer = null;
let devices = [];
let events = [];

// =========================
// СООБЩЕНИЯ СТАТУСА
// =========================

function setActivity(message) {
    if (activityMessage) {
        activityMessage.textContent = message;
    }
}

// =========================
// ПОДКЛЮЧЕНИЕ К GATEWAY
// =========================

function connectGateway() {
    if (
        socket &&
        (
            socket.readyState === WebSocket.OPEN ||
            socket.readyState === WebSocket.CONNECTING
        )
    ) {
        return;
    }

    clearTimeout(reconnectTimer);

    const host = window.location.hostname || "localhost";

    socket = new WebSocket(`ws://${host}:8765`);

    socket.addEventListener("open", () => {
        setActivity("Подключено к Gateway.");

        socket.send(JSON.stringify({
            type: "browser_hello"
        }));

        requestDevices();
        requestEvents();
    });

    socket.addEventListener("message", (event) => {
        let message;

        try {
            message = JSON.parse(event.data);
        } catch (error) {
            console.error("Некорректный JSON от Gateway:", error);
            setActivity("Gateway прислал некорректное сообщение.");
            return;
        }

        handleGatewayMessage(message);
    });

    socket.addEventListener("close", () => {
        setActivity("Соединение потеряно. Повторное подключение...");

        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectGateway, 2000);
    });

    socket.addEventListener("error", (error) => {
        console.error("Ошибка WebSocket:", error);
        setActivity("Ошибка соединения с Gateway.");
    });
}

// =========================
// ОБРАБОТКА СООБЩЕНИЙ
// =========================

function handleGatewayMessage(message) {
    switch (message.type) {
        case "device_list": {
            devices = Array.isArray(message.devices)
                ? message.devices
                : [];

            renderDevices();

            if (devices.length === 0) {
                setActivity("Gateway доступен, устройств пока нет.");
            } else {
                setActivity(`Найдено устройств: ${devices.length}.`);
            }

            break;
        }

        case "device_state": {
            let device = devices.find(
                item => item.device === message.device
            );

            if (device) {
                device.parameters = {
                    ...device.parameters,
                    ...(message.parameters || {})
                };

                device.last_seen = message.last_seen;
                device.connected = true;
            } else {
                devices.push({
                    device: message.device,
                    connected: true,
                    parameters: message.parameters || {},
                    last_seen: message.last_seen
                });
            }

            renderDevices();
            setActivity(`Получено состояние от ${message.device}.`);

            break;
        }

        case "event_history": {
            events = Array.isArray(message.events)
                ? message.events
                : [];

            renderEvents();
            break;
        }

        case "command_sent": {
            setActivity(
                `Команда отправлена ${message.device}. Ожидаем подтверждение состояния...`
            );
            break;
        }

        case "error": {
            setActivity(`Ошибка: ${message.message || "неизвестная ошибка"}`);
            console.error("Gateway:", message.message);
            break;
        }

        default: {
            console.log("Сообщение от Gateway:", message);
        }
    }
}

// =========================
// ОТОБРАЖЕНИЕ УСТРОЙСТВ
// =========================

function renderDevices() {
    if (!deviceCard) {
        console.error("Не найден элемент .device-card");
        return;
    }

    deviceCard.replaceChildren();

    if (devices.length === 0) {
        const empty = document.createElement("p");
        empty.textContent = "Устройства пока не найдены.";
        deviceCard.appendChild(empty);
        return;
    }

    for (const device of devices) {
        const heading = document.createElement("div");
        heading.className = "device-heading";

        const icon = document.createElement("div");
        icon.className = "device-icon";
        icon.textContent = "ESP";

        const titleBlock = document.createElement("div");

        const title = document.createElement("h3");
        title.textContent = device.device;

        const subtitle = document.createElement("p");
        subtitle.className = "device-id";
        subtitle.textContent = "IoT-устройство";

        titleBlock.append(title, subtitle);

        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = device.connected
            ? "Подключено"
            : "Отключено";

        heading.append(icon, titleBlock, badge);
        deviceCard.appendChild(heading);

        const divider = document.createElement("div");
        divider.className = "divider";
        deviceCard.appendChild(divider);

        const parameter = document.createElement("div");
        parameter.className = "parameter";

        const label = document.createElement("span");
        label.textContent = "Светодиод (LED)";

        const ledStatus = document.createElement("strong");
        const ledValue = device.parameters?.led;

        if (typeof ledValue === "boolean") {
            ledStatus.textContent = ledValue
                ? "Включён"
                : "Выключен";
        } else {
            ledStatus.textContent = "Неизвестно";
        }

        parameter.append(label, ledStatus);
        deviceCard.appendChild(parameter);

        const controls = document.createElement("div");
        controls.style.display = "flex";
        controls.style.flexWrap = "wrap";
        controls.style.gap = "10px";
        controls.style.marginTop = "20px";

        const onButton = document.createElement("button");
        onButton.textContent = "Включить LED";
        onButton.disabled = !device.connected;

        onButton.addEventListener("click", () => {
            sendLedCommand(device.device, true);
        });

        const offButton = document.createElement("button");
        offButton.textContent = "Выключить LED";
        offButton.disabled = !device.connected;

        offButton.addEventListener("click", () => {
            sendLedCommand(device.device, false);
        });

        controls.append(onButton, offButton);
        deviceCard.appendChild(controls);

        const hint = document.createElement("p");
        hint.className = "hint";

        hint.textContent = device.connected
            ? `Последнее сообщение: ${device.last_seen || "неизвестно"}`
            : "Устройство сейчас не подключено.";

        deviceCard.appendChild(hint);
    }
}

// =========================
// ИСТОРИЯ СОБЫТИЙ
// =========================

function renderEvents() {
    if (!activityCard) {
        console.error("Не найден элемент .activity-card");
        return;
    }

    // Создаём отдельный контейнер для истории.
    // Не очищаем весь activityCard, чтобы не удалить
    // элемент #activity-message.
    let historyContainer = activityCard.querySelector(".event-history");

    if (!historyContainer) {
        historyContainer = document.createElement("div");
        historyContainer.className = "event-history";
        activityCard.appendChild(historyContainer);
    }

    historyContainer.replaceChildren();

    const heading = document.createElement("h2");
    heading.textContent = "История событий";
    historyContainer.appendChild(heading);

    if (events.length === 0) {
        const empty = document.createElement("p");
        empty.textContent = "Событий пока нет.";
        historyContainer.appendChild(empty);
        return;
    }

    const list = document.createElement("div");
    list.style.display = "grid";
    list.style.gap = "12px";

    for (const event of events) {
        const row = document.createElement("div");
        row.className = "parameter";
        row.style.flexDirection = "column";
        row.style.alignItems = "flex-start";
        row.style.gap = "6px";

        const description = document.createElement("span");

        const rawValue = String(event.event_value ?? "");
        let readableValue = rawValue;

        if (event.event_type === "led") {
            const value = rawValue.toLowerCase();

            if (value === "true" || value === "1") {
                readableValue = "включён";
            } else if (value === "false" || value === "0") {
                readableValue = "выключен";
            }
        }

        description.textContent =
            `${event.device_id} — ${event.event_type}: ${readableValue}`;

        const timestamp = document.createElement("small");
        timestamp.style.color = "#94a3b8";
        timestamp.textContent = event.timestamp || "";

        row.append(description, timestamp);
        list.appendChild(row);
    }

    historyContainer.appendChild(list);
}

// =========================
// ОТПРАВКА КОМАНД
// =========================

function sendLedCommand(deviceId, value) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        setActivity("Нет соединения с Gateway.");
        return;
    }

    socket.send(JSON.stringify({
        type: "browser_message",
        action: "command",
        device: deviceId,
        parameter: "led",
        value: value
    }));
}

// =========================
// ЗАПРОСЫ К GATEWAY
// =========================

function requestDevices() {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_devices"
        }));
    }
}

function requestEvents() {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_events"
        }));
    }
}

// =========================
// КНОПКА ОБНОВЛЕНИЯ
// =========================

if (refreshButton) {
    refreshButton.addEventListener("click", () => {
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            connectGateway();
            return;
        }

        requestDevices();
        requestEvents();

        setActivity("Обновляем устройства и историю...");
    });
} else {
    console.warn("Кнопка #refresh-button не найдена.");
}

// =========================
// ЗАПУСК
// =========================

connectGateway();
