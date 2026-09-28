#include <Arduino.h>
#include <ArduinoJson.h>
#include <BluetoothSerial.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <math.h>

#include "esp_bt.h"   // per esp_bt_controller_mem_release()

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth non abilitato per questa scheda/compilazione.
#endif
#if !defined(CONFIG_BT_SPP_ENABLED)
#error BluetoothSerial/SPP non disponibile.
#endif

// ============================================================
// WIFI
// ============================================================
static const char *WIFI_SSID     = "iPhone di Livio";
static const char *WIFI_PASSWORD = "12345678";

static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 10000;
static const uint32_t WIFI_RETRY_MIN_MS       = 2000;
static const uint32_t WIFI_RETRY_MAX_MS       = 8000;

// ============================================================
// MQTT
// ============================================================
static const char *MQTT_BROKER  = "broker.hivemq.com";
static const uint16_t MQTT_PORT = 1883;

static const char *MQTT_TOPIC = "sensors2mqtt-glo2/esp32/location";

static const char *STATUS_TOPIC_HOTSPOT = "sensors2mqtt-glo2/esp32/status/hotspot";
static const char *STATUS_TOPIC_MQTT    = "sensors2mqtt-glo2/esp32/status/mqtt";
static const char *STATUS_TOPIC_NTC     = "sensors2mqtt-glo2/esp32/status/ntc";
static const char *STATUS_TOPIC_AS5600  = "sensors2mqtt-glo2/esp32/status/as5600";
static const char *STATUS_TOPIC_IR_RPM  = "sensors2mqtt-glo2/esp32/status/ir_rpm";
static const char *STATUS_TOPIC_GPS     = "sensors2mqtt-glo2/esp32/status/gps";

static const uint16_t MQTT_BUFFER_SIZE            = 768;
static const uint16_t MQTT_KEEPALIVE_SECONDS      = 20;
static const uint16_t MQTT_SOCKET_TIMEOUT_SECONDS = 2;

static const uint32_t MQTT_PUBLISH_INTERVAL_MS = 200;
static const uint32_t STATUS_CHECK_INTERVAL_MS = 2000;
static const uint32_t MQTT_RETRY_MIN_MS        = 250;
static const uint32_t MQTT_RETRY_MAX_MS        = 4000;

// ============================================================
// NTC 10K B3950 (GPIO34)
// ============================================================
static const uint8_t NTC_PIN = 34;
static const float NTC_VCC         = 3.30f;
static const float NTC_R_FIXED     = 3300.0f;
static const float NTC_R_NOMINAL   = 10000.0f;
static const float NTC_T_NOMINAL_C = 25.0f;
static const float NTC_BETA        = 3950.0f;
static const uint8_t  NTC_SAMPLES          = 8;
static const uint32_t NTC_READ_INTERVAL_MS = 200;
static const float    NTC_FILTER_ALPHA     = 0.65f;

static float    temperatureC     = NAN;
static bool     temperatureValid = false;
static uint32_t lastTemperatureReadMs = 0;

// ============================================================
// AS5600 (I2C)
// ============================================================
static const uint8_t AS5600_ADDRESS       = 0x36;
static const uint8_t AS5600_REG_STATUS    = 0x0B;
static const uint8_t AS5600_REG_RAW_ANGLE = 0x0C;
static const uint8_t  AS5600_SDA_PIN = 21;
static const uint8_t  AS5600_SCL_PIN = 22;
static const uint32_t AS5600_I2C_CLOCK_HZ     = 400000;
static const uint32_t AS5600_READ_INTERVAL_MS = 10;

static bool     as5600Present         = false;
static bool     as5600MagnetDetected  = false;
static bool     as5600MagnetTooWeak   = false;
static bool     as5600MagnetTooStrong = false;
static uint8_t  as5600Status          = 0;
static uint16_t as5600RawAngle        = 0;
static uint16_t as5600PreviousRawAngle = 0;
static float    as5600AngleDeg        = NAN;
static float    as5600Rpm             = 0.0f;
static uint32_t lastAs5600ReadMs      = 0;
static uint32_t as5600PreviousReadUs  = 0;
static bool     as5600FirstReading    = true;

// ============================================================
// IR RPM (GPIO27)
// ============================================================
static const uint8_t  IR_RPM_PIN               = 27;
static const uint8_t  IR_PULSES_PER_REVOLUTION = 1;
static const uint32_t IR_MIN_VALID_PERIOD_US    = 1500;
static const uint32_t IR_STOP_TIMEOUT_US        = 1000000;
static const uint32_t IR_RPM_UPDATE_INTERVAL_MS = 100;

