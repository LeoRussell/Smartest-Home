#pragma once

#include <cstddef>
#include <cstdint>

namespace UniversalPwm {
constexpr uint8_t NO_CHANNEL = 255;
constexpr uint8_t MAX_CHANNELS = 8;

struct Route {
    uint8_t pin = 0;
    uint8_t channel = NO_CHANNEL;
    bool enabled = false;
};

// Compute the complete route plan before changing GPIO or saving a new config.
// A retained pin keeps its channel even when its component moves in the array.
inline bool plan(Route* next, size_t count, const Route* previous,
                 size_t previousCount, uint8_t capacity, bool* retained) {
    if (capacity == 0 || capacity > MAX_CHANNELS) return false;
    bool used[MAX_CHANNELS] = {};
    for (size_t index = 0; index < count; ++index) {
        retained[index] = false;
        next[index].channel = NO_CHANNEL;
        if (!next[index].enabled) continue;
        for (size_t earlier = 0; earlier < index; ++earlier) {
            if (next[earlier].enabled && next[earlier].pin == next[index].pin) return false;
        }
        for (size_t oldIndex = 0; oldIndex < previousCount; ++oldIndex) {
            const Route& old = previous[oldIndex];
            if (!old.enabled || old.pin != next[index].pin) continue;
            if (old.channel >= capacity || used[old.channel]) return false;
            next[index].channel = old.channel;
            retained[index] = true;
            used[old.channel] = true;
            break;
        }
    }
    for (size_t index = 0; index < count; ++index) {
        if (!next[index].enabled || retained[index]) continue;
        uint8_t channel = 0;
        while (channel < capacity && used[channel]) ++channel;
        if (channel == capacity) return false;
        next[index].channel = channel;
        used[channel] = true;
    }
    return true;
}
}  // namespace UniversalPwm
