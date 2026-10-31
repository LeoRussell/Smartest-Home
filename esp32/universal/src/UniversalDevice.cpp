#include "UniversalDevice.h"

#include <cctype>
#include <cstring>
#include <mbedtls/sha256.h>
#include <esp_arduino_version.h>

#if defined(__has_include)
#if __has_include("device_configuration.generated.h")
#include "device_configuration.generated.h"
#define HAS_EMBEDDED_DEVICE_CONFIGURATION 1
#endif
#endif

namespace {
constexpr unsigned long HEARTBEAT_INTERVAL_MS = 10000;

uint32_t readConfigurationVersion(JsonVariantConst value) {
    return value.is<uint32_t>() && !value.is<bool>() ? value.as<uint32_t>() : 0;
}

void configurePwm(uint8_t pin, uint8_t channel, uint32_t value) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttachChannel(pin, 1000, 8, channel);
    ledcWriteChannel(channel, value);
#else
    ledcSetup(channel, 1000, 8);
    ledcAttachPin(pin, channel);
    ledcWrite(channel, value);
#endif
}

void writePwm(uint8_t channel, uint32_t value) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWriteChannel(channel, value);
#else
    ledcWrite(channel, value);
#endif
}

void detachPwm(uint8_t pin) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcDetach(pin);
#else
    ledcDetachPin(pin);
#endif
}

bool containsControlCharacters(JsonString value) {
    const unsigned char* bytes = reinterpret_cast<const unsigned char*>(value.c_str());
    for (size_t index = 0; index < value.size(); ++index) {
        if (bytes[index] < 0x20 || bytes[index] == 0x7F ||
            (bytes[index] == 0xC2 && index + 1 < value.size() &&
             bytes[index + 1] >= 0x80 && bytes[index + 1] <= 0x9F)) {
            return true;
        }
    }
    return false;
}

bool copyString(JsonVariantConst value, char* destination, size_t capacity,
                bool allowEmpty = false) {
    if (!value.is<const char*>()) return false;
    const JsonString source = value.as<JsonString>();
    const size_t length = source.size();
    if ((!allowEmpty && length == 0) || length >= capacity ||
        containsControlCharacters(source)) return false;
    memcpy(destination, source.c_str(), length);
    destination[length] = '\0';
    return true;
}

bool isBool(JsonVariantConst value) {
    return value.is<bool>();
}

bool isInteger(JsonVariantConst value) {
    return value.is<int>() && !value.is<bool>();
}

bool isAllowedIdCharacter(char value, bool first) {
    return (value >= 'A' && value <= 'Z') ||
           (value >= 'a' && value <= 'z') ||
           (!first && ((value >= '0' && value <= '9') || value == '_' ||
                       value == '.' || value == '-'));
}

bool calculateConfigurationHash(JsonVariantConst configuration, String& hash) {
    String canonical;
    serializeJson(configuration, canonical);

    uint8_t digest[32];
    if (mbedtls_sha256_ret(
            reinterpret_cast<const unsigned char*>(canonical.c_str()),
            canonical.length(), digest, 0) != 0) {
        return false;
    }

    static constexpr char HEX_DIGITS[] = "0123456789abcdef";
    char encoded[65];
    for (size_t index = 0; index < sizeof(digest); ++index) {
        encoded[index * 2] = HEX_DIGITS[digest[index] >> 4];
        encoded[index * 2 + 1] = HEX_DIGITS[digest[index] & 0x0F];
    }
    encoded[64] = '\0';
    hash = encoded;
    return true;
}
}  // namespace

bool UniversalDevice::begin() {
    analogReadResolution(12);
    if (!preferences_.begin("device_cfg", false)) {
        Serial.println("[CONFIG] NVS could not be opened; GPIO remains inactive.");
        return false;
    }

    const String savedEnvelope = preferences_.getString("envelope", "");
    if (savedEnvelope.length() == 0) {
        String error;
        if (!loadEmbeddedConfiguration(error) && !error.isEmpty()) {
            Serial.printf("[CONFIG] Embedded configuration rejected: %s\n", error.c_str());
            return false;
        }
        return true;
    }

    JsonDocument envelope;
    if (savedEnvelope.length() > MAX_CONFIGURATION_BYTES + 256 ||
        deserializeJson(envelope, savedEnvelope)) {
        Serial.println("[CONFIG] Stored configuration is invalid; GPIO remains inactive.");
        configurationVersion_ = 0;
        configurationHash_ = "";
        return false;
    }
    configurationVersion_ = readConfigurationVersion(envelope["version"]);
    configurationHash_ = envelope["hash"] | "";
    JsonVariantConst storedConfiguration = envelope["configuration"];
    if (configurationVersion_ == 0 || configurationHash_.length() != 64) {
        Serial.println("[CONFIG] Stored configuration envelope is incomplete; GPIO remains inactive.");
        configurationVersion_ = 0;
        configurationHash_ = "";
        return false;
    }
    String calculatedHash;
    if (!calculateConfigurationHash(storedConfiguration, calculatedHash) ||
        calculatedHash != configurationHash_) {
        Serial.println("[CONFIG] Stored configuration hash does not match; GPIO remains inactive.");
        configurationVersion_ = 0;
        configurationHash_ = "";
        return false;
    }
    for (size_t index = 0; index < configurationHash_.length(); ++index) {
        const char character = configurationHash_.charAt(index);
        if (!isxdigit(static_cast<unsigned char>(character))) {
            Serial.println("[CONFIG] Stored configuration hash is invalid; GPIO remains inactive.");
            configurationVersion_ = 0;
            configurationHash_ = "";
            return false;
        }
    }

    String error;
    Component parsed[MAX_COMPONENTS];
    uint8_t parsedCount = 0;
    IdentifyIndicator parsedIdentify;
    if (!parseAndValidate(storedConfiguration, parsed, parsedCount, parsedIdentify, error)) {
        Serial.printf("[CONFIG] Stored configuration rejected: %s\n", error.c_str());
        configurationVersion_ = 0;
        configurationHash_ = "";
        return false;
    }

    memcpy(components_, parsed, sizeof(components_));
    componentCount_ = parsedCount;
    identifyIndicator_ = parsedIdentify;
    configured_ = true;
    configurePins();
    Serial.printf("[CONFIG] Loaded version %lu with %u components.\n",
                  static_cast<unsigned long>(configurationVersion_),
                  static_cast<unsigned int>(componentCount_));
    return true;
}