volatile uint32_t irTotalPulses        = 0;
volatile uint32_t irLastEdgeUs         = 0;
volatile uint32_t irLatestPeriodUs     = 0;
volatile bool     irNewPeriodAvailable = false;

static float    irRpm             = 0.0f;
static uint32_t lastIrRpmUpdateMs = 0;
static int      irLastDigitalState    = HIGH;
static bool     irStateChangedAtLeastOnce = false;
static uint32_t irLastStateChangeMs   = 0;

// ============================================================
// GARMIN GLO 2
// ============================================================
static const char *GLO2_MAC = "14:13:0B:C0:B2:1E";
static const char *PAIRING_PIN       = "1234";
static const uint8_t PAIRING_PIN_LEN = 4;
static const uint32_t MAX_NMEA_LINE_LENGTH      = 160;
static const uint32_t GPS_RECONNECT_INTERVAL_MS = 5000;
static const uint32_t GPS_NMEA_TIMEOUT_MS       = 5000;

static const uint32_t GPS_TASK_STACK_SIZE = 6144;   // originale, sufficiente
static const UBaseType_t GPS_TASK_PRIORITY = 1;
static const BaseType_t  GPS_TASK_CORE     = 1;     // *** fix principale ***

static TaskHandle_t gpsTaskHandle = nullptr;
static volatile bool     btConnected        = false;
static volatile uint32_t lastNmeaSentenceMs = 0;
static String nmeaLine;

// ============================================================
// RETE
// ============================================================
static volatile bool wifiConnected = false;
static uint32_t nextWifiRetryMs   = 0;
static uint32_t nextMqttRetryMs   = 0;
static uint32_t wifiAttemptStartMs = 0;
static bool     wifiAttemptActive  = false;
static uint32_t wifiRetryDelayMs = WIFI_RETRY_MIN_MS;
static uint32_t mqttRetryDelayMs = MQTT_RETRY_MIN_MS;
static uint32_t lastPublishMs     = 0;
static uint32_t lastStatusCheckMs = 0;

// Diagnostica
static volatile uint32_t wifiDisconnectCount = 0;
static volatile uint32_t mqttDisconnectCount = 0;

// ============================================================
// STATUS COMPONENTI
// ============================================================
struct ComponentStatus {
  bool hotspot = false;
  bool mqtt    = false;
  bool ntc     = false;
  bool as5600  = false;
  bool irRpm   = false;
  bool gps     = false;
};
static ComponentStatus currentStatus;

static int8_t publishedHotspot = -1;
static int8_t publishedMqtt    = -1;
static int8_t publishedNtc     = -1;
static int8_t publishedAs5600  = -1;
static int8_t publishedIrRpm   = -1;
static int8_t publishedGps     = -1;
static bool forcePublishAllStatuses = true;

// ============================================================
// GPS STATE
// ============================================================
struct GpsState {
  char     rmcStatus = 0;
  double   latitude  = 0.0;
  double   longitude = 0.0;
  double   speedKnots = 0.0;
  double   speedKmph  = 0.0;
  double   trackTrueDeg = 0.0;
  uint8_t  fixQuality    = 0;
  uint8_t  satellitesUsed = 0;
  uint8_t  fixType        = 0;
  double   hdop = 0.0;
  bool     validFix = false;
};
static GpsState gps;
static portMUX_TYPE gpsMux = portMUX_INITIALIZER_UNLOCKED;

// ============================================================
// OGGETTI
// ============================================================
BluetoothSerial SerialBT;
WiFiClient      wifiClient;
PubSubClient    mqttClient(wifiClient);

// ============================================================
// UTILITY
// ============================================================
double safeDouble(const char *text, double fallback = 0.0) {
  if (!text || !*text) return fallback;
  char *endPtr = nullptr;
  double value = strtod(text, &endPtr);
  return endPtr == text ? fallback : value;
}
double knotsToKmph(double knots) { return knots * 1.852; }
double nmeaCoordinateToDecimal(double value) {
  int    degrees = static_cast<int>(value / 100.0);
  double minutes = value - (degrees * 100.0);
  return degrees + (minutes / 60.0);
}
void updateValidFix() {
  gps.validFix = (gps.rmcStatus == 'A') || (gps.fixQuality > 0) || (gps.fixType > 1);
}

