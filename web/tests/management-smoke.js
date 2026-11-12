/* Delayed and out-of-order replies must never replace newer user edits. */
async function runManagementSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const input = (element, value) => { element.value = value; element.dispatchEvent(new Event("input", { bubbles: true })); };
    const change = (element, value) => { element.value = value; element.dispatchEvent(new Event("change", { bubbles: true })); };
    const latest = action => socket.sent.findLast(message => message.action === action);
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const device = { ...JSON.parse(JSON.stringify(originalDevices[0])), common_commands: { off: { kind: "parameter", name: "power", value: false } }, common_command_sources: { off: "override" } };
    const groups = [{ group_id: "room-one", name: "Комната 1" }, { group_id: "room-two", name: "Комната 2" }];
    socket.receive({ type: "device_list", devices: [device] });
    socket.receive({ type: "device_groups", groups });
    setActivePage("devices");

    document.getElementById("device-groups-manage").click();
    let groupDialog = document.querySelector(".management-dialog[open]");
    const groupField = id => groupDialog.querySelector(`input[data-group-id="${id}"]`);
    input(groupField("room-one"), "First submitted");
    input(groupField("room-two"), "Other unsaved group");
    groupField("room-one").closest(".management-group-row").querySelector("button").click();
    const rename = latest("save_device_group");
    check(rename.request_id && rename.group_id === "room-one", "Group rename has no request identity.");
    input(groupField("room-one"), "Newer first group draft");
    socket.receive({ type: "device_groups", groups });
    check([...groupDialog.querySelectorAll(".management-group-row button")].every(button => button.disabled), "A group broadcast enabled duplicate mutations.");
    socket.receive({ type: "group_update_result", request_id: "unrelated", success: true });
    check(groupField("room-one").value === "Newer first group draft" && groupField("room-two").value === "Other unsaved group", "An unrelated group reply lost drafts.");
    socket.receive({ type: "group_update_result", request_id: rename.request_id, group_id: rename.group_id, success: true });
    check(groupField("room-one").value === "Newer first group draft" && groupField("room-two").value === "Other unsaved group", "Saving one group cleared newer or unrelated group drafts.");
    groupDialog.close(); await tick();

    const openSettings = () => { DeviceManagement.openDeviceSettings(devices[0]); return document.querySelector(".device-settings-dialog[open]"); };
    let settings = openSettings();
    let name = settings.querySelector('[data-role="identity-name"]');
    input(name, "Submitted name");
    settings.querySelector(".device-identity-form").requestSubmit();
    const identity = latest("update_device_identity");
    check(identity.request_id, "Identity save has no request identity.");
    settings.close(); await tick();
    settings = openSettings(); name = settings.querySelector('[data-role="identity-name"]');
    input(name, "New dialog draft");
    socket.receive({ type: "device_identity_result", request_id: identity.request_id, success: true, device: device.device, new_device_id: device.device });
    check(name.value === "New dialog draft", "An identity reply from a closed dialog replaced new edits.");

    const membership = settings.querySelector('[data-role="device-group-membership"]');
    change(membership, "room-two");
    const assigned = latest("set_device_group");
    socket.receive({ type: "group_update_result", request_id: "old-membership", device: device.device, success: true });
    check(settings.querySelector('[data-role="device-group-membership"]').disabled, "A stale membership reply completed a newer assignment.");
    socket.receive({ type: "error", request_id: assigned.request_id, message: "Injected assignment failure" });
    check(!settings.querySelector('[data-role="device-group-membership"]').disabled && settings.querySelector(".management-feedback").textContent.includes("Injected"), "A membership error left the selection locked.");

    let mode = settings.querySelector('[data-role="common-command-mode"]');
    change(mode, "none");
    settings.querySelector(".management-mapping-form").requestSubmit();
    const mapping = latest("save_common_command");
    change(mode, "schema");
    socket.receive({ type: "common_command_save_result", request_id: "old-mapping", device: device.device, success: true });
    check(settings.querySelector('.management-mapping-form button[type="submit"]').disabled, "An old mapping reply released a pending save.");
    socket.receive({ type: "common_command_save_result", request_id: mapping.request_id, device: device.device, success: true });
    socket.receive({ type: "device_list", devices: [{ ...device, common_commands: {}, common_command_sources: { off: "disabled" } }] });
    check(settings.querySelector('[data-role="common-command-mode"]').value === "schema" && settings.textContent.includes("Новые изменения остаются"), "Mapping confirmation replaced an edit made during saving.");
    // A refreshed list replaces device objects while a dirty form deliberately
    // stays mounted. Renaming must rebind its callbacks to the current ID.
    socket.receive({ type: "device_list", devices: [{ ...device, connected: false }] });
    input(settings.querySelector('[data-role="identity-id"]'), "smoke-final-id");
    settings.querySelector(".device-identity-form").requestSubmit();
    const renamed = latest("update_device_identity");
    check(settings.querySelector('.management-mapping-form button[type="submit"]').disabled && settings.querySelector('[data-role="device-group-membership"]').disabled, "Identity saving did not serialize device mutations.");
    socket.receive({ type: "device_identity_result", request_id: renamed.request_id, success: true, device: device.device, new_device_id: "smoke-final-id" });
    socket.receive({ type: "device_list", devices: [{ ...device, device: "smoke-final-id", connected: false }] });
    settings.querySelector(".management-mapping-form").requestSubmit();
    const renamedMapping = latest("save_common_command");
    check(renamedMapping.request_id !== mapping.request_id && renamedMapping.device === "smoke-final-id", "A preserved mapping form kept sending the pre-rename ID.");
    check(settings.querySelector('[data-role="identity-save"]').disabled, "A mapping save allowed a concurrent identity mutation.");
    socket.receive({ type: "common_command_save_result", request_id: renamedMapping.request_id, device: "smoke-final-id", success: true });
    settings.close(); await tick();

    socket.receive({ type: "device_list", devices: [device] });
    DeviceManagement.executeOff(null);
    const firstBulk = latest("execute_common_command");
    socket.receive({ type: "common_command_result", request_id: firstBulk.request_id, results: [{ device: device.device, status: "succeeded" }] });
    DeviceManagement.executeOff(null);
    const secondBulk = latest("execute_common_command");
    socket.receive({ type: "common_command_result", request_id: firstBulk.request_id, results: [] });
    check(document.getElementById("common-off-all").disabled && document.getElementById("common-command-summary").textContent.includes("ожидаются"), "A previous bulk result completed a newer operation.");
    socket.receive({ type: "common_command_result", request_id: secondBulk.request_id, results: [{ device: device.device, status: "succeeded" }] });
    socket.receive({ type: "common_command_result", request_id: firstBulk.request_id, results: [] });
    check(document.getElementById("common-command-summary").textContent.includes("выполнено 1"), "An old bulk result replaced the latest completed operation.");

    setActivePage("settings");
    socket.receive({ type: "gateway_settings", event_retention_days: 0 });
    const days = document.getElementById("event-retention-days");
    input(days, "10");
    document.getElementById("event-retention-form").requestSubmit();
    const retention = latest("set_gateway_settings");
    socket.receive({ type: "gateway_settings_result", request_id: "old-retention", success: true, event_retention_days: 3 });
    check(days.disabled && days.value === "10", "An unrelated retention response completed a pending save.");
    socket.receive({ type: "error", request_id: retention.request_id, message: "Injected retention failure" });
    check(!days.disabled && days.value === "10" && !document.getElementById("event-retention-save").disabled, "A retention error lost the draft or left saving locked.");
    document.getElementById("event-retention-form").requestSubmit();
    const retry = latest("set_gateway_settings");
    check(retry.request_id !== retention.request_id, "A new settings submission reused an old request identity.");
    socket.receive({ type: "gateway_settings_result", request_id: retention.request_id, success: true, event_retention_days: 3 });
    check(days.disabled && days.value === "10", "A delayed retention reply completed its replacement request.");
    socket.receive({ type: "gateway_settings_result", request_id: retry.request_id, success: true });
    check(!days.disabled && days.value === "10" && document.getElementById("event-retention-feedback").dataset.state === "success", "A matching retention acknowledgement did not finish saving.");
    let expire;
    const originalTimer = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => {
        const timer = originalTimer(callback, delay, ...args);
        if (delay === 20000) expire = () => { clearTimeout(timer); callback(...args); };
        return timer;
    };
    try { input(days, "11"); document.getElementById("event-retention-form").requestSubmit(); }
    finally { window.setTimeout = originalTimer; }
    const timedOut = latest("set_gateway_settings");
    check(expire, "A settings save did not install a response deadline."); expire();
    check(!days.disabled && days.value === "11" && document.getElementById("event-retention-feedback").dataset.state === "warning", "A missing reply left settings locked or discarded the draft.");
    socket.receive({ type: "gateway_settings_result", request_id: timedOut.request_id, success: true, event_retention_days: 7 });
    check(days.value === "11" && !document.getElementById("event-retention-save").disabled, "An expired settings acknowledgement discarded the unsaved value.");

    socket.receive({ type: "device_list", devices: originalDevices });
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { groupDrafts: true, identityCorrelation: true, membershipErrors: true, mappingDrafts: true, renamedMapping: true, bulkCorrelation: true, retentionCorrelation: true, responseDeadline: true };
}
