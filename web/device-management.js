/* Groups, explicitly mapped common commands, health and shared retention settings. */
(() => {
    "use strict";
    let context = null;
    let groups = [];
    let groupsReady = false;
    let groupFilter = "all";
    let online = false;
    let activeBulk = null;
    let bulkTimer = null;
    let lastBulk = null;
    let groupDialog = null;
    let deviceSettingsDialog = null;
    let retention = null;
    let retentionDirty = false;
    let retentionPending = false;
    let retentionTimer = null;
    let dialogSequence = 0;
    const healthBaselines = new Map();
    const byId = id => document.getElementById(id);
    const t = (ru, en) => context?.localized(ru, en) || ru;
    function setFeedback(element, text, outcome = "info") {
        if (!element) return;
        element.textContent = text || "";
        element.dataset.state = outcome === true ? "success" : outcome === false ? "error" : outcome;
    }
    const node = (tag, text, className) => {
        const result = document.createElement(tag);
        if (text != null) result.textContent = text;
        if (className) result.className = className;
        return result;
    };
    const deviceName = device => device.display_name || device.name || device.device;
    const getDevices = () => context?.getDevices() || [];
    const requestId = () => window.crypto?.randomUUID?.() || `management-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const groupName = id => groups.find(group => group.group_id === id)?.name || id;
    const writable = device => Object.entries(device.schema?.parameters || {}).filter(([, definition]) => definition.access === "read_write");
    const offOperation = device => device.common_commands?.off || null;

    function option(select, value, caption) {
        const item = node("option", caption);
        item.value = value;
        select.append(item);
    }

    function label(caption, input) {
        const result = node("label", null, "management-field");
        result.append(node("span", caption), input);
        return result;
    }

    function button(caption, onClick, className) {
        const result = node("button", caption, className);
        result.type = "button";
        if (onClick) result.addEventListener("click", onClick);
        return result;
    }

    function dialog(title) {
        const result = node("dialog", null, "management-dialog");
        const opener = document.activeElement;
        result.setAttribute("aria-label", title);
        const heading = node("h2", title, "management-dialog-title");
        heading.id = `management-dialog-title-${++dialogSequence}`;
        result.setAttribute("aria-labelledby", heading.id);
        result.append(heading);
        const body = node("div", null, "management-dialog-body");
        result.append(body);
        document.body.append(result);
        result.addEventListener("close", () => {
            result.remove();
            const active = document.activeElement;
            if (active && active !== document.body && active !== opener && !active.closest("[hidden]")) return;
            if (opener?.isConnected && !opener.disabled && !opener.closest("[hidden]")) {
                opener.focus({ preventScroll: true });
                return;
            }
            const panel = [...document.querySelectorAll(".device-panel")]
                .find(item => item.dataset.deviceId === result.dataset.deviceId && !item.closest("[hidden]"));
            panel?.querySelector('[data-role="device-settings"]')?.focus({ preventScroll: true });
        });
        result.showModal();
        queueMicrotask(() => {
            if (result.open && document.activeElement === result) {
                result.querySelector("input:not(:disabled), select:not(:disabled), button:not(:disabled)")?.focus({ preventScroll: true });
            }
        });
        return { root: result, body };
    }

    function requestGroups() {
        return context?.send("get_device_groups");
    }

    function refresh() {
        if (!context) return;
        const deviceIds = new Set(getDevices().map(device => device.device));
        for (const id of healthBaselines.keys()) if (!deviceIds.has(id)) healthBaselines.delete(id);
        const select = byId("device-group-filter");
        if (select) {
            select.replaceChildren();
            option(select, "all", t("Все группы", "All groups"));
            option(select, "ungrouped", t("Без группы", "Ungrouped"));
            for (const group of groups) option(select, group.group_id, `${group.name} (${getDevices().filter(device => device.group_id === group.group_id).length})`);
            if (groupFilter !== "all" && groupFilter !== "ungrouped" && !groups.some(group => group.group_id === groupFilter)) groupFilter = "all";
            select.value = groupFilter;
            select.disabled = !groupsReady;
        }
        const all = getDevices();
        const groupDevices = all.filter(device => device.group_id === groupFilter);
        const offAll = byId("common-off-all");
        const offGroup = byId("common-off-group");
        if (offAll) {
            offAll.disabled = !online || Boolean(activeBulk) || !all.some(offOperation);
            offAll.title = t("Для всех устройств с назначенным действием выключения, независимо от фильтров", "All devices with an assigned off operation, regardless of filters");
        }
        if (offGroup) {
            offGroup.disabled = !online || Boolean(activeBulk) || !groups.some(group => group.group_id === groupFilter) || !groupDevices.some(offOperation);
            offGroup.title = t("Выберите группу и назначьте устройствам общее выключение", "Select a group and assign off operations to its devices");
        }
        if (byId("device-groups-manage")) byId("device-groups-manage").disabled = !online || !groupsReady;
        renderRetention();
        renderBulk();
        refreshDeviceSettings();
        window.DeviceViews?.refresh();
    }

    function scopeDevices(groupId) {
        return getDevices().filter(device => groupId == null || device.group_id === groupId);
    }

    function executeOff(groupId) {
        if (!online || activeBulk) return;
        const scope = scopeDevices(groupId);
        const configured = scope.filter(offOperation);
        if (!configured.length) return;
        const scopeName = groupId == null ? t("все устройства", "all devices") : t(`группа «${groupName(groupId)}»`, `group “${groupName(groupId)}”`);
        const lines = configured.slice(0, 15).map(device => `• ${deviceName(device)} (${device.device})`).join("\n");
        const offline = configured.filter(device => !device.connected).length;
        const prompt = t(
            `Выполнить общее выключение: ${scopeName}?\n\nНазначено: ${configured.length} из ${scope.length}. Не в сети: ${offline}.\n${lines}${configured.length > 15 ? "\n…" : ""}\n\nБудут выполнены назначенные команды. Недоступные устройства будут пропущены. Физическое отключение питания ESP32 не подразумевается.`,
            `Run common off for ${scopeName}?\n\nAssigned: ${configured.length} of ${scope.length}. Offline: ${offline}.\n${lines}${configured.length > 15 ? "\n…" : ""}\n\nAssigned operations will run. Unavailable devices will be skipped. This does not imply cutting power to the ESP32.`
        );
        if (!window.confirm(prompt)) return;
        const id = requestId();
        if (!context.send("execute_common_command", { command: "off", group_id: groupId, request_id: id })) return;
        activeBulk = id;
        lastBulk = { pending: true, results: [], group_id: groupId };
        clearTimeout(bulkTimer);
        bulkTimer = setTimeout(() => {
            if (activeBulk !== id) return;
            activeBulk = null;
            lastBulk = { ...lastBulk, pending: false, uncertain: true };
            refresh();
        }, 120000);
        refresh();
        byId("common-command-results")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function renderBulk() {
        const details = byId("common-command-results");
        if (!details || !lastBulk) return;
        details.hidden = false;
        details.open = true;
        const results = Array.isArray(lastBulk.results) ? lastBulk.results : [];
        const succeeded = results.filter(result => result.status === "succeeded").length;
        const failed = results.filter(result => result.status === "failed").length;
        const skipped = results.filter(result => result.status === "skipped").length;
        byId("common-command-summary").textContent = lastBulk.pending
            ? t("Общее выключение: ожидаются подтверждения…", "Common off: waiting for confirmations…")
            : lastBulk.uncertain
                ? t("Результат общего выключения не подтверждён", "Common off result is not confirmed")
                : t(`Общее выключение: выполнено ${succeeded}, ошибок ${failed}, пропущено ${skipped}`, `Common off: ${succeeded} succeeded, ${failed} failed, ${skipped} skipped`);
        byId("common-command-note").textContent = lastBulk.uncertain
            ? t("Проверьте фактическое состояние устройств перед повторным запуском.", "Check the actual device states before running this again.")
            : lastBulk.message || t("Подтверждение относится к назначенной операции каждого устройства.", "Confirmation refers to the assigned operation of each device.");
        const list = byId("common-command-result-list");
        list.replaceChildren();
        const statuses = {
            succeeded: t("Выполнено", "Succeeded"), failed: t("Ошибка", "Failed"),
            skipped: t("Пропущено", "Skipped"), queued: t("В очереди", "Queued")
        };
        for (const result of results) {
            const item = node("li", `${result.device || "—"} · ${statuses[result.status] || result.status || "—"}${result.message ? ` · ${result.message}` : ""}`);
            item.dataset.state = result.status || "unknown";
            list.append(item);
        }
    }

    function openGroups() {
        if (!online || groupDialog?.root.open) return;
        requestGroups();
        const view = dialog(t("Группы устройств", "Device groups"));
        groupDialog = { ...view };
        const form = node("form", null, "management-group-form");
        const input = node("input");
        input.required = true;
        input.maxLength = 60;
        input.placeholder = t("Например, Гостиная", "For example, Living room");
        const add = button(t("Создать группу", "Create group"));
        add.type = "submit";
        form.append(label(t("Название новой группы", "New group name"), input), add);
        const list = node("div", null, "management-group-list");
        const drafts = new Map();
        const feedback = node("p", "", "management-feedback");
        feedback.setAttribute("aria-live", "polite");
        groupDialog.feedback = feedback;
        view.body.append(form, list, feedback, node("p", t("У каждого устройства одна группа. Удаление группы сохраняет устройства и их команды.", "Each device belongs to one group. Deleting a group keeps its devices and commands."), "management-help"), button(t("Закрыть", "Close"), () => view.root.close()));
        form.addEventListener("submit", event => {
            event.preventDefault();
            const name = input.value.trim();
            if (!name || !online) return;
            if (context.send("save_device_group", { name })) {
                add.disabled = true;
                setFeedback(feedback, t("Сохраняю…", "Saving…"), "info");
            }
        });
        groupDialog.render = () => {
            for (const field of list.querySelectorAll("input[data-group-id]")) {
                if (field.value !== field.dataset.savedValue) drafts.set(field.dataset.groupId, field.value);
            }
            list.replaceChildren();
            add.disabled = !online;
            if (!groups.length) list.append(node("p", t("Групп пока нет.", "No groups yet.")));
            for (const group of groups) {
                const row = node("div", null, "management-group-row");
                const name = node("input");
                name.value = drafts.get(group.group_id) ?? group.name;
                name.dataset.groupId = group.group_id;
                name.dataset.savedValue = group.name;
                name.maxLength = 60;
                name.setAttribute("aria-label", t("Название группы", "Group name"));
                const save = button(t("Переименовать", "Rename"), () => {
                    if (!name.value.trim()) return;
                    if (context.send("save_device_group", { group_id: group.group_id, name: name.value.trim() })) {
                        save.disabled = true;
                        setFeedback(feedback, t("Сохраняю…", "Saving…"));
                    }
                });
                const remove = button(t("Удалить", "Delete"), () => {
                    if (!window.confirm(t(`Удалить группу «${group.name}»? Устройства останутся без группы.`, `Delete group “${group.name}”? Its devices will become ungrouped.`))) return;
                    if (context.send("delete_device_group", { group_id: group.group_id })) {
                        remove.disabled = true;
                        setFeedback(feedback, t("Удаляю…", "Deleting…"));
                    }
                });
                save.disabled = remove.disabled = !online;
                row.append(name, node("small", `${getDevices().filter(device => device.group_id === group.group_id).length}`), save, remove);
                list.append(row);
            }
        };
        groupDialog.clearDrafts = () => { drafts.clear(); list.replaceChildren(); };
        view.root.addEventListener("close", () => { if (groupDialog?.root === view.root) groupDialog = null; });
        groupDialog.render();
        input.focus();
    }

    function operationDescription(device, operation) {
        if (!operation) return t("Не назначено", "Not assigned");
        if (operation.kind === "parameter") {
            const definition = device.schema?.parameters?.[operation.name];
            const value = definition?.type === "enum"
                ? definition.options?.find(item => typeof item.value === typeof operation.value && item.value === operation.value)?.label ?? operation.value
                : typeof operation.value === "boolean" ? (operation.value ? t("Вкл", "On") : t("Выкл", "Off")) : operation.value;
            return `${definition?.label || operation.name} = ${String(value)}`;
        }
        return device.schema?.actions?.[operation.name]?.label || operation.name;
    }

    function valueEditor(definition, value, optional = false) {
        let input;
        if (definition.type === "boolean" || definition.type === "enum") {
            input = node("select");
            if (optional) option(input, "", t("Не передавать", "Omit"));
            const choices = definition.type === "boolean"
                ? [{ label: t("Выкл / ложь", "Off / false"), value: false }, { label: t("Вкл / истина", "On / true"), value: true }]
                : definition.options || [];
            for (const choice of choices) option(input, JSON.stringify(choice.value), choice.label);
            if (value !== undefined) input.value = JSON.stringify(value);
            return { input, read: () => input.value === "" ? undefined : JSON.parse(input.value) };
        }
        input = node("input");
        input.type = ["integer", "number"].includes(definition.type) ? "number" : "text";
        input.required = !optional;
        if (input.type === "number") {
            input.step = definition.step ?? (definition.type === "integer" ? 1 : "any");
            if (Number.isFinite(definition.min)) input.min = definition.min;
            if (Number.isFinite(definition.max)) input.max = definition.max;
        } else input.maxLength = 1024;
        input.value = value ?? "";
        return {
            input,
            read: () => {
                if (!input.value && optional) return undefined;
                if (input.type === "number") {
                    const number = Number(input.value);
                    if (!input.value || !Number.isFinite(number) || (definition.type === "integer" && !Number.isSafeInteger(number))) throw new Error(t("Введите корректное число.", "Enter a valid number."));
                    return number;
                }
                return input.value;
            }
        };
    }

    function openDeviceSettings(device) {
        if (deviceSettingsDialog?.root.open) return;
        const view = dialog(t(`Настройки устройства · ${deviceName(device)}`, `Device settings · ${deviceName(device)}`));
        view.root.classList.add("device-settings-dialog");
        view.root.dataset.deviceId = device.device;
        const identity = node("p", "", "device-settings-identity");
        const identityEditor = node("section", null, "device-settings-section device-settings-identity-editor");
        identityEditor.dataset.role = "device-settings-identity";
        const notice = node("p", "", "management-feedback");
        notice.setAttribute("aria-live", "polite");
        const group = node("section", null, "device-settings-section");
        group.dataset.role = "device-settings-group";
        const common = node("section", null, "device-settings-section");
        common.dataset.role = "device-settings-common";
        const options = node("div", null, "device-settings-options");
        options.append(group, common);
        const history = node("section", null, "device-settings-section");
        history.dataset.role = "device-settings-schema";
        const connections = node("section", null, "device-settings-section device-settings-connections");
        const connectionTitle = node("h3"), connectionHelp = node("p", null, "management-help");
        const connectionHistory = button("", () => window.ConnectionDiagnostics?.open(deviceSettingsDialog?.device || device.device));
        for (const [element, ru, en] of [
            [connectionTitle, "Связь и подключения", "Connection diagnostics"],
            [connectionHelp, "Сессии, причины разрывов, Wi-Fi и задержки Gateway.", "Sessions, disconnect reasons, Wi-Fi and Gateway delays."],
            [connectionHistory, "История соединений", "Connection history"]
        ]) { element.dataset.ru = ru; element.dataset.en = en; element.textContent = t(ru, en); }
        connections.append(connectionTitle, connectionHelp, connectionHistory);
        const footer = node("div", null, "management-dialog-actions");
        const close = button(t("Закрыть", "Close"), () => view.root.close());
        footer.append(close);
        view.body.append(identity, notice, identityEditor, options, connections, history, footer);
        deviceSettingsDialog = {
            ...view, device: device.device, identity, identityEditor, notice, group, common, history, close,
            formDirty: false, savePending: false,
        };
        createIdentityEditor(device);
        view.root.addEventListener("close", () => {
            if (deviceSettingsDialog?.root === view.root) deviceSettingsDialog = null;
        });
        refreshDeviceSettings();
    }

    function createIdentityEditor(device) {
        const view = deviceSettingsDialog;
        const title = node("h3", t("Имя и ID устройства", "Device name and ID"));
        const form = node("form", null, "device-identity-form");
        const name = node("input");
        name.type = "text";
        name.maxLength = 60;
        name.dataset.role = "identity-name";
        name.value = device.display_name || "";
        name.placeholder = device.name || device.device;
        const id = node("input");
        id.type = "text";
        id.required = true;
        id.maxLength = 64;
        id.pattern = "[A-Za-z][A-Za-z0-9_-]{0,63}";
        id.autocomplete = "off";
        id.autocapitalize = "off";
        id.spellcheck = false;
        id.dataset.role = "identity-id";
        id.value = device.device;
        const nameField = label(t("Отображаемое имя", "Display name"), name);
        const idField = label(t("ID Gateway (используется в Blockly)", "Gateway ID (used in Blockly)"), id);
        const help = node("p", "", "management-help");
        const feedback = node("p", "", "management-feedback");
        feedback.dataset.role = "identity-feedback";
        feedback.setAttribute("aria-live", "polite");
        const actions = node("div", null, "management-dialog-actions");
        const save = button(t("Сохранить имя и ID", "Save name and ID"));
        save.type = "submit";
        save.dataset.role = "identity-save";
        actions.append(save);
        form.append(nameField, idField, help, feedback, actions);
        view.identityEditor.append(title, form);
        view.identityState = {
            name, id, save, title, nameField, idField, help, feedback,
            nameDirty: false, idDirty: false, pending: null,
            savedName: device.display_name || "", savedId: device.device,
        };
        name.addEventListener("input", () => {
            view.identityState.nameDirty = name.value !== view.identityState.savedName;
            setFeedback(feedback, "", "info");
        });
        id.addEventListener("input", () => {
            id.setCustomValidity("");
            view.identityState.idDirty = id.value !== view.identityState.savedId;
            setFeedback(feedback, "", "info");
        });
        form.addEventListener("submit", event => {
            event.preventDefault();
            const current = getDevices().find(item => item.device === view.device);
            if (!online || !current || view.identityState.pending) return;
            const nextId = current.connected ? current.device : id.value.trim();
            if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(nextId)) {
                id.setCustomValidity(t("ID должен начинаться с английской буквы и содержать буквы, цифры, дефис или подчёркивание.", "The ID must start with an English letter and use letters, digits, hyphens or underscores."));
                id.reportValidity();
                return;
            }
            if (getDevices().some(item => item.device !== current.device && item.device.toLowerCase() === nextId.toLowerCase())) {
                id.setCustomValidity(t("Такой ID уже используется.", "This ID is already in use."));
                id.reportValidity();
                return;
            }
            const payload = { device: current.device, new_device_id: nextId, display_name: name.value.trim() };
            if (context.send("update_device_identity", payload)) {
                view.identityState.pending = payload;
                setFeedback(feedback, t("Сохраняю…", "Saving…"), "info");
                refreshIdentityEditor(current);
            }
        });
    }

    function refreshIdentityEditor(device) {
        const state = deviceSettingsDialog?.identityState;
        if (!state) return;
        state.title.textContent = t("Имя и ID устройства", "Device name and ID");
        state.nameField.firstChild.textContent = t("Отображаемое имя", "Display name");
        state.idField.firstChild.textContent = t("ID Gateway (используется в Blockly)", "Gateway ID (used in Blockly)");
        state.save.textContent = t("Сохранить имя и ID", "Save name and ID");
        if (!state.nameDirty && !state.pending) state.name.value = device.display_name || "";
        if (!state.idDirty && !state.pending) state.id.value = device.device;
        state.savedName = device.display_name || "";
        state.savedId = device.device;
        state.name.placeholder = device.name || device.device;
        state.name.disabled = !online || Boolean(state.pending);
        state.id.disabled = !online || Boolean(device.connected) || Boolean(state.pending);
        state.save.disabled = !online || Boolean(state.pending);
        state.help.textContent = device.connected
            ? t("Чтобы изменить ID, сначала отключите устройство. Имя можно сохранить и сейчас.", "Disconnect the device before changing its ID. You can save its name now.")
            : t("ID уникален: английская буква в начале, затем буквы, цифры, дефис или подчёркивание. Gateway сохранит связь с ID прошивки.", "The unique ID starts with an English letter, followed by letters, digits, hyphens or underscores. Gateway retains its link to the firmware ID.");
    }

    function identityResult(message) {
        const view = deviceSettingsDialog;
        if (!view?.root.open || view.device !== message.device) return;
        const state = view.identityState;
        const pending = state.pending;
        state.pending = null;
        setFeedback(state.feedback, message.message || (message.success ? t("Имя и ID сохранены.", "Name and ID saved.") : t("Не удалось сохранить имя и ID.", "Could not save the name and ID.")), message.success);
        if (message.success) {
            const current = getDevices().find(item => item.device === message.device);
            const nextId = message.new_device_id || pending?.new_device_id || message.device;
            if (current) {
                current.device = nextId;
                if (pending) current.display_name = pending.display_name || null;
            }
            view.device = nextId;
            view.root.dataset.deviceId = nextId;
            state.id.value = nextId;
            if (pending) state.name.value = pending.display_name;
            state.nameDirty = state.idDirty = false;
            view.groupSignature = view.historySignature = null;
            view.commonSignature = null;
        }
        refreshDeviceSettings();
        context.send("get_devices");
    }

    function membershipControl(device) {
        const select = node("select");
        select.dataset.role = "device-group-membership";
        select.setAttribute("aria-label", t(`Группа устройства ${deviceName(device)}`, `Group of ${deviceName(device)}`));
        option(select, "", t("Без группы", "Ungrouped"));
        for (const group of groups) option(select, group.group_id, group.name);
        select.value = device.group_id || "";
        select.disabled = !online || !groupsReady;
        select.addEventListener("change", () => {
            if (context.send("set_device_group", { device: device.device, group_id: select.value || null })) select.disabled = true;
        });
        return label(t("Группа", "Group"), select);
    }

    function refreshDeviceSettings() {
        const view = deviceSettingsDialog;
        if (!view?.root.open) return;
        const device = getDevices().find(item => item.device === view.device);
        if (!device) {
            view.identity.textContent = t("Устройство больше не доступно в списке Gateway.", "This device is no longer available in Gateway.");
            for (const control of view.body.querySelectorAll("input, select, button")) control.disabled = control !== view.close;
            return;
        }
        view.root.querySelector(".management-dialog-title").textContent = t(`Настройки устройства · ${deviceName(device)}`, `Device settings · ${deviceName(device)}`);
        view.root.setAttribute("aria-label", view.root.querySelector(".management-dialog-title").textContent);
        view.identity.textContent = `${device.device} · ${device.connected ? t("Подключено", "Online") : t("Отключено", "Offline")}`;
        view.close.textContent = t("Закрыть", "Close");
        refreshIdentityEditor(device);
        const groupSignature = JSON.stringify([device.group_id, groups, online, groupsReady, t("Группа", "Group")]);
        if (view.groupSignature !== groupSignature) {
            view.group.replaceChildren(membershipControl(device));
            view.groupSignature = groupSignature;
        }
        const commonSignature = JSON.stringify([device.schema, device.common_commands, device.common_command_sources, t("Общее выключение", "Common off")]);
        if (view.commonSignature !== commonSignature && !view.formDirty && !view.savePending) {
            view.common.replaceChildren(node("h3", t("Общее выключение", "Common off")));
            renderCommonCommandEditor(device, view.common);
            view.commonSignature = commonSignature;
        }
        const mode = view.common.querySelector('[data-role="common-command-mode"]')?.value;
        const noChoices = mode === "parameter" ? !writable(device).length : mode === "action" && !Object.keys(device.schema?.actions || {}).length;
        if (view.save) view.save.disabled = !online || Boolean(device.pending_schema) || view.savePending || noChoices;
        const historySignature = JSON.stringify([device.connected, device.schema, online, context.schemaHistoryKey?.(device.device), t("История Schema", "Schema history")]);
        if (view.historySignature !== historySignature && context.createSchemaHistory) {
            const open = view.history.querySelector("details")?.open ?? true;
            const scroll = view.root.scrollTop;
            view.history.replaceChildren(context.createSchemaHistory(device, open));
            view.historySignature = historySignature;
            view.root.scrollTop = scroll;
        }
    }

    function renderCommonCommandEditor(device, container) {
        const form = node("form", null, "management-mapping-form");
        const mode = node("select");
        mode.dataset.role = "common-command-mode";
        option(mode, "schema", t("Использовать назначение прошивки", "Use firmware assignment"));
        option(mode, "none", t("Не участвовать в общем выключении", "Do not participate in common off"));
        option(mode, "parameter", t("Установить параметр", "Set a parameter"));
        option(mode, "action", t("Вызвать действие", "Invoke an action"));
        const operation = offOperation(device);
        const inherited = device.common_command_defaults?.off || device.schema?.common_commands?.off;
        const source = device.common_command_sources?.off;
        mode.value = source === "disabled" ? "none" : source === "schema" || source === "none" ? "schema"
            : operation ? (inherited && JSON.stringify(operation) === JSON.stringify(inherited) ? "schema" : operation.kind) : inherited ? "none" : "schema";
        const fields = node("div", null, "management-operation-fields");
        const feedback = node("p", "", "management-feedback");
        feedback.setAttribute("aria-live", "polite");
        const save = button(t("Сохранить", "Save"));
        save.type = "submit";
        const actions = node("div", null, "management-dialog-actions");
        actions.append(save);
        form.append(label(t("Что означает «выключить» для этого устройства", "What off means for this device"), mode), fields, node("p", t("Назначьте фактическую операцию: например, реле = ложь или скорость = 0. Gateway не выбирает значение по имени параметра.", "Assign the actual operation, for example relay = false or speed = 0. Gateway does not infer values from parameter names."), "management-help"), feedback, actions);
        container.append(form);
        deviceSettingsDialog.feedback = feedback;
        deviceSettingsDialog.save = save;
        let readOperation = () => null;
        function renderFields() {
            fields.replaceChildren();
            if (mode.value === "none") {
                fields.append(node("p", t("Устройство будет пропускаться при общем выключении.", "This device will be skipped by common off."), "management-help"));
                readOperation = () => null;
                save.disabled = !online;
                return;
            }
            if (mode.value === "schema") {
                fields.append(node("p", inherited
                    ? operationDescription(device, inherited)
                    : t("Прошивка не объявила общее выключение. До его появления устройство не участвует.", "The firmware has not declared a common off operation. The device will not participate until one is declared."), "management-help"));
                readOperation = () => null;
                save.disabled = !online;
                return;
            }
            const choices = mode.value === "parameter" ? writable(device) : Object.entries(device.schema?.actions || {});
            if (!choices.length) {
                fields.append(node("p", mode.value === "parameter" ? t("Нет управляемых параметров.", "No writable parameters.") : t("Нет объявленных действий.", "No declared actions."), "management-help"));
                save.disabled = true;
                return;
            }
            save.disabled = !online;
            const select = node("select");
            for (const [id, definition] of choices) option(select, id, `${definition.label || id} (${id})`);
            if (choices.some(([id]) => id === operation?.name) && operation.kind === mode.value) select.value = operation.name;
            const values = node("div", null, "management-operation-values");
            fields.append(label(mode.value === "parameter" ? t("Параметр", "Parameter") : t("Действие", "Action"), select), values);
            function selected() {
                values.replaceChildren();
                const definition = choices.find(([id]) => id === select.value)?.[1];
                if (!definition) return;
                if (mode.value === "parameter") {
                    const editor = valueEditor(definition, operation?.kind === "parameter" && operation.name === select.value ? operation.value : undefined);
                    values.append(label(t("Значение", "Value") + (definition.unit ? ` (${definition.unit})` : ""), editor.input));
                    readOperation = () => ({ kind: "parameter", name: select.value, value: editor.read() });
                } else {
                    const readers = [];
                    for (const [id, argument] of Object.entries(definition.arguments || {})) {
                        const editor = valueEditor(argument, operation?.kind === "action" && operation.name === select.value ? operation.arguments?.[id] : undefined, argument.required === false);
                        values.append(label(`${argument.label || id}${argument.required === false ? t(" (необязательно)", " (optional)") : ""}${argument.unit ? ` (${argument.unit})` : ""}`, editor.input));
                        readers.push([id, editor.read]);
                    }
                    readOperation = () => {
                        const args = {};
                        for (const [id, read] of readers) {
                            const value = read();
                            if (value !== undefined) args[id] = value;
                        }
                        return { kind: "action", name: select.value, arguments: args };
                    };
                }
            }
            select.addEventListener("change", selected);
            selected();
        }
        mode.addEventListener("change", renderFields);
        form.addEventListener("submit", event => {
            event.preventDefault();
            const current = getDevices().find(item => item.device === device.device);
            if (!online || !current || current.pending_schema || deviceSettingsDialog?.savePending) return;
            try {
                const fields = { device: device.device, command: "off", request_id: requestId() };
                if (mode.value === "schema") fields.use_schema = true;
                else fields.operation = readOperation();
                if (context.send("save_common_command", fields)) {
                    deviceSettingsDialog.savePending = true;
                    save.disabled = true;
                    setFeedback(feedback, t("Сохраняю…", "Saving…"), "info");
                }
            } catch (error) {
                setFeedback(feedback, error.message, "error");
            }
        });
        for (const event of ["input", "change"]) form.addEventListener(event, () => {
            if (deviceSettingsDialog) {
                deviceSettingsDialog.formDirty = true;
                refreshDeviceSettings();
            }
        });
        renderFields();
    }

    function appendDeviceExtras(device, panel) {
        const health = node("section", null, "device-health");
        health.dataset.role = "device-health";
        health.setAttribute("aria-live", "off");
        const content = node("div"); content.dataset.role = "device-health-content";
        const history = button(t("История соединений", "Connection history"), () => window.ConnectionDiagnostics?.open(device.device), "device-connection-link");
        history.dataset.role = "connection-history";
        health.append(content, history);
        panel.append(health);
        updateHealth(device, panel);
    }

    function updateHealth(device, panel) {
        const wrapper = panel.querySelector('[data-role="device-health"]');
        const target = wrapper?.querySelector('[data-role="device-health-content"]') || wrapper;
        if (!target) return;
        const health = device.health || {};
        const hasRssi = Number.isInteger(health.wifi_rssi) && health.wifi_rssi <= 0 && health.wifi_rssi >= -127;
        const hasUptime = Number.isFinite(health.uptime_seconds) && health.uptime_seconds >= 0;
        let seconds = hasUptime ? health.uptime_seconds : null;
        const baselineKey = `${health.updated_at || ""}/${health.uptime_seconds}/${Boolean(device.connected && online)}`;
        let baseline = healthBaselines.get(device.device);
        if (!baseline || baseline.key !== baselineKey) {
            baseline = { key: baselineKey, at: performance.now() };
            healthBaselines.set(device.device, baseline);
        }
        if (hasUptime && device.connected && online) seconds += Math.max(0, (performance.now() - baseline.at) / 1000);
        const stats = node("dl", null, "device-health-stats");
        function stat(caption, value, className = "") {
            const row = node("div", null, className);
            row.append(node("dt", caption), node("dd", value));
            stats.append(row);
        }
        const signal = !hasRssi ? "—" : `${health.wifi_rssi} dBm · ` + (health.wifi_rssi >= -60 ? t("отлично", "excellent") : health.wifi_rssi >= -70 ? t("хорошо", "good") : health.wifi_rssi >= -80 ? t("слабо", "weak") : t("очень слабо", "very weak"));
        stat(device.connected ? "Wi-Fi" : t("Последний Wi-Fi", "Last Wi-Fi"), signal, hasRssi && health.wifi_rssi < -75 ? "is-weak" : "");
        stat(t("Время работы ESP32", "ESP32 uptime"), seconds == null ? "—" : context.formatDuration(seconds));
        if (health.reset_reason) stat(t("Запуск", "Boot"), health.reset_reason);
        const reason = health.last_disconnect_reason || health.device_disconnect_reason;
        const last = node("p", null, "device-health-disconnect");
        last.textContent = t("Последнее отключение: ", "Last disconnect: ") + (reason || t("ещё не зарегистрировано", "not recorded yet"));
        if (health.last_disconnect_at) last.textContent += ` · ${context.formatTime(health.last_disconnect_at)}`;
        if (health.device_disconnect_reason && health.last_disconnect_reason && health.device_disconnect_reason !== health.last_disconnect_reason) last.title = health.device_disconnect_reason;
        target.replaceChildren(stats, last);
        if (!hasRssi && !hasUptime) {
            const note = node("small", t("Для Wi-Fi и времени работы нужна прошивка с передачей диагностики.", "Wi-Fi and uptime require firmware that reports health data."), "device-health-note");
            target.append(note);
        }
    }

    function renderRetention() {
        const input = byId("event-retention-days");
        const save = byId("event-retention-save");
        if (!input || !save) return;
        input.disabled = !online || retention == null || retentionPending;
        save.disabled = input.disabled || !retentionDirty;
        if (retention != null && !retentionDirty) input.value = retention.event_retention_days;
    }

    function saveRetention(event) {
        event.preventDefault();
        const value = Number(byId("event-retention-days").value);
        if (!Number.isInteger(value) || value < 0 || value > 3650 || retentionPending) return;
        if (value > 0 && !window.confirm(t(`Сохранять события за последние ${value} дней? Более старые события будут удалены без возможности отмены. Резервные копии остаются отдельно.`, `Keep events for the last ${value} days? Older events will be permanently deleted. Backups are kept separately.`))) return;
        if (!context.send("set_gateway_settings", { event_retention_days: value, request_id: requestId() })) return;
        retentionPending = true;
        setFeedback(byId("event-retention-feedback"), t("Сохраняю настройку…", "Saving setting…"), "info");
        clearTimeout(retentionTimer);
        retentionTimer = setTimeout(() => {
            if (!retentionPending) return;
            retentionPending = false;
            setFeedback(byId("event-retention-feedback"), t("Ответ не получен. Переподключитесь и проверьте сохранённое значение.", "No response received. Reconnect and check the saved value."), "error");
            renderRetention();
        }, 20000);
        renderRetention();
    }

    function setConnected(connected) {
        if (online && !connected) {
            const notice = t("Соединение потеряно. После подключения проверьте сохранённые изменения.", "Connection lost. Check the saved changes after reconnecting.");
            if (groupDialog?.root.open) setFeedback(groupDialog.feedback, notice, "warning");
            if (deviceSettingsDialog?.root.open) {
                setFeedback(deviceSettingsDialog.notice, notice, "warning");
                deviceSettingsDialog.savePending = false;
                if (deviceSettingsDialog.identityState) {
                    deviceSettingsDialog.identityState.pending = null;
                    setFeedback(deviceSettingsDialog.identityState.feedback, notice, "warning");
                }
            }
            if (activeBulk) {
                clearTimeout(bulkTimer);
                activeBulk = null;
                lastBulk = { ...lastBulk, pending: false, uncertain: true };
            }
            if (retentionPending) {
                clearTimeout(retentionTimer);
                retentionPending = false;
                setFeedback(byId("event-retention-feedback"), t("Связь потеряна. После подключения проверьте сохранённое значение.", "Connection lost. Check the saved value after reconnecting."), "warning");
            }
        }
        online = connected;
        refresh();
        if (groupDialog?.root.open) groupDialog.render();
    }

    function handleMessage(message) {
        if (!context) return false;
        switch (message.type) {
            case "device_identity_result":
                identityResult(message);
                return false;
            case "device_groups":
                groups = (Array.isArray(message.groups) ? message.groups : []).filter(group => typeof group.group_id === "string" && typeof group.name === "string");
                groupsReady = true;
                if (groupDialog?.root.open) groupDialog.render();
                refresh();
                context.renderDevices();
                return true;
            case "group_update_result":
                if (deviceSettingsDialog?.root.open && deviceSettingsDialog.device === message.device) {
                    setFeedback(deviceSettingsDialog.notice, message.message || t("Группа обновлена.", "Group updated."), message.success);
                    deviceSettingsDialog.groupSignature = null;
                }
                if (groupDialog?.root.open) {
                    setFeedback(groupDialog.feedback, message.message || (message.success ? t("Сохранено.", "Saved.") : t("Не удалось сохранить.", "Could not save.")), message.success);
                    if (message.success) groupDialog.clearDrafts();
                    groupDialog.render();
                }
                else context.feedback(message.message || (message.success ? t("Группа обновлена.", "Group updated.") : t("Не удалось изменить группу.", "Could not update group.")), message.success);
                requestGroups();
                context.send("get_devices");
                refresh();
                return true;
            case "common_command_save_result":
                if (deviceSettingsDialog?.root.open && (!message.device || deviceSettingsDialog.device === message.device)) {
                    deviceSettingsDialog.savePending = false;
                    deviceSettingsDialog.formDirty = message.success ? false : deviceSettingsDialog.formDirty;
                    setFeedback(deviceSettingsDialog.feedback, message.message || (message.success ? t("Назначение сохранено.", "Assignment saved.") : t("Не удалось сохранить назначение.", "Could not save assignment.")), message.success);
                    setFeedback(deviceSettingsDialog.notice, deviceSettingsDialog.feedback.textContent, message.success);
                    refreshDeviceSettings();
                }
                context.feedback(message.message || (message.success ? t("Назначение сохранено.", "Assignment saved.") : t("Ошибка назначения.", "Assignment failed.")), message.success);
                context.send("get_devices");
                return true;
            case "common_command_result":
                if (activeBulk && message.request_id && activeBulk !== message.request_id) return true;
                clearTimeout(bulkTimer);
                activeBulk = null;
                lastBulk = { ...message, pending: false };
                refresh();
                context.send("get_devices");
                return true;
            case "gateway_settings":
                if (Number.isInteger(message.event_retention_days)) retention = message;
                else if (Number.isInteger(message.settings?.event_retention_days)) retention = message.settings;
                renderRetention();
                return true;
            case "gateway_settings_result":
                clearTimeout(retentionTimer);
                retentionPending = false;
                if (message.success) {
                    retentionDirty = false;
                    const value = message.event_retention_days ?? message.settings?.event_retention_days ?? Number(byId("event-retention-days").value);
                    retention = { ...retention, event_retention_days: value };
                    context.send("get_gateway_settings");
                    context.eventsChanged?.();
                }
                setFeedback(byId("event-retention-feedback"), message.message || (message.success ? t("Срок хранения сохранён.", "Retention saved.") : t("Не удалось сохранить настройку.", "Could not save setting.")), message.success);
                renderRetention();
                return true;
            case "device_health": {
                const device = getDevices().find(item => item.device === message.device);
                if (device && message.health) {
                    device.health = message.health;
                    const panel = [...document.querySelectorAll(".device-panel")].find(item => item.dataset.deviceId === device.device);
                    if (panel) updateHealth(device, panel);
                }
                return true;
            }
            default: return false;
        }
    }

    window.DeviceManagement = {
        initialize(options) {
            context = options;
            byId("device-group-filter")?.addEventListener("change", event => {
                groupFilter = event.target.value;
                context.renderDevices();
            });
            byId("device-groups-manage")?.addEventListener("click", openGroups);
            byId("common-off-all")?.addEventListener("click", () => executeOff(null));
            byId("common-off-group")?.addEventListener("click", () => executeOff(groupFilter));
            byId("event-retention-form")?.addEventListener("submit", saveRetention);
            byId("event-retention-days")?.addEventListener("input", () => { retentionDirty = true; renderRetention(); });
            refresh();
        },
        connected() { requestGroups(); context?.send("get_gateway_settings"); },
        setConnected, refresh, handleMessage, appendDeviceExtras, updateHealth,
        openDeviceSettings, refreshDeviceSettings,
        executeOff,
        getGroupName(id) { return groups.find(group => group.group_id === id)?.name || null; },
        getGroups() { return groups.map(group => ({ ...group })); },
        canExecuteOff(id) {
            return online && !activeBulk && groups.some(group => group.group_id === id) &&
                scopeDevices(id).some(offOperation);
        },
        settingsResult(message) {
            if (deviceSettingsDialog?.root.open && deviceSettingsDialog.device === message.device) {
                setFeedback(deviceSettingsDialog.notice, message.message || "", message.success);
                refreshDeviceSettings();
            }
        },
        matchesGroup(device) { return groupFilter === "all" || (groupFilter === "ungrouped" ? !device.group_id : device.group_id === groupFilter); },
        hasGroupFilter() { return groupFilter !== "all"; },
        resetGroupFilter() { groupFilter = "all"; refresh(); }
    };
})();
