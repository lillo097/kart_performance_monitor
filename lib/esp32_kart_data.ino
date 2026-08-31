#include <Arduino.h>
#include <BluetoothSerial.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
  #error Bluetooth non abilitato per questa scheda/compilazione.
#endif

#if !defined(CONFIG_BT_SPP_ENABLED)
  #error BluetoothSerial/SPP non disponibile. Serve un ESP32 classico con Bluetooth Classic.
#endif

// ============================================================
// CONFIGURAZIONE WIFI / MQTT
// ============================================================
static const char *WIFI_SSID     = "iPhone di Livio";
static const char *WIFI_PASSWORD = "12345678";

static const char *MQTT_BROKER = "broker.hivemq.com";
static const uint16_t MQTT_PORT = 1883;
static const char *MQTT_TOPIC = "sensors2mqtt-glo2/esp32/location";

static const uint16_t MQTT_BUFFER_SIZE = 512;
static const uint16_t MQTT_KEEPALIVE_SECONDS = 20;
static const uint16_t MQTT_SOCKET_TIMEOUT_SECONDS = 3;

// 200 ms = massimo 5 payload al secondo.
static const uint32_t MQTT_PUBLISH_INTERVAL_MS = 200;

// Se Wi-Fi è sotto questa soglia, si evita di inviare payload:
// aiuta a evitare publish in condizioni radio molto instabili.
static const int WIFI_MIN_RSSI_DBM = -82;

// ============================================================
// CONFIGURAZIONE GARMIN GLO 2
// ============================================================
static const char *GLO2_MAC = "14:13:0B:C0:B2:1E";
static const char *PAIRING_PIN = "1234";
static const uint8_t PAIRING_PIN_LEN = 4;

static const uint32_t CONNECT_RETRY_MAX = 5;
static const uint32_t CONNECT_RETRY_DELAY_MS = 1000;
static const uint32_t RECONNECT_DELAY_MS = 2000;
static const uint32_t MAX_NMEA_LINE_LENGTH = 160;

// ============================================================
// LOGGING
// ============================================================
static const uint32_t GPS_ACQUIRING_LOG_INTERVAL_MS = 3000;
static const uint32_t NMEA_WAIT_LOG_INTERVAL_MS = 3000;
static const uint32_t TELEMETRY_LOG_INTERVAL_MS = 5000;
static const uint32_t WIFI_WEAK_LOG_INTERVAL_MS = 5000;

// ============================================================
// RETE: RETRY NON BLOCCANTE
// ============================================================
static volatile bool wifiConnected = false;
static volatile bool mqttNeedsReconnect = true;

static unsigned long nextWifiRetryMs = 0;
static unsigned long nextMqttRetryMs = 0;

static uint32_t wifiRetryDelayMs = 1000;
static uint32_t mqttRetryDelayMs = 1000;

static const uint32_t WIFI_RETRY_MIN_MS = 1000;
static const uint32_t WIFI_RETRY_MAX_MS = 30000;

static const uint32_t MQTT_RETRY_MIN_MS = 1000;
static const uint32_t MQTT_RETRY_MAX_MS = 15000;

// ============================================================
// STATO GPS / NMEA
// ============================================================
struct GpsState {
  char rmc_status = 0;

  double latitude = 0.0;
  double longitude = 0.0;

  double speed_knots = 0.0;
  double speed_kmph = 0.0;
  double track_true_deg = 0.0;

  uint8_t gps_qual = 0;
  uint8_t num_sats = 0;
  uint8_t gsa_fix_type = 0;

  double hdop = 0.0;

  bool valid_fix = false;
};

static GpsState gps;

// ============================================================
// STATO RUNTIME / OGGETTI
// ============================================================
enum GpsRuntimeState {
  GPS_STATE_BT_DISCONNECTED,
  GPS_STATE_WAITING_NMEA,
  GPS_STATE_ACQUIRING_FIX,
  GPS_STATE_FIX_READY
};

BluetoothSerial SerialBT;
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

static bool btConnected = false;
static String nmeaLine;

static unsigned long lastPublishMs = 0;
static unsigned long lastNmeaSentenceMs = 0;

static unsigned long lastNmeaWaitLogMs = 0;
static unsigned long lastAcquiringLogMs = 0;
static unsigned long lastTelemetryLogMs = 0;
static unsigned long lastWeakWifiLogMs = 0;

