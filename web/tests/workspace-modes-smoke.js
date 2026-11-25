/* Grouped firmware modes in the Overview; all Gateway traffic is mocked. */
(() => {
    const layoutKey = "iot-gateway-dashboard-v1";
    const deviceId = "workspace-modes";
    const item = (kind, id) => ({ kind, device: deviceId, id });
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    const fixture = () => ({ device: deviceId, name: "Режимы обзора", connected: true,
        parameters: { level: 12, swing_active: false, quiet_active: false, zero_status: 0 },
        schema: { parameters: {
            level: { label: "Яркость", type: "integer", access: "read_write", min: 0, max: 100 },
            selected_mode: { label: "Выбранный режим", type: "integer", access: "read_only", min: 0, max: 1, history: false },
            swing_active: { label: "Поворот", type: "boolean", access: "read_only", history: false },
            quiet_active: { label: "Тихий режим", type: "boolean", access: "read_only", history: false },
            zero_status: { label: "Нулевой режим", type: "enum", access: "read_only", options: [{ label: "Ноль", value: 0 }, { label: "Ложь", value: false }] },
        }, actions: Object.fromEntries([
            "manual_select", "blink_select", "refresh", "swing_enable", "swing_disable", "quiet_enable", "quiet_disable", "zero_enable", "zero_disable", "unsupported_enable", "unsupported_disable",
        ].map(id => [id, { label: id, arguments: {} }]).concat([
            ["arg_select", { label: "Выбор с аргументом", arguments: { level: { type: "integer", min: 0, max: 10 } } }],
        ])), modes: {
            manual: { label: "Ручной", operations: { select: "manual_select" }, status_parameter: "selected_mode", status_value: 0 },
            blink: { label: "Мигание", operations: { select: "blink_select" }, status_parameter: "selected_mode", status_value: 1 },
            swing: { label: "Поворот", operations: { enable: "swing_enable", disable: "swing_disable" }, status_parameter: "swing_active", status_value: true },
            quiet: { label: "Тихий", operations: { enable: "quiet_enable", disable: "quiet_disable" }, status_parameter: "quiet_active", status_value: false },
            zero: { label: "Нулевой", operations: { enable: "zero_enable", disable: "zero_disable" }, status_parameter: "zero_status", status_value: 0 },
            arguments: { label: "С аргументом", operations: { select: "arg_select" } },
            missing: { label: "Нет действия", operations: { select: "missing_action" } },
            unsupported: { label: "Без значения статуса", operations: { enable: "unsupported_enable", disable: "unsupported_disable" }, status_parameter: "quiet_active" },
        } },
    });
    const legacy = [item("parameter", "level"), item("action", "manual_select"), item("action", "refresh"), item("action", "blink_select"),
        item("action", "swing_disable"), item("action", "swing_enable"), item("action", "quiet_enable"), item("action", "quiet_disable"),
        item("action", "zero_disable"), item("action", "zero_enable"), item("action", "arg_select")];
    const expected = [item("parameter", "level"), item("mode-select", "select"), item("action", "refresh"),
        item("mode-toggle", "swing"), item("mode-toggle", "quiet"), item("mode-toggle", "zero"), item("action", "arg_select")];
    const tiles = () => [...document.querySelectorAll(".workspace-tile")];
    const tile = (kind, id) => tiles().find(tile => tile.dataset.tileKey === JSON.stringify([kind, deviceId, id]));
    const control = (kind, id, name) => tile(kind, id)?.querySelector(`[data-workspace-control="${name}"]`);
    const modeControls = () => document.querySelectorAll('[data-workspace-control="mode-apply"], [data-workspace-control="mode-toggle"]');
    const invokes = () => socket.sent.filter(message => message.action === "invoke_action");
    const receive = value => socket.receive({ type: "device_list", devices: [value] });
    const state = parameters => socket.receive({ type: "device_state", device: deviceId, parameters });
    const acknowledge = (request, parameters = {}) => {
        state(parameters);
        socket.receive({ type: "command_result", command_id: request.command_id, device: deviceId, action: request.action_id, success: true, status: "succeeded" });
    };
    const click = (button, actionId) => {
        check(button && !button.disabled, `Mode control is unavailable before ${actionId}.`);
        const before = invokes().length; button.click();
        const sent = invokes().slice(before);
        check(sent.length === 1 && sent[0].device === deviceId && sent[0].action_id === actionId, `Wrong mode operation for ${actionId}: ${JSON.stringify(sent)}`);
        return sent[0];
    };
    const assertLayout = () => {
        const actual = tiles().map(tile => { const [kind, device, id] = JSON.parse(tile.dataset.tileKey); return { kind, device, id }; });
        check(JSON.stringify(actual) === JSON.stringify(expected), `Legacy mode grouping lost order, duplicated operations or removed a plain action: ${JSON.stringify(actual)}`);
        const saved = JSON.parse(localStorage.getItem(layoutKey)).map(({ kind, device, id }) => ({ kind, device, id }));
        check(JSON.stringify(saved) === JSON.stringify(expected), "The grouped v1 layout was not saved for the next page load.");
    };
    window.WorkspaceModesSmoke = {
        seed() { localStorage.setItem(layoutKey, JSON.stringify(legacy)); },
        migration() {
            check(window.WebSocket.name === "SmokeSocket", "Mode tests require a mocked Gateway.");
            const before = invokes().length;
            receive(fixture()); setActivePage("overview"); assertLayout();
            check(invokes().length === before, "Migrating an old dashboard invoked an operation.");
            document.getElementById("workspace-customize").click();
            const dialog = document.querySelector(".workspace-customize-dialog[open]");
            const options = [...dialog.querySelectorAll(".workspace-choice-row")].map(row => JSON.parse(row.dataset.tileKey));
            check(options.filter(([kind, device]) => kind === "mode-select" && device === deviceId).length === 1, "Customization has more than one selector for this device.");
            check(!options.some(([kind, , id]) => kind === "action" && ["manual_select", "blink_select", "swing_enable", "swing_disable"].includes(id)), "Grouped operations remain duplicated as plain actions.");
            check(options.some(([kind, , id]) => kind === "action" && id === "arg_select") && options.some(([kind, , id]) => kind === "action" && id === "unsupported_enable"), "An action that cannot form a safe mode control disappeared.");
            dialog.close();
            return { firstPositionPreserved: true, selectDeduplicated: true, togglePairsDeduplicated: true, ordinaryActionsRetained: true, customizationChoices: true };
        },
        interactions() {
            receive(fixture()); setActivePage("overview"); assertLayout();
            let selector = control("mode-select", "select", "mode-select"), apply = control("mode-select", "select", "mode-apply");
            check(selector && apply && selector.value === "", "Unknown current mode needs an unselected placeholder.");
            const options = [...selector.options].map(option => option.value).filter(Boolean);
            check(JSON.stringify(options.sort()) === JSON.stringify(["blink", "manual"]), "The mode selector includes missing or argument-taking actions.");
            const before = invokes().length;
            selector.value = "blink"; selector.dispatchEvent(new Event("change", { bubbles: true }));
            check(invokes().length === before, "Changing a mode draft applied it without an explicit button press.");
            const selected = click(apply, "blink_select");
            check([...modeControls()].every(button => button.disabled), "A pending select operation did not block all mode controls for this device.");
            for (const button of modeControls()) button.click();
            check(invokes().length === before + 1, "A pending mode command was submitted twice.");
            acknowledge(selected, { selected_mode: 1 });

            let swing = control("mode-toggle", "swing", "mode-toggle");
            check(swing?.getAttribute("role") === "switch" && swing.getAttribute("aria-checked") === "false", "The mode toggle does not expose its inactive state accessibly.");
            const enabled = click(swing, "swing_enable");
            check(control("mode-select", "select", "mode-apply").disabled && [...modeControls()].every(button => button.disabled), "A different mode's pending operation failed to block the selector.");
            acknowledge(enabled, { swing_active: true });
            swing = control("mode-toggle", "swing", "mode-toggle");
            check(swing.getAttribute("aria-checked") === "true", "Confirmed mode state did not update its switch.");
            acknowledge(click(swing, "swing_disable"), { swing_active: false });

            const quiet = control("mode-toggle", "quiet", "mode-toggle"), zero = control("mode-toggle", "zero", "mode-toggle");
            check(quiet.getAttribute("aria-checked") === "true" && zero.getAttribute("aria-checked") === "true", "Explicit status_value=false or numeric 0 was treated as missing.");
            acknowledge(click(quiet, "quiet_disable"), { quiet_active: true });
            check(control("mode-toggle", "quiet", "mode-toggle").getAttribute("aria-checked") === "false", "A false-valued mode did not reflect its disabled state.");
            state({ zero_status: false });
            check(control("mode-toggle", "zero", "mode-toggle").getAttribute("aria-checked") === "false", "Mode state coerced false and numeric 0 instead of using Object.is.");
            acknowledge(click(control("mode-toggle", "zero", "mode-toggle"), "zero_enable"), { zero_status: 0 });
            acknowledge(click(control("mode-toggle", "zero", "mode-toggle"), "zero_disable"), { zero_status: false });

            const unknown = JSON.parse(JSON.stringify(devices.find(device => device.device === deviceId))); delete unknown.parameters.swing_active;
            receive(unknown);
            swing = control("mode-toggle", "swing", "mode-toggle");
            const unknownBefore = invokes().length; check(swing.disabled, "Unknown mode status is still actionable."); swing.click();
            check(invokes().length === unknownBefore, "An unknown mode guessed an enable/disable operation.");
            const offline = JSON.parse(JSON.stringify(unknown)); offline.connected = false; receive(offline);
            check([...modeControls()].every(button => button.disabled), "Offline mode controls remain actionable.");
            for (const button of modeControls()) button.click();
            check(invokes().length === unknownBefore, "An offline device received a mode operation.");

            receive(fixture());
            selector = control("mode-select", "select", "mode-select"); selector.value = "blink"; selector.dispatchEvent(new Event("change", { bubbles: true }));
            selector.focus();
            const relabeled = fixture(); relabeled.schema.parameters.level.label = "Новая подпись яркости";
            receive(relabeled);
            selector = control("mode-select", "select", "mode-select");
            check(selector.value === "blink" && document.activeElement === selector, "An unrelated Schema redraw lost the mode draft or keyboard focus.");
            const changed = fixture(); delete changed.schema.actions.blink_select;
            delete changed.schema.modes.quiet; delete changed.schema.actions.quiet_enable; delete changed.schema.actions.quiet_disable;
            delete changed.schema.parameters.quiet_active; changed.schema.modes.unsupported.status_parameter = "swing_active";
            receive(changed);
            selector = control("mode-select", "select", "mode-select");
            check(![...selector.options].some(option => option.value === "blink"), "A removed Schema action remained selectable.");
            const invalidToggle = control("mode-toggle", "quiet", "mode-toggle");
            check(!invalidToggle || invalidToggle.disabled, "A mode with a removed status parameter remained actionable.");
            check(invokes().length === unknownBefore, "A Schema update automatically invoked or substituted a mode action.");

            const stale = fixture(); stale.parameter_ages_seconds = { level: 0, swing_active: 120, quiet_active: 0, zero_status: 0 };
            stale.telemetry_stale_after_seconds = 45; receive(stale);
            swing = control("mode-toggle", "swing", "mode-toggle");
            check(swing.disabled, "A stale mode status still decides the next toggle operation.");
            swing.click(); check(invokes().length === unknownBefore, "A stale mode received an operation based on an old state.");
            const reconnected = fixture(); reconnected.parameter_ages_seconds = { level: 0, swing_active: null, quiet_active: 0, zero_status: 0 };
            receive(reconnected);
            check(control("mode-toggle", "swing", "mode-toggle").disabled, "A cached mode value without a reading in this connection remained actionable.");

            receive(fixture());
            acknowledge(click(control("action", "refresh", "run"), "refresh"));
            check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
            return { v1Reload: true, explicitApply: true, modeWidePending: true, switchStateAndOperations: true, falseAndZero: true, unknownAndOfflineBlocked: true, schemaChanges: true, draftAndFocus: true, staleAndReconnectBlocked: true, ordinaryAction: true };
        },
        layout() {
            receive(fixture()); setActivePage("overview");
            const cards = [tile("mode-select", "select"), tile("mode-toggle", "swing")];
            check(cards.every(Boolean), "Mode tiles are missing in the layout check.");
            for (const card of cards) {
                const box = card.getBoundingClientRect();
                check(card.scrollWidth <= card.clientWidth + 1, `A mode tile overflows at ${innerWidth}px.`);
                for (const control of card.querySelectorAll("button, select")) {
                    const bounds = control.getBoundingClientRect();
                    check(bounds.left >= box.left - 1 && bounds.right <= box.right + 1, `A mode control escapes its tile at ${innerWidth}px.`);
                }
            }
            check(document.documentElement.scrollWidth <= innerWidth + 1, `Mode controls overflow the page at ${innerWidth}px.`);
            return { width: innerWidth, language: uiSettings.language, insideTiles: true };
        },
    };
})();
