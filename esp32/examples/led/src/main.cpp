
#include <Arduino.h>
#include "device_config.h"
#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
#include <Adafruit_NeoPixel.h>
#endif
#include <ArduinoJson.h>
#include "secrets.h"
#include <GatewayClient.h>
#include "DeviceModes.h"
#include "LedModes.h"
#include "DeviceSchema.h"
#include <CommandResultCache.h>

const uint16_t GATEWAY_PORT = 8765;

const unsigned long STATE_INTERVAL_MS = 10000;

GatewayClient gateway;
CommandResultCache commandCache;
unsigned long lastStateSentAt = 0;
uint32_t stateSequence = 0;
bool ledState = false;
// The command remains stable during a mode; history follows the level actually
// written to the output, including temporary identify pulses.
bool ledOutputState = false;
bool ledOutputDirty = false;
bool gatewayRegistered = false;
bool identifyRunning = false;
bool identifyOn = false;
unsigned int identifyPulsesRemaining = 0;
unsigned long identifyNextAt = 0;

#if DEVICE_LED_DRIVER == DEVICE_LED_DRIVER_NEOPIXEL
Adafruit_NeoPixel onboardLed(1, LED_PIN, NEO_GRB + NEO_KHZ800);
#endif

void writeLedOutput(bool enabled) {
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
    if (ledOutputState != enabled) {
        ledOutputState = enabled;
        ledOutputDirty = true;
    }
}

void setLedOutput(bool enabled) {
    ledState = enabled;
    if (!identifyRunning) writeLedOutput(ledModeOutput(enabled));
}

void startIdentify() {
    identifyPulsesRemaining = IDENTIFY_PULSE_COUNT;
    identifyRunning = identifyPulsesRemaining > 0;
    identifyOn = identifyRunning;
    writeLedOutput(identifyRunning ? true : ledModeOutput(ledState));
    identifyNextAt = millis() + IDENTIFY_PULSE_DURATION_MS;
}

