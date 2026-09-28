#!/usr/bin/env python3
"""
Kart Telemetry - Production-ready v2.1 (con logging)
Raspberry Pi Zero 2W
"""

import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
import uuid

import smbus2
import paho.mqtt.client as mqtt

import sys
from pathlib import Path

# Aggiunge src/logging/ al sys.path per importare i moduli di logging
_SRC_LOGGING = Path(__file__).resolve().parent / "src" / "logging"
if str(_SRC_LOGGING) not in sys.path:
    sys.path.insert(0, str(_SRC_LOGGING))

from logging_setup import setup_logging
from session_logger import SessionLogger

log = logging.getLogger("main")

try:
    import sdnotify
    _sd = sdnotify.SystemdNotifier()
except ImportError:
    _sd = None

def notify_ready():
    if _sd:
        try: _sd.notify("READY=1")
        except Exception: pass

def notify_watchdog():
    if _sd:
        try: _sd.notify("WATCHDOG=1")
        except Exception: pass

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    log.warning("RPi.GPIO non disponibile: IR RPM disabilitato.")

ADC_AVAILABLE = False
_adc_device = None
_adc_kind = None

try:
    from gpiozero import MCP3008
    try:
        _adc_device = MCP3008(channel=0)
        ADC_AVAILABLE = True
        _adc_kind = "MCP3008"
        log.info("ADC MCP3008 rilevato (canale 0)")
    except Exception as e:
        log.warning(f"MCP3008 non inizializzabile: {e}")
except ImportError:
    pass

if not ADC_AVAILABLE:
    try:
        import board, busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        _i2c = busio.I2C(board.SCL, board.SDA)
        _ads = ADS.ADS1115(_i2c)
        _adc_device = AnalogIn(_ads, ADS.P0)
        ADC_AVAILABLE = True
        _adc_kind = "ADS1115"
        log.info("ADC ADS1115 rilevato (canale A0)")
    except Exception:
        pass

if not ADC_AVAILABLE:
    log.warning("Nessun ADC rilevato: NTC disabilitata")

# ============================================================
# CONFIG
# ============================================================
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883
MQTT_TOPIC  = "sensors2mqtt-glo2/esp32/location"

STATUS_TOPIC_HOTSPOT = "sensors2mqtt-glo2/esp32/status/hotspot"
STATUS_TOPIC_MQTT    = "sensors2mqtt-glo2/esp32/status/mqtt"
STATUS_TOPIC_NTC     = "sensors2mqtt-glo2/esp32/status/ntc"
STATUS_TOPIC_AS5600  = "sensors2mqtt-glo2/esp32/status/as5600"
STATUS_TOPIC_IR_RPM  = "sensors2mqtt-glo2/esp32/status/ir_rpm"
STATUS_TOPIC_GPS     = "sensors2mqtt-glo2/esp32/status/gps"
ALIVE_TOPIC          = "sensors2mqtt-glo2/esp32/status/alive"

MQTT_KEEPALIVE_S        = 20
MQTT_PUBLISH_INTERVAL_S = 0.200
STATUS_CHECK_INTERVAL_S = 2.000
MQTT_RECONNECT_MIN_S    = 1
MQTT_RECONNECT_MAX_S    = 3
MQTT_MAX_QUEUED         = 5000
MQTT_MAX_INFLIGHT       = 20

_MAC_HEX = f"{uuid.getnode():012x}"
MQTT_CLIENT_ID = f"RPi-KART-{_MAC_HEX}"

NTC_VCC = 3.30
NTC_R_FIXED = 3300.0
NTC_R_NOMINAL = 10000.0
NTC_T_NOMINAL_C = 25.0
NTC_BETA = 3950.0
NTC_SAMPLES = 8
NTC_READ_INTERVAL_S = 0.200
NTC_FILTER_ALPHA = 0.65

AS5600_ADDRESS = 0x36
AS5600_REG_STATUS = 0x0B
AS5600_REG_RAW_ANGLE = 0x0C
AS5600_I2C_BUS = 1
AS5600_READ_INTERVAL_S = 0.010

IR_RPM_PIN = 27
IR_PULSES_PER_REVOLUTION = 1
IR_MIN_VALID_PERIOD_US = 1500
IR_STOP_TIMEOUT_S = 1.0
IR_RPM_UPDATE_INTERVAL_S = 0.100

GLO2_MAC = "14:13:0B:C0:B2:1E"
RFCOMM_CHANNEL = 1
GPS_TASK_START_DELAY_S = 3.0
GPS_RECONNECT_MIN_S = 1.0
GPS_RECONNECT_MAX_S = 3.0
GPS_NMEA_TIMEOUT_S = 5.0
MAX_NMEA_LINE_LENGTH = 160
GPS_RECV_TIMEOUT_S = 0.5

