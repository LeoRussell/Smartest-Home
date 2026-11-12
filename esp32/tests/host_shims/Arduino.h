#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

// Only String/console operations used by CommandResultCache. This does not
// model an ESP32 loop, GPIO, timing, Wi-Fi, flash wear or power-loss behavior.
class String {
public:
    String() = default;
    String(const char* text) : value_(text == nullptr ? "" : text) {}
    String& operator=(const char* text) {
        value_ = text == nullptr ? "" : text;
        return *this;
    }
    const char* c_str() const { return value_.c_str(); }
    size_t length() const { return value_.size(); }
    bool isEmpty() const { return value_.empty(); }
    bool reserve(size_t size) { value_.reserve(size); return true; }
    bool concat(const char* text) { value_ += text; return true; }

private:
    std::string value_;
};

struct HostSerial {
    void println(const char*) const {}
    template <typename... Args> void printf(const char*, Args...) const {}
};
extern HostSerial Serial;
