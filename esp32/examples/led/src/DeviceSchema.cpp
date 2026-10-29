#include "DeviceSchema.h"

#include "DeviceModes.h"
#include "device_config.h"

void buildDeviceSchema(JsonObject schema) {
    schema.clear();
    schema["format_version"] = 1;

    JsonObject components = schema["components"].to<JsonObject>();
    JsonObject ledComponent = components["status_led"].to<JsonObject>();
    ledComponent["label"] = "Встроенный светодиод";
    ledComponent["category"] = "actuator";
    ledComponent["description"] = "Индикатор платы, управляемый прошивкой.";

    JsonObject parameters = schema["parameters"].to<JsonObject>();
    JsonObject led = parameters["led"].to<JsonObject>();
    led["label"] = "Светодиод";
    led["component"] = "status_led";
    led["type"] = "boolean";
    led["access"] = "read_write";
    led["control"] = "toggle";

    JsonObject actions = schema["actions"].to<JsonObject>();
#if DEVICE_HAS_IDENTIFY_INDICATOR
    schema["capabilities"]["built_in_indicator"] = true;
    schema["capabilities"]["identify"] = true;
    JsonObject identify = actions["identify"].to<JsonObject>();
    identify["label"] = "Найти устройство";
    identify["description"] = "Пять раз мигает встроенным индикатором.";
#endif

    appendRegisteredDeviceModes(schema);
}