bool UniversalDevice::loadEmbeddedConfiguration(String& error) {
#if defined(HAS_EMBEDDED_DEVICE_CONFIGURATION)
    JsonDocument configuration;
    if (deserializeJson(configuration, UNIVERSAL_GENERATED_CONFIGURATION_JSON)) {
        error = "generated configuration is not valid JSON";
        return false;
    }

    String hash;
    JsonVariantConst value = configuration.as<JsonVariantConst>();
    if (!calculateConfigurationHash(value, hash)) {
        error = "generated configuration hash could not be calculated";
        return false;
    }
    const uint32_t version = UNIVERSAL_GENERATED_CONFIGURATION_VERSION;
    if (version < configurationVersion_ ||
        (version == configurationVersion_ && hash == configurationHash_)) {
        return true;
    }
    if (!applyConfiguration(value, version, hash.c_str(), error)) return false;
    Serial.printf("[CONFIG] Loaded generated configuration v%lu.\n",
                  static_cast<unsigned long>(version));
#else
    (void)error;
#endif
    return true;
}

uint32_t UniversalDevice::configurationVersion() const {
    return configurationVersion_;
}

bool UniversalDevice::isAllowedPin(uint8_t pin) const {
    // Conservative DevKitC-1 profile: omit strapping, USB, flash/PSRAM and
    // both possible onboard RGB LED pins (GPIO38 and GPIO48).
    return pin == 1 || pin == 2 || (pin >= 4 && pin <= 18) || pin == 21;
}

bool UniversalDevice::isAllowedAdcPin(uint8_t pin) const {
    return pin == 1 || pin == 2 || (pin >= 4 && pin <= 10);
}

bool UniversalDevice::validLogicalId(const char* value) const {
    if (value == nullptr || value[0] == '\0') return false;
    size_t length = 0;
    while (value[length] != '\0' && length <= 64) {
        if (!isAllowedIdCharacter(value[length], length == 0)) return false;
        ++length;
    }
    return length > 0 && length <= 64;
}

bool UniversalDevice::keyIsAllowed(const char* key, Driver driver) const {
    if (strcmp(key, "id") == 0 || strcmp(key, "driver") == 0 ||
        strcmp(key, "label") == 0 || strcmp(key, "parameter_id") == 0 ||
        strcmp(key, "parameter_label") == 0 || strcmp(key, "pin") == 0) {
        return true;
    }
    switch (driver) {
        case Driver::GpioOutput:
            return strcmp(key, "active_high") == 0 || strcmp(key, "initial_value") == 0;
        case Driver::DigitalInput:
            return strcmp(key, "pull") == 0 || strcmp(key, "interval_ms") == 0;
        case Driver::AnalogInput:
            return strcmp(key, "interval_ms") == 0 || strcmp(key, "unit") == 0 ||
                   strcmp(key, "raw_min") == 0 || strcmp(key, "raw_max") == 0 ||
                   strcmp(key, "value_min") == 0 || strcmp(key, "value_max") == 0;
        case Driver::PwmOutput:
            return strcmp(key, "initial_value") == 0 ||
                   strcmp(key, "min_value") == 0 ||
                   strcmp(key, "max_value") == 0;
    }
    return false;
}

