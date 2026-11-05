/* Personal dashboard and bounded, server-backed parameter history. */
(() => {
    "use strict";
    const LAYOUT_KEY = "iot-gateway-dashboard-v1";
    const HISTORY_RANGE_KEY = "iot-gateway-history-range-v1";
    const MAX_TILES = 24;
    let context, layout = null, catalogKey = "", tileKey = "", selected = "", pending = null;
    let historyTimer = null, settingsTimer = null, settingsRequest = null, settings = null, settingsDirty = false;
    let points = [], response = null, historyState = "idle", historyError = "", chartKey = "", selectedSchemaKey = "";
    let resizeFrame = null, selectedPointTimestamp = null;
    let historyUnsupported = false;
    let showHistoryRange = false;
    try { showHistoryRange = localStorage.getItem(HISTORY_RANGE_KEY) === "true"; } catch (_) { /* Storage is optional. */ }
    const tiles = new Map();
    let draggedTileKey = null, pointerDrag = null;
    const ui = {}, settingsUi = {};
    const t = (ru, en) => context.localized(ru, en);
    const node = (tag, text, className) => {
        const element = document.createElement(tag);
        if (text != null) element.textContent = text;
        if (className) element.className = className;
        return element;
    };
    const button = (text, action, className) => {
        const element = node("button", text, className);
        element.type = "button";
        element.addEventListener("click", action);
        return element;
    };
    const field = (caption, input) => {
        const element = node("label", null, "management-field");
        element.append(node("span", caption), input);
        return element;
    };
    const name = device => device.display_name || device.name || device.device;
    const key = item => JSON.stringify([item.kind, item.device || "", item.id]);
    const scalar = definition => ["number", "integer", "boolean"].includes(definition?.type);
    const devices = () => context.getDevices();
    const id = () => window.crypto?.randomUUID?.() || `history-${Date.now()}-${Math.random()}`;
    const connected = () => context.isConnected();
    const save = () => { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (_) { /* Private storage may be unavailable. */ } };

    function selectionSchemaSignature() {
        const [, deviceId, parameterId] = selected ? JSON.parse(selected) : [];
        const definition = devices().find(device => device.device === deviceId)?.schema?.parameters?.[parameterId];
        return JSON.stringify([selected, definition?.type, definition?.unit, definition?.history]);
    }

    function choices() {
        const result = [];
        for (const device of devices()) {
            for (const [parameter, definition] of Object.entries(device.schema?.parameters || {})) {
                result.push({ kind: "parameter", device: device.device, id: parameter, title: `${name(device)} · ${definition.label || parameter}` });
            }
            for (const [action, definition] of Object.entries(device.schema?.actions || {})) {
                result.push({ kind: "action", device: device.device, id: action, title: `${name(device)} · ${definition.label || action}` });
            }
        }
        for (const scenario of context.getAutomations()) {
            if (scenario.config?.program?.trigger?.type === "manual") result.push({ kind: "scenario", id: scenario.automation_id, title: scenario.config.name });
        }
        return result;
    }

    function activeLayout() {
        if (layout !== null) return layout;
        const available = choices();
        return [...available.filter(item => item.kind === "parameter").slice(0, 4), ...available.filter(item => item.kind === "scenario").slice(0, 2)];
    }

    function tileType(item) {
        if (item.kind === "scenario") return { kind: "scenario", caption: t("Сценарий", "Scenario") };
        if (item.kind === "action") return { kind: "command", caption: t("Команда", "Command") };
        const definition = devices().find(device => device.device === item.device)?.schema?.parameters?.[item.id];
        return definition?.access === "read_write"
            ? { kind: "device", caption: t("Устройство", "Device") }
            : { kind: "reading", caption: t("Показание", "Reading") };
    }

    function typeBadge(item) {
        const type = tileType(item);
        const badge = node("span", null, `workspace-type workspace-type-${type.kind}`);
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const paths = {
            device: ["M4 3h16v13H4Z", "M8 21h8M12 16v5M8 7h8M8 11h5"],
            reading: ["M4 18V6h16v12Z", "M8 15V9m4 6v-4m4 4V7"],
            command: ["M3 5h18v14H3Z", "m7 9 3 3-3 3m6 0h4"],
            scenario: ["M3 3h7v6H3Zm11 12h7v6h-7Z", "M6 9v9h8m3-3V6h-7"]
        };
        for (const d of paths[type.kind]) {
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("d", d); svg.append(path);
        }
        badge.append(svg, node("span", type.caption));
        return badge;
    }

    function cleanDrag() {
        const previous = pointerDrag;
        pointerDrag = null;
        if (previous?.handle.hasPointerCapture(previous.id)) previous.handle.releasePointerCapture(previous.id);
        draggedTileKey = null;
        for (const card of document.querySelectorAll(".workspace-tile")) card.classList.remove("is-dragging", "is-drop-target");
    }

    function moveTile(sourceKey, targetKey) {
        const items = activeLayout().map(item => ({ kind: item.kind, ...(item.device ? { device: item.device } : {}), id: item.id }));
        const source = items.findIndex(item => key(item) === sourceKey), target = items.findIndex(item => key(item) === targetKey);
        if (source < 0 || target < 0 || source === target) return;
        items.splice(target, 0, items.splice(source, 1)[0]);
        layout = items; save(); refresh();
        tiles.get(sourceKey)?.dragHandle?.focus({ preventScroll: true });
        const status = document.getElementById("workspace-order-status");
        if (status) status.textContent = t(`Плитка перемещена: ${target + 1} из ${items.length}.`, `Tile moved: ${target + 1} of ${items.length}.`);
    }

    function dragHandle(item, card) {
        const handle = button("⠿", event => { event.preventDefault(); if (event.detail === 0) customize(); }, "workspace-drag-handle");
        const caption = t("Перетащить плитку; порядок также меняется в «Настроить»", "Drag tile; order can also be changed in Customize");
        handle.title = caption; handle.setAttribute("aria-label", `${caption} · ${item.device || ""} ${item.id}`);
        // Pointer capture supports both mouse and touch without making buttons,
        // fields or text in the rest of the tile draggable.
        handle.addEventListener("pointerdown", event => {
            if (event.button !== 0 || !event.isPrimary) return;
            cleanDrag();
            pointerDrag = { id: event.pointerId, x: event.clientX, y: event.clientY, handle, active: false };
            draggedTileKey = key(item);
            handle.setPointerCapture(event.pointerId);
            handle.focus({ preventScroll: true });
            event.preventDefault();
        });
        handle.addEventListener("pointermove", event => {
            const drag = pointerDrag;
            if (!drag || drag.id !== event.pointerId) return;
            if (!drag.active && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
            drag.active = true; card.classList.add("is-dragging");
            for (const other of document.querySelectorAll(".workspace-tile.is-drop-target")) other.classList.remove("is-drop-target");
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".workspace-tile");
            if (target && target !== card) target.classList.add("is-drop-target");
            if (event.clientY > innerHeight - 36) window.scrollBy(0, 18);
            else if (event.clientY < 36) window.scrollBy(0, -18);
            event.preventDefault();
        });
        handle.addEventListener("pointerup", event => {
            if (!pointerDrag || pointerDrag.id !== event.pointerId) return;
            const source = draggedTileKey, active = pointerDrag.active;
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".workspace-tile");
            cleanDrag();
            if (active && target?.dataset.tileKey) moveTile(source, target.dataset.tileKey);
            event.preventDefault();
        });
        handle.addEventListener("pointercancel", event => { if (pointerDrag?.id === event.pointerId) cleanDrag(); });
        handle.addEventListener("lostpointercapture", () => { if (pointerDrag?.handle === handle) cleanDrag(); });
        return handle;
    }

    function customize() {
        const dialog = node("dialog", null, "management-dialog workspace-customize-dialog");
        const heading = node("h2", t("Настроить обзор", "Customize overview"), "management-dialog-title");
        context.labelDialog(dialog, heading);
        const form = node("form", null, "management-dialog-body");
        form.append(node("p", t("Выберите до 24 элементов. Меняйте порядок стрелками ↑ и ↓; на основной панели плитки можно перетаскивать за ручку. Изменения применятся после сохранения.", "Choose up to 24 tiles. Change order with ↑ and ↓; on the dashboard, drag tiles by their handle. Changes apply when saved."), "management-help"));
        const available = new Map(choices().map(item => [key(item), item]));
        const current = activeLayout();
        const selectedKeys = new Set(current.map(key));
        const ordered = [...current.map(item => available.get(key(item)) || { ...item, title: `${item.device || ""} · ${item.id} (${t("недоступно", "unavailable")})` }), ...[...available.values()].filter(item => !selectedKeys.has(key(item)))];
        const list = node("div", null, "workspace-choice-list");
        const checks = new Map();
        for (const item of ordered) {
            const check = node("input"); check.type = "checkbox"; check.checked = selectedKeys.has(key(item));
            const row = node("div", null, "workspace-choice-row"); row.dataset.tileKey = key(item);
            const label = node("label", null, "workspace-choice");
            const copy = node("span");
            copy.append(typeBadge(item), node("strong", item.title));
            const reorder = node("div", null, "workspace-choice-order");
            const move = direction => {
                const adjacent = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
                if (!adjacent) return;
                if (direction < 0) list.insertBefore(row, adjacent);
                else list.insertBefore(adjacent, row);
                updateOrderButtons();
                feedback.dataset.state = "info";
                feedback.textContent = t("Порядок изменён. Нажмите «Сохранить», чтобы применить.", "Order changed. Choose Save to apply it.");
            };
            const up = button("↑", () => move(-1)), down = button("↓", () => move(1));
            up.setAttribute("aria-label", t(`Выше: ${item.title}`, `Move up: ${item.title}`));
            down.setAttribute("aria-label", t(`Ниже: ${item.title}`, `Move down: ${item.title}`));
            reorder.append(up, down); label.append(check, copy); row.append(label, reorder); list.append(row);
            checks.set(key(item), { check, item, up, down });
        }
        function updateOrderButtons() {
            const focused = document.activeElement;
            const rows = [...list.querySelectorAll(".workspace-choice-row")];
            rows.forEach((row, index) => {
                const record = checks.get(row.dataset.tileKey);
                record.up.disabled = index === 0; record.down.disabled = index === rows.length - 1;
            });
            if (focused?.disabled && list.contains(focused)) focused.closest(".workspace-choice-order")?.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
        }
        updateOrderButtons();
        if (!ordered.length) list.append(node("p", t("Подключите устройство или создайте ручной сценарий.", "Connect a device or create a manual scenario.")));
        const feedback = node("p", "", "management-feedback"); feedback.setAttribute("role", "status");
        const actions = node("div", null, "management-dialog-actions");
        const cancel = button(t("Отмена", "Cancel"), () => dialog.close());
        const submit = node("button", t("Сохранить", "Save")); submit.type = "submit";
        actions.append(cancel, submit); form.append(list, feedback, actions); dialog.append(heading, form);
        form.addEventListener("submit", event => {
            event.preventDefault();
            const chosen = [...list.querySelectorAll(".workspace-choice-row")].map(row => checks.get(row.dataset.tileKey))
                .filter(item => item.check.checked).map(({ item }) => ({ kind: item.kind, ...(item.device ? { device: item.device } : {}), id: item.id }));
            if (chosen.length > MAX_TILES) { feedback.dataset.state = "error"; feedback.textContent = t("Можно выбрать не больше 24 элементов.", "Choose at most 24 tiles."); return; }
            layout = chosen; save(); tileKey = ""; refresh(); dialog.close();
        });
        dialog.addEventListener("close", () => dialog.remove(), { once: true });
        document.body.append(dialog); dialog.showModal();
    }

    function renderTiles() {
        const host = document.getElementById("workspace-tiles");
        if (!host) return;
        const items = activeLayout();
        const signature = JSON.stringify([context.language(), items, devices().map(device => [device.device, name(device), device.schema]), context.getAutomations().map(item => [item.automation_id, item.config?.name])]);
        if (signature !== tileKey) {
            cleanDrag();
            const drafts = new Map();
            const focused = document.activeElement;
            let focusedKey = null, focusedControl = null;
            for (const [tileId, record] of tiles) {
                if (record.input && record.input.dataset.dirty === "true") drafts.set(tileId, {
                    value: record.input.value, signature: record.controlSignature,
                    start: record.input.selectionStart, end: record.input.selectionEnd
                });
                if (record.card.contains(focused)) {
                    focusedKey = tileId;
                    focusedControl = focused.dataset.workspaceControl;
                }
            }
            tileKey = signature; tiles.clear(); host.replaceChildren();
            if (!items.length) host.append(node("p", t("Панель пока пуста. Нажмите «Настроить», чтобы добавить показания и команды.", "Your dashboard is empty. Choose Customize to add readings and commands."), "workspace-empty"));
            for (const item of items) {
                const card = node("article", null, "workspace-tile");
                card.dataset.tileKey = key(item);
                const title = node("h2"), subtitle = node("p", "", "workspace-tile-subtitle"), value = node("strong", "", "workspace-tile-value"), status = node("p", "", "workspace-tile-status");
                const controls = node("div", null, "workspace-tile-controls");
                const header = node("div", null, "workspace-tile-header");
                const tools = node("div", null, "workspace-tile-tools");
                const handle = dragHandle(item, card); tools.append(handle); header.append(typeBadge(item), tools);
                handle.dataset.workspaceControl = "move";
                const available = choices().find(candidate => key(candidate) === key(item));
                title.textContent = available?.title || item.id;
                subtitle.textContent = item.device || t("Автоматизация Gateway", "Gateway automation");
                const record = { item, card, title, subtitle, value, status, controls, tools, dragHandle: handle };
                if (item.kind === "parameter") {
                    buildParameterTile(record);
                    const draft = drafts.get(key(item));
                    if (record.input && draft?.signature === record.controlSignature) {
                        record.input.value = draft.value; record.input.dataset.dirty = "true";
                        if (record.input.type === "text" && draft.start != null) record.input.setSelectionRange(draft.start, draft.end);
                    }
                }
                else {
                    record.run = button(item.kind === "scenario" ? t("Запустить", "Run") : t("Выполнить", "Execute"), () => {
                        if (item.kind === "scenario") context.runScenario(item.id);
                        else context.callAction(item.device, item.id);
                        refresh();
                    }, "workspace-primary");
                    record.run.dataset.workspaceControl = "run";
                    controls.append(record.run);
                    if (item.kind === "scenario") {
                        controls.classList.add("workspace-scenario-controls");
                        record.edit = button(t("Изменить", "Edit"), () => context.editScenario(item.id), "workspace-scenario-edit");
                        record.edit.dataset.workspaceControl = "edit";
                        record.test = button(t("Тест", "Test"), () => context.testScenario(item.id), "workspace-scenario-test");
                        record.test.dataset.workspaceControl = "test";
                        controls.append(record.edit, record.test);
                        record.lastRun = node("div", null, "workspace-scenario-last-run");
                        record.lastResult = node("p", "", "workspace-scenario-last-result");
                        record.lastError = node("p", "", "workspace-scenario-last-error");
                        record.lastRun.append(record.lastResult, record.lastError);
                    }
                }
                card.append(header, title, subtitle, value);
                if (record.lastRun) card.append(record.lastRun);
                card.append(controls, status); host.append(card); tiles.set(key(item), record);
            }
            for (const record of tiles.values()) updateTile(record);
            if (focusedKey && document.activeElement === document.body) {
                const record = tiles.get(focusedKey);
                const replacement = [...(record?.card.querySelectorAll("[data-workspace-control]") || [])]
                    .find(element => element.dataset.workspaceControl === focusedControl && !element.disabled)
                    || (record?.edit && !record.edit.disabled ? record.edit : record?.dragHandle)
                    || document.getElementById("workspace-customize");
                replacement?.focus({ preventScroll: true });
            }
        }
        for (const record of tiles.values()) updateTile(record);
    }

    function buildParameterTile(record) {
        const { item, controls } = record;
        const device = devices().find(candidate => candidate.device === item.device);
        const definition = device?.schema?.parameters?.[item.id];
        record.controlSignature = JSON.stringify(definition && [definition.type, definition.access, definition.min, definition.max, definition.step, definition.options]);
        if (definition?.access === "read_write") {
            if (definition.type === "boolean") {
                record.toggle = button("", () => {
                    const current = devices().find(candidate => candidate.device === item.device)?.parameters?.[item.id];
                    if (typeof current === "boolean") context.command(item.device, item.id, !current);
                    refresh();
                }, "workspace-toggle");
                record.toggle.dataset.workspaceControl = "toggle";
                controls.append(record.toggle);
            } else if (["integer", "number", "enum", "string"].includes(definition.type)) {
                const form = node("form", null, "workspace-inline-form");
                const input = node(definition.type === "enum" ? "select" : "input");
                if (definition.type === "enum") {
                    const placeholder = node("option", t("Выберите значение", "Choose a value")); placeholder.value = ""; placeholder.disabled = true; input.append(placeholder);
                    input.required = true;
                    for (const option of definition.options || []) {
                        const entry = node("option", option.label || String(option.value)); entry.value = JSON.stringify(option.value); input.append(entry);
                    }
                }
                else {
                    input.type = ["integer", "number"].includes(definition.type) ? "number" : "text";
                    if (input.type === "number") input.required = true;
                    for (const bound of ["min", "max", "step"]) if (definition[bound] != null) input[bound] = definition[bound];
                    if (input.type === "number" && definition.step == null) input.step = definition.type === "integer" ? "1" : "any";
                    if (input.type === "text") input.maxLength = 1024;
                }
                input.setAttribute("aria-label", `${definition.label || item.id} · ${name(device)}`);
                input.dataset.workspaceControl = "value";
                const apply = node("button", t("Применить", "Apply")); apply.type = "submit";
                apply.dataset.workspaceControl = "apply";
                form.append(input, apply); controls.append(form); record.input = input; record.apply = apply;
                for (const eventName of ["input", "change"]) input.addEventListener(eventName, () => { input.dataset.dirty = "true"; });
                form.addEventListener("submit", event => {
                    event.preventDefault();
                    if (input.disabled || apply.disabled || !form.reportValidity()) return;
                    if (definition.type === "enum" && !input.value) return;
                    const value = definition.type === "enum" ? JSON.parse(input.value) : input.type === "number" ? Number(input.value) : input.value;
                    if (input.type === "number" && (!input.value.trim() || !Number.isFinite(value))) return;
                    context.command(item.device, item.id, value); delete input.dataset.dirty; refresh();
                });
            }
        }
        if (scalar(definition)) {
            const history = window.GatewayIcons.button("chart", `${t("График показаний", "Readings chart")} · ${definition.label || item.id} · ${name(device)}`,
                () => openHistory(item.device, item.id), "history-open-button");
            history.dataset.workspaceControl = "history"; record.tools.prepend(history);
        }
        const open = button(t("К устройству", "Open device"), () => context.openDevice(item.device), "workspace-device-link");
        open.dataset.workspaceControl = "device"; controls.append(open);
    }

    function updateTile(record) {
        const { item } = record;
        const device = devices().find(candidate => candidate.device === item.device);
        if (item.kind === "scenario") {
            const scenario = context.getAutomations().find(candidate => candidate.automation_id === item.id);
            const busy = scenario && context.scenarioBusy(scenario);
            const manual = scenario?.config?.program?.trigger?.type === "manual";
            const focusedRun = document.activeElement === record.run;
            record.run.disabled = !connected() || !scenario?.enabled || !manual || busy;
            record.run.textContent = busy && connected() ? t("Выполняется…", "Running…") : t("Запустить", "Run");
            record.edit.disabled = !scenario;
            record.test.disabled = !connected() || !scenario;
            const scenarioName = scenario?.config?.name || item.id;
            record.title.textContent = scenarioName;
            record.run.setAttribute("aria-label", `${t("Запустить сценарий", "Run scenario")} «${scenarioName}»`);
            record.edit.setAttribute("aria-label", `${t("Изменить сценарий", "Edit scenario")} «${scenarioName}»`);
            record.test.setAttribute("aria-label", `${t("Тестировать сценарий", "Test scenario")} «${scenarioName}»`);
            record.test.title = t("Симуляция с виртуальными значениями. Команды устройствам не отправляются.", "Simulate with virtual values. No commands are sent to devices.");
            record.status.textContent = !scenario ? t("Сценарий удалён", "Scenario removed") : !manual ? t("Сценарий больше не поддерживает ручной запуск", "This scenario no longer supports manual runs") : !connected() ? t("Нет связи с Gateway", "Gateway disconnected") : busy ? t("Выполняется…", "Running…") : scenario.enabled ? t("Готов к запуску", "Ready to run") : t("Сценарий отключён", "Scenario disabled");
            record.card.dataset.scenarioState = !scenario ? "removed" : !connected() ? "offline" : busy ? "running" : scenario.enabled && manual ? "ready" : "disabled";
            const started = scenario?.last_triggered_at ? context.formatTime(scenario.last_triggered_at) : "";
            const lastStatus = scenario?.last_run_status;
            record.lastRun.hidden = !scenario;
            if (busy) {
                record.lastResult.textContent = !connected()
                    ? t("Связь потеряна во время запуска. Итог будет известен после подключения.", "Connection lost during execution. Reconnect to check the outcome.")
                    : started && scenario.run_status === "running"
                        ? t(`Начало текущего запуска: ${started}`, `Current run started: ${started}`)
                        : t("Ожидается запуск на Gateway…", "Waiting for the Gateway to start…");
            } else {
                const parts = [started, lastStatus ? context.scenarioStatusLabel(lastStatus) : ""].filter(Boolean);
                record.lastResult.textContent = parts.length
                    ? t(`Последний запуск: ${parts.join(" · ")}`, `Last run: ${parts.join(" · ")}`)
                    : t("Ещё не запускался", "Not run yet");
            }
            record.lastRun.dataset.state = busy ? "running" : lastStatus || "none";
            record.lastError.hidden = busy || !scenario?.last_error;
            record.lastError.textContent = !record.lastError.hidden ? t(`Причина: ${scenario.last_error}`, `Reason: ${scenario.last_error}`) : "";
            if (focusedRun && record.run.disabled && !record.edit.disabled) record.edit.focus({ preventScroll: true });
            record.value.hidden = true; return;
        }
        const canControl = Boolean(device && context.canControl(device));
        record.status.textContent = !device ? t("Устройство недоступно", "Device unavailable") : !connected() ? t("Нет связи с Gateway", "Gateway disconnected") : device.connected ? t("На связи", "Online") : t("Отключено · последнее известное значение", "Offline · last known value");
        record.card.dataset.online = String(Boolean(device?.connected && connected()));
        if (item.kind === "action") {
            record.run.disabled = !canControl || !device?.schema?.actions?.[item.id] || context.actionBusy(item.device, item.id);
            record.value.hidden = true; return;
        }
        const definition = device?.schema?.parameters?.[item.id];
        const value = device?.parameters?.[item.id];
        record.value.textContent = definition ? context.formatValue(value, definition) : "—";
        const busy = context.commandBusy(item.device, item.id);
        if (busy && connected()) record.status.textContent = t("Ожидается подтверждение команды…", "Waiting for command confirmation…");
        if (record.toggle) {
            record.toggle.disabled = !canControl || busy || typeof value !== "boolean";
            const known = typeof value === "boolean";
            const action = value ? t("Выключить", "Turn off") : t("Включить", "Turn on");
            record.toggle.textContent = known ? (value ? t("Выкл", "Off") : t("Вкл", "On")) : "—";
            record.toggle.dataset.action = known ? (value ? "off" : "on") : "unknown";
            const caption = `${known ? action : t("Состояние неизвестно", "State unknown")} · ${definition?.label || item.id} · ${name(device || { device: item.device })}`;
            record.toggle.setAttribute("aria-label", caption);
            record.toggle.title = caption;
        }
        if (record.input) {
            record.input.disabled = !canControl || !definition || busy; record.apply.disabled = record.input.disabled;
            if (document.activeElement !== record.input && record.input.dataset.dirty !== "true") record.input.value = record.input.tagName === "SELECT" ? JSON.stringify(value) : (value ?? "");
        }
    }

    function buildHistory() {
        const host = document.getElementById("parameter-history-panel");
        const heading = node("h2", t("История показаний", "Parameter history")); heading.id = "history-title";
        const tools = node("div", null, "history-tools");
        ui.parameter = node("select"); ui.range = node("select");
        for (const [value, ru, en] of [["hour", "Час", "Hour"], ["day", "Сутки", "Day"], ["week", "Неделя", "Week"]]) {
            const option = node("option", t(ru, en)); option.value = value; option.dataset.ru = ru; option.dataset.en = en; ui.range.append(option);
        }
        ui.reload = button(t("Обновить", "Refresh"), () => requestHistory());
        ui.export = button(t("Скачать CSV", "Download CSV"), exportHistory);
        tools.append(field(t("Устройство и параметр", "Device and parameter"), ui.parameter), field(t("Период", "Period"), ui.range), ui.reload, ui.export);
        ui.status = node("p", "", "history-status"); ui.status.setAttribute("role", "status");
        ui.rangeToggle = node("input"); ui.rangeToggle.type = "checkbox"; ui.rangeToggle.checked = showHistoryRange;
        ui.rangeControl = node("label", null, "history-range-control");
        ui.rangeControl.append(ui.rangeToggle, node("span", t("Показывать колебания внутри интервала (min/max)", "Show variation within each interval (min/max)")));
        ui.legend = node("p", "", "history-legend");
        ui.legend.id = "history-legend";
        ui.chart = node("div", null, "history-chart");
        ui.cursor = node("input"); ui.cursor.type = "range"; ui.cursor.min = "0"; ui.cursor.step = "1"; ui.cursor.setAttribute("aria-label", t("Выбрать точку графика", "Select chart point"));
        ui.readout = node("p", "", "history-readout"); ui.readout.setAttribute("aria-live", "polite");
        ui.tableDetails = node("details", null, "history-table-details");
        ui.tableDetails.append(node("summary", t("Таблица показаний", "Readings table"))); ui.table = node("div", null, "history-table-scroll"); ui.tableDetails.append(ui.table);
        host.append(heading, tools, ui.status, ui.rangeControl, ui.legend, ui.chart, ui.cursor, ui.readout, ui.tableDetails);
        ui.parameter.addEventListener("change", () => { selected = ui.parameter.value; selectedSchemaKey = selectionSchemaSignature(); requestHistory(); });
        ui.range.addEventListener("change", () => requestHistory());
        ui.rangeToggle.addEventListener("change", () => {
            showHistoryRange = ui.rangeToggle.checked;
            try { localStorage.setItem(HISTORY_RANGE_KEY, String(showHistoryRange)); } catch (_) { /* Current view still works. */ }
            renderHistory();
        });
        ui.cursor.addEventListener("input", () => { selectedPointTimestamp = points[Number(ui.cursor.value)]?.timestamp || null; updateReadout(); });
        ui.chart.addEventListener("click", event => {
            if (!points.length) return;
            const svg = ui.chart.querySelector("svg"), rect = svg?.getBoundingClientRect();
            if (!rect?.width) return;
            const width = svg.viewBox.baseVal.width;
            const relative = Math.max(0, Math.min(1, ((event.clientX - rect.left) / rect.width * width - 64) / (width - 80)));
            const timestamp = Date.parse(points[0].timestamp) + relative * (Date.parse(points.at(-1).timestamp) - Date.parse(points[0].timestamp));
            let closest = 0;
            for (let index = 1; index < points.length; index++) if (Math.abs(Date.parse(points[index].timestamp) - timestamp) < Math.abs(Date.parse(points[closest].timestamp) - timestamp)) closest = index;
            ui.cursor.value = String(closest); selectedPointTimestamp = points[closest].timestamp; updateReadout();
        });
    }

    function historyCatalog() {
        const options = choices().filter(item => item.kind === "parameter" && scalar(devices().find(device => device.device === item.device)?.schema?.parameters?.[item.id]));
        const signature = JSON.stringify([context.language(), options.map(item => [item, devices().find(device => device.device === item.device)?.schema?.parameters?.[item.id]])]);
        if (signature === catalogKey) return;
        catalogKey = signature;
        const previous = selected; ui.parameter.replaceChildren();
        for (const item of options) { const option = node("option", `${item.title} (${item.device})`); option.value = key(item); ui.parameter.append(option); }
        if (!options.length) { const option = node("option", t("Нет числовых или логических параметров", "No numeric or boolean parameters")); option.value = ""; ui.parameter.append(option); }
        selected = options.some(item => key(item) === previous) ? previous : options[0] ? key(options[0]) : "";
        ui.parameter.value = selected; ui.parameter.disabled = !options.length;
        const schemaKey = selectionSchemaSignature();
        const schemaChanged = schemaKey !== selectedSchemaKey;
        selectedSchemaKey = schemaKey;
        if (selected !== previous || schemaChanged) { clearHistory(); if (context.activePage() === "overview" && connected()) requestHistory(); }
    }

    function clearHistory() {
        clearTimeout(historyTimer); pending = null; points = []; response = null; chartKey = ""; historyState = "idle"; historyError = ""; selectedPointTimestamp = null;
    }

    function requestHistory() {
        clearTimeout(historyTimer); pending = null;
        if (historyUnsupported) { renderHistory(); return; }
        if (!selected || !connected()) { clearHistory(); renderHistory(); return; }
        const [, device, parameter] = JSON.parse(selected);
        // A previous parameter/range must not appear under a new selector while loading.
        if (response && (response.device !== device || response.parameter !== parameter || response.range !== ui.range.value)) {
            points = []; response = null; chartKey = ""; selectedPointTimestamp = null;
        }
        pending = { request: id(), selected, range: ui.range.value };
        historyState = "loading"; historyError = "";
        if (!context.send("get_parameter_history", { request_id: pending.request, device, parameter, range: pending.range, max_points: 360 })) {
            pending = null; historyState = "error"; historyError = t("Не удалось отправить запрос.", "Could not send request.");
        } else historyTimer = setTimeout(() => { if (!pending) return; pending = null; historyState = "error"; historyError = t("Нет ответа от Gateway. Повторите запрос.", "Gateway did not respond. Try again."); renderHistory(); }, 12000);
        renderHistory();
    }

    function renderHistory() {
        if (!ui.status) return;
        ui.reload.disabled = !connected() || historyUnsupported || !selected || Boolean(pending); ui.export.disabled = !points.length || Boolean(pending);
        ui.status.textContent = !connected() ? t("Нет связи с Gateway. Сохранённый график остаётся на экране.", "Gateway disconnected. The loaded chart remains visible.") : historyUnsupported ? unsupportedHistoryMessage() : !selected ? t("Подключите устройство с числовым или логическим параметром.", "Connect a device with a numeric or boolean parameter.") : historyState === "loading" ? t("Загружаю показания…", "Loading readings…") : historyState === "error" ? historyError : historyState === "idle" ? t("Выберите параметр и нажмите «Обновить».", "Select a parameter and choose Refresh.") : (!points.length ? t("Показаний за этот период пока нет. История собирается с момента включения записи.", "No readings in this period yet. History starts when recording is enabled.") : t(`Показано ${points.length} точек.`, `${points.length} points shown.`)) + (response?.recording_enabled === false ? t(" Запись этого параметра отключена.", " Recording for this parameter is disabled.") : "");
        ui.status.dataset.state = historyUnsupported ? "error" : historyState;
        const hasVariation = points.some(point => point.min !== point.max);
        ui.rangeControl.hidden = !points.length || !hasVariation;
        ui.legend.hidden = !points.length;
        ui.legend.textContent = response?.value_type === "boolean"
            ? t("Синяя линия — последнее состояние в интервале: 0 — выключено, 1 — включено.", "The blue line is the last state in each interval: 0 is off, 1 is on.")
            : t("Синяя линия — среднее значение в интервале.", "The blue line is the average value in each interval.");
        if (hasVariation) ui.legend.textContent += showHistoryRange
            ? t(" Голубые полосы — минимум и максимум внутри интервала.", " Light blue bars show the minimum and maximum within each interval.")
            : response?.value_type === "boolean"
                ? t(" Внутри некоторых интервалов были переключения; их диапазон можно показать флажком min/max.", "Some intervals contain state changes; select min/max to see their range.")
                : t(" Диапазон колебаний внутри интервала можно показать флажком min/max.", "Select min/max to see the variation within each interval.");
        const host = document.getElementById("parameter-history-panel");
        const hostStyle = getComputedStyle(host);
        const width = Math.max(160, Math.floor(ui.chart.clientWidth || host.clientWidth - parseFloat(hostStyle.paddingLeft) - parseFloat(hostStyle.paddingRight) - 4 || 800));
        const signature = JSON.stringify([context.language(), selected, ui.range.value, points, response?.unit, response?.value_type, context.timezone(), width, showHistoryRange]);
        if (chartKey === signature) return;
        chartKey = signature; ui.chart.replaceChildren(); ui.table.replaceChildren();
        ui.cursor.hidden = ui.readout.hidden = ui.tableDetails.hidden = !points.length;
        if (!points.length) return;
        const svgNode = (tag, attributes, text) => {
            const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
            for (const [attribute, value] of Object.entries(attributes)) element.setAttribute(attribute, value);
            if (text != null) element.textContent = text; return element;
        };
        const boolean = response?.value_type === "boolean";
        let low = Math.min(...points.map(point => Math.min(point.min, point.value))), high = Math.max(...points.map(point => Math.max(point.max, point.value)));
        if (boolean) { low = 0; high = 1; }
        // Normalize before subtraction: finite values near +/-Number.MAX_VALUE
        // otherwise produce an infinite range and invalid SVG coordinates.
        const scale = Math.max(Math.abs(low), Math.abs(high), low === 0 && high === 0 ? 1 : Number.MIN_VALUE);
        let scaledLow = low / scale, scaledHigh = high / scale;
        if (scaledLow === scaledHigh) { scaledLow -= .05; scaledHigh += .05; }
        const start = Date.parse(points[0].timestamp), finish = Date.parse(points.at(-1).timestamp);
        const left = 64, right = width - 16;
        const x = point => left + (finish === start ? .5 : (Date.parse(point.timestamp) - start) / (finish - start)) * (right - left);
        const y = value => 210 - (value / scale - scaledLow) / (scaledHigh - scaledLow) * 180;
        const caption = `${ui.parameter.selectedOptions[0]?.textContent || ""}${response?.unit ? ` · ${response.unit}` : ""}`;
        const svg = svgNode("svg", { viewBox: `0 0 ${width} 260`, role: "img", "aria-describedby": "history-legend", "aria-label": caption + ". " + t("Точные значения доступны в таблице ниже.", "Exact readings are available in the table below.") });
        const axisValue = value => {
            if (Math.abs(value) >= 1e6 || value !== 0 && Math.abs(value) < .001) return value.toExponential(1);
            return new Intl.NumberFormat(context.language(), { maximumSignificantDigits: 4 }).format(value);
        };
        for (let index = 0; index <= 4; index++) {
            const normalized = scaledLow + (scaledHigh - scaledLow) * index / 4;
            const value = Math.max(-Number.MAX_VALUE, Math.min(Number.MAX_VALUE, normalized * scale));
            const position = 210 - index / 4 * 180;
            svg.append(svgNode("line", { x1: left, x2: right, y1: position, y2: position, class: "history-grid-line" }));
            svg.append(svgNode("text", { x: left - 8, y: position + 4, "text-anchor": "end", class: "history-axis" }, boolean ? (index === 0 ? "0" : index === 4 ? "1" : "") : axisValue(value)));
        }
        if (showHistoryRange) for (const point of points) if (point.min !== point.max) svg.append(svgNode("line", { x1: x(point), x2: x(point), y1: y(point.min), y2: y(point.max), class: "history-envelope" }));
        const path = points.map((point, index) => `${index ? (boolean ? `H${x(point)}V` : "L") : "M"}${index && boolean ? y(point.value) : `${x(point)},${y(point.value)}`}`).join(" ");
        svg.append(svgNode("path", { d: path, class: "history-series" }));
        if (points.length === 1) svg.append(svgNode("circle", { cx: x(points[0]), cy: y(points[0].value), r: 4, class: "history-point" }));
        const timeLabel = timestamp => new Intl.DateTimeFormat(context.language(), {
            ...(ui.range.value === "week" ? { day: "2-digit", month: "2-digit" } : { hour: "2-digit", minute: "2-digit" }),
            ...(context.timezone() && context.timezone() !== "local" ? { timeZone: context.timezone() } : {})
        }).format(new Date(timestamp));
        svg.append(svgNode("text", { x: left, y: 240, class: "history-axis" }, timeLabel(points[0].timestamp)));
        if (points.length > 1) svg.append(svgNode("text", { x: right, y: 240, "text-anchor": "end", class: "history-axis" }, timeLabel(points.at(-1).timestamp)));
        ui.chart.append(svg); ui.cursor.max = String(points.length - 1);
        const pointIndex = selectedPointTimestamp && points.findIndex(point => point.timestamp === selectedPointTimestamp);
        ui.cursor.value = String(pointIndex != null && pointIndex >= 0 ? pointIndex : points.length - 1); updateReadout();
        const table = node("table");
        const tableCaption = node("caption", t("Сохранённые точки; при объединении показаны минимум и максимум интервала.", "Stored points; aggregated intervals include their minimum and maximum."));
        const head = node("thead"), row = node("tr");
        for (const title of [t("Время", "Time"), t("Значение", "Value"), t("Минимум", "Minimum"), t("Максимум", "Maximum"), t("Замеров", "Samples")]) { const cell = node("th", title); cell.scope = "col"; row.append(cell); }
        head.append(row); const body = node("tbody");
        for (const point of points) { const row = node("tr"); for (const value of [context.formatTime(point.timestamp), point.value, point.min, point.max, point.count]) row.append(node("td", value)); body.append(row); }
        table.append(tableCaption, head, body); ui.table.append(table);
    }

    function updateReadout() {
        const point = points[Number(ui.cursor.value)]; if (!point) return;
        const value = response?.value_type === "boolean" ? (point.value ? t("Вкл", "On") : t("Выкл", "Off")) : `${point.value}${response?.unit ? ` ${response.unit}` : ""}`;
        ui.readout.textContent = `${context.formatTime(point.timestamp)} · ${value}`;
    }

    function exportHistory() {
        if (!points.length || pending) return;
        const escape = value => {
            let text = String(value ?? "");
            if (typeof value === "string" && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
            return `"${text.replaceAll('"', '""')}"`;
        };
        const rows = [["timestamp", "value", "min", "max", "count", "unit"], ...points.map(point => [point.timestamp, point.value, point.min, point.max, point.count, response?.unit || ""])];
        const url = URL.createObjectURL(new Blob(["\uFEFF" + rows.map(row => row.map(escape).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" }));
        const anchor = node("a"); anchor.href = url; anchor.download = "parameter-history.csv"; anchor.hidden = true;
        document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function buildSettings() {
        const host = document.querySelector("#settings-page .settings-card");
        const section = node("section", null, "settings-group history-settings");
        const heading = node("div", null, "settings-group-heading"); const copy = node("div");
        copy.append(node("h2", t("История показаний", "Parameter history")), node("p", t("Запись датчиков и состояний хранится отдельно от событий", "Sensor and state readings are stored separately from events")));
        heading.append(node("span", "⌁", "settings-group-icon"), copy);
        const form = node("form", null, "history-settings-form");
        settingsUi.enabled = node("input"); settingsUi.enabled.type = "checkbox";
        const toggle = node("label", null, "history-record-toggle"); toggle.append(settingsUi.enabled, node("span", t("Записывать показания", "Record readings")));
        settingsUi.days = node("input"); settingsUi.days.type = "number"; settingsUi.days.min = "1"; settingsUi.days.max = "365"; settingsUi.days.required = true;
        settingsUi.interval = node("input"); settingsUi.interval.type = "number"; settingsUi.interval.min = "1"; settingsUi.interval.max = "3600"; settingsUi.interval.required = true;
        settingsUi.save = node("button", t("Сохранить", "Save")); settingsUi.save.type = "submit";
        settingsUi.feedback = node("p", "", "management-feedback"); settingsUi.feedback.setAttribute("role", "status");
        form.append(toggle, field(t("Хранить, дней", "Retention, days"), settingsUi.days), field(t("Интервал записи, секунд", "Recording interval, seconds"), settingsUi.interval), settingsUi.save, settingsUi.feedback);
        form.addEventListener("input", () => { settingsDirty = true; renderSettings(); });
        form.addEventListener("submit", event => {
            event.preventDefault(); if (settingsRequest || !connected() || historyUnsupported) return;
            const days = Number(settingsUi.days.value), interval = Number(settingsUi.interval.value);
            if (!form.reportValidity() || !Number.isInteger(days) || days < 1 || days > 365 || !Number.isInteger(interval) || interval < 1 || interval > 3600) return;
            settingsRequest = id();
            const payload = { request_id: settingsRequest, retention_days: days, enabled: settingsUi.enabled.checked, interval_seconds: interval };
            settingsFeedback(t("Сохраняю…", "Saving…"), "info");
            if (!context.send("set_history_settings", payload)) { settingsRequest = null; settingsFeedback(t("Не удалось отправить запрос.", "Could not send request.")); }
            else settingsTimer = setTimeout(() => { settingsRequest = null; settingsFeedback(t("Нет ответа от Gateway. Проверьте связь и повторите.", "No reply from Gateway. Check the connection and try again.")); renderSettings(); }, 12000);
            renderSettings();
        });
        section.append(heading, form);
        const savedNotice = host.querySelector("#settings-saved");
        if (savedNotice) savedNotice.before(section);
        else host.append(section);
        settingsUi.section = section;
    }

    function settingsFeedback(text, state = "error") {
        settingsUi.feedback.textContent = text;
        settingsUi.feedback.dataset.state = state;
    }

    function unsupportedHistoryMessage() {
        return t("Запущенная версия Gateway не поддерживает историю. Перезапустите Gateway и обновите страницу.", "The running Gateway version does not support history. Restart the Gateway and refresh this page.");
    }

    function markHistoryUnsupported() {
        historyUnsupported = true;
        clearTimeout(historyTimer); clearTimeout(settingsTimer); pending = null; settingsRequest = null;
        settingsFeedback(unsupportedHistoryMessage()); renderHistory(); renderSettings();
    }

    function renderSettings() {
        if (!settingsUi.days) return;
        if (historyUnsupported) settingsFeedback(unsupportedHistoryMessage());
        if (settings && !settingsDirty && !settingsRequest) {
            settingsUi.enabled.checked = settings.enabled; settingsUi.days.value = settings.retention_days; settingsUi.interval.value = settings.interval_seconds;
        }
        for (const control of [settingsUi.enabled, settingsUi.days, settingsUi.interval]) control.disabled = !connected() || historyUnsupported || !settings || Boolean(settingsRequest);
        settingsUi.save.disabled = !connected() || historyUnsupported || !settings || !settingsDirty || Boolean(settingsRequest);
    }

    function readSettings(message) {
        const value = message.settings || message;
        return typeof value.enabled === "boolean" && Number.isInteger(value.retention_days) && value.retention_days >= 1 && value.retention_days <= 365 &&
            Number.isInteger(value.interval_seconds) && value.interval_seconds >= 1 && value.interval_seconds <= 3600 ? value : null;
    }

    function refresh() {
        if (!context) return;
        renderTiles(); historyCatalog(); renderHistory(); renderSettings();
    }

    function openHistory(device, parameter) {
        selected = JSON.stringify(["parameter", device, parameter]);
        selectedSchemaKey = selectionSchemaSignature();
        context.setActivePage("overview"); historyCatalog(); ui.parameter.value = selected;
        if (!pending || pending.selected !== selected || pending.range !== ui.range.value) requestHistory();
        document.getElementById("parameter-history-panel").scrollIntoView({ block: "start", behavior: "smooth" }); ui.parameter.focus({ preventScroll: true });
    }

    window.GatewayWorkspace = {
        initialize(options) {
            context = options;
            try {
                const stored = JSON.parse(localStorage.getItem(LAYOUT_KEY));
                if (Array.isArray(stored)) {
                    const unique = new Map();
                    for (const item of stored) {
                        if (item && ["parameter", "action", "scenario"].includes(item.kind) && typeof item.id === "string" && item.id.length > 0 && item.id.length <= 100 &&
                            (item.kind === "scenario" || typeof item.device === "string" && item.device.length > 0 && item.device.length <= 128)) {
                            const normalized = { kind: item.kind, ...(item.kind !== "scenario" ? { device: item.device } : {}), id: item.id };
                            unique.set(key(normalized), normalized);
                        }
                    }
                    layout = [...unique.values()].slice(0, MAX_TILES);
                }
            } catch (_) { /* Start with useful default tiles. */ }
            buildHistory(); buildSettings(); document.getElementById("workspace-customize").addEventListener("click", customize); refresh();
            const orderStatus = node("p", "", "workspace-order-status"); orderStatus.id = "workspace-order-status"; orderStatus.setAttribute("role", "status");
            document.getElementById("workspace-tiles").after(orderStatus);
            const resize = () => {
                if (resizeFrame != null) return;
                resizeFrame = requestAnimationFrame(() => { resizeFrame = null; if (context.activePage() === "overview") renderHistory(); });
            };
            if (window.ResizeObserver) new ResizeObserver(resize).observe(document.getElementById("parameter-history-panel"));
            window.addEventListener("resize", resize);
            setInterval(() => { if (!document.hidden && context.activePage() === "overview" && connected() && !historyUnsupported && selected && !pending) requestHistory(); }, 30000);
        },
        connected() { historyUnsupported = false; context.send("get_history_settings"); if (context.activePage() === "overview" && !pending) requestHistory(); },
        connectionChanged() {
            historyUnsupported = false;
            if (!connected()) {
                clearTimeout(historyTimer); clearTimeout(settingsTimer); pending = null;
                if (historyState === "loading") historyState = response ? "ready" : "idle";
                if (settingsRequest) settingsFeedback(t("Связь прервана. Настройки могли сохраниться; проверьте их после подключения.", "Connection lost. Settings may have been saved; check them after reconnecting."), "warning");
                settingsRequest = null;
            }
            refresh();
        },
        pageChanged() {
            if (context.activePage() !== "overview") cleanDrag();
            refresh();
            if (context.activePage() === "overview" && !points.length && selected && connected() && !pending) requestHistory();
        },
        languageChanged() {
            // Rebuild only static captions; keep selection, loaded points and field drafts.
            const range = ui.range.value, tableOpen = ui.tableDetails.open;
            const draft = { enabled: settingsUi.enabled.checked, days: settingsUi.days.value, interval: settingsUi.interval.value, feedback: settingsUi.feedback.textContent, state: settingsUi.feedback.dataset.state };
            const host = document.getElementById("parameter-history-panel"); host.replaceChildren(); buildHistory();
            ui.range.value = range; ui.tableDetails.open = tableOpen;
            settingsUi.section.remove(); buildSettings();
            settingsUi.enabled.checked = draft.enabled; settingsUi.days.value = draft.days; settingsUi.interval.value = draft.interval; settingsFeedback(draft.feedback, draft.state || "info");
            catalogKey = tileKey = chartKey = ""; refresh();
        },
        handleMessage(message) {
            if (message.type === "gateway_capabilities") {
                if (Array.isArray(message.features) && !message.features.includes("parameter_history")) markHistoryUnsupported();
                return true;
            }
            if (message.type === "error") {
                const unknownHistoryAction = typeof message.message === "string" && /^(?:Неизвестное действие|Unknown action):\s*(?:get_parameter_history|get_history_settings|set_history_settings)\s*$/i.test(message.message);
                const explicitUnsupported = ["unknown_action", "unsupported_action"].includes(message.code || message.error_code) && ["get_parameter_history", "get_history_settings", "set_history_settings"].includes(message.action);
                if (unknownHistoryAction || explicitUnsupported) { markHistoryUnsupported(); return true; }
                if (pending && message.request_id === pending.request) {
                    clearTimeout(historyTimer); pending = null; historyState = "error";
                    historyError = message.message || t("Не удалось загрузить историю.", "Could not load history.");
                    renderHistory(); return true;
                }
                if (settingsRequest && message.request_id === settingsRequest) {
                    clearTimeout(settingsTimer); settingsRequest = null;
                    settingsFeedback(message.message || t("Не удалось сохранить настройки.", "Could not save settings.")); renderSettings(); return true;
                }
                return false;
            }
            if (message.type === "parameter_history") {
                if (!pending || pending.request !== message.request_id || pending.selected !== selected || pending.range !== ui.range.value) return true;
                const [, device, parameter] = JSON.parse(pending.selected);
                if (message.success !== false && (message.device !== device || message.parameter !== parameter || message.range !== pending.range)) return true;
                clearTimeout(historyTimer); pending = null;
                if (message.success === false) { historyState = "error"; historyError = message.message || t("Не удалось загрузить историю.", "Could not load history."); points = []; response = null; }
                else {
                    response = message;
                    points = (Array.isArray(message.points) ? message.points : []).filter(point => point && Number.isFinite(Date.parse(point.timestamp)) &&
                        Number.isFinite(point.value) && Number.isFinite(point.min) && Number.isFinite(point.max) && point.min <= point.max &&
                        (message.value_type !== "boolean" || [0, 1].includes(point.value) && [0, 1].includes(point.min) && [0, 1].includes(point.max)))
                        .slice(0, 500).map(point => ({ ...point, count: Number.isInteger(point.count) && point.count > 0 ? point.count : 1 }))
                        .sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
                    historyState = "ready";
                }
                renderHistory(); return true;
            }
            if (message.type === "history_settings") {
                const next = message.success === false ? null : readSettings(message);
                if (next) {
                    const changed = settings && ["enabled", "retention_days", "interval_seconds"].some(field => settings[field] !== next[field]);
                    settings = next;
                    if (changed && context.activePage() === "overview" && connected() && !pending) requestHistory();
                }
                else settingsFeedback(message.message || t("Не удалось загрузить настройки истории.", "Could not load history settings."));
                renderSettings(); return true;
            }
            if (message.type === "history_settings_result") {
                if (!settingsRequest || message.request_id !== settingsRequest) return true;
                clearTimeout(settingsTimer); settingsRequest = null;
                if (message.success) { settingsDirty = false; settings = readSettings(message) || settings; context.send("get_history_settings"); }
                settingsFeedback(message.message || (message.success ? t("Настройки истории сохранены.", "History settings saved.") : t("Не удалось сохранить настройки.", "Could not save settings.")), message.success ? "success" : "error");
                renderSettings(); return true;
            }
            if (message.type === "device_identity_result" && message.success && layout && message.device !== message.new_device_id) {
                layout = layout.map(item => item.device === message.device ? { ...item, device: message.new_device_id } : item); save();
            }
            return false;
        },
        attachDevice(device, panel) {
            for (const row of panel.querySelectorAll(".device-parameter")) {
                const parameter = row.dataset.parameterId;
                if (!scalar(device.schema?.parameters?.[parameter])) continue;
                if (row.querySelector('[data-role="parameter-history"]')) continue;
                const open = window.GatewayIcons.button("chart", `${t("График показаний", "Readings chart")} · ${device.schema.parameters[parameter].label || parameter} · ${name(device)}`,
                    () => openHistory(device.device, parameter), "history-open-button");
                open.dataset.role = "parameter-history"; open.dataset.parameterId = parameter;
                const value = row.querySelector('[data-role="parameter-value"]');
                if (!value) continue;
                row.classList.add("has-history");
                const reading = node("div", null, "device-parameter-reading");
                value.replaceWith(reading); reading.append(value, open);
            }
        },
        refresh, openHistory
    };
})();