void updateIdentify() {
    if (!identifyRunning || static_cast<int32_t>(millis() - identifyNextAt) < 0) return;
    identifyOn = !identifyOn;
    if (!identifyOn && --identifyPulsesRemaining == 0) {
        identifyRunning = false;
        writeLedOutput(ledModeOutput(ledState));
        Serial.println("[IDENTIFY] complete");
        return;
    }
    writeLedOutput(identifyOn);
    identifyNextAt = millis() + IDENTIFY_PULSE_DURATION_MS;
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

void sendHello() {
    JsonDocument doc;

    doc["type"] = "hello";
    doc["id"] = "hello-001";
    doc["device"] = DEVICE_ID;
    doc["name"] = DEVICE_NAME;
    doc["protocol_version"] = 2;
    doc["command_dedup"] = commandCache.available();
    doc["command_replay_guard"] = commandCache.available();
    doc["firmware_version"] = "1.8.0";
    doc["schema_version"] = 10;
    gateway.appendHealth(doc["health"].to<JsonObject>());

    buildDeviceSchema(doc["schema"].to<JsonObject>());

    // Оставляем прежние поля для устройств и Gateway старой версии.
    doc["data"]["model"] = DEVICE_MODEL;
    doc["data"]["firmware"] = "1.8.0";
    doc["data"]["protocol"] = 2;

    gateway.sendJson(doc);
}

void sendState(bool ledOn, bool logMessage = true) {
    if (!gatewayRegistered || !gateway.isConnected()) return;
    JsonDocument doc;

    doc["type"] = "state";
    doc["id"] = ++stateSequence;
    doc["device"] = DEVICE_ID;
    gateway.appendHealth(doc["health"].to<JsonObject>());
    JsonObject parameters = doc["data"]["parameters"].to<JsonObject>();
    parameters["led"] = ledOn;
    parameters["led_output"] = ledOutputState;
    appendRegisteredDeviceModeStates(parameters);

    String message;
    serializeJson(doc, message);
    if (!gateway.sendText(message)) return;
    ledOutputDirty = false;

    if (logMessage) {
        Serial.print("State sent: ");
        Serial.println(message);
    }
}

void sendDurableResult(JsonDocument& result) {
    String outgoing;
    const bool durable = commandCache.complete(result, outgoing);
    if (!outgoing.isEmpty()) gateway.sendText(outgoing);
    if (!durable) {
        Serial.println("[COMMAND] Result could not be persisted; new commands are blocked until cache recovery.");
        sendState(ledState, false);
        lastStateSentAt = millis();
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

    sendDurableResult(doc);

    Serial.printf(
        "[COMMAND] result id=%s parameter=%s success=%s value=%s\n",
        commandId,
        parameter,
        doc["success"].as<bool>() ? "true" : "false",
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

    sendDurableResult(doc);
    Serial.printf(
        "[COMMAND] result id=%s action=%s success=%s\n",
        commandId,
        action,
        doc["success"].as<bool>() ? "true" : "false"
    );
}

bool reserveCommand(JsonDocument& request, const char* commandId) {
    String replay;
    JsonVariantConst replayOnly = request["data"]["require_cached_result"];
    const bool validReplayFlag = replayOnly.isUnbound() || replayOnly.is<bool>();
    const auto decision = validReplayFlag
        ? commandCache.reserve(commandId, replay, !replayOnly.as<bool>())
        : CommandResultCache::Decision::Rejected;
    if (decision == CommandResultCache::Decision::Execute) return true;
    if (decision == CommandResultCache::Decision::Replay) {
        gateway.sendText(replay);
        Serial.println("[COMMAND] Replayed cached acknowledgement; no action repeated.");
    } else {
        JsonDocument result;
        result["type"] = "command_result";
        result["id"] = commandId;
        result["command_id"] = commandId;
        result["device"] = DEVICE_ID;
        result["success"] = false;
        JsonObject data = result["data"].to<JsonObject>();
        if (request["data"]["command"] == "invoke") {
            data["action"] = request["data"]["action"];
        } else {
            data["parameter"] = request["data"]["parameter"];
            if (request["data"]["parameter"] == "led") data["value"] = ledState;
        }
        data["error"] = !validReplayFlag
            ? "require_cached_result must be boolean; no action was performed."
            : decision == CommandResultCache::Decision::Uncertain
                ? "Command result is unknown or no longer cached; it was not repeated. Check the current state before issuing a new command."
                : "Durable command cache is unavailable or full of unresolved commands; no action was performed. Check NVS and restart the device.";
        gateway.sendJson(result);
        Serial.printf("[COMMAND] %s\n", data["error"].as<const char*>());
    }
    sendState(ledState, false);
    lastStateSentAt = millis();
    return false;
}

void onWebSocketEvent(
    WStype_t type,
    uint8_t* payload,
    size_t length
) {
    switch (type) {
        case WStype_CONNECTED:
            Serial.println("WebSocket connected!");
            gatewayRegistered = false;
            sendHello();
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
            if (strcmp(typeName, "hello_ack") == 0) {
                gateway.setGatewayDeviceId(doc["device"]);
                gatewayRegistered = true;
                sendState(ledState);
                lastStateSentAt = millis();
                break;
            }
            if (!gatewayRegistered) break;

            if (strcmp(typeName, "command") == 0) {
                if (!gateway.targetsDevice(doc["device"], DEVICE_ID)) {
                    Serial.println("[COMMAND] Ignored command addressed to another device.");
                    break;
                }
                const char* commandName = doc["data"]["command"] | "set";
                JsonVariantConst idValue = doc["data"]["command_id"];
                if (!CommandResultCache::validCommandId(idValue)) {
                    Serial.println("[COMMAND] Missing or invalid command_id.");
                    break;
                }
                const char* commandId = idValue.as<const char*>();
                Serial.printf(
                    "[COMMAND] received id=%s type=%s\n",
                    commandId,
                    commandName
                );

                if (!reserveCommand(doc, commandId)) break;

                if (strcmp(commandName, "invoke") == 0) {
                    const char* action = doc["data"]["action"] | "";
                    if (
                        DEVICE_HAS_IDENTIFY_INDICATOR &&
                        strcmp(action, "identify") == 0
                    ) {
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
                        startIdentify();
                        sendState(ledState, false);
                        lastStateSentAt = millis();
                        sendActionResult(commandId, action, true);
                    } else {
                        String modeError;
                        const bool success = runRegisteredDeviceModeAction(
                            action, modeError
                        );
                        if (success) {
                            // The mode callback owns its policy; the physical
                            // phase remains subordinate to an active identify.
                            setLedOutput(ledState);
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
                JsonVariantConst requestedValue = doc["data"]["value"];
                if (
                    strcmp(commandName, "set") == 0 &&
                    strcmp(parameter, "led") == 0
                ) {
                    if (!requestedValue.is<bool>()) {
                        sendCommandResult(commandId, parameter, false, ledState,
                                          "LED parameter requires a boolean value");
                        break;
                    }
                    const bool value = requestedValue.as<bool>();
                    stopLedModeForManualControl();
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
                        true,
                        appliedValue
                    );
                    // Manual writes also leave blink mode. Report its status
                    // immediately instead of waiting for the 10-second heartbeat.
                    sendState(ledState, false);
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
            gatewayRegistered = false;
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
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
    // A closed or unread native USB console must not stall network processing.
    Serial.setTxTimeoutMs(1);
#endif
    if (!commandCache.begin(DEVICE_ID, "command_ack_v2", true)) {
        Serial.println("[COMMAND] Durable cache unavailable; new hardware commands will be rejected.");
    }
    initializeLedOutput();
    initializeLedModes(setLedOutput);

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
    const bool modeOutputChanged = updateLedModes(now);
    updateIdentify();
    if (modeOutputChanged && !identifyRunning) {
        writeLedOutput(ledModeOutput(ledState));
    }
    if (
        gatewayRegistered && gateway.isConnected() &&
        (ledOutputDirty || now - lastStateSentAt >= STATE_INTERVAL_MS)
    ) {
        // Send every driven edge rather than sampling a blink at heartbeat rate.
        // Unchanged logical `led` values do not restart its saved automations.
        sendState(ledState, false);
        lastStateSentAt = now;
    }
}
