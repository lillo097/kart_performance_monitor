#!/usr/bin/env python3

import json
import math
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TRACK_FILE = BASE_DIR / "config" / "tracks" / "milano-edolo.yaml"
APP_CONFIG_FILE = BASE_DIR / "config" / "app.yaml"

MAIN_APP_API = "http://127.0.0.1:8080/api/live"

load_dotenv(BASE_DIR / ".env")

HOST = "0.0.0.0"
PORT = 8081

app = Flask(__name__)

DEMO_MAPBOX_TOKEN = os.environ.get("MAPBOX_DEMO_TOKEN", "")


# -----------------------------------------------------------------------------
# Configurazione
# -----------------------------------------------------------------------------

def load_yaml(path):
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def load_track_yaml():
    return load_yaml(TRACK_FILE)


def load_mqtt_config():
    config = load_yaml(APP_CONFIG_FILE)
    mqtt_config = config.get("mqtt", {})

    broker = mqtt_config.get("broker", "127.0.0.1")
    port = int(mqtt_config.get("port", 1883))
    websocket_port = int(mqtt_config.get("websocket_port", 9001))
    websocket_path = str(mqtt_config.get("websocket_path", "") or "")
    keepalive_s = int(mqtt_config.get("keepalive_s", 60))

    topic = mqtt_config.get("topic")

    if not topic:
        raise RuntimeError(
            "Il topic MQTT non è presente in config/app.yaml "
            "alla chiave mqtt.topic."
        )

    return {
        "broker": broker,
        "port": port,
        "websocket_port": websocket_port,
        "websocket_path": websocket_path,
        "keepalive_s": keepalive_s,
        "topic": topic,
    }


def load_mapbox_token():
    token = os.environ.get("MAPBOX_TOKEN", "").strip()
    if token:
        return token, False
    if DEMO_MAPBOX_TOKEN:
        return DEMO_MAPBOX_TOKEN, True
    raise RuntimeError(
        "MAPBOX_TOKEN non impostato. Crealo nel file .env "
        "oppure esportalo come variabile d'ambiente."
    )


MQTT_CONFIG = load_mqtt_config()
MAPBOX_TOKEN, USING_DEMO_TOKEN = load_mapbox_token()


# -----------------------------------------------------------------------------
# Geometria GPS
# -----------------------------------------------------------------------------

def yaml_point(value):
    if not isinstance(value, dict):
        return None

    latitude = value.get("lat")
    longitude = value.get("lon")

    if latitude is None or longitude is None:
        return None

    return [float(latitude), float(longitude)]


def midpoint(point_a, point_b):
    return [
        (point_a[0] + point_b[0]) / 2.0,
        (point_a[1] + point_b[1]) / 2.0,
    ]


def bearing_degrees(point_a, point_b):
    latitude_1 = math.radians(point_a[0])
    longitude_1 = math.radians(point_a[1])
    latitude_2 = math.radians(point_b[0])
    longitude_2 = math.radians(point_b[1])

    delta_longitude = longitude_2 - longitude_1

    x = math.sin(delta_longitude) * math.cos(latitude_2)
    y = (
            math.cos(latitude_1) * math.sin(latitude_2)
            - math.sin(latitude_1)
            * math.cos(latitude_2)
            * math.cos(delta_longitude)
    )

    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination_point(point, bearing_deg, distance_m):
    earth_radius_m = 6371000.0
    latitude_1 = math.radians(point[0])
    longitude_1 = math.radians(point[1])
    bearing = math.radians(bearing_deg)
    angular_distance = distance_m / earth_radius_m

    latitude_2 = math.asin(
        math.sin(latitude_1) * math.cos(angular_distance)
        + math.cos(latitude_1)
        * math.sin(angular_distance)
        * math.cos(bearing)
    )

    longitude_2 = longitude_1 + math.atan2(
        math.sin(bearing)
        * math.sin(angular_distance)
        * math.cos(latitude_1),
        math.cos(angular_distance)
        - math.sin(latitude_1) * math.sin(latitude_2),
    )

    return [
        math.degrees(latitude_2),
        math.degrees(longitude_2),
    ]


def extend_line(point_a, point_b, extension_each_side_m):
    extension = max(0.0, float(extension_each_side_m))

    if extension == 0.0:
        return [point_a, point_b]

    heading_a_to_b = bearing_degrees(point_a, point_b)
    heading_b_to_a = (heading_a_to_b + 180.0) % 360.0

    extended_a = destination_point(
        point_a,
        heading_b_to_a,
        extension,
    )

    extended_b = destination_point(
        point_b,
        heading_a_to_b,
        extension,
    )

    return [extended_a, extended_b]


def direction_arrow(before, after):
    heading = bearing_degrees(before, after)
    center = midpoint(before, after)

    tail = destination_point(
        center,
        (heading + 180.0) % 360.0,
        8.0,
    )

    tip = destination_point(
        center,
        heading,
        16.0,
    )

    return {
        "center": center,
        "tail": tail,
        "tip": tip,
        "heading_deg": heading,
    }


