#pragma once

// Give each physical board its own ID before flashing multiple boards.
#ifndef DEVICE_ID
#define DEVICE_ID "esp32-01"
#endif

#ifndef DEVICE_NAME
#define DEVICE_NAME "ESP32-S3"
#endif

#ifndef DEVICE_MODEL
#define DEVICE_MODEL "Adafruit Feather ESP32-S3"
#endif

// Board-specific hardware mapping for the current test device.
// Set DEVICE_HAS_IDENTIFY_INDICATOR to 0 when the board has no wired indicator.
#ifndef LED_PIN
#define LED_PIN 48
#endif
#ifndef IDENTIFY_PIN
#define IDENTIFY_PIN 48
#endif
#ifndef DEVICE_HAS_IDENTIFY_INDICATOR
#define DEVICE_HAS_IDENTIFY_INDICATOR 1
#endif
