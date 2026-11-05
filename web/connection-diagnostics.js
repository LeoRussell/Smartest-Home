/* Per-device connection sessions. This view never sends device commands. */
(() => {
    "use strict";
    let context, view = null, sequence = 0;
    const cache = new Map();
    const t = (ru, en) => context.localized(ru, en);
    const node = (tag, text, className) => {
        const element = document.createElement(tag);
        if (text != null) element.textContent = text;
        if (className) element.className = className;
        return element;
    };
    const button = (caption, action) => {
        const element = node("button", caption); element.type = "button";
        element.addEventListener("click", action); return element;
    };
    const deviceName = id => { const device = context.getDevices().find(item => item.device === id); return device?.display_name || device?.name || id; };
    const time = value => value ? context.formatTime(value) : "—";
    const number = value => Number.isFinite(value) ? new Intl.NumberFormat(context.language(), { maximumFractionDigits: 1 }).format(value) : "—";
    const categories = () => ({
        closed: t("Соединение завершено", "Connection closed"),
        transport_timeout: t("Нет ответа на проверку связи", "Connection probe timed out"),
        network_lost: t("Транспортное соединение прервано", "Transport connection lost"),
        server_error: t("Ошибка обработки соединения", "Connection handling error"),
        replaced: t("Новое соединение с тем же ID", "Replaced by a connection with the same ID"),
        forgotten: t("Устройство забыто пользователем", "Device forgotten by the user"),
        user_blocked: t("Устройство заблокировано пользователем", "Device blocked by the user"),
        gateway_shutdown: t("Gateway остановлен", "Gateway stopped"),
        gateway_restart: t("Gateway перезапущен; завершение прошлой сессии не зафиксировано", "Gateway restarted; the previous session did not close cleanly"),
        database_restore: t("Восстановление базы Gateway", "Gateway database restored")
    });
    const help = () => t("Wi-Fi, ответы Ping и задержки Gateway помогают сопоставить разрывы, но сами по себе не доказывают причину. Пустое поле означает, что измерение не получено.", "Wi-Fi, Ping replies and Gateway delays help compare disconnects but do not prove their cause. An empty field means that measurement was not received.");

    function remember() {
        if (!view?.data) return;
        cache.delete(view.device);
        cache.set(view.device, { ...view.data, sessions: view.sessions.slice(0, 100).map(session => ({
            ...session, duration_seconds: duration(session), receivedAt: null
        })) });
        while (cache.size > 20) cache.delete(cache.keys().next().value);
    }

    function abortPending() {
        if (!view) return;
        clearTimeout(view.timeout); view.pending = null;
    }

    function duration(session) {
        const elapsed = view?.live && session.status === "active" && session.receivedAt != null
            ? Math.max(0, (performance.now() - session.receivedAt) / 1000) : 0;
        return (Number(session.duration_seconds) || 0) + elapsed;
    }

    function freezeDurations() {
        if (!view) return;
        for (const session of view.sessions) {
            session.duration_seconds = duration(session);
            session.receivedAt = null;
        }
        view.live = false;
    }

    function downloadReport() {
        if (!view?.data || !view.sessions.length || view.pending) return;
        const report = {
            format: "smartest-home-connection-report", version: 1,
            exported_at: new Date().toISOString(), received_at: view.data.received_at || null,
            device: { id: view.device, name: deviceName(view.device) },
            gateway_connected: context.isConnected(),
            scope: "loaded_sessions", loaded_session_count: view.sessions.length,
            has_more: Boolean(view.data.has_more), retention_days: view.data.retention_days,
            summary: view.data.summary,
            sessions: view.sessions.map(session => {
                const { receivedAt, ...fields } = session;
                return { ...fields, duration_seconds: duration(session) };
            })
        };
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json;charset=utf-8" }));
        const link = node("a"); link.href = url;
        link.download = `${view.device.replace(/[^A-Za-z0-9_.-]/g, "_")}-connections-${new Date().toISOString().slice(0, 10)}.json`;
        link.hidden = true; document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function request(append = false) {
        if (!view || view.pending || view.unsupported || !context.isConnected()) return;
        const requestId = `connections-${Date.now()}-${++sequence}`;
        view.pending = { id: requestId, append };
        view.notice = t("Загружаю историю соединений…", "Loading connection history…");
        view.noticeState = "info";
        const payload = { request_id: requestId, device: view.device, limit: 12 };
        if (append && view.data?.next_before_id) payload.before_id = view.data.next_before_id;
        if (!context.send("get_device_connections", payload)) {
            abortPending(); view.notice = t("Не удалось отправить запрос.", "Could not send request."); view.noticeState = "error";
        } else view.timeout = setTimeout(() => {
            if (!view?.pending || view.pending.id !== requestId) return;
            abortPending(); view.notice = t("Ответ не получен. Проверьте, что Gateway перезапущен после обновления.", "No response. Make sure the Gateway was restarted after updating."); view.noticeState = "error"; renderHeader();
        }, 12000);
        renderHeader();
    }

    function renderHeader() {
        if (!view) return;
        view.heading.textContent = t(`История соединений · ${deviceName(view.device)}`, `Connection history · ${deviceName(view.device)}`);
        view.refresh.textContent = t("Обновить", "Refresh");
        view.download.textContent = t("Скачать отчёт", "Download report");
        view.download.title = t("JSON с загруженными сессиями и измерениями. Для полной истории сначала загрузите более ранние подключения.", "JSON containing loaded sessions and measurements. Load older connections first to include more history.");
        view.download.disabled = !view.sessions.length || Boolean(view.pending);
        view.more.textContent = t("Более ранние подключения", "Older connections");
        view.close.textContent = t("Закрыть", "Close");
        view.explanation.textContent = help();
        view.refresh.disabled = !context.isConnected() || view.unsupported || Boolean(view.pending);
        view.more.disabled = !context.isConnected() || view.unsupported || Boolean(view.pending) || view.sessions.length >= 100;
        view.more.hidden = !view.data?.has_more || view.sessions.length >= 100;
        view.status.textContent = !context.isConnected()
            ? (view.sessions.length ? t("Gateway недоступен. Показана загруженная ранее история.", "Gateway is offline. Showing previously loaded history.") : t("Нет связи с Gateway.", "Gateway is offline."))
            : view.notice || (view.paged
                ? t("Автообновление приостановлено на время просмотра старых сессий. «Обновить» вернёт последние подключения.", "Automatic refresh is paused while browsing older sessions. Refresh returns to the latest connections.")
                : t("История обновляется каждые 15 секунд, пока окно открыто.", "History refreshes every 15 seconds while this window is open."));
        view.status.dataset.state = view.noticeState || "info";
        const summary = view.data?.summary;
        view.summary.textContent = summary
            ? t(`Сессий в истории: ${summary.session_count} · Всего на связи: ${context.formatDuration(summary.total_connected_seconds)}`, `Sessions recorded: ${summary.session_count} · Total connected: ${context.formatDuration(summary.total_connected_seconds)}`)
            : t("Подробная история собирается после обновления Gateway; старые события подключения остаются в журнале событий.", "Detailed history starts after the Gateway update; earlier connection events remain in the event log.");
    }

    function renderSessions() {
        if (!view) return;
        const opened = new Map([...view.list.querySelectorAll("details[data-session-id]")].map(element => [element.dataset.sessionId, element.open]));
        const focusedSession = view.list.contains(document.activeElement)
            ? document.activeElement.closest("details[data-session-id]")?.dataset.sessionId : null;
        view.list.replaceChildren();
        if (!view.sessions.length) view.list.append(node("p", view.pending ? t("Ожидается ответ…", "Waiting for a reply…") : t("Сессий пока нет. Подключите устройство после обновления Gateway.", "No sessions yet. Connect the device after updating the Gateway."), "management-help"));
        for (const [index, session] of view.sessions.entries()) {
            const details = node("details", null, "connection-session");
            details.dataset.sessionId = String(session.session_id); details.dataset.status = session.status === "active" && !context.isConnected() ? "cached" : session.status;
            details.open = opened.get(String(session.session_id)) ?? index < 2;
            const summary = node("summary");
            const status = node("span", session.status === "active" ? (context.isConnected() ? t("На связи", "Connected") : t("Последняя активная сессия", "Last known active session")) : session.status === "interrupted" ? t("Прервана", "Interrupted") : t("Завершена", "Ended"), "connection-session-state");
            const duration = node("span", "", "connection-session-duration");
            duration.dataset.sessionId = String(session.session_id);
            summary.append(node("strong", time(session.started_at)), status, duration);
            const content = node("div", null, "connection-session-content");
            const reason = node("p", session.status === "active" ? t("Текущая сессия", "Current session") : categories()[session.category] || t("Соединение завершено", "Connection ended"), "connection-session-reason");
            if (session.close_code != null || session.close_reason) {
                reason.append(node("small", [session.close_code != null ? `WebSocket ${session.close_code}` : "", session.close_reason].filter(Boolean).join(" · ")));
            }
            const stats = node("dl", null, "connection-session-stats");
            function stat(label, value) { const row = node("div"); row.append(node("dt", label), node("dd", value)); stats.append(row); }
            stat(t("Завершение", "Ended"), session.status === "active" ? t("Сессия продолжается", "Session is active") : time(session.ended_at));
            stat(t("Последнее сообщение", "Last message"), time(session.last_message_at));
            stat(t("Последние показания", "Last readings"), time(session.last_data_at));
            stat(t("Последний Wi-Fi", "Last Wi-Fi"), Number.isFinite(session.wifi_rssi_last) ? `${session.wifi_rssi_last} dBm` : "—");
            stat(t("Wi-Fi за сессию: min…max", "Session Wi-Fi: min…max"), Number.isFinite(session.wifi_rssi_min) && Number.isFinite(session.wifi_rssi_max) ? `${session.wifi_rssi_min}…${session.wifi_rssi_max} dBm` : "—");
            stat(t("Время работы ESP32: начало → конец", "ESP32 uptime: start → end"), `${session.uptime_start == null ? "—" : context.formatDuration(session.uptime_start)} → ${session.uptime_last == null ? "—" : context.formatDuration(session.uptime_last)}`);
            stat(t("Причина запуска ESP32", "ESP32 boot reason"), session.reset_reason || "—");
            stat(t("Последний разрыв по отчёту ESP32", "Last disconnect reported by ESP32"), session.device_disconnect_reason || "—");
            stat(t("Время ответа Ping", "Ping round-trip time"), session.probe_rtt_ms == null ? "—" : `${number(session.probe_rtt_ms)} ${t("мс", "ms")}`);
            stat(t("Проверок без ответа Pong", "Probes without a Pong"), String(session.failed_probes || 0));
            stat(t("Макс. задержка цикла Gateway", "Maximum Gateway loop delay"), session.gateway_loop_lag_ms == null ? "—" : `${number(session.gateway_loop_lag_ms)} ${t("мс", "ms")}`);
            content.append(reason, stats); details.append(summary, content); view.list.append(details);
        }
        updateDurations(); renderHeader();
        if (focusedSession) [...view.list.children].find(element => element.dataset.sessionId === focusedSession)
            ?.querySelector("summary")?.focus({ preventScroll: true });
    }

    function updateDurations() {
        if (!view) return;
        for (const element of view.list.querySelectorAll(".connection-session-duration")) {
            const session = view.sessions.find(item => String(item.session_id) === element.dataset.sessionId);
            if (session) element.textContent = context.formatDuration(duration(session));
        }
    }

    function open(deviceId) {
        if (view?.root.open) {
            if (view.device === deviceId) return;
            freezeDurations(); remember(); abortPending(); view.root.close();
        }
        const root = node("dialog", null, "management-dialog connection-history-dialog"); root.dataset.deviceId = deviceId;
        const heading = node("h2", "", "management-dialog-title"); context.labelDialog(root, heading);
        const body = node("div", null, "management-dialog-body");
        const tools = node("div", null, "connection-history-tools");
        const refresh = button("", () => request());
        const download = button("", downloadReport); download.dataset.role = "connection-report";
        const summary = node("p", "", "management-help");
        const status = node("p", "", "management-feedback"); status.setAttribute("role", "status");
        const list = node("div", null, "connection-session-list");
        const more = button("", () => request(true));
        const explanation = node("p", help(), "management-help");
        const actions = node("div", null, "management-dialog-actions"); const close = button("", () => root.close());
        tools.append(download, refresh); actions.append(close); body.append(tools, summary, status, list, more, explanation, actions); root.append(heading, body);
        const data = cache.get(deviceId) || null;
        const current = { root, heading, body, refresh, download, more, close, summary, status, list, explanation, device: deviceId, pending: null, timeout: null, sessions: (data?.sessions || []).map(session => ({ ...session })), data, live: false, paged: false, notice: "", noticeState: "info", unsupported: false };
        view = current;
        root.addEventListener("close", () => {
            if (view === current) { freezeDurations(); remember(); view = null; }
            clearTimeout(current.timeout);
            clearInterval(current.durationTimer); clearInterval(current.refreshTimer); root.remove();
        }, { once: true });
        current.durationTimer = setInterval(() => { if (!document.hidden) updateDurations(); }, 1000);
        current.refreshTimer = setInterval(() => { if (view === current && !document.hidden && root.open && !current.paged && !current.unsupported && !current.pending && context.isConnected()) request(); }, 15000);
        document.body.append(root); root.showModal(); renderSessions(); request();
    }

    window.ConnectionDiagnostics = {
        initialize(options) { context = options; }, open,
        refresh() { if (view) { renderHeader(); } },
        languageChanged() { if (view) renderSessions(); },
        disconnected() { if (view) { abortPending(); freezeDurations(); remember(); renderSessions(); } },
        connected() { if (view?.root.open) { view.unsupported = false; request(); } },
        handleMessage(message) {
            if (message.type === "device_identity_result" && message.success && message.new_device_id !== message.device) {
                const previous = cache.get(message.device); if (previous) { cache.delete(message.device); cache.set(message.new_device_id, previous); }
                if (view?.device === message.device) { abortPending(); view.device = message.new_device_id; view.root.dataset.deviceId = view.device; renderHeader(); request(); }
                return false;
            }
            if (message.type === "device_connections") {
                if (!view?.pending || view.pending.id !== message.request_id || (message.success !== false && message.device !== view.device)) return true;
                const append = view.pending.append; abortPending();
                if (message.success === false) { view.notice = message.message || t("Не удалось загрузить историю.", "Could not load history."); view.noticeState = "error"; renderHeader(); return true; }
                const incoming = (Array.isArray(message.sessions) ? message.sessions : []).filter(item => item && Number.isInteger(item.session_id))
                    .map(session => ({ ...session, receivedAt: performance.now() }));
                const combined = new Map((append ? view.sessions : []).map(item => [item.session_id, item]));
                for (const session of incoming) combined.set(session.session_id, session);
                view.sessions = [...combined.values()].sort((a, b) => b.session_id - a.session_id).slice(0, 100);
                view.data = { ...message, received_at: new Date().toISOString() }; view.live = true; view.paged = append; view.notice = ""; view.noticeState = "info";
                remember(); renderSessions(); return true;
            }
            if (message.type === "error" && view?.pending) {
                const unknown = message.action === "get_device_connections" && ["unsupported_action", "unknown_action"].includes(message.code) || /^(?:Неизвестное действие|Unknown action):\s*get_device_connections\s*$/i.test(message.message || "");
                if (unknown || message.request_id === view.pending.id) {
                    if (unknown) view.unsupported = true;
                    abortPending(); view.notice = unknown ? t("Перезапустите Gateway после обновления, чтобы открыть историю соединений.", "Restart the Gateway after updating to open connection history.") : message.message || t("Не удалось загрузить историю.", "Could not load history.");
                    view.noticeState = "error"; renderHeader(); return true;
                }
            }
            return false;
        }
    };
})();
