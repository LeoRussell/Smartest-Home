
#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>

// Wi-Fi
const char* ssid = "Pepito";
const char* password = "215646pepito";

// IP компьютера с Python Gateway
const char* gateway_ip = "192.168.0.103";
const uint16_t gateway_port = 8765;

WebSocketsClient webSocket;

void onWebSocketEvent(
    WStype_t type,
    uint8_t* payload,
    size_t length
) {
    switch (type) {
        case WStype_CONNECTED:
            Serial.println("WebSocket connected!");

            webSocket.sendTXT(
                "{\"type\":\"hello\","
                "\"id\":\"hello-001\","
                "\"device\":\"esp32-01\","
                "\"data\":{\"model\":\"Adafruit Feather ESP32-S3\","
                "\"firmware\":\"1.0.0\",\"protocol\":1}}"
            );
            break;

        case WStype_TEXT:
            Serial.printf(
                "Gateway says: %s\n",
                (char*)payload
            );
            break;

        case WStype_DISCONNECTED:
            Serial.println("WebSocket disconnected");
            break;

        case WStype_ERROR:
            Serial.println("WebSocket error");
            break;

        default:
            break;
    }
}

void setup() {
    Serial.begin(9600);
    delay(1000);

    Serial.println("Connecting to Wi-Fi...");
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, password);

    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }

    Serial.println();
    Serial.println("Wi-Fi connected!");
    Serial.println(WiFi.localIP());

    webSocket.begin(
        gateway_ip,
        gateway_port,
        "/"
    );

    webSocket.onEvent(onWebSocketEvent);
    webSocket.setReconnectInterval(5000);
}

void loop() {
    webSocket.loop();
}
