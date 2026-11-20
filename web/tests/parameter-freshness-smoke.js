/* A heartbeat or another output must not make an old sensor reading fresh. */
async function runParameterFreshnessSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const originalPage = activePage;
    const realNow = Date.now;
    const started = realNow();
    let elapsed = 0;
    const tileItem = tile => JSON.parse(tile.dataset.tileKey);
    const tileFor = (deviceId, parameterId) => [...document.querySelectorAll(".workspace-tile")].find(tile => {
        const [kind, device, parameter] = tileItem(tile);
        return kind === "parameter" && device === deviceId && parameter === parameterId;
    });
    GatewayWorkspace.refresh();
    const existing = [...document.querySelectorAll(".workspace-tile")].map(tileItem).filter(item => item[0] === "parameter");
    check(existing.length >= 2 && existing[0][1] === existing[1][1], "The freshness smoke requires the dashboard fixture's two parameter tiles.");
    const [, deviceId, sensorId] = existing[0];
    const draftId = existing[1][2];
    const outputId = "freshness_output";
    const fixture = {
        device: deviceId, name: "Свежесть DS18B20", connected: true,
        last_seen: new Date(started).toISOString(), last_telemetry_at: new Date(started).toISOString(),
        telemetry_stale_after_seconds: 45,
        parameters: { [sensorId]: 23.5, [draftId]: 20, [outputId]: false },
        parameter_ages_seconds: { [sensorId]: 0, [draftId]: 0, [outputId]: 0 },
        schema: { parameters: {
            [sensorId]: { label: "Температура", type: "number", access: "read_only", min: -55, max: 125, precision: 4, unit: "°C" },
            [draftId]: { label: "Скорость", type: "integer", access: "read_write", min: 0, max: 100, control: "input" },
            [outputId]: { label: "Выход", type: "boolean", access: "read_write", control: "toggle" },
        }, actions: {} },
    };
    const panel = () => [...document.querySelectorAll(".device-panel")].find(item => item.dataset.deviceId === deviceId);
    const reading = () => panel().querySelector(`[data-role="parameter-value"][data-parameter-id="${sensorId}"]`);
    const sendPartial = (parameters, sensorAge) => socket.receive({
        type: "device_state", device: deviceId, parameters,
        last_seen: new Date(started + elapsed).toISOString(), last_telemetry_at: new Date(started + elapsed).toISOString(),
        parameter_ages_seconds: { [sensorId]: sensorAge, [draftId]: 0, [outputId]: 0 },
    });
    try {
        Date.now = () => started + elapsed;
        socket.receive({ type: "device_list", devices: [fixture] });
        setActivePage("overview");
        const sensorTile = tileFor(deviceId, sensorId);
        const draftTile = tileFor(deviceId, draftId);
        const input = draftTile.querySelector("input");
        check(reading().dataset.readingState === "fresh" && !reading().querySelector('[data-role="parameter-freshness"]'), "A new session sample was not shown as fresh.");
        input.value = "37";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
        elapsed = 45000;
        updateDeviceFreshnessHints();
        check(reading().dataset.readingState === "fresh", "A sample aged exactly 45 seconds was marked stale too early.");
        elapsed = 46000;
        updateDeviceFreshnessHints();
        check(reading().dataset.readingState === "stale" && reading().textContent.includes("Устарело"), "A sensor did not become stale without a new WebSocket message.");
        check(sensorTile.querySelector(".workspace-tile-status").textContent === "Устарело", "The Overview tile failed to age its sensor sample.");
        check(document.activeElement === input && input.value === "37" && draftTile.querySelector("input") === input, "The freshness timer replaced a focused dashboard draft.");

        sendPartial({ [outputId]: true }, 46);
        check(reading().dataset.readingState === "stale" && reading().textContent.includes("23.5"), "Changing an output made a cached sensor value fresh.");
        check(!panel().querySelector(`[data-role="parameter-command"][data-parameter-id="${outputId}"]`).disabled, "An old sensor blocked an unrelated output.");
        check(document.activeElement === input && input.value === "37", "Partial telemetry overwrote a dashboard input draft.");
        check(!panel().querySelector('[data-role="device-hint"]').textContent.includes("Все показания свежие"), "The device summary promises that all readings are fresh.");
        elapsed = 50000;
        sendPartial({}, 50);
        check(reading().dataset.readingState === "stale", "An empty heartbeat refreshed the sensor.");

        setActivePage("devices");
        const cardInput = panel().querySelector(`[data-role="parameter-input"][data-parameter-id="${draftId}"]`);
        cardInput.value = "38";
        cardInput.dispatchEvent(new Event("input", { bubbles: true }));
        cardInput.focus();
        elapsed = 51000;
        updateDeviceFreshnessHints();
        check(document.activeElement === cardInput && cardInput.value === "38", "The freshness timer replaced a focused device input draft.");
        socket.receive({ type: "device_list", devices: [{ ...fixture, parameter_ages_seconds: { [sensorId]: null, [draftId]: 0, [outputId]: 0 } }] });
        check(reading().dataset.readingState === "unknown" && reading().textContent.includes("Нет свежих показаний"), "A reconnect presented a cached value as a new session measurement.");
        check(tileFor(deviceId, sensorId).querySelector(".workspace-tile-status").textContent === "Нет свежих показаний", "The Overview tile ignored a missing current-session sample.");

        sendPartial({ [sensorId]: 24.0625 }, 0);
        check(reading().dataset.readingState === "fresh" && reading().textContent.includes("24.0625") && !reading().querySelector('[data-role="parameter-freshness"]'), "A genuine fractional sensor sample did not clear its stale warning.");
        check(tileFor(deviceId, sensorId).querySelector(".workspace-tile-status").textContent === "На связи", "A fresh sample did not restore the Overview status.");

        const legacy = JSON.parse(JSON.stringify(fixture));
        delete legacy.parameter_ages_seconds;
        socket.receive({ type: "device_list", devices: [legacy] });
        elapsed += 60000;
        updateDeviceFreshnessHints();
        check(reading().dataset.readingState === "legacy" && !reading().querySelector('[data-role="parameter-freshness"]'), "Old Gateway fixtures acquired unsupported per-parameter freshness warnings.");
        check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
        return { independentReadings: true, timerExpiry: true, partialOutputAndHeartbeat: true, reconnectCache: true, realSampleRecovery: true, focusedDrafts: true, legacyGateway: true };
    } finally {
        Date.now = realNow;
        socket.receive({ type: "device_list", devices: originalDevices });
        setActivePage(originalPage);
    }
}
