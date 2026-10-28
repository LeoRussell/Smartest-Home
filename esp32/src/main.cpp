
#include <Arduino.h>
#include "device_config.h"
#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
#include <Adafruit_NeoPixel.h>
#endif
#include <ArduinoJson.h>
#include <Preferences.h>
#include "secrets.h"
#include <GatewayClient.h>
#include "DeviceModes.h"
#include "DeviceSchema.h"

const uint16_t GATEWAY_PORT = 8765;

const unsigned long STATE_INTERVAL_MS = 10000;
const uint8_t COMMAND_CACHE_SLOTS = 16;

GatewayClient gateway;
Preferences commandCache;
bool commandCacheReady = false;
unsigned long lastStateSentAt = 0;
uint32_t stateSequence = 0;
bool ledState = false;

#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
Adafruit_NeoPixel onboardLed(1, LED_PIN, NEO_GRB + NEO_KHZ800);
#endif

void setLedOutput(bool enabled) {
#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
    onboardLed.setPixelColor(
        0,
        enabled ? onboardLed.Color(255, 255, 255) : 0
    );
    onboardLed.show();
#else
    // The GPIO profile drives a plain LED connected to LED_PIN directly.
    // Keep the logical state separately; reading the output pin is not a
    // reliable way to determine whether the LED is on.
    const int inactiveLevel = LED_ACTIVE_LEVEL == HIGH ? LOW : HIGH;
    digitalWrite(LED_PIN, enabled ? LED_ACTIVE_LEVEL : inactiveLevel);
#endif
    ledState = enabled;
}

void initializeLedOutput() {
#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
    onboardLed.begin();
    onboardLed.setBrightness(LED_BRIGHTNESS);
    Serial.printf("[LED] driver=NeoPixel data_gpio=%d\n", LED_PIN);
#else
    const int inactiveLevel = LED_ACTIVE_LEVEL == HIGH ? LOW : HIGH;
    digitalWrite(LED_PIN, inactiveLevel);
    pinMode(LED_PIN, OUTPUT);
    Serial.printf(
        "[LED] driver=GPIO pin=%d active=%s\n",
        LED_PIN,
        LED_ACTIVE_LEVEL == HIGH ? "HIGH" : "LOW"
    );
#endif
    setLedOutput(false);
}

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
        String cachedResult = commandCache.getString(resultKey.c_str(), "");
        if (cachedResult.length() == 0) {
            return false;
        }

        JsonDocument cachedDocument;
        if (!deserializeJson(cachedDocument, cachedResult)) {
            const char* parameter = cachedDocument["data"]["parameter"] | "";
            JsonVariantConst cachedValue = cachedDocument["data"]["value"];
            if (
                strcmp(parameter, "led") == 0 &&
                cachedValue.is<bool>()
            ) {
                // A command retry can arrive after an ESP32 reboot. Reapply
                // the cached logical output before acknowledging the retry.
                setLedOutput(cachedValue.as<bool>());
            }
        }

        gateway.sendText(cachedResult);
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
    doc["firmware_version"] = "1.5.0";
    doc["schema_version"] = 7;

    buildDeviceSchema(doc["schema"].to<JsonObject>());

    // Оставляем прежние поля для устройств и Gateway старой версии.
    doc["data"]["model"] = DEVICE_MODEL;
    doc["data"]["firmware"] = "1.5.0";
    doc["data"]["protocol"] = 2;

    gateway.sendJson(doc);
}

void sendState(bool ledOn, bool logMessage = true) {
    JsonDocument doc;

    doc["type"] = "state";
    doc["id"] = ++stateSequence;
    doc["device"] = DEVICE_ID;
    JsonObject parameters = doc["data"]["parameters"].to<JsonObject>();
    parameters["led"] = ledOn;
    appendRegisteredDeviceModeStates(parameters);

    String message;
    serializeJson(doc, message);
    gateway.sendText(message);

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
    gateway.sendText(message);

    Serial.printf(
        "[COMMAND] result id=%s parameter=%s success=%s value=%s\n",
        commandId,
        parameter,
        success ? "true" : "false",
        value ? "true" : "false"
    );
}

