
#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "secrets.h"
#include "device_config.h"

const uint16_t gateway_port = 8765;

const unsigned long STATE_INTERVAL_MS = 10000;
const unsigned long WIFI_RETRY_INTERVAL_MS = 10000;

WebSocketsClient webSocket;
Preferences commandCache;
bool commandCacheReady = false;
const uint8_t COMMAND_CACHE_SLOTS = 16;
unsigned long lastStateSentAt = 0;
unsigned long lastWifiReconnectAt = 0;
uint32_t stateSequence = 0;

bool replayCachedCommandResult(const char* commandId) {
    if (!commandCacheReady || commandId == nullptr || commandId[0] == '\0') {
        return false;
    }

    for (uint8_t slot = 0; slot < COMMAND_CACHE_SLOTS; slot++) {
        String idKey = "id";
        idKey += String(slot);
        if (commandCache.getString(idKey.c_str(), "") != commandId) {
            continue;
        }

        String resultKey = "r";
        resultKey += String(slot);
        const String cachedResult = commandCache.getString(resultKey.c_str(), "");
        if (cachedResult.length() == 0) {
            return false;
        }
        webSocket.sendTXT(cachedResult);
        Serial.println("Replayed cached result for duplicate command");
        return true;
    }
    return false;
}

void rememberCommandResult(const char* commandId, const String& result) {
    if (!commandCacheReady || commandId == nullptr || commandId[0] == '\0') {
        return;
    }

    const uint8_t slot = commandCache.getUChar("next", 0) % COMMAND_CACHE_SLOTS;
    String idKey = "id";
    idKey += String(slot);
    String resultKey = "r";
    resultKey += String(slot);
    commandCache.putString(idKey.c_str(), "");
    commandCache.putString(resultKey.c_str(), result);
    commandCache.putString(idKey.c_str(), commandId);
    commandCache.putUChar("next", (slot + 1) % COMMAND_CACHE_SLOTS);
}

void sendHello() {
    JsonDocument doc;

    doc["type"] = "hello";
    doc["id"] = "hello-001";
    doc["device"] = DEVICE_ID;
    doc["name"] = DEVICE_NAME;
    doc["protocol_version"] = 2;
    doc["command_dedup"] = commandCacheReady;
    doc["firmware_version"] = "1.1.0";
    doc["schema_version"] = 2;

    JsonObject led = doc["schema"]["parameters"]["led"].to<JsonObject>();
    led["label"] = "Светодиод";
    led["type"] = "boolean";
    led["access"] = "read_write";

#if DEVICE_HAS_IDENTIFY_INDICATOR
    JsonObject identify = doc["schema"]["actions"]["identify"].to<JsonObject>();
    identify["label"] = "Найти устройство";
    identify["description"] = "Трижды мигает индикатором устройства.";
#endif

    // Оставляем прежние поля для устройств и Gateway старой версии.
    doc["data"]["model"] = DEVICE_MODEL;
    doc["data"]["firmware"] = "1.1.0";
    doc["data"]["protocol"] = 2;

    String message;
    serializeJson(doc, message);
    webSocket.sendTXT(message);
}

void sendState(bool ledOn, bool logMessage = true) {
    JsonDocument doc;

    doc["type"] = "state";
    doc["id"] = ++stateSequence;
    doc["device"] = DEVICE_ID;
    doc["data"]["parameters"]["led"] = ledOn;

    String message;
    serializeJson(doc, message);
    webSocket.sendTXT(message);

    if (logMessage) {
        Serial.print("State sent: ");
        Serial.println(message);
    }
}

void sendCommandResult(
    const char* commandId,
    const char* parameter,
    bool success,
    bool value,
    const char* errorMessage = nullptr
) {
    JsonDocument doc;

    doc["type"] = "command_result";
    doc["id"] = commandId;
    doc["command_id"] = commandId;
    doc["device"] = DEVICE_ID;
    doc["success"] = success;
    doc["data"]["parameter"] = parameter;
    doc["data"]["value"] = value;

    if (errorMessage) {
        doc["data"]["error"] = errorMessage;
    }

    String message;
    serializeJson(doc, message);
    rememberCommandResult(commandId, message);
    webSocket.sendTXT(message);

    Serial.print("Command result sent: ");
    Serial.println(message);
}

void sendActionResult(const char* commandId, const char* action, bool success) {
    JsonDocument doc;
    doc["type"] = "command_result";
    doc["id"] = commandId;
    doc["command_id"] = commandId;
    doc["device"] = DEVICE_ID;
    doc["success"] = success;
    doc["data"]["action"] = action;
    if (!success) {
        doc["data"]["error"] = "Unsupported action";
    }

    String message;
    serializeJson(doc, message);
    rememberCommandResult(commandId, message);
    webSocket.sendTXT(message);
    Serial.print("Action result sent: ");
    Serial.println(message);
}

