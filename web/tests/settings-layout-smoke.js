/* Settings layout follows the logical reading order at desktop and mobile. */
async function runSettingsLayoutSmoke() {
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    const nextFrame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const originals = JSON.parse(JSON.stringify(devices));
    const fixture = {
        device: "settings-layout-lamp", name: "Лампа в гостиной", connected: true,
        display_name: "Лампа в гостиной", group_id: "settings-room",
        health: { wifi_rssi: -62, uptime_seconds: 300 },
        parameters: { power: false }, schema: { parameters: {
            power: { label: "Питание", type: "boolean", access: "read_write" }
        }, actions: {} },
        common_commands: { off: { kind: "parameter", name: "power", value: false } },
        common_command_sources: { off: "override" },
    };
    socket.receive({ type: "device_list", devices: [fixture] });
    socket.receive({ type: "device_groups", groups: [{ group_id: "settings-room", name: "Гостиная" }] });
    setActivePage("devices");
    DeviceManagement.openDeviceSettings(devices[0]);
    const dialog = document.querySelector(".device-settings-dialog[open]");
    check(dialog, "Device settings did not open.");
    await nextFrame();
    const group = dialog.querySelector('[data-role="device-settings-group"]');
    const connections = dialog.querySelector('[data-role="device-settings-connections"]');
    const common = dialog.querySelector('[data-role="device-settings-common"]');
    check(group.parentElement === connections.parentElement && group.parentElement.classList.contains("device-settings-sidebar"), "Group and connections do not share a sidebar.");
    check(group.compareDocumentPosition(connections) & Node.DOCUMENT_POSITION_FOLLOWING, "Connections are not below membership in the reading order.");
    const boxes = [group, connections, common].map(element => element.getBoundingClientRect());
    if (innerWidth > 650) {
        check(Math.abs(boxes[0].left - boxes[1].left) <= 1 && boxes[1].top >= boxes[0].bottom, "Desktop connections are not directly below membership.");
        check(boxes[2].left > boxes[0].right && Math.abs(boxes[2].top - boxes[0].top) <= 1, "Common off is not aligned to the right of the sidebar.");
    } else {
        check(boxes[1].top >= boxes[0].bottom && boxes[2].top >= boxes[1].bottom, "Mobile settings do not follow group, connections, common off.");
    }
    check(dialog.scrollWidth <= dialog.clientWidth + 1, "Device settings overflow horizontally.");
    check(dialog.querySelector('[data-role="identity-id"]').disabled, "A connected device unexpectedly allows changing its ID.");
    check(dialog.querySelector(".device-identity-footer .management-help").textContent.includes("ID"), "The critical ID restriction disappeared into a tooltip.");
    check(dialog.querySelector('[data-role="identity-save"] svg') && dialog.querySelector(".device-settings-history-link svg"), "Settings action icons are missing.");
    check(dialog.querySelectorAll(".gateway-help-trigger").length >= 3, "Settings explanations were not moved into help controls.");
    const name = dialog.querySelector('[data-role="identity-name"]');
    name.value = "Несохранённое имя"; name.dispatchEvent(new Event("input", { bubbles: true }));
    const help = dialog.querySelector(".device-settings-id-field .gateway-help-trigger");
    help.click();
    check(help.getAttribute("aria-expanded") === "true", "ID help did not open by click.");
    help.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await tick();
    check(dialog.open && name.value === "Несохранённое имя", "Dismissing help closed the settings dialog or lost its draft.");
    dialog.close(); await tick();
    socket.receive({ type: "device_list", devices: originals });
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { sidebar: true, identityFooter: true, helpPreservesDraft: true, icons: true };
}
