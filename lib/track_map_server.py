#!/usr/bin/env python3

import json
import math
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml
from flask import Flask, jsonify, render_template_string


BASE_DIR = Path(__file__).resolve().parent.parent
TRACK_FILE = BASE_DIR / "config" / "tracks" / "prima-pista.yaml"
APP_CONFIG_FILE = BASE_DIR / "config" / "app.yaml"

MAIN_APP_API = "http://127.0.0.1:8080/api/live"

HOST = "0.0.0.0"
PORT = 8081

app = Flask(__name__)


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
    keepalive_s = int(mqtt_config.get("keepalive_s", 60))

    # Il topic è quello usato dall'app principale per ricevere la telemetria.
    # Il file allegato contiene MQTT_CONFIG["topic"].
    topic = mqtt_config.get("topic")

    if not topic:
        raise RuntimeError(
            "Il topic MQTT non è presente in config/app.yaml "
            "alla chiave mqtt.topic."
        )

    return {
        "broker": broker,
        "port": port,
        "keepalive_s": keepalive_s,
        "topic": topic,
    }


MQTT_CONFIG = load_mqtt_config()


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

    result = {
        "track": {
            "id": track.get("id", "unknown"),
            "name": track.get("name", "Pista non configurata"),
        },
        "start_finish": None,
        "sectors": [],
        "pit_box": None,
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

    if pit_center:
        result["pit_box"] = {
            "center": pit_center,
            "radius_m": float(
                pit_box_config.get("radius_m", 12.0)
            ),
            "exit_hysteresis_m": float(
                pit_box_config.get("exit_hysteresis_m", 4.0)
            ),
        }

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


# -----------------------------------------------------------------------------
# MQTT: il pulsante pubblica direttamente sul topic dell'app principale.
# -----------------------------------------------------------------------------

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_connected = False
mqtt_last_error = None


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    global mqtt_connected, mqtt_last_error

    mqtt_connected = reason_code == 0
    mqtt_last_error = None if mqtt_connected else str(reason_code)

    if mqtt_connected:
        print(
            "[MQTT] Connected: "
            f"{MQTT_CONFIG['broker']}:{MQTT_CONFIG['port']}"
        )


def on_mqtt_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):
    global mqtt_connected, mqtt_last_error

    mqtt_connected = False
    mqtt_last_error = f"MQTT disconnected: {reason_code}"
    print(f"[MQTT] Disconnected: {reason_code}")


def start_mqtt():
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect_async(
        MQTT_CONFIG["broker"],
        MQTT_CONFIG["port"],
        MQTT_CONFIG["keepalive_s"],
    )
    mqtt_client.loop_start()


