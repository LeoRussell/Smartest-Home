#pragma once

// Give each physical board its own ID before flashing multiple boards.
#ifndef DEVICE_ID
#define DEVICE_ID "esp32-01"
#endif

#ifndef DEVICE_NAME
#define DEVICE_NAME "ESP32-S3"
#endif

#ifndef DEVICE_MODEL
#define DEVICE_MODEL "ESP32-S3 development board"
#endif

#define DEVICE_LED_DRIVER_GPIO 0
#define DEVICE_LED_DRIVER_NEOPIXEL 1

// The default profile drives a plain LED directly from GPIO 48. Select a
// NeoPixel profile only for a board whose indicator is addressable RGB.
#ifndef DEVICE_LED_DRIVER
#define DEVICE_LED_DRIVER DEVICE_LED_DRIVER_GPIO
#endif

#ifndef LED_PIN
#define LED_PIN 48
#endif

#ifndef LED_ACTIVE_LEVEL
#define LED_ACTIVE_LEVEL HIGH
#endif

#ifndef LED_BRIGHTNESS
#define LED_BRIGHTNESS 96
#endif

#ifndef IDENTIFY_PULSE_COUNT
#define IDENTIFY_PULSE_COUNT 5
#endif

#ifndef IDENTIFY_PULSE_DURATION_MS
#define IDENTIFY_PULSE_DURATION_MS 250
#endif

#ifndef DEVICE_HAS_IDENTIFY_INDICATOR
#define DEVICE_HAS_IDENTIFY_INDICATOR 1
#endif

#if DEVICE_LED_DRIVER != DEVICE_LED_DRIVER_GPIO && \
    DEVICE_LED_DRIVER != DEVICE_LED_DRIVER_NEOPIXEL
#error "Unsupported DEVICE_LED_DRIVER value"
#endif
