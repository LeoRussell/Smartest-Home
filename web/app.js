const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const refreshButton = document.getElementById("refresh-button");
const activityCard = document.querySelector(".activity-card");

// =========================
// СОСТОЯНИЕ ПРИЛОЖЕНИЯ
// =========================

let socket = null;
let reconnectTimer = null;

let devices = [];
let events = [];
let eventFilter = "all";
let eventDeviceFilter = "all";
let eventCursor = null;
let eventsHasMore = false;
let eventsLoading = false;
let eventRequestSequence = 0;
let activeEventRequest = null;

const pendingCommands = new Map();
const COMMAND_TIMEOUT_MS = 7000;
let commandSequence = 0;

function createCommandId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return `web-${window.crypto.randomUUID()}`;
    }

    commandSequence += 1;

    return `web-${Date.now().toString(36)}-${commandSequence.toString(36)}-` +
        Math.random().toString(36).slice(2, 12);
}

function mergeEventPages(existingEvents, newEvents) {
    const eventsById = new Map();

    for (const event of [...existingEvents, ...newEvents]) {
        eventsById.set(event.id, event);
    }

    return [...eventsById.values()].sort(
        (left, right) => right.id - left.id
    );
}

// =========================
// СТАТУС ПОДКЛЮЧЕНИЯ
// =========================

function setActivity(message) {
    if (activityMessage) {
        activityMessage.textContent = message;
    }
}

function clearPendingCommand(commandId) {
    const pending = pendingCommands.get(commandId);

    if (pending) {
        clearTimeout(pending.timer);
        pendingCommands.delete(commandId);
    }
}

function clearAllPendingCommands() {
    for (const pending of pendingCommands.values()) {
        clearTimeout(pending.timer);
    }

    pendingCommands.clear();
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
        requestEvents({ reset: true });
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
        clearAllPendingCommands();

        setActivity(
            "Соединение потеряно. Повторное подключение..."
        );

        clearTimeout(reconnectTimer);

        reconnectTimer = setTimeout(() => {
            connectGateway();
        }, 2000);
    });

    socket.addEventListener("error", (error) => {
        console.error("Ошибка WebSocket:", error);
        setActivity("Ошибка соединения с Gateway.");
    });
}

// =========================
// ОБРАБОТКА СООБЩЕНИЙ GATEWAY
// =========================

function handleGatewayMessage(message) {
    switch (message.type) {
        case "device_list": {
            devices = Array.isArray(message.devices)
                ? message.devices
                : [];

            // Отменяем ожидание, если устройство отключилось.
            for (const [commandId, pending] of pendingCommands) {
                const device = devices.find(
                    item => item.device === pending.deviceId
                );

                if (device && !device.connected) {
                    clearPendingCommand(commandId);
                }
            }

            renderDevices();
            renderEvents();

            if (devices.length === 0) {
                setActivity(
                    "Gateway доступен, устройств пока нет."
                );
            } else {
                setActivity(
                    `Найдено устройств: ${devices.length}.`
                );
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
                device = {
                    device: message.device,
                    connected: true,
                    parameters: message.parameters || {},
                    last_seen: message.last_seen
                };

                devices.push(device);
            }

            renderDevices();

            setActivity(
                `Получено состояние от ${message.device}.`
            );

            break;
        }

        case "command_result": {
            const pending = pendingCommands.get(message.command_id);

            if (!pending || message.device !== pending.deviceId) {
                break;
            }

            clearPendingCommand(message.command_id);

            if (
                message.success === true &&
                message.parameter === "led" &&
                message.value === pending.value
            ) {
                const stateText = message.value ? "включён" : "выключен";

                setActivity(
                    `Подтверждено: LED на ${message.device} ${stateText}.`
                );
            } else if (message.status === "timeout") {
                setActivity(
                    `Gateway не получил подтверждение от ${message.device} ` +
                    `за отведённое время.`
                );
            } else if (message.status === "unavailable") {
                setActivity(
                    `Команда для ${message.device} прервана: ` +
                    `${message.error || "устройство недоступно"}`
                );
            } else if (message.success !== true) {
                setActivity(
                    `Устройство ${message.device} не выполнило команду: ` +
                    `${message.error || "причина не указана"}.`
                );
            } else {
                setActivity(
                    `Результат команды ${pending.commandId} не совпал ` +
                    `с ожидаемым состоянием LED.`
                );
            }

            break;
        }

        case "event_history": {
            const incomingEvents = Array.isArray(message.events)
                ? message.events
                : [];

            if (message.request_id) {
                if (
                    !activeEventRequest ||
                    activeEventRequest.requestId !== message.request_id
                ) {
                    break;
                }

                if (activeEventRequest.deviceFilter !== eventDeviceFilter) {
                    activeEventRequest = null;
                    eventsLoading = false;
                    break;
                }

                if (activeEventRequest.mode === "reset") {
                    events = incomingEvents;
                } else {
                    events = mergeEventPages(events, incomingEvents);
                }

                eventCursor = message.next_before_id ?? null;
                eventsHasMore = message.has_more === true;
                activeEventRequest = null;
                eventsLoading = false;
            } else {
                const hadLoadedEvents = events.length > 0;
                const matchingEvents = incomingEvents.filter(event => {
                    return (
                        eventDeviceFilter === "all" ||
                        event.device_id === eventDeviceFilter
                    );
                });

                events = mergeEventPages(events, matchingEvents);

                if (!hadLoadedEvents && eventDeviceFilter === "all") {
                    eventCursor = message.next_before_id ?? null;
                    eventsHasMore = message.has_more === true;
                }
            }

            renderEvents();
            break;
        }

        case "command_sent": {
            const pending = pendingCommands.get(message.command_id);

            if (pending && pending.deviceId === message.device) {
                setActivity(
                    `Команда отправлена ${message.device}. ` +
                    "Ожидаем результат выполнения..."
                );
            }

            break;
        }

        case "error": {
            if (
                message.request_id &&
                activeEventRequest &&
                message.request_id === activeEventRequest.requestId
            ) {
                activeEventRequest = null;
                eventsLoading = false;
                renderEvents();
            }

            if (message.command_id) {
                clearPendingCommand(message.command_id);
            }

            const errorText = message.message || "Неизвестная ошибка";

            setActivity(`Ошибка: ${errorText}`);
            console.error("Gateway:", errorText);
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

        // Состояние LED.

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

        // Кнопки управления.

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

        // Время последнего сообщения.

        const hint = document.createElement("p");
        hint.className = "hint";

        hint.textContent = device.connected
            ? `Последнее сообщение: ${device.last_seen || "неизвестно"}`
            : "Устройство сейчас не подключено.";

        deviceCard.appendChild(hint);
    }
}

// =========================
// ФОРМАТИРОВАНИЕ ИСТОРИИ
// =========================

function formatEventTime(value) {
    if (!value) {
        return "Время неизвестно";
    }

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return value;
    }

    return new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
    }).format(date);
}

