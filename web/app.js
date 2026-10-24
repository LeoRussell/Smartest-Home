const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const refreshButton = document.getElementById("refresh-button");
const activityCard = document.querySelector(".activity-card");
const overview = document.getElementById("dashboard-overview");
const gatewayStatus = document.getElementById("gateway-status");
const gatewayStatusDot = document.getElementById("gateway-status-dot");

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
const pendingSchemaDecisions = new Set();
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

function setGatewayConnectionStatus(message, connected) {
    if (gatewayStatus) gatewayStatus.textContent = message;
    if (gatewayStatusDot) {
        gatewayStatusDot.classList.toggle("connected", connected);
        gatewayStatusDot.classList.toggle("disconnected", !connected);
    }
}

function clearPendingCommand(commandId) {
    const pending = pendingCommands.get(commandId);

    if (pending) {
        clearTimeout(pending.timer);
        pendingCommands.delete(commandId);
        const device = devices.find(item => item.device === pending.deviceId);
        if (device) updateDevicePanel(device);
    }
}

function clearAllPendingCommands() {
    for (const pending of pendingCommands.values()) {
        clearTimeout(pending.timer);
    }

    pendingCommands.clear();
    for (const device of devices) updateDevicePanel(device);
}

function hasPendingParameterCommand(deviceId, parameter) {
    return [...pendingCommands.values()].some(pending =>
        pending.deviceId === deviceId && pending.parameter === parameter
    );
}

function hasPendingAction(deviceId, actionId) {
    return [...pendingCommands.values()].some(pending =>
        pending.deviceId === deviceId && pending.actionId === actionId
    );
}

