/******************************************************************************
 * PARTE 1 - FIRMWARE ESP32  |  SMARTTRAFFIC (Maqueta en miniatura)
 * -----------------------------------------------------------------------------
 * 4 accesos (Norte, Sur, Este, Oeste):
 *   - 4 x sensor ultrasonico HC-SR04  -> detecta "carritos" (distancia)
 *   - 4 x semaforo RGB (Rojo/Amarillo/Verde, logica ALTA = encendido)
 *
 * LIBRERIAS NECESARIAS (Arduino IDE -> Gestor de Librerias):
 *   - PubSubClient   by Nick O'Leary   (MQTT)
 *   - ArduinoJson    by Benoit Blanchon (version 6.21.x recomendada)
 *
 * TOPICOS MQTT
 *   smarttraffic/telemetry  (ESP32 -> servidor)  telemetria periodica
 *   smarttraffic/events     (ESP32 -> servidor)  eventos de vehiculo
 *   smarttraffic/control    (servidor -> ESP32)  estado completo de luces + modo
 *   smarttraffic/commands   (servidor -> ESP32)  comando puntual (opcional)
 ******************************************************************************/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ============================ TABLA DE PINES ==============================
//  IMPORTANTE: no se usan los pines de strapping (0, 2, 12, 15) ni los de
//  UART (1, 3) ni los de la flash (6-11).
//
//  ACCESO | GPIO TRIG | GPIO ECHO | GPIO ROJO | GPIO AMARILLO | GPIO VERDE
//  -------|-----------|-----------|-----------|---------------|-----------
//  NORTE  |    13     |    34     |    32     |      33       |     25
//  SUR    |    26     |    35     |    27     |      14       |      5
//  ESTE   |     4     |    36     |    21     |      19       |     18
//  OESTE  |    17     |    39     |    22     |      23       |     16
//
//  NOTAS DE CABLEADO:
//  * GPIO 34/35/36/39 son SOLO entrada: ideales para ECHO.
//  * ECHO del HC-SR04 entrega 5V -> usar divisor de voltaje (R1=1k, R2=2k)
//    o (10k/20k) entre el ECHO y el GPIO del ESP32.
//  * VCC de los HC-SR04 a 5V (VIN), GND comun con el ESP32.
//  * LEDs con resistencia de 220 ohm. Modulo semaforo de anodo comun
//    (common cathode) = nivel ALTA enciende.
// ==========================================================================

// ------------------------- CONFIGURACION USUARIO --------------------------
const char* WIFI_SSID     = "Aqui_el_SSID_del_Hotspot";   // hotspot PC/celular
const char* WIFI_PASSWORD = "Aqui_la_password";           // minimo 8 caracteres
const char* MQTT_HOST     = "192.168.137.1";              // IP de la PC (ver README)
const uint16_t MQTT_PORT  = 1883;
const char* MQTT_USER     = "";                           // vacio = sin usuario
const char* MQTT_PASSWORD = "";                           // vacio = sin password
// ---------------------------------------------------------------------------

#define TOPIC_TELEMETRY "smarttraffic/telemetry"
#define TOPIC_EVENTS    "smarttraffic/events"
#define TOPIC_CONTROL   "smarttraffic/control"
#define TOPIC_COMMAND   "smarttraffic/commands"

#define TELEMETRY_PERIOD_MS   2000UL   // cada 2 s publica telemetria
#define SENSOR_PERIOD_MS      200UL    // se lee un sensor cada 200 ms (round robin)
#define WIFI_RETRY_MS         5000UL
#define MQTT_RETRY_MS         3000UL
#define SERVER_TIMEOUT_MS     20000UL  // sin mensajes del servidor -> ciclo local
#define PRESENCE_ON_CM        25.0f    // <= 25 cm = vehiculo presente
#define PRESENCE_OFF_CM       35.0f    // >  35 cm = vehiculo ausente
#define PRESENCE_DEBOUNCE_MS  400UL    // estabilidad minima del estado
#define MAX_RANGE_CM          300.0f
#define LOCAL_GREEN_MS        8000UL   // ciclo autonomo (sin servidor)
#define LOCAL_YELLOW_MS       2000UL