function getEventDescription(event) {
    const value = String(event.event_value ?? "");
    const normalized = value.toLowerCase();

    if (event.event_type === "led") {
        if (normalized === "true" || normalized === "1") {
            return "Светодиод включён";
        }

        if (normalized === "false" || normalized === "0") {
            return "Светодиод выключен";
        }

        return `Состояние LED: ${value}`;
    }

    if (event.event_type === "connection") {
        if (normalized === "подключено") {
            return "Устройство подключилось";
        }

        if (normalized === "отключено") {
            return "Устройство отключилось";
        }

        return `Состояние подключения: ${value}`;
    }

    return `${event.event_type}: ${value}`;
}

// =========================
// ИСТОРИЯ СОБЫТИЙ
// =========================

function renderEvents() {
    if (!activityCard) {
        console.error("Не найден элемент .activity-card");
        return;
    }

    // История находится в отдельном контейнере,
    // поэтому сообщение #activity-message не удаляется.

    let container = activityCard.querySelector(".event-history");

    if (!container) {
        container = document.createElement("div");
        container.className = "event-history";
        activityCard.appendChild(container);
    }

    container.replaceChildren();

    const heading = document.createElement("h2");
    heading.textContent = "История событий";
    container.appendChild(heading);

    const deviceFilterLabel = document.createElement("label");
    deviceFilterLabel.className = "event-device-filter-group";
    deviceFilterLabel.textContent = "Устройство:";

    const deviceFilter = document.createElement("select");
    deviceFilter.className = "event-device-filter";

    const allDevicesOption = document.createElement("option");
    allDevicesOption.value = "all";
    allDevicesOption.textContent = "Все устройства";
    deviceFilter.appendChild(allDevicesOption);

    for (const device of devices) {
        const option = document.createElement("option");
        option.value = device.device;
        option.textContent = device.device;
        deviceFilter.appendChild(option);
    }

    deviceFilter.value = eventDeviceFilter;
    deviceFilter.addEventListener("change", () => {
        eventDeviceFilter = deviceFilter.value;
        events = [];
        eventCursor = null;
        eventsHasMore = false;
        requestEvents({ reset: true });
        renderEvents();
    });

    deviceFilterLabel.appendChild(deviceFilter);
    container.appendChild(deviceFilterLabel);

    // Фильтры истории.

    const filters = document.createElement("div");
    filters.className = "event-filter-group";

    const filterOptions = [
        { value: "all", label: "Все события" },
        { value: "led", label: "LED" },
        { value: "connection", label: "Подключения" }
    ];

    for (const option of filterOptions) {
        const button = document.createElement("button");

        button.type = "button";
        button.className = "event-filter";
        button.textContent = option.label;

        if (eventFilter === option.value) {
            button.classList.add("active");
        }

        button.setAttribute(
            "aria-pressed",
            String(eventFilter === option.value)
        );

        button.addEventListener("click", () => {
            eventFilter = option.value;
            renderEvents();
        });

        filters.appendChild(button);
    }

    container.appendChild(filters);

    // Применяем выбранный фильтр.

    const deduplicatedEvents = [];
    const lastLedValues = new Map();

    for (const event of events) {
        if (event.event_type === "led") {
            const value = String(event.event_value).toLowerCase();
            const previousValue = lastLedValues.get(event.device_id);

            if (previousValue === value) {
                continue;
            }

            lastLedValues.set(event.device_id, value);
        }

        deduplicatedEvents.push(event);
    }

    const filteredEvents = deduplicatedEvents.filter(event => {
        return (
            eventFilter === "all" ||
            event.event_type === eventFilter
        );
    });

    const summary = document.createElement("p");
    summary.className = "event-summary";
    summary.textContent =
        `Показано ${filteredEvents.length} из ${deduplicatedEvents.length} ` +
        `загруженных событий`;

    container.appendChild(summary);

    if (filteredEvents.length === 0) {
        const empty = document.createElement("p");
        empty.className = "event-empty";

        empty.textContent = events.length === 0
            ? "Событий пока нет."
            : "Нет событий для выбранного фильтра.";

        container.appendChild(empty);
        renderLoadMoreButton(container);
        return;
    }

    const list = document.createElement("div");
    list.className = "event-list";

    // Gateway возвращает события от новых к старым.
    // Оставляем этот порядок при отображении.

    for (const event of filteredEvents) {
        const row = document.createElement("article");
        row.className = "event-entry";

        const top = document.createElement("div");
        top.className = "event-entry-top";

        const deviceName = document.createElement("strong");
        deviceName.className = "event-device";
        deviceName.textContent =
            event.device_id || "Неизвестное устройство";

        const typeBadge = document.createElement("span");
        typeBadge.className = "event-type";

        if (event.event_type === "led") {
            typeBadge.textContent = "LED";
        } else if (event.event_type === "connection") {
            typeBadge.textContent = "Подключение";
        } else {
            typeBadge.textContent = event.event_type || "Событие";
        }

        top.append(deviceName, typeBadge);

        const description = document.createElement("p");
        description.className = "event-description";
        description.textContent = getEventDescription(event);

        const timestamp = document.createElement("time");
        timestamp.className = "event-time";
        timestamp.textContent = formatEventTime(event.timestamp);

        if (event.timestamp) {
            timestamp.dateTime = event.timestamp;
        }

        row.append(top, description, timestamp);
        list.appendChild(row);
    }

    container.appendChild(list);
    renderLoadMoreButton(container);
}

