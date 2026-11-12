/* Executed only by browser_smoke.py, which replaces WebSocket before page load. */
async function runGatewaySmoke() {
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    check(window.WebSocket.name === "SmokeSocket", "A real Gateway must never be contacted by this test.");
    const now = new Date().toISOString();
    const device = { device: "smoke-lamp", name: "Тестовая лампа", connected: true, last_seen: now,
        health: { wifi_rssi: -65, uptime_seconds: 120, updated_at: now },
        parameters: { power: false, temperature: 23.4 }, schema: { parameters: {
            power: { type: "boolean", access: "read_write", label: "Питание" },
            temperature: { type: "number", access: "read_only", label: "Температура", unit: "°C" }
        }, actions: {} } };
    socket.receive({ type: "gateway_capabilities", features: ["parameter_history", "connection_history", "automation_simulation", "automation_progress"] });
    socket.receive({ type: "device_list", devices: [device] });
    setActivePage("devices");
    const history = document.querySelector('[data-role="connection-history"]');
    check(history?.querySelector("svg") && history.getAttribute("aria-label"), "Labelled connection icon is missing.");
    history.click();
    const request = socket.sent.findLast(item => item.action === "get_device_connections");
    check(request?.device === device.device, "Connection history requested the wrong device.");
    socket.receive({ type: "device_connections", success: true, request_id: request.request_id, device: device.device,
        sessions: [], has_more: false, summary: { session_count: 0, total_connected_seconds: 0 } });
    document.querySelector(".connection-history-dialog[open]").close();
    await new Promise(resolve => setTimeout(resolve, 0));
    const chart = document.querySelector('[data-role="parameter-history"][data-parameter-id="temperature"]');
    check(chart?.querySelector("svg"), "Chart icon is missing."); chart.click();
    const query = socket.sent.findLast(item => item.action === "get_parameter_history");
    check(activePage === "overview" && query.device === device.device && query.parameter === "temperature", "Chart navigation lost its parameter.");
    socket.receive({ type: "parameter_history", success: true, request_id: query.request_id, device: device.device,
        parameter: "temperature", range: query.range, value_type: "number", unit: "°C", recording_enabled: true,
        points: [{ timestamp: now, value: 23.4, min: 23.4, max: 23.4, count: 1 }] });
    check(document.querySelectorAll(".history-table-scroll tbody tr").length === 1, "Accessible history table is missing.");
    socket.receive({ type: "device_identity_result", success: true, device: "smoke-lamp", new_device_id: "smoke-renamed" });
    const renamedQuery = socket.sent.findLast(item => item.action === "get_parameter_history");
    check(renamedQuery.device === "smoke-renamed" && renamedQuery.parameter === "temperature", "Renaming a device changed the selected graph.");
    const before = socket.sent.length;
    document.querySelector(".workspace-toggle").click(); document.querySelector(".workspace-toggle").click();
    check(socket.sent.slice(before).filter(item => item.action === "command").length === 1, "A pending command was sent twice.");
    const sentCommand = socket.sent.slice(before).find(item => item.action === "command");
    socket.receive({ type: "command_result", device: sentCommand.device, command_id: sentCommand.command_id,
        parameter: sentCommand.parameter, value: false, success: false, status: "failed",
        error: "Execution may have occurred; check the current state" });
    check(lastActivityMessage.includes("сообщило об ошибке команды") && devices[0].parameters.power === false,
        "An uncertain result was presented as proof that the action never happened.");
    setActivePage("automations");
    const workspace = { blocks: { languageVersion: 0, blocks: [{ type: "iot_when_manual", id: "smoke-root",
        fields: { DASHBOARD: "TRUE" }, next: { block: { type: "iot_wait_seconds", id: "smoke-wait", fields: { SECONDS: 5 } } } }] } };
    AutomationBlockly.load(workspace);
    const previous = JSON.stringify(AutomationBlockly.getState());
    try { AutomationBlockly.load({ blocks: { blocks: [{ type: "nonexistent_smoke_block" }] } }); } catch { /* Expected. */ }
    check(JSON.stringify(AutomationBlockly.getState()) === previous, "A failed block load destroyed the current editor.");
    const scenario = { automation_id: "smoke-scene", enabled: true, run_status: "idle",
        config: { version: 2, name: "Тестовый сценарий", workspace: AutomationBlockly.getState(), program: AutomationBlockly.compile() } };
    socket.receive({ type: "automation_list", automations: [scenario] });
    const count = socket.sent.length;
    document.querySelector('[data-role="copy-automation"]').click();
    check(editingAutomationId == null && document.getElementById("automation-name").value !== scenario.config.name, "Scenario copying overwrote the original identity.");
    check(socket.sent.length === count, "Copying unexpectedly sent a Gateway request.");
    document.getElementById("automation-simulate-draft").click();
    const dialog = document.querySelector(".automation-simulator-dialog[open]");
    check(dialog, "Draft simulation is missing."); dialog.querySelector("form").requestSubmit();
    const simulation = socket.sent.findLast(item => item.action === "simulate_automation");
    check(simulation?.config?.program?.body[0].seconds === 5, "Simulation used the wrong draft.");
    check(!socket.sent.slice(count).some(item => ["command", "run_automation", "invoke_action", "save_automation"].includes(item.action)), "A simulation or copy caused a real operation.");
    dialog.close(); await new Promise(resolve => setTimeout(resolve, 0));
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const originalWorkspace = AutomationBlockly.getState();
    const filterDevice = definition => ({ device: "smoke-filter", name: "Filter device", connected: true,
        parameters: {}, schema: { parameters: { reading: { access: "read_only", label: "Reading", ...definition } }, actions: {} } });
    const filterWorkspace = raw => ({ blocks: { languageVersion: 0, blocks: [{ type: "iot_when_parameter_changes", id: "smoke-filter-root",
        fields: { DEVICE: "smoke-filter", PARAMETER: "reading", MATCH: "equals", VALUE: raw },
        next: { block: { type: "iot_wait_seconds", id: "smoke-filter-wait", fields: { SECONDS: 1 } } } }] } });
    const rejectsFilter = () => { try { AutomationBlockly.compile(); return false; } catch { return true; } };
    const enumDefinition = values => ({ type: "enum", options: values.map(value => ({ label: value, value })) });
    socket.receive({ type: "device_list", devices: [filterDevice(enumDefinition(["a", "b"]))] });
    AutomationBlockly.load(filterWorkspace('"b"'));
    check(AutomationBlockly.compile().trigger.value === "b", "The enum filter did not load its original value.");
    socket.receive({ type: "device_list", devices: [filterDevice(enumDefinition(["a", "c"]))] });
    check(AutomationBlockly.getState().blocks.blocks[0].fields.VALUE === '"b"' && rejectsFilter(), "A removed enum filter value silently changed the scenario.");
    check(document.getElementById("automation-workspace").textContent.includes("недоступно"), "The unavailable trigger value is not marked.");
    socket.receive({ type: "device_list", devices: [filterDevice({ type: "number", min: 0, max: 100 })] });
    AutomationBlockly.load(filterWorkspace(10.12345));
    check(AutomationBlockly.compile().trigger.value === 10.12345, "An unrestricted numeric filter was rounded.");
    socket.receive({ type: "device_list", devices: [filterDevice({ type: "number", min: 20, max: 100 })] });
    check(AutomationBlockly.getState().blocks.blocks[0].fields.VALUE === 10.12345 && rejectsFilter(), "A narrowed numeric range clamped the saved filter.");
    AutomationBlockly.load(filterWorkspace(25));
    check(AutomationBlockly.compile().trigger.value === 25, "An explicitly corrected numeric filter remained blocked.");
    AutomationBlockly.load({ blocks: { languageVersion: 0, blocks: [{ type: "iot_when_manual", id: "smoke-compare-root",
        next: { block: { type: "controls_if", id: "smoke-compare-if", inputs: {
            IF0: { block: { type: "iot_parameter_compare", id: "smoke-compare", fields: { DEVICE: "smoke-filter", PARAMETER: "reading", OPERATOR: "lt" },
                inputs: { RIGHT: { block: { type: "math_number", id: "smoke-compare-number", fields: { NUM: 30 } } } } } },
            DO0: { block: { type: "iot_wait_seconds", id: "smoke-compare-wait", fields: { SECONDS: 1 } } }
        } } } }] } });
    check(AutomationBlockly.compile().body[0].branches[0].condition.operator === "lt", "The numeric comparison did not load.");
    socket.receive({ type: "device_list", devices: [filterDevice(enumDefinition([20, 30]))] });
    const comparison = AutomationBlockly.getState().blocks.blocks[0].next.block.inputs.IF0.block;
    check(comparison.fields.OPERATOR === "lt" && rejectsFilter(), "A no longer supported comparison silently changed to equality.");
    const actionArguments = {
        level: { type: "integer", label: "Level", min: 0, max: 100 },
        mode: { type: "enum", label: "Mode", options: [{ label: "Night", value: "night" }, { label: "Day", value: "day" }] }
    };
    const actionDevice = args => ({ device: "smoke-action-device", name: "Action device", connected: true, parameters: {}, schema: {
        parameters: {}, actions: { run: { label: "Run", arguments: args }, other: { label: "Other", arguments: { level: { type: "integer", label: "Level", required: false } } } }
    } });
    const actionWorkspace = { blocks: { languageVersion: 0, blocks: [{ type: "iot_when_manual", id: "smoke-action-root",
        next: { block: { type: "iot_invoke_action", id: "smoke-action", fields: { DEVICE: "smoke-action-device", ACTION: "run", ARG_level: "37", ARG_mode: '"night"' } } } }] } };
    const actionState = () => AutomationBlockly.getState().blocks.blocks[0].next.block;
    const actionBlock = () => Blockly.getMainWorkspace().getBlockById("smoke-action");
    socket.receive({ type: "device_list", devices: [actionDevice(actionArguments)] }); AutomationBlockly.load(actionWorkspace);
    check(AutomationBlockly.compile().body[0].arguments.mode === "night", "Action arguments did not load.");
    socket.receive({ type: "device_list", devices: [actionDevice({ ...actionArguments, mode: { ...actionArguments.mode, options: [{ label: "Day", value: "day" }] } })] });
    check(actionState().fields.ARG_mode === '"night"' && rejectsFilter(), "A removed enum argument option was changed or accepted.");
    socket.receive({ type: "device_list", devices: [actionDevice({ ...actionArguments, level: { ...actionArguments.level, max: 20 } })] });
    check(actionState().fields.ARG_level === "37" && rejectsFilter(), "A narrowed argument range was changed or accepted.");
    socket.receive({ type: "device_list", devices: [actionDevice({ level: actionArguments.level })] });
    check(actionState().fields.ARG_mode === '"night"' && actionState().fields.DISCARD_REMOVED_ARGS === false && rejectsFilter(), `A removed action argument was silently omitted: ${JSON.stringify(actionState())}`);
    const orphanDraft = AutomationBlockly.getState(); AutomationBlockly.load(orphanDraft);
    check(actionState().fields.ARG_mode === '"night"' && rejectsFilter(), "Serialized/local action recovery lost an unavailable argument.");
    socket.receive({ type: "device_list", devices: [] });
    check(actionState().fields.ARG_mode === '"night"' && actionState().fields.ARG_level === "37" && !Object.hasOwn(actionState().fields, "DISCARD_REMOVED_ARGS"), "Missing device arguments were discarded or offered for deletion.");
    socket.receive({ type: "device_list", devices: [actionDevice(actionArguments)] });
    check(AutomationBlockly.compile().body[0].arguments.mode === "night" && AutomationBlockly.compile().body[0].arguments.level === 37, "Returning Schema failed to restore original arguments.");
    socket.receive({ type: "device_list", devices: [actionDevice({ level: actionArguments.level })] });
    actionBlock().setFieldValue("TRUE", "DISCARD_REMOVED_ARGS"); await new Promise(resolve => setTimeout(resolve, 80));
    check(!Object.hasOwn(actionState().fields, "ARG_mode") && AutomationBlockly.compile().body[0].arguments.level === 37, "Explicitly removing unavailable arguments did not produce a valid action.");
    AutomationBlockly.undo(); await new Promise(resolve => setTimeout(resolve, 80));
    check(actionState().fields.ARG_mode === '"night"' && rejectsFilter(), "Undo did not restore the removed argument.");
    AutomationBlockly.redo(); await new Promise(resolve => setTimeout(resolve, 80));
    check(!Object.hasOwn(actionState().fields, "ARG_mode") && AutomationBlockly.compile().body[0].arguments.level === 37, "Redo did not remove the obsolete argument again.");
    socket.receive({ type: "device_list", devices: [actionDevice(actionArguments)] }); AutomationBlockly.load(actionWorkspace);
    actionBlock().setFieldValue("other", "ACTION"); await new Promise(resolve => setTimeout(resolve, 80));
    check(AutomationBlockly.compile().body[0].action === "other" && !Object.hasOwn(AutomationBlockly.compile().body[0].arguments, "level"), "Choosing another action leaked an old argument with the same name.");
    socket.receive({ type: "device_list", devices: originalDevices }); AutomationBlockly.load(originalWorkspace);
    const normalize = GatewayConnection.normalize;
    check(normalize("http://127.0.0.1:18765") === "ws://127.0.0.1:18765/", "Gateway URL normalization is inconsistent.");
    for (const address of ["file:///etc/passwd", "ws://user:password@localhost:8765", "ws://localhost:0", "ws://localhost:8765/path", "ws://localhost:8765/#fragment", "ws://localhost:8765/#", "ws://localhost:8765/?"]) {
        let rejected = false; try { normalize(address); } catch { rejected = true; }
        check(rejected, `Invalid Gateway address was accepted: ${address}`);
    }
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { diagnostics: true, charts: true, commandDeduplication: true, blockRollback: true, copy: true, simulation: true, filterPreservation: true, argumentPreservation: true, connectionSettings: true };
}