// ============================================================
// NTC
// ============================================================
float readNtcTemperatureC() {
  uint32_t sumMillivolts = 0;
  for (uint8_t i = 0; i < NTC_SAMPLES; i++) {
    sumMillivolts += analogReadMilliVolts(NTC_PIN);
  }
  float voltage = (sumMillivolts / static_cast<float>(NTC_SAMPLES)) / 1000.0f;
  if (voltage < 0.03f || voltage > (NTC_VCC - 0.03f)) return NAN;
  float rNtc = NTC_R_FIXED * ((NTC_VCC / voltage) - 1.0f);
  if (rNtc <= 0.0f || isnan(rNtc)) return NAN;
  float inverseKelvin = (1.0f / (NTC_T_NOMINAL_C + 273.15f)) +
                        (log(rNtc / NTC_R_NOMINAL) / NTC_BETA);
  return (1.0f / inverseKelvin) - 273.15f;
}

void updateTemperature() {
  uint32_t now = millis();
  if (now - lastTemperatureReadMs < NTC_READ_INTERVAL_MS) return;
  lastTemperatureReadMs = now;
  float raw = readNtcTemperatureC();
  if (isnan(raw)) { temperatureValid = false; temperatureC = NAN; return; }
  if (!temperatureValid || isnan(temperatureC)) {
    temperatureC = raw;
  } else {
    temperatureC = NTC_FILTER_ALPHA * raw + (1.0f - NTC_FILTER_ALPHA) * temperatureC;
  }
  temperatureValid = true;
}

// ============================================================
// AS5600
// ============================================================
bool as5600ReadBytes(uint8_t reg, uint8_t *buffer, uint8_t length) {
  Wire.beginTransmission(AS5600_ADDRESS);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  uint8_t received = Wire.requestFrom(AS5600_ADDRESS, length);
  if (received != length) return false;
  for (uint8_t i = 0; i < length; i++) buffer[i] = Wire.read();
  return true;
}

bool detectAS5600() {
  Wire.beginTransmission(AS5600_ADDRESS);
  return Wire.endTransmission() == 0;
}

void updateAS5600() {
  uint32_t nowMs = millis();
  if (nowMs - lastAs5600ReadMs < AS5600_READ_INTERVAL_MS) return;
  lastAs5600ReadMs = nowMs;

  uint8_t statusBuffer[1];
  uint8_t angleBuffer[2];

  if (!as5600ReadBytes(AS5600_REG_STATUS, statusBuffer, 1)) {
    as5600Present = false; as5600MagnetDetected = false;
    as5600MagnetTooWeak = false; as5600MagnetTooStrong = false;
    as5600AngleDeg = NAN; as5600Rpm = 0.0f; as5600FirstReading = true;
    return;
  }

  as5600Present = true;
  as5600Status  = statusBuffer[0];
  as5600MagnetDetected  = (as5600Status & 0x20) != 0;
  as5600MagnetTooWeak   = (as5600Status & 0x10) != 0;
  as5600MagnetTooStrong = (as5600Status & 0x08) != 0;

  if (!as5600ReadBytes(AS5600_REG_RAW_ANGLE, angleBuffer, 2)) {
    as5600Present = false; as5600AngleDeg = NAN;
    as5600Rpm = 0.0f; as5600FirstReading = true;
    return;
  }

  as5600RawAngle = ((static_cast<uint16_t>(angleBuffer[0]) << 8) |
                    static_cast<uint16_t>(angleBuffer[1])) & 0x0FFF;
  as5600AngleDeg = as5600RawAngle * 360.0f / 4096.0f;

  bool fieldValid = as5600MagnetDetected &&
                    !as5600MagnetTooWeak && !as5600MagnetTooStrong;
  if (!fieldValid) { as5600Rpm = 0.0f; as5600FirstReading = true; return; }

  uint32_t nowUs = micros();
  if (as5600FirstReading) {
    as5600PreviousRawAngle = as5600RawAngle;
    as5600PreviousReadUs   = nowUs;
    as5600FirstReading     = false;
    return;
  }
  uint32_t deltaTimeUs = nowUs - as5600PreviousReadUs;
  if (deltaTimeUs == 0) return;
  int32_t deltaRaw = static_cast<int32_t>(as5600RawAngle) -
                     static_cast<int32_t>(as5600PreviousRawAngle);
  if (deltaRaw >  2048) deltaRaw -= 4096;
  if (deltaRaw < -2048) deltaRaw += 4096;
  float instantaneousRpm = (deltaRaw * 60000000.0f) / (4096.0f * deltaTimeUs);
  as5600Rpm = 0.35f * instantaneousRpm + 0.65f * as5600Rpm;
  as5600PreviousRawAngle = as5600RawAngle;
  as5600PreviousReadUs   = nowUs;
}

