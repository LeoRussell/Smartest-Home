const deviceCard = document.querySelector(".device-card");
const activityMessage = document.getElementById("activity-message");
const actionFeedback = document.getElementById("action-feedback");
const refreshButton = document.getElementById("refresh-button");
const activityCard = document.querySelector(".activity-card");
const overview = document.getElementById("dashboard-overview");
const deviceSearchInput = document.getElementById("device-search");
const deviceSortSelect = document.getElementById("device-sort");
const deviceFilterResetButton = document.getElementById("device-filter-reset");
const deviceResultCount = document.getElementById("device-result-count");
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
const databaseBackupCreateButton = document.getElementById("database-backup-create");
const databaseBackupRefreshButton = document.getElementById("database-backup-refresh");
const databaseBackupList = document.getElementById("database-backup-list");
const databaseBackupFeedback = document.getElementById("database-backup-feedback");
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
const DEVICE_FAVORITES_KEY = "iot-gateway-device-favorites-v1";
const DEVICE_SORT_KEY = "iot-gateway-device-sort-v1";

function readLocalState(key, fallback) {
    try {
        const value = JSON.parse(localStorage.getItem(key));
        return value ?? fallback;
    } catch (error) {
        return fallback;
    }
}

const storedFavoriteIds = readLocalState(DEVICE_FAVORITES_KEY, []);
const favoriteDeviceIds = new Set(
    Array.isArray(storedFavoriteIds)
        ? storedFavoriteIds.filter(item => typeof item === "string")
        : []
);
const storedDeviceSort = readLocalState(DEVICE_SORT_KEY, "recent");
let deviceFilter = "all";
let deviceSort = ["recent", "name", "status"].includes(storedDeviceSort)
    ? storedDeviceSort
    : "recent";
const deviceDrafts = new Map();
let overviewRenderSignature = "";

const savedUiSettings = readLocalState(UI_SETTINGS_KEY, {});
const uiSettings = {
    navigation: savedUiSettings.navigation === "side" ? "side" : "top",
    language: savedUiSettings.language === "en" ? "en" : "ru",
    timezone: typeof savedUiSettings.timezone === "string" ? savedUiSettings.timezone : "local",
    importantOnly: savedUiSettings.importantOnly === true
};
const savedNotificationState = readLocalState(NOTIFICATION_STATE_KEY, {});
let unreadNotifications = Array.isArray(savedNotificationState.unread)
    ? savedNotificationState.unread.filter(item => isVisibleEvent(item) && Number.isFinite(Number(item.id))).slice(0, 50)
    : [];
let notificationCacheNeedsCleaning = Array.isArray(savedNotificationState.unread) &&
    savedNotificationState.unread.some(item => !isVisibleEvent(item));
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
let pendingAutomationRunFocus = null;
let activePage = "devices";
let lastActivityMessage = "";
let lastGatewayStatusMessage = "Нет соединения с Gateway";
let gatewayIsConnectedState = false;
let databaseStats = null;
let databaseStatsReceivedAt = 0;
let databaseStatsFrozenElapsed = null;
let databaseBackups = [];
let databaseBackupActionPending = false;
let databaseBackupRefreshPending = false;
let automations = [];
let editingAutomationId = null;
let pendingAutomationWorkspaceState = null;
const automationRunHistories = new Map();
const automationRunHistoryCounts = new Map();
const automationRunHistoryCompletedCounts = new Map();
const automationRunHistoryRequests = new Set();
const automationRunHistoryRefreshRequested = new Set();
const openAutomationRunHistories = new Set();

// =========================
// СОСТОЯНИЕ ПРИЛОЖЕНИЯ
// =========================

let socket = null;
let reconnectTimer = null;

let devices = [];
let parameterUiPreferences = {};
const selectedDeviceModes = new Map();
let events = [];
let blockedDevices = [];
let pendingDeviceAccessRequests = [];
let deviceAccessDialog = null;
let deviceAccessDialogRequestId = null;
let deviceIdentityDialog = null;
let deviceConfigurationDialog = null;
let eventFilter = "all";
let eventDeviceFilter = "all";
let eventSearchQuery = "";
let eventSearchTimer = null;
let eventCursor = null;
let eventMatchCount = null;
let eventLegacyVisibilityCounts = false;
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
        if (!isVisibleEvent(event)) continue;
        eventsById.set(event.id, event);
    }

    return [...eventsById.values()].sort(
        (left, right) => right.id - left.id
    );
}