enum LightColor : uint8_t { ROJO = 0, AMARILLO = 1, VERDE = 2 };

struct AccessPoint {
  const char* id;          // "norte" | "sur" | "este" | "oeste"
  uint8_t trigPin;
  uint8_t echoPin;
  uint8_t redPin;
  uint8_t yellowPin;
  uint8_t greenPin;
  float    distance;       // cm (-1 = sin eco / fuera de rango)
  bool     occupied;       // hay vehiculo esperando
  unsigned int vehicles;   // total de vehiculos detectados
  uint8_t  light;          // ROJO / AMARILLO / VERDE
  bool     rawPresent;     // estado crudo (antes del debounce)
  unsigned long rawSince;  // cuando cambio el estado crudo
  unsigned long lastRead;  // ultima lectura del sensor
};

//                    id      trig echo red yel grn dist occ veh lt raw rawSince lastRead
AccessPoint accesos[4] = {
  {"norte", 13, 34, 32, 33, 25, -1.0f, false, 0, ROJO, false, 0, 0},
  {"sur",   26, 35, 27, 14,  5, -1.0f, false, 0, ROJO, false, 0, 0},
  {"este",   4, 36, 21, 19, 18, -1.0f, false, 0, ROJO, false, 0, 0},
  {"oeste", 17, 39, 22, 23, 16, -1.0f, false, 0, ROJO, false, 0, 0},
};

WiFiClient espClient;
PubSubClient mqttClient(espClient);

String espMode = "auto";              // "auto" | "manual" (lo decide el servidor)
unsigned long lastServerMsg = 0;      // ultimo mensaje MQTT del servidor
unsigned long lastTelemetry = 0;
int sensorIndex = 0;

// ciclo autonomo local (solo cuando el servidor esta caido)
int localIdx = 0;
uint8_t localPhase = VERDE;
unsigned long localPhaseStart = 0;

// ============================== UTILIDADES ================================

const char* lightName(uint8_t c) {
  switch (c) {
    case AMARILLO: return "yellow";
    case VERDE:    return "green";
    default:       return "red";
  }
}

uint8_t lightFromName(const char* name) {
  if (!name) return 255;
  if (strcmp(name, "red") == 0)    return ROJO;
  if (strcmp(name, "yellow") == 0) return AMARILLO;
  if (strcmp(name, "green") == 0)  return VERDE;
  return 255;
}

void applyLight(int i, uint8_t color) {
  if (color > VERDE) return;
  AccessPoint& a = accesos[i];
  digitalWrite(a.redPin,    color == ROJO);
  digitalWrite(a.yellowPin, color == AMARILLO);
  digitalWrite(a.greenPin,  color == VERDE);
  a.light = color;
}

void applyLightById(const char* id, uint8_t color) {
  for (int i = 0; i < 4; i++) {
    if (strcmp(accesos[i].id, id) == 0) { applyLight(i, color); return; }
  }
}

// ============================== SENSORES ==================================

float readUltrasonic(uint8_t trig, uint8_t echo) {
  digitalWrite(trig, LOW);
  delayMicroseconds(4);
  digitalWrite(trig, HIGH);
  delayMicroseconds(10);
  digitalWrite(trig, LOW);
  long duration = pulseIn(echo, HIGH, 25000UL);   // ~4.3 m max
  if (duration == 0) return -1.0f;                // sin eco
  float cm = duration * 0.0343f / 2.0f;
  if (cm > MAX_RANGE_CM) return -1.0f;
  return cm;
}

void publishVehicleEvent(int i);   // prototipo

void scanSensor(int i) {
  AccessPoint& a = accesos[i];
  unsigned long now = millis();
  float d = readUltrasonic(a.trigPin, a.echoPin);
  a.distance = d;

  // estado crudo con histesis
  bool raw;
  if (a.occupied) raw = !(d < 0.0f || d > PRESENCE_OFF_CM);
  else            raw = (d > 0.0f && d <= PRESENCE_ON_CM);

  if (raw != a.rawPresent) {
    a.rawPresent = raw;
    a.rawSince = now;
  }

  // confirmacion estable (antirrebote)
  if (raw != a.occupied && (now - a.rawSince) >= PRESENCE_DEBOUNCE_MS) {
    a.occupied = raw;
    if (raw) {
      a.vehicles++;
      Serial.printf("[SENSOR] %s: vehiculo detectado (total=%u, d=%.1f cm)\n",
                    a.id, a.vehicles, a.distance);
      publishVehicleEvent(i);
    }
  }
}

