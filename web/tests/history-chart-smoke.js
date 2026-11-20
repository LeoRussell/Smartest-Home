/* Isolated readings: every request is answered by browser_smoke.py's mock WebSocket. */
async function runHistoryChartSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    check(window.WebSocket.name === "SmokeSocket", "History checks must use a mocked Gateway.");
    const original = { devices: JSON.parse(JSON.stringify(devices)), page: activePage, language: uiSettings.language, timezone: uiSettings.timezone };
    const panel = () => document.getElementById("parameter-history-panel");
    const svg = () => panel().querySelector(".history-chart svg");
    const tooltip = () => panel().querySelector(".history-tooltip");
    const frame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const timestamps = ["2026-11-20T10:00:00Z", "2026-11-20T10:01:00Z", "2026-11-20T10:02:00Z"];
    const makePoints = values => values.map((value, index) => ({ timestamp: timestamps[index], value, min: value, max: value, count: 1 }));
    const fixture = { device: "history-interaction", name: "History <readings>", connected: true, parameters: {}, schema: { parameters: {
        temperature: { label: "Temperature <sample>", type: "number", access: "read_only", unit: "°C", history: { enabled: true } },
        led: { label: "LED", type: "boolean", access: "read_write", history: { enabled: true } },
        mode_active: { label: "Blink mode", type: "boolean", access: "read_only", history: false },
        logical_power: { label: "Requested state", type: "boolean", access: "read_write", history: { enabled: false } },
    }, actions: {} } };
    const reply = samples => {
        const query = socket.sent.findLast(item => item.action === "get_parameter_history");
        check(query?.device === fixture.device, "The chart requested another device.");
        socket.receive({ type: "parameter_history", success: true, request_id: query.request_id, device: query.device,
            parameter: query.parameter, range: query.range, value_type: query.parameter === "led" ? "boolean" : "number",
            unit: query.parameter === "temperature" ? "°C" : "", recording_enabled: true, points: samples });
    };
    const load = (parameter, samples) => {
        const select = panel().querySelector(".history-tools select");
        select.value = JSON.stringify(["parameter", fixture.device, parameter]);
        select.dispatchEvent(new Event("change", { bubbles: true }));
        reply(samples);
    };
    const pathPoint = fraction => {
        const path = svg().querySelector(".history-series");
        return path.getPointAtLength(path.getTotalLength() * fraction).matrixTransform(svg().getScreenCTM());
    };
    const clickPoint = fraction => {
        const point = pathPoint(fraction);
        svg().dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: point.x, clientY: point.y }));
    };
    const key = value => svg().dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: value }));
    const selected = (timestamp, value) => {
        check(!tooltip().hidden, "A selected reading has no tooltip.");
        check(tooltip().querySelector("span").textContent === formatEventTime(timestamp), "The tooltip selected the wrong reading time.");
        check(tooltip().querySelector("strong").textContent === value, `Wrong selected value: ${tooltip().textContent}`);
        check(panel().querySelector(".history-readout").textContent.includes(formatEventTime(timestamp)), "The selected reading is not announced to assistive technology.");
        const bounds = panel().querySelector(".history-chart").getBoundingClientRect(), tip = tooltip().getBoundingClientRect();
        check(tip.left >= bounds.left && tip.right <= bounds.right + .5 && tip.top >= bounds.top && tip.bottom <= bounds.bottom + .5, "The point tooltip escapes the chart.");
        check(svg().querySelector(".history-selection").getAttribute("visibility") === "visible", "The selected point has no visible marker.");
    };
    try {
        GatewayWorkspace.connected();
        socket.receive({ type: "device_list", devices: [fixture] });
        setActivePage("overview");
        const optionParameters = () => [...panel().querySelector(".history-tools select").options].map(option => JSON.parse(option.value)[2]);
        check(JSON.stringify(optionParameters().sort()) === JSON.stringify(["led", "temperature"]), "The history selector includes modes or values with disabled recording.");
        const card = () => document.querySelector('[data-device-id="history-interaction"]');
        check(!card().querySelector('[data-role="parameter-history"][data-parameter-id="mode_active"]') &&
            !card().querySelector('[data-role="parameter-history"][data-parameter-id="logical_power"]'), "A disabled history parameter still has a card chart button.");
        // Reuse the same rendered card: attachDevice must also remove a previously created graph button.
        const revised = JSON.parse(JSON.stringify(fixture)); revised.schema.parameters.temperature.history = false;
        GatewayWorkspace.attachDevice(revised, card());
        check(!card().querySelector('[data-role="parameter-history"][data-parameter-id="temperature"]'), "An existing chart link survives recording being disabled in Schema.");
        GatewayWorkspace.attachDevice(fixture, card());
        for (const language of ["ru", "en"]) {
            uiSettings.language = language; uiSettings.timezone = language === "ru" ? "Europe/Moscow" : "UTC"; applyLanguage();
            load("temperature", makePoints([22.5, -3.25, 22.5]));
            await frame();
            check(!panel().querySelector('input[type="range"]'), "The old point-selection slider is still present.");
            check(svg().tabIndex === 0 && svg().getAttribute("aria-keyshortcuts").includes("Home"), "The graph cannot be navigated with a keyboard.");
            check(tooltip().hidden, "A fresh graph should wait for a point selection.");
            clickPoint(0); selected(timestamps[0], "22.5 °C");
            clickPoint(.5); selected(timestamps[1], "-3.25 °C");
            clickPoint(1); selected(timestamps[2], "22.5 °C");
            key("Home"); selected(timestamps[0], "22.5 °C");
            key("ArrowRight"); selected(timestamps[1], "-3.25 °C");
            key("End"); key("ArrowRight"); selected(timestamps[2], "22.5 °C");
            key("ArrowLeft"); selected(timestamps[1], "-3.25 °C");
            // Reflow and a reload must preserve the selected reading, including keyboard focus.
            const chart = panel().querySelector(".history-chart");
            chart.style.width = `${Math.min(chart.clientWidth - 20, 430)}px`;
            svg().focus(); window.dispatchEvent(new Event("resize")); await frame();
            selected(timestamps[1], "-3.25 °C");
            check(document.activeElement === svg(), "Reflow loses keyboard focus on the chart.");
            chart.style.width = ""; window.dispatchEvent(new Event("resize")); await frame();
            panel().querySelector(".history-tools button").click(); reply(makePoints([22.5, -3.25, 22.5]));
            selected(timestamps[1], "-3.25 °C");
            check(panel().querySelectorAll(".history-table-scroll tbody tr").length === 3, "Point selection broke the readings table.");
            const exportButton = panel().querySelector(".history-tools button:last-child");
            check(!exportButton.disabled, "Point selection disabled CSV export.");

            load("led", makePoints([0, 1, 0])); await frame();
            if (!localStorage.getItem("iot-gateway-history-period-v1")) check(panel().querySelectorAll(".history-tools select")[1].value === "minute", "Boolean readings do not default to the Minute range.");
            const off = language === "ru" ? "Выкл (0)" : "Off (0)", on = language === "ru" ? "Вкл (1)" : "On (1)";
            check(svg().querySelector(".history-series").getAttribute("d").includes("H"), "Boolean states lost their stepped graph.");
            clickPoint(0); selected(timestamps[0], off);
            key("ArrowRight"); selected(timestamps[1], on);
            clickPoint(1); selected(timestamps[2], off);
            check(svg().querySelectorAll("circle").length === 1, "The graph draws a distracting marker for every reading.");

            // Points at nearly equal times can be far apart vertically: use both display coordinates.
            const clustered = [
                { timestamp: timestamps[0], value: 0, min: 0, max: 0, count: 1 },
                { timestamp: "2026-11-20T10:00:01Z", value: 100, min: 100, max: 100, count: 1 },
                { timestamp: timestamps[2], value: 0, min: 0, max: 0, count: 1 },
            ];
            load("temperature", clustered); await frame();
            const path = svg().querySelector(".history-series");
            const first = path.getPointAtLength(0), peak = path.getPointAtLength(180);
            const near = new DOMPoint(peak.x, first.y).matrixTransform(svg().getScreenCTM());
            svg().dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: near.x, clientY: near.y }));
            selected(timestamps[0], "0 °C");

            load("temperature", makePoints([0])); await frame();
            const single = svg().querySelector(".history-point");
            const position = new DOMPoint(Number(single.getAttribute("cx")), Number(single.getAttribute("cy"))).matrixTransform(svg().getScreenCTM());
            svg().dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: position.x, clientY: position.y }));
            selected(timestamps[0], "0 °C");
            load("temperature", []);
            check(!svg() && !tooltip() && panel().querySelector(".history-table-details").hidden, "An empty chart retained a stale selection.");
            check(exportButton.isConnected === false || panel().querySelector(".history-tools button:last-child").disabled, "An empty chart still permits CSV export.");
        }
        check(document.documentElement.scrollWidth <= innerWidth + 1, "History interactions cause horizontal page overflow.");
        check(!panel().querySelector("sample"), "Parameter labels were interpreted as HTML.");
        check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
        return { pointerValuesAndTimes: true, nearestScreenPoint: true, booleanSteps: true, keyboard: true, selectionReflowAndReload: true, singleAndEmpty: true, localizedTimezone: true, historyEligibility: true, minuteDefault: true };
    } finally {
        uiSettings.language = original.language; uiSettings.timezone = original.timezone;
        socket.receive({ type: "device_list", devices: original.devices });
        applyLanguage(); setActivePage(original.page);
    }
}