function isVisibleEvent(event) {
    if (!event || typeof event !== "object" || Array.isArray(event)) return false;
    if (event.event_type !== "command") return true;
    try {
        const command = JSON.parse(event.event_value || "{}");
        return !(command && typeof command === "object" && !Array.isArray(command) &&
            command.status === "succeeded" &&
            ((typeof command.automation_id === "string" && command.automation_id.trim().length > 0) ||
                (typeof command.command_id === "string" && command.command_id.startsWith("auto-"))));
    } catch (error) {
        // Legacy plain-text command records remain visible.
        return true;
    }
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

function databaseBackupDownloadUrl(backupId) {
    const host = window.location.hostname || "localhost";
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    return `${protocol}//${host}:8765/gateway-backups/${encodeURIComponent(backupId)}`;
}

function deviceConfigurationExportUrl(deviceId) {
    const host = window.location.hostname || "localhost";
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    return `${protocol}//${host}:8765/gateway-schemas/${encodeURIComponent(deviceId)}.json`;
}

function renderDatabaseBackups() {
    if (!databaseBackupList) return;
    databaseBackupList.replaceChildren();
    if (databaseBackupCreateButton) {
        databaseBackupCreateButton.disabled = !isGatewayConnected()
            || databaseBackupActionPending
            || databaseBackupRefreshPending;
    }
    if (databaseBackupRefreshButton) {
        databaseBackupRefreshButton.disabled = !isGatewayConnected()
            || databaseBackupActionPending
            || databaseBackupRefreshPending;
    }

    if (databaseBackups.length === 0) {
        const empty = document.createElement("p");
        empty.className = "database-backup-empty";
        empty.textContent = localized("Резервных копий пока нет.", "No backups yet.");
        databaseBackupList.append(empty);
        return;
    }

    for (const backup of databaseBackups) {
        const entry = document.createElement("article");
        entry.className = "database-backup-entry";

        const details = document.createElement("div");
        details.className = "database-backup-details";
        const date = document.createElement("strong");
        date.textContent = formatEventTime(backup.created_at);
        const metadata = document.createElement("small");
        metadata.textContent = `${formatDatabaseSize(backup.size_bytes)} · ${backup.backup_id}`;
        details.append(date, metadata);

        const actions = document.createElement("div");
        actions.className = "database-backup-entry-actions";
        const download = document.createElement("a");
        download.className = "database-backup-download";
        download.href = databaseBackupDownloadUrl(backup.backup_id);
        download.download = backup.backup_id;
        download.target = "_blank";
        download.rel = "noopener";
        download.textContent = localized("Скачать", "Download");

        const restore = document.createElement("button");
        restore.type = "button";
        restore.className = "database-backup-restore";
        restore.disabled = !isGatewayConnected()
            || databaseBackupActionPending
            || databaseBackupRefreshPending;
        restore.textContent = localized("Восстановить", "Restore");
        restore.addEventListener("click", () => {
            const prompt = localized(
                `Восстановить базу из копии от ${formatEventTime(backup.created_at)}? Текущая база будет сохранена отдельно. Gateway остановит сценарии и отключит устройства; они смогут подключиться снова. Данные, созданные после копии, будут заменены.`,
                `Restore the database from the backup created ${formatEventTime(backup.created_at)}? The current database will be saved separately. Gateway will stop scenarios and disconnect devices, which can reconnect afterward. Data created after this backup will be replaced.`
            );
            if (!window.confirm(prompt)) return;
            databaseBackupActionPending = true;
            databaseBackupFeedback.textContent = localized("Восстанавливаю базу…", "Restoring database…");
            renderDatabaseBackups();
            if (!sendGatewayAction("restore_database_backup", { backup_id: backup.backup_id })) {
                databaseBackupActionPending = false;
                databaseBackupFeedback.textContent = localized(
                    "Gateway не принял запрос на восстановление.",
                    "Gateway did not accept the restore request."
                );
                renderDatabaseBackups();
            }
        });

        actions.append(download, restore);
        entry.append(details, actions);
        databaseBackupList.append(entry);
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
    if (deviceSearchInput) {
        deviceSearchInput.placeholder = localized(
            "Название, ID или прошивка",
            "Name, ID or firmware"
        );
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
    renderDatabaseBackups();
    refreshAutomationEditor();
    if (window.AutomationInspector) window.AutomationInspector.refresh();
    else renderAutomations();
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
    if (!isVisibleEvent(event)) return false;
    if (["connection", "schema", "configuration"].includes(event.event_type)) return true;
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
    const validEvents = incomingEvents.filter(event => event && Number.isFinite(Number(event.id)));
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
        if (!isVisibleEvent(event)) continue;
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
        configuration: localized("Конфигурация", "Configuration"),
        led: localized("Изменение состояния", "State change"),
        parameter: localized("Изменение состояния", "State change")
    };
    return labels[event.event_type] || localized("Событие", "Event");
}

function renderNotifications() {
    const visibleNotifications = unreadNotifications.filter(isVisibleEvent);
    if (notificationCacheNeedsCleaning || visibleNotifications.length !== unreadNotifications.length) {
        unreadNotifications = visibleNotifications;
        notificationCacheNeedsCleaning = false;
        saveNotificationState();
    }
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
    if (!isVisibleEvent(event)) return;
    closeNotificationPanel();
    eventSearchQuery = "";
    if (eventSearchTimer !== null) window.clearTimeout(eventSearchTimer);
    eventSearchTimer = null;
    eventDeviceFilter = "all";
    eventFilter = ["led", "parameter"].includes(event.event_type)
        ? "state"
        : ["connection", "schema", "command", "configuration"].includes(event.event_type)
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
    window.AutomationInspector?.initialize({
        translate: localized,
        language: () => uiSettings.language,
        getAutomations: () => automations,
        getDraft: buildAutomationConfig,
        isConnected: isGatewayConnected,
        createRequestId: createCommandId,
        send: sendGatewayAction,
        formatTime: formatEventTime,
        render: renderAutomations,
        hasDevice: deviceId => devices.some(item => item.device === deviceId),
        showDevice: deviceId => {
            deviceFilter = "all";
            if (deviceSearchInput) deviceSearchInput.value = deviceId;
            setActivePage("devices");
            renderDevices();
            const panel = [...deviceCard.querySelectorAll(".device-panel")]
                .find(item => item.dataset.deviceId === deviceId);
            panel?.scrollIntoView({ behavior: "smooth", block: "center" });
        }
    });
    if (settingNavigation) settingNavigation.value = uiSettings.navigation;
    if (settingLanguage) settingLanguage.value = uiSettings.language;
    if (deviceSortSelect) deviceSortSelect.value = deviceSort;
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
    deviceSearchInput?.addEventListener("input", renderDevices);
    deviceSortSelect?.addEventListener("change", () => {
        deviceSort = ["recent", "name", "status"].includes(deviceSortSelect.value)
            ? deviceSortSelect.value
            : "recent";
        try {
            localStorage.setItem(DEVICE_SORT_KEY, JSON.stringify(deviceSort));
        } catch (error) {
            console.warn("Could not save device sorting preference:", error);
        }
        renderDevices();
    });
    deviceFilterResetButton?.addEventListener("click", () => {
        deviceFilter = "all";
        if (deviceSearchInput) deviceSearchInput.value = "";
        deviceSort = "recent";
        if (deviceSortSelect) deviceSortSelect.value = deviceSort;
        try {
            localStorage.setItem(DEVICE_SORT_KEY, JSON.stringify(deviceSort));
        } catch (error) {
            console.warn("Could not save device sorting preference:", error);
        }
        renderDevices();
    });
    overview?.addEventListener("click", event => {
        const metric = event.target.closest("[data-device-filter]");
        if (metric) {
            deviceFilter = metric.dataset.deviceFilter;
            renderDevices();
        }
    });
    automationCancelEdit?.addEventListener("click", () => {
        editingAutomationId = null;
        automationForm?.reset();
        window.AutomationBlockly?.clear();
        document.getElementById("automation-editor-title").textContent = localized("Новый сценарий", "New scenario");
        updateAutomationEditorMode();
        if (automationFeedback) automationFeedback.textContent = "";
        window.AutomationInspector?.clearDraft();
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
    if (!connected && wasConnected) {
        if ((databaseBackupRefreshPending || databaseBackupActionPending) && databaseBackupFeedback) {
            databaseBackupFeedback.textContent = localized(
                "Соединение с Gateway потеряно. После подключения обновите список и проверьте результат операции.",
                "Gateway connection was lost. Reconnect and refresh the list to check the operation result."
            );
        }
        databaseBackupRefreshPending = false;
        databaseBackupActionPending = false;
        const configurationSave = deviceConfigurationDialog?.querySelector(".device-config-save");
        if (configurationSave) configurationSave.disabled = false;
        const identitySave = deviceIdentityDialog?.querySelector('[data-role="identity-save"]');
        if (identitySave) identitySave.disabled = false;
    }
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
    deviceConfigurationDialog?.configurationEditor?.connectionChanged(connected);
    renderDatabaseBackups();
    if (wasConnected !== connected) {
        if (connected) window.AutomationInspector?.refresh();
        else window.AutomationInspector?.disconnected();
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
    return Boolean(findPendingCommand(deviceId, "parameter", parameter));
}

function hasPendingAction(deviceId, actionId) {
    return Boolean(findPendingCommand(deviceId, "actionId", actionId));
}

function findPendingCommand(deviceId, field, name) {
    for (const pending of pendingCommands.values()) {
        if (pending.deviceId === deviceId && pending[field] === name) return pending;
    }
    return null;
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
        const valueFilter = Object.prototype.hasOwnProperty.call(trigger, "value")
            ? localized(
                ` → значение стало ${formatAutomationRunValue(trigger.value, definition)}`,
                ` → value became ${formatAutomationRunValue(trigger.value, definition)}`
            )
            : "";
        triggerText = localized(
            `При изменении ${source?.display_name || source?.name || trigger.device} · ${definition?.label || trigger.parameter}${valueFilter}`,
            `When ${source?.display_name || source?.name || trigger.device} · ${definition?.label || trigger.parameter} changes${valueFilter}`
        );
    } else if (trigger.type === "device_connected") {
        triggerText = localized(`При подключении ${trigger.device}`, `When ${trigger.device} connects`);
    } else if (trigger.type === "device_disconnected") {
        triggerText = localized(`При отключении ${trigger.device}`, `When ${trigger.device} disconnects`);
    } else {
        triggerText = localized("При запуске Gateway", "When Gateway starts");
    }
    return `${triggerText}${automationTriggerTimingLabel(trigger)} → ${automationActionCount(program.body)} ${localized("действий", "actions")}`;
}

function automationTriggerTimingLabel(trigger) {
    const parts = [];
    if (Number(trigger?.for_seconds) > 0) {
        parts.push(localized(
            `условие ${trigger.for_seconds} сек.`,
            `stable ${trigger.for_seconds}s`
        ));
    }
    if (Number(trigger?.cooldown_seconds) > 0) {
        parts.push(localized(
            `пауза ${trigger.cooldown_seconds} сек.`,
            `${trigger.cooldown_seconds}s cooldown`
        ));
    }
    return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function refreshAutomationEditor(config = null) {
    const BlocklyEditor = window.AutomationBlockly;
    if (config) {
        window.AutomationInspector?.clearDraft();
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
    const visibleAutomations = window.AutomationInspector?.filtered(automations) || automations;
    if (automations.length === 0) {
        const empty = document.createElement("p");
        empty.className = "automation-empty";
        empty.textContent = localized("Сценариев пока нет. Перетащите блоки в редактор выше.", "No scenarios yet. Drag blocks into the editor above.");
        automationList.append(empty);
        return;
    }
    if (!visibleAutomations.length) {
        const empty = document.createElement("p");
        empty.className = "automation-empty";
        empty.textContent = localized("Сценариев по выбранному фильтру нет.", "No scenarios match the current filter.");
        automationList.append(empty);
        return;
    }
    for (const item of visibleAutomations) {
        const card = document.createElement("article");
        card.className = `automation-card${item.enabled ? "" : " is-disabled"}`;
        const copy = document.createElement("div");
        copy.className = "automation-card-copy";
        const title = document.createElement("h3");
        title.textContent = item.config.name || localized("Без названия", "Untitled scenario");
        const enabledBadge = document.createElement("span");
        enabledBadge.className = "automation-enabled-badge";
        enabledBadge.textContent = item.enabled ? localized("Включён", "Enabled") : localized("Отключён", "Disabled");
        title.append(enabledBadge);
        const description = document.createElement("p");
        description.textContent = automationSummary(item.config);
        const status = document.createElement("small");
        const runtimeStatus = item.run_status || "idle";
        if (runtimeStatus === "running") {
            status.textContent = localized("Выполняется…", "Running…");
        } else if (item.last_error) {
            const prefix = item.last_run_status === "interrupted"
                ? localized("Прервано", "Interrupted")
                : localized("Ошибка", "Error");
            status.textContent = `${prefix}: ${item.last_error}`;
            status.classList.add("automation-status-error");
        } else if (item.last_triggered_at) {
            const runLabels = {
                succeeded: localized("успешно", "succeeded"),
                failed: localized("ошибка", "failed"),
                stopped: localized("остановлен", "stopped"),
                interrupted: localized("прерван", "interrupted")
            };
            status.textContent = localized(
                `Последний запуск: ${formatEventTime(item.last_triggered_at)}${item.last_run_status ? ` · ${runLabels[item.last_run_status] || item.last_run_status}` : ""}`,
                `Last run: ${formatEventTime(item.last_triggered_at)}${item.last_run_status ? ` · ${runLabels[item.last_run_status] || item.last_run_status}` : ""}`
            );
        } else {
            status.textContent = localized("Ожидает событие запуска", "Waiting for its trigger");
        }
        copy.append(title, description, status);
        const history = createAutomationRunHistory(item);
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
        const check = document.createElement("button");
        check.type = "button";
        check.className = "automation-check-button";
        check.textContent = window.AutomationInspector?.pending(item.automation_id)
            ? localized("Проверяю…", "Checking…")
            : localized("Проверить", "Check");
        check.title = localized("Проверить готовность без запуска команд", "Check readiness without running commands");
        check.disabled = !isGatewayConnected() || Boolean(window.AutomationInspector?.pending(item.automation_id));
        check.addEventListener("click", () => window.AutomationInspector?.request(item.automation_id));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "event-action-button";
        remove.textContent = localized("Удалить", "Delete");
        remove.addEventListener("click", () => {
            if (window.confirm(localized(`Удалить сценарий «${item.config.name}»?`, `Delete scenario “${item.config.name}”?`))) {
                sendGatewayAction("delete_automation", { automation_id: item.automation_id });
            }
        });
        actions.append(toggle, check, edit, remove);
        card.append(copy, actions);
        const diagnostics = window.AutomationInspector?.createPanel(item.automation_id);
        if (diagnostics) card.append(diagnostics);
        card.append(history);
        automationList.append(card);
    }
}

function requestAutomationRunHistory(automationId) {
    if (!isGatewayConnected() || automationRunHistoryRequests.has(automationId)) return;
    automationRunHistoryRequests.add(automationId);
    if (!sendGatewayAction("get_automation_run_history", { automation_id: automationId })) {
        automationRunHistoryRequests.delete(automationId);
    }
}

function focusPendingAutomationRun() {
    if (!pendingAutomationRunFocus) return;
    const { automationId, runId } = pendingAutomationRunFocus;
    const entry = [...(automationList?.querySelectorAll(".automation-run-entry") || [])]
        .find(item => item.dataset.runId === runId);
    if (entry) {
        entry.scrollIntoView({ behavior: "smooth", block: "center" });
        entry.classList.add("event-focus");
        pendingAutomationRunFocus = null;
        return;
    }
    if (automationRunHistories.has(automationId)) {
        setActivity(localized(
            "Запуск не найден в загруженной истории сценария.",
            "Run was not found in the loaded scenario history."
        ));
        pendingAutomationRunFocus = null;
    }
}

function openAutomationRunFromEvent(automationId, runId) {
    if (!automationId || !runId) return;
    if (!isGatewayConnected()) {
        setActivity(localized("Нет соединения с Gateway.", "Gateway is disconnected."));
        return;
    }
    pendingAutomationRunFocus = { automationId, runId };
    window.AutomationInspector?.reveal();
    openAutomationRunHistories.add(automationId);
    setActivePage("automations");
    if (!automations.some(item => item.automation_id === automationId)) {
        sendGatewayAction("get_automations");
    } else if (!automationRunHistories.has(automationId)) {
        requestAutomationRunHistory(automationId);
    } else {
        renderAutomations();
        window.setTimeout(focusPendingAutomationRun, 0);
    }
}

function createAutomationRunHistory(automation) {
    const details = document.createElement("details");
    details.className = "automation-run-history";
    details.open = openAutomationRunHistories.has(automation.automation_id);

    const summary = document.createElement("summary");
    summary.textContent = localized("История запусков", "Run history");
    details.append(summary);

    const content = document.createElement("div");
    content.className = "automation-run-history-list";
    const runs = automationRunHistories.get(automation.automation_id);
    const runCount = automationRunHistoryCounts.get(automation.automation_id);
    const completedCount = automationRunHistoryCompletedCounts.get(automation.automation_id);
    if (runs) {
        const toolbar = document.createElement("div");
        toolbar.className = "automation-run-history-toolbar";
        const count = document.createElement("span");
        count.textContent = uiSettings.language === "en"
            ? `${runCount ?? runs.length} saved runs`
            : `Записей: ${runCount ?? runs.length}`;
        const clear = document.createElement("button");
        clear.type = "button";
        clear.className = "automation-run-clear-button";
        clear.textContent = localized("Очистить историю", "Clear history");
        clear.disabled = !completedCount || !isGatewayConnected();
        clear.addEventListener("click", () => {
            if (!completedCount || !isGatewayConnected()) return;
            const prompt = localized(
                `Удалить все записи запусков сценария «${automation.config?.name || automation.automation_id}»? Выполняющиеся запуски останутся в истории.`,
                `Delete all run records for “${automation.config?.name || automation.automation_id}”? Runs in progress will be kept.`
            );
            if (window.confirm(prompt)) {
                sendGatewayAction("clear_automation_run_history", {
                    automation_id: automation.automation_id
                });
            }
        });
        toolbar.append(count, clear);
        content.append(toolbar);
    }
    if (!runs) {
        const loading = document.createElement("p");
        loading.textContent = localized("Загружаю историю…", "Loading history…");
        content.append(loading);
    } else if (runs.length === 0) {
        const empty = document.createElement("p");
        empty.textContent = localized("Запусков пока не было.", "No runs yet.");
        content.append(empty);
    } else {
        for (const run of runs) {
            content.append(createAutomationRunEntry(
                run,
                automation.automation_id,
                automation.config?.name || run.name || automation.automation_id
            ));
        }
    }
    details.append(content);
    details.addEventListener("toggle", () => {
        const id = automation.automation_id;
        if (details.open) {
            openAutomationRunHistories.add(id);
            if (!automationRunHistories.has(id)) requestAutomationRunHistory(id);
        } else {
            openAutomationRunHistories.delete(id);
        }
    });
    if (details.open && !runs) requestAutomationRunHistory(automation.automation_id);
    return details;
}

function automationRunStatusLabel(status) {
    const labels = {
        running: localized("Выполняется", "Running"),
        succeeded: localized("Успешно", "Succeeded"),
        failed: localized("Ошибка", "Failed"),
        stopped: localized("Остановлен", "Stopped"),
        interrupted: localized("Прерван перезапуском", "Interrupted by restart")
    };
    return labels[status] || status || localized("Неизвестно", "Unknown");
}

function automationRunTriggerLabel(run) {
    const trigger = run.trigger || {};
    if (trigger.type === "parameter_changed") {
        const device = devices.find(item => item.device === trigger.device);
        const parameter = device?.schema?.parameters?.[trigger.parameter];
        const valueFilter = Object.prototype.hasOwnProperty.call(trigger, "value")
            ? localized(
                ` → стало ${formatAutomationRunValue(trigger.value, parameter)}`,
                ` → changed to ${formatAutomationRunValue(trigger.value, parameter)}`
            )
            : "";
        return localized(
            `Изменение ${device?.display_name || device?.name || trigger.device} · ${parameter?.label || trigger.parameter}${valueFilter}${automationTriggerTimingLabel(trigger)}`,
            `Change on ${device?.display_name || device?.name || trigger.device} · ${parameter?.label || trigger.parameter}${valueFilter}${automationTriggerTimingLabel(trigger)}`
        );
    }
    if (trigger.type === "device_connected") {
        return localized(`Подключение ${trigger.device}`, `Connect ${trigger.device}`);
    }
    if (trigger.type === "device_disconnected") {
        return localized(`Отключение ${trigger.device}`, `Disconnect ${trigger.device}`);
    }
    if (trigger.type === "gateway_started") {
        return localized("Запуск Gateway", "Gateway started");
    }
    if (trigger.device && trigger.parameter) {
        const device = devices.find(item => item.device === trigger.device);
        const parameter = device?.schema?.parameters?.[trigger.parameter];
        const operators = {
            above: localized("больше", "above"),
            at_least: localized("не меньше", "at least"),
            below: localized("меньше", "below"),
            at_most: localized("не больше", "at most")
        };
        return localized(
            `Порог ${device?.display_name || device?.name || trigger.device} · ${parameter?.label || trigger.parameter} ${operators[trigger.operator] || trigger.operator} ${trigger.threshold}`,
            `Threshold ${device?.display_name || device?.name || trigger.device} · ${parameter?.label || trigger.parameter} ${operators[trigger.operator] || trigger.operator} ${trigger.threshold}`
        );
    }
    return run.trigger_reason || localized("Событие запуска", "Trigger event");
}

function formatAutomationRunValue(value, definition) {
    let formatted;
    if (definition) return formatDisplayedParameterValue(value, definition);
    if (typeof value === "string") {
        formatted = value;
    } else {
        try {
            formatted = JSON.stringify(value);
        } catch {
            formatted = String(value);
        }
    }
    return formatted.length > 160 ? `${formatted.slice(0, 157)}…` : formatted;
}

function automationRunStepLabel(step) {
    const details = step.details || {};
    if (step.type === "condition") {
        const result = details.result === true
            ? localized("условие выполнено", "condition matched")
            : details.result === false
                ? localized("условие не выполнено", "condition did not match")
                : localized("проверка условия", "condition check");
        return localized(`ЕСЛИ, ветка ${details.branch || "?"}: ${result}`, `IF, branch ${details.branch || "?"}: ${result}`);
    }
    if (step.type === "otherwise") return localized("Ветка ИНАЧЕ", "ELSE branch");
    if (step.type === "wait") {
        return localized(`Пауза ${details.seconds ?? "?"} сек.`, `Wait ${details.seconds ?? "?"} sec.`);
    }
    const device = devices.find(item => item.device === details.device);
    const deviceLabel = device?.display_name || device?.name || details.device || "";
    if (step.type === "set_parameter") {
        const parameter = device?.schema?.parameters?.[details.parameter];
        const requestedValue = Object.hasOwn(details, "requested_value")
            ? ` ← ${formatAutomationRunValue(details.requested_value, parameter)}`
            : "";
        const resultValue = Object.hasOwn(details, "result_value")
            ? ` · ${localized("результат", "result")}: ${formatAutomationRunValue(details.result_value, parameter)}`
            : "";
        return localized(
            `Установить ${deviceLabel} · ${parameter?.label || details.parameter}${requestedValue}${resultValue}`,
            `Set ${deviceLabel} · ${parameter?.label || details.parameter}${requestedValue}${resultValue}`
        );
    }
    if (step.type === "invoke_action") {
        const action = device?.schema?.actions?.[details.action];
        const argumentsText = details.arguments && Object.keys(details.arguments).length
            ? ` · ${formatAutomationRunValue(details.arguments)}`
            : "";
        const resultText = Object.hasOwn(details, "result_value")
            ? ` · ${localized("результат", "result")}: ${formatAutomationRunValue(details.result_value)}`
            : "";
        return localized(
            `Действие ${deviceLabel} · ${action?.label || details.action}${argumentsText}${resultText}`,
            `Action ${deviceLabel} · ${action?.label || details.action}${argumentsText}${resultText}`
        );
    }
    return step.type || localized("Шаг сценария", "Scenario step");
}

function createAutomationRunEntry(run, automationId, automationName) {
    const entry = document.createElement("article");
    entry.className = `automation-run-entry is-${run.status || "unknown"}`;
    entry.dataset.runId = run.run_id;

    const heading = document.createElement("div");
    heading.className = "automation-run-heading";
    const time = document.createElement("time");
    time.dateTime = run.started_at || "";
    time.textContent = run.started_at ? formatEventTime(run.started_at) : "—";
    const status = document.createElement("strong");
    status.textContent = automationRunStatusLabel(run.status);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "automation-run-delete-button";
    remove.textContent = localized("Удалить запуск", "Delete run");
    remove.disabled = run.status === "running";
    remove.title = run.status === "running"
        ? localized("Нельзя удалить выполняющийся запуск.", "A running run cannot be deleted.")
        : localized("Удалить эту запись из истории", "Delete this run from history");
    remove.addEventListener("click", () => {
        if (run.status === "running") return;
        const prompt = localized(
            `Удалить запись запуска сценария «${automationName}»?`,
            `Delete this run of “${automationName}”?`
        );
        if (window.confirm(prompt)) {
            sendGatewayAction("delete_automation_run", {
                automation_id: automationId,
                run_id: run.run_id
            });
        }
    });
    heading.append(time, status, remove);

    const trigger = document.createElement("p");
    trigger.className = "automation-run-trigger";
    trigger.textContent = automationRunTriggerLabel(run);
    entry.append(heading, trigger);

    if (Array.isArray(run.steps) && run.steps.length) {
        const list = document.createElement("ol");
        list.className = "automation-run-steps";
        for (const step of run.steps) {
            const item = document.createElement("li");
            item.className = `is-${step.status || "unknown"}`;
            const label = document.createElement("span");
            label.textContent = automationRunStepLabel(step);
            const result = document.createElement("small");
            result.textContent = automationRunStatusLabel(step.status);
            item.append(label, result);
            if (step.error) {
                const error = document.createElement("p");
                error.className = "automation-run-error";
                error.textContent = step.error;
                item.append(error);
            }
            list.append(item);
        }
        entry.append(list);
    }

    if (run.error) {
        const error = document.createElement("p");
        error.className = "automation-run-error";
        error.textContent = run.error;
        entry.append(error);
    }
    return entry;
}

function buildAutomationConfig() {
    const program = window.AutomationBlockly?.compile();
    if (!program) throw new Error(localized("Редактор блоков пока не готов.", "The block editor is not ready."));
    const name = document.getElementById("automation-name").value.trim();
    if (!name) throw new Error(localized("Введите название сценария.", "Enter a scenario name."));
    return {
        version: 2,
        name,
        workspace: window.AutomationBlockly.getState(),
        program
    };
}

function submitAutomation(event) {
    event.preventDefault();
    let config;
    try {
        config = buildAutomationConfig();
    } catch (error) {
        automationFeedback.textContent = error.message || localized("Проверьте структуру блоков.", "Check the block structure.");
        return;
    }
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

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const gatewaySocket = new WebSocket(`${protocol}//${host}:8765`);
    socket = gatewaySocket;

    gatewaySocket.addEventListener("open", () => {
        if (socket !== gatewaySocket) return;
        setGatewayConnectionStatus("Gateway подключён", true);
        setActivity("Подключено к Gateway.");
        for (const device of devices) updateDevicePanel(device);
        renderBlockedDevices();

        gatewaySocket.send(JSON.stringify({
            type: "browser_hello"
        }));

        requestDevices();
        requestEvents({ reset: true });
        sendGatewayAction("get_automations");
    });

    gatewaySocket.addEventListener("message", (event) => {
        if (socket !== gatewaySocket) return;
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

    gatewaySocket.addEventListener("close", () => {
        if (socket !== gatewaySocket) return;
        setGatewayConnectionStatus("Нет соединения с Gateway", false);
        pendingSchemaDecisions.clear();
        schemaHistoryRequests.clear();
        automationRunHistoryRequests.clear();
        automationRunHistoryRefreshRequested.clear();
        activeEventRequest = null;
        eventsLoading = false;
        notificationBackfillActive = null;
        pendingDeviceAccessRequests = [];
        renderDeviceAccessDialog();
        for (const device of devices) updateDevicePanel(device);
        renderBlockedDevices();
        renderEvents();
        renderAutomations();

        setActivity(
            "Соединение потеряно. Повторное подключение..."
        );

        clearTimeout(reconnectTimer);

        reconnectTimer = setTimeout(() => {
            connectGateway();
        }, 2000);
    });

    gatewaySocket.addEventListener("error", (error) => {
        if (socket !== gatewaySocket) return;
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
            window.AutomationInspector?.sync(automations);
            const automationIds = new Set(automations.map(item => item.automation_id));
            for (const counts of [automationRunHistoryCounts, automationRunHistoryCompletedCounts]) {
                for (const automationId of counts.keys()) {
                    if (!automationIds.has(automationId)) counts.delete(automationId);
                }
            }
            if (
                pendingAutomationRunFocus &&
                !automationIds.has(pendingAutomationRunFocus.automationId)
            ) {
                setActivity(localized(
                    "Сценарий из события больше не существует.",
                    "The scenario linked to this event no longer exists."
                ));
                pendingAutomationRunFocus = null;
            }
            for (const automationId of automationRunHistories.keys()) {
                automationRunHistories.delete(automationId);
                if (!automationIds.has(automationId)) {
                    openAutomationRunHistories.delete(automationId);
                    automationRunHistoryRequests.delete(automationId);
                    automationRunHistoryRefreshRequested.delete(automationId);
                }
            }
            for (const automationId of [...openAutomationRunHistories]) {
                if (!automationIds.has(automationId)) {
                    openAutomationRunHistories.delete(automationId);
                    automationRunHistoryRequests.delete(automationId);
                    automationRunHistoryRefreshRequested.delete(automationId);
                }
            }
            for (const automationId of [...automationRunHistoryRequests]) {
                if (!automationIds.has(automationId)) {
                    automationRunHistoryRequests.delete(automationId);
                    automationRunHistoryRefreshRequested.delete(automationId);
                } else {
                    automationRunHistoryRefreshRequested.add(automationId);
                }
            }
            renderAutomations();
            for (const automationId of openAutomationRunHistories) {
                if (!automationRunHistoryRequests.has(automationId)) {
                    automationRunHistories.delete(automationId);
                    requestAutomationRunHistory(automationId);
                }
            }
            break;
        }

        case "automation_diagnostics":
            window.AutomationInspector?.handleMessage(message);
            break;

        case "automation_run_history": {
            const automationId = message.automation_id;
            automationRunHistoryRequests.delete(automationId);
            const refreshAgain = automationRunHistoryRefreshRequested.delete(automationId);
            if (!automations.some(item => item.automation_id === automationId)) {
                automationRunHistories.delete(automationId);
                break;
            }
            if (message.success && Array.isArray(message.runs)) {
                automationRunHistories.set(automationId, message.runs);
                automationRunHistoryCounts.set(
                    automationId,
                    Number.isInteger(message.run_count)
                        ? message.run_count
                        : message.runs.length
                );
                automationRunHistoryCompletedCounts.set(
                    automationId,
                    Number.isInteger(message.completed_count)
                        ? message.completed_count
                        : message.runs.filter(run => run.status !== "running").length
                );
            } else {
                automationRunHistories.set(automationId, []);
                automationRunHistoryCounts.set(automationId, 0);
                automationRunHistoryCompletedCounts.set(automationId, 0);
            }
            if (!refreshAgain) renderAutomations();
            else {
                automationRunHistories.delete(automationId);
                renderAutomations();
                requestAutomationRunHistory(automationId);
            }
            if (!refreshAgain) focusPendingAutomationRun();
            break;
        }

        case "automation_run_delete_result":
            automationFeedback.textContent = message.success
                ? localized("Запись запуска удалена.", "Run deleted.")
                : (message.message || localized("Не удалось удалить запуск.", "Could not delete the run."));
            break;

        case "automation_run_clear_result":
            automationFeedback.textContent = message.success
                ? localized(
                    `Удалено записей: ${message.deleted_count || 0}.`,
                    `Deleted runs: ${message.deleted_count || 0}.`
                )
                : (message.message || localized("Не удалось очистить историю.", "Could not clear the history."));
            break;

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
                window.AutomationInspector?.clearDraft();
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
            const affectedDevices = new Set(
                [...pendingCommands.values()].map(item => item.deviceId)
            );
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
                affectedDevices.add(item.device);
            }
            for (const deviceId of affectedDevices) {
                const device = devices.find(item => item.device === deviceId);
                if (device) updateDevicePanel(device);
            }
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
                "device_configuration_count",
                "device_configuration_history_count",
                "command_record_count",
                "automation_count",
                "automation_run_count",
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

        case "database_backups":
            databaseBackups = Array.isArray(message.backups) ? message.backups : [];
            if (databaseBackupRefreshPending) {
                databaseBackupRefreshPending = false;
                if (databaseBackupFeedback) {
                    databaseBackupFeedback.textContent = localized(
                        "Список резервных копий обновлён.",
                        "Backup list updated."
                    );
                }
            }
            renderDatabaseBackups();
            break;

        case "database_backup_result":
            databaseBackupActionPending = false;
            if (databaseBackupFeedback) {
                databaseBackupFeedback.textContent = message.success
                    ? localized("Резервная копия создана на Gateway.", "Backup created on the Gateway.")
                    : localized(
                        `Не удалось создать копию: ${message.message || "ошибка"}`,
                        `Could not create the backup: ${message.message || "unknown error"}`
                    );
            }
            renderDatabaseBackups();
            break;

        case "database_backup_restore_result":
            databaseBackupActionPending = false;
            if (databaseBackupFeedback) {
                databaseBackupFeedback.textContent = message.success
                    ? localized(
                        "База восстановлена. Устройства отключены и могут подключиться снова.",
                        "Database restored. Devices are disconnected and can reconnect."
                    )
                    : localized(
                        `Не удалось восстановить базу: ${message.message || "ошибка"}`,
                        `Could not restore the database: ${message.message || "unknown error"}`
                    );
            }
            renderDatabaseBackups();
            break;

        case "device_list": {
            devices = Array.isArray(message.devices)
                ? message.devices
                : [];

            deviceConfigurationDialog?.configurationEditor?.refreshSavedState();
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

        case "device_configuration_save_result": {
            const dialog = deviceConfigurationDialog;
            if (dialog && dialog.dataset.deviceId === message.device) {
                dialog.configurationEditor?.saveResult(message);
            }
            requestDevices();
            break;
        }

        case "device_configuration_validation":
            if (deviceConfigurationDialog?.dataset.deviceId === message.device) {
                deviceConfigurationDialog.configurationEditor?.validationResult(message);
            }
            break;

        case "device_configuration_retry_result":
            if (deviceConfigurationDialog?.dataset.deviceId === message.device) {
                deviceConfigurationDialog.configurationEditor?.retryResult(message);
            }
            requestDevices();
            break;

        case "device_configuration_apply_result": {
            const dialog = deviceConfigurationDialog;
            if (dialog && dialog.dataset.deviceId === message.device) {
                dialog.configurationEditor?.applyResult(message);
            }
            if (message.message) setActivity(message.message);
            requestDevices();
            break;
        }

        case "parameter_ui_preferences": {
            parameterUiPreferences = message.preferences && typeof message.preferences === "object"
                ? message.preferences
                : {};
            deviceConfigurationDialog?.configurationEditor?.refreshSavedState();
            renderDevices();
            break;
        }

        case "parameter_ui_preference_result": {
            if (message.success) {
                setActivity(message.message || localized("Вид элемента сохранён.", "Control style saved."));
            } else {
                setActivity(message.message || localized("Не удалось сохранить вид элемента.", "Could not save the control style."));
                renderDevices();
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
                    if (favoriteDeviceIds.delete(message.device)) {
                        favoriteDeviceIds.add(message.new_device_id);
                        saveFavoriteDeviceIds();
                    }
                    for (const [key, value] of [...deviceDrafts]) {
                        const [draftDeviceId, parameterId] = key.split("\u0000");
                        if (draftDeviceId === message.device) {
                            deviceDrafts.delete(key);
                            deviceDrafts.set(
                                `${message.new_device_id}\u0000${parameterId}`,
                                value
                            );
                        }
                    }
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
                if (message.last_telemetry_at) {
                    device.last_telemetry_at = message.last_telemetry_at;
                }
            } else {
                device = {
                    device: message.device,
                    connected: true,
                    parameters: message.parameters || {},
                    last_seen: message.last_seen,
                    last_telemetry_at: message.last_telemetry_at || null
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
                deviceDrafts.delete(`${pending.deviceId}\u0000${pending.parameter}`);
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
            const receivedEvents = Array.isArray(message.events)
                ? message.events
                : [];
            const incomingEvents = receivedEvents.filter(isVisibleEvent);
            const containsHiddenEvents = incomingEvents.length !== receivedEvents.length;
            if (
                typeof message.request_id === "string" &&
                message.request_id.startsWith("notification-backfill-")
            ) {
                if (notificationBackfillActive !== message.request_id) break;
                notificationBackfillActive = null;
                observeEventNotifications(receivedEvents);
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
                if (
                    activeEventRequest.searchQuery !== eventSearchQuery.trim() ||
                    (message.search ?? "") !== activeEventRequest.searchQuery
                ) {
                    activeEventRequest = null;
                    eventsLoading = false;
                    if (eventSearchQuery.trim()) {
                        setActivity(localized(
                            "Поиск требует перезапуска Gateway с обновлённым кодом.",
                            "Search requires restarting Gateway with the updated code."
                        ));
                    }
                    renderEvents();
                    break;
                }

                if (activeEventRequest.mode === "reset") {
                    events = incomingEvents;
                    eventLegacyVisibilityCounts = containsHiddenEvents;
                    initializeNotifications =
                        !notificationStateReady && !activeEventRequest.searchQuery;
                    shouldBackfillNotifications =
                        activeEventRequest.deviceFilter === "all" &&
                        !activeEventRequest.searchQuery;
                } else {
                    events = mergeEventPages(events, incomingEvents);
                    eventLegacyVisibilityCounts ||= containsHiddenEvents;
                }

                eventCursor = message.next_before_id ?? null;
                eventsHasMore = message.has_more === true;
                eventMatchCount = eventLegacyVisibilityCounts
                    ? (eventsHasMore ? null : events.length)
                    : Number.isInteger(message.total_count) ? message.total_count : events.length;
                activeEventRequest = null;
                eventsLoading = false;
            } else {
                const hadLoadedEvents = events.length > 0;
                const matchingEvents = incomingEvents.filter(event => {
                    return (
                        eventDeviceFilter === "all" ||
                        event.device_id === eventDeviceFilter
                    ) && eventMatchesSearch(event, eventSearchQuery);
                });

                events = mergeEventPages(events, matchingEvents);

                if (!hadLoadedEvents && eventDeviceFilter === "all" && !eventSearchQuery) {
                    eventCursor = message.next_before_id ?? null;
                    eventsHasMore = message.has_more === true;
                    eventLegacyVisibilityCounts ||= containsHiddenEvents;
                    if (eventLegacyVisibilityCounts) {
                        eventMatchCount = eventsHasMore ? null : events.length;
                    } else if (Number.isInteger(message.total_count)) {
                        eventMatchCount = message.total_count;
                    }
                }
            }

            observeEventNotifications(receivedEvents, initializeNotifications);
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
        ["components", localized("Компонент", "Component")],
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
                if (
                    group === "parameters" &&
                    previous[id].control !== next[id].control
                ) {
                    const controlNames = {
                        toggle: localized("переключатель", "toggle"),
                        slider: localized("ползунок", "slider"),
                        select: localized("список", "select list"),
                        input: localized("поле ввода", "input field")
                    };
                    const oldControl = controlNames[previous[id].control] ||
                        localized("авто по типу", "automatic by type");
                    const newControl = controlNames[next[id].control] ||
                        localized("авто по типу", "automatic by type");
                    changes.push(localized(
                        `Интерфейс параметра «${newLabel}»: ${oldControl} → ${newControl}`,
                        `Control for “${newLabel}”: ${oldControl} → ${newControl}`
                    ));
                }
                changes.push(oldLabel === newLabel
                    ? `${label} «${newLabel}» изменён`
                    : `${label} «${oldLabel}» переименован в «${newLabel}»`);
            }
        }
    }

    const previousCapabilities = previousSchema?.capabilities || {};
    const nextCapabilities = nextSchema?.capabilities || {};
    const capabilityIds = new Set([
        ...Object.keys(previousCapabilities),
        ...Object.keys(nextCapabilities)
    ]);
    for (const id of capabilityIds) {
        const wasAvailable = previousCapabilities[id] === true;
        const isAvailable = nextCapabilities[id] === true;
        if (wasAvailable === isAvailable) continue;
        const label = id === "built_in_indicator"
            ? localized("встроенный индикатор", "built-in indicator")
            : id === "identify"
                ? localized("поиск устройства", "device identification")
                : id;
        changes.push(isAvailable
            ? localized(`Добавлена возможность: ${label}`, `Capability added: ${label}`)
            : localized(`Удалена возможность: ${label}`, `Capability removed: ${label}`));
    }

    if (changes.length === 0 && canonicalJson(previousSchema) !== canonicalJson(nextSchema)) {
        changes.push("Изменились другие поля Schema");
    }
    return changes;
}

function isDeviceConfigurationBusy(device) {
    return device?.device_profile === "universal" &&
        (device.configuration_status === "applying" || device.configuration_sync_pending === true);
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
    select.disabled = !device.connected || !isGatewayConnected() || Boolean(device.pending_schema) || isDeviceConfigurationBusy(device);

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
        button.disabled = !device.connected || !isGatewayConnected() || Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) || Boolean(pending);
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

function downloadDeviceSchema(device) {
    if (!device?.schema || typeof device.schema !== "object") return;
    const exportDocument = {
        export_format: "smartest-home-schema",
        export_version: 1,
        exported_at: new Date().toISOString(),
        source_device_id: device.device,
        schema_version: device.schema_version ?? null,
        firmware_version: device.firmware_version ?? null,
        schema: device.schema
    };
    const blob = new Blob(
        [JSON.stringify(exportDocument, null, 2)],
        { type: "application/json;charset=utf-8" }
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${String(device.device || "device").replace(/[^A-Za-z0-9_-]/g, "_")}-schema.json`;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
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

    const exportButton = document.createElement("button");
    exportButton.type = "button";
    exportButton.className = "event-tool-button";
    exportButton.textContent = localized("Скачать текущую Schema", "Download current Schema");
    exportButton.disabled = !device.schema || typeof device.schema !== "object";
    exportButton.addEventListener("click", () => downloadDeviceSchema(device));

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
        actions.append(restore, exportButton, remove);
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

    if (!history || history.length === 0) {
        const exportActions = document.createElement("div");
        exportActions.className = "schema-history-actions";
        exportActions.appendChild(exportButton);
        content.appendChild(exportActions);
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

function parameterHasZeroOneValues(definition) {
    if (definition.type === "boolean") return true;
    if (definition.type === "integer" || definition.type === "number") {
        return definition.min === 0 && definition.max === 1 &&
            (definition.type === "integer"
                ? definition.step === undefined || definition.step === 1
                : definition.step === 1);
    }
    if (definition.type === "enum" && definition.options?.length === 2) {
        const values = definition.options.map(option => option.value);
        const numeric = values.every(value => typeof value === "number");
        const strings = values.every(value => typeof value === "string");
        return numeric
            ? values.includes(0) && values.includes(1)
            : strings && values.includes("0") && values.includes("1");
    }
    return false;
}

function parameterSelectOptions(definition) {
    if (definition.type === "enum") {
        const optionValues = (definition.options || []).map(option => option.value);
        const numericZeroOne = optionValues.length === 2 &&
            optionValues.every(value => typeof value === "number") &&
            optionValues.includes(0) && optionValues.includes(1);
        const stringZeroOne = optionValues.length === 2 &&
            optionValues.every(value => typeof value === "string") &&
            optionValues.includes("0") && optionValues.includes("1");
        return (definition.options || []).map(option => ({
            value: option.value,
            label: numericZeroOne || stringZeroOne
                ? String(option.value)
                : option.label
        }));
    }
    if (parameterHasZeroOneValues(definition) && definition.type !== "boolean") {
        return [
            { value: 0, label: "0" },
            { value: 1, label: "1" }
        ];
    }
    return [];
}

function parameterControlType(definition, preference = null) {
    if (preference === "toggle" && definition.type === "boolean") return "toggle";
    if (preference === "list" && definition.type === "boolean") return "boolean-list";
    if (preference === "list" && parameterHasZeroOneValues(definition)) return "select";
    if (preference === "input" && ["integer", "number"].includes(definition.type)) return "input";
    if (
        preference === "slider" && ["integer", "number"].includes(definition.type) &&
        Number.isFinite(definition.min) && Number.isFinite(definition.max) &&
        definition.min < definition.max
    ) return "slider";
    if (definition.control) return definition.control;
    if (definition.type === "boolean") return "toggle";
    if (definition.type === "enum") return "select";
    if (
        ["integer", "number"].includes(definition.type) &&
        Number.isFinite(definition.min) && Number.isFinite(definition.max) &&
        definition.min < definition.max
    ) return "slider";
    return "input";
}

function parameterControlStyleOptions(definition) {
    if (definition.access !== "read_write") return [];
    if (definition.type === "boolean") {
        return [
            ["schema", localized("По Schema", "From Schema")],
            ["toggle", localized("Переключатель", "Switch")],
            ["list", localized("Список", "List")]
        ];
    }
    if (["integer", "number"].includes(definition.type)) {
        const choices = [["schema", localized("По Schema", "From Schema")]];
        if (
            Number.isFinite(definition.min) && Number.isFinite(definition.max) &&
            definition.min < definition.max
        ) choices.push(["slider", localized("Ползунок", "Slider")]);
        if (parameterHasZeroOneValues(definition)) {
            choices.splice(1, 0, ["list", localized("Список", "List")]);
        }
        choices.push(["input", localized("Ввод числа", "Number input")]);
        return choices;
    }
    return [];
}

function createParameterControlStylePicker(device, parameterId, definition, preference) {
    const styleOptions = parameterControlStyleOptions(definition);
    if (styleOptions.length === 0) return null;

    const details = document.createElement("details");
    details.className = "device-control-style-picker";

    const summary = document.createElement("summary");
    summary.className = "device-control-style-button";
    summary.title = localized("Настроить вид элемента", "Choose control style");
    summary.setAttribute("aria-label", summary.title);

    const menu = document.createElement("div");
    menu.className = "device-control-style-menu";
    const label = document.createElement("label");
    label.className = "device-control-style-label";
    label.textContent = localized("Вид элемента", "Control style");

    const select = document.createElement("select");
    select.className = "device-parameter-input device-control-style-select";
    select.dataset.role = "parameter-control-style";
    select.dataset.deviceId = device.device;
    select.dataset.parameterId = parameterId;
    select.setAttribute("aria-label", label.textContent);
    for (const [styleValue, styleName] of styleOptions) {
        const option = document.createElement("option");
        option.value = styleValue;
        option.textContent = styleName;
        option.selected = styleValue === preference;
        select.appendChild(option);
    }
    select.disabled = !isGatewayConnected() || Boolean(device.pending_schema);
    select.addEventListener("change", () => {
        const saved = sendGatewayAction("set_parameter_ui_preference", {
            device: device.device,
            parameter: parameterId,
            control: select.value
        });
        if (saved) {
            select.disabled = true;
            details.open = false;
        }
    });

    label.appendChild(select);
    menu.appendChild(label);
    details.append(summary, menu);
    return details;
}

function saveFavoriteDeviceIds() {
    try {
        localStorage.setItem(
            DEVICE_FAVORITES_KEY,
            JSON.stringify([...favoriteDeviceIds])
        );
    } catch (error) {
        console.warn("Could not save pinned devices:", error);
    }
}

function toggleDeviceFavorite(deviceId) {
    if (favoriteDeviceIds.has(deviceId)) {
        favoriteDeviceIds.delete(deviceId);
    } else {
        favoriteDeviceIds.add(deviceId);
    }
    saveFavoriteDeviceIds();
    renderDevices();
}

function getVisibleDevices() {
    const query = deviceSearchInput?.value.trim().toLocaleLowerCase() || "";
    const filtered = devices.filter(device => {
        if (deviceFilter === "online" && !device.connected) return false;
        if (deviceFilter === "offline" && device.connected) return false;
        if (deviceFilter === "favorites" && !favoriteDeviceIds.has(device.device)) return false;
        if (!query) return true;
        const searchable = [
            device.display_name,
            device.name,
            device.device,
            device.firmware_version
        ].filter(Boolean).join(" ").toLocaleLowerCase();
        return searchable.includes(query);
    });

    const collator = new Intl.Collator(uiSettings.language, {
        numeric: true,
        sensitivity: "base"
    });
    filtered.sort((left, right) => {
        const favoriteOrder = Number(favoriteDeviceIds.has(right.device)) -
            Number(favoriteDeviceIds.has(left.device));
        if (favoriteOrder !== 0) return favoriteOrder;
        if (deviceSort === "name") {
            return collator.compare(
                left.display_name || left.name || left.device,
                right.display_name || right.name || right.device
            );
        }
        if (deviceSort === "status") {
            return Number(right.connected) - Number(left.connected) ||
                collator.compare(left.device, right.device);
        }
        const leftSeen = Date.parse(left.last_seen || "") || 0;
        const rightSeen = Date.parse(right.last_seen || "") || 0;
        return rightSeen - leftSeen || collator.compare(left.device, right.device);
    });
    return filtered;
}

function renderDevices() {
    if (!deviceCard) {
        console.error("Не найден элемент .device-card");
        return;
    }

    for (const input of deviceCard.querySelectorAll('[data-role="parameter-input"][data-dirty="true"]')) {
        deviceDrafts.set(`${input.dataset.deviceId}\u0000${input.dataset.parameterId}`, input.value);
    }

    deviceCard.replaceChildren();

    if (devices.length === 0) {
        if (deviceResultCount) {
            deviceResultCount.textContent = localized("Устройств пока нет", "No devices yet");
        }
        if (deviceFilterResetButton) deviceFilterResetButton.disabled = true;
        const empty = document.createElement("p");
        empty.className = "device-empty";
        empty.textContent = localized("Устройства пока не найдены.", "No devices found yet.");

        deviceCard.appendChild(empty);
        renderOverview();
        return;
    }

    const visibleDevices = getVisibleDevices();
    if (deviceResultCount) {
        deviceResultCount.textContent = localized(
            `Показано ${visibleDevices.length} из ${devices.length}`,
            `Showing ${visibleDevices.length} of ${devices.length}`
        );
    }
    if (deviceFilterResetButton) {
        deviceFilterResetButton.disabled =
            deviceFilter === "all" &&
            !deviceSearchInput?.value.trim() &&
            deviceSort === "recent";
    }

    if (visibleDevices.length === 0) {
        const empty = document.createElement("div");
        empty.className = "device-filter-empty";
        const message = document.createElement("p");
        message.textContent = deviceFilter === "favorites" && favoriteDeviceIds.size === 0
            ? localized(
                "Пока нет закреплённых устройств. Нажмите ★ в карточке устройства, чтобы добавить его сюда.",
                "No pinned devices yet. Select the star on a device card to add it here."
            )
            : localized(
                "По этим условиям устройств не найдено.",
                "No devices match these filters."
            );
        const reset = document.createElement("button");
        reset.type = "button";
        reset.textContent = localized("Показать все устройства", "Show all devices");
        reset.addEventListener("click", () => deviceFilterResetButton?.click());
        empty.append(message, reset);
        deviceCard.appendChild(empty);
        renderOverview();
        return;
    }

    for (const device of visibleDevices) {
        const panel = document.createElement("article");
        panel.className = "device-panel";
        panel.dataset.deviceId = device.device;

        const heading = document.createElement("div");
        heading.className = "device-heading";

        const icon = document.createElement("div");
        icon.className = "device-icon";
        icon.textContent = "IoT";

        const titleBlock = document.createElement("div");
        titleBlock.className = "device-title-block";

        const titleLine = document.createElement("div");
        titleLine.className = "device-title-line";

        const title = document.createElement("h3");
        title.textContent = device.display_name || device.name || device.device;

        const badge = document.createElement("span");
        badge.className = "badge device-status";
        badge.textContent = device.connected
            ? localized("Подключено", "Online")
            : localized("Отключено", "Offline");
        badge.classList.toggle("is-online", device.connected);
        badge.classList.toggle("is-offline", !device.connected);
        titleLine.append(title, badge);

        const subtitle = document.createElement("p");
        subtitle.className = "device-id";
        const schemaNote = device.schema_source === "inferred"
            ? localized("Автоматическая Schema · только чтение", "Inferred Schema · read only")
            : device.schema_source === "missing"
                ? localized("Schema ещё не получена", "Schema not received yet")
                : null;
        const configurationNotes = {
            not_configured: localized("Требует настройки", "Needs configuration"),
            pending: localized("Конфигурация ожидает подключения", "Configuration queued"),
            applying: localized("Применение конфигурации…", "Applying configuration…"),
            failed: localized("Ошибка конфигурации", "Configuration failed")
        };
        const configurationNote = device.device_profile === "universal"
            ? configurationNotes[device.configuration_status] || null
            : null;
        subtitle.textContent = [
            device.device,
            device.firmware_version
                ? localized(`Прошивка ${device.firmware_version}`, `Firmware ${device.firmware_version}`)
                : null,
            configurationNote,
            schemaNote
        ].filter(Boolean).join(" · ");

        titleBlock.append(titleLine, subtitle);
        if (device.device_profile === "universal" && Number(device.configuration_version) > 0) {
            const configurationSummary = document.createElement("p");
            configurationSummary.className = "device-config-card-status";
            configurationSummary.dataset.state = device.configuration_status || "pending";
            const applied = Number(device.configuration_applied_version) || 0;
            configurationSummary.textContent = `Gateway v${device.configuration_version} · ESP32 ` + (applied ? `v${applied}` : localized("ещё не подтвердила", "not confirmed yet"));
            if (isDeviceConfigurationBusy(device)) configurationSummary.textContent += localized(" · Ожидается подтверждение конфигурации", " · Waiting for configuration confirmation");
            configurationSummary.title = device.configuration_error || localized(
                "Gateway хранит сохранённую версию; ESP32 показывает последнюю подтверждённую версию.",
                "Gateway shows the saved version; ESP32 shows the last confirmed version."
            );
            titleBlock.append(configurationSummary);
        }

        const headerActions = document.createElement("div");
        headerActions.className = "device-header-actions";
        const favorite = document.createElement("button");
        const isFavorite = favoriteDeviceIds.has(device.device);
        favorite.type = "button";
        favorite.className = "device-favorite-button";
        favorite.dataset.role = "device-favorite";
        favorite.setAttribute("aria-pressed", String(isFavorite));
        favorite.textContent = isFavorite ? "★" : "☆";
        favorite.title = isFavorite
            ? localized("Открепить устройство", "Unpin device")
            : localized("Закрепить устройство сверху", "Pin device to the top");
        favorite.setAttribute("aria-label", favorite.title);
        favorite.addEventListener("click", () => toggleDeviceFavorite(device.device));
        headerActions.appendChild(favorite);

        const editIdentity = document.createElement("button");
        editIdentity.type = "button";
        editIdentity.className = "device-identity-button";
        editIdentity.textContent = localized("Изменить", "Edit");
        editIdentity.setAttribute("aria-label", localized("Изменить имя и ID", "Edit name and ID"));
        editIdentity.title = localized("Изменить отображаемое имя и ID Gateway", "Edit the display name and Gateway ID");
        editIdentity.addEventListener("click", () => openDeviceIdentityDialog(device));

        const configureDevice = device.device_profile === "universal"
            ? document.createElement("button")
            : null;
        if (configureDevice) {
            configureDevice.type = "button";
            configureDevice.className = "device-config-button";
            configureDevice.textContent = localized("Настроить", "Configure");
            configureDevice.title = localized(
                "Настроить поддерживаемые компоненты ESP32",
                "Configure supported ESP32 components"
            );
            configureDevice.addEventListener("click", () => openDeviceConfigurationDialog(device));
        }

        const identify = device.schema?.actions?.identify
            ? document.createElement("button")
            : null;
        if (identify) {
            identify.type = "button";
            identify.className = "device-identify-button";
            identify.dataset.role = "device-identify";
            identify.dataset.actionId = "identify";
            identify.title = device.schema.actions.identify.label ||
                localized("Найти устройство", "Find device");
            identify.setAttribute("aria-label", identify.title);
            const pin = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            pin.setAttribute("viewBox", "0 0 24 24");
            pin.setAttribute("aria-hidden", "true");
            const pinOutline = document.createElementNS("http://www.w3.org/2000/svg", "path");
            pinOutline.setAttribute("d", "M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z");
            const pinCenter = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            pinCenter.setAttribute("cx", "12");
            pinCenter.setAttribute("cy", "10");
            pinCenter.setAttribute("r", "2.5");
            pin.append(pinOutline, pinCenter);
            identify.appendChild(pin);
            identify.addEventListener("click", () => {
                sendDeviceAction(device.device, "identify");
            });
        }

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
        if (configureDevice) headerActions.append(configureDevice);
        headerActions.append(editIdentity);
        if (identify) headerActions.appendChild(identify);
        headerActions.appendChild(forget);

        heading.append(icon, titleBlock, headerActions);
        panel.appendChild(heading);

        const divider = document.createElement("div");
        divider.className = "divider";
        panel.appendChild(divider);

        if (device.pending_schema) {
            panel.appendChild(createSchemaReview(device));
        }

        const parameters = device.schema?.parameters || {};
        const schemaComponents = device.schema?.components || {};
        const componentGroups = new Map();

        for (const [parameterId, definition] of Object.entries(parameters)) {
            const parameter = document.createElement("div");
            parameter.className = "device-parameter";
            parameter.dataset.parameterId = parameterId;

            let parameterContainer = panel;
            const componentId = definition.component;
            const componentDefinition = schemaComponents[componentId];
            if (componentDefinition) {
                if (!componentGroups.has(componentId)) {
                    const group = document.createElement("section");
                    group.className = "device-component-group";
                    const groupHeading = document.createElement("strong");
                    groupHeading.className = "device-component-heading";
                    groupHeading.textContent = componentDefinition.label || componentId;
                    group.appendChild(groupHeading);
                    if (componentDefinition.description) {
                        const groupDescription = document.createElement("p");
                        groupDescription.className = "device-component-description";
                        groupDescription.textContent = componentDefinition.description;
                        group.appendChild(groupDescription);
                    }
                    panel.appendChild(group);
                    componentGroups.set(componentId, group);
                }
                parameterContainer = componentGroups.get(componentId);
            }

            const labelBlock = document.createElement("div");
            labelBlock.className = "device-parameter-name";
            const label = document.createElement("strong");
            label.className = "device-parameter-label";
            label.textContent = definition.label || parameterId;
            if (definition.description) label.title = definition.description;
            labelBlock.appendChild(label);
            if (definition.description) {
                const description = document.createElement("small");
                description.className = "device-parameter-description";
                description.textContent = definition.description;
                labelBlock.appendChild(description);
            }

            const value = device.parameters?.[parameterId];
            const currentValue = document.createElement("span");
            currentValue.className = "device-parameter-value";
            currentValue.dataset.role = "parameter-value";
            currentValue.dataset.parameterId = parameterId;
            currentValue.textContent = formatDisplayedParameterValue(value, definition);

            parameter.append(labelBlock, currentValue);

            if (definition.access === "read_write") {
                const control = document.createElement("div");
                control.className = "device-parameter-control";
                const preference = parameterUiPreferences?.[device.device]?.[parameterId] || "schema";
                const stylePicker = createParameterControlStylePicker(
                    device, parameterId, definition, preference
                );

                if (device.connected) {
                    const commandControl = document.createElement("div");
                    commandControl.className = "device-parameter-command-control";
                    const controlType = parameterControlType(definition, preference);

                    if (controlType === "toggle") {
                        const button = document.createElement("button");
                        button.type = "button";
                        button.className = "device-boolean-switch";
                        button.setAttribute("role", "switch");
                        button.dataset.role = "parameter-command";
                        button.dataset.parameterId = parameterId;
                        const track = document.createElement("span");
                        track.className = "device-boolean-switch-track";
                        track.setAttribute("aria-hidden", "true");
                        const thumb = document.createElement("span");
                        thumb.className = "device-boolean-switch-thumb";
                        track.appendChild(thumb);
                        const stateLabel = document.createElement("span");
                        stateLabel.className = "device-boolean-switch-label";
                        button.append(track, stateLabel);
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
                        commandControl.appendChild(button);
                    } else {
                        const isBooleanList = controlType === "boolean-list";
                        const isSelect = controlType === "select" || isBooleanList;
                        const input = document.createElement(
                            isSelect ? "select" : "input"
                        );
                        input.className = "device-parameter-input";
                        input.dataset.role = "parameter-input";
                        input.dataset.deviceId = device.device;
                        input.dataset.parameterId = parameterId;
                        input.dataset.controlType = controlType;
                        input.setAttribute("aria-label", definition.label || parameterId);
                        let valuePreview = null;
                        let sliderBounds = null;
                        input.addEventListener(
                            isSelect ? "change" : "input",
                            () => {
                                input.dataset.dirty = "true";
                                deviceDrafts.set(
                                    `${device.device}\u0000${parameterId}`,
                                    input.value
                                );
                                if (valuePreview) {
                                    valuePreview.textContent = formatDisplayedParameterValue(
                                        Number(input.value), definition
                                    );
                                }
                            }
                        );

                        if (isBooleanList) {
                            const options = [
                                ["true", localized("Включено", "On")],
                                ["false", localized("Выключено", "Off")]
                            ];
                            if (typeof value !== "boolean") {
                                const placeholder = document.createElement("option");
                                placeholder.value = "";
                                placeholder.textContent = localized("Выберите значение", "Choose a value");
                                placeholder.selected = true;
                                input.required = true;
                                input.appendChild(placeholder);
                            }
                            for (const [optionValue, optionLabel] of options) {
                                const item = document.createElement("option");
                                item.value = optionValue;
                                item.textContent = optionLabel;
                                item.selected = String(value) === optionValue;
                                input.appendChild(item);
                            }
                        } else if (controlType === "select") {
                            for (const option of parameterSelectOptions(definition)) {
                                const item = document.createElement("option");
                                item.value = JSON.stringify(option.value);
                                item.textContent = option.label;
                                item.selected = option.value === value;
                                input.appendChild(item);
                            }
                        } else {
                            const isSlider = controlType === "slider";
                            input.type = isSlider
                                ? "range"
                                : definition.type === "string" ? "text" : "number";
                            input.value = value ?? (isSlider ? definition.min : "");
                            if (definition.type === "integer" || definition.type === "number") {
                                input.required = !isSlider;
                            }
                            if (definition.type === "integer") {
                                input.step = String(definition.step || 1);
                            } else if (definition.type === "number") {
                                input.step = String(definition.step ?? (
                                    isSlider && definition.precision != null
                                        ? 10 ** -definition.precision
                                        : "any"
                                ));
                            }
                            if (definition.min !== undefined) input.min = definition.min;
                            if (definition.max !== undefined) input.max = definition.max;

                            if (isSlider) {
                                const minimum = document.createElement("span");
                                minimum.className = "device-parameter-slider-bound";
                                minimum.textContent = String(definition.min);
                                minimum.setAttribute("aria-hidden", "true");
                                const maximum = document.createElement("span");
                                maximum.className = "device-parameter-slider-bound";
                                maximum.textContent = String(definition.max);
                                maximum.setAttribute("aria-hidden", "true");
                                sliderBounds = { minimum, maximum };
                                valuePreview = document.createElement("output");
                                valuePreview.className = "device-parameter-slider-preview";
                                valuePreview.dataset.role = "parameter-input-preview";
                                valuePreview.dataset.parameterId = parameterId;
                                valuePreview.textContent = formatDisplayedParameterValue(value, definition);
                            }
                        }

                        const savedDraft = deviceDrafts.get(
                            `${device.device}\u0000${parameterId}`
                        );
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
                            } else if (definition.type === "boolean") {
                                nextValue = input.value === "true" || input.value === "1";
                            } else if (definition.type === "integer") {
                                nextValue = Number.parseInt(input.value, 10);
                            } else if (definition.type === "number") {
                                nextValue = Number(input.value);
                            }
                            sendParameterCommand(device.device, parameterId, nextValue);
                        });
                        if (sliderBounds) commandControl.append(sliderBounds.minimum);
                        commandControl.append(input);
                        if (sliderBounds) commandControl.append(sliderBounds.maximum);
                        if (valuePreview) commandControl.append(valuePreview);
                        commandControl.append(apply);
                    }
                    if (stylePicker) commandControl.appendChild(stylePicker);
                    control.appendChild(commandControl);
                } else if (stylePicker) {
                    control.appendChild(stylePicker);
                }

                parameter.appendChild(control);
            }

            parameterContainer.appendChild(parameter);
        }

        panel.appendChild(createDeviceModeControls(device));

        const actions = device.schema?.actions || {};
        const modeActions = modeActionIds(device);
        const availableActions = Object.entries(actions).filter(([actionId]) =>
            !modeActions.has(actionId) && actionId !== "identify"
        );
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

        panel.appendChild(createSchemaHistory(device));

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

function deviceTelemetryFreshness(device) {
    const timestamp = Date.parse(device.last_telemetry_at || "");
    if (!Number.isFinite(timestamp)) {
        return {
            state: "unknown",
            label: localized("Телеметрия ещё не поступала", "No telemetry received yet")
        };
    }
    const ageSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    const staleAfter = Number(device.telemetry_stale_after_seconds) || 45;
    const age = formatDatabaseDuration(ageSeconds);
    return ageSeconds > staleAfter
        ? {
            state: "stale",
            label: localized(`Данные устарели · ${age} назад`, `Data is stale · ${age} ago`)
        }
        : {
            state: "fresh",
            label: localized(`Данные свежие · ${age} назад`, `Data is fresh · ${age} ago`)
        };
}

function updateDeviceHint(device, panel = null) {
    panel ||= [...(deviceCard?.querySelectorAll(".device-panel") || [])]
        .find(item => item.dataset.deviceId === device.device);
    const hint = panel?.querySelector('[data-role="device-hint"]');
    if (!hint) return;
    const freshness = deviceTelemetryFreshness(device);
    const lastMessage = device.connected
        ? localized("Последнее сообщение", "Last message")
        : localized("Устройство отключено", "Device offline");
    hint.textContent = `${lastMessage}: ${device.last_seen || "—"} · ${freshness.label}`;
    hint.classList.toggle("telemetry-fresh", freshness.state === "fresh");
    hint.classList.toggle("telemetry-stale", freshness.state === "stale");
    hint.classList.toggle("telemetry-unknown", freshness.state === "unknown");
}

function updateDeviceFreshnessHints() {
    const panels = new Map(
        [...(deviceCard?.querySelectorAll(".device-panel") || [])]
            .map(panel => [panel.dataset.deviceId, panel])
    );
    for (const device of devices) {
        const panel = panels.get(device.device);
        if (panel) updateDeviceHint(device, panel);
    }
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
        const parameterId = input.dataset.parameterId;
        const definition = device.schema?.parameters?.[parameterId];
        const value = device.parameters?.[parameterId];
        input.disabled = !device.connected || Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) ||
            (input.type === "range" && typeof value !== "number");
        if (input.dataset.dirty === "true" || document.activeElement === input) {
            const preview = input.parentElement?.querySelector(
                '[data-role="parameter-input-preview"]'
            );
            if (preview && input.type === "range") {
                preview.textContent = formatDisplayedParameterValue(
                    Number(input.value), definition
                );
            }
            continue;
        }
        if (input.tagName === "SELECT" && input.dataset.controlType === "boolean-list") {
            input.value = value === undefined ? "" : String(value);
        } else {
            input.value = input.tagName === "SELECT"
                ? (value === undefined ? "" : JSON.stringify(value))
                : (value ?? (input.type === "range" ? definition?.min ?? "" : ""));
        }
        const preview = input.parentElement?.querySelector(
            '[data-role="parameter-input-preview"]'
        );
        if (preview) {
            preview.textContent = formatDisplayedParameterValue(value, definition);
        }
    }

    for (const select of panel.querySelectorAll('[data-role="parameter-control-style"]')) {
        const parameterId = select.dataset.parameterId;
        const preference = parameterUiPreferences?.[device.device]?.[parameterId] || "schema";
        select.value = [...select.options].some(option => option.value === preference)
            ? preference
            : "schema";
        select.disabled = !isGatewayConnected() || Boolean(device.pending_schema);
    }

    for (const button of panel.querySelectorAll('[data-role="parameter-command"]')) {
        const parameterId = button.dataset.parameterId;
        const definition = device.schema?.parameters?.[parameterId];
        const value = device.parameters?.[parameterId];
        const pendingCommand = findPendingCommand(device.device, "parameter", parameterId);
        const pending = Boolean(pendingCommand);
        const label = definition?.label || parameterId;
        const isOn = value === true;
        button.classList.toggle("is-on", isOn);
        button.setAttribute("aria-checked", String(isOn));
        const stateLabel = button.querySelector(".device-boolean-switch-label");
        if (stateLabel) {
            stateLabel.textContent = pending
                ? localized("Ожидание…", "Pending…")
                : isOn
                    ? localized("Вкл", "On")
                    : localized("Выкл", "Off");
        }
        button.title = pending
            ? pendingCommand?.status === "queued"
                ? localized(`Команда «${label}» в очереди`, `The ${label} command is queued`)
                : localized(`Ожидаем подтверждение: ${label}`, `Waiting for ${label} confirmation`)
            : isOn
                ? localized(`Выключить: ${label}`, `Turn off: ${label}`)
                : localized(`Включить: ${label}`, `Turn on: ${label}`);
        button.setAttribute("aria-label", pending
            ? button.title
            : localized(
                `${label}: ${isOn ? "включено" : "выключено"}. ${isOn ? "Выключить" : "Включить"}`,
                `${label}: ${isOn ? "on" : "off"}. Turn ${isOn ? "off" : "on"}`
            ));
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) || typeof value !== "boolean" || pending;
    }

    for (const button of panel.querySelectorAll('[data-role="parameter-apply"]')) {
        const input = button.parentElement?.querySelector('[data-role="parameter-input"]');
        const parameterValue = device.parameters?.[button.dataset.parameterId];
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) ||
            (input?.type === "range" && typeof parameterValue !== "number") ||
            hasPendingParameterCommand(device.device, button.dataset.parameterId);
    }

    for (const button of panel.querySelectorAll('[data-role="device-action"]')) {
        const pendingAction = findPendingCommand(device.device, "actionId", button.dataset.actionId);
        if (pendingAction) {
            const label = device.schema?.actions?.[button.dataset.actionId]?.label || button.dataset.actionId;
            button.textContent = pendingAction.status === "queued"
                ? localized(`В очереди: ${label}`, `Queued: ${label}`)
                : localized(`Ожидаем: ${label}…`, `Waiting: ${label}…`);
        } else {
            button.textContent = device.schema?.actions?.[button.dataset.actionId]?.label || button.dataset.actionId;
        }
        button.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) ||
            Boolean(pendingAction);
    }

    const identifyButton = panel.querySelector('[data-role="device-identify"]');
    if (identifyButton) {
        const pendingCommand = findPendingCommand(device.device, "actionId", "identify");
        const pending = Boolean(pendingCommand);
        const label = device.schema?.actions?.identify?.label ||
            localized("Найти устройство", "Find device");
        identifyButton.disabled = !device.connected || !isGatewayConnected() ||
            Boolean(device.pending_schema) || isDeviceConfigurationBusy(device) || pending;
        identifyButton.title = pending
            ? pendingCommand?.status === "queued"
                ? localized("Команда поиска в очереди", "Find command queued")
                : localized("Ищем устройство…", "Finding device…")
            : label;
        identifyButton.setAttribute("aria-label", identifyButton.title);
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

    updateDeviceHint(device, panel);
    renderOverview();
}

function renderOverview() {
    if (!overview) return;
    let online = 0;
    let favoriteCount = 0;
    for (const device of devices) {
        if (device.connected) online += 1;
        if (favoriteDeviceIds.has(device.device)) favoriteCount += 1;
    }
    const signature = JSON.stringify([
        devices.length, online, favoriteCount, deviceFilter, uiSettings.language
    ]);
    if (overviewRenderSignature === signature) return;
    overviewRenderSignature = signature;
    const metrics = [
        {
            value: String(devices.length),
            label: localized("Все устройства", "All devices"),
            filter: "all"
        },
        {
            value: String(online),
            label: localized("На связи", "Online"),
            filter: "online"
        },
        {
            value: String(Math.max(0, devices.length - online)),
            label: localized("Не в сети", "Offline"),
            filter: "offline"
        },
        {
            value: String(favoriteCount),
            label: localized("Избранное", "Pinned"),
            filter: "favorites"
        }
    ];
    overview.replaceChildren();
    for (const metric of metrics) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "overview-metric";
        item.dataset.deviceFilter = metric.filter;
        item.setAttribute("aria-pressed", String(deviceFilter === metric.filter));
        item.classList.toggle("is-active", deviceFilter === metric.filter);
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
        if (!command || typeof command !== "object" || Array.isArray(command)) {
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

        const automationId = typeof command.automation_id === "string" && command.automation_id.trim()
            ? command.automation_id
            : null;
        const scenario = automationId
            ? automations.find(item => item.automation_id === command.automation_id)
            : null;
        const sourceLabel = automationId
            ? `${localized("Автоматизация", "Automation")}${scenario?.config?.name ? ` «${scenario.config.name}»` : ""} · `
            : typeof command.command_id === "string" && command.command_id.startsWith("auto-")
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
            const scenario = change.source === "automation"
                ? automations.find(item => item.automation_id === change.automation_id)
                : null;
            const sourceLabel = change.source === "automation"
                ? `${localized("Автоматизация", "Automation")}${scenario?.config?.name ? ` «${scenario.config.name}»` : ""} · `
                : "";
            return `${sourceLabel}${label}: ${formatParameterValue(change.value, definition)}`;
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

    if (event.event_type === "configuration") {
        const restored = value.match(/Gateway восстановил аппаратную конфигурацию версии (\d+) из ESP32\.?/i);
        if (restored) {
            return localized(
                `Gateway восстановил аппаратную конфигурацию версии ${restored[1]} из ESP32`,
                `Gateway restored hardware configuration version ${restored[1]} from ESP32`
            );
        }
        const applied = value.match(/(?:Аппаратная конфигурация версии|Конфигурация версии) (\d+) (?:применена устройством|восстановлена из устройства)\.?/i);
        if (applied) {
            return localized(`Конфигурация версии ${applied[1]} применена`, `Configuration version ${applied[1]} applied`);
        }
        const saved = value.match(/Сохранена аппаратная конфигурация версии (\d+); статус: ([^.]+)\.?/i);
        if (saved) {
            const statuses = {
                pending: localized("ожидает подключения", "waiting for device"),
                applying: localized("отправлена устройству", "sent to device"),
                applied: localized("применена", "applied"),
                failed: localized("ошибка применения", "apply failed")
            };
            return localized(
                `Сохранена конфигурация версии ${saved[1]}: ${statuses[saved[2]] || saved[2]}`,
                `Configuration version ${saved[1]} saved: ${statuses[saved[2]] || saved[2]}`
            );
        }
        const failure = value.match(/Ошибка применения аппаратной конфигурации версии (\d+):?\s*(.*)/i);
        if (failure) {
            const detail = failure[2] ? `: ${failure[2]}` : "";
            return localized(`Ошибка конфигурации версии ${failure[1]}${detail}`, `Configuration version ${failure[1]} failed${detail}`);
        }
        return localized(`Настройка оборудования: ${value}`, `Hardware configuration: ${value}`);
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

function getAutomationEventSource(event) {
    if (!(["parameter", "command"].includes(event.event_type))) return null;
    try {
        const data = JSON.parse(event.event_value || "{}");
        if (event.event_type === "parameter") {
            return data.source === "automation" ? data : null;
        }
        return typeof data.automation_id === "string" && data.automation_id.trim() &&
            typeof data.run_id === "string" && data.run_id.trim()
            ? { automation_id: data.automation_id, run_id: data.run_id }
            : null;
    } catch (error) {
        return null;
    }
}

function createCommandEchoIndex(eventList) {
    const index = new Map();
    for (const event of eventList) {
        if (!isVisibleEvent(event) || event.event_type !== "command") continue;
        try {
            const command = JSON.parse(event.event_value || "{}");
            const value = command.result_value ?? command.requested_value;
            const time = Date.parse(event.timestamp);
            if (command.status !== "succeeded" || !command.parameter ||
                command.command_id?.startsWith("auto-") || !Number.isFinite(time) ||
                (value !== null && typeof value === "object")) continue;
            const key = JSON.stringify([event.device_id, command.parameter, typeof value, value]);
            if (!index.has(key)) index.set(key, []);
            index.get(key).push({ id: event.id, time });
        } catch (error) {
            // Historical events may contain a legacy, non-JSON payload.
        }
    }
    for (const commands of index.values()) commands.sort((left, right) => left.time - right.time);
    return index;
}

function isCommandEchoEvent(event, commandIndex) {
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
            if (change.source === "automation") return false;
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

    const key = JSON.stringify([event.device_id, parameter, typeof value, value]);
    const commands = commandIndex.get(key) || [];
    let left = 0;
    let right = commands.length;
    while (left < right) {
        const middle = (left + right) >>> 1;
        if (commands[middle].time < stateTime - 5000) left = middle + 1;
        else right = middle;
    }
    for (let index = left; index < commands.length && commands[index].time <= stateTime + 5000; index += 1) {
        const command = commands[index];
        if (!Number.isFinite(command.id) || !Number.isFinite(event.id) || command.id > event.id) return true;
    }
    return false;
}

// =========================
// ИСТОРИЯ СОБЫТИЙ
// =========================

function eventMatchesSearch(event, query) {
    if (!isVisibleEvent(event)) return false;
    const normalizedQuery = String(query || "").trim().toLocaleLowerCase();
    if (!normalizedQuery) return true;
    const device = devices.find(item => item.device === event.device_id);
    const searchableText = [
        event.device_id,
        event.event_type,
        event.event_value,
        event.timestamp,
        device?.name,
        device?.display_name,
        JSON.stringify(device?.schema || {}),
        getEventDescription(event)
    ].map(value => String(value ?? "")).join(" ").toLocaleLowerCase();
    return searchableText.includes(normalizedQuery);
}

function exportEventsCsv(eventList) {
    const columns = [
        localized("ID", "ID"),
        localized("Дата и время", "Date and time"),
        localized("Устройство", "Device"),
        localized("Тип", "Type"),
        localized("Описание", "Description"),
        localized("Значение", "Value")
    ];
    const escapeCell = value => {
        let text = String(value ?? "");
        if (/^[=+@\-\t\r]/.test(text)) text = `'${text}`;
        return `"${text.replaceAll('"', '""')}"`;
    };
    const lines = [columns, ...eventList.filter(isVisibleEvent).map(event => [
        event.id,
        event.timestamp,
        event.device_id,
        event.event_type,
        getEventDescription(event),
        event.event_value
    ])].map(row => row.map(escapeCell).join(","));
    const blob = new Blob([`\uFEFF${lines.join("\r\n")}`], {
        type: "text/csv;charset=utf-8"
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `gateway-events-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function renderEvents() {
    if (!activityCard) {
        console.error("Не найден элемент .activity-card");
        return;
    }

    const searchWasFocused = document.activeElement?.classList.contains("event-search-input");
    const oldSearchInput = document.activeElement;
    const searchSelectionStart = searchWasFocused ? oldSearchInput.selectionStart : null;
    const searchSelectionEnd = searchWasFocused ? oldSearchInput.selectionEnd : null;

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

    const controls = document.createElement("div");
    controls.className = "event-controls";

    const searchLabel = document.createElement("label");
    searchLabel.className = "event-search-label";
    const searchCaption = document.createElement("span");
    searchCaption.textContent = localized("Поиск по истории", "Search history");
    const searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.className = "event-search-input";
    searchInput.maxLength = 120;
    searchInput.value = eventSearchQuery;
    searchInput.placeholder = localized(
        "Устройство, событие или текст…",
        "Device, event type or text…"
    );
    searchInput.setAttribute("aria-label", localized("Поиск по истории событий", "Search event history"));
    searchInput.addEventListener("input", () => {
        eventSearchQuery = searchInput.value.slice(0, 120);
        clearSearch.disabled = !eventSearchQuery.trim();
        events = [];
        eventCursor = null;
        eventMatchCount = null;
        eventsHasMore = false;
        if (eventSearchTimer !== null) window.clearTimeout(eventSearchTimer);
        eventSearchTimer = window.setTimeout(() => {
            eventSearchTimer = null;
            requestEvents({ reset: true });
        }, 350);
    });
    searchLabel.append(searchCaption, searchInput);

    const searchActions = document.createElement("div");
    searchActions.className = "event-search-actions";
    const clearSearch = document.createElement("button");
    clearSearch.type = "button";
    clearSearch.className = "event-tool-button";
    clearSearch.textContent = localized("Очистить поиск", "Clear search");
    clearSearch.disabled = !eventSearchQuery.trim();
    clearSearch.addEventListener("click", () => {
        eventSearchQuery = "";
        events = [];
        eventCursor = null;
        eventMatchCount = null;
        eventsHasMore = false;
        if (eventSearchTimer !== null) window.clearTimeout(eventSearchTimer);
        eventSearchTimer = null;
        requestEvents({ reset: true });
    });
    const exportButton = document.createElement("button");
    exportButton.type = "button";
    exportButton.className = "event-tool-button";
    exportButton.textContent = localized("Экспорт CSV", "Export CSV");
    exportButton.disabled = events.length === 0;
    exportButton.title = localized(
        "Скачать загруженные записи с учётом выбранного фильтра",
        "Download loaded records using the selected filters"
    );
    searchActions.append(clearSearch, exportButton);
    controls.append(searchLabel, searchActions);
    container.appendChild(controls);

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
        eventMatchCount = null;
        eventsHasMore = false;
        requestEvents({ reset: true });
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
        { value: "schema", label: localized("Обновления Schema", "Schema updates") },
        { value: "configuration", label: localized("Конфигурации", "Configurations") }
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
    const visibleEvents = events.filter(isVisibleEvent);
    const commandEchoIndex = createCommandEchoIndex(visibleEvents);

    for (const event of visibleEvents) {
        if (String(event.id) !== pendingEventFocusId && isCommandEchoEvent(event, commandEchoIndex)) {
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
    const legacyCounts = eventLegacyVisibilityCounts || visibleEvents.length !== events.length;
    const totalMatchedCount = legacyCounts ? (eventsHasMore ? null : visibleEvents.length) : eventMatchCount;
    const totalMatchedText = totalMatchedCount === null
        ? "—"
        : new Intl.NumberFormat(uiSettings.language === "en" ? "en-US" : "ru-RU").format(totalMatchedCount);
    summary.textContent = uiSettings.language === "en"
        ? `Matches in history: ${totalMatchedText} · Loaded: ${deduplicatedEvents.length} · Shown by type: ${filteredEvents.length}`
        : `Найдено в истории: ${totalMatchedText} · Загружено: ${deduplicatedEvents.length} · Показано по типу: ${filteredEvents.length}`;

    container.appendChild(summary);

    exportButton.disabled = filteredEvents.length === 0;
    exportButton.addEventListener("click", () => exportEventsCsv(filteredEvents));

    const restoreSearchFocus = () => {
        if (!searchWasFocused) return;
        searchInput.focus({ preventScroll: true });
        if (Number.isInteger(searchSelectionStart) && Number.isInteger(searchSelectionEnd)) {
            searchInput.setSelectionRange(searchSelectionStart, searchSelectionEnd);
        }
    };

    if (filteredEvents.length === 0) {
        const empty = document.createElement("p");
        empty.className = "event-empty";

        empty.textContent = visibleEvents.length === 0
            ? localized("Событий пока нет.", "No events yet.")
            : localized("Нет событий для выбранного фильтра.", "No events match this filter.");

        container.appendChild(empty);
        renderLoadMoreButton(container);
        restoreSearchFocus();
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
        } else if (event.event_type === "configuration") {
            typeBadge.textContent = localized("Конфигурация", "Configuration");
        } else {
            typeBadge.textContent = event.event_type || localized("Событие", "Event");
        }

        const side = document.createElement("div");
        side.className = "event-entry-side";
        side.appendChild(typeBadge);
        top.append(deviceName, side);

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

        const automationSource = getAutomationEventSource(event);
        if (automationSource?.automation_id && automationSource?.run_id) {
            const scenario = automations.find(
                item => item.automation_id === automationSource.automation_id
            );
            const openRun = document.createElement("button");
            openRun.type = "button";
            openRun.className = "event-automation-link";
            const openRunLabel = scenario?.config?.name
                ? localized(
                    `Открыть запуск «${scenario.config.name}»`,
                    `Open run “${scenario.config.name}”`
                )
                : localized("Открыть запуск сценария", "Open scenario run");
            openRun.textContent = ">>";
            openRun.title = openRunLabel;
            openRun.setAttribute("aria-label", openRunLabel);
            openRun.addEventListener("click", () => openAutomationRunFromEvent(
                automationSource.automation_id,
                automationSource.run_id
            ));
            side.appendChild(openRun);
        }

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
    restoreSearchFocus();
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

    if (isDeviceConfigurationBusy(device)) {
        setActivity(localized("Дождитесь подтверждения аппаратной конфигурации от ESP32.", "Wait for ESP32 to confirm the hardware configuration."));
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
            empty.textContent = localized("Не указывать", "Not set");
            input.appendChild(empty);
        }
        for (const value of [true, false]) {
            const option = document.createElement("option");
            option.value = String(value);
            option.textContent = value ? localized("Да", "Yes") : localized("Нет", "No");
            input.appendChild(option);
        }
    } else if (definition.type === "enum") {
        if (!required) {
            const empty = document.createElement("option");
            empty.value = "";
            empty.textContent = localized("Не указывать", "Not set");
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

function downloadEmbeddedDeviceConfiguration(configuration, version) {
    const canonical = canonicalJson(configuration);
    const header = [
        "#pragma once",
        "",
        "// Generated by IoT Gateway. Copy this file to esp32/universal/include/.",
        `#define UNIVERSAL_GENERATED_CONFIGURATION_VERSION ${version}UL`,
        `#define UNIVERSAL_GENERATED_CONFIGURATION_JSON ${JSON.stringify(canonical)}`,
        ""
    ].join("\n");
    const blob = new Blob([header], { type: "text/x-c++hdr;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "device_configuration.generated.h";
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function openDeviceConfigurationDialog(device) {
    if (deviceConfigurationDialog) {
        if (deviceConfigurationDialog.configurationEditor?.canClose() === false) return;
        deviceConfigurationDialog.close();
    }
    const currentDevice = () => devices.find(item => item.device === device.device) || device;
    let fieldContext = "";
    const fieldNodes = new Map();
    let fieldSequence = 0;
    const fieldPrefix = createCommandId();
    let validationTimer = null;
    let validationTimeout = null;
    let operationTimeout = null;
    let validationRequestId = null;
    let validationSignature = null;
    let validatedSignature = null;
    let serverErrors = [];
    let savingRequestId = null;
    let savingSignature = null;
    let retryRequestId = null;
    let saveAfterValidation = null;
    let baselineSignature = null;
    let localErrors = [];

    const drivers = Array.isArray(device.driver_catalog) ? device.driver_catalog : [];
    const components = JSON.parse(JSON.stringify(
        device.hardware_configuration?.components || []
    ));
    let identifyIndicator = device.hardware_configuration?.identify_indicator
        ? JSON.parse(JSON.stringify(device.hardware_configuration.identify_indicator))
        : null;
    for (const component of components) {
        component.ui_control = parameterUiPreferences?.[device.device]?.[component.parameter_id] || "schema";
        if (component.driver === "analog_input") {
            component.raw_min ??= 0;
            component.raw_max ??= 4095;
            component.value_min ??= 0;
            component.value_max ??= 4095;
        }
        if (component.driver === "pwm_output") {
            component.min_value ??= 0;
            component.max_value ??= 255;
        }
    }
    const dialog = document.createElement("dialog");
    dialog.className = "action-dialog device-config-dialog";
    const form = document.createElement("form");
    form.className = "action-dialog-form device-config-form";
    form.addEventListener("submit", event => event.preventDefault());

    const heading = document.createElement("h3");
    heading.textContent = localized("Конфигурация оборудования", "Hardware configuration");
    const intro = document.createElement("p");
    intro.textContent = localized(
        "Выбирайте только драйверы и GPIO, объявленные прошивкой. Конфигурация хранится в Gateway и применяется после подтверждения ESP32.",
        "Choose only drivers and GPIO pins advertised by the firmware. Gateway stores the configuration and waits for the ESP32 to confirm it."
    );
    const status = document.createElement("p");
    status.className = "device-config-status";
    status.setAttribute("aria-live", "polite");
    const savedDetails = document.createElement("dl");
    savedDetails.className = "device-config-saved-details";
    const progress = document.createElement("progress");
    progress.className = "device-config-progress";
    progress.setAttribute("aria-label", localized("Применение конфигурации", "Applying configuration"));
    progress.hidden = true;
    const draftStatus = document.createElement("p");
    draftStatus.className = "device-config-draft-status";
    const validationStatus = document.createElement("p");
    validationStatus.className = "device-config-validation";
    validationStatus.setAttribute("aria-live", "polite");
    const validationErrorsList = document.createElement("ul");
    validationErrorsList.className = "device-config-errors";
    validationErrorsList.hidden = true;
    const componentList = document.createElement("div");
    componentList.className = "device-config-components";
    const toolbar = document.createElement("div");
    toolbar.className = "device-config-toolbar";
    const add = document.createElement("button");
    add.type = "button";
    add.className = "event-tool-button";
    add.textContent = localized("Добавить компонент", "Add component");
    add.disabled = drivers.length === 0;
    const presets = [
        { id: "led", driver: "gpio_output", label: localized("Светодиод", "LED"), parameter: localized("Включён", "Enabled") },
        { id: "relay", driver: "gpio_output", label: localized("Реле", "Relay"), parameter: localized("Включено", "Enabled") },
        { id: "button", driver: "digital_input", label: localized("Кнопка", "Button"), parameter: localized("Сигнал кнопки", "Button signal") },
        { id: "sensor", driver: "analog_input", label: localized("Аналоговый датчик (ADC)", "Analog sensor (ADC)"), parameter: localized("Показание датчика", "Sensor reading") },
        { id: "brightness", driver: "pwm_output", label: localized("Яркость (PWM)", "Brightness (PWM)"), parameter: localized("Яркость", "Brightness") }
    ].filter(preset => drivers.some(driver => driver.id === preset.driver));
    const addRow = document.createElement("div");
    addRow.className = "device-config-add-row";
    const presetLabel = document.createElement("label");
    presetLabel.className = "device-config-preset";
    presetLabel.textContent = localized("Добавить из шаблона", "Add from a preset");
    const presetSelect = document.createElement("select");
    presetSelect.setAttribute("aria-label", presetLabel.textContent);
    for (const preset of presets) {
        const option = document.createElement("option");
        option.value = preset.id;
        option.textContent = preset.label;
        presetSelect.append(option);
    }
    presetSelect.disabled = presets.length === 0;
    presetLabel.append(presetSelect);
    addRow.append(presetLabel, add);
    const exportButton = document.createElement("button");
    exportButton.type = "button";
    exportButton.className = "event-tool-button";
    exportButton.textContent = localized("Экспорт JSON", "Export JSON");
    const savedExportButton = document.createElement("button");
    savedExportButton.type = "button";
    savedExportButton.className = "event-tool-button";
    savedExportButton.textContent = localized("Скачать Schema из Gateway", "Download Schema from Gateway");
    const firmwareButton = document.createElement("button");
    firmwareButton.type = "button";
    firmwareButton.className = "event-tool-button";
    firmwareButton.textContent = localized("Файл для прошивки (.h)", "Firmware file (.h)");
    const importLabel = document.createElement("label");
    importLabel.className = "event-tool-button device-config-import";
    importLabel.textContent = localized("Импорт JSON", "Import JSON");
    const importInput = document.createElement("input");
    importInput.type = "file";
    importInput.accept = "application/json,.json";
    importLabel.appendChild(importInput);
    toolbar.append(exportButton, savedExportButton, firmwareButton, importLabel);
    const files = document.createElement("details");
    files.className = "device-config-files";
    const filesSummary = document.createElement("summary");
    filesSummary.textContent = localized("Файлы и перенос конфигурации", "Files and configuration transfer");
    files.append(filesSummary, toolbar);
    const preview = document.createElement("section");
    preview.className = "device-config-preview";
    preview.setAttribute("aria-label", localized("Предпросмотр карточки устройства", "Device card preview"));

    const firmwareHelp = document.createElement("p");
    firmwareHelp.className = "device-config-firmware-help";
    firmwareHelp.textContent = localized(
        "Файл .h можно добавить в esp32/universal/include и прошить устройство с этой конфигурацией. Для обычных изменений это не требуется: Gateway передаёт конфигурацию устройству сам.",
        "You can add the .h file to esp32/universal/include and flash the device with this configuration. Regular changes do not need reflashing; Gateway can apply them directly."
    );
    const schemaLocation = document.createElement("p");
    schemaLocation.className = "device-config-firmware-help";
    schemaLocation.textContent = localized(
        `JSON-копия Schema хранится в schemas/${device.device}.json рядом с базой Gateway. Кнопка «Скачать Schema из Gateway» загружает именно сохранённый файл; «Экспорт JSON» выгружает текущий черновик.`,
        `The saved Schema is stored at schemas/${device.device}.json next to the Gateway database. “Download Schema from Gateway” retrieves that saved file; “Export JSON” exports the current draft.`
    );

    const feedback = document.createElement("p");
    feedback.className = "device-config-feedback";
    feedback.setAttribute("aria-live", "polite");

    const buttons = document.createElement("div");
    buttons.className = "action-dialog-buttons device-config-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "device-config-save";
    save.textContent = localized("Сохранить и применить", "Save and apply");
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "device-config-retry";
    retry.textContent = localized("Повторить сохранённую", "Retry saved configuration");
    retry.title = localized("Отправляет сохранённую версию. Изменения в черновике сохраняются отдельно кнопкой слева.", "Resends the saved version. Save draft changes with the first button.");
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = localized("Вернуть сохранённую", "Revert to saved");
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary-button";
    cancel.textContent = localized("Закрыть", "Close");
    cancel.addEventListener("click", () => { if (canClose()) dialog.close(); });
    buttons.append(save, retry, reset, cancel);

    function field(labelText, value, type, onChange, options = {}) {
        const label = document.createElement("label");
        label.className = "device-config-field";
        const caption = document.createElement("span");
        caption.textContent = labelText;
        const input = type === "select"
            ? document.createElement("select")
            : document.createElement("input");
        if (type !== "select") input.type = type;
        if (options.options) {
            for (const optionData of options.options) {
                const option = document.createElement("option");
                option.value = String(optionData.value);
                option.textContent = optionData.label;
                option.disabled = optionData.disabled === true;
                input.appendChild(option);
            }
        }
        input.value = value == null ? "" : String(value);
        if (options.required) input.required = true;
        if (options.maxLength) input.maxLength = options.maxLength;
        if (options.pattern) input.pattern = options.pattern;
        if (options.min !== undefined) input.min = String(options.min);
        if (options.max !== undefined) input.max = String(options.max);
        if (options.step !== undefined) input.step = String(options.step);
        if (type === "select" && options.required && !options.boolean) {
            const empty = document.createElement("option");
            empty.value = "";
            empty.textContent = localized("Выберите значение…", "Choose a value…");
            input.prepend(empty);
            input.value = value == null ? "" : String(value);
        }
        const path = options.path || (options.key ? `${fieldContext}.${options.key}` : null);
        const error = document.createElement("small");
        error.className = "device-config-field-error";
        error.id = `${fieldPrefix}-error-${++fieldSequence}`;
        error.hidden = true;
        input.setAttribute("aria-describedby", error.id);
        if (path) {
            input.dataset.configPath = path;
            fieldNodes.set(path, { input, error });
        }
        input.addEventListener(type === "select" ? "change" : "input", () => {
            let nextValue = input.value;
            if (options.number) nextValue = input.value === "" ? null : Number(input.value);
            if (options.boolean) nextValue = input.value === "true";
            onChange(nextValue);
            draftChanged();
        });
        label.append(caption, input);
        const helpText = options.help || fieldHint(options.key);
        if (helpText) {
            const help = document.createElement("small");
            help.className = "device-config-field-help";
            help.textContent = helpText;
            label.append(help);
        }
        label.append(error);
        return label;
    }

    function defaultComponent(index, driver) {
        let nextId = Math.max(1, index + 1);
        while (components.some(component =>
            component.id === `component_${nextId}` ||
            component.parameter_id === `value_${nextId}`
        )) nextId += 1;
        const pin = driver.pins?.find(candidate =>
            !components.some(component => component.pin === candidate) &&
            identifyIndicator?.pin !== candidate
        ) ?? null;
        return {
            id: `component_${nextId}`,
            driver: driver.id,
            label: driver.label,
            parameter_id: `value_${nextId}`,
            parameter_label: driver.label,
            ui_control: "schema",
            pin,
            ...(driver.id === "gpio_output" ? { active_high: true, initial_value: false } : {}),
            ...(driver.id === "digital_input" ? { pull: "up", interval_ms: 1000 } : {}),
            ...(driver.id === "analog_input"
                ? {
                    interval_ms: 1000, unit: "raw",
                    raw_min: 0, raw_max: 4095,
                    value_min: 0, value_max: 4095
                }
                : {}),
            ...(driver.id === "pwm_output"
                ? { initial_value: 0, min_value: 0, max_value: 255 }
                : {})
        };
    }

    function renderComponents() {
        componentList.replaceChildren();
        fieldNodes.clear();
        components.forEach((component, index) => {
            fieldContext = `components.${index}`;
            const driver = drivers.find(item => item.id === component.driver) || drivers[0];
            if (!driver) return;
            const section = document.createElement("fieldset");
            section.className = "device-config-component";
            const legend = document.createElement("legend");
            legend.textContent = `${localized("Компонент", "Component")} ${index + 1}`;
            section.appendChild(legend);
            const fields = document.createElement("div");
            fields.className = "device-config-fields";
            fields.appendChild(field(
                localized("Драйвер", "Driver"),
                component.driver,
                "select",
                value => {
                    const replacement = defaultComponent(index, drivers.find(item => item.id === value) || driver);
                    replacement.label = component.label;
                    replacement.id = component.id;
                    replacement.parameter_id = component.parameter_id;
                    replacement.parameter_label = component.parameter_label;
                    components[index] = replacement;
                    renderComponents();
                },
                { key: "driver", required: true, options: drivers.map(item => ({ value: item.id, label: driverLabel(item.id) })) }
            ));
            fields.appendChild(field(
                localized("Логический ID", "Component ID"), component.id, "text",
                value => { component.id = value.trim(); }, {
                    key: "id", required: true, maxLength: 64,
                    pattern: "[A-Za-z][A-Za-z0-9_.-]{0,63}"
                }
            ));
            fields.appendChild(field(
                localized("Название компонента", "Component label"), component.label, "text",
                value => { component.label = value; }, { key: "label", required: true, maxLength: 80 }
            ));
            fields.appendChild(field(
                localized("GPIO", "GPIO pin"), component.pin, "select",
                value => { component.pin = value === "" ? null : Number(value); },
                { key: "pin", required: true, options: driver.pins.map(pin => ({
                    value: pin,
                    label: `GPIO ${pin}`,
                    disabled: components.some((other, otherIndex) =>
                        otherIndex !== index && Number(other.pin) === pin
                    ) || identifyIndicator?.pin === pin
                })) }
            ));
            fields.appendChild(field(
                localized("ID параметра", "Parameter ID"), component.parameter_id, "text",
                value => { component.parameter_id = value.trim(); }, {
                    key: "parameter_id", required: true, maxLength: 64,
                    pattern: "[A-Za-z][A-Za-z0-9_.-]{0,63}"
                }
            ));
            fields.appendChild(field(
                localized("Название параметра", "Parameter label"), component.parameter_label, "text",
                value => { component.parameter_label = value; }, { key: "parameter_label", required: true, maxLength: 80 }
            ));

            if (component.driver === "gpio_output") {
                fields.appendChild(field(
                    localized("Отображение", "Control style"),
                    component.ui_control || "schema",
                    "select",
                    value => { component.ui_control = value; },
                    { path: `ui_preferences.${component.parameter_id}`, key: "ui_control", options: [
                        { value: "schema", label: localized("Автоматически", "Automatic") },
                        { value: "toggle", label: localized("Переключатель", "Switch") },
                        { value: "list", label: localized("Список", "List") }
                    ] }
                ));
            } else if (component.driver === "pwm_output") {
                fields.appendChild(field(
                    localized("Минимальное значение", "Minimum value"),
                    component.min_value ?? 0,
                    "number",
                    value => { component.min_value = value; },
                    { key: "min_value", required: true, number: true, min: 0, max: 254, step: 1 }
                ));
                fields.appendChild(field(
                    localized("Максимальное значение", "Maximum value"),
                    component.max_value ?? 255,
                    "number",
                    value => { component.max_value = value; },
                    { key: "max_value", required: true, number: true, min: 1, max: 255, step: 1 }
                ));
                fields.appendChild(field(
                    localized("Отображение", "Control style"),
                    component.ui_control || "schema",
                    "select",
                    value => { component.ui_control = value; },
                    { path: `ui_preferences.${component.parameter_id}`, key: "ui_control", options: [
                        { value: "schema", label: localized("Автоматически", "Automatic") },
                        { value: "slider", label: localized("Ползунок", "Slider") },
                        { value: "input", label: localized("Ввод числа", "Number input") }
                    ] }
                ));
            } else if (component.driver === "analog_input") {
                fields.appendChild(field(
                    localized("ADC: raw минимум", "ADC raw minimum"),
                    component.raw_min ?? 0,
                    "number",
                    value => { component.raw_min = value; },
                    { key: "raw_min", required: true, number: true, min: 0, max: 4094, step: 1 }
                ));
                fields.appendChild(field(
                    localized("ADC: raw максимум", "ADC raw maximum"),
                    component.raw_max ?? 4095,
                    "number",
                    value => { component.raw_max = value; },
                    { key: "raw_max", required: true, number: true, min: 1, max: 4095, step: 1 }
                ));
                fields.appendChild(field(
                    localized("Значение минимум", "Value minimum"),
                    component.value_min ?? 0,
                    "number",
                    value => { component.value_min = value; },
                    { key: "value_min", required: true, number: true, min: -1000000, max: 999999, step: 1 }
                ));
                fields.appendChild(field(
                    localized("Значение максимум", "Value maximum"),
                    component.value_max ?? 4095,
                    "number",
                    value => { component.value_max = value; },
                    { key: "value_max", required: true, number: true, min: -999999, max: 1000000, step: 1 }
                ));
                fields.appendChild(field(
                    localized("Единица измерения", "Unit"), component.unit, "text",
                    value => { component.unit = value; }, { key: "unit", maxLength: 24 }
                ));
            }

            if (component.driver === "gpio_output") {
                fields.appendChild(field(
                    localized("Активный уровень", "Active level"), component.active_high,
                    "select", value => { component.active_high = value; },
                    { key: "active_high", boolean: true, options: [
                        { value: true, label: "HIGH" },
                        { value: false, label: "LOW" }
                    ] }
                ));
                fields.appendChild(field(
                    localized("Начальное состояние", "Initial state"), component.initial_value,
                    "select", value => { component.initial_value = value; },
                    { key: "initial_value", boolean: true, options: [
                        { value: false, label: localized("Выключено", "Off") },
                        { value: true, label: localized("Включено", "On") }
                    ] }
                ));
            } else if (component.driver === "digital_input") {
                fields.appendChild(field(
                    localized("Подтяжка входа", "Input pull"), component.pull, "select",
                    value => { component.pull = value; },
                    { key: "pull", options: [
                        { value: "none", label: localized("Нет", "None") },
                        { value: "up", label: "Pull-up" },
                        { value: "down", label: "Pull-down" }
                    ] }
                ));
                fields.appendChild(field(
                    localized("Опрос, мс", "Polling interval, ms"), component.interval_ms,
                    "number", value => { component.interval_ms = value; },
                    { key: "interval_ms", number: true, min: 100, max: 60000, step: 1, required: true }
                ));
            } else if (component.driver === "analog_input") {
                fields.appendChild(field(
                    localized("Опрос, мс", "Polling interval, ms"), component.interval_ms,
                    "number", value => { component.interval_ms = value; },
                    { key: "interval_ms", number: true, min: 250, max: 60000, step: 1, required: true }
                ));
            } else if (component.driver === "pwm_output") {
                fields.appendChild(field(
                    localized("Начальное значение", "Initial value"),
                    component.initial_value, "number",
                    value => { component.initial_value = value; },
                    { key: "initial_value", number: true, min: 0, max: 255, step: 1, required: true }
                ));
            }

            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "schema-delete-button";
            remove.textContent = localized("Удалить компонент", "Remove component");
            remove.addEventListener("click", () => {
                components.splice(index, 1);
                renderComponents();
            });
            const driverHelp = document.createElement("p");
            driverHelp.className = "device-config-driver-help";
            driverHelp.textContent = driverDescription(component.driver);
            section.append(driverHelp, fields, remove);
            componentList.appendChild(section);
        });
        add.disabled = drivers.length === 0 || components.length >= 8;
        if (!components.length) {
            const empty = document.createElement("p");
            empty.className = "device-config-empty";
            empty.textContent = localized(
                "Компоненты не добавлены. В пустой конфигурации прошивка не управляет GPIO.",
                "No components are configured. With an empty configuration, the firmware will not drive GPIO pins."
            );
            componentList.appendChild(empty);
        }

        const identifySection = document.createElement("fieldset");
        fieldContext = "identify_indicator";
        identifySection.className = "device-config-component device-config-identify";
        const identifyLegend = document.createElement("legend");
        identifyLegend.textContent = localized("Индикатор для «Найти устройство»", "Indicator for Find device");
        identifySection.appendChild(identifyLegend);
        const identifyFields = document.createElement("div");
        identifyFields.className = "device-config-fields";
        identifyFields.appendChild(field(
            localized("Тип индикатора", "Indicator type"),
            identifyIndicator?.driver || "none",
            "select",
            value => {
                const freeGpioPin = drivers.find(item => item.id === "gpio_output")?.pins
                    ?.find(pin => !components.some(component => Number(component.pin) === pin));
                const freeNeoPixelPin = [38, 48].find(pin =>
                    !components.some(component => Number(component.pin) === pin)
                );
                identifyIndicator = value === "gpio"
                    ? { driver: "gpio", pin: freeGpioPin ?? null, active_high: true, built_in: false }
                    : value === "neopixel"
                        ? { driver: "neopixel", pin: freeNeoPixelPin ?? null, brightness: 32, built_in: true }
                        : null;
                renderComponents();
            },
            { key: "driver", options: [
                { value: "none", label: localized("Не использовать", "Not used") },
                { value: "gpio", label: localized("Обычный светодиод на GPIO", "Regular GPIO LED") },
                { value: "neopixel", label: localized("Адресный RGB (NeoPixel)", "Addressable RGB (NeoPixel)") }
            ] }
        ));
        if (identifyIndicator?.driver === "gpio") {
            const gpioPins = drivers.find(item => item.id === "gpio_output")?.pins || [];
            identifyFields.appendChild(field(
                localized("GPIO индикатора", "Indicator GPIO"),
                identifyIndicator.pin ?? "",
                "select",
                value => { identifyIndicator.pin = value === "" ? null : Number(value); },
                { key: "pin", required: true, options: gpioPins.map(pin => ({
                    value: pin,
                    label: `GPIO ${pin}`,
                    disabled: components.some(component => Number(component.pin) === pin)
                })) }
            ));
            identifyFields.appendChild(field(
                localized("Активный уровень", "Active level"),
                identifyIndicator.active_high,
                "select",
                value => { identifyIndicator.active_high = value; },
                { key: "active_high", boolean: true, options: [
                    { value: true, label: "HIGH" },
                    { value: false, label: "LOW" }
                ] }
            ));
        } else if (identifyIndicator?.driver === "neopixel") {
            identifyFields.appendChild(field(
                localized("Пин адресного индикатора", "Addressable LED pin"),
                identifyIndicator.pin ?? "",
                "select",
                value => { identifyIndicator.pin = value === "" ? null : Number(value); },
                { key: "pin", required: true, options: [38, 48].map(pin => ({
                    value: pin,
                    label: `GPIO ${pin} (${pin === 48 ? "ESP32-S3 v1.0" : "ESP32-S3 v1.1"})`,
                    disabled: components.some(component => Number(component.pin) === pin)
                })) }
            ));
            identifyFields.appendChild(field(
                localized("Яркость", "Brightness"),
                identifyIndicator.brightness ?? 32,
                "number",
                value => { identifyIndicator.brightness = value; },
                { key: "brightness", number: true, min: 1, max: 255, step: 1, required: true }
            ));
        }
        if (identifyIndicator) {
            identifyFields.appendChild(field(
                localized("Считать встроенным индикатором", "Mark as built-in indicator"),
                identifyIndicator.built_in ?? identifyIndicator.driver === "neopixel",
                "select",
                value => { identifyIndicator.built_in = value; },
                { key: "built_in", boolean: true, options: [
                    { value: true, label: localized("Да", "Yes") },
                    { value: false, label: localized("Нет", "No") }
                ] }
            ));
        }
        const identifyHelp = document.createElement("p");
        identifyHelp.className = "device-config-firmware-help";
        identifyHelp.textContent = localized(
            "Пин выбирается вручную: прошивка не может определить, где физически подключён индикатор. GPIO 38/48 зарезервированы для адресного индикатора ESP32-S3 и зависят от ревизии платы.",
            "Choose the pin manually; firmware cannot detect the indicator wiring. GPIO 38/48 are reserved for the ESP32-S3 addressable LED and depend on board revision."
        );
        identifySection.append(identifyFields, identifyHelp);
        componentList.appendChild(identifySection);
        draftChanged();
    }

    function configurationFromDraft() {
        const configuration = {
            format_version: 1,
            components: components.map(component => {
                const base = {
                    id: String(component.id || "").trim(),
                    driver: component.driver,
                    label: String(component.label || "").trim(),
                    parameter_id: String(component.parameter_id || "").trim(),
                    parameter_label: String(component.parameter_label || "").trim(),
                    pin: component.pin
                };
                if (component.driver === "gpio_output") {
                    base.active_high = component.active_high;
                    base.initial_value = component.initial_value;
                } else if (component.driver === "digital_input") {
                    base.pull = component.pull;
                    base.interval_ms = component.interval_ms;
                } else if (component.driver === "analog_input") {
                    base.interval_ms = component.interval_ms;
                    base.unit = String(component.unit || "").trim() || "raw";
                    base.raw_min = component.raw_min ?? 0;
                    base.raw_max = component.raw_max ?? 4095;
                    base.value_min = component.value_min ?? 0;
                    base.value_max = component.value_max ?? 4095;
                } else if (component.driver === "pwm_output") {
                    base.initial_value = component.initial_value;
                    base.min_value = component.min_value ?? 0;
                    base.max_value = component.max_value ?? 255;
                }
                return base;
            })
        };
        if (identifyIndicator) {
            configuration.identify_indicator = { ...identifyIndicator };
        }
        return configuration;
    }

    function uiPreferencesFromDraft() {
        const preferences = {};
        for (const component of components) {
            if (!["gpio_output", "pwm_output"].includes(component.driver)) continue;
            preferences[component.parameter_id] = component.ui_control || "schema";
        }
        return preferences;
    }

    function driverLabel(id) {
        const labels = {
            gpio_output: localized("Цифровой выход", "Digital output"),
            digital_input: localized("Цифровой вход", "Digital input"),
            analog_input: localized("Аналоговый вход (ADC)", "Analog input (ADC)"),
            pwm_output: localized("PWM-выход", "PWM output")
        };
        return labels[id] || drivers.find(driver => driver.id === id)?.label || id;
    }

    function driverDescription(id) {
        const descriptions = {
            gpio_output: localized("Для обычного светодиода или управляющего входа реле. Выберите GPIO и проверьте, какой уровень включает компонент: HIGH или LOW.", "For a regular LED or relay control input. Choose a GPIO and the active HIGH or LOW level."),
            digital_input: localized("Считывает электрический уровень 0/1. При Pull-up кнопка между GPIO и GND даёт 0 при нажатии. Интервал опроса определяет, какие нажатия успеет заметить устройство.", "Reads the electrical 0/1 level. With pull-up, a button between GPIO and GND reads 0 when pressed. The polling interval determines which presses the device can detect."),
            analog_input: localized("Считывает ADC и линейно переводит исходный диапазон в итоговый. Название единицы само по себе не измеряет люксы или температуру: диапазоны нужно откалибровать для вашего датчика.", "Reads ADC and maps the raw range linearly to the output range. A unit label does not measure lux or temperature by itself: calibrate the ranges for your sensor."),
            pwm_output: localized("Регулирует заполнение PWM в диапазоне 0–255. Подходит для яркости совместимой нагрузки; минимальное и максимальное значения задают доступный пользователю диапазон.", "Controls PWM duty within 0–255. Use it for compatible brightness control; the minimum and maximum define the range available to the user.")
        };
        return descriptions[id] || "";
    }

    function fieldHint(key) {
        const hints = {
            id: localized("Стабильный ID компонента: латиница, цифры, . _ -. Начните с буквы.", "Stable component ID: Latin letters, digits, . _ -. Start with a letter."),
            parameter_id: localized("Уникальный ID значения для сценариев Blockly. После создания сценариев меняйте его осознанно.", "Unique value ID used by Blockly scenarios. Change it carefully after creating scenarios."),
            pin: localized("Выбирайте фактический GPIO подключения из списка прошивки. Один пин назначается одному компоненту.", "Choose the actual wired GPIO from the firmware list. Each pin can belong to one component."),
            ui_control: localized("Меняет только вид элемента управления в карточке устройства.", "Changes only how the control appears on the device card."),
            raw_min: localized("Исходное показание ADC, соответствующее итоговому минимуму.", "Raw ADC reading that corresponds to the output minimum."),
            raw_max: localized("Исходное показание ADC, соответствующее итоговому максимуму.", "Raw ADC reading that corresponds to the output maximum."),
            value_min: localized("Значение в выбранных единицах при raw минимуме.", "Value in the selected unit at the raw minimum."),
            value_max: localized("Значение в выбранных единицах при raw максимуме.", "Value in the selected unit at the raw maximum."),
            unit: localized("Например, %, lx или °C. Пересчёт задаётся парами диапазонов, а не названием единицы.", "For example %, lx, or °C. The range pairs define conversion; the unit is a label."),
            initial_value: localized("Состояние выхода после запуска или применения этой конфигурации.", "Output state after startup or after this configuration is applied."),
            interval_ms: localized("Как часто устройство читает вход. Меньшее значение быстрее замечает изменения.", "How often the device reads the input. A shorter interval detects changes sooner.")
        };
        return hints[key] || "";
    }

    function draftSignature() {
        return canonicalJson({ configuration: configurationFromDraft(), ui_preferences: uiPreferencesFromDraft() });
    }

    function isDirty() {
        return baselineSignature !== null && draftSignature() !== baselineSignature;
    }

    function savedSignature() {
        const configuration = currentDevice().hardware_configuration || { format_version: 1, components: [] };
        const preferences = {};
        for (const component of configuration.components || []) {
            if (["gpio_output", "pwm_output"].includes(component.driver)) {
                preferences[component.parameter_id] = parameterUiPreferences?.[device.device]?.[component.parameter_id] || "schema";
            }
        }
        return canonicalJson({ configuration, ui_preferences: preferences });
    }

    function canClose() {
        return !isDirty() || window.confirm(localized(
            "В конфигурации есть несохранённые изменения. Закрыть окно и потерять этот черновик?",
            "This configuration has unsaved changes. Close the window and discard this draft?"
        ));
    }

    function collectLocalErrors() {
        const errors = new Map();
        const addError = (path, message) => { if (!errors.has(path)) errors.set(path, { path, message }); };
        const encoder = new TextEncoder();
        for (const [path, { input }] of fieldNodes) {
            if (input.validity.valueMissing) addError(path, localized("Заполните это поле.", "Complete this field."));
            else if (input.validity.patternMismatch) addError(path, localized("Начните с латинской буквы; используйте только латиницу, цифры, . _ -.", "Start with a Latin letter; use only Latin letters, digits, . _ -."));
            else if (input.validity.rangeUnderflow || input.validity.rangeOverflow) addError(path, localized(`Допустимый диапазон: ${input.min}…${input.max}.`, `Allowed range: ${input.min}…${input.max}.`));
            else if (input.validity.stepMismatch || input.validity.badInput) addError(path, localized(`Введите число с шагом ${input.step || 1}.`, `Enter a number with step ${input.step || 1}.`));
        }
        const ids = new Map();
        const parameterIds = new Map();
        const pins = new Map();
        function unique(map, value, path, message) {
            if (value === null || value === undefined || value === "") return;
            if (map.has(value)) {
                addError(path, message);
                addError(map.get(value), message);
            } else map.set(value, path);
        }
        components.forEach((component, index) => {
            const path = `components.${index}`;
            unique(ids, component.id, `${path}.id`, localized("Этот ID компонента уже используется.", "This component ID is already used."));
            unique(parameterIds, component.parameter_id, `${path}.parameter_id`, localized("Этот ID параметра уже используется.", "This parameter ID is already used."));
            unique(pins, component.pin, `${path}.pin`, localized("GPIO уже назначен другому компоненту или индикатору.", "This GPIO is already assigned to another component or indicator."));
            for (const key of ["label", "parameter_label", "unit"]) {
                const value = String(component[key] || "").trim();
                if (["label", "parameter_label"].includes(key) && !value) addError(`${path}.${key}`, localized("Введите название.", "Enter a label."));
                if (encoder.encode(value).length > (key === "unit" ? 24 : 80)) addError(`${path}.${key}`, localized(`Максимум ${key === "unit" ? 24 : 80} байт UTF-8.`, `Maximum ${key === "unit" ? 24 : 80} UTF-8 bytes.`));
                if (/[\u0000-\u001f\u007f]/.test(value)) addError(`${path}.${key}`, localized("Уберите управляющие символы.", "Remove control characters."));
            }
            for (const [minimum, maximum] of component.driver === "analog_input"
                ? [["raw_min", "raw_max"], ["value_min", "value_max"]]
                : component.driver === "pwm_output" ? [["min_value", "max_value"]] : []) {
                if (component[minimum] >= component[maximum]) {
                    const message = localized("Минимум должен быть меньше максимума.", "The minimum must be smaller than the maximum.");
                    addError(`${path}.${minimum}`, message);
                    addError(`${path}.${maximum}`, message);
                }
            }
            if (component.driver === "pwm_output" && (component.initial_value < component.min_value || component.initial_value > component.max_value)) {
                addError(`${path}.initial_value`, localized("Начальное значение должно быть внутри выбранного диапазона.", "The initial value must be within the selected range."));
            }
        });
        if (identifyIndicator) unique(pins, identifyIndicator.pin, "identify_indicator.pin", localized("GPIO уже назначен компоненту или индикатору.", "This GPIO is already assigned to a component or indicator."));
        if (encoder.encode(canonicalJson(configurationFromDraft())).length > 3000) addError("configuration", localized("Конфигурация превышает лимит прошивки в 3000 байт. Сократите названия или количество компонентов.", "The configuration exceeds the firmware limit of 3000 bytes. Shorten labels or reduce the number of components."));
        return [...errors.values()];
    }

    function renderValidation() {
        const errors = [...localErrors, ...serverErrors];
        for (const [path, { input, error }] of fieldNodes) {
            const messages = [...new Set(errors.filter(item => item.path === path).map(item => item.message))];
            error.textContent = messages.join(" ");
            error.hidden = messages.length === 0;
            input.setAttribute("aria-invalid", String(messages.length > 0));
        }
        validationErrorsList.replaceChildren();
        for (const error of errors.filter(error => !fieldNodes.has(error.path))) {
            const item = document.createElement("li");
            item.textContent = error.message;
            validationErrorsList.append(item);
        }
        validationErrorsList.hidden = !validationErrorsList.childElementCount;
        validationStatus.dataset.state = errors.length ? "invalid" : validationRequestId ? "pending" : validatedSignature === draftSignature() ? "valid" : "draft";
        validationStatus.textContent = errors.length
            ? localized(`Исправьте отмеченные поля: ${new Set(errors.map(error => error.path)).size}.`, `Fix the highlighted fields: ${new Set(errors.map(error => error.path)).size}.`)
            : validationRequestId ? localized("Gateway проверяет GPIO, диапазоны и Schema…", "Gateway is checking GPIO pins, ranges, and Schema…")
                : validatedSignature === draftSignature() ? localized("Проверено Gateway. Конфигурация готова к сохранению.", "Validated by Gateway. The configuration is ready to save.")
                    : localized("Локальная проверка пройдена. Проверка Gateway выполняется при подключении.", "Local checks passed. Gateway validation runs when connected.");
        save.disabled = Boolean(savingRequestId || retryRequestId || saveAfterValidation) || !isGatewayConnected() || errors.length > 0;
        save.textContent = savingRequestId ? localized("Сохраняю…", "Saving…") : saveAfterValidation ? localized("Проверяю…", "Validating…") : localized("Сохранить и применить", "Save and apply");
    }

    function focusFirstError() {
        const first = [...fieldNodes.values()].find(({ input }) => input.getAttribute("aria-invalid") === "true");
        if (first) {
            first.input.focus();
            first.input.scrollIntoView({ block: "center", behavior: "smooth" });
        } else validationStatus.scrollIntoView({ block: "center" });
    }

    function updatePinChoices() {
        for (const [path, { input }] of fieldNodes) {
            if (!path.endsWith(".pin")) continue;
            const ownIndex = path.startsWith("components.") ? Number(path.split(".")[1]) : -1;
            for (const option of input.options) {
                if (option.value === "") continue;
                const pin = Number(option.value);
                option.disabled = components.some((component, index) => index !== ownIndex && component.pin === pin)
                    || (ownIndex >= 0 && identifyIndicator?.pin === pin);
            }
        }
    }

    function renderPreview() {
        preview.replaceChildren();
        const heading = document.createElement("h4");
        heading.textContent = localized("Предпросмотр карточки", "Device card preview");
        const summary = document.createElement("p");
        const bytes = new TextEncoder().encode(canonicalJson(configurationFromDraft())).length;
        summary.textContent = localized(
            `${components.length} из 8 компонентов · ${bytes} из 3000 байт. Эти параметры будут доступны в карточке устройства и Blockly.`,
            `${components.length} of 8 components · ${bytes} of 3000 bytes. These parameters will be available on the device card and in Blockly.`
        );
        preview.append(heading, summary);
        const list = document.createElement("div");
        list.className = "device-config-preview-list";
        for (const component of components) {
            const item = document.createElement("article");
            item.className = "device-config-preview-item";
            const name = document.createElement("strong");
            name.textContent = component.parameter_label || localized("Без названия", "Untitled");
            const metadata = document.createElement("small");
            metadata.textContent = `${component.parameter_id || "—"} · GPIO ${component.pin ?? "—"} · ${driverLabel(component.driver)}`;
            const value = document.createElement("span");
            if (component.driver === "gpio_output") {
                const state = component.initial_value ? localized("Вкл", "On") : localized("Выкл", "Off");
                value.className = "device-config-preview-control";
                value.textContent = component.ui_control === "list" ? `▾ ${state}` : `▣ ${state}`;
            } else if (component.driver === "pwm_output") {
                if (component.ui_control !== "input") {
                    const slider = document.createElement("input");
                    slider.type = "range";
                    slider.min = component.min_value ?? 0;
                    slider.max = component.max_value ?? 255;
                    slider.value = component.initial_value ?? 0;
                    slider.disabled = true;
                    slider.setAttribute("aria-label", name.textContent);
                    value.append(slider);
                }
                value.append(`${component.initial_value ?? "—"} (${component.min_value ?? 0}…${component.max_value ?? 255})`);
            } else if (component.driver === "analog_input") {
                value.textContent = `${component.value_min ?? 0}…${component.value_max ?? 4095} ${String(component.unit || "raw")} · ` + localized("только чтение", "read only");
            } else value.textContent = localized("Сигнал 0/1 · только чтение", "Signal 0/1 · read only");
            item.append(name, metadata, value);
            list.append(item);
        }
        preview.append(list);
        const indicator = document.createElement("p");
        indicator.textContent = identifyIndicator
            ? localized(`«Найти устройство»: ${identifyIndicator.driver === "neopixel" ? "NeoPixel" : "GPIO"} на GPIO ${identifyIndicator.pin ?? "—"}.`, `Find device: ${identifyIndicator.driver === "neopixel" ? "NeoPixel" : "GPIO"} on GPIO ${identifyIndicator.pin ?? "—"}.`)
            : localized("Индикатор поиска не настроен. Кнопка «Найти устройство» появится после его настройки и подтверждения ESP32.", "No find indicator is configured. Find device becomes available after configuring it and receiving ESP32 confirmation.");
        preview.append(indicator);
    }

    function refreshSavedState() {
        const latest = currentDevice();
        const savedVersion = Number(latest.configuration_version) || 0;
        const appliedVersion = Number(latest.configuration_applied_version) || 0;
        const currentStatus = latest.configuration_status || "not_configured";
        const labels = {
            not_configured: localized("Не настроено: GPIO остаются неактивны.", "Not configured: GPIO pins remain inactive."),
            pending: localized(`Версия ${savedVersion} сохранена; ожидает подключения устройства.`, `Version ${savedVersion} is saved and waiting for the device to connect.`),
            applying: localized(`Версия ${savedVersion} отправлена. Ожидаем подтверждение ESP32.`, `Version ${savedVersion} was sent. Waiting for ESP32 confirmation.`),
            failed: localized(`Не удалось применить версию ${savedVersion}: ${latest.configuration_error || "ошибка устройства"}`, `Could not apply version ${savedVersion}: ${latest.configuration_error || "device error"}`),
            applied: localized(`ESP32 подтвердила версию ${appliedVersion || savedVersion}.`, `ESP32 confirmed version ${appliedVersion || savedVersion}.`)
        };
        status.textContent = labels[currentStatus] || labels.not_configured;
        if (latest.configuration_sync_pending && currentStatus !== "applying") {
            status.textContent += localized(" Управление ожидает подтверждения конфигурации в текущем подключении.", " Controls are waiting for configuration confirmation on the current connection.");
        }
        status.dataset.state = currentStatus;
        savedDetails.replaceChildren();
        const entries = [
            [localized("Сохранено в Gateway", "Saved on Gateway"), savedVersion ? `v${savedVersion}` : "—"],
            [localized("Подтверждено ESP32", "Confirmed by ESP32"), appliedVersion ? `v${appliedVersion}` : "—"],
            [localized("Сохранено", "Saved at"), latest.configuration_updated_at ? formatEventTime(latest.configuration_updated_at) : "—"],
            [localized("Применено", "Applied at"), latest.configuration_applied_at ? formatEventTime(latest.configuration_applied_at) : "—"]
        ];
        for (const [label, value] of entries) {
            const row = document.createElement("div");
            const term = document.createElement("dt");
            const detail = document.createElement("dd");
            term.textContent = label;
            detail.textContent = value;
            row.append(term, detail);
            savedDetails.append(row);
        }
        progress.hidden = !isGatewayConnected() || !(savingRequestId || retryRequestId || currentStatus === "applying" || (savedVersion > 0 && latest.configuration_sync_pending));
        retry.disabled = !isGatewayConnected() || !savedVersion || !latest.hardware_configuration || Boolean(retryRequestId || savingRequestId || saveAfterValidation) || currentStatus === "applying";
        const hasNewSavedConfiguration = savedSignature() !== baselineSignature;
        reset.disabled = (!isDirty() && !hasNewSavedConfiguration) || Boolean(savingRequestId);
        reset.textContent = !isDirty() && hasNewSavedConfiguration ? localized("Загрузить сохранённую", "Load saved configuration") : localized("Вернуть сохранённую", "Revert to saved");
        draftStatus.textContent = isDirty()
            ? localized("Есть несохранённые изменения. Предпросмотр показывает ваш черновик.", "There are unsaved changes. The preview shows your draft.")
            : hasNewSavedConfiguration ? localized("В Gateway сохранена другая конфигурация. Нажмите «Загрузить сохранённую», чтобы открыть её в этом окне.", "Gateway has a different saved configuration. Select Load saved configuration to open it in this window.")
                : !savedVersion ? localized("Начните с шаблона компонента или настройте индикатор поиска, затем сохраните конфигурацию.", "Start with a component preset or configure the find indicator, then save the configuration.")
                    : localized("Черновик совпадает с открытой сохранённой конфигурацией.", "The draft matches the saved configuration opened in this window.");
        draftStatus.classList.toggle("is-dirty", isDirty());
    }

    function requestValidation(forSave = false) {
        window.clearTimeout(validationTimer);
        validationTimer = null;
        if (!dialog.isConnected || !isGatewayConnected() || localErrors.length) return;
        const signature = draftSignature();
        if (validatedSignature === signature && serverErrors.length === 0) {
            if (forSave) saveDraft();
            return;
        }
        if (forSave) saveAfterValidation = signature;
        validationRequestId = createCommandId();
        validationSignature = signature;
        window.clearTimeout(validationTimeout);
        if (!sendGatewayAction("validate_device_configuration", {
            device: device.device,
            configuration: configurationFromDraft(),
            ui_preferences: uiPreferencesFromDraft(),
            request_id: validationRequestId
        })) {
            validationRequestId = null;
            saveAfterValidation = null;
        } else {
            const requestId = validationRequestId;
            validationTimeout = window.setTimeout(() => {
                if (validationRequestId !== requestId) return;
                validationRequestId = null;
                saveAfterValidation = null;
                feedback.textContent = localized("Gateway не ответил на проверку. Повторите сохранение после восстановления связи.", "Gateway did not respond to validation. Retry saving when the connection is available.");
                refreshSavedState();
                renderValidation();
            }, 8000);
        }
        refreshSavedState();
        renderValidation();
    }

    function draftChanged() {
        window.clearTimeout(validationTimer);
        window.clearTimeout(validationTimeout);
        validationTimer = null;
        validationRequestId = null;
        validatedSignature = null;
        saveAfterValidation = null;
        serverErrors = [];
        updatePinChoices();
        localErrors = collectLocalErrors();
        renderPreview();
        refreshSavedState();
        renderValidation();
        if (!localErrors.length) validationTimer = window.setTimeout(() => requestValidation(), 350);
    }

    function saveDraft() {
        savingRequestId = createCommandId();
        savingSignature = draftSignature();
        saveAfterValidation = null;
        feedback.textContent = currentDevice().connected
            ? localized("Сохраняю и отправляю конфигурацию…", "Saving and sending the configuration…")
            : localized("Сохраняю конфигурацию; она будет отправлена при подключении устройства.", "Saving the configuration; it will be sent when the device connects.");
        if (!sendGatewayAction("save_device_configuration", {
            device: device.device,
            configuration: configurationFromDraft(),
            ui_preferences: uiPreferencesFromDraft(),
            request_id: savingRequestId
        })) savingRequestId = null;
        else startOperationTimeout(savingRequestId);
        refreshSavedState();
        renderValidation();
    }

    function startOperationTimeout(requestId) {
        window.clearTimeout(operationTimeout);
        operationTimeout = window.setTimeout(() => {
            if (savingRequestId !== requestId && retryRequestId !== requestId) return;
            savingRequestId = null;
            retryRequestId = null;
            feedback.textContent = localized("Ответ Gateway не получен. Черновик остаётся в окне; проверьте сохранённую и подтверждённую версии перед повторной отправкой.", "No Gateway response was received. Your draft remains in this window; check the saved and confirmed versions before resending.");
            requestDevices();
            refreshSavedState();
            renderValidation();
        }, 12000);
    }

    firmwareButton.addEventListener("click", () => {
        const latestDevice = currentDevice();
        const savedConfiguration = latestDevice.hardware_configuration;
        const version = Number(latestDevice.configuration_version);
        if (
            !savedConfiguration ||
            !Number.isInteger(version) || version < 1 ||
            canonicalJson(configurationFromDraft()) !== canonicalJson(savedConfiguration)
        ) {
            feedback.textContent = localized(
                "Сначала сохраните эту конфигурацию в Gateway. После этого скачайте файл для прошивки.",
                "Save this configuration on Gateway first, then download the firmware file."
            );
            return;
        }
        downloadEmbeddedDeviceConfiguration(savedConfiguration, version);
        feedback.textContent = localized(
            "Скопируйте файл в esp32/universal/include/device_configuration.generated.h и соберите прошивку. Gateway продолжит хранить основную копию конфигурации.",
            "Copy the file to esp32/universal/include/device_configuration.generated.h and build the firmware. Gateway remains the primary configuration store."
        );
    });

    add.addEventListener("click", () => {
        if (components.length >= 8 || !drivers.length) return;
        const preset = presets.find(item => item.id === presetSelect.value);
        const driver = drivers.find(item => item.id === preset?.driver) || drivers[0];
        const component = defaultComponent(components.length, driver);
        if (preset) {
            let suffix = 1;
            let id = preset.id;
            while (components.some(item => item.id === id || item.parameter_id === `${id}_value`)) id = `${preset.id}_${++suffix}`;
            Object.assign(component, { id, parameter_id: `${id}_value`, label: preset.label, parameter_label: preset.parameter });
            if (preset.id === "button") component.interval_ms = 100;
        }
        components.push(component);
        renderComponents();
    });
    save.addEventListener("click", () => {
        if (savingRequestId) return;
        localErrors = collectLocalErrors();
        renderValidation();
        if (localErrors.length || serverErrors.length) focusFirstError();
        else requestValidation(true);
    });
    retry.addEventListener("click", () => {
        if (retry.disabled || savingRequestId || saveAfterValidation) return;
        retryRequestId = createCommandId();
        feedback.textContent = localized("Повторно отправляю сохранённую конфигурацию. Черновик не меняется.", "Resending the saved configuration. Your draft is unchanged.");
        if (!sendGatewayAction("retry_device_configuration", { device: device.device, request_id: retryRequestId })) retryRequestId = null;
        else startOperationTimeout(retryRequestId);
        refreshSavedState();
        renderValidation();
    });
    reset.addEventListener("click", () => {
        if (isDirty() && !window.confirm(localized("Заменить черновик последней сохранённой конфигурацией? Несохранённые изменения будут потеряны.", "Replace the draft with the latest saved configuration? Unsaved changes will be lost."))) return;
        const latest = currentDevice();
        components.splice(0, components.length, ...JSON.parse(JSON.stringify(latest.hardware_configuration?.components || [])));
        identifyIndicator = latest.hardware_configuration?.identify_indicator ? { ...latest.hardware_configuration.identify_indicator } : null;
        for (const component of components) component.ui_control = parameterUiPreferences?.[device.device]?.[component.parameter_id] || "schema";
        baselineSignature = draftSignature();
        renderComponents();
    });
    exportButton.addEventListener("click", () => {
        const latestDevice = currentDevice();
        const envelope = {
            export_format: "smartest-home-device",
            export_version: 1,
            exported_at: new Date().toISOString(),
            device_id: device.device,
            device_profile: latestDevice.device_profile,
            board_profile: latestDevice.board_profile,
            schema_version: latestDevice.schema_version ?? null,
            schema: latestDevice.schema,
            hardware_configuration: configurationFromDraft(),
            ui_preferences: uiPreferencesFromDraft(),
            configuration_version: latestDevice.configuration_version ?? null,
            configuration_status: latestDevice.configuration_status ?? null
        };
        const blob = new Blob([JSON.stringify(envelope, null, 2)], {
            type: "application/json;charset=utf-8"
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `${device.device}-configuration.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
    });
    savedExportButton.addEventListener("click", () => {
        const link = document.createElement("a");
        link.href = deviceConfigurationExportUrl(device.device);
        link.download = `${device.device}.json`;
        link.hidden = true;
        document.body.appendChild(link);
        link.click();
        link.remove();
    });
    importInput.addEventListener("change", async () => {
        const file = importInput.files?.[0];
        if (!file) return;
        try {
            const imported = JSON.parse(await file.text());
            const configuration = imported?.hardware_configuration || imported?.configuration || imported;
            if (configuration?.format_version !== 1 || !Array.isArray(configuration.components)) {
                throw new Error(localized("Файл не содержит конфигурацию версии 1.", "File does not contain a version 1 configuration."));
            }
            if (configuration.components.length > 8 || configuration.components.some(component =>
                !component || typeof component !== "object" ||
                !drivers.some(driver => driver.id === component.driver)
            )) {
                throw new Error(localized(
                    "Файл содержит неизвестный драйвер или больше восьми компонентов.",
                    "The file contains an unknown driver or more than eight components."
                ));
            }
            components.splice(0, components.length, ...JSON.parse(JSON.stringify(configuration.components)));
            identifyIndicator = configuration.identify_indicator
                ? JSON.parse(JSON.stringify(configuration.identify_indicator))
                : null;
            const importedPreferences = imported?.ui_preferences || {};
            for (const component of components) {
                component.ui_control = importedPreferences[component.parameter_id] || "schema";
            }
            renderComponents();
            feedback.textContent = localized("Конфигурация загружена. Нажмите «Сохранить и применить».", "Configuration loaded. Select Save and apply to continue.");
        } catch (error) {
            feedback.textContent = error.message || localized("Не удалось прочитать JSON.", "Could not read the JSON file.");
        } finally {
            importInput.value = "";
        }
    });

    dialog.configurationEditor = {
        canClose,
        refreshSavedState,
        validationResult(message) {
            if (message.request_id !== validationRequestId || validationSignature !== draftSignature()) return;
            window.clearTimeout(validationTimeout);
            validationRequestId = null;
            serverErrors = Array.isArray(message.errors) ? message.errors : [];
            if (!message.success && !serverErrors.length) serverErrors = [{ path: "configuration", message: localized("Gateway отклонил конфигурацию.", "Gateway rejected the configuration.") }];
            validatedSignature = message.success ? validationSignature : null;
            const shouldSave = message.success && saveAfterValidation === draftSignature();
            saveAfterValidation = null;
            renderValidation();
            if (shouldSave) saveDraft();
        },
        saveResult(message) {
            if (message.request_id && message.request_id !== savingRequestId) return;
            if (!savingRequestId) return;
            window.clearTimeout(operationTimeout);
            if (message.success) baselineSignature = savingSignature;
            savingRequestId = null;
            savingSignature = null;
            feedback.textContent = message.message || localized("Запрос сохранения обработан.", "Save request processed.");
            if (!message.success && Array.isArray(message.errors)) serverErrors = message.errors;
            refreshSavedState();
            renderValidation();
        },
        retryResult(message) {
            if (message.request_id !== retryRequestId) return;
            window.clearTimeout(operationTimeout);
            retryRequestId = null;
            feedback.textContent = message.message || localized("Запрос повторного применения обработан.", "Retry request processed.");
            refreshSavedState();
            renderValidation();
        },
        applyResult(message) {
            feedback.textContent = message.message || "";
            refreshSavedState();
        },
        connectionChanged(connected) {
            if (!connected) {
                window.clearTimeout(validationTimer);
                window.clearTimeout(validationTimeout);
                window.clearTimeout(operationTimeout);
                validationRequestId = null;
                validatedSignature = null;
                saveAfterValidation = null;
                savingRequestId = null;
                retryRequestId = null;
                feedback.textContent = localized("Соединение с Gateway потеряно. Черновик сохранён в этом окне; после подключения проверьте версии.", "Gateway connection was lost. Your draft remains in this window; check the versions after reconnecting.");
            } else requestValidation();
            refreshSavedState();
            renderValidation();
        }
    };
    baselineSignature = draftSignature();
    renderComponents();
    dialog.dataset.deviceId = device.device;
    files.append(firmwareHelp, schemaLocation);
    form.append(heading, intro, status, savedDetails, progress, draftStatus, addRow, componentList, preview, validationStatus, validationErrorsList, files, feedback, buttons);
    dialog.appendChild(form);
    const beforeUnload = event => {
        if (!isDirty()) return;
        event.preventDefault();
        event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    dialog.addEventListener("cancel", event => {
        event.preventDefault();
        if (canClose()) dialog.close();
    });
    const cleanUpEditor = () => {
        window.clearTimeout(validationTimer);
        window.clearTimeout(validationTimeout);
        window.clearTimeout(operationTimeout);
        window.removeEventListener("beforeunload", beforeUnload);
        dialog.remove();
        if (deviceConfigurationDialog === dialog) deviceConfigurationDialog = null;
    };
    dialog.addEventListener("close", cleanUpEditor, { once: true });
    document.body.appendChild(dialog);
    deviceConfigurationDialog = dialog;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else {
        cleanUpEditor();
        setActivity(localized("Этот браузер не поддерживает окно конфигуратора.", "This browser does not support the configuration dialog."));
    }
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
    form.addEventListener("submit", event => {
        event.preventDefault();
        submit.click();
    });
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
    if (isDeviceConfigurationBusy(device)) {
        setActivity(localized("Дождитесь подтверждения аппаратной конфигурации от ESP32.", "Wait for ESP32 to confirm the hardware configuration."));
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
    if (isGatewayConnected()) sendGatewayAction("get_devices");
}

function requestDatabaseStats() {
    if (isGatewayConnected()) sendGatewayAction("get_database_stats");
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
    if (reset && eventSearchTimer !== null) {
        window.clearTimeout(eventSearchTimer);
        eventSearchTimer = null;
    }
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
    const searchQuery = eventSearchQuery.trim();
    activeEventRequest = {
        requestId,
        mode: reset ? "reset" : "append",
        deviceFilter: eventDeviceFilter,
        searchQuery
    };
    if (reset) {
        eventMatchCount = null;
        eventLegacyVisibilityCounts = false;
    }
    eventsLoading = true;
    renderEvents();

    try {
        socket.send(JSON.stringify({
            type: "browser_message",
            action: "get_events",
            device: eventDeviceFilter,
            search: searchQuery,
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

databaseBackupCreateButton?.addEventListener("click", () => {
    if (databaseBackupActionPending) return;
    databaseBackupActionPending = true;
    if (databaseBackupFeedback) {
        databaseBackupFeedback.textContent = localized(
            "Создаю согласованную копию базы…",
            "Creating a consistent database snapshot…"
        );
    }
    renderDatabaseBackups();
    if (!sendGatewayAction("create_database_backup")) {
        databaseBackupActionPending = false;
        if (databaseBackupFeedback) {
            databaseBackupFeedback.textContent = localized(
                "Gateway не принял запрос на создание копии.",
                "Gateway did not accept the backup request."
            );
        }
        renderDatabaseBackups();
    }
});

databaseBackupRefreshButton?.addEventListener("click", () => {
    if (databaseBackupActionPending || databaseBackupRefreshPending) return;
    databaseBackupRefreshPending = true;
    databaseBackupFeedback.textContent = localized("Обновляю список…", "Refreshing backup list…");
    renderDatabaseBackups();
    if (!sendGatewayAction("get_database_backups")) {
        databaseBackupRefreshPending = false;
        databaseBackupFeedback.textContent = localized(
            "Gateway не принял запрос на обновление списка.",
            "Gateway did not accept the refresh request."
        );
        renderDatabaseBackups();
    }
});

// =========================
// ЗАПУСК ПРИЛОЖЕНИЯ
// =========================

initializeInterface();
connectGateway();
setInterval(renderDatabaseStats, 1000);
setInterval(updateDeviceFreshnessHints, 1000);
setInterval(requestDatabaseStats, 30000);
