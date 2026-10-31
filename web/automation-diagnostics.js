(() => {
    "use strict";

    const DRAFT_KEY = Symbol("editor-draft");
    const checks = new Map();
    let options = null;
    let draftChangeTimer = null;

    function text(ru, en) {
        return options.translate(ru, en);
    }

    function keyFor(automationId) {
        return automationId || DRAFT_KEY;
    }

    function release(check) {
        if (check?.timer) clearTimeout(check.timer);
    }

    function update() {
        options.render();
        const host = document.getElementById("automation-editor-diagnostics");
        if (host) {
            host.replaceChildren();
            const panel = createPanel(null);
            host.hidden = !panel;
            if (panel) host.append(panel);
        }
        const button = document.getElementById("automation-check-draft");
        if (button) button.disabled = !options.isConnected() || Boolean(checks.get(DRAFT_KEY)?.pending);
    }

    function request(automationId = null) {
        if (!options.isConnected() || checks.get(keyFor(automationId))?.pending) return;
        const key = keyFor(automationId);
        let config;
        try {
            config = automationId
                ? options.getAutomations().find(item => item.automation_id === automationId)?.config
                : options.getDraft();
            if (!config) return;
        } catch (error) {
            checks.set(key, {
                issues: [{ severity: "error", message: error.message }],
                checkedAt: new Date().toISOString()
            });
            update();
            return;
        }
        release(checks.get(key));
        const check = {
            requestId: options.createRequestId(),
            signature: JSON.stringify(config),
            pending: true,
            checkedAt: new Date().toISOString(),
            issues: []
        };
        checks.set(key, check);
        check.timer = setTimeout(() => {
            if (checks.get(key) !== check || !check.pending) return;
            check.pending = false;
            check.transportError = true;
            update();
        }, 10000);
        if (!options.send("check_automation", {
            automation_id: automationId,
            ...(automationId ? {} : { config }),
            request_id: check.requestId
        })) {
            release(check);
            check.pending = false;
            check.transportError = true;
        }
        update();
    }

    function createPanel(automationId) {
        const check = checks.get(keyFor(automationId));
        if (!check) return null;
        const panel = document.createElement("section");
        panel.className = "automation-diagnostics";
        panel.setAttribute("aria-live", "polite");
        const title = document.createElement("strong");
        if (check.pending) {
            title.textContent = text("Проверяю сценарий…", "Checking scenario…");
        } else if (check.stale) {
            title.textContent = check.connectionChanged
                ? text("Соединение менялось — повторите проверку", "Connection changed — check again")
                : text("Сценарий изменён — повторите проверку", "Scenario changed — check it again");
        } else if (check.transportError) {
            title.textContent = text("Ответ Gateway не получен. Повторите проверку.", "No Gateway response. Check again.");
        } else if (check.issues.length) {
            const hasErrors = check.issues.some(issue => issue.severity === "error");
            title.textContent = hasErrors
                ? text("Нужно исправить сценарий", "Scenario needs changes")
                : text("Проверьте условия работы", "Check operating conditions");
            panel.classList.add(hasErrors ? "has-errors" : "has-warnings");
        } else {
            title.textContent = text("Сценарий готов к работе", "Scenario is ready");
            panel.classList.add("is-ready");
        }
        panel.append(title);
        if (!check.pending && !check.stale && !check.transportError && check.issues.length) {
            const list = document.createElement("ul");
            for (const issue of check.issues) {
                const row = document.createElement("li");
                row.className = issue.severity === "error" ? "is-error" : "is-warning";
                const message = document.createElement("span");
                message.textContent = (options.language() === "en" && issue.message_en) ||
                    issue.message || text("Проверьте сценарий.", "Check the scenario.");
                row.append(message);
                if (issue.device && options.hasDevice(issue.device)) {
                    const link = document.createElement("button");
                    link.type = "button";
                    link.className = "automation-diagnostic-device";
                    link.textContent = issue.device;
                    link.title = text("Открыть устройство", "Open device");
                    link.addEventListener("click", () => options.showDevice(issue.device));
                    row.append(link);
                }
                list.append(row);
            }
            panel.append(list);
        }
        const note = document.createElement("small");
        note.textContent = check.checkedAt
            ? text(`Состояние на ${options.formatTime(check.checkedAt)}. Проверка не запускает команды.`,
                `Status at ${options.formatTime(check.checkedAt)}. Checks do not run commands.`)
            : text("Проверка не запускает команды.", "Checks do not run commands.");
        panel.append(note);
        return panel;
    }

    function filtered(items) {
        const query = (document.getElementById("automation-search")?.value || "").trim().toLocaleLowerCase();
        const filter = document.getElementById("automation-filter")?.value || "all";
        const result = items.filter(item => {
            const check = checks.get(item.automation_id);
            const attention = Boolean(item.last_error) || (!check?.stale && check?.issues?.length > 0);
            if (filter === "enabled" && !item.enabled || filter === "disabled" && item.enabled ||
                filter === "attention" && !attention) return false;
            if (!query) return true;
            const searchable = [item.config?.name, item.automation_id,
                JSON.stringify(item.config?.program || item.config?.trigger || {}), item.last_error]
                .filter(Boolean).join(" ").toLocaleLowerCase();
            return searchable.includes(query);
        });
        const count = document.getElementById("automation-result-count");
        if (count) count.textContent = text(
            `Показано ${result.length} из ${items.length} · Включено: ${items.filter(item => item.enabled).length}`,
            `Showing ${result.length} of ${items.length} · Enabled: ${items.filter(item => item.enabled).length}`
        );
        return result;
    }

    function invalidateDraft() {
        if (!checks.has(DRAFT_KEY)) return;
        clearTimeout(draftChangeTimer);
        draftChangeTimer = setTimeout(() => {
            const check = checks.get(DRAFT_KEY);
            if (!check) return;
            let signature;
            try { signature = JSON.stringify(options.getDraft()); } catch { signature = null; }
            if (signature !== check.signature) {
                release(check);
                check.pending = false;
                check.stale = true;
                update();
            }
        }, 150);
    }

    window.AutomationInspector = {
        initialize(callbacks) {
            options = callbacks;
            document.getElementById("automation-search")?.addEventListener("input", options.render);
            document.getElementById("automation-filter")?.addEventListener("change", options.render);
            document.getElementById("automation-check-draft")?.addEventListener("click", () => request());
            document.getElementById("automation-name")?.addEventListener("input", invalidateDraft);
            document.addEventListener("automation-history-change", invalidateDraft);
        },
        sync(items) {
            const configs = new Map(items.map(item => [item.automation_id, item.config]));
            for (const [key, check] of checks) {
                if (key === DRAFT_KEY) continue;
                if (!configs.has(key)) {
                    release(check);
                    checks.delete(key);
                } else if (check.signature !== JSON.stringify(configs.get(key))) {
                    release(check);
                    check.pending = false;
                    check.stale = true;
                }
            }
        },
        handleMessage(message) {
            if (message.type !== "automation_diagnostics") return;
            const key = keyFor(message.automation_id);
            const check = checks.get(key);
            if (!check || check.requestId !== message.request_id || check.stale || !check.pending) return;
            if (key === DRAFT_KEY) {
                let signature;
                try { signature = JSON.stringify(options.getDraft()); } catch { signature = null; }
                if (signature !== check.signature) {
                    clearTimeout(draftChangeTimer);
                    release(check);
                    check.pending = false;
                    check.stale = true;
                    update();
                    return;
                }
            }
            release(check);
            check.pending = false;
            check.checkedAt = message.checked_at || check.checkedAt;
            check.issues = Array.isArray(message.issues) ? message.issues.slice(0, 50) : [];
            if (message.success === false && !check.issues.length) {
                check.issues.push({ severity: "error", message: message.message || text("Сценарий недоступен.", "Scenario is unavailable.") });
            }
            update();
        },
        disconnected() {
            for (const check of checks.values()) {
                release(check);
                if (check.pending) {
                    check.pending = false;
                    check.transportError = true;
                } else {
                    check.stale = true;
                    check.connectionChanged = true;
                }
            }
            update();
        },
        clearDraft() {
            release(checks.get(DRAFT_KEY));
            checks.delete(DRAFT_KEY);
            update();
        },
        reveal() {
            const search = document.getElementById("automation-search");
            const filter = document.getElementById("automation-filter");
            if (search) search.value = "";
            if (filter) filter.value = "all";
        },
        refresh: update,
        filtered,
        createPanel,
        request,
        pending: automationId => Boolean(checks.get(keyFor(automationId))?.pending)
    };
})();
