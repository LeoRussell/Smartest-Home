#include "CommandResultCache.h"

#include <esp_system.h>

namespace {
void recordKey(uint8_t slot, char* key, size_t capacity) {
    snprintf(key, capacity, "c%u", static_cast<unsigned int>(slot));
}
}

bool CommandResultCache::begin(const char* deviceId, const char* storageNamespace, bool migrateLedLegacy) {
    bound_ = false;
    cache_.disable();
    if (deviceId == nullptr || deviceId[0] == '\0' || strlen(deviceId) > 256 ||
        storageNamespace == nullptr || storageNamespace[0] == '\0' ||
        strlen(storageNamespace) > 15) return false;
    if (migrateLedLegacy && strcmp(storageNamespace, "command_ack_v2") != 0) return false;
    deviceId_ = deviceId;
    if (deviceId_.length() != strlen(deviceId)) return false;
    if (opened_) nvs_close(handle_);
    opened_ = nvs_open(storageNamespace, NVS_READWRITE, &handle_) == ESP_OK;
    if (!opened_) {
        cache_.disable();
        return false;
    }
    if (!bindDevice(deviceId, migrateLedLegacy)) return false;
    bound_ = true;
    if (!cache_.begin()) return false;
    if (migrateLedLegacy && !migrateLegacy(deviceId)) {
        cache_.disable();
        return false;
    }
    if (migrateLedLegacy) cleanLegacyKeys();
    return true;
}

bool CommandResultCache::validCommandId(JsonVariantConst commandId) {
    if (!commandId.is<const char*>()) return false;
    const JsonString value = commandId.as<JsonString>();
    return value.size() > 0 && value.size() <= DurableCommandCache::MAX_ID_BYTES &&
           value.size() == strlen(value.c_str());
}

CommandResultCache::Decision CommandResultCache::reserve(const char* commandId, String& result, bool allowExecution) {
    result = "";
    if (!bound_) return Decision::Rejected;
    std::string replay;
    const Decision decision = cache_.reserve(commandId == nullptr ? "" : commandId, replay, allowExecution);
    if (decision == Decision::Replay) {
        JsonDocument acknowledgement;
        if (deserializeJson(acknowledgement, replay.c_str(), replay.size()) ||
            acknowledgement["type"] != "command_result" ||
            acknowledgement["command_id"] != commandId ||
            acknowledgement["device"] != deviceId_.c_str() ||
            !acknowledgement["success"].is<bool>()) {
            cache_.disable();
            return Decision::Uncertain;
        }
        // The saved result describes the original execution, not today's GPIO
        // after newer commands or reboot. The following state is authoritative.
        acknowledgement["replayed"] = true;
        const size_t expectedSize = measureJson(acknowledgement);
        if (acknowledgement.overflowed() || !result.reserve(expectedSize) ||
            serializeJson(acknowledgement, result) != expectedSize ||
            result.length() != expectedSize) {
            cache_.disable();
            result = "";
            return Decision::Uncertain;
        }
    }
    return decision;
}

bool CommandResultCache::serializeBounded(JsonDocument& acknowledgement, String& outgoing) const {
    outgoing = "";
    const size_t size = measureJson(acknowledgement);
    if (acknowledgement.overflowed() || size > DurableCommandCache::MAX_RESULT_BYTES ||
        !outgoing.reserve(size) || serializeJson(acknowledgement, outgoing) != size ||
        outgoing.length() != size) {
        outgoing = "";
        return false;
    }
    return true;
}

bool CommandResultCache::complete(JsonDocument& acknowledgement, String& outgoing) {
    outgoing = "";
    if (!bound_ || acknowledgement["type"] != "command_result" ||
        acknowledgement["device"] != deviceId_.c_str() ||
        !validCommandId(acknowledgement["command_id"]) ||
        !acknowledgement["success"].is<bool>()) {
        cache_.disable();
        return false;
    }
    const char* commandId = acknowledgement["command_id"].as<const char*>();
    if (serializeBounded(acknowledgement, outgoing) &&
        cache_.complete(commandId, std::string(outgoing.c_str(), outgoing.length()))) return true;
    cache_.disable();
    acknowledgement["success"] = false;
    acknowledgement["data"]["error"] = "Command execution may have occurred, but its result could not be durably saved. Check the current state; it will not be repeated.";
    acknowledgement.remove("replayed");
    serializeBounded(acknowledgement, outgoing);
    return false;
}

bool CommandResultCache::bindDevice(const char* deviceId, bool adoptLedLegacy) {
    size_t size = 0;
    const esp_err_t status = nvs_get_str(handle_, "owner", nullptr, &size);
    if (status == ESP_OK) {
        if (size == 0 || size > 257) return false;
        std::string owner(size, '\0');
        const size_t expectedSize = size;
        return nvs_get_str(handle_, "owner", &owner[0], &size) == ESP_OK &&
               size == expectedSize && owner == std::string(deviceId) + '\0';
    }
    if (status != ESP_ERR_NVS_NOT_FOUND) return false;
    if (!adoptLedLegacy) {
        for (uint8_t slot = 0; slot < DurableCommandCache::SLOT_COUNT; ++slot) {
            std::string record;
            if (read(slot, record) != ReadStatus::Missing) return false;
        }
    }
    if (nvs_set_str(handle_, "owner", deviceId) != ESP_OK || nvs_commit(handle_) != ESP_OK) return false;
    // Validate the exact persisted identity, not a truncated or default value.
    std::string owner;
    return readLegacyString("owner", owner, 256) && owner == deviceId;
}