WIFI_CHECK_INTERVAL_S = 10.0
WIFI_LOG_INTERVAL_S   = 60.0

APP_VERSION = "2.1.0"

# ============================================================
# STATO
# ============================================================
gps_data = {
    "rmc_status": None, "latitude": 0.0, "longitude": 0.0,
    "speed_knots": 0.0, "speed_kmph": 0.0, "track_true_deg": 0.0,
    "fix_quality": 0, "satellites_used": 0, "fix_type": 0,
    "hdop": 0.0, "valid_fix": False,
}
gps_lock = threading.Lock()
last_nmea_time = 0.0
bt_connected = False

temperature_c = None
temperature_valid = False

as5600_present = False
as5600_magnet_detected = False
as5600_magnet_weak = False
as5600_magnet_strong = False
as5600_raw_angle = 0
as5600_prev_raw_angle = 0
as5600_angle_deg = None
as5600_rpm = 0.0
as5600_prev_read_time = 0.0
as5600_first_reading = True

ir_lock = threading.Lock()
ir_total_pulses = 0
ir_last_edge_time = 0.0
ir_latest_period_us = 0
ir_new_period_available = False
ir_rpm = 0.0
ir_state_changed = False
ir_last_digital_state = None

mqtt_client = None
mqtt_connected = False
mqtt_disconnect_count = 0

published = {"hotspot": None, "mqtt": None, "ntc": None,
             "as5600": None, "ir_rpm": None, "gps": None}
force_publish_all_statuses = True

i2c_bus = None
_shutdown = threading.Event()
session_logger = None

_last_wifi_snapshot = None
_last_wifi_log_time = 0.0
_last_bt_state = None
_last_mqtt_state = None
_last_ntc_state = None
_last_as5600_state = None
_last_gps_state = None


# ============================================================
# UTILITY
# ============================================================
def safe_float(text, fallback=0.0):
    try: return float(text)
    except (TypeError, ValueError): return fallback

def knots_to_kmph(k): return k * 1.852

def nmea_coord_to_decimal(v):
    d = int(v / 100.0)
    m = v - (d * 100.0)
    return d + (m / 60.0)

def update_valid_fix():
    gps_data["valid_fix"] = (
        gps_data["rmc_status"] == 'A'
        or gps_data["fix_quality"] > 0
        or gps_data["fix_type"] > 1
    )

def is_network_connected():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        s.close()
        return True
    except Exception:
        return False

def get_wifi_info():
    """Legge SSID/IP/MAC da /sys e nmcli. Ritorna dict o {}."""
    info = {}
    try:
        import subprocess
        r = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            if line.startswith("yes:"):
                info["ssid"] = line.split(":", 1)[1]
                break
        r = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "dev", "show", "wlan0"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            if line.startswith("IP4.ADDRESS"):
                info["ip"] = line.split(":", 1)[1].split("/")[0]
                break
        mac_path = "/sys/class/net/wlan0/address"
        if os.path.exists(mac_path):
            with open(mac_path) as f:
                info["mac"] = f.read().strip()
    except Exception:
        pass
    return info


# ============================================================
# NTC
# ============================================================
def read_ntc_voltage():
    if not ADC_AVAILABLE or _adc_device is None:
        return None
    try:
        return float(_adc_device.voltage)
    except Exception:
        return None

def read_ntc_temperature():
    voltages = []
    for _ in range(NTC_SAMPLES):
        v = read_ntc_voltage()
        if v is not None:
            voltages.append(v)
    if not voltages:
        return None
    voltage = sum(voltages) / len(voltages)
    if voltage < 0.03 or voltage > (NTC_VCC - 0.03):
        return None
    r_ntc = NTC_R_FIXED * ((NTC_VCC / voltage) - 1.0)
    if r_ntc <= 0.0 or math.isnan(r_ntc):
        return None
    inv_k = (1.0 / (NTC_T_NOMINAL_C + 273.15)) + (math.log(r_ntc / NTC_R_NOMINAL) / NTC_BETA)
    if inv_k == 0.0:
        return None
    return (1.0 / inv_k) - 273.15