bool UniversalDevice::parseAndValidate(
    JsonVariantConst configuration,
    Component* output,
    uint8_t& count,
    IdentifyIndicator& identify,
    String& error
) const {
    if (!configuration.is<JsonObjectConst>()) {
        error = "configuration must be an object";
        return false;
    }
    JsonObjectConst root = configuration.as<JsonObjectConst>();
    if (root.size() < 2 || root.size() > 3 || root["format_version"].as<int>() != 1 ||
        !isInteger(root["format_version"]) || !root["components"].is<JsonArrayConst>()) {
        error = "format_version 1 and a components array are required";
        return false;
    }
    for (JsonPairConst pair : root) {
        const char* key = pair.key().c_str();
        if (containsControlCharacters(pair.key()) ||
            (strcmp(key, "format_version") != 0 && strcmp(key, "components") != 0 &&
             strcmp(key, "identify_indicator") != 0)) {
            error = "configuration contains an unsupported field";
            return false;
        }
    }

    identify = IdentifyIndicator{};
    JsonVariantConst identifyValue = root["identify_indicator"];
    if (!identifyValue.isUnbound() && !identifyValue.isNull()) {
        if (!identifyValue.is<JsonObjectConst>()) {
            error = "identify_indicator must be an object or null";
            return false;
        }
        JsonObjectConst indicator = identifyValue.as<JsonObjectConst>();
        for (JsonPairConst pair : indicator) {
            if (containsControlCharacters(pair.key()) ||
                (pair.value().is<const char*>() &&
                 containsControlCharacters(pair.value().as<JsonString>()))) {
                error = "identify indicator contains a control character";
                return false;
            }
        }
        const char* driver = indicator["driver"] | "";
        JsonVariantConst builtInValue = indicator["built_in"];
        if (!builtInValue.isUnbound() && !isBool(builtInValue)) {
            error = "identify_indicator built_in must be boolean";
            return false;
        }
        const bool builtIn = builtInValue.isUnbound()
            ? strcmp(driver, "neopixel") == 0
            : builtInValue.as<bool>();
        if (strcmp(driver, "gpio") == 0) {
            if ((indicator.size() != 3 && indicator.size() != 4) ||
                !isInteger(indicator["pin"]) ||
                !isBool(indicator["active_high"])) {
                error = "GPIO identify indicator requires pin and active_high";
                return false;
            }
            const int pin = indicator["pin"].as<int>();
            if (pin < 0 || pin > 48 || !isAllowedPin(static_cast<uint8_t>(pin))) {
                error = "GPIO identify pin is not supported by this board profile";
                return false;
            }
            identify.driver = IdentifyDriver::Gpio;
            identify.pin = static_cast<uint8_t>(pin);
            identify.activeHigh = indicator["active_high"].as<bool>();
            identify.builtIn = builtIn;
        } else if (strcmp(driver, "neopixel") == 0) {
            if ((indicator.size() != 3 && indicator.size() != 4) ||
                !isInteger(indicator["pin"]) ||
                !isInteger(indicator["brightness"])) {
                error = "NeoPixel identify indicator requires pin and brightness";
                return false;
            }
            const int pin = indicator["pin"].as<int>();
            const int brightness = indicator["brightness"].as<int>();
            if ((pin != 38 && pin != 48) || brightness < 1 || brightness > 255) {
                error = "NeoPixel identify pin must be GPIO 38 or 48 and brightness 1..255";
                return false;
            }
            identify.driver = IdentifyDriver::NeoPixel;
            identify.pin = static_cast<uint8_t>(pin);
            identify.brightness = static_cast<uint8_t>(brightness);
            identify.builtIn = builtIn;
        } else {
            error = "identify driver must be gpio or neopixel";
            return false;
        }
        for (JsonPairConst pair : indicator) {
            const char* key = pair.key().c_str();
            if (strcmp(key, "driver") != 0 && strcmp(key, "pin") != 0 &&
                strcmp(key, "built_in") != 0 &&
                !(identify.driver == IdentifyDriver::Gpio && strcmp(key, "active_high") == 0) &&
                !(identify.driver == IdentifyDriver::NeoPixel && strcmp(key, "brightness") == 0)) {
                error = "identify_indicator contains an unsupported field";
                return false;
            }
        }
    }

    JsonArrayConst components = root["components"].as<JsonArrayConst>();
    if (components.size() > MAX_COMPONENTS) {
        error = "at most 8 components are supported";
        return false;
    }
    count = 0;
    bool usedPins[49] = {};

    for (JsonVariantConst raw : components) {
        if (!raw.is<JsonObjectConst>()) {
            error = "each component must be an object";
            return false;
        }
        JsonObjectConst source = raw.as<JsonObjectConst>();
        for (JsonPairConst pair : source) {
            if (containsControlCharacters(pair.key()) ||
                (pair.value().is<const char*>() &&
                 containsControlCharacters(pair.value().as<JsonString>()))) {
                error = "component text contains a control character";
                return false;
            }
        }
        const char* driverName = source["driver"] | "";
        Component component;
        if (strcmp(driverName, "gpio_output") == 0) {
            component.driver = Driver::GpioOutput;
        } else if (strcmp(driverName, "digital_input") == 0) {
            component.driver = Driver::DigitalInput;
        } else if (strcmp(driverName, "analog_input") == 0) {
            component.driver = Driver::AnalogInput;
        } else if (strcmp(driverName, "pwm_output") == 0) {
            component.driver = Driver::PwmOutput;
        } else {
            error = "unknown driver";
            return false;
        }

        for (JsonPairConst pair : source) {
            const char* key = pair.key().c_str();
            if (!keyIsAllowed(key, component.driver)) {
                error = "configuration contains an unsupported field";
                return false;
            }
        }

        if (!copyString(source["id"], component.id, sizeof(component.id)) ||
            !validLogicalId(component.id) ||
            !copyString(source["parameter_id"], component.parameterId,
                        sizeof(component.parameterId)) ||
            !validLogicalId(component.parameterId) ||
            !copyString(source["label"], component.label, sizeof(component.label)) ||
            !copyString(source["parameter_label"], component.parameterLabel,
                        sizeof(component.parameterLabel))) {
            error = "component IDs and labels are missing or too long";
            return false;
        }
        if (!isInteger(source["pin"])) {
            error = "GPIO pin must be an integer";
            return false;
        }
        const int pin = source["pin"].as<int>();
        if (pin < 0 || pin > 48 || !isAllowedPin(static_cast<uint8_t>(pin))) {
            error = "GPIO pin is not supported by this board profile";
            return false;
        }
        component.pin = static_cast<uint8_t>(pin);
        if (usedPins[component.pin]) {
            error = "a GPIO pin cannot be assigned to multiple components";
            return false;
        }
        usedPins[component.pin] = true;

        for (uint8_t previous = 0; previous < count; ++previous) {
            if (strcmp(output[previous].id, component.id) == 0 ||
                strcmp(output[previous].parameterId, component.parameterId) == 0) {
                error = "component and parameter IDs must be unique";
                return false;
            }
        }

        if (component.driver == Driver::GpioOutput) {
            JsonVariantConst activeHigh = source["active_high"];
            JsonVariantConst initial = source["initial_value"];
            if (!isBool(activeHigh) || !isBool(initial)) {
                error = "digital output requires boolean active_high and initial_value";
                return false;
            }
            component.activeHigh = activeHigh.as<bool>();
            component.initialBoolean = initial.as<bool>();
            component.booleanValue = component.initialBoolean;
        } else if (component.driver == Driver::DigitalInput) {
            if (!source["pull"].is<const char*>()) {
                error = "digital input pull must be a string";
                return false;
            }
            const char* pull = source["pull"] | "";
            const int interval = source["interval_ms"] | 0;
            if ((strcmp(pull, "none") != 0 && strcmp(pull, "up") != 0 &&
                 strcmp(pull, "down") != 0) || !isInteger(source["interval_ms"]) ||
                interval < 100 || interval > 60000) {
                error = "digital input pull or polling interval is invalid";
                return false;
            }
            component.pull = strcmp(pull, "up") == 0 ? INPUT_PULLUP :
                             strcmp(pull, "down") == 0 ? INPUT_PULLDOWN : INPUT;
            component.intervalMs = static_cast<uint32_t>(interval);
        } else if (component.driver == Driver::AnalogInput) {
            const int interval = source["interval_ms"] | 0;
            JsonVariantConst unitValue = source["unit"];
            JsonVariantConst rawMinimumValue = source["raw_min"];
            JsonVariantConst rawMaximumValue = source["raw_max"];
            JsonVariantConst valueMinimumValue = source["value_min"];
            JsonVariantConst valueMaximumValue = source["value_max"];
            const int rawMinimum = rawMinimumValue.isUnbound() ? 0 : rawMinimumValue.as<int>();
            const int rawMaximum = rawMaximumValue.isUnbound() ? 4095 : rawMaximumValue.as<int>();
            const int valueMinimum = valueMinimumValue.isUnbound() ? 0 : valueMinimumValue.as<int>();
            const int valueMaximum = valueMaximumValue.isUnbound() ? 4095 : valueMaximumValue.as<int>();
            if (!unitValue.isUnbound() &&
                !copyString(unitValue, component.unit, sizeof(component.unit), true)) {
                error = "ADC unit must be a string of at most 24 bytes without control characters";
                return false;
            }
            if (!isAllowedAdcPin(component.pin) || !isInteger(source["interval_ms"]) ||
                interval < 250 || interval > 60000 ||
                (!rawMinimumValue.isUnbound() && !isInteger(rawMinimumValue)) ||
                (!rawMaximumValue.isUnbound() && !isInteger(rawMaximumValue)) ||
                (!valueMinimumValue.isUnbound() && !isInteger(valueMinimumValue)) ||
                (!valueMaximumValue.isUnbound() && !isInteger(valueMaximumValue)) ||
                rawMinimum < 0 || rawMaximum > 4095 || rawMinimum >= rawMaximum ||
                valueMinimum < -1000000 || valueMaximum > 1000000 ||
                valueMinimum >= valueMaximum) {
                error = "ADC pin, scale range, unit or polling interval is invalid";
                return false;
            }
            if (component.unit[0] == '\0') strlcpy(component.unit, "raw", sizeof(component.unit));
            component.intervalMs = static_cast<uint32_t>(interval);
            component.rawMinimum = rawMinimum;
            component.rawMaximum = rawMaximum;
            component.minimumValue = valueMinimum;
            component.maximumValue = valueMaximum;
        } else {
            JsonVariantConst initial = source["initial_value"];
            JsonVariantConst minimumValue = source["min_value"];
            JsonVariantConst maximumValue = source["max_value"];
            const int minimum = minimumValue.isUnbound() ? 0 : minimumValue.as<int>();
            const int maximum = maximumValue.isUnbound() ? 255 : maximumValue.as<int>();
            if (!isInteger(initial) ||
                (!minimumValue.isUnbound() && !isInteger(minimumValue)) ||
                (!maximumValue.isUnbound() && !isInteger(maximumValue)) ||
                minimum < 0 || maximum > 255 || minimum >= maximum ||
                initial.as<int>() < minimum || initial.as<int>() > maximum) {
                error = "PWM range must fit 0..255 and contain the initial value";
                return false;
            }
            component.minimumValue = minimum;
            component.maximumValue = maximum;
            component.initialInteger = initial.as<int>();
            component.integerValue = component.initialInteger;
        }

        output[count++] = component;
    }
    if (identify.driver != IdentifyDriver::None && usedPins[identify.pin]) {
        error = "identify indicator pin is already assigned to a component";
        return false;
    }
    return true;
}

