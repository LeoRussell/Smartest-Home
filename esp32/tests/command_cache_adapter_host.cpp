// Compile the production adapter with real ArduinoJson and fake SDK storage.
#include "CommandResultCache.h"

#include <cassert>
#include <iostream>
#include <map>

#ifdef NDEBUG
#error "Firmware host regression requires enabled assertions"
#endif

HostSerial Serial;
using Decision = CommandResultCache::Decision;

namespace {
enum class Type { Blob, Text, Byte };
struct StoredValue {
    Type type;
    std::string bytes;
};
using Values = std::map<std::string, StoredValue>;
std::map<std::string, Values> storage;
std::map<nvs_handle_t, std::string> handles;
nvs_handle_t nextHandle = 1;
std::string failSetKey;
std::string failEraseKey;
bool failCommit = false;

Values& values(nvs_handle_t handle) { return storage.at(handles.at(handle)); }
esp_err_t get(nvs_handle_t handle, const char* key, Type type, void* destination, size_t* size) {
    const auto found = values(handle).find(key);
    if (found == values(handle).end()) return ESP_ERR_NVS_NOT_FOUND;
    if (found->second.type != type) return ESP_ERR_NVS_TYPE_MISMATCH;
    const size_t required = found->second.bytes.size();
    if (destination != nullptr) {
        if (*size < required) return ESP_ERR_NVS_INVALID_LENGTH;
        memcpy(destination, found->second.bytes.data(), required);
    }
    *size = required;
    return ESP_OK;
}
esp_err_t set(nvs_handle_t handle, const char* key, Type type, const void* bytes, size_t size) {
    if (failSetKey == key) return ESP_FAIL;
    values(handle)[key] = StoredValue{type, std::string(static_cast<const char*>(bytes), size)};
    return ESP_OK;
}
void reset() {
    assert(handles.empty());
    storage.clear();
    failSetKey.clear();
    failEraseKey.clear();
    failCommit = false;
}
void makeAck(JsonDocument& result, const char* device, const char* id, bool value = true) {
    result.clear();
    result["type"] = "command_result";
    result["id"] = id;
    result["command_id"] = id;
    result["device"] = device;
    result["success"] = true;
    result["data"]["parameter"] = "led";
    result["data"]["value"] = value;
}
JsonDocument parse(const String& text) {
    JsonDocument parsed;
    assert(!deserializeJson(parsed, text.c_str(), text.length()));
    return parsed;
}
void seedLegacy(const char* id = "legacy") {
    nvs_handle_t handle;
    assert(nvs_open("command_ack_v2", NVS_READWRITE, &handle) == ESP_OK);
    JsonDocument result;
    makeAck(result, "led-device", id);
    std::string serialized;
    serializeJson(result, serialized);
    assert(nvs_set_str(handle, "id0", id) == ESP_OK);
    assert(nvs_set_str(handle, "r0", serialized.c_str()) == ESP_OK);
    assert(nvs_set_u8(handle, "next", 1) == ESP_OK);
    assert(nvs_commit(handle) == ESP_OK);
    nvs_close(handle);
}

void profileNamespacesAndOwnerBinding() {
    reset();
    String output;
    JsonDocument result;
    {
        CommandResultCache led;
        CommandResultCache universal;
        assert(led.begin("led-device", "command_ack_v2", true));
        assert(universal.begin("universal-device", "universal_ack"));
        assert(led.reserve("same-id", output) == Decision::Execute);
        makeAck(result, "led-device", "same-id", true);
        assert(led.complete(result, output));
        assert(universal.reserve("same-id", output) == Decision::Execute);
        makeAck(result, "universal-device", "same-id", false);
        assert(universal.complete(result, output));
        assert(!led.begin("different-device", "command_ack_v2", true));
        assert(!led.available());
        assert(led.reserve("same-id", output) == Decision::Rejected);
    }
    CommandResultCache led;
    CommandResultCache universal;
    assert(led.begin("led-device", "command_ack_v2", true));
    assert(universal.begin("universal-device", "universal_ack"));
    assert(led.reserve("same-id", output) == Decision::Replay);
    auto ledAck = parse(output);
    assert(ledAck["device"] == "led-device" && ledAck["data"]["value"] == true);
    assert(ledAck["replayed"] == true);
    assert(universal.reserve("same-id", output) == Decision::Replay);
    auto universalAck = parse(output);
    assert(universalAck["device"] == "universal-device" && universalAck["data"]["value"] == false);
    assert(universalAck["replayed"] == true);
    assert(storage.at("command_ack_v2").at("c0").bytes.find("replayed") == std::string::npos);
    assert(storage.at("universal_ack").at("c0").bytes.find("replayed") == std::string::npos);
}

void failedResultWriteNeverSendsSuccess(bool commitFails) {
    reset();
    String output;
    {
        CommandResultCache cache;
        assert(cache.begin("universal-device", "universal_ack"));
        assert(cache.reserve("executed", output) == Decision::Execute);
        if (commitFails) failCommit = true;
        else failSetKey = "c0";
        JsonDocument result;
        makeAck(result, "universal-device", "executed");
        assert(!cache.complete(result, output));
        assert(!cache.available());
        auto failed = parse(output);
        assert(failed["success"] == false);
        assert(failed["data"]["value"] == true);  // Truthful current state.
        assert(failed["data"]["error"].is<const char*>());
        assert(cache.reserve("executed", output) == Decision::Uncertain);
        assert(cache.reserve("new", output) == Decision::Rejected);
    }
    failCommit = false;
    failSetKey.clear();
    CommandResultCache rebooted;
    assert(rebooted.begin("universal-device", "universal_ack"));
    // This fake keeps a set's value visible even when commit fails. That models
    // the ambiguous case where the durable record may actually have survived.
    assert(rebooted.reserve("executed", output) ==
           (commitFails ? Decision::Replay : Decision::Uncertain));
    if (commitFails) assert(parse(output)["replayed"] == true);
}

void oversizedAckAndInvalidIds() {
    reset();
    CommandResultCache cache;
    assert(cache.begin("universal-device", "universal_ack"));
    String output;
    assert(cache.reserve("large", output) == Decision::Execute);
    JsonDocument result;
    makeAck(result, "universal-device", "large");
    result["data"]["error"] = std::string(DurableCommandCache::MAX_RESULT_BYTES + 1, 'x');
    assert(!cache.complete(result, output));
    assert(output.length() <= DurableCommandCache::MAX_RESULT_BYTES);
    assert(parse(output)["success"] == false);
    assert(cache.reserve("large", output) == Decision::Uncertain);
    JsonDocument malformed;
    assert(!deserializeJson(malformed, "{\"id\":\"bad\\u0000suffix\"}"));
    assert(!CommandResultCache::validCommandId(malformed["id"]));
    malformed["id"] = 12;
    assert(!CommandResultCache::validCommandId(malformed["id"]));
}

void legacyCleanupAfterVerifiedMigration() {
    reset();
    seedLegacy();
    storage["device_cfg"]["envelope"] = StoredValue{Type::Text, "configuration"};
    CommandResultCache cache;
    assert(cache.begin("led-device", "command_ack_v2", true));
    const auto& keys = storage.at("command_ack_v2");
    assert(keys.count("id0") == 0 && keys.count("r0") == 0 && keys.count("next") == 0);
    assert(keys.count("c0") == 1 && keys.count("owner") == 1 && keys.count("format") == 1);
    assert(storage.at("device_cfg").at("envelope").bytes == "configuration");
    String output;
    assert(cache.reserve("legacy", output) == Decision::Replay);
    assert(parse(output)["replayed"] == true);
}

void interruptedMigrationKeepsLegacyUntilConfirmed() {
    reset();
    seedLegacy();
    String output;
    {
        failSetKey = "format";
        CommandResultCache cache;
        assert(!cache.begin("led-device", "command_ack_v2", true));
        const auto& keys = storage.at("command_ack_v2");
        assert(keys.count("id0") == 1 && keys.count("r0") == 1 && keys.count("next") == 1);
        assert(keys.count("c0") == 1 && keys.count("format") == 0);
        assert(cache.reserve("new", output) == Decision::Rejected);
    }
    failSetKey.clear();
    CommandResultCache rebooted;
    assert(rebooted.begin("led-device", "command_ack_v2", true));
    assert(rebooted.reserve("legacy", output) == Decision::Replay);
    assert(storage.at("command_ack_v2").count("id0") == 0);
}

void cleanupFailurePreservesCurrentLedger() {
    reset();
    seedLegacy();
    String output;
    {
        failEraseKey = "id0";
        CommandResultCache cache;
        assert(cache.begin("led-device", "command_ack_v2", true));
        assert(storage.at("command_ack_v2").count("id0") == 1);
        assert(storage.at("command_ack_v2").count("r0") == 0);
        assert(storage.at("command_ack_v2").count("c0") == 1);
        assert(cache.reserve("legacy", output) == Decision::Replay);
    }
    failEraseKey.clear();
    CommandResultCache rebooted;
    assert(rebooted.begin("led-device", "command_ack_v2", true));
    assert(storage.at("command_ack_v2").count("id0") == 0);
    assert(rebooted.reserve("legacy", output) == Decision::Replay);
}

void orphanUniversalRecordsCannotBeAdopted() {
    reset();
    storage["universal_ack"]["c0"] = StoredValue{Type::Blob, "orphan"};
    CommandResultCache cache;
    assert(!cache.begin("universal-device", "universal_ack"));
    assert(!cache.available());
    assert(storage.at("universal_ack").count("owner") == 0);
    String output;
    assert(cache.reserve("new", output) == Decision::Rejected);
}

void retryOnlyReplaysKnownDurableResults() {
    reset();
    String output;
    {
        CommandResultCache cache;
        assert(cache.begin("universal-device", "universal_ack"));
        for (unsigned int index = 0; index <= DurableCommandCache::SLOT_COUNT; ++index) {
            const std::string id = "command-" + std::to_string(index);
            assert(cache.reserve(id.c_str(), output) == Decision::Execute);
            JsonDocument result;
            makeAck(result, "universal-device", id.c_str());
            assert(cache.complete(result, output));
        }
        assert(cache.reserve("command-0", output, false) == Decision::Uncertain);
        assert(output.isEmpty());
        assert(cache.reserve("command-16", output, false) == Decision::Replay);
        assert(parse(output)["replayed"] == true);
    }
    CommandResultCache rebooted;
    assert(rebooted.begin("universal-device", "universal_ack"));
    assert(rebooted.reserve("command-0", output, false) == Decision::Uncertain);
    assert(output.isEmpty());
    assert(rebooted.reserve("command-16", output, false) == Decision::Replay);
    assert(parse(output)["command_id"] == "command-16");
}
}  // namespace