function isGatewayConnected() {
    return socket?.readyState === WebSocket.OPEN;
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
    setGatewayConnectionStatus("Подключение к Gateway...", false);

    const host = window.location.hostname || "localhost";

    socket = new WebSocket(`ws://${host}:8765`);

    socket.addEventListener("open", () => {
        setGatewayConnectionStatus("Gateway подключён", true);
        setActivity("Подключено к Gateway.");
        for (const device of devices) updateDevicePanel(device);

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
        setGatewayConnectionStatus("Нет соединения с Gateway", false);
        pendingSchemaDecisions.clear();
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
        setGatewayConnectionStatus("Ошибка соединения с Gateway", false);
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
            const isNewDevice = !device;

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

            if (isNewDevice) {
                renderDevices();
            } else {
                updateDevicePanel(device);
            }

            break;
        }

        case "command_result": {
            const pending = pendingCommands.get(message.command_id);

            if (
                !pending ||
                message.device !== pending.deviceId ||
                (pending.actionId
                    ? message.action !== pending.actionId
                    : message.parameter !== pending.parameter)
            ) {
                break;
            }

            if (pending.parameter) {
                const panel = [...(deviceCard?.querySelectorAll(".device-panel") || [])]
                    .find(item => item.dataset.deviceId === pending.deviceId);
                const input = [...(panel?.querySelectorAll('[data-role="parameter-input"]') || [])]
                    .find(item => item.dataset.parameterId === pending.parameter);
                if (input) input.dataset.dirty = "false";
            }

            clearPendingCommand(message.command_id);

            if (
                message.success === true &&
                (pending.actionId || message.value === pending.value)
            ) {
                const device = devices.find(
                    item => item.device === message.device
                );
                if (pending.actionId) {
                    const label = device?.schema?.actions?.[pending.actionId]?.label || pending.actionId;
                    setActivity(`Действие «${label}» на ${message.device} выполнено.`);
                } else {
                    const definition = device?.schema?.parameters?.[pending.parameter];
                    const label = definition?.label || pending.parameter;
                    setActivity(`Подтверждено: ${label} на ${message.device} обновлено.`);
                }
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
                    `с ожидаемым значением параметра.`
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

        case "schema_decision_result": {
            pendingSchemaDecisions.delete(
                `${message.device}\u0000${message.proposal_id}`
            );
            setActivity(message.message || "Решение по Schema обработано.");
            if (message.success !== true) requestDevices();
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
                const pending = pendingCommands.get(message.command_id);
                clearPendingCommand(message.command_id);
                const device = devices.find(item => item.device === pending?.deviceId);
                if (device) updateDevicePanel(device);
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

function canonicalJson(value) {
    if (Array.isArray(value)) {
        return `[${value.map(canonicalJson).join(",")}]`;
    }
    if (value && typeof value === "object") {
        return `{${Object.keys(value).sort().map(key =>
            `${JSON.stringify(key)}:${canonicalJson(value[key])}`
        ).join(",")}}`;
    }
    return JSON.stringify(value);
}

function describeSchemaChanges(previousSchema, nextSchema) {
    const changes = [];
    const groups = [
        ["parameters", "Параметр"],
        ["actions", "Действие"]
    ];

    for (const [group, label] of groups) {
        const previous = previousSchema?.[group] || {};
        const next = nextSchema?.[group] || {};
        const ids = new Set([...Object.keys(previous), ...Object.keys(next)]);

        for (const id of ids) {
            if (!(id in previous)) {
                changes.push(`${label} «${next[id].label || id}» добавлен`);
            } else if (!(id in next)) {
                changes.push(`${label} «${previous[id].label || id}» удалён`);
            } else if (canonicalJson(previous[id]) !== canonicalJson(next[id])) {
                const oldLabel = previous[id].label || id;
                const newLabel = next[id].label || id;
                changes.push(oldLabel === newLabel
                    ? `${label} «${newLabel}» изменён`
                    : `${label} «${oldLabel}» переименован в «${newLabel}»`);
            }
        }
    }

    if (changes.length === 0 && canonicalJson(previousSchema) !== canonicalJson(nextSchema)) {
        changes.push("Изменились другие поля Schema");
    }
    return changes;
}

function createSchemaReview(device) {
    const proposal = device.pending_schema;
    const review = document.createElement("section");
    review.className = "schema-review";

    const heading = document.createElement("h4");
    heading.textContent = "Обнаружено изменение Schema";

    const version = document.createElement("p");
    const oldVersion = device.schema_version ?? "не указана";
    const newVersion = proposal.schema_version ?? "не указана";
    version.textContent = `Сохранённая версия: ${oldVersion} · новая версия: ${newVersion}`;

    const list = document.createElement("ul");
    for (const change of describeSchemaChanges(device.schema, proposal.schema)) {
        const item = document.createElement("li");
        item.textContent = change;
        list.appendChild(item);
    }

    const note = document.createElement("p");
    note.className = "schema-review-note";
    note.textContent = "Пока вы выбираете, действует прежняя Schema и команды заблокированы. Если оставить её, Gateway повторит предложение при следующем подключении устройства.";

    const buttons = document.createElement("div");
    buttons.className = "schema-review-buttons";

    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = "Принять Schema";
    const decisionKey = `${device.device}\u0000${proposal.proposal_id}`;
    accept.disabled = !isGatewayConnected() || pendingSchemaDecisions.has(decisionKey);
    accept.addEventListener("click", () => {
        sendSchemaDecision(device.device, proposal.proposal_id, "accept");
    });

    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "secondary-button";
    reject.textContent = "Оставить текущую Schema";
    reject.disabled = !isGatewayConnected() || pendingSchemaDecisions.has(decisionKey);
    reject.addEventListener("click", () => {
        sendSchemaDecision(device.device, proposal.proposal_id, "reject");
    });

    buttons.append(accept, reject);
    review.append(heading, version, list, note, buttons);
    return review;
}

function renderDevices() {
    if (!deviceCard) {
        console.error("Не найден элемент .device-card");
        return;
    }

    const drafts = new Map();
    for (const input of deviceCard.querySelectorAll('[data-role="parameter-input"][data-dirty="true"]')) {
        drafts.set(`${input.dataset.deviceId}\u0000${input.dataset.parameterId}`, input.value);
    }

    deviceCard.replaceChildren();

    if (devices.length === 0) {
        const empty = document.createElement("p");
        empty.className = "device-empty";
        empty.textContent = "Устройства пока не найдены.";

        deviceCard.appendChild(empty);
        renderOverview();
        return;
    }

    for (const device of devices) {
        const panel = document.createElement("article");
        panel.className = "device-panel";
        panel.dataset.deviceId = device.device;

        const heading = document.createElement("div");
        heading.className = "device-heading";

        const icon = document.createElement("div");
        icon.className = "device-icon";
        icon.textContent = "IoT";

        const titleBlock = document.createElement("div");

        const title = document.createElement("h3");
        title.textContent = device.name || device.device;

        const subtitle = document.createElement("p");
        subtitle.className = "device-id";
        const schemaNote = device.schema_source === "inferred"
            ? "Автоматическая Schema · только чтение"
            : device.schema_source === "missing"
                ? "Schema ещё не получена"
                : null;
        subtitle.textContent = [
            device.device,
            device.firmware_version
                ? `Прошивка ${device.firmware_version}`
                : null,
            schemaNote
        ].filter(Boolean).join(" · ");

        titleBlock.append(title, subtitle);

        const badge = document.createElement("span");
        badge.className = "badge device-status";
        badge.textContent = device.connected
            ? "Подключено"
            : "Отключено";

        heading.append(icon, titleBlock, badge);
        panel.appendChild(heading);

        const divider = document.createElement("div");
        divider.className = "divider";
        panel.appendChild(divider);

        if (device.pending_schema) {
            panel.appendChild(createSchemaReview(device));
        }

        const parameters = device.schema?.parameters || {};

        for (const [parameterId, definition] of Object.entries(parameters)) {
            const parameter = document.createElement("div");
            parameter.className = "device-parameter";
            parameter.dataset.parameterId = parameterId;

            const label = document.createElement("strong");
            label.className = "device-parameter-label";
            label.textContent = definition.label || parameterId;

            const value = device.parameters?.[parameterId];
            const currentValue = document.createElement("span");
            currentValue.className = "device-parameter-value";
            currentValue.dataset.role = "parameter-value";
            currentValue.dataset.parameterId = parameterId;
            currentValue.textContent = formatParameterValue(value, definition);

            parameter.append(label, currentValue);

            if (definition.access === "read_write" && device.connected) {
                const control = document.createElement("div");
                control.className = "device-parameter-control";

                if (definition.type === "boolean") {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.dataset.role = "parameter-command";
                    button.dataset.parameterId = parameterId;
                    button.addEventListener("click", () => {
                        const currentDevice = devices.find(
                            item => item.device === device.device
                        );
                        const currentValue = currentDevice?.parameters?.[parameterId];
                        if (typeof currentValue === "boolean") {
                            sendParameterCommand(
                                device.device,
                                parameterId,
                                !currentValue
                            );
                        }
                    });
                    control.appendChild(button);
                } else {
                    const input = document.createElement(
                        definition.type === "enum" ? "select" : "input"
                    );
                    input.className = "device-parameter-input";
                    input.dataset.role = "parameter-input";
                    input.dataset.deviceId = device.device;
                    input.dataset.parameterId = parameterId;
                    input.addEventListener(
                        definition.type === "enum" ? "change" : "input",
                        () => { input.dataset.dirty = "true"; }
                    );

                    if (definition.type === "enum") {
                        for (const option of definition.options || []) {
                            const item = document.createElement("option");
                            item.value = JSON.stringify(option.value);
                            item.textContent = option.label;
                            item.selected = option.value === value;
                            input.appendChild(item);
                        }
                    } else {
                        input.type = definition.type === "string" ? "text" : "number";
                        input.value = value ?? "";
                        if (definition.type === "integer" || definition.type === "number") {
                            input.required = true;
                        }
                        if (definition.type === "integer") {
                            input.step = String(definition.step || 1);
                        } else if (definition.type === "number") {
                            input.step = String(definition.step || "any");
                        }
                        if (definition.min !== undefined) input.min = definition.min;
                        if (definition.max !== undefined) input.max = definition.max;
                    }

                    const savedDraft = drafts.get(`${device.device}\u0000${parameterId}`);
                    if (savedDraft !== undefined) {
                        input.value = savedDraft;
                        input.dataset.dirty = "true";
                    }

                    const apply = document.createElement("button");
                    apply.type = "button";
                    apply.dataset.role = "parameter-apply";
                    apply.dataset.parameterId = parameterId;
                    apply.textContent = "Применить";
                    apply.addEventListener("click", () => {
                        if (!input.checkValidity()) {
                            input.reportValidity();
                            return;
                        }

                        let nextValue = input.value;
                        if (definition.type === "enum") {
                            nextValue = JSON.parse(input.value);
                        } else if (definition.type === "integer") {
                            nextValue = Number.parseInt(input.value, 10);
                        } else if (definition.type === "number") {
                            nextValue = Number(input.value);
                        }
                        sendParameterCommand(device.device, parameterId, nextValue);
                    });
                    control.append(input, apply);
                }

                parameter.appendChild(control);
            }

            panel.appendChild(parameter);
        }

        const actions = device.schema?.actions || {};
        const availableActions = Object.entries(actions);
        if (availableActions.length > 0) {
            const actionList = document.createElement("div");
            actionList.className = "device-actions";
            const actionHeading = document.createElement("strong");
            actionHeading.textContent = "Действия устройства";
            actionList.appendChild(actionHeading);

            for (const [actionId, definition] of availableActions) {
                const actionButton = document.createElement("button");
                actionButton.type = "button";
                actionButton.dataset.role = "device-action";
                actionButton.dataset.actionId = actionId;
                actionButton.textContent = definition.label || actionId;
                actionButton.title = definition.description || "";
                actionButton.addEventListener("click", () => {
                    if (Object.keys(definition.arguments || {}).length > 0) {
                        openActionDialog(device.device, actionId, definition);
                    } else {
                        sendDeviceAction(device.device, actionId);
                    }
                });
                actionList.appendChild(actionButton);
            }
            panel.appendChild(actionList);
        }

        // Время последнего сообщения.

        const hint = document.createElement("p");
        hint.className = "hint";

        hint.textContent = device.connected
            ? `Последнее сообщение: ${device.last_seen || "неизвестно"}`
            : "Устройство сейчас не подключено.";

        hint.dataset.role = "device-hint";
        panel.appendChild(hint);

        deviceCard.appendChild(panel);
        updateDevicePanel(device);
    }

    renderOverview();
}

function updateDevicePanel(device) {
    const panel = [...(deviceCard?.querySelectorAll(".device-panel") || [])]
        .find(item => item.dataset.deviceId === device.device);
    if (!panel) return;

    const badge = panel.querySelector(".device-status");
    if (badge) {
        badge.textContent = device.connected ? "Подключено" : "Отключено";
        badge.classList.toggle("is-online", device.connected);
        badge.classList.toggle("is-offline", !device.connected);
    }

    for (const valueNode of panel.querySelectorAll('[data-role="parameter-value"]')) {
        const parameterId = valueNode.dataset.parameterId;
        const definition = device.schema?.parameters?.[parameterId];
        valueNode.textContent = formatParameterValue(
            device.parameters?.[parameterId],
            definition || { type: typeof device.parameters?.[parameterId] }
        );
    }

    for (const input of panel.querySelectorAll('[data-role="parameter-input"]')) {
        input.disabled = !device.connected || Boolean(device.pending_schema);
        if (input.dataset.dirty === "true" || document.activeElement === input) {
            continue;
        }
        const parameterId = input.dataset.parameterId;
        const value = device.parameters?.[parameterId];
        input.value = input.tagName === "SELECT"
            ? (value === undefined ? "" : JSON.stringify(value))
            : (value ?? "");
    }

    for (const button of panel.querySelectorAll('[data-role="parameter-command"]')) {
        const parameterId = button.dataset.parameterId;
        const definition = device.schema?.parameters?.[parameterId];
        const value = device.parameters?.[parameterId];
        const pending = hasPendingParameterCommand(device.device, parameterId);
        const label = definition?.label || parameterId;
        button.textContent = pending
            ? `Ожидаем: ${label}…`
            : value === true
                ? `Выключить: ${label}`
                : `Включить: ${label}`;
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || typeof value !== "boolean" || pending;
    }

    for (const button of panel.querySelectorAll('[data-role="parameter-apply"]')) {
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) ||
            hasPendingParameterCommand(device.device, button.dataset.parameterId);
    }

    for (const button of panel.querySelectorAll('[data-role="device-action"]')) {
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) ||
            hasPendingAction(device.device, button.dataset.actionId);
    }

    const schemaDecisionPending = device.pending_schema && pendingSchemaDecisions.has(
        `${device.device}\u0000${device.pending_schema.proposal_id}`
    );
    for (const button of panel.querySelectorAll(".schema-review-buttons button")) {
        button.disabled = !isGatewayConnected() || Boolean(schemaDecisionPending);
    }

    const hint = panel.querySelector('[data-role="device-hint"]');
    if (hint) {
        hint.textContent = device.connected
            ? `Последнее сообщение: ${device.last_seen || "неизвестно"}`
            : "Устройство сейчас не подключено.";
    }
    renderOverview();
}

function renderOverview() {
    if (!overview) return;
    const online = devices.filter(device => device.connected).length;
    const metrics = [
        { value: String(devices.length), label: "Устройства" },
        { value: String(online), label: "На связи" },
        { value: String(Math.max(0, devices.length - online)), label: "Не в сети" }
    ];
    overview.replaceChildren();
    for (const metric of metrics) {
        const item = document.createElement("div");
        item.className = "overview-metric";
        const value = document.createElement("strong");
        value.textContent = metric.value;
        const label = document.createElement("span");
        label.textContent = metric.label;
        item.append(value, label);
        overview.appendChild(item);
    }
}

function formatParameterValue(value, definition) {
    if (value === undefined || value === null) {
        return "Неизвестно";
    }

    let formatted = value;
    if (definition.type === "json") {
        formatted = JSON.stringify(value);
    } else if (definition.type === "boolean") {
        formatted = value ? "Включено" : "Выключено";
    } else if (definition.type === "enum") {
        formatted = definition.options?.find(option => option.value === value)?.label || value;
    } else if (typeof value === "number" && Number.isInteger(definition.precision)) {
        formatted = value.toFixed(definition.precision);
    }

    return definition.unit
        ? `${formatted} ${definition.unit}`
        : String(formatted);
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

    if (event.event_type === "command") {
        let command;

        try {
            command = JSON.parse(value);
        } catch (error) {
            return `Результат команды: ${value}`;
        }

        const device = devices.find(item => item.device === event.device_id);
        const isAction = typeof command.action === "string";
        const definition = isAction
            ? device?.schema?.actions?.[command.action]
            : device?.schema?.parameters?.[command.parameter];
        const operationLabel = definition?.label || command.action || command.parameter || "Команда";
        const requestedValue = isAction
            ? "вызвать"
            : typeof command.requested_value === "boolean"
                ? (command.requested_value ? "включить" : "выключить")
                : formatParameterValue(
                    command.requested_value,
                    definition || { type: typeof command.requested_value }
                );
        const statusLabels = {
            succeeded: "выполнена",
            failed: "завершилась ошибкой",
            timeout: "не получила подтверждение",
            unavailable: "прервана: устройство отключилось"
        };
        const status = statusLabels[command.status] || command.status;
        const actualValue = command.result_value;
        const actualText = (
            !isAction &&
            ["succeeded", "failed"].includes(command.status) &&
            actualValue !== undefined &&
            actualValue !== null &&
            actualValue !== command.requested_value
        )
            ? `, фактически: ${formatParameterValue(
                actualValue,
                definition || { type: typeof actualValue }
            )}`
            : "";
        const errorText = command.error ? ` — ${command.error}` : "";

        return `${operationLabel}: ${requestedValue}, ${status}${actualText}${errorText}`;
    }

    if (event.event_type === "parameter") {
        try {
            const change = JSON.parse(value);
            const device = devices.find(item => item.device === event.device_id);
            const definition = device?.schema?.parameters?.[change.parameter] || {
                type: typeof change.value
            };
            const label = definition.label || change.parameter;
            return `${label}: ${formatParameterValue(change.value, definition)}`;
        } catch (error) {
            return `Изменение параметра: ${value}`;
        }
    }

    if (event.event_type === "schema") {
        return `Обновление Schema: ${value}`;
    }

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

function isCommandEchoEvent(event, eventList) {
    let parameter;
    let value;
    try {
        if (event.event_type === "led") {
            parameter = "led";
            const normalized = String(event.event_value).toLowerCase();
            if (normalized !== "true" && normalized !== "false" && normalized !== "1" && normalized !== "0") {
                return false;
            }
            value = normalized === "true" || normalized === "1";
        } else if (event.event_type === "parameter") {
            const change = JSON.parse(event.event_value || "{}");
            parameter = change.parameter;
            value = change.value;
        } else {
            return false;
        }
    } catch (error) {
        return false;
    }

    const stateTime = new Date(event.timestamp).getTime();
    if (!parameter || Number.isNaN(stateTime)) return false;

    return eventList.some(commandEvent => {
        if (
            commandEvent.event_type !== "command" ||
            commandEvent.device_id !== event.device_id ||
            (Number.isFinite(commandEvent.id) && Number.isFinite(event.id) && commandEvent.id <= event.id)
        ) {
            return false;
        }

        try {
            const command = JSON.parse(commandEvent.event_value || "{}");
            if (command.status !== "succeeded" || command.parameter !== parameter) {
                return false;
            }
            const commandValue = command.result_value ?? command.requested_value;
            const commandTime = new Date(commandEvent.timestamp).getTime();
            return commandValue === value &&
                !Number.isNaN(commandTime) &&
                Math.abs(commandTime - stateTime) <= 5000;
        } catch (error) {
            return false;
        }
    });
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
        { value: "connection", label: "Подключения" },
        { value: "command", label: "Команды" },
        { value: "state", label: "Изменения состояния" },
        { value: "schema", label: "Обновления Schema" }
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
        if (isCommandEchoEvent(event, events)) {
            continue;
        }

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
        if (eventFilter === "all") {
            return true;
        }

        if (eventFilter === "command") {
            return event.event_type === "command";
        }

        if (eventFilter === "state") {
            return event.event_type === "led" || event.event_type === "parameter";
        }

        return event.event_type === eventFilter;
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
        if (event.event_type === "command") {
            try {
                const command = JSON.parse(event.event_value || "{}");
                if (["succeeded", "failed", "timeout", "unavailable"].includes(command.status)) {
                    row.classList.add(`event-status-${command.status}`);
                }
            } catch (error) {
                // Older command entries may contain plain text.
            }
        }

        const top = document.createElement("div");
        top.className = "event-entry-top";

        const deviceName = document.createElement("strong");
        deviceName.className = "event-device";
        deviceName.textContent =
            event.device_id || "Неизвестное устройство";

        const typeBadge = document.createElement("span");
        typeBadge.className = "event-type";

        if (event.event_type === "connection") {
            typeBadge.textContent = "Подключение";
        } else if (event.event_type === "schema") {
            typeBadge.textContent = "Обновление Schema";
        } else if (event.event_type === "parameter") {
            typeBadge.textContent = "Изменение состояния";
        } else if (event.event_type === "led") {
            typeBadge.textContent = "Изменение состояния";
        } else if (event.event_type === "command") {
            typeBadge.textContent = "Результат команды";
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
// ОТПРАВКА КОМАНД ДЛЯ ПАРАМЕТРОВ
// =========================

function sendParameterCommand(deviceId, parameter, value) {
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

    if (device.pending_schema) {
        setActivity("Сначала рассмотрите предложенное изменение Schema устройства.");
        return;
    }

    if (hasPendingParameterCommand(deviceId, parameter)) {
        setActivity(`Для параметра ${parameter} уже ожидается ответ.`);
        return;
    }

    const definition = device.schema?.parameters?.[parameter];
    if (
        !definition ||
        definition.access !== "read_write" ||
        !isValueCompatible(value, definition)
    ) {
        setActivity("Значение не соответствует разрешениям или типу параметра.");
        return;
    }

    const pending = {
        commandId: createCommandId(),
        deviceId: deviceId,
        parameter: parameter,
        value: value,
        timer: null
    };

    pendingCommands.set(pending.commandId, pending);
    updateDevicePanel(device);

    pending.timer = setTimeout(() => {
        if (pendingCommands.get(pending.commandId) !== pending) {
            return;
        }

        pendingCommands.delete(pending.commandId);
        updateDevicePanel(device);

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
            parameter: parameter,
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

function createActionArgumentInput(definition) {
    const input = ["boolean", "enum"].includes(definition.type)
        ? document.createElement("select")
        : document.createElement("input");
    const required = definition.required !== false;
    input.className = "device-parameter-input";
    input.required = required;

    if (definition.type === "boolean") {
        if (!required) {
            const empty = document.createElement("option");
            empty.value = "";
            empty.textContent = "Не указывать";
            input.appendChild(empty);
        }
        for (const value of [true, false]) {
            const option = document.createElement("option");
            option.value = String(value);
            option.textContent = value ? "Да" : "Нет";
            input.appendChild(option);
        }
    } else if (definition.type === "enum") {
        if (!required) {
            const empty = document.createElement("option");
            empty.value = "";
            empty.textContent = "Не указывать";
            input.appendChild(empty);
        }
        for (const option of definition.options || []) {
            const item = document.createElement("option");
            item.value = JSON.stringify(option.value);
            item.textContent = option.label;
            input.appendChild(item);
        }
    } else {
        input.type = definition.type === "string" ? "text" : "number";
        if (definition.type === "integer") {
            input.step = String(definition.step || 1);
        } else if (definition.type === "number") {
            input.step = String(definition.step || "any");
        }
        if (definition.min !== undefined) input.min = definition.min;
        if (definition.max !== undefined) input.max = definition.max;
    }

    return input;
}

function openActionDialog(deviceId, actionId, definition) {
    const dialog = document.createElement("dialog");
    dialog.className = "action-dialog";
    const form = document.createElement("form");
    form.className = "action-dialog-form";

    const heading = document.createElement("h3");
    heading.textContent = definition.label || actionId;
    form.appendChild(heading);

    if (definition.description) {
        const description = document.createElement("p");
        description.textContent = definition.description;
        form.appendChild(description);
    }

    const fields = [];
    for (const [argumentId, argumentDefinition] of Object.entries(
        definition.arguments || {}
    )) {
        const label = document.createElement("label");
        label.className = "action-argument";
        const argumentLabel = argumentDefinition.label || argumentId;
        label.textContent = argumentDefinition.unit
            ? `${argumentLabel} (${argumentDefinition.unit})`
            : argumentLabel;
        const input = createActionArgumentInput(argumentDefinition);
        label.appendChild(input);
        form.appendChild(label);
        fields.push({ argumentId, definition: argumentDefinition, input });
    }

    const buttons = document.createElement("div");
    buttons.className = "action-dialog-buttons";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.textContent = "Выполнить";
    submit.addEventListener("click", () => {
        for (const field of fields) {
            if (!field.input.checkValidity()) {
                field.input.reportValidity();
                return;
            }
        }

        const argumentsValue = {};
        for (const field of fields) {
            const { argumentId, definition: argumentDefinition, input } = field;
            if (input.value === "" && argumentDefinition.required === false) {
                continue;
            }

            let value = input.value;
            if (argumentDefinition.type === "boolean") {
                value = value === "true";
            } else if (argumentDefinition.type === "enum") {
                value = JSON.parse(value);
            } else if (argumentDefinition.type === "integer") {
                value = Number.parseInt(value, 10);
            } else if (argumentDefinition.type === "number") {
                value = Number(value);
            }
            argumentsValue[argumentId] = value;
        }

        dialog.close();
        sendDeviceAction(deviceId, actionId, argumentsValue);
    });

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary-button";
    cancel.textContent = "Отмена";
    cancel.addEventListener("click", () => dialog.close());
    buttons.append(submit, cancel);
    form.appendChild(buttons);
    dialog.appendChild(form);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });

    document.body.appendChild(dialog);
    if (typeof dialog.showModal === "function") {
        dialog.showModal();
    } else {
        dialog.remove();
        setActivity("Браузер не поддерживает форму аргументов действия.");
    }
}

function sendDeviceAction(deviceId, actionId, argumentsValue = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        setActivity("Нет соединения с Gateway.");
        return;
    }
    const device = devices.find(item => item.device === deviceId);
    if (!device || !device.connected || !device.schema?.actions?.[actionId]) {
        setActivity(`Действие ${actionId} недоступно для ${deviceId}.`);
        return;
    }
    if (device.pending_schema) {
        setActivity("Сначала рассмотрите предложенное изменение Schema устройства.");
        return;
    }
    if (hasPendingAction(deviceId, actionId)) {
        setActivity(`Действие ${actionId} уже выполняется на ${deviceId}.`);
        return;
    }

    const pending = {
        commandId: createCommandId(),
        deviceId,
        actionId,
        arguments: argumentsValue,
        timer: null
    };
    pendingCommands.set(pending.commandId, pending);
    updateDevicePanel(device);
    pending.timer = setTimeout(() => {
        if (pendingCommands.get(pending.commandId) !== pending) return;
        pendingCommands.delete(pending.commandId);
        updateDevicePanel(device);
        setActivity(`Gateway не сообщил результат действия для ${deviceId}.`);
    }, COMMAND_TIMEOUT_MS);

    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "invoke_action",
            device: deviceId,
            action_id: actionId,
            arguments: argumentsValue,
            command_id: pending.commandId
        }));
        setActivity(`Действие отправлено ${deviceId}. Ожидаем результат...`);
    } catch (error) {
        clearPendingCommand(pending.commandId);
        setActivity("Ошибка отправки действия в Gateway.");
    }
}

function isValueCompatible(value, definition) {
    if (definition.type === "boolean") return typeof value === "boolean";
    if (definition.type === "integer") return Number.isInteger(value);
    if (definition.type === "number") return Number.isFinite(value);
    if (definition.type === "string") return typeof value === "string";
    if (definition.type === "enum") {
        return definition.options?.some(option => option.value === value) === true;
    }
    return false;
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

function sendSchemaDecision(deviceId, proposalId, decision) {
    if (!isGatewayConnected()) {
        setActivity("Нет соединения с Gateway.");
        return;
    }

    const decisionKey = `${deviceId}\u0000${proposalId}`;
    if (pendingSchemaDecisions.has(decisionKey)) return;
    pendingSchemaDecisions.add(decisionKey);

    const panel = [...(deviceCard?.querySelectorAll(".device-panel") || [])]
        .find(item => item.dataset.deviceId === deviceId);
    for (const button of panel?.querySelectorAll(".schema-review-buttons button") || []) {
        button.disabled = true;
    }

    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "decide_schema",
            device: deviceId,
            proposal_id: proposalId,
            decision
        }));
        setActivity(decision === "accept"
            ? `Принимаем новую Schema устройства ${deviceId}...`
            : `Оставляем прежнюю Schema устройства ${deviceId}; предложение повторится при следующем подключении.`);
    } catch (error) {
        pendingSchemaDecisions.delete(decisionKey);
        requestDevices();
        setActivity("Не удалось отправить решение по Schema в Gateway.");
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
