#pragma once

#include <Arduino.h>

// Mode callbacks change the logical LED state through the application; the
// regular loop renders their physical phase without blocking GatewayClient.
using LedModeOutputHandler = void (*)(bool enabled);

void initializeLedModes(LedModeOutputHandler setLogicalOutput);
void stopLedModeForManualControl();
bool selectManualLedMode(String& error);
bool enableBlinkLedMode(String& error);
bool disableBlinkLedMode(String& error);
bool manualLedModeIsActive();
bool blinkLedModeIsActive();
bool ledModeOutput(bool logicalOn);
bool updateLedModes(uint32_t now);
