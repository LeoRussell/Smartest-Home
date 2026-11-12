#pragma once

#include <cstddef>
#include <cstdint>

using esp_err_t = int;
using nvs_handle_t = uint32_t;
constexpr esp_err_t ESP_OK = 0;
constexpr esp_err_t ESP_FAIL = -1;
constexpr esp_err_t ESP_ERR_NVS_NOT_FOUND = 1;
constexpr esp_err_t ESP_ERR_NVS_TYPE_MISMATCH = 2;
constexpr esp_err_t ESP_ERR_NVS_INVALID_LENGTH = 3;
constexpr int NVS_READWRITE = 1;

esp_err_t nvs_open(const char*, int, nvs_handle_t*);
void nvs_close(nvs_handle_t);
esp_err_t nvs_get_blob(nvs_handle_t, const char*, void*, size_t*);
esp_err_t nvs_set_blob(nvs_handle_t, const char*, const void*, size_t);
esp_err_t nvs_get_str(nvs_handle_t, const char*, char*, size_t*);
esp_err_t nvs_set_str(nvs_handle_t, const char*, const char*);
esp_err_t nvs_get_u8(nvs_handle_t, const char*, uint8_t*);
esp_err_t nvs_set_u8(nvs_handle_t, const char*, uint8_t);
esp_err_t nvs_erase_key(nvs_handle_t, const char*);
esp_err_t nvs_commit(nvs_handle_t);
const char* esp_err_to_name(esp_err_t);
