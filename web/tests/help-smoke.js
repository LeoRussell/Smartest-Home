/* Explanations must be usable with pointer, keyboard and inside modal forms. */
async function runGatewayHelpSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const frame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const sent = socket.sent.length;
    const dialog = document.createElement("dialog"); dialog.className = "management-dialog";
    const title = document.createElement("h2"); title.className = "management-dialog-title"; title.textContent = "Help smoke";
    const body = document.createElement("div"); body.className = "management-dialog-body";
    const label = document.createElement("label"); label.textContent = "Checkbox ";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox";
    const explanation = 'Plain text <img src="invalid" onerror="throw 1"> — '.repeat(5);
    const help = GatewayHelp.create(explanation, "Explanation of this checkbox");
    label.append(checkbox, help); body.append(label); dialog.append(title, body); document.body.append(dialog); dialog.showModal();
    const trigger = help.querySelector(".gateway-help-trigger"), panel = help.querySelector(".gateway-help-panel");
    try {
        trigger.focus();
        check(trigger.getAttribute("aria-expanded") === "true" && !panel.hidden, `Keyboard focus did not show help: ${JSON.stringify({ focused: document.hasFocus(), active: document.activeElement?.outerHTML, root: help.outerHTML, rectangles: trigger.getClientRects().length })}`);
        check(trigger.getAttribute("aria-describedby") === panel.id && panel.getAttribute("role") === "tooltip", "Help lost its accessible text association.");
        check(panel.textContent === explanation.trim() && !panel.querySelector("img"), "Device help was interpreted as HTML.");
        trigger.click();
        check(!checkbox.checked && trigger.getAttribute("aria-expanded") === "true", "Opening help toggled the surrounding label's checkbox.");
        await frame();
        const bounds = panel.getBoundingClientRect();
        check(bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight && bounds.width <= 361, "Help overflowed the viewport or became too wide to read.");
        GatewayHelp.update(help, "Long explanation ".repeat(400), "Long explanation"); await frame();
        const longBounds = panel.getBoundingClientRect(), anchor = trigger.getBoundingClientRect();
        check(longBounds.bottom <= anchor.top || longBounds.top >= anchor.bottom, "A long explanation covered its own dismiss button.");
        if (typeof panel.showPopover === "function") check(panel.matches(":popover-open"), "Help inside a modal is missing from the top layer.");
        const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
        trigger.dispatchEvent(escape);
        check(panel.hidden && dialog.open && escape.defaultPrevented, "Escape closed the parent modal instead of only its explanation.");
        trigger.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }));
        check(!panel.hidden, "Mouse hover did not show help.");
        GatewayHelp.update(help, "Updated explanation", "Updated label");
        check(panel.textContent === "Updated explanation" && trigger.getAttribute("aria-label") === "Updated label", "Localized explanation updates lost their label or content.");
        body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
        check(panel.hidden, "An outside press did not dismiss help.");
        trigger.click(); trigger.click();
        check(panel.hidden, "A second button activation did not dismiss pinned help.");
        trigger.click(); help.remove(); await frame();
        check(!document.querySelector(".gateway-help-panel:popover-open"), "Removing a card left its explanation in the top layer.");
        check(!socket.sent.slice(sent).some(item => ["command", "invoke_action", "run_automation", "save_device_configuration"].includes(item.action)), "Opening help sent an operation to Gateway.");
        check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    } finally {
        GatewayHelp.closeAll(); dialog.close(); dialog.remove();
    }
    return { keyboard: true, hover: true, click: true, labelSafety: true, modalEscape: true, viewport: true, textSafety: true, cleanup: true };
}