static GpsRuntimeState currentGpsState = GPS_STATE_BT_DISCONNECTED;
static GpsRuntimeState previousGpsState = GPS_STATE_BT_DISCONNECTED;

// ============================================================
// UTILITY
// ============================================================
double safeDouble(const char *text, double fallback = 0.0) {
  if (!text || !*text) {
    return fallback;
  }

  char *endPtr = nullptr;
  double value = strtod(text, &endPtr);

  return endPtr == text ? fallback : value;
}

double knotsToKmph(double knots) {
  return knots * 1.852;
}

double nmeaCoordinateToDecimal(double value) {
  int degrees = static_cast<int>(value / 100.0);
  double minutes = value - (degrees * 100.0);

  return degrees + (minutes / 60.0);
}

void updateValidFix() {
  bool validRmc = gps.rmc_status == 'A';
  bool validGga = gps.gps_qual > 0;
  bool validGsa = gps.gsa_fix_type > 1;

  gps.valid_fix = validRmc || validGga || validGsa;
}

const char *fixQualityLabel(uint8_t quality) {
  switch (quality) {
    case 0: return "nessun fix";
    case 1: return "GPS";
    case 2: return "DGPS";
    case 3: return "PPS";
    case 4: return "RTK fixed";
    case 5: return "RTK float";
    case 6: return "stimato";
    case 7: return "manuale";
    case 8: return "simulazione";
    default: return "sconosciuto";
  }
}

// ============================================================
// LOG GPS A STATI
// ============================================================
void setGpsRuntimeState(GpsRuntimeState newState) {
  currentGpsState = newState;

  if (currentGpsState == previousGpsState) {
    return;
  }

  previousGpsState = currentGpsState;

  switch (currentGpsState) {
    case GPS_STATE_BT_DISCONNECTED:
      Serial.println("[GPS] Bluetooth GLO 2 non connesso.");
      break;

    case GPS_STATE_WAITING_NMEA:
      Serial.println("[GPS] GLO 2 connesso. Attendo stream NMEA...");
      break;

    case GPS_STATE_ACQUIRING_FIX:
      Serial.println("[GPS] Stream NMEA attivo. Acquisizione segnale GPS in corso...");
      break;

    case GPS_STATE_FIX_READY:
      Serial.println("[GPS] Segnale GPS acquisito. Invio telemetria MQTT.");
      break;
  }
}

void updateGpsRuntimeState() {
  if (!btConnected || !SerialBT.connected()) {
    setGpsRuntimeState(GPS_STATE_BT_DISCONNECTED);
    return;
  }

  if (lastNmeaSentenceMs == 0) {
    setGpsRuntimeState(GPS_STATE_WAITING_NMEA);
    return;
  }

  if (!gps.valid_fix) {
    setGpsRuntimeState(GPS_STATE_ACQUIRING_FIX);
    return;
  }

  setGpsRuntimeState(GPS_STATE_FIX_READY);
}

void printPeriodicGpsLog() {
  unsigned long now = millis();

  if (currentGpsState == GPS_STATE_WAITING_NMEA &&
      now - lastNmeaWaitLogMs >= NMEA_WAIT_LOG_INTERVAL_MS) {
    Serial.println("[GPS] In attesa di frasi NMEA dal Garmin GLO 2...");
    lastNmeaWaitLogMs = now;
  }

  if (currentGpsState == GPS_STATE_ACQUIRING_FIX &&
      now - lastAcquiringLogMs >= GPS_ACQUIRING_LOG_INTERVAL_MS) {
    Serial.printf(
      "[GPS] Acquisizione | sats=%u | qualità=%u (%s) | HDOP=%.1f\n",
      gps.num_sats,
      gps.gps_qual,
      fixQualityLabel(gps.gps_qual),
      gps.hdop
    );

    lastAcquiringLogMs = now;
  }

  if (currentGpsState == GPS_STATE_FIX_READY &&
      now - lastTelemetryLogMs >= TELEMETRY_LOG_INTERVAL_MS) {
    Serial.printf(
      "[GPS] Telemetria | sats=%u | qualità=%u (%s) | HDOP=%.1f | %.6f, %.6f | %.2f km/h | WiFi=%d dBm\n",
      gps.num_sats,
      gps.gps_qual,
      fixQualityLabel(gps.gps_qual),
      gps.hdop,
      gps.latitude,
      gps.longitude,
      gps.speed_kmph,
      WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -127
    );

    lastTelemetryLogMs = now;
  }
}

