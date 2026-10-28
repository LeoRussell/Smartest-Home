#include <Arduino.h>
#include <ArduinoJson.h>
#include <GatewayClient.h>
#include "secrets.h"

namespace {
constexpr char DEVICE_ID[] = "esp32-advanced-01";
constexpr char DEVICE_NAME[] = "Пользовательский ESP32";
constexpr uint16_t GATEWAY_PORT = 8765;
constexpr unsigned long STATE_INTERVAL_MS = 10000;

#ifndef LED_PIN
#define LED_PIN 48
#endif

GatewayClient gateway;
bool ledState = false;
uint32_t stateSequence = 0;
unsigned long lastStateSentAt = 0;

void sendHello() {
    JsonDocument message;
    message["type"] = "hello";
    message["device"] = DEVICE_ID;
    message["name"] = DEVICE_NAME;
    message["protocol_version"] = 2;
    message["firmware_version"] = "1.0.0";
    message["schema_version"] = 1;
    // This example does not persist command results, so Gateway must not retry.
    message["command_dedup"] = false;

    JsonObject schema = message["schema"].to<JsonObject>();
    schema["format_version"] = 1;
    JsonObject led = schema["parameters"]["led"].to<JsonObject>();
    led["label"] = "Светодиод";
    led["type"] = "boolean";
    led["access"] = "read_write";
    led["control"] = "toggle";
    schema["actions"].to<JsonObject>();

    gateway.sendJson(message);
}

void sendState() {
    JsonDocument message;
    message["type"] = "state";
    message["id"] = ++stateSequence;
    message["device"] = DEVICE_ID;
    message["data"]["parameters"]["led"] = ledState;
    gateway.sendJson(message);
    lastStateSentAt = millis();
}

void sendCommandResult(
    const char* commandId,
    bool success,
    bool value,
    const char* error = nullptr
) {
    JsonDocument message;
    message["type"] = "command_result";
    message["id"] = commandId;
    message["command_id"] = commandId;
    message["device"] = DEVICE_ID;
    message["success"] = success;
    message["data"]["parameter"] = "led";
    message["data"]["value"] = value;
    if (error != nullptr) message["data"]["error"] = error;
    gateway.sendJson(message);
}

void handleCommand(uint8_t* payload, size_t length) {
    JsonDocument command;
    if (deserializeJson(command, payload, length)) return;
    const char* messageType = command["type"] | "";
    if (strcmp(messageType, "command") != 0) return;

    const char* commandId = command["data"]["command_id"] | "";
    const char* commandName = command["data"]["command"] | "";
    const char* parameter = command["data"]["parameter"] | "";
    JsonVariantConst requestedValue = command["data"]["value"];
    if (commandId[0] == '\0') return;

    if (
        strcmp(commandName, "set") != 0 ||
        strcmp(parameter, "led") != 0 ||
        !requestedValue.is<bool>()
    ) {
        sendCommandResult(commandId, false, ledState, "Unsupported command");
        return;
    }

    ledState = requestedValue.as<bool>();
    digitalWrite(LED_PIN, ledState ? HIGH : LOW);
    sendState();
    sendCommandResult(commandId, true, ledState);
}

void onGatewayEvent(WStype_t type, uint8_t* payload, size_t length) {
    if (type == WStype_CONNECTED) {
        Serial.println("Gateway connected");
        sendHello();
        sendState();
    } else if (type == WStype_TEXT) {
        handleCommand(payload, length);
    } else if (type == WStype_DISCONNECTED) {
        Serial.println("Gateway disconnected");
    }
}
}  // namespace

void setup() {
    Serial.begin(115200);
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
    gateway.begin(GATEWAY_IP, GATEWAY_PORT, WIFI_SSID, WIFI_PASSWORD, onGatewayEvent);
}

void loop() {
    gateway.loop();
    const unsigned long now = millis();
    if (
        gateway.isConnected() &&
        now - lastStateSentAt >= STATE_INTERVAL_MS
    ) {
        sendState();
    }
}
