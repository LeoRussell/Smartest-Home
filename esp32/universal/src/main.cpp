#include <Arduino.h>
#include <ArduinoJson.h>
#include <GatewayClient.h>
#include "secrets.h"
#include "UniversalDevice.h"

namespace {
constexpr uint16_t GATEWAY_PORT = 8765;
constexpr char BOARD_PROFILE[] = "esp32-s3-devkitc-1";
constexpr char FIRMWARE_VERSION[] = "1.0.0";

GatewayClient gateway;
UniversalDevice device;

void sendHello() {
    JsonDocument message;
    message["type"] = "hello";
    String helloId = "hello-";
    helloId += millis();
    message["id"] = helloId;
    message["device"] = UNIVERSAL_DEVICE_ID;
    message["name"] = UNIVERSAL_DEVICE_NAME;
    message["protocol_version"] = 2;
    message["command_dedup"] = false;
    message["device_profile"] = "universal";
    message["board_profile"] = BOARD_PROFILE;
    message["firmware_version"] = FIRMWARE_VERSION;
    JsonArray driverCatalog = message["driver_catalog"].to<JsonArray>();
    driverCatalog.add("gpio_output");
    driverCatalog.add("digital_input");
    driverCatalog.add("analog_input");
    driverCatalog.add("pwm_output");
    message["schema_version"] = device.configurationVersion() == 0
        ? 1 : device.configurationVersion();
    JsonObject schema = message["schema"].to<JsonObject>();
    device.fillSchema(schema);
    gateway.sendJson(message);
    Serial.printf("[HELLO] universal profile; stored configuration v%lu\n",
                  static_cast<unsigned long>(device.configurationVersion()));
}

void onGatewayEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            Serial.println("[GATEWAY] connected");
            sendHello();
            device.sendState(gateway);
            break;
        case WStype_TEXT: {
            JsonDocument message;
            if (deserializeJson(message, payload, length)) {
                Serial.println("[GATEWAY] ignored invalid JSON");
                break;
            }
            device.handleMessage(message, gateway);
            break;
        }
        case WStype_DISCONNECTED:
            Serial.println("[GATEWAY] disconnected");
            break;
        case WStype_ERROR:
            Serial.println("[GATEWAY] WebSocket error");
            break;
        default:
            break;
    }
}
}  // namespace

void setup() {
    Serial.begin(115200);
    delay(100);
    if (!device.begin()) {
        Serial.println("[CONFIG] Device started without applying invalid stored configuration.");
    }
    gateway.begin(
        GATEWAY_IP,
        GATEWAY_PORT,
        WIFI_SSID,
        WIFI_PASSWORD,
        onGatewayEvent
    );
}

void loop() {
    gateway.loop();
    device.loop(gateway);
}
