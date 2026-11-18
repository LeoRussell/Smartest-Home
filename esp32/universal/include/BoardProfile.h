#pragma once

#include <cstdint>

// These are conservative firmware profiles, not an enumeration of every pin
// available on the chip. Keep them in step with gateway/device_configuration.py.
namespace UniversalBoard {
#if defined(CONFIG_IDF_TARGET_ESP32C3) && CONFIG_IDF_TARGET_ESP32C3
constexpr char PROFILE_ID[] = "esp32-c3-supermini";
constexpr uint8_t PWM_CHANNELS = 6;

inline bool allowedPin(uint8_t pin) {
    // Keep flash, native USB, UART and strapping pins away from components.
    return pin == 0 || pin == 1 || (pin >= 3 && pin <= 7) || pin == 10;
}

inline bool allowedAdcPin(uint8_t pin) {
    // ADC1 only. GPIO2 is a strapping pin; ADC2/GPIO5 isn't a stable ADC source.
    return pin == 0 || pin == 1 || pin == 3 || pin == 4;
}

inline bool allowedIdentifyGpioPin(uint8_t pin) {
    // GPIO8 is reserved for the explicitly configured SuperMini indicator.
    return allowedPin(pin) || pin == 8;
}

inline bool allowedIdentifyNeoPixelPin(uint8_t pin) {
    (void)pin;
    return false;
}
#elif defined(CONFIG_IDF_TARGET_ESP32S3) && CONFIG_IDF_TARGET_ESP32S3
constexpr char PROFILE_ID[] = "esp32-s3-devkitc-1";
constexpr uint8_t PWM_CHANNELS = 8;

inline bool allowedPin(uint8_t pin) {
    return pin == 1 || pin == 2 || (pin >= 4 && pin <= 18) || pin == 21;
}

inline bool allowedAdcPin(uint8_t pin) {
    return pin == 1 || pin == 2 || (pin >= 4 && pin <= 10);
}

inline bool allowedIdentifyGpioPin(uint8_t pin) {
    return allowedPin(pin);
}

inline bool allowedIdentifyNeoPixelPin(uint8_t pin) {
    return pin == 38 || pin == 48;
}
#else
#error "The universal firmware supports ESP32-S3 and ESP32-C3 targets only."
#endif
}  // namespace UniversalBoard
