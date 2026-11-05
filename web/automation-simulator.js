/* Explicit virtual execution: this module only sends simulate_automation. */
(() => {
    "use strict";
    let options = null;
    let current = null;
    let sequence = 0;
    let supported = null;
    const t = (ru, en) => options?.localized(ru, en) || ru;
    const node = (tag, value, className) => {
        const result = document.createElement(tag);
        if (value != null) result.textContent = value;
        if (className) result.className = className;
        return result;
    };
    const caption = (tag, ru, en, className) => {
        const result = node(tag, t(ru, en), className);
        result.dataset.ru = ru;
        result.dataset.en = en;
        return result;
    };
    const button = (ru, en, action, className) => {
        const result = caption("button", ru, en, className);
        result.type = "button";
        if (action) result.addEventListener("click", action);
        return result;
    };
    const deviceName = device => device?.display_name || device?.name || device?.device || "—";
    const parameterKey = (device, parameter) => JSON.stringify([device, parameter]);

    function sourceConfig(id) {
        if (id == null) return options.getDraft();
        const item = options.getAutomations().find(item => item.automation_id === id);
        if (!item?.config) throw new Error(t("Сценарий больше не доступен.", "This scenario is no longer available."));
        return item.config;
    }

    function signature(state) {
        return JSON.stringify(sourceConfig(state.automationId));
    }

    function schemaSignature(config) {
        return JSON.stringify(referenced(config || {}, options.getDevices()).devices
            .map(device => [device.device, device.schema]).sort((left, right) => left[0].localeCompare(right[0])));
    }

    function finishRequest(state) {
        clearTimeout(state.timer);
        state.timer = null;
        state.requestId = null;
    }

    function feedback(state, ru, en, kind = "info") {
        state.notice = { ru, en, kind };
        const value = t(ru, en);
        if (state.feedback.textContent !== value) state.feedback.textContent = value;
        state.feedback.dataset.state = kind;
    }

    function controls(state) {
        const busy = Boolean(state.requestId);
        state.submit.disabled = busy || !options.isConnected() || Boolean(state.sourceError) || state.staleSource || supported === false;
        const title = busy ? t("Симулирую…", "Simulating…") : t("Симулировать", "Simulate");
        if (state.submit.textContent !== title) state.submit.textContent = title;
        for (const row of state.parameters.values()) {
            row.override.disabled = busy;
            row.input.disabled = busy || !row.override.checked;
        }
        for (const row of state.connections.values()) row.input.disabled = busy;
    }

    function referenced(config, devices) {
        const parameters = new Map();
        const deviceIds = new Set();
        const stack = [config.program || config];
        let visited = 0;
        while (stack.length && visited++ < 3000) {
            const item = stack.pop();
            if (!item || typeof item !== "object") continue;
            if (typeof item.device === "string") {
                deviceIds.add(item.device);
                const parameter = typeof item.parameter === "string" ? item.parameter
                    : item.kind === "parameter" && typeof item.name === "string" ? item.name : null;
                if (parameter) parameters.set(parameterKey(item.device, parameter), { device: item.device, parameter });
            }
            for (const value of Object.values(item)) if (value && typeof value === "object") {
                if (Array.isArray(value)) stack.push(...value);
                else stack.push(value);
            }
        }
        const lookup = new Map(devices.map(device => [device.device, device]));
        return {
            parameters: [...parameters.values()].map(item => ({
                ...item, deviceInfo: lookup.get(item.device),
                definition: lookup.get(item.device)?.schema?.parameters?.[item.parameter]
            })),
            devices: [...deviceIds].map(id => lookup.get(id) || { device: id })
        };
    }

    function makeValueInput(definition, observed) {
        const type = definition.type;
        const input = node(["boolean", "enum"].includes(type) ? "select" : "input");
        if (["boolean", "enum"].includes(type)) {
            const values = type === "boolean"
                ? [{ value: false, label: t("false — ложь", "false") }, { value: true, label: t("true — истина", "true") }]
                : definition.options || [];
            for (const value of values) {
                const entry = node("option", value.label || String(value.value));
                entry.value = JSON.stringify(value.value);
                input.append(entry);
            }
            input.value = JSON.stringify(observed);
            if (input.selectedIndex < 0) input.selectedIndex = 0;
        } else {
            input.type = ["integer", "number"].includes(type) ? "number" : "text";
            if (input.type === "number") {
                for (const bound of ["min", "max", "step"]) if (definition[bound] != null) input[bound] = definition[bound];
                input.step = definition.step ?? (type === "integer" ? 1 : "any");
                input.value = typeof observed === "number" && Number.isFinite(observed) ? observed : definition.min ?? 0;
            } else {
                input.value = typeof observed === "string" ? observed : "";
                input.maxLength = 1024;
            }
        }
        input.required = definition.type !== "string";
        input.disabled = true;
        return input;
    }

    function buildInputs(state) {
        const refs = referenced(state.config, options.getDevices());
        const parameterSection = node("details", null, "simulation-input-section");
        parameterSection.open = true;
        parameterSection.append(caption("summary", "Исходные значения параметров", "Initial parameter values"));
        parameterSection.append(caption("p", "Отметьте «Подставить», чтобы задать виртуальное значение. Остальные значения Gateway возьмёт только из свежих показаний; недостающие потребуется указать вручную.", "Select Override to enter a virtual value. The Gateway only uses fresh readings for other inputs; missing values must be supplied manually.", "simulation-help"));
        const rows = node("div", null, "simulation-inputs");
        for (const item of refs.parameters.slice(0, 200)) {
            const key = parameterKey(item.device, item.parameter);
            const row = node("section", null, "simulation-input-row");
            const definition = item.definition;
            const title = node("strong", `${deviceName(item.deviceInfo || { device: item.device })} · ${definition?.label || item.parameter}`);
            const identifier = node("small", `${item.device} · ${item.parameter}`, "simulation-input-id");
            row.append(title, identifier);
            if (!definition || !["boolean", "integer", "number", "enum", "string"].includes(definition.type)) {
                row.append(caption("p", "Параметр отсутствует в текущей Schema или не поддерживается тестовым режимом.", "This parameter is absent from the current Schema or unsupported in test mode.", "simulation-input-error"));
                rows.append(row); continue;
            }
            const observed = item.deviceInfo?.parameters?.[item.parameter];
            const last = node("p", "", "simulation-input-observed");
            const observedCaption = caption("span", "Последнее показание: ", "Last reading: ");
            const observedValue = node("span", options.formatValue?.(observed, definition) ?? String(observed ?? "—"));
            last.append(observedCaption, observedValue);
            const override = node("input"); override.type = "checkbox";
            const toggle = node("label", null, "simulation-override-toggle");
            toggle.append(override, caption("span", "Подставить", "Override"));
            const input = makeValueInput(definition, observed);
            input.setAttribute("aria-label", `${t("Виртуальное значение", "Virtual value")} · ${definition.label || item.parameter} · ${item.device}`);
            const control = node("div", null, "simulation-input-control");
            control.append(toggle, input);
            if (definition.unit) control.append(node("span", definition.unit, "simulation-unit"));
            override.addEventListener("change", () => {
                input.disabled = !override.checked || Boolean(state.requestId);
                markInputsChanged(state);
                if (override.checked) input.focus({ preventScroll: true });
            });
            input.addEventListener("input", () => markInputsChanged(state));
            input.addEventListener("change", () => markInputsChanged(state));
            state.parameters.set(key, { ...item, override, input, observed, observedValue });
            row.append(last, control); rows.append(row);
        }
        if (!refs.parameters.length) rows.append(caption("p", "Сценарий не использует значения параметров для чтения или установки.", "This scenario has no parameter inputs or assignments.", "simulation-help"));
        parameterSection.append(rows);
        const connections = node("details", null, "simulation-input-section");
        connections.append(caption("summary", "Виртуальные подключения устройств", "Virtual device connections"));
        connections.append(caption("p", "Соединения моделируются независимо от реальных устройств: по умолчанию они подключены, кроме источника события отключения. Здесь можно изменить эти условия.", "Connections are modeled independently of real devices: they default to connected, except the source of a disconnection event. Override these conditions here.", "simulation-help"));
        const connectionRows = node("div", null, "simulation-connection-inputs");
        for (const device of refs.devices.slice(0, 100)) {
            const select = node("select");
            const disconnectedSource = state.config?.program?.trigger?.type === "device_disconnected" && state.config.program.trigger.device === device.device;
            const defaultCaption = disconnectedSource
                ? ["По умолчанию: отключено в симуляции", "Default: disconnected in simulation"]
                : ["По умолчанию: подключено в симуляции", "Default: connected in simulation"];
            for (const [value, ru, en] of [["default", ...defaultCaption], ["true", "Подключено в симуляции", "Connected in simulation"], ["false", "Отключено в симуляции", "Disconnected in simulation"]]) {
                const option = caption("option", ru, en); option.value = value; select.append(option);
            }
            const label = node("label", null, "management-field");
            label.append(node("span", `${deviceName(device)} · ${device.device}`), select);
            connectionRows.append(label); state.connections.set(device.device, { input: select });
            select.addEventListener("change", () => markInputsChanged(state));
        }
        if (!refs.devices.length) connectionRows.append(caption("p", "Сценарий не обращается к устройствам.", "This scenario does not reference devices.", "simulation-help"));
        connections.append(connectionRows); state.form.append(parameterSection, connections);
    }

    function markInputsChanged(state) {
        if (!state.result) return;
        state.inputsStale = true;
        state.results.classList.add("is-stale");
        feedback(state, "Исходные значения изменены — повторите симуляцию.", "Inputs changed — run the simulation again.", "warning");
    }

    function request(automationId = null) {
        if (!options) return;
        if (current?.dialog.open) { current.dialog.close(); }
        const dialog = node("dialog", null, "management-dialog automation-simulator-dialog");
        const state = { dialog, automationId, parameters: new Map(), connections: new Map(), requestId: null, timer: null, result: null, sourceError: null, inputsStale: false, staleSource: false };
        try { state.config = JSON.parse(JSON.stringify(sourceConfig(automationId))); }
        catch (error) { state.sourceError = error.message; }
        state.configSignature = state.config ? JSON.stringify(state.config) : null;
        state.schemaSignature = schemaSignature(state.config);
        const heading = caption("h2", "Тестовый запуск сценария", "Test scenario execution", "management-dialog-title");
        if (options.labelDialog) options.labelDialog(dialog, heading);
        else { heading.id = `simulation-title-${++sequence}`; dialog.setAttribute("aria-labelledby", heading.id); }
        const body = node("div", null, "management-dialog-body");
        const notice = caption("p", "Симуляция: реальные устройства не управляются. Паузы считаются виртуально, результат не записывается в историю настоящих запусков.", "Simulation: real devices are not controlled. Waits use virtual time, and results are not added to real run history.", "simulation-mode-notice");
        const source = node("p", "", "simulation-source");
        source.append(caption("strong", automationId == null ? "Черновик Blockly: " : "Сохранённый сценарий: ", automationId == null ? "Blockly draft: " : "Saved scenario: "), node("span", state.config?.name || "—"));
        state.form = node("form", null, "simulation-form");
        state.form.id = `automation-simulation-form-${++sequence}`;
        if (state.config) buildInputs(state);
        state.feedback = node("p", "", "management-feedback simulation-feedback"); state.feedback.setAttribute("role", "status");
        const actions = node("div", null, "management-dialog-actions simulation-actions");
        const close = button("Закрыть", "Close", () => dialog.close());
        state.submit = caption("button", "Симулировать", "Simulate", "simulation-submit"); state.submit.type = "submit";
        state.submit.setAttribute("form", state.form.id);
        actions.append(close, state.submit); state.form.append(state.feedback);
        state.results = node("section", null, "simulation-results"); state.results.hidden = true;
        body.append(notice, source, state.form, state.results, actions); dialog.append(heading, body); document.body.append(dialog);
        state.form.addEventListener("submit", event => { event.preventDefault(); simulate(state); });
        dialog.addEventListener("close", () => {
            finishRequest(state);
            if (current === state) current = null;
            dialog.remove();
            if (document.activeElement === document.body) {
                const card = [...document.querySelectorAll(".automation-card")].find(item => item.dataset.automationId === state.automationId);
                const tile = [...document.querySelectorAll(".workspace-tile")]
                    .find(item => item.dataset.tileKey === JSON.stringify(["scenario", "", state.automationId]));
                const candidates = state.automationId == null ? [document.getElementById("automation-simulate-draft")]
                    : [tile?.querySelector('[data-workspace-control="test"]'), card?.querySelector('[data-role="automation-simulate"]')];
                const opener = candidates.find(element => element && !element.disabled && !element.closest("[hidden]") && element.getClientRects().length);
                opener?.focus({ preventScroll: true });
            }
        }, { once: true });
        current = state;
        if (state.sourceError) feedback(state, state.sourceError, state.sourceError, "error");
        else if (supported === false) unsupported(state);
        else if (!options.isConnected()) feedback(state, "Нет связи с Gateway. Подключитесь, чтобы выполнить симуляцию.", "Gateway disconnected. Reconnect to run a simulation.", "warning");
        else feedback(state, "Укажите нужные подстановки и нажмите «Симулировать».", "Choose any overrides, then select Simulate.");
        controls(state); dialog.showModal();
    }

    function simulate(state) {
        if (state !== current || state.requestId || !options.isConnected() || state.sourceError) return;
        if (supported === false) { unsupported(state); controls(state); return; }
        const parameters = [];
        try {
            const config = sourceConfig(state.automationId);
            if (JSON.stringify(config) !== state.configSignature || schemaSignature(config) !== state.schemaSignature) {
                feedback(state, "Сценарий или Schema изменились. Закройте окно и откройте тест заново.", "The scenario or Schema changed. Close this dialog and open a new test.", "warning");
                return;
            }
            for (const row of state.parameters.values()) if (row.override.checked) {
                const value = row.input.tagName === "SELECT" ? JSON.parse(row.input.value)
                    : row.input.type === "number" ? Number(row.input.value) : row.input.value;
                if (row.input.type === "number" && (!row.input.value.trim() || !Number.isFinite(value))) throw new Error(t("Введите конечное числовое значение.", "Enter a finite numeric value."));
                parameters.push({ device: row.device, parameter: row.parameter, value });
            }
            const connections = [...state.connections].filter(([, row]) => row.input.value !== "default")
                .map(([device, row]) => ({ device, connected: row.input.value === "true" }));
            state.requestId = options.createRequestId?.() || `simulation-${Date.now()}-${++sequence}`;
            // Test the exact version displayed here, including for saved scenarios.
            const fields = { request_id: state.requestId, parameters, connections,
                config: JSON.parse(JSON.stringify(state.config)) };
            state.result = null; state.inputsStale = false; state.staleSource = false; state.results.hidden = true;
            feedback(state, "Выполняю виртуальный сценарий…", "Executing the virtual scenario…");
            if (!options.send("simulate_automation", fields)) throw new Error(t("Gateway не принял запрос симуляции.", "The Gateway did not accept the simulation request."));
            state.timer = setTimeout(() => {
                if (current !== state || !state.requestId) return;
                finishRequest(state);
                feedback(state, "Ответ Gateway не получен. Повторите симуляцию после проверки связи.", "No Gateway response. Check the connection and try again.", "error");
                controls(state);
            }, 12000);
        } catch (error) {
            finishRequest(state); feedback(state, error.message, error.message, "error");
        }
        controls(state);
    }

    function valueText(state, device, parameter, value) {
        const definition = state.parameters.get(parameterKey(device, parameter))?.definition
            || options.getDevices().find(item => item.device === device)?.schema?.parameters?.[parameter];
        if (definition && options.formatValue) return options.formatValue(value, definition);
        if (typeof value === "object") { try { return JSON.stringify(value); } catch { return "—"; } }
        return value == null ? "—" : String(value);
    }

    function stepType(type) {
        const labels = {
            trigger: ["Запуск", "Trigger"], condition: ["Условие", "Condition"], otherwise: ["Иначе", "Else"],
            if: ["Условие", "Condition"], wait: ["Виртуальная пауза", "Virtual wait"],
            set_parameter: ["Установить параметр", "Set parameter"], invoke_action: ["Вызвать действие", "Invoke action"],
            action: ["Вызвать действие", "Invoke action"], parameter: ["Установить параметр", "Set parameter"]
        };
        return labels[type] ? t(...labels[type]) : type || t("Шаг", "Step");
    }

    function stepDescription(state, step) {
        const detail = step.details || {};
        const parts = [];
        if (step.type === "trigger") {
            const trigger = typeof detail.trigger === "string" ? detail.trigger : detail.trigger?.type;
            const triggers = { manual: ["Ручной запуск", "Manual trigger"], parameter_changed: ["Изменение параметра", "Parameter changed"], device_connected: ["Подключение устройства", "Device connected"], device_disconnected: ["Отключение устройства", "Device disconnected"], gateway_started: ["Запуск Gateway", "Gateway started"] };
            if (triggers[trigger]) parts.push(t(...triggers[trigger]));
            if (detail.assumed_event) parts.push(t("Виртуальное событие", "Virtual event"));
            if (typeof detail.matched === "boolean") parts.push(detail.matched ? t("Условие запуска выполнено", "Trigger condition matched") : t("Условие запуска не выполнено", "Trigger condition did not match"));
        }
        if (detail.device) {
            const device = options.getDevices().find(item => item.device === detail.device);
            parts.push(deviceName(device || { device: detail.device }));
            const parameter = detail.parameter || (step.type === "set_parameter" ? detail.name : null);
            const action = detail.action || detail.action_id || (step.type === "invoke_action" ? detail.name : null);
            if (parameter) parts.push(`${device?.schema?.parameters?.[parameter]?.label || parameter}${Object.hasOwn(detail, "value") ? ` = ${valueText(state, detail.device, parameter, detail.value)}` : ""}`);
            if (action) parts.push(device?.schema?.actions?.[action]?.label || action);
        }
        if (typeof detail.seconds === "number") parts.push(t(`${detail.seconds} с`, `${detail.seconds} s`));
        if (detail.branch != null) parts.push(t(`Ветвь ${detail.branch}`, `Branch ${detail.branch}`));
        for (const name of ["result", "condition_result", "accepted"]) if (typeof detail[name] === "boolean") parts.push(detail[name] ? t("Истина", "True") : t("Ложь", "False"));
        if (detail.arguments && Object.keys(detail.arguments).length) parts.push(JSON.stringify(detail.arguments));
        if (detail.effects === "unknown") parts.push(t("Побочные изменения параметров неизвестны", "Side effects on other parameters are unknown"));
        if (step.error) parts.push(step.error);
        if (!parts.length && Object.keys(detail).length) parts.push(JSON.stringify(detail));
        return parts.join(" · ") || "—";
    }

    function readingsTable(state, values, withSource) {
        const table = node("table", null, "simulation-readings-table");
        const head = node("thead"), header = node("tr");
        for (const label of [t("Устройство и параметр", "Device and parameter"), t("Значение", "Value"), ...(withSource ? [t("Источник", "Source")] : [])]) {
            const cell = node("th", label); cell.scope = "col"; header.append(cell);
        }
        head.append(header); const body = node("tbody");
        for (const item of values.slice(0, 200)) {
            if (!item || typeof item.device !== "string" || typeof item.parameter !== "string") continue;
            const row = node("tr");
            row.append(node("td", `${item.device} · ${item.parameter}`), node("td", valueText(state, item.device, item.parameter, item.value)));
            if (withSource) row.append(node("td", item.source === "override" ? t("Подстановка", "Override") : t("Свежее показание", "Fresh reading")));
            body.append(row);
        }
        table.append(head, body); return table;
    }

    function renderResults(state) {
        const result = state.result;
        if (!result) return;
        if (state.renderedResult === result && state.renderedLanguage === options.language()) {
            state.results.classList.toggle("is-stale", state.inputsStale || state.staleSource);
            return;
        }
        state.renderedResult = result;
        state.renderedLanguage = options.language();
        state.results.hidden = false; state.results.replaceChildren();
        state.results.classList.toggle("is-stale", state.inputsStale || state.staleSource);
        state.results.dataset.state = result.status === "completed" && result.success ? "success" : result.status === "not_triggered" ? "warning" : "error";
        const heading = node("h3", result.status === "completed" && result.success ? t("Тест завершён", "Test completed")
            : result.status === "not_triggered" ? t("Условие запуска не выполнено", "Trigger condition was not met") : t("Тест остановлен с ошибкой", "Test stopped with an error"));
        const elapsed = typeof result.elapsed_seconds === "number" && Number.isFinite(result.elapsed_seconds) ? Math.max(0, result.elapsed_seconds) : 0;
        const meta = node("p", t(`Виртуальное время: ${elapsed} с. Команды реальным устройствам не отправлялись.`, `Virtual time: ${elapsed} s. No commands were sent to real devices.`), "simulation-result-meta");
        state.results.append(heading, meta);
        if (result.message) state.results.append(node("p", result.message, "simulation-result-message"));
        if (Array.isArray(result.warnings) && result.warnings.length) {
            const warnings = node("ul", null, "simulation-warnings");
            for (const warning of result.warnings.slice(0, 50)) warnings.append(node("li", typeof warning === "string" ? warning : JSON.stringify(warning)));
            state.results.append(warnings);
        }
        const steps = Array.isArray(result.steps) ? result.steps.slice(0, 1000) : [];
        if (steps.length) {
            const scroller = node("div", null, "simulation-trace-scroll");
            scroller.tabIndex = 0; scroller.setAttribute("role", "region"); scroller.setAttribute("aria-label", t("Шаги симуляции", "Simulation steps"));
            const table = node("table", null, "simulation-trace");
            table.append(node("caption", t("Путь выполнения виртуального сценария", "Virtual scenario execution trace")));
            const head = node("thead"), row = node("tr");
            for (const label of ["#", t("Время", "Time"), t("Блок", "Block"), t("Результат", "Result")]) {
                const cell = node("th", label); cell.scope = "col"; row.append(cell);
            }
            head.append(row); const body = node("tbody");
            steps.forEach((step, index) => {
                if (!step || typeof step !== "object") return;
                const row = node("tr"); row.dataset.state = step.status || "";
                if (typeof step.block_id === "string") row.dataset.blockId = step.block_id;
                const when = typeof step.at_seconds === "number" && Number.isFinite(step.at_seconds) ? `${step.at_seconds} ${t("с", "s")}` : "—";
                const type = node("td", stepType(step.type));
                if (step.block_id) type.title = `Blockly: ${step.block_id}`;
                row.append(node("td", step.sequence ?? index + 1), node("td", when), type, node("td", stepDescription(state, step)));
                body.append(row);
            });
            table.append(head, body); scroller.append(table); state.results.append(scroller);
        } else state.results.append(node("p", t("Шаги выполнения отсутствуют.", "No execution steps."), "simulation-help"));
        for (const [key, ru, en, withSource] of [["inputs", "Использованные значения", "Values used", true], ["final_parameters", "Виртуальное состояние после теста", "Virtual state after the test", false]]) {
            if (!Array.isArray(result[key]) || !result[key].length) continue;
            const details = node("details", null, "simulation-output-details");
            details.append(caption("summary", ru, en));
            const wrap = node("div", null, "simulation-table-scroll");
            wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", t(ru, en));
            wrap.append(readingsTable(state, result[key], withSource)); details.append(wrap); state.results.append(details);
        }
        if (Array.isArray(result.connections) && result.connections.length) {
            const details = node("details", null, "simulation-output-details");
            details.append(caption("summary", "Подключения в симуляции", "Connections in the simulation"));
            const list = node("ul", null, "simulation-connection-results");
            for (const connection of result.connections.slice(0, 100)) if (connection && typeof connection.device === "string") {
                list.append(node("li", `${connection.device} · ${connection.connected ? t("Подключено виртуально", "Virtually connected") : t("Отключено виртуально", "Virtually disconnected")}`));
            }
            details.append(list); state.results.append(details);
        }
    }

    function refresh() {
        if (!options || !current?.dialog.open) return;
        const state = current;
        let changed = false;
        try { changed = signature(state) !== state.configSignature || schemaSignature(state.config) !== state.schemaSignature; }
        catch { changed = true; }
        state.staleSource = changed;
        if (changed) {
            finishRequest(state); state.results.classList.add("is-stale");
            feedback(state, "Сценарий или Schema изменились. Откройте тест заново для актуальной версии.", "The scenario or Schema changed. Reopen the test for the current version.", "warning");
        } else if (state.notice) feedback(state, state.notice.ru, state.notice.en, state.notice.kind);
        if (state.result) renderResults(state);
        if (changed) state.results.classList.add("is-stale");
        for (const row of state.parameters.values()) {
            row.observedValue.textContent = options.formatValue?.(row.observed, row.definition) ?? String(row.observed ?? "—");
            if (row.definition.type === "boolean") {
                row.input.options[0].textContent = t("false — ложь", "false");
                row.input.options[1].textContent = t("true — истина", "true");
            }
        }
        controls(state);
    }

    function unsupported(state) {
        supported = false;
        finishRequest(state);
        state.unsupported = true;
        feedback(state, "Запущенная версия Gateway не поддерживает симуляцию. Перезапустите Gateway и обновите страницу.", "The running Gateway does not support simulation. Restart the Gateway and reload this page.", "error");
    }

    window.AutomationSimulator = {
        initialize(callbacks) {
            options = callbacks;
            document.addEventListener("automation-history-change", refresh);
            document.getElementById("automation-name")?.addEventListener("input", refresh);
        },
        request,
        refresh,
        disconnected() {
            supported = null;
            if (!current?.dialog.open) return;
            const state = current;
            finishRequest(state);
            feedback(state, "Соединение с Gateway прервано. Подстановки сохранены в этом окне; повторите тест после подключения.", "Gateway disconnected. Overrides are kept in this dialog; rerun after reconnecting.", "warning");
            controls(state);
        },
        handleMessage(message) {
            if (message?.type === "gateway_capabilities" && Array.isArray(message.features)) {
                supported = message.features.includes("automation_simulation");
                if (current?.dialog.open) {
                    if (!supported) unsupported(current);
                    else if (current.unsupported) {
                        current.unsupported = false;
                        feedback(current, "Gateway готов к симуляции. Повторите тест с выбранными значениями.", "The Gateway is ready for simulation. Rerun with your chosen values.");
                    }
                    controls(current);
                }
                return false;
            }
            const unknownAction = message?.type === "error" && (
                message.action === "simulate_automation" && ["unknown_action", "unsupported_action"].includes(message.code || message.error_code)
                || typeof message.message === "string" && /^(?:Неизвестное действие|Unknown action):\s*simulate_automation\s*$/i.test(message.message)
            );
            if (unknownAction) {
                supported = false;
                if (current?.dialog.open) { unsupported(current); controls(current); }
                return true;
            }
            if (message?.type !== "automation_simulation" && message?.type !== "error") return false;
            const state = current;
            if (!state?.dialog.open || !state.requestId || message.request_id !== state.requestId) return message?.type === "automation_simulation";
            finishRequest(state);
            let changed = false;
            try { changed = signature(state) !== state.configSignature || schemaSignature(state.config) !== state.schemaSignature; } catch { changed = true; }
            if (changed) {
                feedback(state, "Получен результат для предыдущей версии. Откройте тест заново.", "This result belongs to an earlier version. Reopen the test.", "warning");
                controls(state); return true;
            }
            if (message.type === "error") {
                feedback(state, message.message || t("Не удалось выполнить тест.", "Could not run the test."), message.message || t("Не удалось выполнить тест.", "Could not run the test."), "error");
            } else if (message.simulated !== true) {
                feedback(state, "Gateway вернул неподдерживаемый формат результата теста.", "The Gateway returned an unsupported test result format.", "error");
            } else {
                state.result = message; renderResults(state);
                const success = message.success && message.status === "completed";
                feedback(state, success ? "Симуляция завершена. Ниже показаны виртуальные шаги." : message.status === "not_triggered" ? "Сценарий не запустился при этих исходных данных." : "Симуляция остановилась. Подробности показаны ниже.",
                    success ? "Simulation completed. Virtual steps are shown below." : message.status === "not_triggered" ? "The scenario did not trigger with these inputs." : "Simulation stopped. Details are shown below.", success ? "success" : message.status === "not_triggered" ? "warning" : "error");
            }
            controls(state); return true;
        }
    };
})();