// ============================================================
// IR RPM
// ============================================================
void IRAM_ATTR onIrPulse() {
  uint32_t nowUs = micros();
  if (irLastEdgeUs == 0) { irLastEdgeUs = nowUs; irTotalPulses++; return; }
  uint32_t periodUs = nowUs - irLastEdgeUs;
  if (periodUs < IR_MIN_VALID_PERIOD_US) return;
  irLastEdgeUs = nowUs;
  irLatestPeriodUs = periodUs;
  irNewPeriodAvailable = true;
  irTotalPulses++;
}

void updateIrRpm() {
  uint32_t nowMs = millis();
  int currentDigitalState = digitalRead(IR_RPM_PIN);
  if (currentDigitalState != irLastDigitalState) {
    irLastDigitalState = currentDigitalState;
    irStateChangedAtLeastOnce = true;
    irLastStateChangeMs = nowMs;
  }
  if (nowMs - lastIrRpmUpdateMs < IR_RPM_UPDATE_INTERVAL_MS) return;
  lastIrRpmUpdateMs = nowMs;

  uint32_t periodUs = 0;
  uint32_t lastEdgeUs = 0;
  bool hasNewPeriod = false;

  noInterrupts();
  if (irNewPeriodAvailable) {
    periodUs = irLatestPeriodUs;
    irNewPeriodAvailable = false;
    hasNewPeriod = true;
  }
  lastEdgeUs = irLastEdgeUs;
  interrupts();

  if (hasNewPeriod && periodUs > 0) {
    irRpm = 60000000.0f / (periodUs * IR_PULSES_PER_REVOLUTION);
  }
  if (lastEdgeUs == 0 ||
      (uint32_t)(micros() - lastEdgeUs) > IR_STOP_TIMEOUT_US) {
    irRpm = 0.0f;
  }
}

// ============================================================
// STATUS PUBLISH
// ============================================================
bool publishComponentStatus(const char *topic, const char *component, bool present) {
  if (!mqttClient.connected()) return false;
  StaticJsonDocument<128> doc;
  doc["sensor"] = component;
  doc["present"] = present;
  doc["timestamp_ms"] = millis();
  char buffer[128];
  size_t length = serializeJson(doc, buffer, sizeof(buffer));
  if (length == 0) return false;
  return mqttClient.publish(topic, reinterpret_cast<const uint8_t *>(buffer),
                            length, true);
}

void publishStatusIfChanged(const char *topic, const char *component,
                            bool value, int8_t &lastPublished) {
  if (forcePublishAllStatuses || lastPublished == -1 ||
      lastPublished != (value ? 1 : 0)) {
    if (publishComponentStatus(topic, component, value)) {
      lastPublished = value ? 1 : 0;
    }
  }
}

void updateAndPublishComponentStatuses() {
  currentStatus.hotspot = WiFi.status() == WL_CONNECTED;
  currentStatus.mqtt    = mqttClient.connected();
  currentStatus.ntc     = temperatureValid && !isnan(temperatureC);
  currentStatus.as5600  = as5600Present;
  currentStatus.irRpm   = irStateChangedAtLeastOnce || (digitalRead(IR_RPM_PIN) == LOW);

  uint32_t lastNmeaCopy = lastNmeaSentenceMs;
  currentStatus.gps = btConnected && lastNmeaCopy > 0 &&
                      (uint32_t)(millis() - lastNmeaCopy) <= GPS_NMEA_TIMEOUT_MS;

  publishStatusIfChanged(STATUS_TOPIC_HOTSPOT, "hotspot", currentStatus.hotspot, publishedHotspot);
  publishStatusIfChanged(STATUS_TOPIC_MQTT, "mqtt", currentStatus.mqtt, publishedMqtt);
  publishStatusIfChanged(STATUS_TOPIC_NTC, "ntc", currentStatus.ntc, publishedNtc);
  publishStatusIfChanged(STATUS_TOPIC_AS5600, "as5600", currentStatus.as5600, publishedAs5600);
  publishStatusIfChanged(STATUS_TOPIC_IR_RPM, "ir_rpm", currentStatus.irRpm, publishedIrRpm);
  publishStatusIfChanged(STATUS_TOPIC_GPS, "gps", currentStatus.gps, publishedGps);

  forcePublishAllStatuses = false;
}

