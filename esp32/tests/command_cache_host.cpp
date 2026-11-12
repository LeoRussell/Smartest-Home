// Run by gateway/tests/test_firmware_command_cache.py with a native C++ compiler.
// These tests execute production cache logic; they do not simulate GPIO or NVS.
#include "DurableCommandCache.h"

#include <cassert>
#include <iostream>
#include <map>

#ifdef NDEBUG
#error "Firmware host regression requires enabled assertions"
#endif

using Cache = DurableCommandCache;
using Decision = Cache::Decision;

struct MemoryStorage : Cache::Storage {
    std::map<uint8_t, std::string> records;
    bool probeOk = true;
    bool failWrite = false;
    bool commitBeforeFailure = false;
    int failReadSlot = -1;
    unsigned int writes = 0;

    Cache::ReadStatus read(uint8_t slot, std::string& record) override {
        if (failReadSlot == slot) return Cache::ReadStatus::Error;
        const auto found = records.find(slot);
        if (found == records.end()) return Cache::ReadStatus::Missing;
        record = found->second;
        return Cache::ReadStatus::Present;
    }
    bool write(uint8_t slot, const std::string& record) override {
        ++writes;
        if (!failWrite || commitBeforeFailure) records[slot] = record;
        return !failWrite;
    }
    bool verifyWritable() override { return probeOk; }
};

void duplicateAndReboot() {
    MemoryStorage storage;
    std::string result;
    Cache cache(storage);
    assert(cache.begin());
    assert(cache.reserve("on-1", result) == Decision::Execute);
    assert(storage.records.size() == 1);  // Durable intent precedes the action.
    assert(cache.complete("on-1", "on acknowledged"));
    const auto completedWrites = storage.writes;
    assert(cache.complete("on-1", "on acknowledged"));
    assert(storage.writes == completedWrites);
    assert(cache.reserve("off-2", result) == Decision::Execute);
    assert(cache.complete("off-2", "off acknowledged"));
    const auto writes = storage.writes;
    assert(cache.reserve("on-1", result) == Decision::Replay);
    assert(result == "on acknowledged");
    assert(storage.writes == writes);  // Retries never write or actuate again.
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("on-1", result) == Decision::Replay);
    assert(result == "on acknowledged");
}

void interruptedExecutionNeverRepeats() {
    MemoryStorage storage;
    std::string result;
    {
        Cache cache(storage);
        assert(cache.begin());
        assert(cache.reserve("pulse", result) == Decision::Execute);
        // Either side of the physical action can lose power here. Recovery
        // cannot distinguish those two cases and must not repeat the action.
    }
    Cache rebooted(storage);
    assert(rebooted.begin());
    const auto writes = storage.writes;
    assert(rebooted.reserve("pulse", result) == Decision::Uncertain);
    assert(result.empty());
    assert(storage.writes == writes);
    assert(rebooted.reserve("explicit-new-pulse", result) == Decision::Execute);
}

void intentFailureBlocksActions(bool committed) {
    MemoryStorage storage;
    Cache cache(storage);
    std::string result;
    assert(cache.begin());
    storage.failWrite = true;
    storage.commitBeforeFailure = committed;
    assert(cache.reserve("must-not-execute", result) == Decision::Rejected);
    assert(!cache.available());
    storage.failWrite = false;
    assert(cache.reserve("another", result) == Decision::Rejected);
    assert(storage.writes == 1);
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("must-not-execute", result) ==
           (committed ? Decision::Uncertain : Decision::Execute));
}

void finalWriteFailureKeepsIntent(bool committed) {
    MemoryStorage storage;
    Cache cache(storage);
    std::string result;
    assert(cache.begin());
    assert(cache.reserve("already-executed", result) == Decision::Execute);
    storage.failWrite = true;
    storage.commitBeforeFailure = committed;
    assert(!cache.complete("already-executed", "actual result"));
    assert(!cache.available());
    assert(cache.reserve("already-executed", result) == Decision::Uncertain);
    assert(result.empty());
    assert(cache.reserve("new-action", result) == Decision::Rejected);
    storage.failWrite = false;
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("already-executed", result) ==
           (committed ? Decision::Replay : Decision::Uncertain));
    if (committed) assert(result == "actual result");
}

void failedStorageProbeStillReplaysKnownResults() {
    MemoryStorage storage;
    std::string result;
    Cache cache(storage);
    assert(cache.begin());
    assert(cache.reserve("old", result) == Decision::Execute);
    assert(cache.complete("old", "saved result"));
    storage.probeOk = false;
    Cache rebooted(storage);
    assert(!rebooted.begin());
    assert(rebooted.reserve("old", result) == Decision::Replay);
    assert(result == "saved result");
    assert(rebooted.reserve("new", result) == Decision::Rejected);
}

void invalidRecordBlocksNewActions() {
    MemoryStorage storage;
    Cache cache(storage);
    std::string result;
    assert(cache.begin());
    assert(cache.reserve("valid", result) == Decision::Execute);
    assert(cache.complete("valid", "preserved"));
    assert(cache.reserve("corrupt", result) == Decision::Execute);
    storage.records[1][3] ^= 1;  // CRC detects a corrupted sequence, not just JSON.
    Cache rebooted(storage);
    assert(!rebooted.begin());
    assert(rebooted.reserve("valid", result) == Decision::Replay);
    assert(result == "preserved");
    assert(rebooted.reserve("corrupt", result) == Decision::Rejected);
    assert(rebooted.reserve("new", result) == Decision::Rejected);
    storage.records[1] = "CC3";  // A truncated record must fail equally safely.
    Cache truncated(storage);
    assert(!truncated.begin());
    assert(truncated.reserve("new", result) == Decision::Rejected);
}