esp_err_t nvs_open(const char* name, int, nvs_handle_t* handle) {
    *handle = nextHandle++;
    handles[*handle] = name;
    storage[name];
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { handles.erase(handle); }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char* key, void* data, size_t* size) {
    return get(handle, key, Type::Blob, data, size);
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char* key, const void* data, size_t size) {
    return set(handle, key, Type::Blob, data, size);
}
esp_err_t nvs_get_str(nvs_handle_t handle, const char* key, char* data, size_t* size) {
    return get(handle, key, Type::Text, data, size);
}
esp_err_t nvs_set_str(nvs_handle_t handle, const char* key, const char* data) {
    return set(handle, key, Type::Text, data, strlen(data) + 1);
}
esp_err_t nvs_get_u8(nvs_handle_t handle, const char* key, uint8_t* value) {
    size_t size = 1;
    return get(handle, key, Type::Byte, value, &size);
}
esp_err_t nvs_set_u8(nvs_handle_t handle, const char* key, uint8_t value) {
    return set(handle, key, Type::Byte, &value, 1);
}
esp_err_t nvs_erase_key(nvs_handle_t handle, const char* key) {
    if (failEraseKey == key) return ESP_FAIL;
    return values(handle).erase(key) == 0 ? ESP_ERR_NVS_NOT_FOUND : ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t) { return failCommit ? ESP_FAIL : ESP_OK; }
const char* esp_err_to_name(esp_err_t) { return "injected NVS failure"; }

int main() {
    profileNamespacesAndOwnerBinding();
    failedResultWriteNeverSendsSuccess(false);
    failedResultWriteNeverSendsSuccess(true);
    oversizedAckAndInvalidIds();
    legacyCleanupAfterVerifiedMigration();
    interruptedMigrationKeepsLegacyUntilConfirmed();
    cleanupFailurePreservesCurrentLedger();
    orphanUniversalRecordsCannotBeAdopted();
    retryOnlyReplaysKnownDurableResults();
    std::cout << "9 command cache adapter scenarios passed\n";
}