// ============================================================
// NMEA PARSER
// ============================================================
void parseRMC(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';
  char *token = strtok(buffer, ",");
  if (!token) return;
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  if (token && token[0]) gps.rmcStatus = token[0];
  token = strtok(nullptr, ",");
  double rawLat = safeDouble(token);
  token = strtok(nullptr, ",");
  if (rawLat != 0.0) {
    gps.latitude = nmeaCoordinateToDecimal(rawLat);
    if (token && token[0] == 'S') gps.latitude = -gps.latitude;
  }
  token = strtok(nullptr, ",");
  double rawLon = safeDouble(token);
  token = strtok(nullptr, ",");
  if (rawLon != 0.0) {
    gps.longitude = nmeaCoordinateToDecimal(rawLon);
    if (token && token[0] == 'W') gps.longitude = -gps.longitude;
  }
  token = strtok(nullptr, ",");
  if (token && token[0]) {
    gps.speedKnots = safeDouble(token);
    gps.speedKmph  = knotsToKmph(gps.speedKnots);
  }
  token = strtok(nullptr, ",");
  if (token && token[0]) gps.trackTrueDeg = safeDouble(token);
  updateValidFix();
}

void parseGGA(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';
  char *token = strtok(buffer, ",");
  if (!token) return;
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  double rawLat = safeDouble(token);
  token = strtok(nullptr, ",");
  if (rawLat != 0.0) {
    gps.latitude = nmeaCoordinateToDecimal(rawLat);
    if (token && token[0] == 'S') gps.latitude = -gps.latitude;
  }
  token = strtok(nullptr, ",");
  double rawLon = safeDouble(token);
  token = strtok(nullptr, ",");
  if (rawLon != 0.0) {
    gps.longitude = nmeaCoordinateToDecimal(rawLon);
    if (token && token[0] == 'W') gps.longitude = -gps.longitude;
  }
  token = strtok(nullptr, ",");
  gps.fixQuality = token ? static_cast<uint8_t>(atoi(token)) : 0;
  token = strtok(nullptr, ",");
  gps.satellitesUsed = token ? static_cast<uint8_t>(atoi(token)) : 0;
  token = strtok(nullptr, ",");
  gps.hdop = safeDouble(token);
  updateValidFix();
}

void parseGSA(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';
  char *token = strtok(buffer, ",");
  if (!token) return;
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  if (token && token[0]) gps.fixType = static_cast<uint8_t>(atoi(token));
  updateValidFix();
}

void parseVTG(const char *line) {
  char buffer[256];
  strncpy(buffer, line, sizeof(buffer) - 1);
  buffer[sizeof(buffer) - 1] = '\0';
  char *token = strtok(buffer, ",");
  if (!token) return;
  token = strtok(nullptr, ",");
  if (token && token[0]) gps.trackTrueDeg = safeDouble(token);
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  if (token && token[0]) gps.speedKnots = safeDouble(token);
  token = strtok(nullptr, ","); token = strtok(nullptr, ",");
  if (token && token[0]) gps.speedKmph = safeDouble(token);
}

void processNmeaLine(const String &line) {
  if (!line.startsWith("$")) return;
  portENTER_CRITICAL(&gpsMux);
  if (line.startsWith("$GPRMC") || line.startsWith("$GNRMC")) {
    parseRMC(line.c_str());
  } else if (line.startsWith("$GPGGA") || line.startsWith("$GNGGA")) {
    parseGGA(line.c_str());
  } else if (line.startsWith("$GPGSA") || line.startsWith("$GNGSA")) {
    parseGSA(line.c_str());
  } else if (line.startsWith("$GPVTG") || line.startsWith("$GNVTG")) {
    parseVTG(line.c_str());
  }
  portEXIT_CRITICAL(&gpsMux);
  lastNmeaSentenceMs = millis();
}

// ============================================================
// GPS TASK  (core 1)
// ============================================================
void clearGpsState() {
  portENTER_CRITICAL(&gpsMux);
  gps.rmcStatus = 0;
  gps.latitude = 0.0; gps.longitude = 0.0;
  gps.speedKnots = 0.0; gps.speedKmph = 0.0;
  gps.trackTrueDeg = 0.0;
  gps.fixQuality = 0; gps.satellitesUsed = 0; gps.fixType = 0;
  gps.hdop = 0.0;
  gps.validFix = false;
  portEXIT_CRITICAL(&gpsMux);
  lastNmeaSentenceMs = 0;
}

