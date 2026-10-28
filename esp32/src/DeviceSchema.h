#pragma once

#include <ArduinoJson.h>

// Build the capability document independently from transport/command logic.
void buildDeviceSchema(JsonObject schema);