void sendActionResult(
    const char* commandId,
    const char* action,
    bool success,
    const char* errorMessage = nullptr
) {
    JsonDocument doc;
    doc["type"] = "command_result";
    doc["id"] = commandId;
    doc["command_id"] = commandId;
    doc["device"] = DEVICE_ID;
    doc["success"] = success;
    doc["data"]["action"] = action;
    if (!success) {
        doc["data"]["error"] = errorMessage != nullptr
            ? errorMessage : "Unsupported action";
    }

    String message;
    serializeJson(doc, message);
    rememberCommandResult(commandId, message);
    gateway.sendText(message);
    Serial.printf(
        "[COMMAND] result id=%s action=%s success=%s\n",
        commandId,
        action,
        success ? "true" : "false"
    );
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
            sendState(ledState);
            lastStateSentAt = millis();
            break;

        case WStype_TEXT: {
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
                Serial.printf(
                    "[COMMAND] received id=%s type=%s\n",
                    commandId,
                    commandName
                );

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
                        const bool ledWasOn = ledState;
                        Serial.printf(
                            "[IDENTIFY] start driver=%s pin=%d pulses=%u duration=%lu ms\n",
#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
                            "NeoPixel",
#else
                            "GPIO",
#endif
                            LED_PIN,
                            static_cast<unsigned int>(IDENTIFY_PULSE_COUNT),
                            static_cast<unsigned long>(IDENTIFY_PULSE_DURATION_MS)
                        );
                        for (unsigned int i = 0; i < IDENTIFY_PULSE_COUNT; i++) {
                            setLedOutput(true);
                            delay(IDENTIFY_PULSE_DURATION_MS);
                            setLedOutput(false);
                            delay(IDENTIFY_PULSE_DURATION_MS);
                        }
                        setLedOutput(ledWasOn);
                        Serial.println("[IDENTIFY] complete");
                        sendState(ledWasOn, false);
                        lastStateSentAt = millis();
                        sendActionResult(commandId, action, true);
                    } else {
                        String modeError;
                        const bool success = runRegisteredDeviceModeAction(
                            action, modeError
                        );
                        if (success) {
                            sendState(ledState, false);
                            lastStateSentAt = millis();
                        }
                        sendActionResult(
                            commandId,
                            action,
                            success,
                            success ? nullptr : modeError.c_str()
                        );
                    }
                    break;
                }

                const char* parameter = doc["data"]["parameter"] | "";
                bool value = doc["data"]["value"] | false;
                if (
                    strcmp(commandName, "set") == 0 &&
                    strcmp(parameter, "led") == 0
                ) {
                    setLedOutput(value);
                    const bool appliedValue = ledState;

                    Serial.printf(
                        "%s %s\n",
                        parameter,
                        appliedValue ? "ON" : "OFF"
                    );

                    // Report the logical output state. For addressable RGB
                    // LEDs, digitalRead() cannot report the LED's light state.
                    lastStateSentAt = millis();
                    sendCommandResult(
                        commandId,
                        parameter,
                        appliedValue == value,
                        appliedValue,
                        appliedValue == value
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
    Serial.begin(115200);
    // Keep acknowledgements from older firmware behavior out of this cache.
    commandCacheReady = commandCache.begin("command_ack_v2", false);
    initializeLedOutput();

    gateway.begin(
        GATEWAY_IP,
        GATEWAY_PORT,
        WIFI_SSID,
        WIFI_PASSWORD,
        onWebSocketEvent
    );
}

void loop() {
    gateway.loop();
    const unsigned long now = millis();
    if (
        gateway.isConnected() &&
        now - lastStateSentAt >= STATE_INTERVAL_MS
    ) {
        sendState(ledState, false);
        lastStateSentAt = now;
    }
}