void readBluetoothNmeaNonBlocking() {
  if (!SerialBT.connected()) return;
  while (SerialBT.available()) {
    int value = SerialBT.read();
    if (value < 0) continue;
    char c = static_cast<char>(value);
    if (c == '\n') {
      nmeaLine.trim();
      if (nmeaLine.length() > 0) processNmeaLine(nmeaLine);
      nmeaLine = "";
      continue;
    }
    if (c == '\r') continue;
    if (nmeaLine.length() < MAX_NMEA_LINE_LENGTH) {
      nmeaLine += c;
    } else {
      nmeaLine = "";
    }
  }
}

void gpsBluetoothTask(void *parameter) {
  // Lascia stabilizzare WiFi/MQTT prima di aprire SPP: riduce il picco
  // di allocazioni simultanee che nei test causava il panic della heap.
  vTaskDelay(pdMS_TO_TICKS(3000));

  BTAddress address(GLO2_MAC);

  for (;;) {
    if (!SerialBT.connected()) {
      if (btConnected) {
        btConnected = false;
        clearGpsState();
        Serial.println("[GPS] Garmin GLO2 perso.");
      }

      Serial.println("[BT] Tentativo connessione Garmin GLO2...");
      SerialBT.setPin(PAIRING_PIN, PAIRING_PIN_LEN);
      bool ok = SerialBT.connect(address);

      if (ok && SerialBT.connected()) {
        btConnected = true;
        nmeaLine = "";
        lastNmeaSentenceMs = 0;
        Serial.println("[OK] GPS Garmin GLO2 connesso via Bluetooth.");
      } else {
        SerialBT.disconnect();
        btConnected = false;
        clearGpsState();
        Serial.println("[!!] GLO2 non raggiungibile; retry.");
        vTaskDelay(pdMS_TO_TICKS(GPS_RECONNECT_INTERVAL_MS));
        continue;
      }
    }

    readBluetoothNmeaNonBlocking();

    // Watchdog NMEA: BT up ma NMEA fermi per >5 s -> SPP considerato
    // bloccato (spesso perche' il buffer BT si e' riempito durante uno
    // stallo). Forza riconnessione.
    uint32_t lastNmea = lastNmeaSentenceMs;
    if (SerialBT.connected() && lastNmea > 0 &&
        (uint32_t)(millis() - lastNmea) > GPS_NMEA_TIMEOUT_MS) {
      Serial.println("[GPS] BT up ma NMEA fermi -> riconnessione forzata.");
      SerialBT.disconnect();
      btConnected = false;
      clearGpsState();
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }

    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// ============================================================
// WIFI
// ============================================================
void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      wifiConnected = true;
      wifiAttemptActive = false;
      wifiRetryDelayMs = WIFI_RETRY_MIN_MS;
      nextWifiRetryMs = 0;
      forcePublishAllStatuses = true;
      Serial.printf("[OK] WiFi | SSID=%s | IP=%s | RSSI=%d dBm\n",
                    WiFi.SSID().c_str(),
                    WiFi.localIP().toString().c_str(),
                    WiFi.RSSI());
      break;

    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      wifiConnected = false;
      wifiDisconnectCount++;
      forcePublishAllStatuses = true;
      Serial.printf("[WIFI] Disconnesso | motivo=%d | tot=%lu\n",
                    info.wifi_sta_disconnected.reason,
                    (unsigned long)wifiDisconnectCount);
      break;

    default: break;
  }
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    wifiAttemptActive = false;
    wifiRetryDelayMs = WIFI_RETRY_MIN_MS;
    return;
  }
  wifiConnected = false;

  if (wifiAttemptActive) {
    if ((uint32_t)(millis() - wifiAttemptStartMs) < WIFI_CONNECT_TIMEOUT_MS) {
      return;
    }
    wifiAttemptActive = false;
    wifiRetryDelayMs = min(wifiRetryDelayMs * 2, WIFI_RETRY_MAX_MS);
    nextWifiRetryMs = millis() + wifiRetryDelayMs;
    Serial.printf("[WIFI] Timeout, riprovo tra %lu ms\n",
                  (unsigned long)wifiRetryDelayMs);
    return;
  }
  if (millis() < nextWifiRetryMs) return;

  Serial.printf("[WIFI] Connessione: %s...\n", WIFI_SSID);
  WiFi.disconnect(false, false);
  delay(50);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiAttemptActive = true;
  wifiAttemptStartMs = millis();
}

