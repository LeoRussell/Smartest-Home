#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <nvs.h>
#include "DurableCommandCache.h"

class CommandResultCache : private DurableCommandCache::Storage {
public:
    using Decision = DurableCommandCache::Decision;
    CommandResultCache() : cache_(*this) {}
    ~CommandResultCache() { if (opened_) nvs_close(handle_); }
    CommandResultCache(const CommandResultCache&) = delete;
    CommandResultCache& operator=(const CommandResultCache&) = delete;
    bool begin(const char* deviceId, const char* storageNamespace, bool migrateLedLegacy = false);
    bool available() const { return bound_ && cache_.available(); }
    static bool validCommandId(JsonVariantConst commandId);
    Decision reserve(const char* commandId, String& result, bool allowExecution = true);
    // On persistence failure outgoing is a negative ACK, never an unsafe success.
    // An empty outgoing string means even the bounded error could not serialize.
    bool complete(JsonDocument& acknowledgement, String& outgoing);

private:
    using ReadStatus = DurableCommandCache::ReadStatus;
    ReadStatus read(uint8_t slot, std::string& record) override;
    bool write(uint8_t slot, const std::string& record) override;
    bool verifyWritable() override;
    bool migrateLegacy(const char* deviceId);
    void cleanLegacyKeys();
    bool bindDevice(const char* deviceId, bool adoptLedLegacy);
    bool serializeBounded(JsonDocument& acknowledgement, String& outgoing) const;
    bool readLegacyString(const char* key, std::string& value, size_t maximum);
    nvs_handle_t handle_ = 0;
    bool opened_ = false;
    bool bound_ = false;
    String deviceId_;
    DurableCommandCache cache_;
};