function renderLoadMoreButton(container) {
    if (!eventsHasMore && !eventsLoading) {
        return;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "event-load-more";
    button.disabled = eventsLoading;
    button.textContent = eventsLoading
        ? "Загрузка..."
        : "Показать более старые события";
    button.addEventListener("click", () => {
        requestEvents();
    });

    container.appendChild(button);
}

// =========================
// ОТПРАВКА КОМАНД LED
// =========================

function sendLedCommand(deviceId, value) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        setActivity("Нет соединения с Gateway.");
        return;
    }

    const device = devices.find(
        item => item.device === deviceId
    );

    if (!device || !device.connected) {
        setActivity(`Устройство ${deviceId} не подключено.`);
        return;
    }

    const pending = {
        commandId: createCommandId(),
        deviceId: deviceId,
        value: value,
        timer: null
    };

    pendingCommands.set(pending.commandId, pending);

    pending.timer = setTimeout(() => {
        if (pendingCommands.get(pending.commandId) !== pending) {
            return;
        }

        pendingCommands.delete(pending.commandId);

        setActivity(
            `Gateway не сообщил результат команды для ${deviceId}. ` +
            "Проверьте соединение с сервером."
        );
    }, COMMAND_TIMEOUT_MS);

    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "command",
            device: deviceId,
            command_id: pending.commandId,
            parameter: "led",
            value: value
        }));

        setActivity(
            `Команда отправлена ${deviceId}. Ожидаем подтверждение...`
        );
    } catch (error) {
        clearPendingCommand(pending.commandId);

        console.error("Не удалось отправить команду:", error);

        setActivity("Ошибка отправки команды в Gateway.");
    }
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

function requestEvents({ reset = false } = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        return;
    }

    if (eventsLoading && !reset) {
        return;
    }

    if (!reset && !eventsHasMore) {
        return;
    }

    const requestId = `events-${Date.now().toString(36)}-${++eventRequestSequence}`;
    activeEventRequest = {
        requestId,
        mode: reset ? "reset" : "append",
        deviceFilter: eventDeviceFilter
    };
    eventsLoading = true;
    renderEvents();

    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_events",
            device: eventDeviceFilter,
            before_id: reset ? null : eventCursor,
            request_id: requestId
        }));
    } catch (error) {
        activeEventRequest = null;
        eventsLoading = false;
        renderEvents();
        console.error("Не удалось запросить историю событий:", error);
        setActivity("Не удалось запросить историю событий.");
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
        requestEvents({ reset: true });

        setActivity("Обновляем устройства и историю...");
    });
} else {
    console.warn("Кнопка #refresh-button не найдена.");
}

// =========================
// ЗАПУСК ПРИЛОЖЕНИЯ
// =========================

connectGateway();