// ============================================================
// PARSER NMEA
// ============================================================
void parseRMC(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';

  char *token = strtok(buffer, ",");
  if (!token) {
    return;
  }

  token = strtok(nullptr, ","); // UTC
  token = strtok(nullptr, ","); // A/V

  if (token && token[0]) {
    gps.rmc_status = token[0];
  }

  token = strtok(nullptr, ","); // latitudine
  double rawLatitude = safeDouble(token);

  token = strtok(nullptr, ","); // N/S
  if (rawLatitude != 0.0) {
    gps.latitude = nmeaCoordinateToDecimal(rawLatitude);

    if (token && token[0] == 'S') {
      gps.latitude = -gps.latitude;
    }
  }

  token = strtok(nullptr, ","); // longitudine
  double rawLongitude = safeDouble(token);

  token = strtok(nullptr, ","); // E/W
  if (rawLongitude != 0.0) {
    gps.longitude = nmeaCoordinateToDecimal(rawLongitude);

    if (token && token[0] == 'W') {
      gps.longitude = -gps.longitude;
    }
  }

  token = strtok(nullptr, ","); // velocità nodi
  if (token && token[0]) {
    gps.speed_knots = safeDouble(token);
    gps.speed_kmph = knotsToKmph(gps.speed_knots);
  }

  token = strtok(nullptr, ","); // rotta
  if (token && token[0]) {
    gps.track_true_deg = safeDouble(token);
  }

  updateValidFix();
}

void parseGGA(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';

  char *token = strtok(buffer, ",");
  if (!token) {
    return;
  }

  token = strtok(nullptr, ","); // UTC

  token = strtok(nullptr, ","); // latitudine
  double rawLatitude = safeDouble(token);

  token = strtok(nullptr, ","); // N/S
  if (rawLatitude != 0.0) {
    gps.latitude = nmeaCoordinateToDecimal(rawLatitude);

    if (token && token[0] == 'S') {
      gps.latitude = -gps.latitude;
    }
  }

  token = strtok(nullptr, ","); // longitudine
  double rawLongitude = safeDouble(token);

  token = strtok(nullptr, ","); // E/W
  if (rawLongitude != 0.0) {
    gps.longitude = nmeaCoordinateToDecimal(rawLongitude);

    if (token && token[0] == 'W') {
      gps.longitude = -gps.longitude;
    }
  }

  token = strtok(nullptr, ","); // qualità
  gps.gps_qual = token ? static_cast<uint8_t>(atoi(token)) : 0;

  token = strtok(nullptr, ","); // satelliti
  gps.num_sats = token ? static_cast<uint8_t>(atoi(token)) : 0;

  token = strtok(nullptr, ","); // HDOP
  gps.hdop = safeDouble(token);

  updateValidFix();
}

void parseGSA(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';

  char *token = strtok(buffer, ",");
  if (!token) {
    return;
  }

  token = strtok(nullptr, ","); // A/M
  token = strtok(nullptr, ","); // 1 / 2 / 3

  if (token && token[0]) {
    gps.gsa_fix_type = static_cast<uint8_t>(atoi(token));
  }

  updateValidFix();
}

void parseVTG(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';

  char *token = strtok(buffer, ",");
  if (!token) {
    return;
  }

  token = strtok(nullptr, ","); // rotta vera
  if (token && token[0]) {
    gps.track_true_deg = safeDouble(token);
  }

  token = strtok(nullptr, ","); // T
  token = strtok(nullptr, ","); // rotta magnetica
  token = strtok(nullptr, ","); // M

  token = strtok(nullptr, ","); // nodi
  if (token && token[0]) {
    gps.speed_knots = safeDouble(token);
  }

  token = strtok(nullptr, ","); // N
  token = strtok(nullptr, ","); // km/h

  if (token && token[0]) {
    gps.speed_kmph = safeDouble(token);
  }
}

void processNmeaLine(const String &line) {
  if (!line.startsWith("$")) {
    return;
  }

  lastNmeaSentenceMs = millis();

  if (line.startsWith("$GPRMC") || line.startsWith("$GNRMC")) {
    parseRMC(line.c_str());
  } else if (line.startsWith("$GPGGA") || line.startsWith("$GNGGA")) {
    parseGGA(line.c_str());
  } else if (line.startsWith("$GPGSA") || line.startsWith("$GNGSA")) {
    parseGSA(line.c_str());
  } else if (line.startsWith("$GPVTG") || line.startsWith("$GNVTG")) {
    parseVTG(line.c_str());
  }
}