def update_temperature():
    global temperature_c, temperature_valid, _last_ntc_state
    raw = read_ntc_temperature()
    if raw is None:
        if _last_ntc_state is not False:
            log.info("[ntc] reading invalid (open circuit or disconnected)")
            if session_logger:
                session_logger.log_event("sensor_lost", sensor="ntc")
                session_logger.update_sensors_now(ntc=False)
            _last_ntc_state = False
        temperature_valid = False
        temperature_c = None
        return
    if not temperature_valid or temperature_c is None:
        temperature_c = raw
    else:
        temperature_c = NTC_FILTER_ALPHA * raw + (1.0 - NTC_FILTER_ALPHA) * temperature_c
    temperature_valid = True
    if _last_ntc_state is not True:
        log.info(f"[ntc] reading valid ({temperature_c:.1f} C)")
        if session_logger:
            session_logger.log_event("sensor_appeared", sensor="ntc")
            session_logger.update_sensors_now(ntc=True)
        _last_ntc_state = True


# ============================================================
# AS5600
# ============================================================
def as5600_read_bytes(reg, length):
    if i2c_bus is None:
        return None
    try:
        return i2c_bus.read_i2c_block_data(AS5600_ADDRESS, reg, length)
    except Exception:
        return None

def detect_as5600():
    if i2c_bus is None:
        return False
    try:
        i2c_bus.write_byte(AS5600_ADDRESS, 0)
        return True
    except Exception:
        return False

def update_as5600():
    global as5600_present, as5600_magnet_detected, as5600_magnet_weak, as5600_magnet_strong
    global as5600_raw_angle, as5600_prev_raw_angle, as5600_angle_deg, as5600_rpm
    global as5600_prev_read_time, as5600_first_reading, _last_as5600_state

    status_buf = as5600_read_bytes(AS5600_REG_STATUS, 1)
    if status_buf is None or len(status_buf) < 1:
        if _last_as5600_state is not False:
            log.warning("[as5600] not detected at 0x36")
            if session_logger:
                session_logger.log_event("sensor_lost", sensor="as5600")
                session_logger.update_sensors_now(as5600=False)
            _last_as5600_state = False
        as5600_present = False
        as5600_magnet_detected = as5600_magnet_weak = as5600_magnet_strong = False
        as5600_angle_deg = None
        as5600_rpm = 0.0
        as5600_first_reading = True
        return

    as5600_present = True
    status = status_buf[0]
    as5600_magnet_detected = (status & 0x20) != 0
    as5600_magnet_weak     = (status & 0x10) != 0
    as5600_magnet_strong   = (status & 0x08) != 0

    if _last_as5600_state is not True:
        log.info(f"[as5600] detected at 0x36 (magnet_detected={as5600_magnet_detected})")
        if session_logger:
            session_logger.log_event("sensor_appeared", sensor="as5600", address="0x36")
            session_logger.update_sensors_now(as5600=True)
        _last_as5600_state = True

    angle_buf = as5600_read_bytes(AS5600_REG_RAW_ANGLE, 2)
    if angle_buf is None or len(angle_buf) < 2:
        as5600_present = False
        as5600_angle_deg = None
        as5600_rpm = 0.0
        as5600_first_reading = True
        return

    as5600_raw_angle = ((angle_buf[0] << 8) | angle_buf[1]) & 0x0FFF
    as5600_angle_deg = as5600_raw_angle * 360.0 / 4096.0

    if not (as5600_magnet_detected and not as5600_magnet_weak and not as5600_magnet_strong):
        as5600_rpm = 0.0
        as5600_first_reading = True
        return

    now = time.monotonic()
    if as5600_first_reading:
        as5600_prev_raw_angle = as5600_raw_angle
        as5600_prev_read_time = now
        as5600_first_reading = False
        return

    dt = now - as5600_prev_read_time
    if dt <= 0.0:
        return
    delta_raw = as5600_raw_angle - as5600_prev_raw_angle
    if delta_raw > 2048:  delta_raw -= 4096
    if delta_raw < -2048: delta_raw += 4096
    inst_rpm = (delta_raw * 60.0) / (4096.0 * dt)
    as5600_rpm = 0.35 * inst_rpm + 0.65 * as5600_rpm
    as5600_prev_raw_angle = as5600_raw_angle
    as5600_prev_read_time = now


# ============================================================
# IR RPM
# ============================================================
def ir_pulse_callback(channel):
    global ir_total_pulses, ir_last_edge_time, ir_latest_period_us
    global ir_new_period_available, ir_state_changed, ir_last_digital_state

    now = time.monotonic()
    with ir_lock:
        ir_state_changed = True
        try: ir_last_digital_state = GPIO.input(IR_RPM_PIN)
        except Exception: pass
        if ir_last_edge_time == 0.0:
            ir_last_edge_time = now
            ir_total_pulses += 1
            return
        period_us = (now - ir_last_edge_time) * 1_000_000.0
        if period_us < IR_MIN_VALID_PERIOD_US:
            return
        ir_last_edge_time = now
        ir_latest_period_us = period_us
        ir_new_period_available = True
        ir_total_pulses += 1

