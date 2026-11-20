#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <GatewayClient.h>
#include <CommandResultCache.h>
#include <Preferences.h>
#include <Adafruit_NeoPixel.h>
#include <DallasTemperature.h>
#include <OneWire.h>
#include "BoardProfile.h"
#include "PwmChannels.h"
#include "user_config.h"

class UniversalDevice {
public:
    bool begin();
    void loop(GatewayClient& gateway, bool registered);
    void handleMessage(JsonDocument& message, GatewayClient& gateway);
    void fillSchema(JsonObject schema) const;
    void onGatewayRegistered(GatewayClient& gateway);
    void sendState(GatewayClient& gateway, bool logMessage = false);
    uint32_t configurationVersion() const;
    bool commandDedupReady() const;

private:
    enum class IdentifyDriver : uint8_t {
        None,
        Gpio,
        NeoPixel
    };

    struct IdentifyIndicator {
        IdentifyDriver driver = IdentifyDriver::None;
        uint8_t pin = 0;
        uint8_t brightness = 32;
        bool activeHigh = true;
        bool builtIn = false;
    };

    enum class Driver : uint8_t {
        GpioOutput,
        DigitalInput,
        AnalogInput,
        Ds18b20,
        PwmOutput
    };

    struct Component {
        char id[65] = {};
        char parameterId[65] = {};
        char label[81] = {};
        char parameterLabel[81] = {};
        char unit[25] = {};
        Driver driver = Driver::GpioOutput;
        uint8_t pin = 0;
        uint8_t pwmChannel = UniversalPwm::NO_CHANNEL;
        uint32_t intervalMs = 1000;
        uint32_t lastPollAt = 0;
        int32_t integerValue = 0;
        double numberValue = 0;
        int32_t minimumValue = 0;
        int32_t maximumValue = 255;
        int32_t rawMinimum = 0;
        int32_t rawMaximum = 4095;
        bool booleanValue = false;
        bool activeHigh = true;
        bool initialBoolean = false;
        int32_t initialInteger = 0;
        uint8_t pull = INPUT;
        bool hasValue = false;
        bool pendingSample = false;
    };

    static constexpr uint8_t MAX_COMPONENTS = 8;
    static constexpr size_t MAX_CONFIGURATION_BYTES = 3000;
    // Component is copied as a flat configuration. Bus objects stay at stable
    // addresses outside it, so reconfiguration never memcpy-copies a driver.
    struct TemperatureRuntime {
        OneWire wire;
        DallasTemperature sensor{&wire};
        DeviceAddress address = {};
        uint32_t conversionStartedAt = 0;
        bool active = false;
        bool found = false;
        bool converting = false;
        bool warned = false;
    };
    bool parseAndValidate(JsonVariantConst configuration, Component* output,
                          uint8_t& count, IdentifyIndicator& identify,
                          String& error) const;
    bool applyConfiguration(JsonVariantConst configuration, uint32_t version,
                            const char* hash, String& error);
    bool loadEmbeddedConfiguration(String& error);
    bool isAllowedPin(uint8_t pin) const;
    bool isAllowedAdcPin(uint8_t pin) const;
    bool planPwmChannels(Component* next, uint8_t count, bool* retained) const;
    bool validLogicalId(const char* value) const;
    bool keyIsAllowed(const char* key, Driver driver) const;
    bool setParameter(const char* parameterId, JsonVariantConst requested,
                      JsonVariant& applied, String& error);
    int32_t scaleAnalogValue(const Component& component, int32_t rawValue) const;
    bool readComponent(Component& component);
    bool pollTemperature(Component& component, TemperatureRuntime& runtime,
                         uint32_t now);
    bool findTemperatureSensor(Component& component, TemperatureRuntime& runtime);
    void invalidateTemperature(Component& component, TemperatureRuntime& runtime,
                               const char* reason);
    void releaseTemperatureBuses();
    void configurePins(const bool* retainedPwmRoutes = nullptr);
    bool startIdentify(String& error);
    void updateIdentify();
    void setIdentifyIndicator(bool enabled);
    void sendConfigurationResult(GatewayClient& gateway, uint32_t attemptedVersion,
                                 const char* attemptedHash, bool success,
                                 const char* error = nullptr) const;
    void sendConfigurationReport(GatewayClient& gateway);
    Preferences preferences_;
    CommandResultCache commandCache_;
    // No pin is driven before a validated indicator configuration is applied.
    Adafruit_NeoPixel identifyPixel_{1, -1, NEO_GRB + NEO_KHZ800};
    Component components_[MAX_COMPONENTS];
    TemperatureRuntime temperatures_[MAX_COMPONENTS];
    IdentifyIndicator identifyIndicator_;
    uint8_t componentCount_ = 0;
    uint32_t configurationVersion_ = 0;
    uint32_t stateSequence_ = 0;
    String configurationHash_;
    String activeConfigurationJson_;
    unsigned long lastHeartbeatAt_ = 0;
    unsigned long identifyNextAt_ = 0;
    uint8_t identifyPulsesRemaining_ = 0;
    bool identifyRunning_ = false;
    bool identifyOn_ = false;
    bool configured_ = false;
};
