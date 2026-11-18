/* Shared visual fixtures and checks for concise descriptions on device cards. */
function uiPolishDevice() {
    const now = new Date().toISOString();
    return {
        device: "polish-c3", name: "ESP32-C3 SuperMini", display_name: "ESP32-C3 SuperMini", connected: true,
        device_profile: "universal", board_profile: "esp32-c3-supermini", firmware_version: "1.0.3", group_id: "polish-room",
        last_seen: now, last_telemetry_at: now,
        health: { wifi_rssi: -66, uptime_seconds: 600, reset_reason: "power_on", updated_at: now },
        parameters: { temperature: 420, light: false },
        schema: {
            capabilities: { universal_profile: true },
            components: { sensor: { label: "Температурный датчик", category: "sensor", description: "Считывает ADC и линейно преобразует настроенный raw-диапазон в заданные значения." },
                lamp: { label: "Светильник", category: "actuator", description: "Управляет обычным цифровым выходом." } },
            parameters: { temperature: { label: "Показание датчика", component: "sensor", type: "integer", access: "read_only", unit: "raw" },
                light: { label: "Освещение", component: "lamp", type: "boolean", access: "read_write", control: "toggle", description: "Логическое состояние светильника." } },
            actions: { test: { label: "Тестовое действие", description: "Описание авторского действия.", arguments: {} } },
        },
        configuration_limits: { max_components: 8, max_pwm_channels: 6, identify_gpio_pins: [0, 1, 3, 4, 5, 6, 7, 8, 10], identify_neopixel_pins: [] },
        driver_catalog: ["gpio_output", "digital_input", "analog_input", "pwm_output"].map(id => ({ id, label: id,
            pins: id === "analog_input" ? [0, 1, 3, 4] : [0, 1, 3, 4, 5, 6, 7, 10] })),
        hardware_configuration: { format_version: 1, components: [
            { id: "sensor", driver: "analog_input", label: "Температурный датчик", parameter_id: "temperature", parameter_label: "Показание датчика",
                pin: 0, unit: "raw", interval_ms: 1000, raw_min: 0, raw_max: 4095, value_min: 0, value_max: 4095 },
            { id: "lamp", driver: "gpio_output", label: "Светильник", parameter_id: "light", parameter_label: "Освещение", pin: 4, active_high: true, initial_value: false }
        ] }, configuration_version: 1, configuration_applied_version: 1, configuration_status: "applied",
    };
}

async function prepareUiPolishPreview(kind) {
    GatewayHelp.closeAll();
    for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
    await new Promise(resolve => setTimeout(resolve, 0));
    socket.receive({ type: "device_groups", groups: [{ group_id: "polish-room", name: "Гостиная" }] });
    socket.receive({ type: "device_list", devices: [uiPolishDevice()] });
    setActivePage("devices");
    if (kind === "devices") document.querySelector(".device-panel")?.scrollIntoView({ block: "center" });
    if (kind === "settings") DeviceManagement.openDeviceSettings(devices[0]);
    if (kind === "hardware") openDeviceConfigurationDialog(devices[0]);
    if (kind === "connections") {
        ConnectionDiagnostics.open(devices[0].device);
        const request = socket.sent.findLast(message => message.action === "get_device_connections");
        const now = new Date().toISOString();
        socket.receive({ type: "device_connections", success: true, request_id: request.request_id, device: devices[0].device,
            sessions: [{ session_id: 601, status: "active", started_at: now, duration_seconds: 600, last_message_at: now,
                last_data_at: now, wifi_rssi_last: -66, wifi_rssi_min: -78, wifi_rssi_max: -60,
                uptime_start: 240, uptime_last: 840, reset_reason: "power_on", failed_probes: 0, probe_rtt_ms: 3, gateway_loop_lag_ms: 16 }],
            has_more: false, summary: { session_count: 1, total_connected_seconds: 600 } });
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return true;
}

async function runContextualCardsSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const sent = socket.sent.length;
    await prepareUiPolishPreview("devices");
    const help = document.querySelector(".device-component-title .gateway-help-trigger");
    check(help && !document.querySelector(".device-component-description"), "The component description still takes a full card row.");
    help.click();
    check(document.getElementById(help.getAttribute("aria-controls")).textContent.includes("ADC"), "Component information lost its Schema description.");
    GatewayHelp.closeAll();
    check(document.querySelector('[data-role="device-action"] svg'), "Schema actions are missing their icon.");
    openDeviceConfigurationDialog(devices[0]);
    const dialog = document.querySelector(".device-config-dialog[open]");
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    check(dialog.scrollWidth <= dialog.clientWidth + 1, "Hardware configuration overflows horizontally.");
    if (innerWidth <= 540) {
        const actions = dialog.querySelector(".device-config-actions");
        check(actions.getBoundingClientRect().height < 150, "The mobile action bar consumes too much editor space.");
        check(dialog.querySelector(".device-config-retry").getAttribute("aria-label").includes("Retry") || dialog.querySelector(".device-config-retry").getAttribute("aria-label").includes("Повторить"), "Compact mobile actions lost their accessible names.");
    }
    check(dialog.querySelector(".device-config-title .gateway-help-trigger").getAttribute("aria-expanded") === "false", "Opening the configurator unexpectedly opened introductory help.");
    check(!dialog.querySelector(".device-config-driver-help, .device-config-field-help"), "Configurator guidance still occupies a row under every field.");
    const id = dialog.querySelector('[data-config-path="components.0.id"]');
    const wrapper = id.closest(".device-config-field");
    check(wrapper.querySelector("label").control === id && !dialog.querySelector("label .gateway-help-trigger"), "Contextual help broke input labelling.");
    id.value = ""; id.dispatchEvent(new Event("input", { bubbles: true }));
    check(id.getAttribute("aria-invalid") === "true" && !wrapper.querySelector(".device-config-field-error").hidden, "Required-field errors were hidden inside contextual help.");
    const info = wrapper.querySelector(".gateway-help-trigger"); info.click();
    info.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    check(dialog.open && id.value === "", "Help dismissal changed the hardware draft or closed the editor.");
    dialog.close(); await new Promise(resolve => setTimeout(resolve, 0));
    socket.receive({ type: "device_list", devices: originalDevices });
    check(!socket.sent.slice(sent).some(message => ["command", "invoke_action", "run_automation", "save_device_configuration"].includes(message.action)), "Viewing descriptions sent a device operation.");
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { compactDescriptions: true, labelledInputs: true, visibleErrors: true, draftPreservation: true, actionIcons: true };
}
