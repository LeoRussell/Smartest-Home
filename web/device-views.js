/* Browser-local device density and room sections. All commands use explicit mappings. */
(() => {
    "use strict";

    const STORAGE_KEY = "iot-gateway-device-views-v1";
    let context = null;
    let container = null;
    let toolbar = null;
    let rooms = [];
    let density = "comfortable";
    let grouping = "rooms";
    const collapsed = new Set();
    const expandedDetails = new Set();
    const t = (ru, en) => context?.localized(ru, en) || ru;
    const node = (tag, text, className) => {
        const item = document.createElement(tag);
        if (text != null) item.textContent = text;
        if (className) item.className = className;
        return item;
    };
    const groupKey = device => device.group_id ? `group:${device.group_id}` : "ungrouped";
    const getDevices = () => context?.getDevices() || [];

    function loadPreferences() {
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
            density = saved?.density === "compact" ? "compact" : "comfortable";
            grouping = saved?.grouping === "list" ? "list" : "rooms";
            if (Array.isArray(saved?.collapsed)) {
                for (const key of saved.collapsed.slice(0, 101)) {
                    if (typeof key === "string" && key.length <= 200) collapsed.add(key);
                }
            }
        } catch { /* Private browsing or invalid stored preferences must not block controls. */ }
    }

    function savePreferences() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify({ density, grouping, collapsed: [...collapsed] }));
        } catch { /* The current view still works when browser storage is unavailable. */ }
    }

    function label(caption, select) {
        const item = node("label", null, "device-view-field");
        item.append(node("span", caption), select);
        return item;
    }

    function option(select, value, text) {
        const item = node("option", text);
        item.value = value;
        select.append(item);
    }

    function renderToolbar() {
        if (!toolbar) return;
        toolbar.setAttribute("aria-label", t("Отображение устройств", "Device view"));
        const densitySelect = toolbar.querySelector("#device-card-density");
        const groupingSelect = toolbar.querySelector("#device-card-grouping");
        // Keep the selects themselves stable: incoming telemetry must not interrupt focus.
        densitySelect.options[0].textContent = t("Подробные", "Detailed");
        densitySelect.options[1].textContent = t("Компактные", "Compact");
        groupingSelect.options[0].textContent = t("По комнатам / группам", "By room / group");
        groupingSelect.options[1].textContent = t("Общий список", "Single list");
        densitySelect.previousElementSibling.textContent = t("Карточки устройств", "Device cards");
        groupingSelect.previousElementSibling.textContent = t("Расположение", "Layout");
        densitySelect.value = density;
        groupingSelect.value = grouping;
        for (const item of toolbar.querySelectorAll("[data-room-toggle-all]")) {
            const open = item.dataset.roomToggleAll === "open";
            item.textContent = open ? t("Развернуть комнаты", "Expand rooms") : t("Свернуть комнаты", "Collapse rooms");
            item.hidden = grouping !== "rooms" || rooms.length === 0;
        }
    }

    function ensureToolbar() {
        if (toolbar || !container) return;
        toolbar = node("div", null, "device-view-toolbar");
        toolbar.setAttribute("role", "group");
        const densitySelect = node("select");
        densitySelect.id = "device-card-density";
        option(densitySelect, "comfortable", "");
        option(densitySelect, "compact", "");
        densitySelect.addEventListener("change", () => {
            density = densitySelect.value === "compact" ? "compact" : "comfortable";
            savePreferences();
            context.renderDevices();
        });
        const groupingSelect = node("select");
        groupingSelect.id = "device-card-grouping";
        option(groupingSelect, "rooms", "");
        option(groupingSelect, "list", "");
        groupingSelect.addEventListener("change", () => {
            grouping = groupingSelect.value === "list" ? "list" : "rooms";
            savePreferences();
            context.renderDevices();
        });
        toolbar.append(label("", densitySelect), label("", groupingSelect));
        const actions = node("div", null, "device-view-room-actions");
        for (const open of [true, false]) {
            const toggle = node("button");
            toggle.type = "button";
            toggle.dataset.roomToggleAll = open ? "open" : "closed";
            toggle.addEventListener("click", () => {
                for (const room of rooms) setRoomOpen(room, open);
                savePreferences();
            });
            actions.append(toggle);
        }
        toolbar.append(actions);
        container.before(toolbar);
    }

    function setRoomOpen(room, open) {
        room.body.hidden = !open;
        room.toggle.setAttribute("aria-expanded", String(open));
        if (open) collapsed.delete(room.key);
        else collapsed.add(room.key);
    }

    function decoratePanel(panel, device) {
        panel.classList.toggle("is-compact", density === "compact");
        if (density !== "compact" || panel.querySelector(":scope > .device-secondary-details")) return;
        const secondary = [...panel.children].filter(item =>
            item.matches('[data-role="device-health"], [data-role="device-hint"], .device-modes.is-empty')
        );
        if (!secondary.length) return;
        const details = node("details", null, "device-secondary-details");
        details.dataset.deviceId = device.device;
        details.dataset.role = "device-secondary-details";
        details.open = expandedDetails.has(device.device);
        const summary = node("summary", t("Сведения об устройстве", "Device details"));
        const body = node("div", null, "device-secondary-content");
        body.append(...secondary);
        details.append(summary, body);
        details.addEventListener("toggle", () => {
            if (!details.isConnected) return;
            if (details.open) expandedDetails.add(device.device);
            else expandedDetails.delete(device.device);
        });
        panel.append(details);
    }

    function makeRoom(key, id, name, index) {
        const section = node("section", null, "device-room");
        section.dataset.groupKey = key;
        const header = node("div", null, "device-room-header");
        const toggle = node("button", null, "device-room-toggle");
        toggle.type = "button";
        toggle.dataset.role = "room-toggle";
        const nameNode = node("strong", name, "device-room-name");
        const counts = node("span", null, "device-room-counts");
        toggle.append(nameNode, counts);
        const body = node("div", null, "device-room-content");
        body.id = `gateway-device-room-${index}`;
        toggle.setAttribute("aria-controls", body.id);
        header.append(toggle);
        const room = { key, id, section, body, toggle, counts, visible: 0, off: null };
        if (id != null) {
            const off = node("button", null, "device-room-off");
            off.type = "button";
            off.addEventListener("click", () => {
                if (window.DeviceManagement?.canExecuteOff(id)) context.executeOff(id);
            });
            room.off = off;
            header.append(off);
        }
        toggle.addEventListener("click", () => {
            setRoomOpen(room, body.hidden);
            savePreferences();
        });
        section.append(header, body);
        setRoomOpen(room, !collapsed.has(key));
        return room;
    }

    function arrange(target, visibleDevices) {
        if (!context || !target) return;
        container = target;
        ensureToolbar();
        rooms = [];
        const byId = new Map(visibleDevices.map(device => [device.device, device]));
        const panels = [...target.querySelectorAll(".device-panel")];
        // Re-applying a view must not leave empty room wrappers behind.
        for (const panel of panels) target.append(panel);
        for (const section of target.querySelectorAll(":scope > .device-room")) section.remove();
        const currentKeys = new Set(getDevices().map(groupKey));
        // The page can render an empty placeholder before its first device_list.
        // That placeholder must not erase the saved collapse choices.
        if (currentKeys.size) {
            for (const key of collapsed) if (!currentKeys.has(key)) collapsed.delete(key);
        }
        const currentIds = new Set(getDevices().map(device => device.device));
        for (const id of expandedDetails) if (!currentIds.has(id)) expandedDetails.delete(id);
        toolbar.hidden = getDevices().length === 0;
        for (const panel of panels) {
            const device = byId.get(panel.dataset.deviceId);
            if (device) decoratePanel(panel, device);
        }
        if (grouping === "rooms") {
            const sections = new Map();
            for (const panel of panels) {
                const device = byId.get(panel.dataset.deviceId);
                if (!device) continue;
                const key = groupKey(device);
                let room = sections.get(key);
                if (!room) {
                    const name = device.group_id
                        ? window.DeviceManagement?.getGroupName(device.group_id) || device.group_name || device.group_id
                        : t("Без группы", "Ungrouped");
                    room = makeRoom(key, device.group_id || null, name, sections.size);
                    sections.set(key, room);
                    target.append(room.section);
                    rooms.push(room);
                }
                room.body.append(panel);
                room.visible += 1;
            }
        }
        refresh();
    }

    function refresh() {
        if (!context) return;
        rooms = rooms.filter(room => room.section.isConnected);
        renderToolbar();
        const all = getDevices();
        for (const room of rooms) {
            const members = all.filter(device => groupKey(device) === room.key);
            const connected = members.filter(device => device.connected).length;
            room.counts.textContent = context.isConnected()
                ? t(`На связи: ${connected} из ${members.length}`, `Online: ${connected} of ${members.length}`)
                : t(`Gateway недоступен · Устройств: ${members.length}`, `Gateway unavailable · Devices: ${members.length}`);
            if (room.visible !== members.length) room.counts.textContent += t(` · Показано: ${room.visible}`, ` · Shown: ${room.visible}`);
            room.section.classList.toggle("is-offline", !context.isConnected() || connected === 0);
            if (room.off) {
                room.off.textContent = t("Выключить комнату…", "Turn room off…");
                room.off.disabled = !window.DeviceManagement?.canExecuteOff(room.id);
                const name = window.DeviceManagement?.getGroupName(room.id) || room.id;
                room.off.title = t(
                    `Назначенные команды выключения для всей группы «${name}», независимо от поиска и фильтров`,
                    `Assigned off operations for the whole group “${name}”, regardless of search and filters`
                );
            }
        }
    }

    window.DeviceViews = {
        initialize(options) {
            context = options;
            loadPreferences();
            container = document.querySelector(".device-card");
            ensureToolbar();
            refresh();
        },
        revealDevice(deviceId) {
            const room = rooms.find(item => [...item.body.querySelectorAll(".device-panel")].some(panel => panel.dataset.deviceId === deviceId));
            if (!room) return;
            setRoomOpen(room, true);
            savePreferences();
        },
        arrange,
        refresh
    };
})();
