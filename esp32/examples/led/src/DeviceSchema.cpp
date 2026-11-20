#include "DeviceSchema.h"

#include "DeviceModes.h"
#include "device_config.h"

void buildDeviceSchema(JsonObject schema) {
    schema.clear();
    schema["format_version"] = 1;

    JsonObject components = schema["components"].to<JsonObject>();
    JsonObject ledComponent = components["status_led"].to<JsonObject>();
    ledComponent["label"] = "Светодиод";
    ledComponent["category"] = "actuator";
    ledComponent["description"] = "Светодиод, подключённый к выходу, указанному в профиле платы.";

    JsonObject parameters = schema["parameters"].to<JsonObject>();
    JsonObject led = parameters["led"].to<JsonObject>();
    led["label"] = "Включение";
    led["component"] = "status_led";
    led["type"] = "boolean";
    led["access"] = "read_write";
    led["control"] = "toggle";
    // Keep this command stable for saved scenarios. It enables the selected
    // mode; the output can still alternate while blinking or identifying.
    led["history"] = false;

    JsonObject output = parameters["led_output"].to<JsonObject>();
    output["label"] = "Светодиод";
    output["component"] = "status_led";
    output["description"] = "Фактическое состояние выхода: 1 — включён, 0 — выключен.";
    output["type"] = "boolean";
    output["access"] = "read_only";
    output["history"] = true;
    // Pulses belong in readings history without flooding the event journal.
    output["events"] = false;

    // Explicit semantic mapping: Gateway does not guess an off operation from
    // the parameter's ID or type. Users may override this mapping in Gateway.
    JsonObject off = schema["common_commands"]["off"].to<JsonObject>();
    off["kind"] = "parameter";
    off["name"] = "led";
    off["value"] = false;

    JsonObject actions = schema["actions"].to<JsonObject>();
#if DEVICE_HAS_IDENTIFY_INDICATOR
    schema["capabilities"]["built_in_indicator"] = true;
    schema["capabilities"]["identify"] = true;
    JsonObject identify = actions["identify"].to<JsonObject>();
    identify["label"] = "Найти устройство";
    identify["description"] = "Пять раз мигает подключённым светодиодом.";
#endif

    appendRegisteredDeviceModes(schema);
}
