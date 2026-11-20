/* DS18B20 is a firmware capability, not an ADC preset with a temperature label. */
async function runDS18B20ConfigurationSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const wait = (milliseconds = 0) => new Promise(resolve => setTimeout(resolve, milliseconds));
    const edit = (input, value) => {
        input.value = String(value);
        input.dispatchEvent(new Event(input.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
    };
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const pins = [0, 1, 3, 4, 5, 6, 7, 10];
    const oldDrivers = ["gpio_output", "digital_input", "analog_input", "pwm_output"];
    const fixture = {
        device: "smoke-temperature", name: "DS18B20 C3", connected: true,
        device_profile: "universal", board_profile: "esp32-c3-supermini",
        schema: { parameters: {}, actions: {}, capabilities: { universal_profile: true } }, parameters: {},
        configuration_limits: { max_components: 8, max_pwm_channels: 6, identify_gpio_pins: [...pins, 8], identify_neopixel_pins: [] },
        hardware_configuration: { format_version: 1, components: [] },
        driver_catalog: oldDrivers.map(id => ({ id, label: id, pins: id === "analog_input" ? [0, 1, 3, 4] : pins })),
    };
    let dialog;
    const field = path => dialog.querySelector(`[data-config-path="${path}"]`);
    const open = device => {
        socket.receive({ type: "device_list", devices: [device] });
        openDeviceConfigurationDialog(devices[0]);
        dialog = document.querySelector(".device-config-dialog[open]");
    };
    const close = async () => { dialog.close(); await wait(); };
    const preset = () => dialog.querySelector(".device-config-preset select");
    const latestValidation = () => socket.sent.findLast(message => message.action === "validate_device_configuration" && message.device === fixture.device);
    const importConfiguration = async configuration => {
        const input = dialog.querySelector('.device-config-import input[type="file"]');
        const transfer = new DataTransfer();
        transfer.items.add(new File([JSON.stringify(configuration)], "temperature.json", { type: "application/json" }));
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await wait(30);
    };
    try {
        open(fixture);
        check(![...preset().options].some(option => option.value === "temperature"), "Old firmware offers a DS18B20 preset it cannot run.");
        dialog.querySelector(".device-config-add-row button").click();
        check(![...field("components.0.driver").options].some(option => option.value === "ds18b20"), "Old firmware offers the DS18B20 driver.");
        await close();

        const capable = { ...fixture, driver_catalog: [...fixture.driver_catalog, {
            id: "ds18b20", label: "DS18B20", pins, parameter_type: "number", access: "read_only",
        }] };
        open(capable);
        edit(preset(), "temperature");
        dialog.querySelector(".device-config-add-row button").click();
        check(field("components.0.driver").value === "ds18b20", "The temperature preset did not choose the advertised DS18B20 driver.");
        edit(field("components.0.pin"), 4);
        const interval = field("components.0.interval_ms");
        check(interval.value === "2000" && interval.min === "1000" && interval.max === "3600000", "DS18B20 measurement intervals are inconsistent with the firmware.");
        check(field("components.0.unit").value === "°C" && field("components.0.unit").readOnly, "DS18B20 unit is not fixed to °C.");
        check(!field("components.0.raw_min") && !field("components.0.initial_value") && !field("components.0.pull"), "DS18B20 still offers ADC, output or pull-up settings.");
        check(dialog.querySelector(".device-config-preview").textContent.includes("−55…125 °C"), "DS18B20 preview is not a read-only temperature range.");
        check(!dialog.querySelector('.device-config-preview-item input'), "A read-only DS18B20 preview contains an actuator control.");
        check(dialog.scrollWidth <= dialog.clientWidth + 1, "The DS18B20 constructor overflows its viewport.");
        const help = dialog.querySelector(".device-config-component legend .gateway-help-trigger");
        help.click();
        const helpText = document.getElementById(help.getAttribute("aria-controls")).textContent;
        check(helpText.includes("1-Wire") && helpText.includes("3.3 V") && helpText.includes("4.7"), "DS18B20 wiring information lost the bus, supply or external pull-up.");
        GatewayHelp.closeAll();
        edit(interval, 999);
        check(interval.getAttribute("aria-invalid") === "true", "A DS18B20 interval under 1000 ms was accepted locally.");
        const beforeInvalidSave = socket.sent.length;
        dialog.querySelector(".device-config-save").click();
        check(!socket.sent.slice(beforeInvalidSave).some(message => message.action === "save_device_configuration"), "Invalid DS18B20 polling was submitted for saving.");
        edit(interval, 2000);
        await wait(400);
        const presetConfiguration = latestValidation().configuration.components[0];
        check(presetConfiguration.driver === "ds18b20" && presetConfiguration.pin === 4 && presetConfiguration.unit === "°C", "DS18B20 validation request lost its driver, GPIO4 or unit.");
        await close();

        const oldComponent = {
            id: "temperature_sensor", parameter_id: "temp_value", label: "Мой датчик", parameter_label: "Температура комнаты",
            driver: "analog_input", pin: 4, interval_ms: 500, unit: "raw", raw_min: 0, raw_max: 4095, value_min: 0, value_max: 100,
        };
        open({ ...capable, hardware_configuration: { format_version: 1, components: [oldComponent] } });
        edit(field("components.0.driver"), "ds18b20");
        check(field("components.0.id").value === oldComponent.id && field("components.0.parameter_id").value === oldComponent.parameter_id,
            "Changing ADC to DS18B20 broke stable component or parameter IDs.");
        check(field("components.0.label").value === oldComponent.label && field("components.0.parameter_label").value === oldComponent.parameter_label,
            "Changing the temperature driver discarded user labels.");
        check(field("components.0.pin").value === "4", "Changing to DS18B20 unexpectedly moved its GPIO.");
        await wait(400);
        const request = latestValidation();
        const configuration = request.configuration;
        const expectedKeys = "driver,id,interval_ms,label,parameter_id,parameter_label,pin,unit";
        check(Object.keys(configuration.components[0]).sort().join(",") === expectedKeys, "DS18B20 configuration retained obsolete ADC or output fields.");
        check(Object.keys(request.ui_preferences).length === 0, "A read-only DS18B20 retained an actuator display preference.");

        let exported;
        const createUrl = URL.createObjectURL;
        const click = HTMLAnchorElement.prototype.click;
        try {
            URL.createObjectURL = blob => { exported = blob; return "blob:smoke-ds18b20"; };
            HTMLAnchorElement.prototype.click = function () { if (!this.href.startsWith("blob:")) click.call(this); };
            dialog.querySelector(".device-config-toolbar button").click();
            const documentExport = JSON.parse(await exported.text());
            check(JSON.stringify(documentExport.hardware_configuration) === JSON.stringify(configuration), "DS18B20 export differs from the validated hardware document.");
        } finally {
            URL.createObjectURL = createUrl;
            HTMLAnchorElement.prototype.click = click;
        }
        socket.receive({ type: "device_configuration_validation", device: fixture.device, request_id: request.request_id, success: true, errors: [] });
        dialog.querySelector(".device-config-save").click();
        const save = socket.sent.findLast(message => message.action === "save_device_configuration" && message.device === fixture.device);
        check(save && JSON.stringify(save.configuration) === JSON.stringify(configuration), "Saving DS18B20 changed the verified hardware document.");
        await close();

        open(capable);
        const imported = { ...oldComponent, driver: "ds18b20", interval_ms: 2000, unit: "°F" };
        for (const key of ["raw_min", "raw_max", "value_min", "value_max"]) delete imported[key];
        await importConfiguration({ format_version: 1, components: [imported] });
        check(field("components.0.unit").getAttribute("aria-invalid") === "true", "Import silently accepted an unsupported DS18B20 unit.");
        delete imported.unit;
        delete imported.interval_ms;
        await importConfiguration({ format_version: 1, components: [imported] });
        check(field("components.0.unit").value === "°C" && field("components.0.interval_ms").value === "2000", "Importing a DS18B20 document without optional defaults produced an unusable draft.");
        await close();
        check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
        return { firmwareGating: true, gpio4Preset: true, fixedUnit: true, asyncInterval: true, readonlyPreview: true, driverMigration: true, cleanExportAndSave: true, importDefaultsAndValidation: true };
    } finally {
        if (dialog?.open) await close();
        socket.receive({ type: "device_list", devices: originalDevices });
    }
}
