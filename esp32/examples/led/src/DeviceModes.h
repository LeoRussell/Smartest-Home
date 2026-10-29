#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

enum DeviceModeOperation : uint8_t {
    MODE_CAN_SELECT = 1 << 0,
    MODE_CAN_ENABLE = 1 << 1,
    MODE_CAN_DISABLE = 1 << 2
};

using DeviceModeHandler = bool (*)(String& error);
using DeviceModeStatusHandler = bool (*)();

struct DeviceModeRegistration {
    const char* id;
    const char* label;
    const char* description;
    uint8_t operations;
    DeviceModeHandler select;
    DeviceModeHandler enable;
    DeviceModeHandler disable;
    const char* const* conflictsWith;
    const char* statusParameter;
    bool statusValue;
    DeviceModeStatusHandler isActive;
};

void appendRegisteredDeviceModes(JsonObject schema);
bool runRegisteredDeviceModeAction(const char* actionId, String& error);
void appendRegisteredDeviceModeStates(JsonObject parameters);