def line_data(line_config, default_extension_m=16.0):
    if not isinstance(line_config, dict):
        return None

    point_a = yaml_point(line_config.get("a"))
    point_b = yaml_point(line_config.get("b"))

    if not point_a or not point_b:
        return None

    extension = float(
        line_config.get(
            "line_extension_each_side_m",
            default_extension_m,
        )
    )

    return {
        "original_line": [point_a, point_b],
        "line": extend_line(point_a, point_b, extension),
        "center": midpoint(point_a, point_b),
        "line_extension_each_side_m": extension,
        "crossing_tolerance_m": float(
            line_config.get("crossing_tolerance_m", 12.0)
        ),
    }


def build_map_config():
    config = load_track_yaml()

    track = config.get("track", {})
    start_finish_config = config.get("start_finish", {})
    sectors_config = config.get("sectors", [])
    pit_box_config = config.get("pit_box", {})
    pit_lane_config = config.get("pit_lane", {})

    result = {
        "track": {
            "id": track.get("id", "unknown"),
            "name": track.get("name", "Pista non configurata"),
        },
        "start_finish": None,
        "sectors": [],
        "pit_box": None,
        "pit_lane": None,
    }

    start_finish = line_data(
        start_finish_config,
        default_extension_m=18.0,
    )

    if start_finish:
        start_finish["id"] = start_finish_config.get("id", "SF")
        start_finish["name"] = start_finish_config.get(
            "name",
            "Start / Finish",
        )
        start_finish["direction"] = start_finish_config.get(
            "direction",
            "forward",
        )
        start_finish["direction_arrow"] = None

        references = start_finish_config.get(
            "direction_reference",
            {},
        )

        before = yaml_point(references.get("before"))
        after = yaml_point(references.get("after"))

        if before and after:
            start_finish["direction_arrow"] = direction_arrow(
                before,
                after,
            )

        result["start_finish"] = start_finish

    if isinstance(sectors_config, list):
        for index, sector_config in enumerate(sectors_config, start=1):
            sector = line_data(
                sector_config,
                default_extension_m=16.0,
            )

            if not sector:
                continue

            sector["id"] = sector_config.get("id", f"S{index}")
            sector["name"] = sector_config.get(
                "name",
                f"Settore {index}",
            )
            sector["direction"] = sector_config.get(
                "direction",
                "forward",
            )

            result["sectors"].append(sector)

    pit_center = yaml_point(pit_box_config.get("center"))
    pit_polygon = pit_box_config.get("polygon")

    if pit_center:
        result["pit_box"] = {
            "type": "circle",
            "center": pit_center,
            "radius_m": float(
                pit_box_config.get("radius_m", 12.0)
            ),
            "exit_hysteresis_m": float(
                pit_box_config.get("exit_hysteresis_m", 4.0)
            ),
        }
    elif isinstance(pit_polygon, list) and len(pit_polygon) >= 3:
        points = [
            yaml_point(p)
            for p in pit_polygon
            if yaml_point(p) is not None
        ]
        if len(points) >= 3:
            result["pit_box"] = {
                "type": "polygon",
                "polygon": points,
                "exit_hysteresis_m": float(
                    pit_box_config.get("exit_hysteresis_m", 4.0)
                ),
            }

    pit_lane_start = yaml_point(pit_lane_config.get("start"))
    pit_lane_end = yaml_point(pit_lane_config.get("end"))
    pit_lane_path = pit_lane_config.get("path")

    if pit_lane_start and pit_lane_end:
        pit_lane_data = {
            "start": pit_lane_start,
            "end": pit_lane_end,
        }

        if isinstance(pit_lane_path, list):
            path_points = [
                yaml_point(p)
                for p in pit_lane_path
                if yaml_point(p) is not None
            ]
            if len(path_points) >= 2:
                pit_lane_data["path"] = path_points

        result["pit_lane"] = pit_lane_data

    return result


# -----------------------------------------------------------------------------
# GPS live via API dell'app principale
# -----------------------------------------------------------------------------

