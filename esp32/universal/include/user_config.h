#pragma once

// Assign a unique ID to each universal ESP32 before flashing another board.
#ifndef UNIVERSAL_DEVICE_ID
#if defined(CONFIG_IDF_TARGET_ESP32C3) && CONFIG_IDF_TARGET_ESP32C3
#define UNIVERSAL_DEVICE_ID "esp32-c3-universal-01"
#else
#define UNIVERSAL_DEVICE_ID "esp32-universal-01"
#endif
#endif

#ifndef UNIVERSAL_DEVICE_NAME
#if defined(CONFIG_IDF_TARGET_ESP32C3) && CONFIG_IDF_TARGET_ESP32C3
#define UNIVERSAL_DEVICE_NAME "ESP32-C3 SuperMini"
#else
#define UNIVERSAL_DEVICE_NAME "ESP32-S3"
#endif
#endif
