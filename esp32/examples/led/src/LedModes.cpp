#include "LedModes.h"

namespace {
constexpr uint32_t BLINK_INTERVAL_MS = 500;
LedModeOutputHandler outputHandler = nullptr;
bool blinking = false;
bool blinkPhase = false;
uint32_t lastPhaseAt = 0;
}  // namespace

void initializeLedModes(LedModeOutputHandler setLogicalOutput) {
    outputHandler = setLogicalOutput;
    blinking = false;
    blinkPhase = false;
    lastPhaseAt = millis();
}

void stopLedModeForManualControl() {
    blinking = false;
    blinkPhase = false;
}

bool selectManualLedMode(String&) {
    // Selecting manual keeps the requested on/off state. The application
    // reapplies that logical value after this callback stops the mode.
    stopLedModeForManualControl();
    return true;
}

bool enableBlinkLedMode(String& error) {
    if (outputHandler == nullptr) {
        error = "LED mode output is not initialized";
        return false;
    }
    if (blinking) return true;
    blinking = true;
    blinkPhase = true;
    lastPhaseAt = millis();
    outputHandler(true);
    return true;
}

bool disableBlinkLedMode(String&) {
    if (!blinking) return true;
    stopLedModeForManualControl();
    if (outputHandler != nullptr) outputHandler(false);
    return true;
}

bool manualLedModeIsActive() { return !blinking; }
bool blinkLedModeIsActive() { return blinking; }

bool ledModeOutput(bool logicalOn) {
    return logicalOn && (!blinking || blinkPhase);
}

bool updateLedModes(uint32_t now) {
    if (!blinking) return false;
    const uint32_t elapsed = now - lastPhaseAt;
    if (elapsed < BLINK_INTERVAL_MS) return false;
    const uint32_t steps = elapsed / BLINK_INTERVAL_MS;
    lastPhaseAt += steps * BLINK_INTERVAL_MS;
    // Catch up after a delayed loop in constant time, including millis rollover.
    if ((steps & 1U) == 0) return false;
    blinkPhase = !blinkPhase;
    return true;
}
