#include "DeviceModes.h"

#include <cstring>

// Add user-owned mode callbacks here. Keep their IDs stable: saved Gateway
// automations refer to the action IDs generated from these mode IDs.
const DeviceModeRegistration deviceModes[] = {
    {nullptr, nullptr, nullptr, 0, nullptr, nullptr, nullptr, nullptr,
     nullptr, false, nullptr}
};

namespace {
constexpr uint8_t MAX_REGISTERED_MODES = 8;

struct OperationDefinition {
    const char* id;
    uint8_t flag;
    DeviceModeHandler DeviceModeRegistration::*handler;
    const char* russianLabel;
};

constexpr OperationDefinition OPERATIONS[] = {
    {"select", MODE_CAN_SELECT, &DeviceModeRegistration::select,
     "Выбрать режим"},
    {"enable", MODE_CAN_ENABLE, &DeviceModeRegistration::enable,
     "Включить режим"},
    {"disable", MODE_CAN_DISABLE, &DeviceModeRegistration::disable,
     "Выключить режим"}
};

bool publishedModeStatuses[MAX_REGISTERED_MODES] = {};

size_t registeredModeCount() {
    size_t count = 0;
    while (count < MAX_REGISTERED_MODES && deviceModes[count].id != nullptr) ++count;
    return count;
}

bool validModeId(const char* value) {
    if (value == nullptr) return false;
    const char first = value[0];
    if (!((first >= 'A' && first <= 'Z') || (first >= 'a' && first <= 'z'))) {
        return false;
    }
    size_t length = 0;
    for (; value[length] != '\0'; ++length) {
        const char character = value[length];
        const bool alpha = (character >= 'A' && character <= 'Z') ||
                           (character >= 'a' && character <= 'z');
        const bool extra = length > 0 &&
            ((character >= '0' && character <= '9') || character == '_' ||
             character == '.' || character == '-');
        if (!alpha && !extra) return false;
    }
    return length > 0 && length <= 47;
}

String actionIdFor(const char* modeId, const char* operationId) {
    String actionId = "mode_";
    actionId += modeId;
    actionId += '_';
    actionId += operationId;
    return actionId;
}

bool registrationIsValid(const DeviceModeRegistration& mode) {
    return validModeId(mode.id) && mode.label != nullptr &&
           strlen(mode.label) > 0 && strlen(mode.label) <= 80 &&
           mode.description != nullptr && strlen(mode.description) <= 240;
}

bool modeIdWasRegisteredEarlier(size_t index) {
    for (size_t previous = 0; previous < index; ++previous) {
        if (deviceModes[previous].id != nullptr &&
            strcmp(deviceModes[previous].id, deviceModes[index].id) == 0) {
            return true;
        }
    }
    return false;
}
}  // namespace

