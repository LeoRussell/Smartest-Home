#include "GatewayClient.h"

#include <esp_system.h>

#ifndef GATEWAY_WIFI_POWER_SAVE
#define GATEWAY_WIFI_POWER_SAVE 0
#endif

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
    if (!wifiEventsRegistered_) {
        WiFi.onEvent(dispatchWiFiEvent);
        wifiEventsRegistered_ = true;
    }

    WiFi.mode(WIFI_STA);
    // AutoReconnect owns retries. WiFi.reconnect() forcibly disconnects first
    // and would interrupt the driver's own association/DHCP recovery attempts.
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(GATEWAY_WIFI_POWER_SAVE != 0);
    WiFi.begin(wifiSsid, wifiPassword);
    wifiConnectionAnnounced_ = false;

    Serial.printf("[NET] startup reset_reason=%d free_heap=%lu power_save=%s\n",
                  static_cast<int>(esp_reset_reason()),
                  static_cast<unsigned long>(ESP.getFreeHeap()),
                  GATEWAY_WIFI_POWER_SAVE ? "on" : "off");
    Serial.println("[NET] Connecting to Wi-Fi...");
    webSocket_.begin(gatewayIp, gatewayPort, "/");
    webSocket_.onEvent(dispatchWebSocketEvent);
    webSocket_.setReconnectInterval(5000);
    // Gateway owns connection probes. WebSockets automatically sends pong for
    // an incoming ping while loop() runs; a second timeout policy is unneeded.
}

void GatewayClient::loop() {
    if (!maintainWiFiConnection()) return;
    webSocket_.loop();
}

bool GatewayClient::isConnected() {
    return !wifiLinkResetPending_.load() && WiFi.status() == WL_CONNECTED &&
           webSocket_.isConnected();
}

bool GatewayClient::sendText(const String& message) {
    if (!isConnected()) return false;
    const bool sent = webSocket_.sendTXT(message.c_str(), message.length());
    if (sent) lastSentAt_ = millis();
    return sent;
}

bool GatewayClient::sendJson(JsonDocument& document) {
    if (!isConnected()) return false;

    String message;
    message.reserve(measureJson(document));
    serializeJson(document, message);
    return sendText(message);
}

void GatewayClient::dispatchWebSocketEvent(
    WStype_t type,
    uint8_t* payload,
    size_t length
) {
    GatewayClient* client = activeClient_;
    if (client == nullptr) return;
    const unsigned long now = millis();
    if (type == WStype_CONNECTED) {
        client->webSocketWasConnected_ = true;
        client->webSocketConnectedAt_ = now;
        client->lastReceivedAt_ = now;
        client->lastSentAt_ = now;
        client->gatewayPingAnnounced_ = false;
        Serial.printf("[NET] WebSocket connected at %lu ms rssi=%ld dBm heap=%lu\n",
                      now, static_cast<long>(WiFi.RSSI()),
                      static_cast<unsigned long>(ESP.getFreeHeap()));
    } else if (type == WStype_TEXT || type == WStype_BIN ||
               type == WStype_PING || type == WStype_PONG) {
        client->lastReceivedAt_ = now;
        if (type == WStype_PING && !client->gatewayPingAnnounced_) {
            client->gatewayPingAnnounced_ = true;
            Serial.println("[NET] Gateway ping received; automatic pong processing.");
        }
    } else if (type == WStype_DISCONNECTED) {
        Serial.printf(
            "[NET] WebSocket disconnected connection=%lu ms wifi=%u rssi=%ld dBm last_rx=%lu ms last_app_tx=%lu ms peer_reason=%.*s\n",
            client->webSocketWasConnected_ ? now - client->webSocketConnectedAt_ : 0,
            static_cast<unsigned int>(WiFi.status()),
            static_cast<long>(WiFi.RSSI()), now - client->lastReceivedAt_,
            now - client->lastSentAt_,
            static_cast<int>(payload != nullptr ? (length < 120 ? length : 120) : 11),
            payload != nullptr ? reinterpret_cast<const char*>(payload) : "unavailable"
        );
        client->webSocketWasConnected_ = false;
    } else if (type == WStype_ERROR) {
        Serial.printf("[NET] WebSocket error wifi=%u payload=%.*s\n",
                      static_cast<unsigned int>(WiFi.status()),
                      static_cast<int>(payload != nullptr ? (length < 120 ? length : 120) : 11),
                      payload != nullptr ? reinterpret_cast<const char*>(payload) : "unavailable");
    }
    if (client->eventHandler_ != nullptr) {
        client->eventHandler_(type, payload, length);
    }
}

void GatewayClient::dispatchWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
    if (activeClient_ == nullptr) return;
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
        activeClient_->wifiLinkResetPending_.store(true);
        const uint8_t reason = info.wifi_sta_disconnected.reason;
        Serial.printf("[NET] Wi-Fi disconnected at %lu ms reason=%u (%s); driver auto_reconnect=on\n",
                      millis(), static_cast<unsigned int>(reason),
                      WiFi.disconnectReasonName(static_cast<wifi_err_reason_t>(reason)));
    } else if (event == ARDUINO_EVENT_WIFI_STA_LOST_IP ||
               event == ARDUINO_EVENT_WIFI_STA_STOP) {
        activeClient_->wifiLinkResetPending_.store(true);
        Serial.printf("[NET] Wi-Fi lost IP or stopped at %lu ms event=%u\n",
                      millis(), static_cast<unsigned int>(event));
    }
}

bool GatewayClient::maintainWiFiConnection() {
    // Process even a brief disconnect that auto-reconnected between loop()
    // iterations: the old TCP connection must not survive onto the new link.
    if (wifiLinkResetPending_.exchange(false)) {
        webSocket_.disconnect();
        wifiConnectionAnnounced_ = false;
    }
    if (WiFi.status() != WL_CONNECTED) {
        if (wifiConnectionAnnounced_) webSocket_.disconnect();
        wifiConnectionAnnounced_ = false;
        return false;
    }

    if (!wifiConnectionAnnounced_) {
        Serial.print("[NET] Wi-Fi connected IP=");
        Serial.println(WiFi.localIP());
        Serial.printf("[NET] Wi-Fi rssi=%ld dBm power_save=%s\n",
                      static_cast<long>(WiFi.RSSI()),
                      GATEWAY_WIFI_POWER_SAVE ? "on" : "off");
        wifiConnectionAnnounced_ = true;
    }
    return true;
}
