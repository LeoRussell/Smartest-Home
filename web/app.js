const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const actionFeedback = document.getElementById("action-feedback");
const refreshButton = document.getElementById("refresh-button");
const activityCard = document.querySelector(".activity-card");
const overview = document.getElementById("dashboard-overview");
const blockedDevicesSection = document.getElementById("blocked-devices");
const blockedDevicesList = document.getElementById("blocked-devices-list");
const blockedDeviceCount = document.getElementById("blocked-device-count");
const gatewayStatus = document.getElementById("gateway-status");
const gatewayStatusDot = document.getElementById("gateway-status-dot");
const navigation = document.getElementById("main-navigation");
const notificationButton = document.getElementById("notification-button");
const notificationPanel = document.getElementById("notification-panel");
const notificationList = document.getElementById("notification-list");
const notificationCount = document.getElementById("notification-count");
const notificationSummary = document.getElementById("notification-summary");
const markNotificationsReadButton = document.getElementById("mark-notifications-read");
const settingNavigation = document.getElementById("setting-navigation");
const settingLanguage = document.getElementById("setting-language");
const settingTimezone = document.getElementById("setting-timezone");
const settingImportantOnly = document.getElementById("setting-important-only");
const settingsSavedMessage = document.getElementById("settings-saved");
const databaseStatistics = document.getElementById("database-statistics");
const automationForm = document.getElementById("automation-editor-form");
const automationList = document.getElementById("automation-list");
const automationFeedback = document.getElementById("automation-feedback");
const automationCancelEdit = document.getElementById("automation-cancel-edit");
const automationSaveButton = document.getElementById("automation-save-button");
const automationClearButton = document.getElementById("automation-clear");
const automationUndoButton = document.getElementById("automation-undo");
const automationRedoButton = document.getElementById("automation-redo");

const UI_SETTINGS_KEY = "iot-gateway-ui-settings-v1";
const NOTIFICATION_STATE_KEY = "iot-gateway-notifications-v1";

function readLocalState(key, fallback) {
    try {
        const value = JSON.parse(localStorage.getItem(key));
        return value ?? fallback;
    } catch (error) {
        return fallback;
    }
}

const savedUiSettings = readLocalState(UI_SETTINGS_KEY, {});
const uiSettings = {
    navigation: savedUiSettings.navigation === "side" ? "side" : "top",
    language: savedUiSettings.language === "en" ? "en" : "ru",
    timezone: typeof savedUiSettings.timezone === "string" ? savedUiSettings.timezone : "local",
    importantOnly: savedUiSettings.importantOnly === true
};
const savedNotificationState = readLocalState(NOTIFICATION_STATE_KEY, {});
let unreadNotifications = Array.isArray(savedNotificationState.unread)
    ? savedNotificationState.unread.filter(item => item && Number.isFinite(Number(item.id))).slice(0, 50)
    : [];
let lastReadEventId = typeof savedNotificationState.lastReadEventId === "number" && Number.isFinite(savedNotificationState.lastReadEventId)
    ? Number(savedNotificationState.lastReadEventId)
    : null;
let readNotificationIds = new Set(
    Array.isArray(savedNotificationState.readIds)
        ? savedNotificationState.readIds.map(String).slice(-500)
        : []
);
let notificationStateReady = false;
let observedMaxEventId = lastReadEventId || 0;
let notificationBackfillSequence = 0;
let notificationBackfillActive = null;
let pendingEventFocusId = null;
let activePage = "devices";
let lastActivityMessage = "";
let lastGatewayStatusMessage = "Нет соединения с Gateway";
let gatewayIsConnectedState = false;
let databaseStats = null;
let databaseStatsReceivedAt = 0;
let databaseStatsFrozenElapsed = null;
let automations = [];
let editingAutomationId = null;
let pendingAutomationWorkspaceState = null;

// =========================
// СОСТОЯНИЕ ПРИЛОЖЕНИЯ
// =========================

let socket = null;
let reconnectTimer = null;

let devices = [];
const selectedDeviceModes = new Map();
let events = [];
let blockedDevices = [];
let pendingDeviceAccessRequests = [];
let deviceAccessDialog = null;
let deviceAccessDialogRequestId = null;
let deviceIdentityDialog = null;
let eventFilter = "all";
let eventDeviceFilter = "all";
let eventCursor = null;
let eventsHasMore = false;
let eventsLoading = false;
let eventRequestSequence = 0;
let activeEventRequest = null;

const pendingCommands = new Map();
const pendingSchemaDecisions = new Set();
const schemaHistoryByDevice = new Map();
const schemaHistoryRequests = new Set();
const openSchemaHistoryDevices = new Set();
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

function localized(russian, english) {
    return uiSettings.language === "en" ? english : russian;
}

function formatDatabaseDuration(value, elapsedSeconds = 0) {
    let seconds = Math.max(0, Math.floor(Number(value) + elapsedSeconds));
    const days = Math.floor(seconds / 86400);
    seconds %= 86400;
    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    const minutes = Math.floor(seconds / 60);
    seconds %= 60;
    const parts = [];

    if (uiSettings.language === "en") {
        if (days) parts.push(`${days}d`);
        if (hours) parts.push(`${hours}h`);
        if (minutes) parts.push(`${minutes}m`);
        if (parts.length === 0) parts.push(`${seconds}s`);
    } else {
        if (days) parts.push(`${days} д`);
        if (hours) parts.push(`${hours} ч`);
        if (minutes) parts.push(`${minutes} мин`);
        if (parts.length === 0) parts.push(`${seconds} с`);
    }

    return parts.join(" ");
}

function formatDatabaseSize(value) {
    const units = uiSettings.language === "en"
        ? ["B", "KB", "MB", "GB", "TB"]
        : ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let size = Math.max(0, Number(value) || 0);
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
    }
    const locale = uiSettings.language === "en" ? "en-US" : "ru-RU";
    const digits = unit === 0 ? 0 : 1;
    return `${new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(size)} ${units[unit]}`;
}

function renderDatabaseStats() {
    if (!databaseStatistics || !databaseStats) return;
    const elapsed = databaseStatsFrozenElapsed ?? Math.max(
        0,
        performance.now() / 1000 - databaseStatsReceivedAt
    );
    const locale = uiSettings.language === "en" ? "en-US" : "ru-RU";

    for (const element of databaseStatistics.querySelectorAll("[data-db-stat]")) {
        const key = element.dataset.dbStat;
        const value = Number(databaseStats[key]);
        if (!Number.isFinite(value)) continue;

        if (key === "database_size_bytes") {
            element.textContent = formatDatabaseSize(value);
        } else if (key.endsWith("_uptime_seconds")) {
            element.textContent = formatDatabaseDuration(value, elapsed);
        } else {
            element.textContent = new Intl.NumberFormat(locale).format(value);
        }
    }
}

function localizeActivityMessage(message) {
    if (uiSettings.language !== "en") return message;
    const exact = {
        "Нет соединения с Gateway.": "No connection to Gateway.",
        "Не удалось отправить запрос в Gateway.": "Could not send the request to Gateway.",
        "Подключено к Gateway.": "Connected to Gateway.",
        "Gateway доступен, устройств пока нет.": "Gateway is online. No devices found yet.",
        "Обновляем устройства и историю...": "Refreshing devices and event history...",
        "Не удалось запросить историю событий.": "Could not load event history.",
        "Запрос обработан.": "Request completed.",
        "Подключение устройства разрешено.": "Device connection allowed.",
        "Подключение устройства отклонено.": "Device connection denied.",
        "Чтобы изменить ID, сначала отключите устройство.": "Disconnect the device before changing its ID.",
        "Сначала обработайте ожидающий запрос на подключение устройства.": "Resolve the pending device connection request first.",
        "Сначала дождитесь завершения или остановите выполняющиеся сценарии.": "Wait for or stop running automations first.",
        "Такой ID уже занят другим устройством.": "This ID is already used by another device.",
        "Такой ID уже запрашивает подключение.": "A device with this ID is requesting connection approval.",
        "Устройство не найдено.": "Device not found.",
        "Некорректный ID устройства.": "Invalid device ID.",
        "Имя устройства должно быть не длиннее 60 символов.": "The display name must be 60 characters or fewer.",
        "ID должен начинаться с английской буквы и содержать только английские буквы, цифры, дефис или подчёркивание.": "The ID must start with an English letter and contain only English letters, digits, hyphens, or underscores.",
        "Блокировка снята. При следующем подключении появится запрос разрешения.": "Block removed. Gateway will ask for approval next time the device connects.",
        "Снимок Schema удалён из истории. Текущая Schema устройства не изменена.": "Schema snapshot deleted from history. The device's current Schema was not changed.",
        "Schema Gateway восстановлена. При следующем подключении она будет сравнена с Schema устройства.": "Gateway Schema restored. It will be compared with the device Schema next time it connects."
    };
    if (exact[message]) return exact[message];
    let match = message.match(/^Имя и ID устройства обновлены\. В Blockly используется ID «(.+)»\.$/);
    if (match) return `Device name and ID updated. Blockly uses “${match[1]}”.`;
    match = message.match(/^Найдено устройств: (\d+)\.$/);
    if (match) return `Devices found: ${match[1]}.`;
    match = message.match(/^Действие «(.+)» на (.+) выполнено\.$/);
    if (match) return `Action “${match[1]}” completed on ${match[2]}.`;
    match = message.match(/^Подтверждено: (.+) на (.+) обновлено\.$/);
    if (match) return `Confirmed: ${match[1]} updated on ${match[2]}.`;
    match = message.match(/^Действие отправлено (.+)\. Ожидаем результат\.\.\.$/);
    if (match) return `Action sent to ${match[1]}. Waiting for the result...`;
    match = message.match(/^Команда отправлена (.+)\. Ожидаем результат выполнения\.\.\.$/);
    if (match) return `Command sent to ${match[1]}. Waiting for completion...`;
    return message;
}

