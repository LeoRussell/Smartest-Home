/* The configurator must show the C3 profile, never the old S3 pin list. */
async function runC3ConfigurationSmoke() {
    const check = (value, message) => { if (!value) throw new Error(message); };
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const change = (input, value) => { input.value = String(value); input.dispatchEvent(new Event("change", { bubbles: true })); };
    const originalDevices = JSON.parse(JSON.stringify(devices));
    const pins = [0, 1, 3, 4, 5, 6, 7, 10];
    const driverIds = ["gpio_output", "digital_input", "analog_input", "pwm_output"];
    const fixture = {
        device: "smoke-c3", name: "ESP32-C3 SuperMini", connected: true, device_profile: "universal", board_profile: "esp32-c3-supermini",
        schema: { parameters: {}, actions: {}, capabilities: { universal_profile: true } }, parameters: {},
        configuration_limits: { max_components: 8, max_pwm_channels: 6, identify_gpio_pins: [0, 1, 3, 4, 5, 6, 7, 8, 10], identify_neopixel_pins: [] },
        driver_catalog: driverIds.map(id => ({ id, label: id, pins: id === "analog_input" ? [0, 1, 3, 4] : pins })),
        hardware_configuration: { format_version: 1, components: [] }
    };
    const open = device => {
        socket.receive({ type: "device_list", devices: [device] });
        openDeviceConfigurationDialog(devices[0]);
        return document.querySelector(".device-config-dialog[open]");
    };
    let dialog = open(fixture);
    const field = path => dialog.querySelector(`[data-config-path="${path}"]`);
    check([...field("identify_indicator.driver").options].map(item => item.value).join(",") === "none,gpio", "C3 incorrectly offers an S3 NeoPixel indicator.");
    change(field("identify_indicator.driver"), "gpio");
    check([...field("identify_indicator.pin").options].some(item => item.value === "8"), "GPIO8 is missing from the C3 indicator selector.");
    change(field("identify_indicator.pin"), 8); change(field("identify_indicator.active_high"), false);
    dialog.querySelector(".device-config-add-row button").click();
    let choices = [...field("components.0.pin").options].filter(item => item.value !== "").map(item => Number(item.value));
    check(JSON.stringify(choices) === JSON.stringify(pins), "C3 component pins include an unsupported or reserved S3 GPIO.");
    change(field("components.0.driver"), "analog_input");
    choices = [...field("components.0.pin").options].filter(item => item.value !== "").map(item => Number(item.value));
    check(JSON.stringify(choices) === "[0,1,3,4]", "C3 offers ADC2 or a pin outside its ADC1 profile.");
    dialog.close(); await tick();

    const sevenPwm = pins.slice(0, 7).map((pin, index) => ({
        id: `part_${index}`, parameter_id: `value_${index}`, label: `Part ${index}`, parameter_label: `Value ${index}`,
        driver: "pwm_output", pin, initial_value: 0, min_value: 0, max_value: 255,
    }));
    dialog = open({ ...fixture, hardware_configuration: { format_version: 1, components: sevenPwm } });
    check(dialog.textContent.includes("не более 6 PWM"), "The seventh C3 PWM channel was not rejected locally.");
    const sent = socket.sent.length;
    dialog.querySelector(".device-config-save").click();
    check(!socket.sent.slice(sent).some(message => message.action === "save_device_configuration"), "An invalid C3 PWM configuration was submitted for saving.");
    dialog.close(); await tick();

    // Backward-compatible S3 servers may omit configuration_limits.
    const s3 = { ...fixture, device: "smoke-s3", board_profile: "esp32-s3-devkitc-1", configuration_limits: undefined };
    dialog = open(s3);
    check([...field("identify_indicator.driver").options].some(item => item.value === "neopixel"), "Adding C3 removed the existing S3 NeoPixel option.");
    change(field("identify_indicator.driver"), "neopixel");
    check([...field("identify_indicator.pin").options].filter(item => item.value !== "").map(item => item.value).join(",") === "38,48", "S3 indicator pins changed.");
    dialog.close(); await tick();
    socket.receive({ type: "device_list", devices: originalDevices });
    check(__smokeErrors.length === 0, JSON.stringify(__smokeErrors));
    return { c3Gpio: true, c3Adc: true, c3Indicator: true, pwmLimit: true, s3Compatibility: true };
}