def update_ir_rpm():
    global ir_rpm, ir_new_period_available, ir_latest_period_us
    with ir_lock:
        period_us = 0
        has_new = False
        if ir_new_period_available:
            period_us = ir_latest_period_us
            ir_new_period_available = False
            has_new = True
        last_edge = ir_last_edge_time
    if has_new and period_us > 0:
        ir_rpm = 60_000_000.0 / (period_us * IR_PULSES_PER_REVOLUTION)
    now = time.monotonic()
    if last_edge == 0.0 or (now - last_edge) > IR_STOP_TIMEOUT_S:
        ir_rpm = 0.0


# ============================================================
# NMEA PARSER
# ============================================================
def parse_rmc(line):
    p = line.split(",")
    if len(p) < 10: return
    if p[2]: gps_data["rmc_status"] = p[2][0]
    raw_lat = safe_float(p[3])
    if raw_lat != 0.0:
        gps_data["latitude"] = nmea_coord_to_decimal(raw_lat)
        if p[4] == 'S': gps_data["latitude"] = -gps_data["latitude"]
    raw_lon = safe_float(p[5])
    if raw_lon != 0.0:
        gps_data["longitude"] = nmea_coord_to_decimal(raw_lon)
        if p[6] == 'W': gps_data["longitude"] = -gps_data["longitude"]
    if p[7]:
        gps_data["speed_knots"] = safe_float(p[7])
        gps_data["speed_kmph"] = knots_to_kmph(gps_data["speed_knots"])
    if p[8]:
        gps_data["track_true_deg"] = safe_float(p[8])
    update_valid_fix()

def parse_gga(line):
    p = line.split(",")
    if len(p) < 10: return
    raw_lat = safe_float(p[2])
    if raw_lat != 0.0:
        gps_data["latitude"] = nmea_coord_to_decimal(raw_lat)
        if p[3] == 'S': gps_data["latitude"] = -gps_data["latitude"]
    raw_lon = safe_float(p[4])
    if raw_lon != 0.0:
        gps_data["longitude"] = nmea_coord_to_decimal(raw_lon)
        if p[5] == 'W': gps_data["longitude"] = -gps_data["longitude"]
    gps_data["fix_quality"] = int(safe_float(p[6], 0))
    gps_data["satellites_used"] = int(safe_float(p[7], 0))
    gps_data["hdop"] = safe_float(p[8])
    update_valid_fix()

def parse_gsa(line):
    p = line.split(",")
    if len(p) < 3: return
    if p[2]:
        gps_data["fix_type"] = int(safe_float(p[2], 0))
    update_valid_fix()

def parse_vtg(line):
    p = line.split(",")
    if len(p) < 8: return
    if p[1]: gps_data["track_true_deg"] = safe_float(p[1])
    if p[5]: gps_data["speed_knots"] = safe_float(p[5])
    if p[7]: gps_data["speed_kmph"] = safe_float(p[7])

def process_nmea_line(line):
    global last_nmea_time
    if not line.startswith("$"): return
    with gps_lock:
        if line.startswith(("$GPRMC", "$GNRMC")):   parse_rmc(line)
        elif line.startswith(("$GPGGA", "$GNGGA")): parse_gga(line)
        elif line.startswith(("$GPGSA", "$GNGSA")): parse_gsa(line)
        elif line.startswith(("$GPVTG", "$GNVTG")): parse_vtg(line)
    last_nmea_time = time.monotonic()

def clear_gps_state():
    global last_nmea_time
    with gps_lock:
        for k in ("latitude", "longitude", "speed_knots", "speed_kmph",
                  "track_true_deg", "hdop"):
            gps_data[k] = 0.0
        for k in ("fix_quality", "satellites_used", "fix_type"):
            gps_data[k] = 0
        gps_data["rmc_status"] = None
        gps_data["valid_fix"] = False
    last_nmea_time = 0.0