function formatDisplayedParameterValue(value, definition) {
    if (uiSettings.language === "en" && typeof value === "boolean") {
        return value ? "On" : "Off";
    }
    return formatParameterValue(value, definition);
}

function saveUiSettings() {
    try {
        localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify(uiSettings));
    } catch (error) {
        console.warn("Не удалось сохранить настройки интерфейса:", error);
    }
}

function saveNotificationState() {
    try {
        localStorage.setItem(NOTIFICATION_STATE_KEY, JSON.stringify({
            unread: unreadNotifications.slice(0, 50),
            lastReadEventId,
            readIds: [...readNotificationIds].slice(-500)
        }));
    } catch (error) {
        console.warn("Не удалось сохранить уведомления:", error);
    }
}

function applyLanguage() {
    document.documentElement.lang = uiSettings.language;
    for (const element of document.querySelectorAll("[data-ru][data-en]")) {
        element.textContent = element.dataset[uiSettings.language] || element.dataset.ru;
    }
    const automationNameInput = document.getElementById("automation-name");
    if (automationNameInput) {
        automationNameInput.placeholder = automationNameInput.dataset[`placeholder${uiSettings.language === "en" ? "En" : "Ru"}`] || "";
    }
    if (notificationButton) {
        notificationButton.setAttribute(
            "aria-label",
            localized("Уведомления", "Notifications")
        );
    }
    if (navigation) {
        navigation.setAttribute(
            "aria-label",
            localized("Главное меню", "Main navigation")
        );
    }
    renderBlockedDevices();
    renderNotifications();
    renderDevices();
    renderEvents();
    renderDatabaseStats();
    refreshAutomationEditor();
    renderAutomations();
    updateAutomationEditorMode();
    setGatewayConnectionStatus(lastGatewayStatusMessage, gatewayIsConnectedState);
    if (lastActivityMessage) setActivity(lastActivityMessage);
}

function setActivePage(page) {
    if (!document.querySelector(`[data-page-panel="${page}"]`)) return;
    activePage = page;
    for (const panel of document.querySelectorAll("[data-page-panel]")) {
        panel.hidden = panel.dataset.pagePanel !== page;
    }
    for (const button of navigation?.querySelectorAll("[data-page]") || []) {
        const active = button.dataset.page === page;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
    }
    if (page === "automations") refreshAutomationEditor();
    if (page === "events" && events.length === 0 && !pendingEventFocusId && isGatewayConnected()) {
        requestEvents({ reset: true });
    }
}

function isImportantNotification(event) {
    if (event.event_type === "connection" || event.event_type === "schema") return true;
    if (event.event_type !== "command") return false;
    try {
        return ["failed", "timeout", "unavailable"].includes(
            JSON.parse(event.event_value || "{}").status
        );
    } catch (error) {
        return false;
    }
}

function observeEventNotifications(incomingEvents, initialize = false) {
    const validEvents = incomingEvents.filter(event => Number.isFinite(Number(event.id)));
    for (const event of validEvents) {
        observedMaxEventId = Math.max(observedMaxEventId, Number(event.id));
    }

    if (initialize && !notificationStateReady) {
        notificationStateReady = true;
        if (lastReadEventId === null) {
            lastReadEventId = observedMaxEventId;
            saveNotificationState();
            return;
        }
    }
    if (!notificationStateReady) return;

    const existingIds = new Set(unreadNotifications.map(item => String(item.id)));
    let added = false;
    let stateChanged = false;
    for (const event of validEvents) {
        const eventId = Number(event.id);
        const id = String(eventId);
        if (
            uiSettings.importantOnly &&
            !isImportantNotification(event) &&
            (lastReadEventId === null || eventId > lastReadEventId)
        ) {
            readNotificationIds.add(id);
            stateChanged = true;
            continue;
        }
        if (
            (lastReadEventId !== null && eventId <= lastReadEventId) ||
            readNotificationIds.has(id) ||
            existingIds.has(id)
        ) continue;
        unreadNotifications.unshift(event);
        existingIds.add(id);
        added = true;
    }
    if (added) {
        unreadNotifications.sort((left, right) => Number(right.id) - Number(left.id));
        unreadNotifications = unreadNotifications.slice(0, 50);
        stateChanged = true;
    }
    if (stateChanged) {
        saveNotificationState();
    }
    renderNotifications();
}

function requestNotificationBackfill(beforeId) {
    if (
        !isGatewayConnected() ||
        !Number.isInteger(beforeId) ||
        beforeId < 1 ||
        notificationBackfillActive
    ) return;
    const requestId = `notification-backfill-${++notificationBackfillSequence}`;
    notificationBackfillActive = requestId;
    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_events",
            device: "all",
            before_id: beforeId,
            request_id: requestId
        }));
    } catch (error) {
        notificationBackfillActive = null;
    }
}

function eventKindLabel(event) {
    const labels = {
        connection: localized("Подключение", "Connection"),
        schema: localized("Schema", "Schema"),
        command: localized("Команда", "Command"),
        led: localized("Изменение состояния", "State change"),
        parameter: localized("Изменение состояния", "State change")
    };
    return labels[event.event_type] || localized("Событие", "Event");
}

function renderNotifications() {
    if (!notificationList) return;
    const count = unreadNotifications.length;
    notificationButton?.classList.toggle("has-unread", count > 0);
    notificationButton?.setAttribute(
        "aria-label",
        count
            ? localized(`Уведомления, непрочитанных: ${count}`, `Notifications, ${count} unread`)
            : localized("Уведомления", "Notifications")
    );
    if (notificationCount) {
        notificationCount.hidden = count === 0;
        notificationCount.textContent = count > 99 ? "99+" : String(count);
    }
    if (notificationSummary) {
        notificationSummary.textContent = count
            ? localized(`Непрочитанных: ${count}`, `Unread: ${count}`)
            : localized("Новых событий нет", "No new events");
    }

    notificationList.replaceChildren();
    if (count === 0) {
        const empty = document.createElement("p");
        empty.className = "notification-empty";
        empty.textContent = localized(
            "Здесь появятся новые события Gateway.",
            "New Gateway events will appear here."
        );
        notificationList.appendChild(empty);
        return;
    }

    for (const event of unreadNotifications) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "notification-item";
        const title = document.createElement("span");
        title.className = "notification-item-title";
        title.textContent = `${event.device_id || localized("Устройство", "Device")} · ${eventKindLabel(event)}`;
        const description = document.createElement("span");
        description.className = "notification-item-description";
        description.textContent = getEventDescription(event);
        const timestamp = document.createElement("span");
        timestamp.className = "notification-item-time";
        timestamp.textContent = formatEventTime(event.timestamp);
        item.append(title, description, timestamp);
        item.addEventListener("click", () => openNotificationEvent(event));
        notificationList.appendChild(item);
    }
}

function closeNotificationPanel() {
    if (!notificationPanel || !notificationButton) return;
    notificationPanel.hidden = true;
    notificationButton.setAttribute("aria-expanded", "false");
}

function markNotificationRead(eventId) {
    const id = String(eventId);
    unreadNotifications = unreadNotifications.filter(item => String(item.id) !== id);
    readNotificationIds.add(id);
    readNotificationIds = new Set([...readNotificationIds].slice(-500));
    saveNotificationState();
    renderNotifications();
}

function markAllNotificationsRead() {
    for (const event of events) {
        observedMaxEventId = Math.max(observedMaxEventId, Number(event.id) || 0);
    }
    for (const event of unreadNotifications) {
        observedMaxEventId = Math.max(observedMaxEventId, Number(event.id) || 0);
    }
    if (notificationStateReady || observedMaxEventId > 0) {
        lastReadEventId = Math.max(lastReadEventId || 0, observedMaxEventId);
    }
    unreadNotifications = [];
    readNotificationIds.clear();
    saveNotificationState();
    renderNotifications();
}

function openNotificationEvent(event) {
    markNotificationRead(event.id);
    closeNotificationPanel();
    eventDeviceFilter = "all";
    eventFilter = ["led", "parameter"].includes(event.event_type)
        ? "state"
        : ["connection", "schema", "command"].includes(event.event_type)
            ? event.event_type
            : "all";
    pendingEventFocusId = String(event.id);
    setActivePage("events");
    if (events.some(item => String(item.id) === pendingEventFocusId)) {
        renderEvents();
        focusPendingEvent();
    } else {
        requestEvents({ reset: true });
    }
}

function focusPendingEvent() {
    if (!pendingEventFocusId || eventsLoading) return;
    const row = [...(activityCard?.querySelectorAll("[data-event-id]") || [])]
        .find(item => item.dataset.eventId === pendingEventFocusId);
    if (row) {
        row.classList.add("event-focus");
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        pendingEventFocusId = null;
        return;
    }
    if (eventsHasMore && isGatewayConnected()) {
        requestEvents();
    } else {
        pendingEventFocusId = null;
    }
}