bool UniversalDevice::applyConfiguration(
    JsonVariantConst configuration,
    uint32_t version,
    const char* hash,
    String& error
) {
    if (measureJson(configuration) > MAX_CONFIGURATION_BYTES) {
        error = "configuration exceeds the supported size limit";
        return false;
    }
    if (version == 0 || hash == nullptr || strlen(hash) != 64) {
        error = "configuration version or hash is invalid";
        return false;
    }
    for (size_t index = 0; index < 64; ++index) {
        if (!isxdigit(static_cast<unsigned char>(hash[index]))) {
            error = "configuration hash is invalid";
            return false;
        }
    }

    Component parsed[MAX_COMPONENTS];
    uint8_t parsedCount = 0;
    IdentifyIndicator parsedIdentify;
    if (!parseAndValidate(configuration, parsed, parsedCount, parsedIdentify, error)) return false;

    String calculatedHash;
    if (!calculateConfigurationHash(configuration, calculatedHash) ||
        calculatedHash != hash) {
        error = "configuration hash does not match its contents";
        return false;
    }

    const bool identicalConfiguration = configured_ && configurationHash_ == hash;
    if (identicalConfiguration && version == configurationVersion_) {
        Serial.println("[CONFIG] Configuration is already active; GPIO and NVS unchanged.");
        return true;
    }

    for (uint8_t newIndex = 0; newIndex < parsedCount; ++newIndex) {
        Component& next = parsed[newIndex];
        for (uint8_t oldIndex = 0; oldIndex < componentCount_; ++oldIndex) {
            const Component& previous = components_[oldIndex];
            if (strcmp(next.id, previous.id) != 0 ||
                next.driver != previous.driver || next.pin != previous.pin) {
                continue;
            }
            if (next.driver == Driver::GpioOutput &&
                next.initialBoolean == previous.initialBoolean) {
                next.booleanValue = previous.booleanValue;
            } else if (next.driver == Driver::PwmOutput &&
                       next.initialInteger == previous.initialInteger) {
                next.integerValue = constrain(
                    previous.integerValue, next.minimumValue, next.maximumValue
                );
            }
            break;
        }
    }

    JsonDocument envelope;
    envelope["version"] = version;
    envelope["hash"] = hash;
    envelope["configuration"].set(configuration);
    String serialized;
    if (serializeJson(envelope, serialized) > MAX_CONFIGURATION_BYTES + 256) {
        error = "configuration exceeds the supported non-volatile storage limit";
        return false;
    }
    if (preferences_.putString("envelope", serialized) != serialized.length()) {
        error = "configuration could not be saved to NVS";
        return false;
    }
    if (identicalConfiguration) {
        configurationVersion_ = version;
        return true;
    }

    if (identifyRunning_) {
        setIdentifyIndicator(false);
        identifyRunning_ = false;
        identifyOn_ = false;
        identifyPulsesRemaining_ = 0;
    }
    const IdentifyIndicator previousIdentify = identifyIndicator_;
    for (uint8_t oldIndex = 0; oldIndex < componentCount_; ++oldIndex) {
        const Component& oldComponent = components_[oldIndex];
        // PWM channels belong to component slots, not GPIOs. Release the old
        // route before reusing a pin or slot for another component/driver.
        if (oldComponent.driver == Driver::PwmOutput) {
            writePwm(oldIndex, 0);
            detachPwm(oldComponent.pin);
        }
        bool pinRetained = false;
        for (uint8_t newIndex = 0; newIndex < parsedCount; ++newIndex) {
            if (parsed[newIndex].pin == oldComponent.pin) {
                pinRetained = true;
                break;
            }
        }
        if (parsedIdentify.driver != IdentifyDriver::None &&
            parsedIdentify.pin == oldComponent.pin) {
            pinRetained = true;
        }
        if (pinRetained) continue;
        if (oldComponent.driver == Driver::GpioOutput) {
            digitalWrite(
                oldComponent.pin,
                oldComponent.activeHigh ? LOW : HIGH
            );
        }
        pinMode(oldComponent.pin, INPUT);
    }

    if (previousIdentify.driver != IdentifyDriver::None) {
        bool pinRetained = parsedIdentify.driver != IdentifyDriver::None &&
                           parsedIdentify.pin == previousIdentify.pin;
        for (uint8_t newIndex = 0; newIndex < parsedCount; ++newIndex) {
            if (parsed[newIndex].pin == previousIdentify.pin) pinRetained = true;
        }
        if (!pinRetained) {
            if (previousIdentify.driver == IdentifyDriver::Gpio) {
                digitalWrite(previousIdentify.pin,
                             previousIdentify.activeHigh ? LOW : HIGH);
            } else {
                identifyPixel_.setPin(previousIdentify.pin);
                identifyPixel_.begin();
                identifyPixel_.clear();
                identifyPixel_.show();
            }
            pinMode(previousIdentify.pin, INPUT);
        }
    }

    memcpy(components_, parsed, sizeof(components_));
    componentCount_ = parsedCount;
    identifyIndicator_ = parsedIdentify;
    configurationVersion_ = version;
    configurationHash_ = hash;
    configured_ = true;
    configurePins();
    return true;
}