# ============================================================
# GPS THREAD
# ============================================================
def gps_thread_func():
    global bt_connected, last_nmea_time, _last_gps_state

    time.sleep(GPS_TASK_START_DELAY_S)

    nmea_buffer = ""
    sock = None
    backoff = GPS_RECONNECT_MIN_S
    first_nmea_logged = False
    connect_attempts = 0

    while not _shutdown.is_set():
        if sock is None:
            connect_attempts += 1
            log.debug(f"[gps] connecting to {GLO2_MAC} ch={RFCOMM_CHANNEL} (attempt {connect_attempts})")
            try:
                sock = socket.socket(
                    socket.AF_BLUETOOTH,
                    socket.SOCK_STREAM,
                    socket.BTPROTO_RFCOMM,
                )
                sock.settimeout(3.0)
                sock.connect((GLO2_MAC, RFCOMM_CHANNEL))
                sock.settimeout(GPS_RECV_TIMEOUT_S)
                bt_connected = True
                nmea_buffer = ""
                last_nmea_time = 0.0
                backoff = GPS_RECONNECT_MIN_S
                first_nmea_logged = False
                log.info(f"[gps] connected to {GLO2_MAC} (socket RFCOMM)")
                if session_logger:
                    session_logger.log_event("bt_connect", mac=GLO2_MAC,
                                             rfcomm_channel=RFCOMM_CHANNEL,
                                             attempt=connect_attempts)
                    if connect_attempts > 1:
                        session_logger.bump("bt_reconnects")
                    session_logger.update_sensors_now(gps=False)
                _last_gps_state = True
            except OSError as e:
                if sock:
                    try: sock.close()
                    except Exception: pass
                sock = None
                bt_connected = False
                clear_gps_state()
                log.warning(f"[gps] connect failed: {e}; retry in {backoff:.1f}s")
                if session_logger:
                    session_logger.log_event("bt_connect_failed",
                                             reason=str(e),
                                             backoff_s=round(backoff, 2))
                time.sleep(backoff)
                backoff = min(backoff * 1.5, GPS_RECONNECT_MAX_S)
                continue

        data = None
        try:
            data = sock.recv(512)
            if not data:
                raise OSError("peer closed connection")
            backoff = GPS_RECONNECT_MIN_S
        except socket.timeout:
            data = None
        except OSError as e:
            log.warning(f"[gps] socket error: {e}; reconnecting in {backoff:.1f}s")
            if session_logger:
                session_logger.log_event("bt_disconnect", reason=str(e),
                                         backoff_s=round(backoff, 2))
                session_logger.update_sensors_now(gps=False)
            _last_gps_state = False
            try: sock.close()
            except Exception: pass
            sock = None
            bt_connected = False
            clear_gps_state()
            first_nmea_logged = False
            time.sleep(backoff)
            backoff = min(backoff * 1.5, GPS_RECONNECT_MAX_S)
            continue

        if data:
            for b in data:
                c = chr(b)
                if c == '\n':
                    line = nmea_buffer.strip()
                    if line:
                        process_nmea_line(line)
                        if not first_nmea_logged and line.startswith("$"):
                            first_nmea_logged = True
                            log.info(f"[gps] first NMEA: {line[:40]}...")
                            if session_logger:
                                session_logger.log_event("first_nmea",
                                                         sentence=line[:80])
                                session_logger.update_sensors_now(gps=True)
                    nmea_buffer = ""
                elif c == '\r':
                    continue
                else:
                    if len(nmea_buffer) < MAX_NMEA_LINE_LENGTH:
                        nmea_buffer += c
                    else:
                        nmea_buffer = ""

        if bt_connected and last_nmea_time > 0.0:
            if (time.monotonic() - last_nmea_time) > GPS_NMEA_TIMEOUT_S:
                log.warning(f"[gps] no NMEA for {GPS_NMEA_TIMEOUT_S}s; force reconnect")
                if session_logger:
                    session_logger.log_event("nmea_timeout",
                                             seconds_without_nmea=round(
                                                 time.monotonic() - last_nmea_time, 2),
                                             action="force_reconnect")
                    session_logger.bump("nmea_timeouts")
                try: sock.close()
                except Exception: pass
                sock = None
                bt_connected = False
                clear_gps_state()
                first_nmea_logged = False
                time.sleep(0.5)
                continue

        if not data:
            time.sleep(0.02)

    if sock:
        try: sock.close()
        except Exception: pass


# ============================================================
# MQTT
# ============================================================
def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected, force_publish_all_statuses, _last_mqtt_state
    rc_val = reason_code if isinstance(reason_code, int) else getattr(reason_code, "value", 1)
    if rc_val == 0:
        mqtt_connected = True
        force_publish_all_statuses = True
        for k in published:
            published[k] = None
        try:
            client.publish(ALIVE_TOPIC, json.dumps({"alive": True}), qos=1, retain=True)
        except Exception:
            pass
        session_present = False
        try:
            session_present = bool(getattr(flags, "session_present", False))
        except Exception:
            pass
        log.info(f"[mqtt] connected (session_present={session_present})")
        if session_logger:
            session_logger.log_event("mqtt_connect",
                                     broker=MQTT_BROKER, port=MQTT_PORT,
                                     client_id=MQTT_CLIENT_ID,
                                     session_present=session_present)
            if _last_mqtt_state is False:
                session_logger.bump("mqtt_reconnects")
        _last_mqtt_state = True
    else:
        mqtt_connected = False
        log.error(f"[mqtt] connect failed rc={rc_val}")
        _last_mqtt_state = False

