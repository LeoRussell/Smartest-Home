/* A recoverable browser-local editor copy; never sends Gateway messages. */
(() => {
    "use strict";
    const MAX_BYTES = 128 * 1024;
    let options, storageKey, ui, timer = null, recovery = null, stored = null, baseline = null, error = "";
    const t = (ru, en) => options.localized(ru, en);
    const signature = document => JSON.stringify({ automation_id: document.automation_id || null, save_id: document.save_id || null, name: document.name, workspace: document.workspace });
    const node = (tag, className) => { const element = document.createElement(tag); if (className) element.className = className; return element; };
    const content = document => Boolean(document?.name?.trim() || document?.workspace?.blocks?.blocks?.length);

    function decode(raw) {
        if (new TextEncoder().encode(raw).length > MAX_BYTES) throw new Error("size");
        const value = JSON.parse(raw);
        if (value?.version !== 1 || typeof value.name !== "string" || value.name.length > 80 ||
            value.automation_id != null && (typeof value.automation_id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(value.automation_id)) ||
            value.save_id != null && (typeof value.save_id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(value.save_id)) ||
            !value.workspace || typeof value.workspace !== "object" || Array.isArray(value.workspace) ||
            typeof value.updated_at !== "string" || !Number.isFinite(Date.parse(value.updated_at))) throw new Error("format");
        const stack = [{ value: value.workspace, depth: 0 }]; let count = 0;
        while (stack.length) {
            const item = stack.pop();
            if (++count > 12000 || item.depth > 70) throw new Error("depth");
            if (item.value && typeof item.value === "object") {
                for (const child of Object.values(item.value)) stack.push({ value: child, depth: item.depth + 1 });
            }
        }
        return value;
    }

    function render() {
        if (!options || !ui) return;
        ui.root.hidden = !recovery && !stored && !error;
        ui.root.dataset.state = error || recovery ? "warning" : "success";
        ui.title.textContent = recovery
            ? t("Найден локальный черновик", "A local draft is available")
            : error ? t("Локальная копия не обновлена", "The local copy was not updated") : t("Черновик сохранён в браузере", "Draft saved in this browser");
        const record = recovery?.document || stored;
        ui.description.textContent = record
            ? `${record.name || t("Без названия", "Untitled")} · ${options.formatTime(record.updated_at)}` : "";
        ui.note.textContent = error === "corrupt"
            ? t("Сохранённая копия повреждена или имеет неподдерживаемый формат. Её можно удалить; текущие блоки не изменятся.", "The stored copy is damaged or unsupported. You can discard it without changing the current blocks.")
            : error === "size" ? t("Черновик больше 128 КиБ. Текущие блоки остаются в редакторе, прежняя копия сохранена.", "The draft exceeds 128 KiB. Current blocks remain in the editor; the previous copy was kept.")
                : error === "storage" ? t("Браузер не разрешил сохранить копию. Не закрывайте страницу, пока не сохраните сценарий на Gateway.", "The browser could not store a copy. Keep this page open until you save the scenario on the Gateway.")
                    : recovery ? t("Восстановление вернёт блоки в редактор. На Gateway ничего не отправляется. Копия не перезаписывается до вашего выбора.", "Restore returns the blocks to the editor without sending anything to the Gateway. This copy is protected until you choose what to do.")
                        : t("Это копия для восстановления после перезагрузки страницы. На Gateway сценарий ещё не сохранялся автоматически.", "This copy allows recovery after reloading the page. It does not automatically save the scenario on the Gateway.");
        ui.restore.hidden = !recovery?.document;
        ui.restore.disabled = !options.isReady();
        ui.restore.textContent = t("Восстановить черновик", "Restore draft");
        ui.discard.textContent = t("Удалить локальную копию", "Discard local copy");
        ui.discard.hidden = !recovery && !stored;
    }

    function removeCopy() {
        try { localStorage.removeItem(storageKey); }
        catch { error = "storage"; render(); return false; }
        recovery = stored = null; error = ""; return true;
    }

    function flush() {
        clearTimeout(timer); timer = null;
        if (!options?.isReady() || recovery) { render(); return; }
        let document, current;
        try { document = options.capture(); current = signature(document); }
        catch { return; }
        if (current === baseline) { if (stored) removeCopy(); render(); return; }
        if (stored && current === signature(stored)) return;
        if (!content(document)) { if (stored) removeCopy(); render(); return; }
        const record = { version: 1, ...document, updated_at: new Date().toISOString() };
        try {
            const raw = JSON.stringify(record);
            if (new TextEncoder().encode(raw).length > MAX_BYTES) { error = "size"; render(); return; }
            localStorage.setItem(storageKey, raw);
            stored = record; error = "";
        } catch { error = "storage"; }
        render();
    }

    function changed() {
        if (!options) return;
        clearTimeout(timer);
        timer = setTimeout(flush, 500);
    }

    function editorLoaded({ discardActive = false, unsaved = false } = {}) {
        if (!options?.isReady()) return;
        clearTimeout(timer); timer = null;
        baseline = signature(options.capture());
        if (!recovery && stored) {
            if (discardActive) removeCopy();
            else if (signature(stored) !== baseline) { recovery = { document: stored }; stored = null; }
        }
        if (unsaved && !recovery && content(options.capture())) { baseline = null; changed(); }
        render();
    }

    function restore() {
        if (!recovery?.document || !options.isReady()) return;
        const current = options.capture();
        if (content(current) && signature(current) !== baseline && !window.confirm(t("Заменить текущие несохранённые блоки локальным черновиком?", "Replace the current unsaved blocks with the local draft?"))) return;
        const record = recovery.document;
        try {
            if (options.restore(record) === false) return;
            recovery = null; stored = record; error = "";
            // Restored blocks are intentionally still an unsaved editor draft.
            baseline = null;
            render();
        } catch { error = "corrupt"; render(); }
    }

    window.AutomationDraft = {
        initialize(callbacks) {
            if (options) return;
            options = callbacks;
            storageKey = `iot-gateway-automation-draft-v1:${encodeURIComponent(window.GatewayConnection?.url?.() || `${location.hostname || "localhost"}:8765`)}`;
            const root = node("section", "automation-local-draft"), title = node("strong"), description = node("p"), note = node("p"), actions = node("div", "automation-local-draft-actions");
            const restoreButton = node("button"), discard = node("button");
            restoreButton.type = discard.type = "button";
            restoreButton.dataset.role = "restore-automation-draft"; discard.dataset.role = "discard-automation-draft";
            root.setAttribute("aria-label", t("Восстановление черновика", "Draft recovery"));
            title.setAttribute("role", "status");
            actions.append(restoreButton, discard); root.append(title, description, note, actions);
            document.getElementById("automation-editor-form").before(root);
            ui = { root, title, description, note, restore: restoreButton, discard };
            try {
                const raw = localStorage.getItem(storageKey);
                if (raw) { recovery = {}; try { recovery.document = decode(raw); } catch { error = "corrupt"; } }
            } catch { error = "storage"; }
            restoreButton.addEventListener("click", restore);
            discard.addEventListener("click", () => {
                if (removeCopy()) { baseline = options.isReady() ? signature(options.capture()) : null; render(); }
                document.getElementById("automation-name")?.focus({ preventScroll: true });
            });
            document.getElementById("automation-name")?.addEventListener("input", changed);
            document.addEventListener("automation-history-change", changed);
            document.addEventListener("visibilitychange", () => { if (document.hidden) flush(); });
            window.addEventListener("pagehide", flush);
            window.addEventListener("storage", event => {
                if (event.key !== storageKey) return;
                clearTimeout(timer); timer = null; stored = null; error = "";
                if (event.newValue) { recovery = {}; try { recovery.document = decode(event.newValue); } catch { error = "corrupt"; } }
                else recovery = null;
                render();
            });
            render();
        },
        changed, flush, editorLoaded, refresh: render
    };
})();