def read_live_gps():
    import urllib.request

    fallback = {
        "latitude": 0.0,
        "longitude": 0.0,
        "speed_kmph": 0.0,
        "satellites_used": 0,
        "fix_valid": False,
        "fix_type": 0,
        "hdop": None,
    }

    try:
        with urllib.request.urlopen(MAIN_APP_API, timeout=1.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("gps", fallback)
    except Exception as error:
        fallback["error"] = str(error)
        return fallback


def read_live_session_status():
    import urllib.request

    try:
        with urllib.request.urlopen(MAIN_APP_API, timeout=1.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("session", {}).get("status")
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Interfaccia
# -----------------------------------------------------------------------------

MAP_HTML = r"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Track Map Live</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">

  <script src="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.js"></script>
  <link href="https://api.mapbox.com/mapbox-gl-js/v3.0.0/mapbox-gl.css" rel="stylesheet">

  <script src="https://unpkg.com/mqtt@5.10.1/dist/mqtt.min.js"></script>

  <style>
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      padding: 0;
      background: #101114;
    }

    #map {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      width: 100%;
      height: 100%;
      background: #101114;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif;
    }

    .info-panel {
      position: fixed;
      z-index: 1000;
      top: 12px;
      left: 12px;
      width: min(315px, calc(100vw - 24px));
      padding: 12px;
      background: rgba(12, 13, 16, 0.94);
      color: #f5f5f5;
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 9px;
      box-shadow: 0 8px 25px rgba(0, 0, 0, 0.38);
    }

    .track-title {
      margin: 0 0 10px;
      font-size: 16px;
      font-weight: 850;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 7px;
    }

    .metric {
      padding: 7px;
      background: #18191d;
      border: 1px solid #32343a;
      border-radius: 6px;
    }

    .label {
      display: block;
      color: #a5a8ae;
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }

    .value {
      display: block;
      margin-top: 2px;
      color: #fff;
      font-size: 16px;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }

    .good { color: #2ed477; }
    .bad { color: #f16066; }
    .warn { color: #f5a524; }

    .legend {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-top: 10px;
      color: #d9dade;
      font-size: 11px;
    }

    .legend-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .legend-line {
      width: 20px;
      height: 4px;
      flex: 0 0 20px;
      border-radius: 3px;
    }

    .map-status {
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid #36383d;
      color: #b1b4ba;
      font-size: 10px;
      line-height: 1.35;
      white-space: pre-line;
    }

    .live-kart {
      width: 20px;
      height: 20px;
      border: 3px solid #fff;
      border-radius: 50%;
      background: #a855f7;
      box-shadow: 0 0 13px rgba(168, 85, 247, 0.95);
    }

    .direction-arrow {
      width: 34px;
      height: 34px;
      display: block;
      overflow: visible;
      filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.9));
      transform-origin: 50% 50%;
    }

    /* Marker "center dot" per SF / settori / box: dimensione fissa in pixel,
       serve solo come punto di riferimento per popup + tooltip. */
    .center-dot {
      border-radius: 50%;
      border: 2px solid #fff;
      box-shadow: 0 0 4px rgba(0, 0, 0, 0.8);
      cursor: pointer;
    }

    .command-row {
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid #36383d;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .cooldown-button,
    .session-button {
      width: 100%;
      padding: 8px 10px;
      color: #fff;
      border: 1px solid;
      border-radius: 7px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease, opacity .15s ease;
    }

    .cooldown-button {
      background: #1c2333;
      border-color: #3a4a6b;
      color: #9fd7ff;
    }
    .cooldown-button:hover { background: #253150; }
    .cooldown-button:active { background: #2f3f66; }

    .session-button {
      background: #162a1c;
      border-color: #2f7a3d;
      color: #9fffa9;
    }
    .session-button:hover { background: #1d3a24; }
    .session-button:active { background: #254a2e; }
    .session-button.stop {
      background: #2a1616;
      border-color: #7a2f2f;
      color: #ff9f9f;
    }
    .session-button.stop:hover { background: #3a1d1d; }

    .cooldown-button:disabled,
    .session-button:disabled {
      opacity: 0.5;
      cursor: default;
    }

    .command-status {
      margin-top: 6px;
      min-height: 12px;
      color: #b1b4ba;
      font-size: 10px;
      line-height: 1.35;
    }

    .command-status.ok { color: #2ed477; }
    .command-status.error { color: #f16066; }
    .command-status.warn { color: #f5a524; }

    .style-toggle {
      position: fixed;
      top: 12px;
      right: 12px;
      z-index: 1000;
      background: #1c1c1e;
      color: #fff;
      border: 1px solid #3a3a3e;
      border-radius: 7px;
      padding: 8px 12px;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      cursor: pointer;
      letter-spacing: 0.08em;
    }
    .style-toggle:hover {
      background: #2a2a2e;
    }
  </style>
</head>
<body>
  <div id="map"></div>

  <button id="styleToggle" class="style-toggle">Satellite</button>

  <aside class="info-panel">
    <h1 id="track-name" class="track-title">Caricamento…</h1>

    <div class="metric-grid">
      <div class="metric">
        <span class="label">GPS</span>
        <span id="gps-status" class="value bad">NO FIX</span>
      </div>

      <div class="metric">
        <span class="label">Velocità</span>
        <span id="speed" class="value">0 km/h</span>
      </div>

      <div class="metric">
        <span class="label">Satelliti</span>
        <span id="satellites" class="value">0</span>
      </div>

      <div class="metric">
        <span class="label">HDOP</span>
        <span id="hdop" class="value">—</span>
      </div>
    </div>

    <div class="legend">
      <div class="legend-row"><span class="legend-line" style="background:#ef4444"></span>Traguardo</div>
      <div class="legend-row"><span class="legend-line" style="background:#2563eb"></span>Settore 1</div>
      <div class="legend-row"><span class="legend-line" style="background:#16a34a"></span>Settore 2</div>
      <div class="legend-row"><span class="legend-line" style="background:#f59e0b"></span>Settore 3</div>
      <div class="legend-row"><span class="legend-line" style="background:#ff9800"></span>BOX</div>
      <div class="legend-row"><span class="legend-line" style="background:#a855f7"></span>GPS live</div>
      <div class="legend-row"><span class="legend-line" style="background:#ec4899"></span>Pit Lane</div>
    </div>

    <div class="command-row">
      <button id="cooldownButton" class="cooldown-button" type="button">
        Cooldown
      </button>
      <button id="sessionButton" class="session-button" type="button">
        Start Session
      </button>
      <div id="commandStatus" class="command-status"></div>
    </div>

    <div id="map-status" class="map-status">
      Lettura configurazione YAML…
    </div>
  </aside>

  <script>
    mapboxgl.accessToken = "{{ MAPBOX_TOKEN }}";
    const USING_DEMO_TOKEN = {{ 'true' if USING_DEMO_TOKEN else 'false' }};

    const MQTT_BROKER = "{{ MQTT_BROKER }}";
    const MQTT_WS_PORT = {{ MQTT_WS_PORT }};
    const MQTT_WS_PATH = "{{ MQTT_WS_PATH }}";
    const MQTT_TOPIC = "{{ MQTT_TOPIC }}";

    const mqttHost =
      (MQTT_BROKER === "127.0.0.1" || MQTT_BROKER === "localhost")
        ? window.location.hostname
        : MQTT_BROKER;

    const mqttPath = (MQTT_WS_PATH && MQTT_WS_PATH.trim() !== "")
      ? (MQTT_WS_PATH.startsWith("/") ? MQTT_WS_PATH : "/" + MQTT_WS_PATH)
      : "";

    const MQTT_WS_URL = "ws://" + mqttHost + ":" + MQTT_WS_PORT + mqttPath;

    const SATELLITE_STYLE = 'mapbox://styles/mapbox/satellite-streets-v12';
    const STREETS_STYLE = 'mapbox://styles/mapbox/streets-v12';

    const PIT_BOX_COLOR = '#ff9800';

    const mapStatusEl = document.getElementById("map-status");
    const commandStatus = document.getElementById("commandStatus");
    const cooldownButton = document.getElementById("cooldownButton");
    const sessionButton = document.getElementById("sessionButton");

    function setStatus(text) {
      mapStatusEl.textContent = text;
    }

    function setCommandStatus(text, type) {
      commandStatus.className = "command-status" + (type ? " " + type : "");
      commandStatus.textContent = text;
    }

    if (USING_DEMO_TOKEN) {
      setStatus(
        "ATTENZIONE: nessun MAPBOX_TOKEN impostato nell'ambiente.\n" +
        "Uso il token demo pubblico: limitato e spesso mostra tile nere/mancanti.\n" +
        "Imposta la variabile d'ambiente MAPBOX_TOKEN con un token valido da account.mapbox.com."
      );
    }

    if (!mapboxgl.supported()) {
      setStatus("Il browser non supporta WebGL/Mapbox GL JS. Aggiorna il browser o abilita WebGL.");
    }

    // =====================================================================
    // MQTT over WebSocket
    // =====================================================================

    let mqttClient = null;
    let mqttConnected = false;

    setCommandStatus("Connessione MQTT a " + MQTT_WS_URL + "…", "warn");

    try {
      mqttClient = mqtt.connect(MQTT_WS_URL, {
        clientId: "trackmap-web-" + Math.random().toString(16).slice(2, 10),
        clean: true,
        reconnectPeriod: 2000,
        connectTimeout: 8000,
        keepalive: 30,
      });

      mqttClient.on("connect", () => {
        mqttConnected = true;
        console.log("[MQTT] Connesso:", MQTT_WS_URL);
        setCommandStatus("MQTT connesso: " + MQTT_WS_URL, "ok");
      });

      mqttClient.on("reconnect", () => {
        console.log("[MQTT] Riconnessione…");
        setCommandStatus("MQTT riconnessione…", "warn");
      });

      mqttClient.on("close", () => {
        mqttConnected = false;
        setCommandStatus("MQTT disconnesso (" + MQTT_WS_URL + ")", "error");
      });

      mqttClient.on("error", (err) => {
        mqttConnected = false;
        console.error("[MQTT] Errore:", err);
        setCommandStatus(
          "Errore MQTT: " + (err && err.message ? err.message : err),
          "error"
        );
      });
    } catch (err) {
      console.error("[MQTT] Init error:", err);
      setCommandStatus("Init MQTT fallita: " + err.message, "error");
    }

    function publishMqtt(payload, onDone) {
      if (!mqttClient || !mqttConnected) {
        onDone(false, "MQTT non connesso.");
        return;
      }

      const message = JSON.stringify(payload);
      console.log("[MQTT] publish", MQTT_TOPIC, "→", message);

      mqttClient.publish(MQTT_TOPIC, message, { qos: 1, retain: false }, (err) => {
        if (err) {
          console.error("[MQTT] publish error:", err);
          onDone(false, err.message || String(err));
        } else {
          console.log("[MQTT] publish OK");
          onDone(true, null);
        }
      });
    }

    function sendCommand(type) {
      const button = type === 'cooldown' ? cooldownButton : sessionButton;
      button.disabled = true;

      if (type === 'cooldown') {
        setCommandStatus("Pubblicazione MQTT cooldown…", "warn");
        publishMqtt({ cooldown: true }, (ok, err) => {
          setCommandStatus(
            ok
              ? 'Payload {"cooldown":true} pubblicato.'
              : "Errore MQTT: " + err,
            ok ? "ok" : "error"
          );
          button.disabled = false;
        });
      } else {
        setCommandStatus("Pubblicazione MQTT session_toggle…", "warn");
        publishMqtt({ session_toggle: true }, (ok, err) => {
          setCommandStatus(
            ok
              ? 'Payload {"session_toggle":true} pubblicato.'
              : "Errore MQTT: " + err,
            ok ? "ok" : "error"
          );
          button.disabled = false;
        });
      }
    }

    cooldownButton.addEventListener("click", () => sendCommand('cooldown'));
    sessionButton.addEventListener("click", () => sendCommand('session-toggle'));

    // =====================================================================
    // Mappa Mapbox
    // =====================================================================

    const map = new mapboxgl.Map({
      container: 'map',
      style: SATELLITE_STYLE,
      center: [13.1540, 41.2743],
      zoom: 12,
      attributionControl: true
    });

    let styleLoadFailed = false;
    let currentStyle = 'satellite';
    const styleToggle = document.getElementById("styleToggle");

    map.on('error', (event) => {
      const message = (event && event.error && event.error.message) || "Errore sconosciuto";
      console.error("[Mapbox error]", message, event);

      if (!styleLoadFailed && /style|tile|source|access token|401|403/i.test(message)) {
        styleLoadFailed = true;
        setStatus("Errore caricamento stile satellite (" + message + "). Passo a stile Streets.");
        currentStyle = 'streets';
        styleToggle.textContent = 'Satellite';
        map.setStyle(STREETS_STYLE);
      } else {
        setStatus("Errore Mapbox: " + message);
      }
    });

    const colors = ["#2563eb", "#16a34a", "#f59e0b"];
    let configFingerprint = null;
    let configurationLayers = [];
    let initialZoomDone = false;
    let gpsMarker = null;
    let gpsTrail = [];
    let gpsTrailLine = null;
    let lastConfig = null;
    let lastGps = null;
    let updateTimer = null;

    function addLayer(layer) {
      configurationLayers.push(layer);
      return layer;
    }

    function clearConfiguration() {
      configurationLayers.forEach(layer => {
        if (layer && layer.remove) layer.remove();
      });
      configurationLayers = [];
    }

    // ------------------------------------------------------------------
    // Line width espressa in modo che sia visivamente coerente su tutti
    // gli zoom: sottile a zoom basso, più marcata a zoom alto (simula
    // lo "spessore reale" di una riga tracciata a terra).
    // ------------------------------------------------------------------
    function zoomLineWidth(baseAtZoom16) {
      const base = Number(baseAtZoom16) || 4;
      return [
        'interpolate', ['linear'], ['zoom'],
        10, base * 0.35,
        14, base * 0.7,
        16, base * 1.0,
        18, base * 1.6,
        20, base * 2.4
      ];
    }

    // ------------------------------------------------------------------
    // FIX: la freccia di direzione viene ruotata tramite l'opzione
    // `rotation` del Marker. NON usare style.transform (Mapbox lo
    // sovrascrive per posizionare il marker sulla mappa).
    // ------------------------------------------------------------------
    function drawArrow(arrow, color) {
      if (!arrow || !arrow.tail || !arrow.tip || arrow.heading_deg == null) return;

      const center = arrow.center
        ? arrow.center
        : [
            (arrow.tail[0] + arrow.tip[0]) / 2,
            (arrow.tail[1] + arrow.tip[1]) / 2
          ];

      const el = document.createElement('div');
      el.className = 'direction-arrow';

      el.innerHTML =
        '<svg width="34" height="34" viewBox="0 0 34 34" xmlns="http://www.w3.org/2000/svg">' +
          '<line x1="17" y1="30" x2="17" y2="12" stroke="' + color + '" stroke-width="5" stroke-linecap="round"/>' +
          '<path d="M17 3 L27 18 L17 13.5 L7 18 Z" fill="' + color + '" stroke="#ffffff" stroke-width="1.2" stroke-linejoin="round"/>' +
        '</svg>';

      const marker = new mapboxgl.Marker({
        element: el,
        anchor: 'center',
        rotationAlignment: 'map',
        pitchAlignment: 'map',
        rotation: arrow.heading_deg
      })
        .setLngLat([center[1], center[0]])
        .addTo(map);

      addLayer(marker);
    }

    function safeAddSource(id, data) {
      if (map.getSource(id)) {
        map.getSource(id).setData(data);
      } else {
        map.addSource(id, { type: 'geojson', data });
      }
    }

    // ------------------------------------------------------------------
    // FIX box: genera un poligono geografico che approssima un cerchio
    // di raggio `radiusM` metri attorno a `center` = [lat, lon].
    // In questo modo il cerchio scala correttamente con lo zoom.
    // ------------------------------------------------------------------
    function createCirclePolygon(centerLat, centerLon, radiusM, segments) {
      const numSegments = Math.max(24, Math.floor(segments || 64));
      const earthRadiusM = 6371000.0;
      const latRad = centerLat * Math.PI / 180.0;
      const lonRad = centerLon * Math.PI / 180.0;
      const angularDistance = radiusM / earthRadiusM;

      const coords = [];

      for (let i = 0; i <= numSegments; i++) {
        const bearing = (i / numSegments) * 2.0 * Math.PI;

        const newLatRad = Math.asin(
          Math.sin(latRad) * Math.cos(angularDistance) +
          Math.cos(latRad) * Math.sin(angularDistance) * Math.cos(bearing)
        );

        const newLonRad = lonRad + Math.atan2(
          Math.sin(bearing) * Math.sin(angularDistance) * Math.cos(latRad),
          Math.cos(angularDistance) - Math.sin(latRad) * Math.sin(newLatRad)
        );

        // GeoJSON: [lon, lat]
        coords.push([
          newLonRad * 180.0 / Math.PI,
          newLatRad * 180.0 / Math.PI
        ]);
      }

      return coords;
    }

    function addCenterDot(center, color, sizePx, popupText) {
      const el = document.createElement('div');
      el.className = 'center-dot';
      el.style.width = sizePx + 'px';
      el.style.height = sizePx + 'px';
      el.style.background = color;

      const marker = new mapboxgl.Marker({ element: el })
        .setLngLat([center[1], center[0]]);

      if (popupText) {
        marker.setPopup(new mapboxgl.Popup().setText(popupText));
      }

      marker.addTo(map);
      addLayer(marker);
    }

    function drawConfig(config) {
      const fingerprint = JSON.stringify(config);
      if (fingerprint === configFingerprint) return;

      configFingerprint = fingerprint;
      clearConfiguration();

      const bounds = new mapboxgl.LngLatBounds();
      const sf = config.start_finish;
      const sectors = config.sectors || [];
      const pitBox = config.pit_box;
      const pitLane = config.pit_lane;

      document.getElementById("track-name").textContent =
        config.track && config.track.name ? config.track.name : "Pista non configurata";

      // ---------------- START / FINISH ----------------
      if (sf && Array.isArray(sf.line) && sf.line.length === 2) {
        const lineCoords = sf.line.map(p => [p[1], p[0]]);
        const originalCoords = sf.original_line.map(p => [p[1], p[0]]);

        safeAddSource('sf-extended', {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: lineCoords }
        });
        if (!map.getLayer('sf-extended-layer')) {
          map.addLayer({
            id: 'sf-extended-layer',
            type: 'line',
            source: 'sf-extended',
            paint: {
              'line-color': '#ef4444',
              'line-width': zoomLineWidth(7)
            }
          });
        }

        safeAddSource('sf-original', {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: originalCoords }
        });
        if (!map.getLayer('sf-original-layer')) {
          map.addLayer({
            id: 'sf-original-layer',
            type: 'line',
            source: 'sf-original',
            paint: {
              'line-color': '#ffffff',
              'line-width': zoomLineWidth(2),
              'line-dasharray': [1, 1]
            }
          });
        }

        addCenterDot(sf.center, '#ef4444', 12, 'START / FINISH');
        drawArrow(sf.direction_arrow, '#ef4444');

        bounds.extend(lineCoords[0]);
        bounds.extend(lineCoords[1]);
      }

      // ---------------- SETTORI ----------------
      sectors.forEach((sector, index) => {
        const color = colors[index % colors.length];
        if (!Array.isArray(sector.line) || sector.line.length !== 2) return;

        const lineCoords = sector.line.map(p => [p[1], p[0]]);
        const originalCoords = sector.original_line.map(p => [p[1], p[0]]);

        safeAddSource(`sector-${index}-extended`, {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: lineCoords }
        });
        if (!map.getLayer(`sector-${index}-extended-layer`)) {
          map.addLayer({
            id: `sector-${index}-extended-layer`,
            type: 'line',
            source: `sector-${index}-extended`,
            paint: {
              'line-color': color,
              'line-width': zoomLineWidth(6)
            }
          });
        }

        safeAddSource(`sector-${index}-original`, {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: originalCoords }
        });
        if (!map.getLayer(`sector-${index}-original-layer`)) {
          map.addLayer({
            id: `sector-${index}-original-layer`,
            type: 'line',
            source: `sector-${index}-original`,
            paint: {
              'line-color': '#ffffff',
              'line-width': zoomLineWidth(2),
              'line-dasharray': [1, 1]
            }
          });
        }

        addCenterDot(sector.center, color, 10, sector.name);

        bounds.extend(lineCoords[0]);
        bounds.extend(lineCoords[1]);
      });

      // ---------------- PIT BOX ----------------
      if (pitBox) {
        if (pitBox.type === 'circle') {
          const center = pitBox.center;   // [lat, lon]
          const radiusM = Number(pitBox.radius_m) || 12;

          const circleCoords = createCirclePolygon(
            center[0], center[1], radiusM, 72
          );

          safeAddSource('pitbox-circle', {
            type: 'Feature',
            geometry: {
              type: 'Polygon',
              coordinates: [circleCoords]
            }
          });

          if (!map.getLayer('pitbox-circle-fill')) {
            map.addLayer({
              id: 'pitbox-circle-fill',
              type: 'fill',
              source: 'pitbox-circle',
              paint: {
                'fill-color': PIT_BOX_COLOR,
                'fill-opacity': 0.28
              }
            });
          }

          if (!map.getLayer('pitbox-circle-outline')) {
            map.addLayer({
              id: 'pitbox-circle-outline',
              type: 'line',
              source: 'pitbox-circle',
              paint: {
                'line-color': PIT_BOX_COLOR,
                'line-width': zoomLineWidth(3)
              }
            });
          }

          // piccolo dot centrale (solo per popup)
          addCenterDot(center, PIT_BOX_COLOR, 10, 'BOX');

          // espandi i bounds lungo tutto il cerchio
          circleCoords.forEach(c => bounds.extend(c));
        }
        else if (
          pitBox.type === 'polygon' &&
          Array.isArray(pitBox.polygon) &&
          pitBox.polygon.length >= 3
        ) {
          const polyCoords = pitBox.polygon.map(p => [p[1], p[0]]);
          polyCoords.push(polyCoords[0]);

          safeAddSource('pitbox-poly', {
            type: 'Feature',
            geometry: { type: 'Polygon', coordinates: [polyCoords] }
          });

          if (!map.getLayer('pitbox-poly-layer')) {
            map.addLayer({
              id: 'pitbox-poly-layer',
              type: 'fill',
              source: 'pitbox-poly',
              paint: {
                'fill-color': PIT_BOX_COLOR,
                'fill-opacity': 0.28
              }
            });
          }

          if (!map.getLayer('pitbox-poly-outline')) {
            map.addLayer({
              id: 'pitbox-poly-outline',
              type: 'line',
              source: 'pitbox-poly',
              paint: {
                'line-color': PIT_BOX_COLOR,
                'line-width': zoomLineWidth(3)
              }
            });
          }

          polyCoords.forEach(c => bounds.extend(c));
        }
      }

      // ---------------- PIT LANE ----------------
      if (pitLane) {
        let laneCoords = null;
        if (Array.isArray(pitLane.path) && pitLane.path.length >= 2) {
          laneCoords = pitLane.path.map(p => [p[1], p[0]]);
        } else if (pitLane.start && pitLane.end) {
          laneCoords = [
            [pitLane.start[1], pitLane.start[0]],
            [pitLane.end[1], pitLane.end[0]]
          ];
        }

        if (laneCoords) {
          safeAddSource('pitlane', {
            type: 'Feature',
            geometry: { type: 'LineString', coordinates: laneCoords }
          });

          if (!map.getLayer('pitlane-layer')) {
            map.addLayer({
              id: 'pitlane-layer',
              type: 'line',
              source: 'pitlane',
              paint: {
                'line-color': '#ec4899',
                'line-width': zoomLineWidth(5)
              }
            });
          }

          laneCoords.forEach(c => bounds.extend(c));
        }
      }

      if (!USING_DEMO_TOKEN) {
        setStatus(
          "YAML: " + (sf ? "traguardo OK" : "traguardo assente") +
          " | " + sectors.length + " settori | " +
          (pitBox ? "box OK" : "box assente") +
          (pitLane ? " | pit lane OK" : "")
        );
      }

      if (!initialZoomDone && !bounds.isEmpty()) {
        map.fitBounds(bounds, { padding: 50, maxZoom: 19 });
        initialZoomDone = true;
      }
    }

    function updateGps(gps) {
      const gpsStatus = document.getElementById("gps-status");
      const valid = gps && gps.fix_valid === true &&
        Number.isFinite(Number(gps.latitude)) &&
        Number.isFinite(Number(gps.longitude)) &&
        !(Number(gps.latitude) === 0 && Number(gps.longitude) === 0);

      gpsStatus.textContent = valid
        ? (Number(gps.fix_type) === 3 ? "FIX 3D" : "FIX")
        : "NO FIX";
      gpsStatus.className = "value " + (valid ? "good" : "bad");

      document.getElementById("speed").textContent =
        Math.round(Number(gps && gps.speed_kmph) || 0) + " km/h";
      document.getElementById("satellites").textContent =
        String(gps && gps.satellites_used != null ? gps.satellites_used : 0);
      document.getElementById("hdop").textContent =
        gps && gps.hdop != null ? Number(gps.hdop).toFixed(2) : "—";

      if (!valid) return;

      const position = [Number(gps.longitude), Number(gps.latitude)];

      if (!gpsMarker) {
        const el = document.createElement('div');
        el.className = 'live-kart';
        gpsMarker = new mapboxgl.Marker({ element: el })
          .setLngLat(position)
          .setPopup(new mapboxgl.Popup().setText("GPS LIVE"))
          .addTo(map);
      } else {
        gpsMarker.setLngLat(position);
      }

      gpsTrail.push(position);
      if (gpsTrail.length > 1200) gpsTrail.shift();

      if (!gpsTrailLine) {
        safeAddSource('gps-trail', {
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: gpsTrail }
        });
        if (!map.getLayer('gps-trail-layer')) {
          map.addLayer({
            id: 'gps-trail-layer',
            type: 'line',
            source: 'gps-trail',
            paint: {
              'line-color': '#a855f7',
              'line-width': zoomLineWidth(4),
              'line-opacity': 0.85
            }
          });
        }
        gpsTrailLine = true;
      } else {
        map.getSource('gps-trail').setData({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: gpsTrail }
        });
      }
    }

    async function updateMap() {
      try {
        const response = await fetch("/api/map/live", { cache: "no-store" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();
        lastConfig = data.config;
        lastGps = data.gps;
        if (map.isStyleLoaded()) {
          drawConfig(lastConfig);
        }
        updateGps(lastGps);

        const sessBtn = document.getElementById("sessionButton");
        if (data.session_status === "running") {
          sessBtn.textContent = "Stop Session";
          sessBtn.classList.add("stop");
        } else {
          sessBtn.textContent = "Start Session";
          sessBtn.classList.remove("stop");
        }
      } catch (error) {
        if (!USING_DEMO_TOKEN) {
          setStatus("Errore: " + error.message);
        }
        console.error("Errore aggiornamento mappa:", error);
      }

      updateTimer = window.setTimeout(updateMap, 500);
    }

    styleToggle.addEventListener("click", () => {
      const newStyle = (currentStyle === 'satellite') ? STREETS_STYLE : SATELLITE_STYLE;
      currentStyle = (currentStyle === 'satellite') ? 'streets' : 'satellite';
      styleToggle.textContent = (currentStyle === 'satellite') ? 'Satellite' : 'Traditional';

      clearTimeout(updateTimer);
      map.setStyle(newStyle);

      map.once('style.load', () => {
        configFingerprint = null;
        initialZoomDone = false;
        if (gpsMarker) { gpsMarker.remove(); gpsMarker = null; }
        gpsTrailLine = null;
        if (lastConfig) drawConfig(lastConfig);
        if (lastGps) updateGps(lastGps);
        updateTimer = window.setTimeout(updateMap, 500);
      });
    });

    map.on('load', () => {
      map.resize();
      window.setTimeout(() => map.resize(), 250);
      updateMap();
    });

    window.addEventListener('resize', () => map.resize());
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Endpoint Flask
# -----------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template_string(
        MAP_HTML,
        MAPBOX_TOKEN=MAPBOX_TOKEN,
        USING_DEMO_TOKEN=USING_DEMO_TOKEN,
        MQTT_BROKER=MQTT_CONFIG["broker"],
        MQTT_WS_PORT=MQTT_CONFIG["websocket_port"],
        MQTT_WS_PATH=MQTT_CONFIG["websocket_path"],
        MQTT_TOPIC=MQTT_CONFIG["topic"],
    )


@app.get("/api/map/live")
def map_live():
    return jsonify({
        "config": build_map_config(),
        "gps": read_live_gps(),
        "session_status": read_live_session_status(),
    })


if __name__ == "__main__":
    print()
    print("=== Kart Track Map Live ===")
    print(f"Mappa: http://127.0.0.1:{PORT}")
    print(f"Configurazione YAML: {TRACK_FILE}")
    print(f"GPS live da: {MAIN_APP_API}")
    print(
        "MQTT WebSocket: "
        f"ws://{MQTT_CONFIG['broker']}:{MQTT_CONFIG['websocket_port']}"
        f"{MQTT_CONFIG['websocket_path']} "
        f"(topic={MQTT_CONFIG['topic']})"
    )
    if USING_DEMO_TOKEN:
        print(
            "Mapbox Token: DEMO PUBBLICO (imposta MAPBOX_TOKEN per "
            "un token affidabile, es. export MAPBOX_TOKEN=pk.xxxx)"
        )
    else:
        print("Mapbox Token: impostato da variabile d'ambiente")
    print()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )