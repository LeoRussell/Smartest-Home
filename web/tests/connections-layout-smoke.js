/* Compact connection diagnostics must preserve controls, text and pagination. */
async function runConnectionsLayoutSmoke() {
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    const target = devices[0]?.device;
    check(target, "A fixture device is required for connection diagnostics.");
    const previousLanguage = uiSettings.language;
    uiSettings.language = "ru"; applyLanguage();
    const sentBefore = socket.sent.length;
    const timestamp = new Date().toISOString();
    ConnectionDiagnostics.open(target);
    let request = socket.sent.findLast(message => message.action === "get_device_connections");
    socket.receive({ type: "device_connections", success: true, request_id: request.request_id, device: target,
        sessions: [{ session_id: 101, status: "active", started_at: timestamp, duration_seconds: 30,
            last_message_at: timestamp, last_data_at: null, wifi_rssi_last: -61, wifi_rssi_min: -73, wifi_rssi_max: -61,
            uptime_start: 180, uptime_last: 210, reset_reason: "power_on", failed_probes: 0, probe_rtt_ms: 2.5, gateway_loop_lag_ms: 16 }],
        has_more: true, next_before_id: 101, summary: { session_count: 2, total_connected_seconds: 60 } });
    const dialog = document.querySelector(".connection-history-dialog[open]");
    const footer = dialog.querySelector(".connection-history-footer");
    const close = footer.querySelector('[data-role="connection-close"]');
    const note = footer.querySelector(".connection-history-footer-note");
    check(note?.textContent.includes("Пустое поле") && close?.textContent.includes("Закрыть"), "The full connection explanation and Close button must share one footer.");
    if (innerWidth >= 700) {
        check(note.getBoundingClientRect().right <= close.getBoundingClientRect().left, "The footer explanation is not left of Close.");
        const overview = dialog.querySelector(".connection-history-overview");
        check(overview.querySelector(".connection-history-summary").getBoundingClientRect().right <= overview.querySelector(".connection-history-tools").getBoundingClientRect().left, "The history summary and action buttons do not share a compact header row.");
    }
    check(dialog.querySelectorAll(".connection-session-stats .gateway-help-trigger").length >= 5, "Complex connection metrics need contextual information controls.");
    check(close.querySelector("svg") && dialog.querySelector('[data-role="connection-refresh"] svg') && dialog.querySelector('[data-role="connection-report"] svg'), "Connection actions lost their icons.");
    check(dialog.querySelector('[data-role="connection-report"]').disabled === false, "Loaded connection reports should remain downloadable.");
    const more = dialog.querySelector(".connection-history-more");
    more.click();
    request = socket.sent.findLast(message => message.action === "get_device_connections");
    check(request.before_id === 101 && dialog.querySelector('[data-role="connection-report"]').disabled, "Older-session loading or pending report state changed.");
    socket.receive({ type: "device_connections", success: true, request_id: request.request_id, device: target,
        sessions: [{ session_id: 100, status: "closed", category: "closed", started_at: timestamp, ended_at: timestamp, duration_seconds: 30 }],
        has_more: false, summary: { session_count: 2, total_connected_seconds: 60 } });
    check(dialog.querySelectorAll(".connection-session").length === 2 && more.hidden, "Compact history lost the loaded sessions or pagination state.");
    check(more.getClientRects().length === 0, "The older-connections button is still visible after the last page.");
    uiSettings.language = "en"; applyLanguage();
    check(close.querySelector("svg") && close.textContent.includes("Close") && dialog.querySelector('[data-role="connection-refresh"]').textContent.includes("Refresh"), "Translating connection actions removed icons or labels.");
    check(!socket.sent.slice(sentBefore).some(message => ["command", "invoke_action", "run_automation", "save_device_configuration"].includes(message.action)), "Connection diagnostics unexpectedly sent a device operation.");
    close.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    uiSettings.language = previousLanguage; applyLanguage();
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { compactHeader: true, footerLayout: true, metricHelp: true, labelledIcons: true, pagination: true, translation: true };
}
