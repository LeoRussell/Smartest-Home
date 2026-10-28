#include "GatewayClient.h"

#include <WiFi.h>

namespace {
constexpr unsigned long WIFI_RETRY_INTERVAL_MS = 10000;
}

GatewayClient* GatewayClient::activeClient_ = nullptr;

void GatewayClient::begin(
    const char* gatewayIp,
    uint16_t gatewayPort,
    const char* wifiSsid,
    const char* wifiPassword,
    EventHandler eventHandler
) {
    activeClient_ = this;
    eventHandler_ = eventHandler;

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(wifiSsid, wifiPassword);
    lastWifiReconnectAt_ = millis();
    wifiConnectionAnnounced_ = false;

    Serial.println("Connecting to Wi-Fi...");
    webSocket_.begin(gatewayIp, gatewayPort, "/");
    webSocket_.onEvent(dispatchWebSocketEvent);
    webSocket_.setReconnectInterval(5000);
}

void GatewayClient::loop() {
    webSocket_.loop();
    maintainWiFiConnection();
}

bool GatewayClient::isConnected() {
    return webSocket_.isConnected();
}

bool GatewayClient::sendText(const String& message) {
    return isConnected() && webSocket_.sendTXT(message.c_str());
}

bool GatewayClient::sendJson(JsonDocument& document) {
    if (!isConnected()) return false;

    String message;
    serializeJson(document, message);
    return sendText(message);
}

void GatewayClient::dispatchWebSocketEvent(
    WStype_t type,
    uint8_t* payload,
    size_t length
) {
    if (activeClient_ != nullptr && activeClient_->eventHandler_ != nullptr) {
        activeClient_->eventHandler_(type, payload, length);
    }
}

void GatewayClient::maintainWiFiConnection() {
    const unsigned long now = millis();
    if (
        WiFi.status() != WL_CONNECTED &&
        now - lastWifiReconnectAt_ >= WIFI_RETRY_INTERVAL_MS
    ) {
        Serial.println("Wi-Fi disconnected; reconnecting...");
        WiFi.reconnect();
        lastWifiReconnectAt_ = now;
        wifiConnectionAnnounced_ = false;
    }

    if (WiFi.status() == WL_CONNECTED && !wifiConnectionAnnounced_) {
        Serial.print("Wi-Fi connected: ");
        Serial.println(WiFi.localIP());
        wifiConnectionAnnounced_ = true;
    }
}