void UniversalDevice::configurePins() {
    for (uint8_t index = 0; index < componentCount_; ++index) {
        Component& component = components_[index];
        switch (component.driver) {
            case Driver::GpioOutput: {
                const int inactive = component.activeHigh ? LOW : HIGH;
                const int initialLevel = component.booleanValue
                    ? (component.activeHigh ? HIGH : LOW)
                    : inactive;
                digitalWrite(component.pin, initialLevel);
                pinMode(component.pin, OUTPUT);
                digitalWrite(component.pin, initialLevel);
                break;
            }
            case Driver::DigitalInput:
                pinMode(component.pin, component.pull);
                component.booleanValue = digitalRead(component.pin) == HIGH;
                component.hasValue = true;
                break;
            case Driver::AnalogInput:
                pinMode(component.pin, INPUT);
                component.integerValue = scaleAnalogValue(
                    component, analogRead(component.pin)
                );
                component.hasValue = true;
                break;
            case Driver::PwmOutput:
                pinMode(component.pin, OUTPUT);
                configurePwm(component.pin, index, component.integerValue);
                break;
        }
        component.lastPollAt = millis();
    }
    if (identifyIndicator_.driver == IdentifyDriver::Gpio) {
        const int inactive = identifyIndicator_.activeHigh ? LOW : HIGH;
        digitalWrite(identifyIndicator_.pin, inactive);
        pinMode(identifyIndicator_.pin, OUTPUT);
        digitalWrite(identifyIndicator_.pin, inactive);
    } else if (identifyIndicator_.driver == IdentifyDriver::NeoPixel) {
        identifyPixel_.setPin(identifyIndicator_.pin);
        identifyPixel_.begin();
        identifyPixel_.setBrightness(identifyIndicator_.brightness);
        identifyPixel_.clear();
        identifyPixel_.show();
    }
}