void appendRegisteredDeviceModes(JsonObject schema) {
    memset(publishedModeStatuses, 0, sizeof(publishedModeStatuses));
    if (registeredModeCount() == 0) return;
    JsonObject modes = schema["modes"].to<JsonObject>();
    JsonObject actions = schema["actions"].to<JsonObject>();
    JsonObject parameters = schema["parameters"].to<JsonObject>();

    for (size_t index = 0; index < registeredModeCount(); ++index) {
        const DeviceModeRegistration& registration = deviceModes[index];
        if (!registrationIsValid(registration) || modeIdWasRegisteredEarlier(index)) {
            Serial.printf("[MODE] Ignoring invalid mode registration at index %u.\n",
                          static_cast<unsigned int>(index));
            continue;
        }

        JsonObject mode = modes[registration.id].to<JsonObject>();
        mode["label"] = registration.label;
        mode["description"] = registration.description;
        JsonObject modeOperations = mode["operations"].to<JsonObject>();

        for (const OperationDefinition& operation : OPERATIONS) {
            DeviceModeHandler handler = registration.*(operation.handler);
            if ((registration.operations & operation.flag) == 0 || handler == nullptr) {
                continue;
            }

            const String actionId = actionIdFor(registration.id, operation.id);
            if (actionId.length() > 64) continue;
            modeOperations[operation.id] = actionId;
            JsonObject action = actions[actionId].to<JsonObject>();
            action["label"] = operation.russianLabel;
            action["description"] = registration.description;
        }

        if (modeOperations.size() == 0) {
            modes.remove(registration.id);
            continue;
        }

        if (registration.conflictsWith != nullptr) {
            JsonArray conflicts = mode["conflicts_with"].to<JsonArray>();
            for (size_t conflictIndex = 0; conflictIndex < MAX_REGISTERED_MODES;
                 ++conflictIndex) {
                const char* conflictId = registration.conflictsWith[conflictIndex];
                if (conflictId == nullptr) break;
                bool conflictExists = false;
                for (size_t otherIndex = 0; otherIndex < registeredModeCount(); ++otherIndex) {
                    if (otherIndex != index && registrationIsValid(deviceModes[otherIndex]) &&
                        strcmp(conflictId, deviceModes[otherIndex].id) == 0) {
                        conflictExists = true;
                        break;
                    }
                }
                if (conflictExists && validModeId(conflictId) &&
                    strcmp(conflictId, registration.id) != 0) {
                    bool duplicate = false;
                    for (JsonVariantConst existing : conflicts) {
                        const char* existingId = existing.as<const char*>();
                        if (existingId != nullptr && strcmp(existingId, conflictId) == 0) {
                            duplicate = true;
                        }
                    }
                    if (!duplicate) conflicts.add(conflictId);
                }
            }
            if (conflicts.size() == 0) mode.remove("conflicts_with");
        }

        if (registration.statusParameter != nullptr && registration.isActive != nullptr &&
            validModeId(registration.statusParameter)) {
            JsonVariantConst existingValue = parameters[registration.statusParameter];
            const bool alreadyDeclared = !existingValue.isUnbound();
            bool compatible = !alreadyDeclared;
            if (alreadyDeclared) {
                JsonObjectConst existing = existingValue.as<JsonObjectConst>();
                compatible = existing["type"] == "boolean" &&
                    (existing["access"] == "read_only" ||
                     existing["access"] == "read_write");
            } else {
                JsonObject status = parameters[registration.statusParameter].to<JsonObject>();
                status["label"] = registration.label;
                status["type"] = "boolean";
                status["access"] = "read_only";
            }
            if (compatible) {
                mode["status_parameter"] = registration.statusParameter;
                mode["status_value"] = registration.statusValue;
                publishedModeStatuses[index] = true;
            }
        }
    }
}

bool runRegisteredDeviceModeAction(const char* requestedActionId, String& error) {
    if (requestedActionId == nullptr) {
        error = "Mode action ID is missing";
        return false;
    }

    for (size_t index = 0; index < registeredModeCount(); ++index) {
        const DeviceModeRegistration& registration = deviceModes[index];
        if (!registrationIsValid(registration)) continue;
        for (const OperationDefinition& operation : OPERATIONS) {
            DeviceModeHandler handler = registration.*(operation.handler);
            if ((registration.operations & operation.flag) == 0 || handler == nullptr) {
                continue;
            }
            const String actionId = actionIdFor(registration.id, operation.id);
            if (actionId != requestedActionId) continue;
            return handler(error);
        }
    }

    error = "Unsupported action";
    return false;
}

void appendRegisteredDeviceModeStates(JsonObject parameters) {
    for (size_t index = 0; index < registeredModeCount(); ++index) {
        const DeviceModeRegistration& registration = deviceModes[index];
        if (!registrationIsValid(registration) || modeIdWasRegisteredEarlier(index) ||
            !publishedModeStatuses[index] ||
            registration.statusParameter == nullptr || registration.isActive == nullptr) {
            continue;
        }
        parameters[registration.statusParameter] =
            registration.isActive() == registration.statusValue;
    }
}