// ============================================================
// WIFI: EVENTI E RIPRISTINO
// ============================================================
void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      wifiConnected = true;
      mqttNeedsReconnect = true;

      wifiRetryDelayMs = WIFI_RETRY_MIN_MS;
      nextWifiRetryMs = 0;

      Serial.print("[WIFI] Connesso | IP=");
      Serial.print(WiFi.localIP());
      Serial.print(" | RSSI=");
      Serial.print(WiFi.RSSI());
      Serial.println(" dBm");
      break;

    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      wifiConnected = false;
      mqttNeedsReconnect = true;

      Serial.printf(
        "[WIFI] Disconnesso | motivo=%d | retry=%lu ms\n",
        info.wifi_sta_disconnected.reason,
        static_cast<unsigned long>(wifiRetryDelayMs)
      );

      mqttClient.disconnect();

      nextWifiRetryMs = millis() + wifiRetryDelayMs;
      wifiRetryDelayMs = min(wifiRetryDelayMs * 2, WIFI_RETRY_MAX_MS);
      break;

    default:
      break;
  }
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    return;
  }

  wifiConnected = false;

  if (millis() < nextWifiRetryMs) {
    return;
  }

  Serial.println("[WIFI] Riconnessione hotspot...");

  WiFi.disconnect(false, false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  nextWifiRetryMs = millis() + wifiRetryDelayMs;
}

// ============================================================
// MQTT: CONNESSIONE E RIPRISTINO
// ============================================================
void mqttCallback(char *topic, byte *payload, unsigned int length) {
  // Questo progetto pubblica solo telemetria.
}