bool UniversalDevice::startIdentify(String& error) {
    if (identifyIndicator_.driver == IdentifyDriver::None) {
        error = "no identify indicator is configured";
        return false;
    }
    identifyPulsesRemaining_ = 5;
    identifyRunning_ = true;
    identifyOn_ = true;
    setIdentifyIndicator(true);
    identifyNextAt_ = millis() + 250;
    return true;
}

void UniversalDevice::updateIdentify() {
    if (!identifyRunning_ || static_cast<int32_t>(millis() - identifyNextAt_) < 0) return;
    if (identifyOn_) {
        setIdentifyIndicator(false);
        identifyOn_ = false;
        if (identifyPulsesRemaining_ > 0) --identifyPulsesRemaining_;
        if (identifyPulsesRemaining_ == 0) {
            identifyRunning_ = false;
            return;
        }
    } else {
        setIdentifyIndicator(true);
        identifyOn_ = true;
    }
    identifyNextAt_ = millis() + 250;
}

void UniversalDevice::setIdentifyIndicator(bool enabled) {
    if (identifyIndicator_.driver == IdentifyDriver::Gpio) {
        const int active = identifyIndicator_.activeHigh ? HIGH : LOW;
        const int inactive = identifyIndicator_.activeHigh ? LOW : HIGH;
        digitalWrite(identifyIndicator_.pin, enabled ? active : inactive);
    } else if (identifyIndicator_.driver == IdentifyDriver::NeoPixel) {
        identifyPixel_.setPixelColor(0, enabled ? identifyPixel_.Color(255, 255, 255) : 0);
        identifyPixel_.show();
    }
}