void readErrorAndResultLimits() {
    MemoryStorage storage;
    storage.failReadSlot = 4;
    Cache unavailable(storage);
    std::string result;
    assert(!unavailable.begin());
    assert(unavailable.reserve("new", result) == Decision::Rejected);
    storage.failReadSlot = -1;
    Cache cache(storage);
    assert(cache.begin());
    assert(cache.reserve("", result) == Decision::Rejected);
    assert(cache.reserve(std::string("id\0suffix", 9), result) == Decision::Rejected);
    assert(cache.reserve(std::string(Cache::MAX_ID_BYTES + 1, 'a'), result) == Decision::Rejected);
    assert(cache.available());
    assert(cache.reserve("too-large", result) == Decision::Execute);
    assert(!cache.complete("too-large", std::string(Cache::MAX_RESULT_BYTES + 1, 'x')));
    assert(!cache.available());
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("too-large", result) == Decision::Uncertain);
    assert(!rebooted.complete("", "must not create a record without ID"));
    assert(!rebooted.available());
}

void ringRecoversNewestSlotWithoutSeparateMetadata() {
    MemoryStorage storage;
    std::string result;
    for (unsigned int command = 0; command < 80; ++command) {
        Cache cache(storage);  // Restart between commands, including ring wraps.
        assert(cache.begin());
        const auto id = std::to_string(command);
        assert(cache.reserve(id, result) == Decision::Execute);
        assert(cache.complete(id, "result-" + id));
    }
    assert(storage.records.size() == Cache::SLOT_COUNT);
    Cache cache(storage);
    assert(cache.begin());
    for (unsigned int command = 64; command < 80; ++command) {
        const auto id = std::to_string(command);
        assert(cache.reserve(id, result) == Decision::Replay);
        assert(result == "result-" + id);
    }
    // The documented bounded cache no longer remembers the oldest command.
    assert(cache.reserve("0", result) == Decision::Execute);
}

void retryCannotExecuteAnEvictedCommand() {
    MemoryStorage storage;
    Cache cache(storage);
    std::string result;
    assert(cache.begin());
    for (unsigned int command = 0; command <= Cache::SLOT_COUNT; ++command) {
        const auto id = std::to_string(command);
        assert(cache.reserve(id, result) == Decision::Execute);
        assert(cache.complete(id, "result-" + id));
    }
    const auto writes = storage.writes;
    assert(cache.reserve("0", result, false) == Decision::Uncertain);
    assert(result.empty());
    assert(storage.writes == writes);
    assert(cache.reserve("16", result, false) == Decision::Replay);
    assert(result == "result-16");
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("0", result, false) == Decision::Uncertain);
    assert(storage.writes == writes);
    // Only a deliberately new ID may start another action.
    assert(rebooted.reserve("new-request", result) == Decision::Execute);
}

void unresolvedIntentIsNotEvictedByCompletedCommands() {
    MemoryStorage storage;
    std::string result;
    {
        Cache cache(storage);
        assert(cache.begin());
        assert(cache.reserve("unknown-pulse", result) == Decision::Execute);
    }
    for (unsigned int command = 0; command < 80; ++command) {
        Cache cache(storage);
        assert(cache.begin());
        const auto id = std::to_string(command);
        assert(cache.reserve(id, result) == Decision::Execute);
        assert(cache.complete(id, "result-" + id));
        assert(cache.reserve("unknown-pulse", result) == Decision::Uncertain);
    }
    assert(storage.records.size() == Cache::SLOT_COUNT);
    Cache rebooted(storage);
    assert(rebooted.begin());
    assert(rebooted.reserve("unknown-pulse", result) == Decision::Uncertain);
}

void fullUnresolvedCacheRejectsWithoutOverwriting() {
    MemoryStorage storage;
    Cache cache(storage);
    std::string result;
    assert(cache.begin());
    for (unsigned int command = 0; command < Cache::SLOT_COUNT; ++command) {
        assert(cache.reserve(std::to_string(command), result) == Decision::Execute);
    }
    const auto records = storage.records;
    const auto writes = storage.writes;
    assert(cache.reserve("no-space", result) == Decision::Rejected);
    assert(storage.writes == writes && storage.records == records);
    // A caller can still complete an in-flight intent and free an evictable
    // slot. Saturation is not a storage failure that should disable writes.
    assert(cache.complete("0", "completed"));
    assert(cache.reserve("new", result) == Decision::Execute);
    for (unsigned int command = 1; command < Cache::SLOT_COUNT; ++command) {
        assert(cache.reserve(std::to_string(command), result) == Decision::Uncertain);
    }
}

int main() {
    duplicateAndReboot();
    interruptedExecutionNeverRepeats();
    intentFailureBlocksActions(false);
    intentFailureBlocksActions(true);
    finalWriteFailureKeepsIntent(false);
    finalWriteFailureKeepsIntent(true);
    failedStorageProbeStillReplaysKnownResults();
    invalidRecordBlocksNewActions();
    readErrorAndResultLimits();
    ringRecoversNewestSlotWithoutSeparateMetadata();
    retryCannotExecuteAnEvictedCommand();
    unresolvedIntentIsNotEvictedByCompletedCommands();
    fullUnresolvedCacheRejectsWithoutOverwriting();
    std::cout << "13 durable command cache scenarios passed\n";
}
