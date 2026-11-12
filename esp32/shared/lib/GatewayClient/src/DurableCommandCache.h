#pragma once

#include <cstdint>
#include <cstddef>
#include <limits>
#include <string>

// Independent of Arduino so the exact persistence/recovery logic can also run
// against an in-memory, fault-injecting storage implementation on a host.
class DurableCommandCache {
public:
    static constexpr uint8_t SLOT_COUNT = 16;
    static constexpr size_t MAX_ID_BYTES = 320;  // Up to 80 UTF-8 code points.
    static constexpr size_t MAX_RESULT_BYTES = 2048;
    static constexpr size_t MAX_RECORD_BYTES = 19 + MAX_ID_BYTES + MAX_RESULT_BYTES;

    enum class ReadStatus { Missing, Present, Error };
    enum class Decision { Execute, Replay, Uncertain, Rejected };

    class Storage {
    public:
        virtual ~Storage() = default;
        virtual ReadStatus read(uint8_t slot, std::string& record) = 0;
        // Success includes commit and readback verification of this single key.
        virtual bool write(uint8_t slot, const std::string& record) = 0;
        virtual bool verifyWritable() = 0;
    };

    explicit DurableCommandCache(Storage& storage) : storage_(storage) {}

    bool begin() {
        writable_ = false;
        nextSlot_ = 0;
        sequence_ = 0;
        bool valid = true;
        for (uint8_t slot = 0; slot < SLOT_COUNT; ++slot) {
            entries_[slot] = Entry{};
            std::string record;
            const ReadStatus status = storage_.read(slot, record);
            if (status == ReadStatus::Missing) continue;
            if (status != ReadStatus::Present || !decode(record, entries_[slot])) {
                valid = false;
                continue;
            }
            for (uint8_t previous = 0; previous < slot; ++previous) {
                if (!entries_[previous].id.empty() &&
                    (entries_[previous].id == entries_[slot].id ||
                     entries_[previous].sequence == entries_[slot].sequence)) {
                    valid = false;
                }
            }
            if (entries_[slot].sequence > sequence_) {
                sequence_ = entries_[slot].sequence;
                nextSlot_ = (slot + 1) % SLOT_COUNT;
            }
        }
        writable_ = valid && storage_.verifyWritable();
        return writable_;
    }

    bool available() const { return writable_; }
    void disable() { writable_ = false; }

    Decision reserve(const std::string& id, std::string& result, bool allowExecution = true) {
        result.clear();
        if (!validId(id)) return Decision::Rejected;
        for (const Entry& entry : entries_) {
            if (entry.id != id) continue;
            result = entry.result;
            return result.empty() ? Decision::Uncertain : Decision::Replay;
        }
        // A retry must never turn an evicted result into a new hardware action.
        // The sender may have lost an ACK while newer commands filled the ring.
        if (!allowExecution) return Decision::Uncertain;
        if (!writable_ || sequence_ == std::numeric_limits<uint64_t>::max()) {
            writable_ = false;
            return Decision::Rejected;
        }
        // Pending records can represent an action interrupted by power loss.
        // Keep those intents until explicitly completed; evicting one would
        // allow a later delivery of its ID to execute an unknown action again.
        uint8_t target = nextSlot_;
        uint8_t examined = 0;
        while (examined < SLOT_COUNT && !entries_[target].id.empty() &&
               entries_[target].result.empty()) {
            target = (target + 1) % SLOT_COUNT;
            ++examined;
        }
        if (examined == SLOT_COUNT) return Decision::Rejected;
        Entry pending;
        pending.sequence = sequence_ + 1;
        pending.id = id;
        // The intent must be durable BEFORE the caller can touch hardware.
        if (!storage_.write(target, encode(pending))) {
            writable_ = false;
            return Decision::Rejected;
        }
        entries_[target] = pending;
        sequence_ = pending.sequence;
        nextSlot_ = (target + 1) % SLOT_COUNT;
        return Decision::Execute;
    }

    bool complete(const std::string& id, const std::string& result) {
        if (!validId(id)) {
            writable_ = false;
            return false;
        }
        for (uint8_t slot = 0; slot < SLOT_COUNT; ++slot) {
            Entry& entry = entries_[slot];
            if (entry.id != id) continue;
            if (!entry.result.empty()) {
                if (entry.result == result) return true;
                writable_ = false;
                return false;
            }
            if (result.empty() || result.size() > MAX_RESULT_BYTES) {
                writable_ = false;
                return false;
            }
            Entry completed = entry;
            completed.result = result;
            // Never expose a successful cached result before checked storage
            // completion. Failed writes keep the conservative intent in RAM.
            if (!writable_ || !storage_.write(slot, encode(completed))) {
                writable_ = false;
                return false;
            }
            entry = completed;
            return true;
        }
        writable_ = false;
        return false;
    }

private:
    struct Entry {
        uint64_t sequence = 0;
        std::string id;
        std::string result;
    };

    static bool validId(const std::string& id) {
        return !id.empty() && id.size() <= MAX_ID_BYTES &&
               id.find('\0') == std::string::npos;
    }

    static void appendInteger(std::string& bytes, uint64_t value, uint8_t count) {
        for (uint8_t index = 0; index < count; ++index) {
            bytes.push_back(static_cast<char>(value & 0xff));
            value >>= 8;
        }
    }

    static uint64_t readInteger(const std::string& bytes, size_t offset, uint8_t count) {
        uint64_t value = 0;
        for (uint8_t index = 0; index < count; ++index) {
            value |= static_cast<uint64_t>(
                static_cast<unsigned char>(bytes[offset + index])) << (index * 8);
        }
        return value;
    }

    static uint32_t checksum(const std::string& bytes, size_t length) {
        uint32_t crc = 0xffffffff;
        for (size_t index = 0; index < length; ++index) {
            crc ^= static_cast<unsigned char>(bytes[index]);
            for (uint8_t bit = 0; bit < 8; ++bit) {
                crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
            }
        }
        return ~crc;
    }

    static std::string encode(const Entry& entry) {
        std::string record("CC3", 3);
        appendInteger(record, entry.sequence, 8);
        appendInteger(record, entry.id.size(), 2);
        appendInteger(record, entry.result.size(), 2);
        record += entry.id;
        record += entry.result;
        appendInteger(record, checksum(record, record.size()), 4);
        return record;
    }

    static bool decode(const std::string& record, Entry& entry) {
        if (record.size() < 20 || record.size() > MAX_RECORD_BYTES ||
            record.compare(0, 3, "CC3") != 0) return false;
        const size_t idLength = readInteger(record, 11, 2);
        const size_t resultLength = readInteger(record, 13, 2);
        if (idLength == 0 || idLength > MAX_ID_BYTES ||
            resultLength > MAX_RESULT_BYTES ||
            record.size() != 19 + idLength + resultLength ||
            readInteger(record, record.size() - 4, 4) !=
                checksum(record, record.size() - 4)) return false;
        Entry parsed;
        parsed.sequence = readInteger(record, 3, 8);
        parsed.id = record.substr(15, idLength);
        parsed.result = record.substr(15 + idLength, resultLength);
        if (parsed.sequence == 0 || !validId(parsed.id)) return false;
        entry = parsed;
        return true;
    }

    Storage& storage_;
    Entry entries_[SLOT_COUNT];
    uint64_t sequence_ = 0;
    uint8_t nextSlot_ = 0;
    bool writable_ = false;
};