function initializeInterface() {
    if (settingNavigation) settingNavigation.value = uiSettings.navigation;
    if (settingLanguage) settingLanguage.value = uiSettings.language;
    if (settingTimezone) {
        settingTimezone.value = [...settingTimezone.options].some(option => option.value === uiSettings.timezone)
            ? uiSettings.timezone
            : "local";
        uiSettings.timezone = settingTimezone.value;
    }
    if (settingImportantOnly) settingImportantOnly.checked = uiSettings.importantOnly;
    document.body.classList.toggle("nav-side", uiSettings.navigation === "side");
    navigation?.setAttribute("aria-orientation", uiSettings.navigation === "side" ? "vertical" : "horizontal");
    applyLanguage();
    setActivePage(activePage);

    navigation?.addEventListener("click", event => {
        const button = event.target.closest("[data-page]");
        if (button) setActivePage(button.dataset.page);
    });
    automationForm?.addEventListener("submit", submitAutomation);
    document.addEventListener("automation-history-change", event => {
        if (automationUndoButton) automationUndoButton.disabled = !event.detail?.canUndo;
        if (automationRedoButton) automationRedoButton.disabled = !event.detail?.canRedo;
    });
    automationUndoButton?.addEventListener("click", () => window.AutomationBlockly?.undo());
    automationRedoButton?.addEventListener("click", () => window.AutomationBlockly?.redo());
    automationCancelEdit?.addEventListener("click", () => {
        editingAutomationId = null;
        automationForm?.reset();
        window.AutomationBlockly?.clear();
        document.getElementById("automation-editor-title").textContent = localized("Новый сценарий", "New scenario");
        updateAutomationEditorMode();
        if (automationFeedback) automationFeedback.textContent = "";
    });
    automationClearButton?.addEventListener("click", () => window.AutomationBlockly?.clear());
    document.querySelector(".brand")?.addEventListener("click", event => {
        event.preventDefault();
        setActivePage("devices");
    });
    navigation?.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
        const buttons = [...navigation.querySelectorAll("[data-page]")];
        const currentIndex = buttons.findIndex(button => button.classList.contains("is-active"));
        const delta = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
        buttons[(currentIndex + delta + buttons.length) % buttons.length]?.focus();
        event.preventDefault();
    });
    for (const button of document.querySelectorAll('[data-action="refresh-events"]')) {
        button.addEventListener("click", () => requestEvents({ reset: true }));
    }
    settingNavigation?.addEventListener("change", () => {
        uiSettings.navigation = settingNavigation.value === "side" ? "side" : "top";
        document.body.classList.toggle("nav-side", uiSettings.navigation === "side");
        navigation?.setAttribute("aria-orientation", uiSettings.navigation === "side" ? "vertical" : "horizontal");
        saveUiSettings();
        showSettingsSaved();
    });
    settingLanguage?.addEventListener("change", () => {
        uiSettings.language = settingLanguage.value === "en" ? "en" : "ru";
        saveUiSettings();
        applyLanguage();
        showSettingsSaved();
    });
    settingTimezone?.addEventListener("change", () => {
        uiSettings.timezone = settingTimezone.value;
        saveUiSettings();
        renderEvents();
        renderNotifications();
        showSettingsSaved();
    });
    settingImportantOnly?.addEventListener("change", () => {
        uiSettings.importantOnly = settingImportantOnly.checked;
        if (uiSettings.importantOnly) {
            for (const event of unreadNotifications) {
                if (!isImportantNotification(event)) {
                    readNotificationIds.add(String(event.id));
                }
            }
            unreadNotifications = unreadNotifications.filter(isImportantNotification);
            renderNotifications();
            saveNotificationState();
        }
        saveUiSettings();
        showSettingsSaved();
    });
    notificationButton?.addEventListener("click", () => {
        const open = notificationPanel.hidden;
        notificationPanel.hidden = !open;
        notificationButton.setAttribute("aria-expanded", String(open));
        if (open) renderNotifications();
    });
    markNotificationsReadButton?.addEventListener("click", markAllNotificationsRead);
    document.addEventListener("click", event => {
        if (!event.target.closest(".notification-area")) closeNotificationPanel();
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") closeNotificationPanel();
    });
}

let settingsSavedTimer = null;
function showSettingsSaved() {
    if (!settingsSavedMessage) return;
    settingsSavedMessage.textContent = localized("Настройки сохранены", "Settings saved");
    clearTimeout(settingsSavedTimer);
    settingsSavedTimer = setTimeout(() => {
        settingsSavedMessage.textContent = "";
    }, 1800);
}

// =========================
// СТАТУС ПОДКЛЮЧЕНИЯ
// =========================

function setActivity(message) {
    message = typeof message === "string" ? message : String(message ?? "");
    lastActivityMessage = message;
    const displayedMessage = localizeActivityMessage(message);
    if (activityMessage) {
        activityMessage.textContent = displayedMessage;
    }
    if (actionFeedback) {
        actionFeedback.textContent = displayedMessage;
        actionFeedback.hidden = !displayedMessage;
    }
}

function setGatewayConnectionStatus(message, connected) {
    const wasConnected = gatewayIsConnectedState;
    lastGatewayStatusMessage = message;
    gatewayIsConnectedState = connected;
    if (connected && !wasConnected) {
        const now = performance.now() / 1000;
        if (databaseStats) {
            const previousElapsed = databaseStatsFrozenElapsed ?? Math.max(
                0,
                now - databaseStatsReceivedAt
            );
            databaseStatsReceivedAt = now - previousElapsed;
        }
        databaseStatsFrozenElapsed = null;
    } else if (!connected && wasConnected && databaseStats) {
        databaseStatsFrozenElapsed = Math.max(
            0,
            performance.now() / 1000 - databaseStatsReceivedAt
        );
    }
    const englishStatuses = {
        "Нет соединения с Gateway": "Gateway disconnected",
        "Подключение к Gateway...": "Connecting to Gateway...",
        "Gateway подключён": "Gateway connected",
        "Ошибка соединения с Gateway": "Gateway connection error"
    };
    if (gatewayStatus) {
        gatewayStatus.textContent = uiSettings.language === "en"
            ? (englishStatuses[message] || message)
            : message;
    }
    if (gatewayStatusDot) {
        gatewayStatusDot.classList.toggle("connected", connected);
        gatewayStatusDot.classList.toggle("disconnected", !connected);
    }
}

function clearPendingCommand(commandId) {
    const pending = pendingCommands.get(commandId);

    if (pending) {
        pendingCommands.delete(commandId);
        const device = devices.find(item => item.device === pending.deviceId);
        if (device) updateDevicePanel(device);
    }
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

function sendGatewayAction(action, fields = {}) {
    if (!isGatewayConnected()) {
        setActivity("Нет соединения с Gateway.");
        return false;
    }
    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action,
            ...fields
        }));
        return true;
    } catch (error) {
        console.error("Не удалось отправить действие Gateway:", error);
        setActivity("Не удалось отправить запрос в Gateway.");
        return false;
    }
}

function automationActionCount(statements = []) {
    let count = 0;
    for (const statement of statements || []) {
        if (["set_parameter", "invoke_action", "wait"].includes(statement.type)) count += 1;
        if (statement.type === "if") {
            for (const branch of statement.branches || []) count += automationActionCount(branch.then);
            count += automationActionCount(statement.else);
        }
    }
    return count;
}

function automationSummary(config) {
    if (config.trigger && config.target) {
        const source = devices.find(item => item.device === config.trigger.device);
        const target = devices.find(item => item.device === config.target.device);
        const sensor = source?.schema?.parameters?.[config.trigger.parameter];
        const operation = config.target.kind === "action"
            ? target?.schema?.actions?.[config.target.name]?.label || config.target.name
            : target?.schema?.parameters?.[config.target.name]?.label || config.target.name;
        const words = { below: "меньше", above: "больше", at_most: "не больше", at_least: "не меньше" };
        const opEn = { below: "below", above: "above", at_most: "at most", at_least: "at least" };
        return localized(
            `${source?.name || source?.device || config.trigger.device} · ${sensor?.label || config.trigger.parameter} ${words[config.trigger.operator] || config.trigger.operator} ${config.trigger.threshold} → ${target?.name || target?.device || config.target.device} · ${operation}`,
            `${source?.name || source?.device || config.trigger.device} · ${sensor?.label || config.trigger.parameter} ${opEn[config.trigger.operator] || config.trigger.operator} ${config.trigger.threshold} → ${target?.name || target?.device || config.target.device} · ${operation}`
        );
    }
    const program = config.program;
    if (!program?.trigger) return localized("Сценарий требует проверки.", "Scenario needs review.");
    const trigger = program.trigger;
    let triggerText;
    if (trigger.type === "parameter_changed") {
        const source = devices.find(item => item.device === trigger.device);
        const definition = source?.schema?.parameters?.[trigger.parameter];
        triggerText = localized(
            `При изменении ${source?.name || trigger.device} · ${definition?.label || trigger.parameter}`,
            `When ${source?.name || trigger.device} · ${definition?.label || trigger.parameter} changes`
        );
    } else if (trigger.type === "device_connected") {
        triggerText = localized(`При подключении ${trigger.device}`, `When ${trigger.device} connects`);
    } else if (trigger.type === "device_disconnected") {
        triggerText = localized(`При отключении ${trigger.device}`, `When ${trigger.device} disconnects`);
    } else {
        triggerText = localized("При запуске Gateway", "When Gateway starts");
    }
    return `${triggerText} → ${automationActionCount(program.body)} ${localized("действий", "actions")}`;
}

function refreshAutomationEditor(config = null) {
    const BlocklyEditor = window.AutomationBlockly;
    if (config) {
        editingAutomationId = config.automation_id || editingAutomationId;
        document.getElementById("automation-name").value = config.name || "";
        document.getElementById("automation-editor-title").textContent = localized("Изменить сценарий", "Edit scenario");
        pendingAutomationWorkspaceState = config.workspace || null;
        setActivePage("automations");
        if (BlocklyEditor?.isReady()) {
            try {
                if (config.trigger && config.target) BlocklyEditor.loadLegacy(config);
                else BlocklyEditor.load(config.workspace || null);
            } catch (error) {
                editingAutomationId = null;
                pendingAutomationWorkspaceState = null;
                automationFeedback.textContent = error.message || localized("Не удалось открыть сценарий.", "Could not open the scenario.");
                updateAutomationEditorMode();
                return;
            }
            pendingAutomationWorkspaceState = null;
        }
        updateAutomationEditorMode();
        return;
    }
    if (activePage !== "automations" || !BlocklyEditor) return;
    if (!BlocklyEditor.isReady()) {
        BlocklyEditor.initialize(
            "automation-workspace", devices, uiSettings.language, pendingAutomationWorkspaceState
        );
        pendingAutomationWorkspaceState = null;
    } else {
        BlocklyEditor.update(devices, uiSettings.language);
    }
    updateAutomationEditorMode();
}

