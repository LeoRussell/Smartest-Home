(() => {
    "use strict";

    const runs = new Map();
    const completed = new Set();
    let options = null;
    let timer = null;
    let highlightedBlock = null;
    let synchronized = false;

    const text = (ru, en) => options?.translate(ru, en) || ru;
    const now = () => performance.now();

    function rememberCompleted(runId) {
        completed.add(runId);
        if (completed.size > 200) completed.delete(completed.values().next().value);
    }

    function accept(progress) {
        if (!progress || typeof progress.automation_id !== "string" || typeof progress.run_id !== "string") return;
        if (completed.has(progress.run_id)) return;
        const previous = runs.get(progress.automation_id);
        if (previous?.run_id === progress.run_id && Number(previous.sequence) >= Number(progress.sequence)) return;
        if (previous && previous.run_id !== progress.run_id) rememberCompleted(previous.run_id);
        if (progress.status !== "running") {
            rememberCompleted(progress.run_id);
            runs.delete(progress.automation_id);
            return;
        }
        const remaining = Number(progress.remaining_seconds);
        runs.set(progress.automation_id, {
            ...progress,
            deadline: Number.isFinite(remaining) ? now() + Math.max(0, remaining) * 1000 : null
        });
    }

    function stageLabel(run) {
        switch (run.stage) {
            case "condition": return text("Проверка условия", "Checking condition");
            case "otherwise": return text("Выполнение ветки «иначе»", "Running the else branch");
            case "wait": return text("Пауза", "Waiting");
            case "sending": return text("Отправка команды", "Sending command");
            case "acknowledgment": return text("Ожидание подтверждения устройства", "Waiting for device acknowledgement");
            default: return text("Запуск сценария", "Starting scenario");
        }
    }

    function deviceLabel(id) {
        const device = options.getDevices?.().find(item => item.device === id);
        return device?.display_name || device?.name || id;
    }

    function describe(run) {
        const details = run.details || {};
        if (run.stage === "wait") {
            const seconds = Math.max(0, Math.ceil((run.deadline - now()) / 1000));
            return seconds > 0
                ? text(`Осталось ${seconds} с`, `${seconds} s remaining`)
                : text("Пауза завершается…", "Finishing wait…");
        }
        if (details.device) {
            const device = options.getDevices?.().find(item => item.device === details.device);
            const parameter = device?.schema?.parameters?.[details.parameter];
            const action = device?.schema?.actions?.[details.action];
            return [deviceLabel(details.device), parameter?.label || action?.label || details.parameter || details.action].filter(Boolean).join(" · ");
        }
        if (details.branch) return text(`Ветвь ${details.branch}`, `Branch ${details.branch}`);
        return text("Gateway выполняет сохранённую программу", "The Gateway runs the saved program");
    }

    function fill(panel, run) {
        panel.hidden = !run;
        if (!run) return;
        panel.dataset.stage = run.stage;
        panel.dataset.status = run.status;
        const step = Number(run.step_number) || 0;
        const title = panel.querySelector(".automation-progress-title");
        const description = panel.querySelector(".automation-progress-description");
        const meta = panel.querySelector(".automation-progress-meta");
        const progress = panel.querySelector("progress");
        const nextTitle = (step ? text(`Шаг ${step} · `, `Step ${step} · `) : "") + stageLabel(run);
        if (title.textContent !== nextTitle) title.textContent = nextTitle;
        description.textContent = describe(run);
        meta.textContent = text("Выполняется на Gateway", "Running on Gateway");
        progress.hidden = run.stage !== "wait";
        if (!progress.hidden) {
            const duration = Math.max(0.001, Number(run.details?.seconds) || 0);
            const remaining = Math.max(0, (run.deadline - now()) / 1000);
            progress.max = duration;
            progress.value = Math.max(0, duration - remaining);
            progress.setAttribute("aria-label", text("Время паузы", "Wait duration"));
            progress.setAttribute("aria-valuetext", describe(run));
        }
    }

    function createPanel(automationId) {
        const run = runs.get(automationId);
        if (!run) return null;
        const panel = document.createElement("section");
        panel.className = "automation-live-progress";
        panel.dataset.automationId = automationId;
        const title = document.createElement("strong");
        title.className = "automation-progress-title";
        title.setAttribute("role", "status");
        const description = document.createElement("p");
        description.className = "automation-progress-description";
        const progress = document.createElement("progress");
        progress.className = "automation-progress-wait";
        const meta = document.createElement("small");
        meta.className = "automation-progress-meta";
        panel.append(title, description, progress, meta);
        fill(panel, run);
        return panel;
    }

    function updatePanels() {
        if (!options) return;
        for (const card of document.querySelectorAll(".automation-card[data-automation-id]")) {
            if (runs.has(card.dataset.automationId) && !card.querySelector(".automation-live-progress")) {
                card.append(createPanel(card.dataset.automationId));
            }
        }
        for (const panel of document.querySelectorAll(".automation-live-progress[data-automation-id]")) {
            fill(panel, runs.get(panel.dataset.automationId));
        }
        const workspace = document.getElementById("automation-workspace");
        const editorId = options.getEditingId?.();
        const active = editorId ? runs.get(editorId) : null;
        let banner = document.getElementById("automation-editor-progress");
        if (active && workspace) {
            if (!banner || banner.dataset.automationId !== editorId) {
                banner?.remove();
                banner = createPanel(editorId);
                banner.id = "automation-editor-progress";
                workspace.before(banner);
            } else fill(banner, active);
        } else banner?.remove();
        let matchingProgram = false;
        if (active?.block_id && active.program_tag && workspace && !workspace.closest("[hidden]")) {
            try { matchingProgram = window.AutomationBlockly?.programTag() === active.program_tag; }
            catch (error) { /* An incomplete draft cannot represent the running program. */ }
        }
        if (banner && active?.block_id && !matchingProgram) {
            banner.querySelector(".automation-progress-meta").textContent = text(
                "Для подсветки откройте неизменённую программу этого запуска",
                "Open this run's unchanged program to highlight its block"
            );
        }
        const blockId = matchingProgram ? active.block_id : null;
        if (highlightedBlock !== blockId || blockId) {
            window.AutomationBlockly?.highlightBlock(blockId);
            highlightedBlock = blockId;
        }
        if (runs.size && timer === null) timer = setInterval(updatePanels, 1000);
        if (!runs.size && timer !== null) { clearInterval(timer); timer = null; }
    }

    function refresh() {
        if (!options) return;
        const automations = options.getAutomations();
        const ids = new Set(automations.map(item => item.automation_id));
        for (const [id, run] of runs) if (!ids.has(id)) { rememberCompleted(run.run_id); runs.delete(id); }
        if (synchronized && options.isConnected()) for (const item of automations) accept(item.active_progress);
        updatePanels();
    }

    window.AutomationProgress = {
        init(nextOptions) {
            if (!options) {
                document.addEventListener("visibilitychange", updatePanels);
                document.addEventListener("automation-history-change", updatePanels);
            }
            options = nextOptions;
            refresh();
        },
        createPanel,
        refresh,
        isRunning: automationId => runs.has(automationId),
        handleMessage(message) {
            if (message?.type === "automation_list") {
                synchronized = true;
                const ids = new Set((message.automations || []).map(item => item.automation_id));
                for (const [id, run] of runs) if (!ids.has(id)) { rememberCompleted(run.run_id); runs.delete(id); }
                for (const item of message.automations || []) {
                    if (item.active_progress) accept(item.active_progress);
                    else if (item.run_status !== "running" && runs.has(item.automation_id)) {
                        rememberCompleted(runs.get(item.automation_id).run_id);
                        runs.delete(item.automation_id);
                    }
                }
                updatePanels();
                return false;
            }
            if (message?.type !== "automation_progress") return false;
            accept(message);
            updatePanels();
            return true;
        },
        connectionChanged() {
            synchronized = false;
            runs.clear();
            completed.clear();
            updatePanels();
        }
    };
})();