async function runHistoryPeriodPreferenceSmoke() {
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const originalPage = activePage;
    try {
        await prepareHistoryTouchSmoke();
        let range = document.querySelectorAll(".history-tools select")[1];
        range.value = "day"; range.dispatchEvent(new Event("change", { bubbles: true }));
        if (socket.sent.findLast(item => item.action === "get_parameter_history").range !== "day" || localStorage.getItem("iot-gateway-history-period-v1") !== "day") throw new Error("The explicitly selected period was not requested and saved.");
        applyLanguage();
        range = document.querySelectorAll(".history-tools select")[1];
        if (range.value !== "day") throw new Error("Changing language lost the selected history period.");
        await prepareHistoryTouchSmoke();
        if (socket.sent.findLast(item => item.action === "get_parameter_history").range !== "day") throw new Error("Reopening a boolean chart discarded the chosen period.");
        return { rememberedChoice: true, languageAndChartReopen: true };
    } finally {
        socket.receive({ type: "device_list", devices: originalDevices }); setActivePage(originalPage);
    }
}

/* The harness follows this with native DevTools touch events on the displayed SVG. */
async function prepareHistoryTouchSmoke() {
    socket.receive({ type: "device_list", devices: [{ device: "touch-history", name: "Touch LED", connected: true, parameters: { led: false },
        schema: { parameters: { led: { type: "boolean", access: "read_write", history: { enabled: true } } }, actions: {} } }] });
    setActivePage("overview");
    const select = document.querySelector(".history-tools select");
    select.value = JSON.stringify(["parameter", "touch-history", "led"]); select.dispatchEvent(new Event("change", { bubbles: true }));
    const query = socket.sent.findLast(item => item.action === "get_parameter_history");
    socket.receive({ type: "parameter_history", success: true, request_id: query.request_id, device: query.device, parameter: query.parameter,
        range: query.range, value_type: "boolean", unit: "", points: [
            { timestamp: "2026-11-20T10:00:00Z", value: 0, min: 0, max: 0, count: 1 },
            { timestamp: "2026-11-20T10:01:00Z", value: 1, min: 1, max: 1, count: 1 },
        ] });
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const svg = document.querySelector(".history-chart svg");
    svg.scrollIntoView({ block: "center" });
    const path = svg.querySelector(".history-series"), point = path.getPointAtLength(path.getTotalLength()).matrixTransform(svg.getScreenCTM());
    return { x: point.x, y: point.y };
}