function renderAutomations() {
    if (!automationList) return;
    automationList.replaceChildren();
    if (automations.length === 0) {
        const empty = document.createElement("p");
        empty.className = "automation-empty";
        empty.textContent = localized("Сценариев пока нет. Перетащите блоки в редактор выше.", "No scenarios yet. Drag blocks into the editor above.");
        automationList.append(empty);
        return;
    }
    for (const item of automations) {
        const card = document.createElement("article");
        card.className = `automation-card${item.enabled ? "" : " is-disabled"}`;
        const copy = document.createElement("div");
        copy.className = "automation-card-copy";
        const title = document.createElement("h3");
        title.textContent = item.config.name || localized("Без названия", "Untitled scenario");
        const description = document.createElement("p");
        description.textContent = automationSummary(item.config);
        const status = document.createElement("small");
        const runtimeStatus = item.run_status || "idle";
        if (runtimeStatus === "running") {
            status.textContent = localized("Выполняется…", "Running…");
        } else if (item.last_error) {
            status.textContent = `${localized("Ошибка", "Error")}: ${item.last_error}`;
            status.classList.add("automation-status-error");
        } else if (item.last_triggered_at) {
            const runLabels = {
                succeeded: localized("успешно", "succeeded"),
                failed: localized("ошибка", "failed"),
                stopped: localized("остановлен", "stopped")
            };
            status.textContent = localized(
                `Последний запуск: ${formatEventTime(item.last_triggered_at)}${item.last_run_status ? ` · ${runLabels[item.last_run_status] || item.last_run_status}` : ""}`,
                `Last run: ${formatEventTime(item.last_triggered_at)}${item.last_run_status ? ` · ${runLabels[item.last_run_status] || item.last_run_status}` : ""}`
            );
        } else {
            status.textContent = localized("Ожидает событие запуска", "Waiting for its trigger");
        }
        copy.append(title, description, status);
        const actions = document.createElement("div");
        actions.className = "automation-card-actions";
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "event-action-button";
        toggle.textContent = item.enabled ? localized("Отключить", "Disable") : localized("Включить", "Enable");
        toggle.addEventListener("click", () => sendGatewayAction("set_automation_enabled", {
            automation_id: item.automation_id, enabled: !item.enabled
        }));
        if (runtimeStatus === "running") {
            const stop = document.createElement("button");
            stop.type = "button";
            stop.className = "event-action-button";
            stop.textContent = localized("Остановить", "Stop");
            stop.addEventListener("click", () => sendGatewayAction("stop_automation", {
                automation_id: item.automation_id
            }));
            actions.append(stop);
        }
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "event-action-button";
        edit.textContent = localized("Изменить", "Edit");
        edit.addEventListener("click", () => refreshAutomationEditor({
            ...item.config, automation_id: item.automation_id
        }));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "event-action-button";
        remove.textContent = localized("Удалить", "Delete");
        remove.addEventListener("click", () => {
            if (window.confirm(localized(`Удалить сценарий «${item.config.name}»?`, `Delete scenario “${item.config.name}”?`))) {
                sendGatewayAction("delete_automation", { automation_id: item.automation_id });
            }
        });
        actions.append(toggle, edit, remove);
        card.append(copy, actions);
        automationList.append(card);
    }
}

function submitAutomation(event) {
    event.preventDefault();
    let program;
    try {
        program = window.AutomationBlockly?.compile();
        if (!program) throw new Error(localized("Редактор блоков пока не готов.", "The block editor is not ready."));
    } catch (error) {
        automationFeedback.textContent = error.message || localized("Проверьте структуру блоков.", "Check the block structure.");
        return;
    }
    const name = document.getElementById("automation-name").value.trim();
    if (!name) {
        automationFeedback.textContent = localized("Введите название сценария.", "Enter a scenario name.");
        return;
    }
    const config = {
        version: 2,
        name,
        workspace: window.AutomationBlockly.getState(),
        program
    };
    const existing = automations.find(item => item.automation_id === editingAutomationId);
    if (sendGatewayAction("save_automation", {
        automation_id: editingAutomationId,
        enabled: existing?.enabled ?? true,
        config
    })) {
        automationFeedback.textContent = localized("Проверяю и сохраняю…", "Validating and saving…");
    }
}

function updateAutomationEditorMode() {
    if (automationCancelEdit) automationCancelEdit.hidden = !editingAutomationId;
    const title = document.getElementById("automation-editor-title");
    if (title) title.textContent = editingAutomationId
        ? localized("Изменить сценарий", "Edit scenario")
        : localized("Новый сценарий", "New scenario");
    if (automationSaveButton) {
        automationSaveButton.textContent = editingAutomationId
            ? localized("Сохранить изменения", "Save changes")
            : localized("Сохранить сценарий", "Save scenario");
    }
}