def on_mqtt_disconnect(client, userdata, rc, properties=None):
    global mqtt_connected, mqtt_disconnect_count, _last_mqtt_state
    mqtt_connected = False
    mqtt_disconnect_count += 1
    rc_val = rc if isinstance(rc, int) else getattr(rc, "value", rc)
    meaning = {
        1: "unacceptable_protocol", 2: "identifier_rejected",
        3: "broker_unavailable", 4: "bad_credentials",
        5: "not_authorized", 7: "connection_lost",
    }.get(rc_val, "unknown")
    log.warning(f"[mqtt] disconnected rc={rc_val} ({meaning}); paho will retry")
    if session_logger:
        session_logger.log_event("mqtt_disconnect", rc=rc_val,
                                 meaning=meaning,
                                 total_disconnects=mqtt_disconnect_count)
    _last_mqtt_state = False

def build_mqtt_client():
    try:
        from paho.mqtt.client import CallbackAPIVersion
        c = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv311,
            clean_session=False,
        )
    except (ImportError, TypeError):
        c = mqtt.Client(
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv311,
            clean_session=False,
        )
    c.reconnect_delay_set(min_delay=MQTT_RECONNECT_MIN_S, max_delay=MQTT_RECONNECT_MAX_S)
    c.max_queued_messages_set(MQTT_MAX_QUEUED)
    c.max_inflight_messages_set(MQTT_MAX_INFLIGHT)
    c.will_set(ALIVE_TOPIC, json.dumps({"alive": False}), qos=1, retain=True)
    c.on_connect = on_mqtt_connect
    c.on_disconnect = on_mqtt_disconnect
    return c

def publish_status(topic, component, present):
    if not mqtt_connected or mqtt_client is None:
        return False
    payload = json.dumps({
        "sensor": component,
        "present": present,
        "timestamp_ms": int(time.monotonic() * 1000),
    })
    try:
        mqtt_client.publish(topic, payload, qos=1, retain=True)
        return True
    except Exception:
        return False

def publish_status_if_changed(topic, component, value):
    global force_publish_all_statuses
    last = published.get(component)
    if force_publish_all_statuses or last is None or last != value:
        if publish_status(topic, component, value):
            published[component] = value

def update_and_publish_component_statuses():
    global force_publish_all_statuses
    hotspot = is_network_connected()
    mqtt_ok = mqtt_connected
    ntc_ok = temperature_valid and temperature_c is not None
    as5600_ok = as5600_present
    if GPIO_AVAILABLE:
        try:
            ir_ok = ir_state_changed or (GPIO.input(IR_RPM_PIN) == GPIO.LOW)
        except Exception:
            ir_ok = ir_state_changed
    else:
        ir_ok = ir_state_changed
    gps_ok = (bt_connected and last_nmea_time > 0.0
              and (time.monotonic() - last_nmea_time) <= GPS_NMEA_TIMEOUT_S)
    publish_status_if_changed(STATUS_TOPIC_HOTSPOT, "hotspot", hotspot)
    publish_status_if_changed(STATUS_TOPIC_MQTT,    "mqtt",    mqtt_ok)
    publish_status_if_changed(STATUS_TOPIC_NTC,     "ntc",     ntc_ok)
    publish_status_if_changed(STATUS_TOPIC_AS5600,  "as5600",  as5600_ok)
    publish_status_if_changed(STATUS_TOPIC_IR_RPM,  "ir_rpm",  ir_ok)
    publish_status_if_changed(STATUS_TOPIC_GPS,     "gps",     gps_ok)
    force_publish_all_statuses = False


# ============================================================
# TELEMETRIA
# ============================================================
def publish_telemetry():
    if mqtt_client is None:
        return
    with gps_lock:
        gps_copy = dict(gps_data)
    with ir_lock:
        ir_pulses_copy = ir_total_pulses

    as5600_field_valid = (as5600_present and as5600_magnet_detected
                          and not as5600_magnet_weak and not as5600_magnet_strong)

    payload = {
        "satellites_used": gps_copy["satellites_used"],
        "latitude":        gps_copy["latitude"],
        "longitude":       gps_copy["longitude"],
        "speed_kmph":      gps_copy["speed_kmph"],
        "fix_valid":       gps_copy["valid_fix"],
        "fix_quality":     gps_copy["fix_quality"],
        "fix_type":        gps_copy["fix_type"],
        "hdop":            gps_copy["hdop"],
        "temperature_c":   temperature_c if (temperature_valid and temperature_c is not None) else None,
        "as5600_angle_deg": as5600_angle_deg if (as5600_present and as5600_angle_deg is not None) else None,
        "as5600_rpm":       as5600_rpm if as5600_field_valid else None,
        "as5600_magnet_ok": as5600_field_valid,
        "ir_rpm":           ir_rpm,
        "ir_total_pulses":  ir_pulses_copy,
    }

    try:
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=1, retain=False)
    except Exception as e:
        log.error(f"[mqtt] publish failed: {e}")

    if session_logger:
        session_logger.log_raw_sample(payload)