// ================================ MQTT ====================================

void publishVehicleEvent(int i) {
  if (!mqttClient.connected()) return;
  StaticJsonDocument<160> doc;
  doc["type"]     = "vehicle";
  doc["access"]   = accesos[i].id;
  doc["vehicles"] = accesos[i].vehicles;
  doc["distance"] = accesos[i].distance;
  doc["ts"]       = millis();
  char buf[192];
  size_t n = serializeJson(doc, buf);
  mqttClient.publish(TOPIC_EVENTS, buf, n);
}

void publishTelemetry() {
  if (!mqttClient.connected()) return;
  StaticJsonDocument<768> doc;
  doc["ts"]   = millis();
  doc["mode"] = espMode;
  for (int i = 0; i < 4; i++) {
    const char* id = accesos[i].id;
    doc[id]["d"] = accesos[i].distance;              // distancia en cm (-1 = sin dato)
    doc[id]["v"] = accesos[i].vehicles;               // acumulado de vehiculos
    doc[id]["l"] = lightName(accesos[i].light);       // estado del semaforo
    doc[id]["o"] = accesos[i].occupied ? 1 : 0;       // 1 = hay vehiculo esperando
  }
  char buf[768];
  size_t n = serializeJson(doc, buf);
  if (mqttClient.publish(TOPIC_TELEMETRY, buf, n)) {
    Serial.println("[MQTT] telemetria publicada");
  }
}

// comando puntual: {"access":"norte","color":"green"} | {"all":"red"} | {"mode":"manual"}
void handleCommand(JsonDocument& doc) {
  if (doc.containsKey("mode")) {
    const char* m = doc["mode"];
    if (m) {
      espMode = m;
      Serial.printf("[CMD] modo = %s\n", m);
    }
  }
  if (doc.containsKey("all")) {
    uint8_t c = lightFromName(doc["all"]);
    for (int i = 0; i < 4; i++) applyLight(i, c);
    Serial.println("[CMD] todos en rojo/amarillo/verde");
  }
  if (doc.containsKey("access") && doc.containsKey("color")) {
    const char* id = doc["access"];
    uint8_t c = lightFromName(doc["color"]);
    if (id) applyLightById(id, c);
  }
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  lastServerMsg = millis();

  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, payload, length)) {
    Serial.println("[MQTT] JSON invalido");
    return;
  }

  if (strcmp(topic, TOPIC_CONTROL) == 0) {
    // {"mode":"auto","lights":{"norte":"green","sur":"red",...}}
    if (doc.containsKey("mode")) {
      const char* m = doc["mode"];
      if (m) espMode = m;
    }
    if (doc.containsKey("lights")) {
      for (int i = 0; i < 4; i++) {
        const char* c = doc["lights"][accesos[i].id];
        if (c) applyLight(i, lightFromName(c));
      }
    }
  } else if (strcmp(topic, TOPIC_COMMAND) == 0) {
    handleCommand(doc);
  }
}

void connectMqtt() {
  if (!mqttClient.connected()) return;
  String clientId = "esp32-smarttraffic-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool ok;
  if (strlen(MQTT_USER) > 0) ok = mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASSWORD);
  else                       ok = mqttClient.connect(clientId.c_str());

  if (ok) {
    Serial.println("[MQTT] conectado al broker");
    mqttClient.subscribe(TOPIC_CONTROL);
    mqttClient.subscribe(TOPIC_COMMAND);
    publishTelemetry();
  } else {
    Serial.printf("[MQTT] error rc=%d\n", mqttClient.state());
  }
}