void UniversalDevice::fillSchema(JsonObject schema) const {
    schema.clear();
    schema["format_version"] = 1;
    JsonObject components = schema["components"].to<JsonObject>();
    JsonObject parameters = schema["parameters"].to<JsonObject>();
    schema["actions"].to<JsonObject>();
    schema["capabilities"]["universal_profile"] = true;
    if (identifyIndicator_.driver != IdentifyDriver::None) {
        schema["capabilities"]["identify"] = true;
        if (identifyIndicator_.builtIn) {
            schema["capabilities"]["built_in_indicator"] = true;
        }
        JsonObject identify = schema["actions"]["identify"].to<JsonObject>();
        identify["label"] = "\u041d\u0430\u0439\u0442\u0438 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e";
        identify["description"] = "\u041c\u0438\u0433\u0430\u0435\u0442 \u0438\u043d\u0434\u0438\u043a\u0430\u0442\u043e\u0440\u043e\u043c, \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u043c \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430.";
    }

    for (uint8_t index = 0; index < componentCount_; ++index) {
        const Component& component = components_[index];
        JsonObject componentSchema = components[component.id].to<JsonObject>();
        componentSchema["label"] = component.label;
        switch (component.driver) {
            case Driver::GpioOutput:
                componentSchema["category"] = "actuator";
                componentSchema["description"] = "Включает или выключает цифровой выход.";
                break;
            case Driver::DigitalInput:
                componentSchema["category"] = "input";
                componentSchema["description"] = "Считывает логический уровень на входе.";
                break;
            case Driver::AnalogInput:
                componentSchema["category"] = "sensor";
                componentSchema["description"] = "Считывает ADC и линейно преобразует настроенный raw-диапазон в заданные значения.";
                break;
            case Driver::PwmOutput:
                componentSchema["category"] = "actuator";
                componentSchema["description"] = "Устанавливает заполнение PWM в заданном диапазоне.";
                break;
        }

        JsonObject parameter = parameters[component.parameterId].to<JsonObject>();
        parameter["label"] = component.parameterLabel;
        parameter["component"] = component.id;
        if (component.driver == Driver::GpioOutput) {
            parameter["type"] = "boolean";
            parameter["access"] = "read_write";
            parameter["control"] = "toggle";
        } else if (component.driver == Driver::DigitalInput) {
            parameter["type"] = "boolean";
            parameter["access"] = "read_only";
        } else if (component.driver == Driver::AnalogInput) {
            parameter["type"] = "integer";
            parameter["access"] = "read_only";
            parameter["min"] = component.minimumValue;
            parameter["max"] = component.maximumValue;
            parameter["step"] = 1;
            parameter["precision"] = 0;
            if (component.unit[0] != '\0') parameter["unit"] = component.unit;
        } else {
            parameter["type"] = "integer";
            parameter["access"] = "read_write";
            parameter["control"] = "slider";
            parameter["min"] = component.minimumValue;
            parameter["max"] = component.maximumValue;
            parameter["step"] = 1;
            parameter["precision"] = 0;
            parameter["unit"] = "raw";
        }
    }
}

void UniversalDevice::sendState(GatewayClient& gateway, bool logMessage) {
    if (!gateway.isConnected()) return;
    JsonDocument message;
    message["type"] = "state";
    message["id"] = ++stateSequence_;
    message["device"] = UNIVERSAL_DEVICE_ID;
    JsonObject parameters = message["data"]["parameters"].to<JsonObject>();
    for (uint8_t index = 0; index < componentCount_; ++index) {
        Component& component = components_[index];
        JsonVariant value = parameters[component.parameterId];
        if (component.driver == Driver::GpioOutput ||
            component.driver == Driver::DigitalInput) {
            value.set(component.booleanValue);
        } else {
            value.set(component.integerValue);
        }
    }
    if (!gateway.sendJson(message)) return;
    lastHeartbeatAt_ = millis();
    if (logMessage) {
        String serialized;
        serializeJson(message, serialized);
        Serial.printf("[STATE] %s\n", serialized.c_str());
    }
}

bool UniversalDevice::readComponent(Component& component) {
    if (component.driver == Driver::DigitalInput) {
        const bool sampled = digitalRead(component.pin) == HIGH;
        const bool changed = !component.hasValue || sampled != component.booleanValue;
        component.booleanValue = sampled;
        component.hasValue = true;
        return changed;
    }
    if (component.driver == Driver::AnalogInput) {
        const int32_t sampled = scaleAnalogValue(
            component, analogRead(component.pin)
        );
        const bool changed = !component.hasValue || sampled != component.integerValue;
        component.integerValue = sampled;
        component.hasValue = true;
        return changed;
    }
    return false;
}

int32_t UniversalDevice::scaleAnalogValue(
    const Component& component,
    int32_t rawValue
) const {
    const int32_t boundedRaw = constrain(
        rawValue, component.rawMinimum, component.rawMaximum
    );
    const int64_t rawOffset = boundedRaw - component.rawMinimum;
    const int64_t rawSpan = component.rawMaximum - component.rawMinimum;
    const int64_t valueSpan = component.maximumValue - component.minimumValue;
    const int64_t scaledOffset = (rawOffset * valueSpan + rawSpan / 2) / rawSpan;
    return static_cast<int32_t>(component.minimumValue + scaledOffset);
}

void UniversalDevice::loop(GatewayClient& gateway, bool registered) {
    updateIdentify();
    const unsigned long now = millis();
    bool changed = false;
    for (uint8_t index = 0; index < componentCount_; ++index) {
        Component& component = components_[index];
        if (component.driver != Driver::DigitalInput &&
            component.driver != Driver::AnalogInput) continue;
        if (now - component.lastPollAt < component.intervalMs) continue;
        component.lastPollAt = now;
        changed = readComponent(component) || changed;
    }
    if (registered && gateway.isConnected() &&
        (changed || now - lastHeartbeatAt_ >= HEARTBEAT_INTERVAL_MS)) {
        sendState(gateway);
    }
}