function requestDeviceSchemaHistory(deviceId) {
    if (!isGatewayConnected() || schemaHistoryRequests.has(deviceId)) return;
    schemaHistoryRequests.add(deviceId);
    if (!sendGatewayAction("get_schema_history", {
        device: deviceId,
        request_id: createCommandId()
    })) {
        schemaHistoryRequests.delete(deviceId);
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
    setGatewayConnectionStatus("Подключение к Gateway...", false);

    const host = window.location.hostname || "localhost";

    socket = new WebSocket(`ws://${host}:8765`);

    socket.addEventListener("open", () => {
        setGatewayConnectionStatus("Gateway подключён", true);
        setActivity("Подключено к Gateway.");
        for (const device of devices) updateDevicePanel(device);
        renderBlockedDevices();

        socket.send(JSON.stringify({
            type: "browser_hello"
        }));

        requestDevices();
        requestEvents({ reset: true });
        sendGatewayAction("get_automations");
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
        schemaHistoryRequests.clear();
        notificationBackfillActive = null;
        for (const device of devices) updateDevicePanel(device);
        renderBlockedDevices();

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
        case "automation_list": {
            automations = Array.isArray(message.automations) ? message.automations : [];
            renderAutomations();
            break;
        }

        case "automation_save_result":
        case "automation_update_result":
        case "automation_delete_result": {
            automationFeedback.textContent = message.success
                ? localized("Готово.", "Done.")
                : (message.message || localized("Не удалось изменить сценарий.", "Could not update the scenario."));
            if (message.success && message.type === "automation_delete_result" && message.automation_id === editingAutomationId) {
                editingAutomationId = null;
                automationForm?.reset();
                window.AutomationBlockly?.clear();
                updateAutomationEditorMode();
            }
            if (message.success && message.type === "automation_save_result") {
                editingAutomationId = null;
                automationForm?.reset();
                window.AutomationBlockly?.clear();
                document.getElementById("automation-editor-title").textContent = localized("Новый сценарий", "New scenario");
                refreshAutomationEditor();
                updateAutomationEditorMode();
            }
            break;
        }

        case "automation_stop_result":
            automationFeedback.textContent = message.message || localized("Запрос остановки обработан.", "Stop request processed.");
            break;

        case "device_access_requests": {
            pendingDeviceAccessRequests = Array.isArray(message.requests)
                ? message.requests
                : [];
            renderDeviceAccessDialog();
            break;
        }

        case "blocked_devices": {
            blockedDevices = Array.isArray(message.devices)
                ? message.devices
                : [];
            renderBlockedDevices();
            renderEvents();
            break;
        }

        case "device_access_decision_result":
        case "device_forget_result":
        case "blocked_device_result":
        case "schema_restore_result":
        case "schema_history_delete_result": {
            setActivity(message.message || "Запрос обработан.");
            if (message.type === "schema_restore_result" && message.success) {
                schemaHistoryByDevice.delete(message.device);
                requestDevices();
                requestDeviceSchemaHistory(message.device);
            }
            if (message.type === "schema_history_delete_result" && message.success) {
                schemaHistoryByDevice.delete(message.device);
                schemaHistoryRequests.delete(message.device);
                requestDeviceSchemaHistory(message.device);
            }
            break;
        }

        case "schema_history": {
            if (typeof message.device === "string") {
                schemaHistoryRequests.delete(message.device);
                schemaHistoryByDevice.set(
                    message.device,
                    Array.isArray(message.history) ? message.history : []
                );
                renderDevices();
            }
            break;
        }

        case "command_queue": {
            const incoming = Array.isArray(message.commands)
                ? message.commands
                : [];
            const activeIds = new Set(incoming.map(item => item.command_id));
            for (const commandId of pendingCommands.keys()) {
                if (!activeIds.has(commandId)) pendingCommands.delete(commandId);
            }
            for (const item of incoming) {
                const operation = item.operation || {};
                pendingCommands.set(item.command_id, {
                    commandId: item.command_id,
                    deviceId: item.device,
                    parameter: operation.kind === "parameter" ? operation.name : null,
                    value: operation.value,
                    actionId: operation.kind === "action" ? operation.name : null,
                    arguments: operation.arguments || {},
                    status: item.status,
                    attempts: item.attempts || 0
                });
            }
            renderDevices();
            break;
        }

        case "database_stats": {
            const statKeys = [
                "database_size_bytes",
                "record_count",
                "device_count",
                "ever_connected_device_count",
                "connected_device_count",
                "event_count",
                "schema_snapshot_count",
                "command_record_count",
                "automation_count",
                "session_uptime_seconds",
                "total_uptime_seconds"
            ];
            const receivedStats = {};
            for (const key of statKeys) {
                const value = Number(message[key]);
                if (!Number.isFinite(value) || value < 0) break;
                receivedStats[key] = value;
            }
            if (Object.keys(receivedStats).length === statKeys.length) {
                databaseStats = receivedStats;
                databaseStatsReceivedAt = performance.now() / 1000;
                databaseStatsFrozenElapsed = gatewayIsConnectedState ? null : 0;
                renderDatabaseStats();
            }
            break;
        }

        case "device_list": {
            devices = Array.isArray(message.devices)
                ? message.devices
                : [];

            renderDevices();
            renderEvents();
            refreshAutomationEditor();
            renderAutomations();

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

        case "device_identity_result": {
            if (message.success && deviceIdentityDialog) {
                deviceIdentityDialog.close();
                deviceIdentityDialog = null;
            } else {
                const saveButton = deviceIdentityDialog?.querySelector('[data-role="identity-save"]');
                if (saveButton) saveButton.disabled = false;
                const feedback = deviceIdentityDialog?.querySelector('[data-role="identity-feedback"]');
                if (feedback) {
                    feedback.textContent = localizeActivityMessage(message.message || "Не удалось изменить ID устройства.");
                    feedback.hidden = false;
                }
            }
            if (message.success) {
                if (message.device !== message.new_device_id) {
                    window.AutomationBlockly?.replaceDeviceId(message.device, message.new_device_id);
                    selectedDeviceModes.delete(message.device);
                    if (eventDeviceFilter === message.device) eventDeviceFilter = message.new_device_id;
                    for (const event of unreadNotifications) {
                        if (event.device_id === message.device) event.device_id = message.new_device_id;
                    }
                    saveNotificationState();
                    for (const pending of pendingCommands.values()) {
                        if (pending.deviceId === message.device) pending.deviceId = message.new_device_id;
                    }
                    schemaHistoryByDevice.delete(message.device);
                    if (openSchemaHistoryDevices.delete(message.device)) {
                        openSchemaHistoryDevices.add(message.new_device_id);
                    }
                }
            }
            setActivity(message.message || localized("Идентификаторы устройства обновлены.", "Device identity updated."));
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
            if (
                typeof message.request_id === "string" &&
                message.request_id.startsWith("notification-backfill-")
            ) {
                if (notificationBackfillActive !== message.request_id) break;
                notificationBackfillActive = null;
                observeEventNotifications(incomingEvents);
                if (
                    message.has_more === true &&
                    Number.isInteger(message.next_before_id) &&
                    lastReadEventId !== null &&
                    message.next_before_id > lastReadEventId
                ) {
                    requestNotificationBackfill(message.next_before_id);
                }
                break;
            }
            let initializeNotifications = false;
            let shouldBackfillNotifications = false;

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
                    initializeNotifications = !notificationStateReady;
                    shouldBackfillNotifications = activeEventRequest.deviceFilter === "all";
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

            observeEventNotifications(incomingEvents, initializeNotifications);
            if (
                shouldBackfillNotifications &&
                message.has_more === true &&
                Number.isInteger(message.next_before_id) &&
                lastReadEventId !== null &&
                message.next_before_id > lastReadEventId
            ) {
                requestNotificationBackfill(message.next_before_id);
            }
            renderEvents();
            focusPendingEvent();
            break;
        }

        case "command_sent": {
            const pending = pendingCommands.get(message.command_id);

            if (pending && pending.deviceId === message.device) {
                pending.status = "sent";
                pending.attempts = message.attempts || pending.attempts || 0;
                const device = devices.find(item => item.device === pending.deviceId);
                if (device) updateDevicePanel(device);
                setActivity(
                    `Команда отправлена ${message.device}. ` +
                    "Ожидаем результат выполнения..."
                );
            }

            break;
        }

        case "command_queued": {
            const pending = pendingCommands.get(message.command_id);
            if (pending) {
                pending.status = "queued";
                pending.attempts = message.attempts || pending.attempts || 0;
                const device = devices.find(item => item.device === pending.deviceId);
                if (device) updateDevicePanel(device);
            }
            setActivity(
                `Команда для ${message.device} сохранена в очереди и будет повторена при подключении устройства.`
            );
            break;
        }

        case "schema_decision_result": {
            pendingSchemaDecisions.delete(
                `${message.device}\u0000${message.proposal_id}`
            );
            if (message.success) {
                schemaHistoryByDevice.delete(message.device);
                if (openSchemaHistoryDevices.has(message.device)) {
                    requestDeviceSchemaHistory(message.device);
                }
            }
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
        ["actions", "Действие"],
        ["modes", "Режим"]
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

function modeActionIds(device) {
    const ids = new Set();
    for (const mode of Object.values(device.schema?.modes || {})) {
        for (const actionId of Object.values(mode.operations || {})) ids.add(actionId);
    }
    return ids;
}

function createDeviceModeControls(device) {
    const modes = device.schema?.modes || {};
    const entries = Object.entries(modes);
    const section = document.createElement("section");
    section.className = "device-modes";
    section.dataset.role = "device-modes";

    const heading = document.createElement("strong");
    heading.className = "device-modes-heading";
    heading.textContent = localized("Режим работы", "Operating mode");

    const label = document.createElement("label");
    label.className = "device-mode-select-wrap";
    const labelText = document.createElement("span");
    labelText.textContent = localized("Сценарий устройства", "Device mode");
    const select = document.createElement("select");
    select.className = "device-mode-select";
    select.dataset.role = "mode-select";
    label.append(labelText, select);

    const actions = document.createElement("div");
    actions.className = "device-mode-actions";
    actions.dataset.role = "mode-actions";

    const status = document.createElement("p");
    status.className = "device-mode-status";
    status.dataset.role = "mode-status";

    if (!entries.length) {
        select.disabled = true;
        select.setAttribute("aria-label", localized("Нет объявленных режимов", "No registered modes"));
        status.textContent = localized("У этого устройства нет зарегистрированных режимов.", "This device has no registered modes.");
        section.append(heading, label, actions, status);
        return section;
    }

    for (const [modeId, definition] of entries) {
        const option = document.createElement("option");
        option.value = modeId;
        option.textContent = definition.label || modeId;
        select.append(option);
    }
    const storedMode = selectedDeviceModes.get(device.device);
    const selectedModeId = modes[storedMode] ? storedMode : entries[0][0];
    selectedDeviceModes.set(device.device, selectedModeId);
    select.value = selectedModeId;
    select.addEventListener("change", () => {
        selectedDeviceModes.set(device.device, select.value);
        updateDeviceModePanel(device);
    });

    for (const [modeId, mode] of entries) {
        for (const [operation, actionId] of Object.entries(mode.operations || {})) {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.role = "mode-operation";
            button.dataset.modeId = modeId;
            button.dataset.modeOperation = operation;
            button.dataset.actionId = actionId;
            const label = {
                select: localized("Выбрать режим", "Select mode"),
                enable: localized("Включить", "Enable"),
                disable: localized("Выключить", "Disable")
            }[operation] || operation;
            button.dataset.baseLabel = label;
            button.textContent = label;
            button.title = mode.description || "";
            button.addEventListener("click", () => {
                const action = device.schema?.actions?.[actionId];
                if (!action) return;
                if (Object.keys(action.arguments || {}).length > 0) {
                    openActionDialog(device.device, actionId, action);
                } else {
                    sendDeviceAction(device.device, actionId);
                }
            });
            actions.append(button);
        }
    }

    section.append(heading, label, actions, status);
    return section;
}

function updateDeviceModePanel(device) {
    const panel = [...(deviceCard?.querySelectorAll(".device-panel") || [])]
        .find(item => item.dataset.deviceId === device.device);
    const section = panel?.querySelector('[data-role="device-modes"]');
    const select = section?.querySelector('[data-role="mode-select"]');
    if (!section || !select) return;

    const modes = device.schema?.modes || {};
    const entries = Object.entries(modes);
    if (!entries.length) {
        select.disabled = true;
        return;
    }

    let selectedModeId = selectedDeviceModes.get(device.device);
    if (!modes[selectedModeId]) {
        selectedModeId = entries[0][0];
        selectedDeviceModes.set(device.device, selectedModeId);
    }
    select.value = selectedModeId;
    select.disabled = !device.connected || !isGatewayConnected() || Boolean(device.pending_schema);

    const definition = modes[selectedModeId];
    const status = section.querySelector('[data-role="mode-status"]');
    if (definition.status_parameter && status) {
        const currentValue = device.parameters?.[definition.status_parameter];
        status.textContent = currentValue === undefined
            ? localized("Состояние режима: неизвестно", "Mode status: unknown")
            : Object.is(currentValue, definition.status_value)
                ? localized(`Активен режим «${definition.label || selectedModeId}»`, `Mode “${definition.label || selectedModeId}” is active`)
                : localized(`Режим «${definition.label || selectedModeId}» не активен`, `Mode “${definition.label || selectedModeId}” is inactive`);
        status.hidden = false;
    } else if (status) {
        status.hidden = false;
        status.textContent = definition.description || "";
    }

    for (const button of section.querySelectorAll('[data-role="mode-operation"]')) {
        const visible = button.dataset.modeId === selectedModeId;
        button.hidden = !visible;
        if (!visible) continue;
        const actionId = button.dataset.actionId;
        const pending = [...pendingCommands.values()].find(command =>
            command.deviceId === device.device && command.actionId === actionId
        );
        button.textContent = pending
            ? pending.status === "queued"
                ? localized(`В очереди: ${button.dataset.baseLabel}`, `Queued: ${button.dataset.baseLabel}`)
                : localized(`Ожидаем: ${button.dataset.baseLabel}…`, `Waiting: ${button.dataset.baseLabel}…`)
            : button.dataset.baseLabel;
        button.disabled = !device.connected || !isGatewayConnected() || Boolean(device.pending_schema) || Boolean(pending);
    }
}

function createSchemaReview(device) {
    const proposal = device.pending_schema;
    const review = document.createElement("section");
    review.className = "schema-review";

    const heading = document.createElement("h4");
    heading.textContent = localized("Обнаружено изменение Schema", "Schema change detected");

    const version = document.createElement("p");
    const oldVersion = device.schema_version ?? "не указана";
    const newVersion = proposal.schema_version ?? "не указана";
    version.textContent = localized(
        `Сохранённая версия: ${oldVersion} · новая версия: ${newVersion}`,
        `Saved version: ${oldVersion} · new version: ${newVersion}`
    );

    const list = document.createElement("ul");
    for (const change of describeSchemaChanges(device.schema, proposal.schema)) {
        const item = document.createElement("li");
        item.textContent = change;
        list.appendChild(item);
    }

    const note = document.createElement("p");
    note.className = "schema-review-note";
    note.textContent = localized(
        "Пока вы выбираете, действует прежняя Schema и команды заблокированы. Если оставить её, Gateway повторит предложение при следующем подключении устройства.",
        "The previous Schema remains active and commands are paused while you decide. If you keep it, Gateway will ask again the next time the device connects."
    );

    const buttons = document.createElement("div");
    buttons.className = "schema-review-buttons";

    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = localized("Принять Schema", "Accept Schema");
    const decisionKey = `${device.device}\u0000${proposal.proposal_id}`;
    accept.disabled = !isGatewayConnected() || pendingSchemaDecisions.has(decisionKey);
    accept.addEventListener("click", () => {
        sendSchemaDecision(device.device, proposal.proposal_id, "accept");
    });

    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "secondary-button";
    reject.textContent = localized("Оставить текущую Schema", "Keep current Schema");
    reject.disabled = !isGatewayConnected() || pendingSchemaDecisions.has(decisionKey);
    reject.addEventListener("click", () => {
        sendSchemaDecision(device.device, proposal.proposal_id, "reject");
    });

    buttons.append(accept, reject);
    review.append(heading, version, list, note, buttons);
    return review;
}

function renderDeviceAccessDialog() {
    const request = pendingDeviceAccessRequests[0];
    if (!request) {
        if (deviceAccessDialog) {
            if (deviceAccessDialog.open) deviceAccessDialog.close();
            deviceAccessDialog.remove();
            deviceAccessDialog = null;
            deviceAccessDialogRequestId = null;
        }
        return;
    }

    if (
        deviceAccessDialog &&
        deviceAccessDialogRequestId === request.request_id &&
        deviceAccessDialog.isConnected
    ) {
        for (const button of deviceAccessDialog.querySelectorAll("button")) {
            button.disabled = !isGatewayConnected();
        }
        return;
    }

    if (deviceAccessDialog) {
        if (deviceAccessDialog.open) deviceAccessDialog.close();
        deviceAccessDialog.remove();
    }

    const dialog = document.createElement("dialog");
    dialog.className = "device-access-dialog";
    const form = document.createElement("form");
    form.className = "device-access-form";
    form.method = "dialog";
    form.addEventListener("submit", event => event.preventDefault());

    const heading = document.createElement("h2");
    heading.textContent = localized("Разрешить подключение устройства?", "Allow this device to connect?");

    const intro = document.createElement("p");
    intro.textContent = localized(
        "Новое устройство сообщает Gateway свой идентификатор и Schema.",
        "A new device is sharing its ID and Schema with Gateway."
    );

    const details = document.createElement("div");
    details.className = "device-access-details";
    const name = document.createElement("strong");
    name.textContent = request.name || localized("Устройство без имени", "Unnamed device");
    const identity = document.createElement("span");
    identity.textContent = `ID: ${request.device}`;
    details.append(name, identity);
    if (request.firmware_version) {
        const firmware = document.createElement("span");
        firmware.textContent = localized(
            `Прошивка: ${request.firmware_version}`,
            `Firmware: ${request.firmware_version}`
        );
        details.appendChild(firmware);
    }

    const rememberLabel = document.createElement("label");
    rememberLabel.className = "remember-device-choice";
    const remember = document.createElement("input");
    remember.type = "checkbox";
    const rememberText = document.createElement("span");
    rememberText.textContent = localized(
        "Запомнить выбор для этого устройства",
        "Remember this choice for this device"
    );
    rememberLabel.append(remember, rememberText);

    const buttons = document.createElement("div");
    buttons.className = "device-access-buttons";
    const deny = document.createElement("button");
    deny.type = "button";
    deny.className = "device-access-deny";
    deny.textContent = localized("Отклонить", "Deny");
    const allow = document.createElement("button");
    allow.type = "button";
    allow.textContent = localized("Подключить", "Allow");

    const submitDecision = (decision) => {
        if (!sendGatewayAction("device_access_decision", {
            device: request.device,
            request_id: request.request_id,
            decision,
            remember: remember.checked
        })) return;
        deny.disabled = true;
        allow.disabled = true;
    };
    deny.addEventListener("click", () => submitDecision("deny"));
    allow.addEventListener("click", () => submitDecision("allow"));
    dialog.addEventListener("cancel", event => event.preventDefault());

    buttons.append(deny, allow);
    form.append(heading, intro, details, rememberLabel, buttons);
    dialog.appendChild(form);
    document.body.appendChild(dialog);
    deviceAccessDialog = dialog;
    deviceAccessDialogRequestId = request.request_id;
    if (typeof dialog.showModal === "function") dialog.showModal();
}

function renderBlockedDevices() {
    if (!blockedDevicesSection) return;
    blockedDevicesSection.hidden = blockedDevices.length === 0;
    if (blockedDeviceCount) blockedDeviceCount.textContent = String(blockedDevices.length);
    if (!blockedDevicesList) return;
    blockedDevicesList.replaceChildren();

    for (const device of blockedDevices) {
        const row = document.createElement("div");
        row.className = "blocked-device";
        const identity = document.createElement("span");
        identity.className = "blocked-device-id";
        identity.textContent = device.name
            ? `${device.name} · ${device.device_id}`
            : device.device_id;
        const unblock = document.createElement("button");
        unblock.type = "button";
        unblock.textContent = localized("Снять блокировку", "Remove block");
        unblock.disabled = !isGatewayConnected();
        unblock.addEventListener("click", () => {
            sendGatewayAction("clear_device_block", { device: device.device_id });
        });
        row.append(identity, unblock);
        blockedDevicesList.appendChild(row);
    }
}

function createSchemaHistory(device) {
    const details = document.createElement("details");
    details.className = "schema-history";
    details.open = openSchemaHistoryDevices.has(device.device);

    const summary = document.createElement("summary");
    summary.textContent = localized("История Schema", "Schema history");
    const content = document.createElement("div");
    content.className = "schema-history-content";
    const history = schemaHistoryByDevice.get(device.device);

    if (!history) {
        const load = document.createElement("button");
        load.type = "button";
        load.dataset.role = "schema-history-load";
        load.textContent = localized("Загрузить историю", "Load history");
        load.disabled = !isGatewayConnected();
        load.addEventListener("click", () => {
            requestDeviceSchemaHistory(device.device);
        });
        content.append(load);
    } else if (history.length === 0) {
        const note = document.createElement("p");
        note.className = "schema-history-note";
        note.textContent = localized(
            "Для устройства пока нет сохранённых снимков Schema.",
            "This device has no saved Schema snapshots yet."
        );
        content.append(note);
    } else {
        const select = document.createElement("select");
        select.dataset.role = "schema-history-select";
        for (const snapshot of history) {
            const option = document.createElement("option");
            option.value = String(snapshot.id);
            const version = snapshot.schema_version ?? localized("без версии", "unversioned");
            const firmware = snapshot.firmware_version
                ? ` · ${snapshot.firmware_version}`
                : "";
            option.textContent = `Schema ${version}${firmware} · ${formatEventTime(snapshot.accepted_at)}`;
            select.appendChild(option);
        }

        const restore = document.createElement("button");
        restore.type = "button";
        restore.dataset.role = "schema-restore";
        restore.textContent = localized("Восстановить для Gateway", "Restore in Gateway");
        restore.disabled = device.connected || !isGatewayConnected();
        restore.addEventListener("click", () => {
            const historyId = Number(select.value);
            if (!Number.isInteger(historyId) || historyId < 1) return;
            if (!window.confirm(
                localized(
                    "Восстановить этот снимок Schema? Устройство должно быть отключено. При следующем подключении Gateway сверит Schema и при расхождении запросит подтверждение.",
                    "Restore this Schema snapshot? The device must be disconnected. Gateway will compare it with the device on its next connection and ask if they differ."
                )
            )) return;
            sendGatewayAction("restore_schema", {
                device: device.device,
                history_id: historyId
            });
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.dataset.role = "schema-delete";
        remove.className = "schema-delete-button";
        remove.textContent = localized("Удалить из истории", "Delete from history");
        remove.disabled = !isGatewayConnected();
        remove.addEventListener("click", () => {
            const historyId = Number(select.value);
            if (!Number.isInteger(historyId) || historyId < 1) return;
            if (!window.confirm(
                localized(
                    "Удалить выбранный снимок Schema из истории? Текущая Schema устройства не изменится.",
                    "Delete this Schema snapshot from history? The device's current Schema will not change."
                )
            )) return;
            sendGatewayAction("delete_schema_history", {
                device: device.device,
                history_id: historyId
            });
        });
        const actions = document.createElement("div");
        actions.className = "schema-history-actions";
        actions.append(restore, remove);
        const note = document.createElement("p");
        note.className = "schema-history-note";
        note.textContent = device.connected
            ? localized("Отключите устройство, чтобы восстановить снимок.", "Disconnect the device to restore a snapshot.")
            : localized(
                "Восстановится только принятая Schema Gateway; само устройство не перепрошивается. Удаление снимка затрагивает только историю.",
                "Only Gateway's accepted Schema is restored; the device is not reflashed. Deleting a snapshot only changes history."
            );
        content.append(select, actions, note);
    }

    details.addEventListener("toggle", () => {
        if (details.open) {
            openSchemaHistoryDevices.add(device.device);
            if (!schemaHistoryByDevice.has(device.device) && isGatewayConnected()) {
                requestDeviceSchemaHistory(device.device);
            }
        } else {
            openSchemaHistoryDevices.delete(device.device);
        }
    });
    details.append(summary, content);
    if (details.open && !history) requestDeviceSchemaHistory(device.device);
    return details;
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
        empty.textContent = localized("Устройства пока не найдены.", "No devices found yet.");

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
        title.textContent = device.display_name || device.name || device.device;

        const subtitle = document.createElement("p");
        subtitle.className = "device-id";
        const schemaNote = device.schema_source === "inferred"
            ? localized("Автоматическая Schema · только чтение", "Inferred Schema · read only")
            : device.schema_source === "missing"
                ? localized("Schema ещё не получена", "Schema not received yet")
                : null;
        subtitle.textContent = [
            device.device,
            device.firmware_version
                ? localized(`Прошивка ${device.firmware_version}`, `Firmware ${device.firmware_version}`)
                : null,
            schemaNote
        ].filter(Boolean).join(" · ");

        titleBlock.append(title, subtitle);

        const badge = document.createElement("span");
        badge.className = "badge device-status";
        badge.textContent = device.connected
            ? localized("Подключено", "Online")
            : localized("Отключено", "Offline");

        const headerActions = document.createElement("div");
        headerActions.className = "device-header-actions";
        const editIdentity = document.createElement("button");
        editIdentity.type = "button";
        editIdentity.className = "device-identity-button";
        editIdentity.textContent = localized("Изменить", "Edit");
        editIdentity.setAttribute("aria-label", localized("Изменить имя и ID", "Edit name and ID"));
        editIdentity.title = localized("Изменить отображаемое имя и ID Gateway", "Edit the display name and Gateway ID");
        editIdentity.addEventListener("click", () => openDeviceIdentityDialog(device));
        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "device-forget-button";
        forget.dataset.role = "forget-device";
        forget.textContent = localized("Забыть", "Forget");
        forget.title = localized(
            "Отключить устройство и запросить разрешение при следующем подключении",
            "Disconnect this device and ask for approval next time it connects"
        );
        forget.addEventListener("click", () => {
            if (!window.confirm(
                localized(
                    `Забыть устройство ${device.device}? При следующем подключении Gateway снова запросит разрешение.`,
                    `Forget device ${device.device}? Gateway will ask for approval again the next time it connects.`
                )
            )) return;
            sendGatewayAction("forget_device", { device: device.device });
        });
        headerActions.append(editIdentity, badge, forget);

        heading.append(icon, titleBlock, headerActions);
        panel.appendChild(heading);

        const divider = document.createElement("div");
        divider.className = "divider";
        panel.appendChild(divider);

        if (device.pending_schema) {
            panel.appendChild(createSchemaReview(device));
        }

        panel.appendChild(createSchemaHistory(device));

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
            currentValue.textContent = formatDisplayedParameterValue(value, definition);

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
                    apply.textContent = localized("Применить", "Apply");
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

        panel.appendChild(createDeviceModeControls(device));

        const actions = device.schema?.actions || {};
        const modeActions = modeActionIds(device);
        const availableActions = Object.entries(actions).filter(([actionId]) => !modeActions.has(actionId));
        if (availableActions.length > 0) {
            const actionList = document.createElement("div");
            actionList.className = "device-actions";
            const actionHeading = document.createElement("strong");
            actionHeading.textContent = localized("Действия устройства", "Device actions");
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
            ? localized(
                `Последнее сообщение: ${device.last_seen || "неизвестно"}`,
                `Last message: ${device.last_seen || "unknown"}`
            )
            : localized("Устройство сейчас не подключено.", "Device is currently offline.");

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
        badge.textContent = device.connected
            ? localized("Подключено", "Online")
            : localized("Отключено", "Offline");
        badge.classList.toggle("is-online", device.connected);
        badge.classList.toggle("is-offline", !device.connected);
    }

    for (const valueNode of panel.querySelectorAll('[data-role="parameter-value"]')) {
        const parameterId = valueNode.dataset.parameterId;
        const definition = device.schema?.parameters?.[parameterId];
        valueNode.textContent = formatDisplayedParameterValue(
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
        const pendingCommand = [...pendingCommands.values()].find(command =>
            command.deviceId === device.device && command.parameter === parameterId
        );
        button.textContent = pending
            ? pendingCommand?.status === "queued"
                ? localized(`В очереди: ${label}`, `Queued: ${label}`)
                : localized(`Ожидаем: ${label}…`, `Waiting: ${label}…`)
            : value === true
                ? localized(`Выключить: ${label}`, `Turn off: ${label}`)
                : localized(`Включить: ${label}`, `Turn on: ${label}`);
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || typeof value !== "boolean" || pending;
    }

    for (const button of panel.querySelectorAll('[data-role="parameter-apply"]')) {
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) ||
            hasPendingParameterCommand(device.device, button.dataset.parameterId);
    }

    for (const button of panel.querySelectorAll('[data-role="device-action"]')) {
        const pendingAction = [...pendingCommands.values()].find(command =>
            command.deviceId === device.device && command.actionId === button.dataset.actionId
        );
        if (pendingAction) {
            const label = device.schema?.actions?.[button.dataset.actionId]?.label || button.dataset.actionId;
            button.textContent = pendingAction.status === "queued"
                ? localized(`В очереди: ${label}`, `Queued: ${label}`)
                : localized(`Ожидаем: ${label}…`, `Waiting: ${label}…`);
        } else {
            button.textContent = device.schema?.actions?.[button.dataset.actionId]?.label || button.dataset.actionId;
        }
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) ||
            hasPendingAction(device.device, button.dataset.actionId);
    }

    updateDeviceModePanel(device);

    const forgetButton = panel.querySelector('[data-role="forget-device"]');
    if (forgetButton) forgetButton.disabled = !isGatewayConnected();
    const restoreButton = panel.querySelector('[data-role="schema-restore"]');
    if (restoreButton) restoreButton.disabled = device.connected || !isGatewayConnected();
    const deleteSchemaButton = panel.querySelector('[data-role="schema-delete"]');
    if (deleteSchemaButton) deleteSchemaButton.disabled = !isGatewayConnected();
    const historyLoadButton = panel.querySelector('[data-role="schema-history-load"]');
    if (historyLoadButton) historyLoadButton.disabled = !isGatewayConnected();

    const schemaDecisionPending = device.pending_schema && pendingSchemaDecisions.has(
        `${device.device}\u0000${device.pending_schema.proposal_id}`
    );
    for (const button of panel.querySelectorAll(".schema-review-buttons button")) {
        button.disabled = !isGatewayConnected() || Boolean(schemaDecisionPending);
    }

    const hint = panel.querySelector('[data-role="device-hint"]');
    if (hint) {
        hint.textContent = device.connected
            ? localized(
                `Последнее сообщение: ${device.last_seen || "неизвестно"}`,
                `Last message: ${device.last_seen || "unknown"}`
            )
            : localized("Устройство сейчас не подключено.", "Device is currently offline.");
    }
    renderOverview();
}

function renderOverview() {
    if (!overview) return;
    const online = devices.filter(device => device.connected).length;
    const metrics = [
        { value: String(devices.length), label: localized("Устройства", "Devices") },
        { value: String(online), label: localized("На связи", "Online") },
        { value: String(Math.max(0, devices.length - online)), label: localized("Не в сети", "Offline") }
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
        return localized("Неизвестно", "Unknown");
    }

    let formatted = value;
    if (definition.type === "json") {
        formatted = JSON.stringify(value);
    } else if (definition.type === "boolean") {
        formatted = value
            ? localized("Включено", "On")
            : localized("Выключено", "Off");
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
        return localized("Время неизвестно", "Time unknown");
    }

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return value;
    }

    const options = {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
    };
    if (uiSettings.timezone !== "local") options.timeZone = uiSettings.timezone;
    return new Intl.DateTimeFormat(uiSettings.language === "en" ? "en-GB" : "ru-RU", options).format(date);
}

function getEventDescription(event) {
    const value = String(event.event_value ?? "");
    const normalized = value.toLowerCase();

    if (uiSettings.language === "en" && event.event_type === "connection") {
        const translations = [
            ["Для устройства сохранён постоянный запрет подключения", "Permanent connection block saved for this device"],
            ["Постоянная блокировка подключения снята", "Permanent connection block removed"],
            ["Устройство забыто; при следующем подключении потребуется подтверждение", "Device forgotten; approval will be requested next time"],
            ["подключено", "Device connected"],
            ["отключено", "Device disconnected"]
        ];
        const match = translations.find(([russian]) => value.toLowerCase().includes(russian.toLowerCase()));
        return match ? match[1] : `Connection: ${value}`;
    }

    if (event.event_type === "command") {
        let command;

        try {
            command = JSON.parse(value);
        } catch (error) {
            return localized(`Результат команды: ${value}`, `Command result: ${value}`);
        }

        const device = devices.find(item => item.device === event.device_id);
        const isAction = typeof command.action === "string";
        const definition = isAction
            ? device?.schema?.actions?.[command.action]
            : device?.schema?.parameters?.[command.parameter];
        const operationLabel = definition?.label || command.action || command.parameter || "Команда";
        const requestedValue = isAction
            ? localized("вызвать", "invoke")
            : typeof command.requested_value === "boolean"
                ? (command.requested_value
                    ? localized("включить", "turn on")
                    : localized("выключить", "turn off"))
                : formatParameterValue(
                    command.requested_value,
                    definition || { type: typeof command.requested_value }
                );
        const statusLabels = uiSettings.language === "en"
            ? {
                succeeded: "completed",
                failed: "failed",
                timeout: "timed out",
                unavailable: "interrupted: device disconnected"
            }
            : {
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
            ? `${localized(", фактически: ", ", actual: ")}${formatParameterValue(
                actualValue,
                definition || { type: typeof actualValue }
            )}`
            : "";
        const errorText = command.error ? ` — ${command.error}` : "";

        const sourceLabel = command.command_id?.startsWith("auto-")
            ? localized("Автоматизация · ", "Automation · ")
            : "";
        return `${sourceLabel}${operationLabel}: ${requestedValue}, ${status}${actualText}${errorText}`;
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
            return localized(`Изменение параметра: ${value}`, `Parameter change: ${value}`);
        }
    }

    if (event.event_type === "schema") {
        if (uiSettings.language === "en") {
            if (value.startsWith("Принята версия Schema ")) {
                return value.replace("Принята версия Schema ", "Accepted Schema version ");
            }
            if (value.startsWith("Оставлена текущая Schema; предложение версии ")) {
                return value
                    .replace("Оставлена текущая Schema; предложение версии ", "Kept current Schema; proposal for version ")
                    .replace(" отклонено", " declined");
            }
            if (value.startsWith("Восстановлена Schema из истории (снимок ")) {
                return value
                    .replace("Восстановлена Schema из истории (снимок ", "Restored Schema from history (snapshot ");
            }
            if (value.startsWith("Удалён снимок Schema из истории (ID ")) {
                return value.replace("Удалён снимок Schema из истории (ID ", "Deleted Schema snapshot from history (ID ");
            }
        }
        return localized(`Обновление Schema: ${value}`, `Schema update: ${value}`);
    }

    if (event.event_type === "led") {
        if (normalized === "true" || normalized === "1") {
            return localized("Светодиод включён", "LED on");
        }

        if (normalized === "false" || normalized === "0") {
            return localized("Светодиод выключен", "LED off");
        }

        return localized(`Состояние LED: ${value}`, `LED state: ${value}`);
    }

    if (event.event_type === "connection") {
        if (normalized === "подключено") {
            return localized("Устройство подключилось", "Device connected");
        }

        if (normalized === "отключено") {
            return localized("Устройство отключилось", "Device disconnected");
        }

        return localized(`Состояние подключения: ${value}`, `Connection status: ${value}`);
    }

    return localized(`${event.event_type}: ${value}`, `${event.event_type}: ${value}`);
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
    heading.textContent = localized("История событий", "Activity history");
    container.appendChild(heading);

    const deviceFilterLabel = document.createElement("label");
    deviceFilterLabel.className = "event-device-filter-group";
    deviceFilterLabel.textContent = localized("Устройство:", "Device:");

    const deviceFilter = document.createElement("select");
    deviceFilter.className = "event-device-filter";

    const allDevicesOption = document.createElement("option");
    allDevicesOption.value = "all";
    allDevicesOption.textContent = localized("Все устройства", "All devices");
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
        { value: "all", label: localized("Все события", "All events") },
        { value: "connection", label: localized("Подключения", "Connections") },
        { value: "command", label: localized("Команды", "Commands") },
        { value: "state", label: localized("Изменения состояния", "State changes") },
        { value: "schema", label: localized("Обновления Schema", "Schema updates") }
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
        if (String(event.id) !== pendingEventFocusId && isCommandEchoEvent(event, events)) {
            continue;
        }

        if (event.event_type === "led") {
            const value = String(event.event_value).toLowerCase();
            const previousValue = lastLedValues.get(event.device_id);

            if (previousValue === value && String(event.id) !== pendingEventFocusId) {
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
    summary.textContent = uiSettings.language === "en"
        ? `Showing ${filteredEvents.length} of ${deduplicatedEvents.length} loaded events`
        : `Показано ${filteredEvents.length} из ${deduplicatedEvents.length} загруженных событий`;

    container.appendChild(summary);

    if (filteredEvents.length === 0) {
        const empty = document.createElement("p");
        empty.className = "event-empty";

        empty.textContent = events.length === 0
            ? localized("Событий пока нет.", "No events yet.")
            : localized("Нет событий для выбранного фильтра.", "No events match this filter.");

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
        row.dataset.eventId = String(event.id);
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
            typeBadge.textContent = localized("Подключение", "Connection");
        } else if (event.event_type === "schema") {
            typeBadge.textContent = localized("Обновление Schema", "Schema update");
        } else if (event.event_type === "parameter") {
            typeBadge.textContent = localized("Изменение состояния", "State change");
        } else if (event.event_type === "led") {
            typeBadge.textContent = localized("Изменение состояния", "State change");
        } else if (event.event_type === "command") {
            typeBadge.textContent = localized("Результат команды", "Command result");
        } else {
            typeBadge.textContent = event.event_type || localized("Событие", "Event");
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

        const isPermanentlyBlocked =
            event.event_type === "connection" &&
            String(event.event_value ?? "").includes("постоянный запрет подключения") &&
            blockedDevices.some(device => device.device_id === event.device_id);
        if (isPermanentlyBlocked) {
            const undo = document.createElement("button");
            undo.type = "button";
            undo.className = "event-action-button";
            undo.textContent = localized("Снять постоянный запрет", "Remove permanent block");
            undo.disabled = !isGatewayConnected();
            undo.addEventListener("click", () => {
                if (sendGatewayAction("clear_device_block", { device: event.device_id })) {
                    undo.disabled = true;
                }
            });
            row.appendChild(undo);
        }
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
        ? localized("Загрузка...", "Loading...")
        : localized("Показать более старые события", "Load older events");
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
        status: "queued",
        attempts: 0
    };

    pendingCommands.set(pending.commandId, pending);
    updateDevicePanel(device);


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

function openDeviceIdentityDialog(device) {
    const dialog = document.createElement("dialog");
    dialog.className = "action-dialog device-identity-dialog";
    const form = document.createElement("form");
    form.className = "action-dialog-form";
    form.addEventListener("submit", event => event.preventDefault());

    const heading = document.createElement("h3");
    heading.textContent = localized("Имя и ID устройства", "Device name and ID");
    form.append(heading);

    const nameLabel = document.createElement("label");
    nameLabel.className = "action-argument";
    nameLabel.textContent = localized("Отображаемое имя", "Display name");
    const nameInput = document.createElement("input");
    nameInput.className = "device-identity-input";
    nameInput.type = "text";
    nameInput.maxLength = 60;
    nameInput.value = device.display_name || "";
    nameInput.placeholder = device.name || localized("Например, Лампа в гостиной", "For example, Living room lamp");
    nameLabel.append(nameInput);
    form.append(nameLabel);

    const idLabel = document.createElement("label");
    idLabel.className = "action-argument";
    idLabel.textContent = localized("ID Gateway (используется в Blockly)", "Gateway ID (used in Blockly)");
    const idInput = document.createElement("input");
    idInput.className = "device-identity-input";
    idInput.type = "text";
    idInput.required = true;
    idInput.maxLength = 64;
    idInput.pattern = "[A-Za-z][A-Za-z0-9_-]{0,63}";
    idInput.autocomplete = "off";
    idInput.autocapitalize = "off";
    idInput.spellcheck = false;
    idInput.value = device.device;
    idInput.disabled = device.connected;
    idInput.addEventListener("input", () => {
        idInput.setCustomValidity("");
        const feedback = dialog.querySelector('[data-role="identity-feedback"]');
        if (feedback) feedback.hidden = true;
    });
    idLabel.append(idInput);
    form.append(idLabel);

    const help = document.createElement("p");
    help.className = "device-identity-help";
    help.textContent = device.connected
        ? localized("Чтобы изменить ID, сначала отключите устройство. Новое имя можно сохранить и сейчас.", "Disconnect the device before changing its ID. You can still save a new name now.")
        : localized("ID уникален, содержит только английские буквы, цифры, дефис и подчёркивание. Blockly использует именно этот ID; Gateway запомнит связь с ID из прошивки.", "The unique ID uses English letters, digits, hyphens, and underscores. Blockly uses this ID; Gateway keeps its link to the firmware ID.");
    form.append(help);

    const feedback = document.createElement("p");
    feedback.className = "device-identity-feedback";
    feedback.dataset.role = "identity-feedback";
    feedback.hidden = true;
    feedback.setAttribute("aria-live", "polite");
    form.append(feedback);

    const buttons = document.createElement("div");
    buttons.className = "action-dialog-buttons";
    const save = document.createElement("button");
    save.type = "button";
    save.dataset.role = "identity-save";
    save.textContent = localized("Сохранить", "Save");
    save.addEventListener("click", () => {
        if (!idInput.checkValidity()) {
            idInput.reportValidity();
            return;
        }
        const nextId = device.connected ? device.device : idInput.value.trim();
        const duplicate = devices.some(item =>
            item.device !== device.device && item.device.toLowerCase() === nextId.toLowerCase()
        );
        if (duplicate) {
            idInput.setCustomValidity(localized("Такой ID уже используется.", "This ID is already in use."));
            idInput.reportValidity();
            return;
        }
        idInput.setCustomValidity("");
        if (sendGatewayAction("update_device_identity", {
            device: device.device,
            new_device_id: nextId,
            display_name: nameInput.value.trim()
        })) {
            save.disabled = true;
        }
    });
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary-button";
    cancel.textContent = localized("Отмена", "Cancel");
    cancel.addEventListener("click", () => dialog.close());
    buttons.append(save, cancel);
    form.append(buttons);
    dialog.append(form);
    dialog.addEventListener("close", () => {
        dialog.remove();
        if (deviceIdentityDialog === dialog) deviceIdentityDialog = null;
    }, { once: true });
    document.body.append(dialog);
    deviceIdentityDialog = dialog;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else {
        dialog.remove();
        deviceIdentityDialog = null;
        setActivity(localized("Браузер не поддерживает диалог изменения устройства.", "This browser does not support the device identity dialog."));
    }
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
    submit.textContent = localized("Выполнить", "Run action");
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
    cancel.textContent = localized("Отмена", "Cancel");
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
        status: "queued",
        attempts: 0
    };
    pendingCommands.set(pending.commandId, pending);
    updateDevicePanel(device);

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

function requestDatabaseStats() {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_database_stats"
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

initializeInterface();
connectGateway();
setInterval(renderDatabaseStats, 1000);
setInterval(requestDatabaseStats, 30000);