# ============================================================
# WIFI MONITOR
# ============================================================
def wifi_monitor_loop():
    global _last_wifi_snapshot, _last_wifi_log_time
    while not _shutdown.is_set():
        try:
            info = get_wifi_info()
            now = time.time()

            snapshot_key = (info.get("ssid"), info.get("ip"))
            prev_key = None
            if _last_wifi_snapshot:
                prev_key = (_last_wifi_snapshot.get("ssid"),
                            _last_wifi_snapshot.get("ip"))

            if snapshot_key != prev_key:
                if info.get("ssid"):
                    log.info(f"[wifi] connected ssid={info.get('ssid')} ip={info.get('ip')} mac={info.get('mac')}")
                    if session_logger:
                        session_logger.log_event("wifi_connect",
                                                 ssid=info.get("ssid"),
                                                 ip=info.get("ip"),
                                                 mac=info.get("mac"))
                        session_logger.set_wifi(info.get("ssid"), info.get("ip"),
                                                info.get("mac"))
                else:
                    log.warning("[wifi] no active connection")
                    if session_logger:
                        session_logger.log_event("wifi_disconnect")
                _last_wifi_snapshot = info
                _last_wifi_log_time = now
            elif now - _last_wifi_log_time > WIFI_LOG_INTERVAL_S and info.get("ssid"):
                log.debug(f"[wifi] still on {info.get('ssid')} ({info.get('ip')})")
                _last_wifi_log_time = now
        except Exception as e:
            log.debug(f"[wifi] monitor error: {e}")
        time.sleep(WIFI_CHECK_INTERVAL_S)


# ============================================================
# STARTUP CHECK
# ============================================================
def print_check(label, ok, ok_text, fail_text):
    tag = "OK" if ok else "!!"
    msg = f"{label}: {ok_text if ok else fail_text}"
    if ok:
        log.info(f"[check] {msg}")
    else:
        log.warning(f"[check] {msg}")

def initial_hardware_check():
    global as5600_present

    log.info("=" * 52)
    log.info("KART TELEMETRY - STARTUP CHECK")
    log.info("=" * 52)

    t0 = read_ntc_temperature() if ADC_AVAILABLE else None
    print_check("NTC (ADC)", t0 is not None, "OK",
                "not valid" if ADC_AVAILABLE else "no ADC")
    if t0 is not None:
        log.info(f"[check] initial temperature: {t0:.1f} C")

    as5600_present = detect_as5600()
    print_check("AS5600 I2C", as5600_present, "found @ 0x36", "not found")
    if as5600_present:
        update_as5600()
        fv = (as5600_magnet_detected and not as5600_magnet_weak and not as5600_magnet_strong)
        print_check("AS5600 magnet", fv, "OK", "weak/strong/absent")

    if GPIO_AVAILABLE:
        try:
            log.info(f"[check] IR RPM GPIO{IR_RPM_PIN} OUT={GPIO.input(IR_RPM_PIN)}")
        except Exception:
            log.warning("[check] IR RPM state not readable")

    log.info("[check] Garmin GLO2 verification in background")
    log.info("[check] WiFi verification in background")
    log.info("=" * 52)

    if session_logger:
        session_logger.set_sensors_at_boot(
            ntc=(t0 is not None),
            as5600=as5600_present,
            ir_rpm=GPIO_AVAILABLE,
            gps=False,
        )
        session_logger.log_event("hardware_check",
                                 ntc=(t0 is not None),
                                 as5600=as5600_present,
                                 ir_rpm=GPIO_AVAILABLE,
                                 gps=False)


# ============================================================
# MAIN
# ============================================================
def _handle_signal(signum, frame):
    log.info(f"[main] signal {signum} received, shutting down")
    _shutdown.set()