bool UniversalDevice::setParameter(
    const char* parameterId,
    JsonVariantConst requested,
    JsonVariant& applied,
    String& error
) {
    for (uint8_t index = 0; index < componentCount_; ++index) {
        Component& component = components_[index];
        if (strcmp(component.parameterId, parameterId) != 0) continue;
        if (component.driver == Driver::GpioOutput) {
            if (!isBool(requested)) {
                error = "parameter requires a boolean value";
                return false;
            }
            component.booleanValue = requested.as<bool>();
            const int active = component.activeHigh ? HIGH : LOW;
            const int inactive = component.activeHigh ? LOW : HIGH;
            digitalWrite(component.pin, component.booleanValue ? active : inactive);
            applied.set(component.booleanValue);
            return true;
        }
        if (component.driver == Driver::PwmOutput) {
            if (!isInteger(requested) ||
                requested.as<int>() < component.minimumValue ||
                requested.as<int>() > component.maximumValue) {
                error = "PWM parameter is outside the configured range";
                return false;
            }
            component.integerValue = requested.as<int>();
            writePwm(index, component.integerValue);
            applied.set(component.integerValue);
            return true;
        }
        error = "read-only parameters cannot be changed";
        return false;
    }
    error = "unknown parameter";
    return false;
}

void UniversalDevice::sendConfigurationResult(
    GatewayClient& gateway,
    uint32_t attemptedVersion,
    const char* attemptedHash,
    bool success,
    const char* error
) const {
    JsonDocument message;
    message["type"] = "device_config_result";
    message["device"] = UNIVERSAL_DEVICE_ID;
    message["config_version"] = attemptedVersion;
    message["config_hash"] = attemptedHash;
    message["success"] = success;
    if (error != nullptr) message["error"] = error;
    message["schema_version"] = configurationVersion_ == 0 ? 1 : configurationVersion_;
    JsonObject schema = message["schema"].to<JsonObject>();
    fillSchema(schema);
    gateway.sendJson(message);
}

void UniversalDevice::sendConfigurationReport(GatewayClient& gateway) {
    JsonDocument message;
    message["type"] = "device_config_report";
    message["device"] = UNIVERSAL_DEVICE_ID;
    message["config_version"] = configurationVersion_;
    message["config_hash"] = configurationHash_;
    message["schema_version"] = configurationVersion_ == 0 ? 1 : configurationVersion_;
    JsonObject schema = message["schema"].to<JsonObject>();
    fillSchema(schema);
    if (configured_) {
        const String savedEnvelope = preferences_.getString("envelope", "");
        JsonDocument stored;
        if (!deserializeJson(stored, savedEnvelope)) {
            message["configuration"].set(stored["configuration"].as<JsonVariantConst>());
        }
    } else {
        JsonObject empty = message["configuration"].to<JsonObject>();
        empty["format_version"] = 1;
        empty["components"].to<JsonArray>();
    }
    gateway.sendJson(message);
}

void UniversalDevice::handleMessage(JsonDocument& message, GatewayClient& gateway) {
    const char* messageType = message["type"] | "";
    if (strcmp(messageType, "device_config_request") == 0) {
        sendConfigurationReport(gateway);
        return;
    }
    if (strcmp(messageType, "device_config_apply") == 0) {
        const char* target = message["device"] | "";
        const uint32_t version = readConfigurationVersion(message["config_version"]);
        const char* hash = message["config_hash"] | "";
        String error;
        const bool targetMatches = target[0] == '\0' || strcmp(target, UNIVERSAL_DEVICE_ID) == 0;
        const bool success = targetMatches &&
            applyConfiguration(message["configuration"], version, hash, error);
        if (!targetMatches) error = "configuration is addressed to another device";
        sendConfigurationResult(
            gateway, version, hash, success, success ? nullptr : error.c_str()
        );
        if (success) sendState(gateway, true);
        Serial.printf("[CONFIG] Apply version %lu: %s%s%s\n",
                      static_cast<unsigned long>(version),
                      success ? "success" : "failed",
                      success ? "" : " - ",
                      success ? "" : error.c_str());
        return;
    }
    if (strcmp(messageType, "command") != 0) return;

    const char* commandId = message["data"]["command_id"] | "";
    const char* commandName = message["data"]["command"] | "";
    if (commandId[0] == '\0') return;
    JsonDocument result;
    result["type"] = "command_result";
    result["id"] = commandId;
    result["command_id"] = commandId;
    result["device"] = UNIVERSAL_DEVICE_ID;
    JsonObject data = result["data"].to<JsonObject>();
    bool success = false;
    String error;
    if (!configured_) error = "device has no applied hardware configuration";
    if (strcmp(commandName, "set") == 0) {
        const char* parameter = message["data"]["parameter"] | "";
        JsonVariant applied = data["value"];
        success = configured_ &&
            setParameter(parameter, message["data"]["value"], applied, error);
        data["parameter"] = parameter;
    } else if (strcmp(commandName, "invoke") == 0) {
        const char* action = message["data"]["action"] | "";
        data["action"] = action;
        if (strcmp(action, "identify") == 0) {
            success = configured_ && startIdentify(error);
        } else {
            error = "this universal profile does not support the requested action";
        }
    } else {
        error = "unsupported command";
    }
    result["success"] = success;
    if (!success) data["error"] = error;
    gateway.sendJson(result);
    if (success) sendState(gateway);
}