// ============================================================
// MQTT
// ============================================================
void mqttCallback(char *topic, byte *payload, unsigned int length) {
  (void)topic; (void)payload; (void)length;
}

void maintainMqtt() {
  if (!wifiConnected || WiFi.status() != WL_CONNECTED) {
    if (mqttClient.connected()) {
      mqttClient.disconnect();
      mqttDisconnectCount++;
    }
    return;
  }
  if (mqttClient.connected()) { mqttClient.loop(); return; }
  if (millis() < nextMqttRetryMs) return;

  String clientId = "ESP32-KART-" + WiFi.macAddress();
  clientId.replace(":", "");
  bool ok = mqttClient.connect(clientId.c_str());
  if (ok) {
    mqttRetryDelayMs = MQTT_RETRY_MIN_MS;
    nextMqttRetryMs  = 0;
    forcePublishAllStatuses = true;
    publishedHotspot = -1; publishedMqtt = -1; publishedNtc = -1;
    publishedAs5600 = -1; publishedIrRpm = -1; publishedGps = -1;
    Serial.println("[OK] MQTT broker connesso.");
  } else {
    Serial.printf("[MQTT] fail | stato=%d\n", mqttClient.state());
    nextMqttRetryMs = millis() + mqttRetryDelayMs;
    mqttRetryDelayMs = min(mqttRetryDelayMs * 2, MQTT_RETRY_MAX_MS);
  }
}

// ============================================================
// TELEMETRIA
// ============================================================
void publishTelemetryPayload() {
  if (!wifiConnected || !mqttClient.connected()) return;

  GpsState gpsCopy;
  portENTER_CRITICAL(&gpsMux);
  gpsCopy = gps;
  portEXIT_CRITICAL(&gpsMux);

  uint32_t irPulsesCopy;
  noInterrupts();
  irPulsesCopy = irTotalPulses;
  interrupts();

  bool as5600FieldValid = as5600Present && as5600MagnetDetected &&
                          !as5600MagnetTooWeak && !as5600MagnetTooStrong;

  StaticJsonDocument<512> doc;
  doc["satellites_used"] = gpsCopy.satellitesUsed;
  doc["latitude"] = gpsCopy.latitude;
  doc["longitude"] = gpsCopy.longitude;
  doc["speed_kmph"] = gpsCopy.speedKmph;
  doc["fix_valid"] = gpsCopy.validFix;
  doc["fix_quality"] = gpsCopy.fixQuality;
  doc["fix_type"] = gpsCopy.fixType;
  doc["hdop"] = gpsCopy.hdop;
  if (temperatureValid && !isnan(temperatureC)) doc["temperature_c"] = temperatureC;
  else doc["temperature_c"] = nullptr;
  if (as5600Present && !isnan(as5600AngleDeg)) doc["as5600_angle_deg"] = as5600AngleDeg;
  else doc["as5600_angle_deg"] = nullptr;
  if (as5600FieldValid) doc["as5600_rpm"] = as5600Rpm;
  else doc["as5600_rpm"] = nullptr;
  doc["as5600_magnet_ok"] = as5600FieldValid;
  doc["ir_rpm"] = irRpm;
  doc["ir_total_pulses"] = irPulsesCopy;

  char jsonBuffer[512];
  size_t jsonLength = serializeJson(doc, jsonBuffer, sizeof(jsonBuffer));
  if (jsonLength == 0) return;

  bool published = mqttClient.publish(
      MQTT_TOPIC, reinterpret_cast<const uint8_t *>(jsonBuffer),
      jsonLength, false);

  if (!published) {
    mqttClient.disconnect();
    mqttDisconnectCount++;
    nextMqttRetryMs = millis() + 250;
    mqttRetryDelayMs = MQTT_RETRY_MIN_MS;
  }
}

// ============================================================
// STARTUP CHECK
// ============================================================
void printCheck(const char *label, bool ok, const char *okText, const char *failText) {
  Serial.printf("%s %-18s %s\n", ok ? "[OK]" : "[!!]", label, ok ? okText : failText);
}