void maintainMqtt() {
  if (!wifiConnected || WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (mqttClient.connected()) {
    mqttClient.loop();
    return;
  }

  if (millis() < nextMqttRetryMs) {
    return;
  }

  String clientId = "ESP32-GLO2-" + WiFi.macAddress();
  clientId.replace(":", "");

  Serial.println("[MQTT] Riconnessione broker...");

  bool ok = mqttClient.connect(clientId.c_str());

  if (ok) {
    mqttNeedsReconnect = false;
    mqttRetryDelayMs = MQTT_RETRY_MIN_MS;
    nextMqttRetryMs = 0;

    Serial.println("[MQTT] Broker connesso.");
  } else {
    Serial.printf(
      "[MQTT] Connessione fallita | stato=%d | retry=%lu ms\n",
      mqttClient.state(),
      static_cast<unsigned long>(mqttRetryDelayMs)
    );

    nextMqttRetryMs = millis() + mqttRetryDelayMs;
    mqttRetryDelayMs = min(mqttRetryDelayMs * 2, MQTT_RETRY_MAX_MS);
  }
}

// ============================================================
// PUBBLICAZIONE MQTT
// ============================================================
void publishPayload() {
  if (!wifiConnected || !mqttClient.connected()) {
    return;
  }

  int rssi = WiFi.RSSI();

  if (rssi < WIFI_MIN_RSSI_DBM) {
    if (millis() - lastWeakWifiLogMs >= WIFI_WEAK_LOG_INTERVAL_MS) {
      Serial.printf(
        "[WIFI] Segnale debole (%d dBm), invio telemetria rimandato.\n",
        rssi
      );

      lastWeakWifiLogMs = millis();
    }

    return;
  }

  StaticJsonDocument<256> doc;

  doc["satellites_used"] = gps.num_sats;
  doc["latitude"] = gps.latitude;
  doc["longitude"] = gps.longitude;
  doc["speed_kmph"] = gps.speed_kmph;
  doc["fix_valid"] = gps.valid_fix;
  doc["fix_quality"] = gps.gps_qual;
  doc["fix_type"] = gps.gsa_fix_type;
  doc["hdop"] = gps.hdop;

  char jsonBuffer[256];
  size_t jsonLength = serializeJson(doc, jsonBuffer, sizeof(jsonBuffer));

  if (jsonLength == 0) {
    Serial.println("[MQTT] Errore serializzazione JSON.");
    return;
  }

  bool published = mqttClient.publish(
    MQTT_TOPIC,
    reinterpret_cast<const uint8_t *>(jsonBuffer),
    jsonLength,
    false
  );

  if (!published) {
    Serial.printf(
      "[MQTT] Publish fallito | mqtt=%d | WiFi=%d | RSSI=%d dBm | reset socket\n",
      mqttClient.connected() ? 1 : 0,
      WiFi.status(),
      WiFi.RSSI()
    );

    // Evita di restare con un socket half-open:
    // il prossimo maintainMqtt() stabilirà una nuova sessione.
    mqttClient.disconnect();

    mqttNeedsReconnect = true;
    nextMqttRetryMs = millis() + 250;
    mqttRetryDelayMs = MQTT_RETRY_MIN_MS;
  }
}

// ============================================================
// BLUETOOTH GLO 2
// ============================================================
bool connectToGLO2() {
  BTAddress address(GLO2_MAC);

  SerialBT.setPin(PAIRING_PIN, PAIRING_PIN_LEN);

  for (uint32_t attempt = 1; attempt <= CONNECT_RETRY_MAX; attempt++) {
    Serial.printf(
      "[BT] Connessione GLO 2 | tentativo %lu/%lu\n",
      static_cast<unsigned long>(attempt),
      static_cast<unsigned long>(CONNECT_RETRY_MAX)
    );

    bool connectedNow = SerialBT.connect(address);

    if (connectedNow && SerialBT.connected()) {
      Serial.println("[BT] GLO 2 connesso via Bluetooth.");

      btConnected = true;
      nmeaLine = "";
      lastNmeaSentenceMs = 0;

      setGpsRuntimeState(GPS_STATE_WAITING_NMEA);
      return true;
    }

    SerialBT.disconnect();
    delay(CONNECT_RETRY_DELAY_MS);
  }

  Serial.println("[BT] Connessione GLO 2 fallita.");

  btConnected = false;
  setGpsRuntimeState(GPS_STATE_BT_DISCONNECTED);

  return false;
}

void readBluetoothNmea() {
  while (SerialBT.connected()) {
    // Rete sempre mantenuta senza interrompere il BT/NMEA.
    maintainWiFi();
    maintainMqtt();

    while (SerialBT.available()) {
      int value = SerialBT.read();

      if (value < 0) {
        continue;
      }

      char character = static_cast<char>(value);

      if (character == '\n') {
        nmeaLine.trim();

        if (nmeaLine.length() > 0) {
          processNmeaLine(nmeaLine);
        }

        nmeaLine = "";
        continue;
      }

      if (character == '\r') {
        continue;
      }

      if (nmeaLine.length() < MAX_NMEA_LINE_LENGTH) {
        nmeaLine += character;
      } else {
        Serial.println("[NMEA] Riga troppo lunga: buffer resettato.");
        nmeaLine = "";
      }
    }

    updateGpsRuntimeState();
    printPeriodicGpsLog();

    if (millis() - lastPublishMs >= MQTT_PUBLISH_INTERVAL_MS) {
      // Non invia finché non è arrivata almeno una frase NMEA.
      if (lastNmeaSentenceMs > 0) {
        publishPayload();
      }

      lastPublishMs = millis();
    }

    delay(2);
  }

  Serial.println("[BT] Connessione GLO 2 persa.");

  btConnected = false;
  setGpsRuntimeState(GPS_STATE_BT_DISCONNECTED);
}

// ============================================================
// SETUP / LOOP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(800);

  Serial.println();
  Serial.println("=== ESP32 Garmin GLO 2 -> MQTT · resilient telemetry ===");

  // Wi-Fi station stabile e a bassa latenza.
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.onEvent(onWiFiEvent);

  Serial.println("[WIFI] Avvio connessione hotspot...");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setKeepAlive(MQTT_KEEPALIVE_SECONDS);
  mqttClient.setSocketTimeout(MQTT_SOCKET_TIMEOUT_SECONDS);

  if (!mqttClient.setBufferSize(MQTT_BUFFER_SIZE)) {
    Serial.println("[MQTT] Errore: impossibile allocare buffer MQTT.");

    while (true) {
      delay(1000);
    }
  }

  mqttClient.setCallback(mqttCallback);

  if (!SerialBT.begin("ESP32-GLO2", true)) {
    Serial.println("[BT] BluetoothSerial.begin() fallita.");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[BT] Bluetooth Classic inizializzato in modalità client.");
}

void loop() {
  maintainWiFi();
  maintainMqtt();

  if (!btConnected || !SerialBT.connected()) {
    btConnected = false;

    if (!connectToGLO2()) {
      delay(RECONNECT_DELAY_MS);
      return;
    }
  }

  readBluetoothNmea();
}