def publish_cooldown():
    payload = {"cooldown": True}
    payload_json = json.dumps(payload, separators=(",", ":"))

    if not mqtt_connected:
        return False, {
            "error": "MQTT non connesso.",
            "mqtt_error": mqtt_last_error,
            "topic": MQTT_CONFIG["topic"],
        }

    result = mqtt_client.publish(
        MQTT_CONFIG["topic"],
        payload_json,
        qos=1,
        retain=False,
    )

    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return False, {
            "error": mqtt.error_string(result.rc),
            "topic": MQTT_CONFIG["topic"],
            "payload": payload,
        }

    return True, {
        "topic": MQTT_CONFIG["topic"],
        "payload": payload,
        "qos": 1,
        "retain": False,
    }


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

  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  >

  <style>
    html, body, #map {
      width: 100%;
      height: 100%;
      margin: 0;
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
      width: 0;
      height: 0;
      border-left: 9px solid transparent;
      border-right: 9px solid transparent;
      border-bottom: 25px solid #ef4444;
      filter: drop-shadow(0 0 3px rgba(0, 0, 0, 0.95));
      transform-origin: 50% 50%;
    }

    .command-row {
      margin-top: 10px;
      padding-top: 9px;
      border-top: 1px solid #36383d;
    }

    .cooldown-button {
      width: 100%;
      padding: 8px 10px;
      background: #1c2333;
      color: #9fd7ff;
      border: 1px solid #3a4a6b;
      border-radius: 7px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background-color .15s ease, color .15s ease, opacity .15s ease;
    }

    .cooldown-button:hover { background: #253150; }
    .cooldown-button:active { background: #2f3f66; }
    .cooldown-button:disabled { opacity: 0.5; cursor: default; }

    .command-status {
      margin-top: 6px;
      min-height: 12px;
      color: #b1b4ba;
      font-size: 10px;
      line-height: 1.35;
    }

    .command-status.ok { color: #2ed477; }
    .command-status.error { color: #f16066; }
  </style>
</head>
<body>
  <div id="map"></div>

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
      <div class="legend-row"><span class="legend-line" style="background:#6b7280"></span>BOX</div>
      <div class="legend-row"><span class="legend-line" style="background:#a855f7"></span>GPS live</div>
    </div>

    <div class="command-row">
      <button id="cooldownButton" class="cooldown-button" type="button">
        Cooldown
      </button>
      <div id="commandStatus" class="command-status"></div>
    </div>

    <div id="map-status" class="map-status">
      Lettura configurazione YAML…
    </div>
  </aside>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const map = L.map("map", { preferCanvas: true });

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 20,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
    }).addTo(map);

    const colors = ["#2563eb", "#16a34a", "#f59e0b"];
    let configFingerprint = null;
    let configurationLayers = [];
    let initialZoomDone = false;
    let gpsMarker = null;
    let gpsTrail = [];
    let gpsTrailLine = null;

    function addLayer(layer) {
      configurationLayers.push(layer);
      return layer;
    }

    function clearConfiguration() {
      configurationLayers.forEach((layer) => map.removeLayer(layer));
      configurationLayers = [];
    }

    function drawArrow(arrow, color) {
      if (!arrow || !arrow.tail || !arrow.tip) return;

      const tail = L.latLng(arrow.tail[0], arrow.tail[1]);
      const tip = L.latLng(arrow.tip[0], arrow.tip[1]);
      const angle = Math.atan2(tip.lng - tail.lng, tip.lat - tail.lat) * 180 / Math.PI;

      const icon = L.divIcon({
        className: "",
        html: '<div class="direction-arrow" style="border-bottom-color:' + color + ';transform:rotate(' + angle + 'deg)"></div>',
        iconSize: [18, 25],
        iconAnchor: [9, 12]
      });

      addLayer(L.marker([(tail.lat + tip.lat) / 2, (tail.lng + tip.lng) / 2], {
        icon: icon,
        interactive: false
      }).addTo(map));
    }

    function drawConfig(config) {
      const fingerprint = JSON.stringify(config);
      if (fingerprint === configFingerprint) return;

      configFingerprint = fingerprint;
      clearConfiguration();

      const bounds = [];
      const sf = config.start_finish;
      const sectors = config.sectors || [];
      const pitBox = config.pit_box;

      document.getElementById("track-name").textContent =
        config.track && config.track.name ? config.track.name : "Pista non configurata";

      if (sf && Array.isArray(sf.line) && sf.line.length === 2) {
        addLayer(L.polyline(sf.line, { color: "#ef4444", weight: 7, opacity: 1 }).addTo(map));
        addLayer(L.polyline(sf.original_line, { color: "#ffffff", weight: 2, opacity: 0.65, dashArray: "3 5" }).addTo(map));
        addLayer(L.circleMarker(sf.center, { radius: 6, color: "#ffffff", weight: 2, fillColor: "#ef4444", fillOpacity: 1 }).addTo(map).bindPopup(
          "<b>START / FINISH</b><br>Estensione: +" + sf.line_extension_each_side_m + " m per lato"
        ))
        drawArrow(sf.direction_arrow, "#ef4444");
        bounds.push(...sf.line);
      }

      sectors.forEach((sector, index) => {
        const color = colors[index % colors.length];
        if (!Array.isArray(sector.line) || sector.line.length !== 2) return;

        addLayer(L.polyline(sector.line, { color: color, weight: 6, opacity: 1 }).addTo(map));
        addLayer(L.polyline(sector.original_line, { color: "#ffffff", weight: 2, opacity: 0.6, dashArray: "3 5" }).addTo(map));
        addLayer(L.circleMarker(sector.center, { radius: 5, color: "#ffffff", weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map).bindPopup(
          "<b>" + sector.name + "</b><br>Estensione: +" + sector.line_extension_each_side_m + " m per lato"
        ));

        bounds.push(...sector.line);
      });

      if (pitBox && pitBox.center) {
        addLayer(L.circle(pitBox.center, { radius: pitBox.radius_m, color: "#6b7280", weight: 3, fillColor: "#6b7280", fillOpacity: 0.30 }).addTo(map).bindPopup(
          "<b>BOX</b><br>Raggio: " + pitBox.radius_m + " m"
        ));
        bounds.push(pitBox.center);
      }

      document.getElementById("map-status").textContent =
        "YAML: " + (sf ? "traguardo OK" : "traguardo assente") +
        " | " + sectors.length + " settori | " +
        (pitBox ? "box OK" : "box assente");

      if (!initialZoomDone && bounds.length > 1) {
        map.fitBounds(bounds, { padding: [80, 80], maxZoom: 19 });
        initialZoomDone = true;
      }
    }

    function updateGps(gps) {
      const gpsStatus = document.getElementById("gps-status");
      const valid = gps && gps.fix_valid === true && Number.isFinite(Number(gps.latitude)) && Number.isFinite(Number(gps.longitude)) && !(Number(gps.latitude) === 0 && Number(gps.longitude) === 0);

      gpsStatus.textContent = valid ? (Number(gps.fix_type) === 3 ? "FIX 3D" : "FIX") : "NO FIX";
      gpsStatus.className = "value " + (valid ? "good" : "bad");
      document.getElementById("speed").textContent = Math.round(Number(gps && gps.speed_kmph) || 0) + " km/h";
      document.getElementById("satellites").textContent = String(gps && gps.satellites_used != null ? gps.satellites_used : 0);
      document.getElementById("hdop").textContent = gps && gps.hdop != null ? Number(gps.hdop).toFixed(2) : "—";

      if (!valid) return;

      const position = [Number(gps.latitude), Number(gps.longitude)];

      if (!gpsMarker) {
        const icon = L.divIcon({ className: "", html: '<div class="live-kart"></div>', iconSize: [20, 20], iconAnchor: [10, 10] });
        gpsMarker = L.marker(position, { icon: icon, zIndexOffset: 1000 }).addTo(map);
        gpsMarker.bindPopup("<b>GPS LIVE</b>");
      } else {
        gpsMarker.setLatLng(position);
      }

      gpsTrail.push(position);
      if (gpsTrail.length > 1200) gpsTrail.shift();

      if (!gpsTrailLine) {
        gpsTrailLine = L.polyline(gpsTrail, { color: "#a855f7", weight: 4, opacity: 0.85 }).addTo(map);
      } else {
        gpsTrailLine.setLatLngs(gpsTrail);
      }
    }

    async function updateMap() {
      try {
        const response = await fetch("/api/map/live", { cache: "no-store" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();
        drawConfig(data.config);
        updateGps(data.gps);
      } catch (error) {
        document.getElementById("map-status").textContent = "Errore: " + error.message;
        console.error("Errore aggiornamento mappa:", error);
      }

      window.setTimeout(updateMap, 500);
    }

    const cooldownButton = document.getElementById("cooldownButton");
    const commandStatus = document.getElementById("commandStatus");

    async function sendCooldownCommand() {
      cooldownButton.disabled = true;
      commandStatus.className = "command-status";
      commandStatus.textContent = "Pubblicazione MQTT cooldown…";

      try {
        const response = await fetch("/api/command/cooldown", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command: "cooldown" })
        });

        const data = await response.json().catch(() => ({}));

        if (response.ok && data.ok) {
          commandStatus.className = "command-status ok";
          commandStatus.textContent = "Cooldown pubblicato via MQTT.";
        } else {
          commandStatus.className = "command-status error";
          commandStatus.textContent = "Errore MQTT" + (data && data.error ? ": " + data.error : ".");
        }
      } catch (error) {
        commandStatus.className = "command-status error";
        commandStatus.textContent = "Errore rete: " + error.message;
      } finally {
        cooldownButton.disabled = false;
      }
    }

    cooldownButton.addEventListener("click", sendCooldownCommand);
    updateMap();
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Endpoint Flask
# -----------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template_string(MAP_HTML)


@app.get("/api/map/live")
def map_live():
    return jsonify({
        "config": build_map_config(),
        "gps": read_live_gps(),
    })


@app.post("/api/command/cooldown")
def api_command_cooldown():
    """
    Endpoint locale usato solo dalla pagina web.

    Non inoltra più il comando a un endpoint HTTP dell'app principale:
    pubblica direttamente su MQTT il payload {"cooldown": true}, usando
    lo stesso topic configurato in app.yaml e usato dall'app principale.
    """
    ok, result = publish_cooldown()

    if ok:
        return jsonify({
            "ok": True,
            **result,
        })

    return jsonify({
        "ok": False,
        **result,
    }), 503


if __name__ == "__main__":
    start_mqtt()

    print()
    print("=== Kart Track Map Live ===")
    print(f"Mappa: http://127.0.0.1:{PORT}")
    print(f"Configurazione YAML: {TRACK_FILE}")
    print(f"GPS live da: {MAIN_APP_API}")
    print(
        "Cooldown MQTT: "
        f"topic={MQTT_CONFIG['topic']} "
        f"broker={MQTT_CONFIG['broker']}:{MQTT_CONFIG['port']}"
    )
    print()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
