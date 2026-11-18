#include <cassert>
#include <cstring>
#include <iostream>

#include "BoardProfile.h"
#include "PwmChannels.h"
#include "user_config.h"

#ifdef NDEBUG
#error "Native firmware checks require enabled assertions."
#endif

namespace {
UniversalPwm::Route route(uint8_t pin, uint8_t channel, bool enabled = true) {
    UniversalPwm::Route result;
    result.pin = pin;
    result.channel = channel;
    result.enabled = enabled;
    return result;
}

void checkFreshMixedComponents() {
    // PWM components can occur in slots 6/7 even though C3 has only channels0..5.
    UniversalPwm::Route next[8];
    next[6] = route(3, UniversalPwm::NO_CHANNEL);
    next[7] = route(4, UniversalPwm::NO_CHANNEL);
    bool retained[8] = {};
    assert(UniversalPwm::plan(next, 8, nullptr, 0, UniversalBoard::PWM_CHANNELS, retained));
    assert(next[6].channel == 0 && next[7].channel == 1);
    assert(!retained[6] && !retained[7]);
    for (uint8_t index = 0; index < 6; ++index) assert(next[index].channel == UniversalPwm::NO_CHANNEL);
}

void checkRetainedRoutesAndReuse() {
    UniversalPwm::Route previous[3] = {route(3, 0), route(4, 1), route(5, 2)};
    UniversalPwm::Route next[4] = {route(7, UniversalPwm::NO_CHANNEL), route(5, UniversalPwm::NO_CHANNEL),
                                    route(3, UniversalPwm::NO_CHANNEL), route(6, UniversalPwm::NO_CHANNEL, false)};
    bool retained[4] = {};
    assert(UniversalPwm::plan(next, 4, previous, 3, UniversalBoard::PWM_CHANNELS, retained));
    // Pin4's channel is free. The new first component must not steal channels
    // still owned by pins5/3, even though those appear later in the new array.
    assert(next[0].channel == 1 && !retained[0]);
    assert(next[1].channel == 2 && retained[1]);
    assert(next[2].channel == 0 && retained[2]);
    assert(next[3].channel == UniversalPwm::NO_CHANNEL && !retained[3]);
    assert(previous[0].channel == 0 && previous[1].channel == 1 && previous[2].channel == 2);
}

void checkCapacityBeforeHardwareWork() {
    UniversalPwm::Route next[8];
    bool retained[8] = {};
    for (uint8_t index = 0; index < 8; ++index) next[index] = route(index, UniversalPwm::NO_CHANNEL);
    assert(UniversalPwm::plan(next, UniversalBoard::PWM_CHANNELS, nullptr, 0, UniversalBoard::PWM_CHANNELS, retained));
    for (uint8_t index = 0; index < UniversalBoard::PWM_CHANNELS; ++index) assert(next[index].channel == index);
    assert(!UniversalPwm::plan(next, 8, nullptr, 0, 6, retained));
    assert(!UniversalPwm::plan(next, 1, nullptr, 0, 0, retained));
    assert(!UniversalPwm::plan(next, 1, nullptr, 0, 9, retained));
}

void checkRejectedConflictingRoutes() {
    bool retained[2] = {};
    UniversalPwm::Route duplicate[2] = {route(4, UniversalPwm::NO_CHANNEL), route(4, UniversalPwm::NO_CHANNEL)};
    assert(!UniversalPwm::plan(duplicate, 2, nullptr, 0, UniversalBoard::PWM_CHANNELS, retained));
    UniversalPwm::Route old[2] = {route(4, 0), route(5, 0)};
    UniversalPwm::Route next[2] = {route(4, UniversalPwm::NO_CHANNEL), route(5, UniversalPwm::NO_CHANNEL)};
    assert(!UniversalPwm::plan(next, 2, old, 2, UniversalBoard::PWM_CHANNELS, retained));
    old[1].channel = UniversalBoard::PWM_CHANNELS;
    assert(!UniversalPwm::plan(next, 2, old, 2, UniversalBoard::PWM_CHANNELS, retained));
}

void checkPinsAndIdentity() {
#if CONFIG_IDF_TARGET_ESP32C3
    assert(std::strcmp(UniversalBoard::PROFILE_ID, "esp32-c3-supermini") == 0);
    assert(std::strcmp(UNIVERSAL_DEVICE_ID, "esp32-c3-universal-01") == 0);
    assert(std::strcmp(UNIVERSAL_DEVICE_NAME, "ESP32-C3 SuperMini") == 0);
    assert(UniversalBoard::PWM_CHANNELS == 6);
    for (uint8_t pin : {0, 1, 3, 4, 5, 6, 7, 10}) assert(UniversalBoard::allowedPin(pin));
    for (uint8_t pin : {2, 8, 9, 11, 12, 17, 18, 19, 20, 21, 38, 48}) assert(!UniversalBoard::allowedPin(pin));
    for (uint8_t pin : {0, 1, 3, 4}) assert(UniversalBoard::allowedAdcPin(pin));
    for (uint8_t pin : {2, 5, 6, 7, 8, 9, 10}) assert(!UniversalBoard::allowedAdcPin(pin));
    assert(UniversalBoard::allowedIdentifyGpioPin(8));
    assert(!UniversalBoard::allowedIdentifyGpioPin(9));
    for (uint8_t pin : {8, 38, 48}) assert(!UniversalBoard::allowedIdentifyNeoPixelPin(pin));
#else
    assert(std::strcmp(UniversalBoard::PROFILE_ID, "esp32-s3-devkitc-1") == 0);
    assert(std::strcmp(UNIVERSAL_DEVICE_ID, "esp32-universal-01") == 0);
    assert(std::strcmp(UNIVERSAL_DEVICE_NAME, "ESP32-S3") == 0);
    assert(UniversalBoard::PWM_CHANNELS == 8);
    for (uint8_t pin : {1, 2, 4, 10, 18, 21}) assert(UniversalBoard::allowedPin(pin));
    for (uint8_t pin : {0, 3, 19, 20, 38, 45, 46, 48}) assert(!UniversalBoard::allowedPin(pin));
    for (uint8_t pin : {1, 2, 4, 10}) assert(UniversalBoard::allowedAdcPin(pin));
    assert(!UniversalBoard::allowedAdcPin(11));
    assert(UniversalBoard::allowedIdentifyNeoPixelPin(38));
    assert(UniversalBoard::allowedIdentifyNeoPixelPin(48));
#endif
}
}  // namespace

int main() {
    checkPinsAndIdentity();
    checkFreshMixedComponents();
    checkRetainedRoutesAndReuse();
    checkCapacityBeforeHardwareWork();
    checkRejectedConflictingRoutes();
    std::cout << "5 universal board and PWM scenarios passed: " << UniversalBoard::PROFILE_ID << '\n';
}