void initialHardwareCheck() {
  Serial.println();
  Serial.println("====================================================");
  Serial.println("         KART TELEMETRY - STARTUP CHECK");
  Serial.println("====================================================");

  float t0 = readNtcTemperatureC();
  printCheck("NTC GPIO34", !isnan(t0), "presente",
             "non valido / controlla cablaggio");
  if (!isnan(t0)) Serial.printf("     Temperatura iniziale: %.1f C\n", t0);

  as5600Present = detectAS5600();
  printCheck("AS5600 I2C", as5600Present, "trovato @ 0x36", "non trovato");
  if (as5600Present) {
    updateAS5600();
    bool fieldValid = as5600MagnetDetected &&
                      !as5600MagnetTooWeak && !as5600MagnetTooStrong;
    printCheck("AS5600 magnete", fieldValid, "campo OK",
               "centra / avvicina magnete");
  }

  Serial.printf("[--] IR RPM GPIO27      OUT=%d\n", digitalRead(IR_RPM_PIN));
  Serial.println("[--] Garmin GLO2       verifica in background.");
  Serial.println("[--] WiFi hotspot      verifica in background.");
  Serial.println("====================================================");
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  // *** FIX HEAP ***
  // Libera la memoria riservata al controller BLE: usiamo solo BT
  // Classic (SPP). Recupera ~30 KB di heap, necessari per far
  // convivere WiFi + BT + MQTT + task GPS senza panic di allocazione.
  esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
  Serial.printf("[MEM] Heap dopo release BLE: %u bytes\n",
                (unsigned)ESP.getFreeHeap());

  // GPIO
  pinMode(NTC_PIN, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(NTC_PIN, ADC_11db);

  pinMode(IR_RPM_PIN, INPUT);
  irLastDigitalState = digitalRead(IR_RPM_PIN);
  attachInterrupt(digitalPinToInterrupt(IR_RPM_PIN), onIrPulse, FALLING);

  Wire.begin(AS5600_SDA_PIN, AS5600_SCL_PIN, AS5600_I2C_CLOCK_HZ);

  initialHardwareCheck();

  // WiFi
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);   // gestito dalla state machine
  WiFi.setSleep(false);
  WiFi.onEvent(onWiFiEvent);

  Serial.printf("[WIFI] Avvio connessione: %s...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiAttemptActive = true;
  wifiAttemptStartMs = millis();
  wifiRetryDelayMs = WIFI_RETRY_MIN_MS;
  nextWifiRetryMs = 0;

  // MQTT
  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setKeepAlive(MQTT_KEEPALIVE_SECONDS);
  mqttClient.setSocketTimeout(MQTT_SOCKET_TIMEOUT_SECONDS);
  if (!mqttClient.setBufferSize(MQTT_BUFFER_SIZE)) {
    Serial.println("[MQTT] Buffer alloc fail.");
    while (true) delay(1000);
  }
  mqttClient.setCallback(mqttCallback);

  // Bluetooth Classic
  if (!SerialBT.begin("ESP32-GLO2", true)) {
    Serial.println("[BT] BluetoothSerial.begin() fallita.");
    while (true) delay(1000);
  }
  Serial.println("[OK] Bluetooth Classic inizializzato.");

  Serial.printf("[MEM] Heap dopo WiFi+BT: %u bytes\n",
                (unsigned)ESP.getFreeHeap());

  // Task GPS su core 1
  BaseType_t ok = xTaskCreatePinnedToCore(
      gpsBluetoothTask, "GarminGpsTask",
      GPS_TASK_STACK_SIZE, nullptr,
      GPS_TASK_PRIORITY, &gpsTaskHandle,
      GPS_TASK_CORE);      // <-- CORE 1 (il fix)

  if (ok != pdPASS) {
    Serial.println("[BT] Task Garmin non creato.");
    while (true) delay(1000);
  }

  Serial.println("[OK] Task Garmin GPS avviato (core 1).");
  Serial.println("[SYS] Pronto.");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  updateTemperature();
  updateAS5600();
  updateIrRpm();

  maintainWiFi();
  maintainMqtt();

  if (mqttClient.connected()) {
    uint32_t now = millis();
    if (now - lastStatusCheckMs >= STATUS_CHECK_INTERVAL_MS) {
      lastStatusCheckMs = now;
      updateAndPublishComponentStatuses();
    }
  }

  if (millis() - lastPublishMs >= MQTT_PUBLISH_INTERVAL_MS) {
    publishTelemetryPayload();
    lastPublishMs = millis();
  }

  delay(2);
}