void onWebSocketEvent(
    WStype_t type,
    uint8_t* payload,
    size_t length
) {
    switch (type) {
        case WStype_CONNECTED:
            Serial.println("WebSocket connected!");
            sendHello();
            sendState(digitalRead(LED_PIN) == HIGH);
            lastStateSentAt = millis();
            break;

        case WStype_TEXT: {
            Serial.printf("Received: %.*s\n", (int)length, (char*)payload);

            JsonDocument doc;
            DeserializationError error =
                deserializeJson(doc, payload, length);

            if (error) {
                Serial.println("Invalid JSON");
                break;
            }

            const char* typeName = doc["type"] | "";

            if (strcmp(typeName, "command") == 0) {
                const char* commandName = doc["data"]["command"] | "set";
                const char* commandId =
                    doc["data"]["command_id"] | "";

                if (commandId[0] == '\0') {
                    Serial.println("Command has no command_id");
                    break;
                }
                if (replayCachedCommandResult(commandId)) {
                    break;
                }

                if (strcmp(commandName, "invoke") == 0) {
                    const char* action = doc["data"]["action"] | "";
                    if (
                        DEVICE_HAS_IDENTIFY_INDICATOR &&
                        strcmp(action, "identify") == 0
                    ) {
                        const bool indicatorWasOn = digitalRead(IDENTIFY_PIN) == HIGH;
                        const bool ledWasOn = digitalRead(LED_PIN) == HIGH;
                        for (int i = 0; i < 3; i++) {
                            digitalWrite(IDENTIFY_PIN, HIGH);
                            delay(150);
                            digitalWrite(IDENTIFY_PIN, LOW);
                            delay(150);
                        }
                        digitalWrite(IDENTIFY_PIN, indicatorWasOn ? HIGH : LOW);
                        sendState(ledWasOn, false);
                        lastStateSentAt = millis();
                        sendActionResult(commandId, action, true);
                    } else {
                        sendActionResult(commandId, action, false);
                    }
                    break;
                }

                const char* parameter = doc["data"]["parameter"] | "";
                bool value = doc["data"]["value"] | false;
                if (
                    strcmp(commandName, "set") == 0 &&
                    strcmp(parameter, "led") == 0
                ) {
                    digitalWrite(
                        LED_PIN,
                        value ? HIGH : LOW
                    );

                    bool actualValue = digitalRead(LED_PIN) == HIGH;

                    Serial.printf(
                        "%s %s\n",
                        parameter,
                        actualValue ? "ON" : "OFF"
                    );

                    // The command result reports the actual value to the
                    // Gateway, which updates the dashboard state directly.
                    // Periodic state messages remain for telemetry and
                    // out-of-band changes.
                    lastStateSentAt = millis();
                    sendCommandResult(
                        commandId,
                        parameter,
                        actualValue == value,
                        actualValue,
                        actualValue == value
                            ? nullptr
                            : "LED output did not reach requested value"
                    );
                } else {
                    sendCommandResult(
                        commandId,
                        parameter,
                        false,
                        false,
                        "Unsupported parameter"
                    );
                }
            }

            break;
        }

        case WStype_DISCONNECTED:
            Serial.println("WebSocket disconnected");
            break;

        case WStype_ERROR:
            Serial.println("WebSocket error");
            break;

        default:
            break;
    }
}

void setup() {
    Serial.begin(9600);
    commandCacheReady = commandCache.begin("command_ack", false);
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
#if DEVICE_HAS_IDENTIFY_INDICATOR && IDENTIFY_PIN != LED_PIN
    pinMode(IDENTIFY_PIN, OUTPUT);
    digitalWrite(IDENTIFY_PIN, LOW);
#endif

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWifiReconnectAt = millis();

    Serial.println("Connecting to Wi-Fi...");

    webSocket.begin(
        GATEWAY_IP,
        gateway_port,
        "/"
    );

    webSocket.onEvent(onWebSocketEvent);
    webSocket.setReconnectInterval(5000);
}

void loop() {
    webSocket.loop();

    const unsigned long now = millis();
    if (
        WiFi.status() != WL_CONNECTED &&
        now - lastWifiReconnectAt >= WIFI_RETRY_INTERVAL_MS
    ) {
        Serial.println("Wi-Fi disconnected; reconnecting...");
        WiFi.reconnect();
        lastWifiReconnectAt = now;
    }

    if (WiFi.status() == WL_CONNECTED && lastWifiReconnectAt != 0) {
        Serial.print("Wi-Fi connected: ");
        Serial.println(WiFi.localIP());
        lastWifiReconnectAt = 0;
    }

    if (
        webSocket.isConnected() &&
        now - lastStateSentAt >= STATE_INTERVAL_MS
    ) {
        sendState(digitalRead(LED_PIN) == HIGH, false);
        lastStateSentAt = now;
    }
}
