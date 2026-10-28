#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>

// One GatewayClient instance is expected per ESP32, matching the single
// WebSocket connection used by a device.
class GatewayClient {
public:
    using EventHandler = void (*)(WStype_t, uint8_t*, size_t);

    void begin(
        const char* gatewayIp,
        uint16_t gatewayPort,
        const char* wifiSsid,
        const char* wifiPassword,
        EventHandler eventHandler
    );
    void loop();
    bool isConnected();
    bool sendText(const String& message);
    bool sendJson(JsonDocument& document);

private:
    static void dispatchWebSocketEvent(
        WStype_t type,
        uint8_t* payload,
        size_t length
    );

    void maintainWiFiConnection();

    WebSocketsClient webSocket_;
    EventHandler eventHandler_ = nullptr;
    unsigned long lastWifiReconnectAt_ = 0;
    bool wifiConnectionAnnounced_ = false;

    static GatewayClient* activeClient_;
};