void maintainConnections() {
  static unsigned long lastWifiTry = 0, lastMqttTry = 0;
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED && (now - lastWifiTry) >= WIFI_RETRY_MS) {
    lastWifiTry = now;
    Serial.println("[WiFi] reconectando...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }

  if (WiFi.status() == WL_CONNECTED && !mqttClient.connected() &&
      (now - lastMqttTry) >= MQTT_RETRY_MS) {
    lastMqttTry = now;
    connectMqtt();
  }
}

// ======================= CICLO LOCAL (SIN SERVIDOR) =======================
// Si durante SERVER_TIMEOUT_MS no llegan mensajes del servidor, el ESP32
// opera de forma autonoma con un ciclo fijo (verde 8 s / amarillo 2 s).

bool serverAlive() {
  return (millis() - lastServerMsg) < SERVER_TIMEOUT_MS;
}

void localCycle(unsigned long now) {
  if (localPhaseStart == 0) {
    localPhaseStart = now;
    for (int i = 0; i < 4; i++) applyLight(i, i == localIdx ? VERDE : ROJO);
    localPhase = VERDE;
    return;
  }

  if (localPhase == VERDE && (now - localPhaseStart) >= LOCAL_GREEN_MS) {
    applyLight(localIdx, AMARILLO);
    localPhase = AMARILLO;
    localPhaseStart = now;
  } else if (localPhase == AMARILLO && (now - localPhaseStart) >= LOCAL_YELLOW_MS) {
    localIdx = (localIdx + 1) % 4;
    for (int i = 0; i < 4; i++) applyLight(i, i == localIdx ? VERDE : ROJO);
    localPhase = VERDE;
    localPhaseStart = now;
  }
}

// ================================ SETUP ===================================

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== SmartTraffic ESP32 ===");

  for (int i = 0; i < 4; i++) {
    pinMode(accesos[i].trigPin, OUTPUT);
    digitalWrite(accesos[i].trigPin, LOW);
    pinMode(accesos[i].echoPin, INPUT);
    pinMode(accesos[i].redPin, OUTPUT);
    pinMode(accesos[i].yellowPin, OUTPUT);
    pinMode(accesos[i].greenPin, OUTPUT);
    applyLight(i, ROJO);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                     // menor latencia MQTT
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[WiFi] Conectando a \"%s\"", WIFI_SSID);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 15000UL) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] OK -> IP local: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[WiFi] no conectado, se reintenta en loop()");
  }

  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setBufferSize(1024);            // la telemetria > 256 bytes
  mqttClient.setKeepAlive(15);
  if (WiFi.status() == WL_CONNECTED) connectMqtt();
}

// ================================= LOOP ===================================

void loop() {
  maintainConnections();
  if (mqttClient.connected()) mqttClient.loop();

  unsigned long now = millis();

  // lectura round-robin de los 4 sensores (1 cada SENSOR_PERIOD_MS)
  if (now - accesos[sensorIndex].lastRead >= SENSOR_PERIOD_MS) {
    scanSensor(sensorIndex);
    accesos[sensorIndex].lastRead = now;
    sensorIndex = (sensorIndex + 1) % 4;
  }

  // telemetria periodica
  if (now - lastTelemetry >= TELEMETRY_PERIOD_MS) {
    lastTelemetry = now;
    publishTelemetry();
  }

  // las luces las manda el servidor; si no responde, ciclo autonomo local
  if (serverAlive()) {
    localPhaseStart = 0;    // al volver el servidor se retoma su estado
  } else {
    localCycle(now);
  }

  // feedback por consola cada 10 s
  static unsigned long lastLog = 0;
  if (now - lastLog >= 10000UL) {
    lastLog = now;
    Serial.printf("[Estado] servidor=%s  WiFi=%d  MQTT=%d\n",
                  serverAlive() ? "SI" : "NO (ciclo local)",
                  WiFi.status() == WL_CONNECTED,
                  mqttClient.connected());
    for (int i = 0; i < 4; i++) {
      Serial.printf("   %-6s d=%6.1f cm  v=%2u  %s  %s\n",
                    accesos[i].id, accesos[i].distance, accesos[i].vehicles,
                    lightName(accesos[i].light),
                    accesos[i].occupied ? "[carrito]" : "");
    }
  }
}
