const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const refreshButton = document.getElementById("refresh-button");

let socket = null;
let reconnectTimer = null;
let devices = [];

function setActivity(message) {
    activityMessage.textContent = message;
}

function connectGateway() {
    if (socket && (
        socket.readyState === WebSocket.OPEN ||
        socket.readyState === WebSocket.CONNECTING
    )) {
        return;
    }

    const address = `ws://${window.location.hostname}:8765`;

    socket = new WebSocket(address);

    socket.addEventListener("open", () => {
        setActivity("Подключено к Gateway. Загружаем устройства...");

        socket.send(JSON.stringify({
            type: "browser_hello"
        }));
    });

    socket.addEventListener("message", (event) => {
        let message;

        try {
            message = JSON.parse(event.data);
        } catch {
            setActivity("Gateway прислал некорректное сообщение.");
            return;
        }

        handleGatewayMessage(message);
    });

    socket.addEventListener("close", () => {
        setActivity("Соединение с Gateway потеряно. Повторное подключение...");

        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectGateway, 2000);
    });

    socket.addEventListener("error", () => {
        setActivity("Не удалось связаться с Gateway.");
    });
}

function handleGatewayMessage(message) {
    if (message.type === "device_list") {
        devices = message.devices || [];
        renderDevices();

        setActivity(
            devices.length
                ? `Получен список устройств: ${devices.length}.`
                : "Gateway доступен, но устройства пока не зарегистрированы."
        );
    }

    else if (message.type === "device_state") {
        const device = devices.find(
            item => item.device === message.device
        );

        if (device) {
            device.parameters = message.parameters || {};
            device.last_seen = message.last_seen;
        } else {
            devices.push({
                device: message.device,
                connected: true,
                parameters: message.parameters || {},
                last_seen: message.last_seen
            });
        }

        renderDevices();
        setActivity(`Получено новое состояние от ${message.device}.`);
    }

    else if (message.type === "command_sent") {
        setActivity(
            `Команда отправлена устройству ${message.device}. Ожидаем состояние от ESP32...`
        );
    }

    else if (message.type === "error") {
        setActivity(`Ошибка: ${message.message}`);
    }
}

function renderDevices() {
    deviceCard.replaceChildren();

    if (devices.length === 0) {
        const message = document.createElement("p");
        message.textContent = "Устройства пока не найдены.";
        deviceCard.appendChild(message);
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
        badge.textContent = device.connected ? "Подключено" : "Отключено";

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
            ledStatus.textContent = ledValue ? "Включён" : "Выключен";
        } else {
            ledStatus.textContent = "Неизвестно";
        }

        parameter.append(label, ledStatus);
        deviceCard.appendChild(parameter);

        const controls = document.createElement("div");
        controls.style.display = "flex";
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
            ? `Последнее сообщение: ${device.last_seen || "пока неизвестно"}`
            : "Устройство сейчас не подключено.";

        deviceCard.appendChild(hint);
    }
}

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

refreshButton.addEventListener("click", () => {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_devices"
        }));
        setActivity("Запрашиваем список устройств...");
    } else {
        connectGateway();
    }
});

connectGateway();