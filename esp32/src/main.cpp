
#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>

const char* ssid = "Pepito";
const char* password = "215646pepito";

const char* gateway_ip = "192.168.0.110";
const uint16_t gateway_port = 8765;

const int LED_PIN = 48;

WebSocketsClient webSocket;

void sendState(bool ledOn) {
    JsonDocument doc;

    doc["type"] = "state";
    doc["id"] = "state-001";
    doc["device"] = "esp32-01";
    doc["data"]["parameters"]["led"] = ledOn;

    String message;
    serializeJson(doc, message);
    webSocket.sendTXT(message);

    Serial.print("State sent: ");
    Serial.println(message);
}

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

        case WStype_TEXT: {
            Serial.printf("Received: %s\n", (char*)payload);

            JsonDocument doc;
            DeserializationError error =
                deserializeJson(doc, payload, length);

            if (error) {
                Serial.println("Invalid JSON");
                break;
            }

            const char* typeName = doc["type"] | "";

            if (strcmp(typeName, "command") == 0) {
                const char* parameter =
                    doc["data"]["parameter"] | "";
                bool value = doc["data"]["value"] | false;

                if (strcmp(parameter, "led") == 0) {
                    digitalWrite(
                        LED_PIN,
                        value ? HIGH : LOW
                    );

                    Serial.println(
                        value ? "LED ON" : "LED OFF"
                    );

                    sendState(value);
                }
            }

            break;
        }

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
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, password);

    Serial.print("Connecting to Wi-Fi");

    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }

    Serial.println();
    Serial.print("Wi-Fi connected: ");
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