DurableCommandCache::ReadStatus CommandResultCache::read(uint8_t slot, std::string& record) {
    char key[5];
    recordKey(slot, key, sizeof(key));
    size_t size = 0;
    const esp_err_t status = nvs_get_blob(handle_, key, nullptr, &size);
    if (status == ESP_ERR_NVS_NOT_FOUND) return ReadStatus::Missing;
    if (status != ESP_OK || size == 0 ||
        size > DurableCommandCache::MAX_RECORD_BYTES) return ReadStatus::Error;
    record.resize(size);
    const size_t expectedSize = size;
    return nvs_get_blob(handle_, key, &record[0], &size) == ESP_OK && size == expectedSize
        ? ReadStatus::Present : ReadStatus::Error;
}

bool CommandResultCache::write(uint8_t slot, const std::string& record) {
    char key[5];
    recordKey(slot, key, sizeof(key));
    if (nvs_set_blob(handle_, key, record.data(), record.size()) != ESP_OK ||
        nvs_commit(handle_) != ESP_OK) return false;
    std::string verified;
    return read(slot, verified) == ReadStatus::Present && verified == record;
}

bool CommandResultCache::verifyWritable() {
    const uint32_t probe = esp_random();
    uint32_t readback = 0;
    size_t size = sizeof(readback);
    return nvs_set_blob(handle_, "probe", &probe, sizeof(probe)) == ESP_OK &&
           nvs_commit(handle_) == ESP_OK &&
           nvs_get_blob(handle_, "probe", &readback, &size) == ESP_OK &&
           size == sizeof(readback) && readback == probe;
}

bool CommandResultCache::migrateLegacy(const char* deviceId) {
    uint8_t format = 0;
    const esp_err_t formatStatus = nvs_get_u8(handle_, "format", &format);
    if (formatStatus == ESP_OK && format == 3) return true;
    if (formatStatus != ESP_ERR_NVS_NOT_FOUND) return false;
    uint8_t oldest = 0;
    const esp_err_t nextStatus = nvs_get_u8(handle_, "next", &oldest);
    if (nextStatus != ESP_OK && nextStatus != ESP_ERR_NVS_NOT_FOUND) return false;
    oldest %= DurableCommandCache::SLOT_COUNT;
    for (uint8_t index = 0; index < DurableCommandCache::SLOT_COUNT; ++index) {
        const uint8_t slot = (oldest + index) % DurableCommandCache::SLOT_COUNT;
        char idKey[5];
        char resultKey[5];
        snprintf(idKey, sizeof(idKey), "id%u", static_cast<unsigned int>(slot));
        snprintf(resultKey, sizeof(resultKey), "r%u", static_cast<unsigned int>(slot));
        std::string id;
        std::string result;
        if (!readLegacyString(idKey, id, DurableCommandCache::MAX_ID_BYTES) ||
            !readLegacyString(resultKey, result, DurableCommandCache::MAX_RESULT_BYTES)) return false;
        if (id.empty() && result.empty()) continue;
        // A torn legacy pair must not be silently treated as a new command.
        JsonDocument document;
        if (id.empty() || result.empty() ||
            deserializeJson(document, result.c_str(), result.size()) || document["type"] != "command_result" ||
            !document["success"].is<bool>() || document["device"] != deviceId ||
            document["command_id"] != id.c_str()) return false;
        std::string replay;
        const auto decision = cache_.reserve(id, replay);
        if (decision == Decision::Rejected) return false;
        if (decision == Decision::Replay) continue;
        // An interrupted migration already has an intent. Its legacy result is
        // still available, so finish the record without executing any action.
        if (!cache_.complete(id, result)) return false;
    }
    return nvs_set_u8(handle_, "format", 3) == ESP_OK && nvs_commit(handle_) == ESP_OK &&
           nvs_get_u8(handle_, "format", &format) == ESP_OK && format == 3;
}

bool CommandResultCache::readLegacyString(const char* key, std::string& value, size_t maximum) {
    size_t size = 0;
    const esp_err_t status = nvs_get_str(handle_, key, nullptr, &size);
    if (status == ESP_ERR_NVS_NOT_FOUND) return true;
    if (status != ESP_OK || size == 0 || size > maximum + 1) return false;
    value.resize(size);
    const size_t expectedSize = size;
    if (nvs_get_str(handle_, key, &value[0], &size) != ESP_OK || size != expectedSize ||
        value.back() != '\0') return false;
    value.pop_back();
    return value.find('\0') == std::string::npos;
}

void CommandResultCache::cleanLegacyKeys() {
    // Reached only after all records and format=3 were committed and read back.
    // A cleanup failure can leave redundant old keys; it never removes cN keys.
    bool changed = false;
    for (uint8_t slot = 0; slot <= DurableCommandCache::SLOT_COUNT; ++slot) {
        const uint8_t keysAtSlot = slot == DurableCommandCache::SLOT_COUNT ? 1 : 2;
        for (uint8_t index = 0; index < keysAtSlot; ++index) {
            char key[5];
            if (slot == DurableCommandCache::SLOT_COUNT) {
                snprintf(key, sizeof(key), "next");
            } else {
                snprintf(key, sizeof(key), index == 0 ? "id%u" : "r%u",
                         static_cast<unsigned int>(slot));
            }
            const esp_err_t status = nvs_erase_key(handle_, key);
            if (status == ESP_OK) changed = true;
            else if (status != ESP_ERR_NVS_NOT_FOUND) {
                Serial.printf("[COMMAND CACHE] Legacy cleanup left %s: %s\n", key, esp_err_to_name(status));
            }
        }
    }
    if (changed && nvs_commit(handle_) != ESP_OK) {
        Serial.println("[COMMAND CACHE] Legacy cleanup commit failed; current command records are preserved.");
    }
}