def main():
    global i2c_bus, mqtt_client, mqtt_connected, session_logger, log

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    session_logger = SessionLogger(app_version=APP_VERSION)
    log = setup_logging(session_logger.session_dir)

    log.info(f"[SESSION] started id={session_logger.session_id}")
    log.info(f"[SESSION] directory={session_logger.session_dir}")
    log.info("[SESSION] files=glo2-telemetry.log, boot.json, raw.jsonl, events.jsonl")
    log.info(f"=== Kart Telemetry v{APP_VERSION} starting ===")
    log.info(f"[main] hostname={socket.gethostname()} pid={os.getpid()}")
    log.info(f"[main] mqtt_client_id={MQTT_CLIENT_ID} broker={MQTT_BROKER}:{MQTT_PORT}")

    session_logger.set_mqtt(MQTT_BROKER, MQTT_PORT, MQTT_CLIENT_ID, MQTT_TOPIC)
    session_logger.set_gps(GLO2_MAC, RFCOMM_CHANNEL)
    session_logger.log_event("service_start",
                             pid=os.getpid(),
                             app_version=APP_VERSION,
                             hostname=socket.gethostname())

    try:
        i2c_bus = smbus2.SMBus(AS5600_I2C_BUS)
        log.info(f"[i2c] bus {AS5600_I2C_BUS} opened")
    except Exception as e:
        i2c_bus = None
        log.error(f"[i2c] open bus {AS5600_I2C_BUS} failed: {e}")

    if GPIO_AVAILABLE:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(IR_RPM_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        try:
            ir_last_digital_state = GPIO.input(IR_RPM_PIN)
        except Exception:
            pass
        try:
            GPIO.add_event_detect(IR_RPM_PIN, GPIO.FALLING,
                                  callback=ir_pulse_callback, bouncetime=1)
            log.info(f"[gpio] IR edge detection armed (BCM{IR_RPM_PIN}, FALLING)")
        except RuntimeError as e:
            log.error(f"[gpio] add_event_detect failed: {e}; IR RPM disabled")

    initial_hardware_check()

    # WiFi info iniziale
    winfo = get_wifi_info()
    if winfo.get("ssid"):
        log.info(f"[wifi] active ssid={winfo.get('ssid')} ip={winfo.get('ip')}")
        session_logger.set_wifi(winfo.get("ssid"), winfo.get("ip"), winfo.get("mac"))
        session_logger.log_event("wifi_connect",
                                 ssid=winfo.get("ssid"),
                                 ip=winfo.get("ip"),
                                 mac=winfo.get("mac"))

    # MQTT
    mqtt_client = build_mqtt_client()
    log.info("[mqtt] connecting...")
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE_S)
        mqtt_client.loop_start()
    except Exception as e:
        log.error(f"[mqtt] initial connect failed: {e}; paho will retry")

    # WiFi monitor thread
    threading.Thread(target=wifi_monitor_loop, daemon=True, name="WifiMon").start()

    # GPS thread
    threading.Thread(target=gps_thread_func, daemon=True, name="GarminGps").start()
    log.info("[gps] task started")

    log.info("[main] READY (watchdog armed, 30s)")
    notify_ready()

    last_ntc = 0.0
    last_as5600 = 0.0
    last_ir = 0.0
    last_publish = 0.0
    last_status_check = 0.0
    last_boot_update = 0.0

    try:
        while not _shutdown.is_set():
            notify_watchdog()
            now = time.monotonic()

            if (now - last_ntc) >= NTC_READ_INTERVAL_S:
                last_ntc = now
                update_temperature()

            if (now - last_as5600) >= AS5600_READ_INTERVAL_S:
                last_as5600 = now
                update_as5600()

            if (now - last_ir) >= IR_RPM_UPDATE_INTERVAL_S:
                last_ir = now
                update_ir_rpm()

            if mqtt_connected and (now - last_status_check) >= STATUS_CHECK_INTERVAL_S:
                last_status_check = now
                update_and_publish_component_statuses()

            if (now - last_publish) >= MQTT_PUBLISH_INTERVAL_S:
                last_publish = now
                publish_telemetry()

            if session_logger and (now - last_boot_update) >= 30.0:
                last_boot_update = now
                session_logger.update_boot_json()

            time.sleep(0.002)

    except KeyboardInterrupt:
        log.info("[main] interrupted by user")
    finally:
        _shutdown.set()
        if GPIO_AVAILABLE:
            try: GPIO.cleanup()
            except Exception: pass
        if mqtt_client:
            try:
                mqtt_client.publish(ALIVE_TOPIC, json.dumps({"alive": False}),
                                    qos=1, retain=True)
                time.sleep(0.2)
            except Exception: pass
            try: mqtt_client.loop_stop()
            except Exception: pass
        if i2c_bus:
            try: i2c_bus.close()
            except Exception: pass
        if session_logger:
            session_logger.close(reason="shutdown")
        log.info(f"=== shutdown complete ===")


if __name__ == "__main__":
    main()