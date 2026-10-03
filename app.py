#!/usr/bin/env python3

import json
import math
import signal
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml
from flask import Flask, jsonify, render_template_string, request


BASE_DIR = Path(__file__).resolve().parent


def load_yaml(relative_path):
    with (BASE_DIR / relative_path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


APP_CONFIG = load_yaml("config/app.yaml")
DRIVERS_CONFIG = load_yaml("config/drivers.yaml")

# -------------------------------------------------------------
# Nessuna pista di default.
# TRACK_CONFIG resta vuoto {} finché l'utente non seleziona
# una pista dal setup (via START SESSION o GO TO DASHBOARD).
# -------------------------------------------------------------
TRACK_CONFIG = {}
current_track_id = None

TRACKS_DIR = BASE_DIR / "config" / "tracks"

SERVER_CONFIG = APP_CONFIG["server"]
MQTT_CONFIG = APP_CONFIG["mqtt"]
SESSION_CONFIG = APP_CONFIG["session"]
DASHBOARD_CONFIG = APP_CONFIG["dashboard"]
FILTER_CONFIG = APP_CONFIG["telemetry_filter"]

DEFAULT_DRIVER_ID = SESSION_CONFIG.get("default_driver_id")

SESSIONS_DIR = BASE_DIR / SESSION_CONFIG["sessions_dir"]
CURRENT_DIR = BASE_DIR / SESSION_CONFIG["current_dir"]

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
CURRENT_DIR.mkdir(parents=True, exist_ok=True)

AUTOSAVE_PATH = CURRENT_DIR / "active_session.json"
EVENTS_PATH = CURRENT_DIR / "active_events.jsonl"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR.parent / "templates"),
    static_folder=str(BASE_DIR.parent / "static"),
)

lock = threading.RLock()
drivers = DRIVERS_CONFIG.get("drivers", [])

latest_sensors = {
    "connected": False,
    "updated_at": None,
    "satellites_used": 0,
    "latitude": 0.0,
    "longitude": 0.0,
    "speed_kmph": 0.0,
    "fix_valid": False,
    "fix_quality": 0,
    "fix_type": 0,
    "hdop": None,
    "temperature_c": None,
    "as5600_angle_deg": None,
    "as5600_rpm": None,
    "as5600_magnet_ok": False,
    "ir_rpm": None,
    "ir_total_pulses": None,
}

last_seen = {
    "hotspot": None,
    "mqtt": None,
    "ntc": None,
    "as5600": None,
    "ir_rpm": None,
    "gps": None,
}

sensor_status = {
    "hotspot": False,
    "mqtt": False,
    "ntc": False,
    "as5600": False,
    "ir_rpm": False,
    "gps": False,
}

SENSOR_OFFLINE_TIMEOUT_S = 5 * 60
DATA_AGE_SAMPLE_INTERVAL_S = 0.25

FIX_TYPE_LABELS = {
    0: "No Fix",
    1: "GPS",
    2: "DGPS",
    3: "PPS",
    4: "RTK",
    5: "Float RTK",
    6: "Estimated",
    7: "Manual",
    8: "Simulation",
}

runtime = {
    "mqtt_connected": False,
    "mqtt_last_message_at": None,
    "mqtt_last_message_epoch": 0.0,
    "mqtt_topic": None,
    "mqtt_messages": 0,
    "telemetry_payload_received": False,
    "last_error": None,
}

mqtt_client = None

# Driver + pista "in anteprima" quando si va in dashboard senza START.
preview = {
    "driver": None,
}

# =============================================================
# SCHEMA — formato colonnare per i track_points
# =============================================================

TRACK_POINT_FIELDS = [
    "lap_elapsed_s",
    "lap_distance_m",
    "delta_live_s",
    "latitude",
    "longitude",
    "speed_kmph",
    "satellites_used",
    "hdop",
    "temperature_c",
    "as5600_rpm",
    "ir_rpm",
    "received_at_epoch",
]

WARMUP_POINT_FIELDS = [
    "received_at_epoch",
    "latitude",
    "longitude",
    "speed_kmph",
    "satellites_used",
    "hdop",
    "temperature_c",
    "as5600_rpm",
    "ir_rpm",
]

LAP_SCOPED_EVENTS = {
    "lap_started",
    "lap_completed",
    "lap_aborted",
    "sector_completed",
    "sector_cross_ignored",
    "finish_cross_ignored",
}


def columnar(points, fields):
    columns = {field: [] for field in fields}

    for point in points:
        for field in fields:
            columns[field].append(point.get(field))

    return columns


def _new_point_from_sample(sample, epoch, lap_elapsed_s):
    return {
        "latitude": float(sample["latitude"]),
        "longitude": float(sample["longitude"]),
        "received_at_epoch": float(epoch),
        "lap_elapsed_s": max(0.0, float(lap_elapsed_s)),
        "speed_kmph": safe_float(sample.get("speed_kmph"), 0.0),
        "satellites_used": safe_int(sample.get("satellites_used"), 0),
        "hdop": safe_float(sample.get("hdop"), None),
        "temperature_c": safe_float(sample.get("temperature_c"), None),
        "as5600_rpm": safe_float(sample.get("as5600_rpm"), None),
        "ir_rpm": safe_int(sample.get("ir_rpm"), None),
        "delta_live_s": None,
        "lap_distance_m": 0.0,
    }


def now_local():
    return datetime.now().astimezone()


def now_iso():
    return now_local().isoformat(timespec="milliseconds")


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def get_driver(driver_id):
    for driver in drivers:
        if driver.get("id") == driver_id:
            return driver
    return None


def active_track_loaded():
    return bool(TRACK_CONFIG) and bool(current_track_id)


# =============================================================
# Track discovery
# =============================================================

def _extract_track_info(config, path):
    info = config.get("track", {}) or {}

    track_id = info.get("id") or config.get("id") or path.stem
    name = info.get("name") or config.get("name") or track_id
    location = info.get("location") or config.get("location") or ""

    return track_id, name, location


def list_available_tracks():
    tracks = []
    seen_ids = set()

    print(f"[TRACKS] Scan directory: {TRACKS_DIR}")

    if not TRACKS_DIR.exists():
        print(f"[TRACKS] !! Directory does not exist: {TRACKS_DIR}")
        return []

    yaml_files = sorted(TRACKS_DIR.glob("*.yaml"))

    print(f"[TRACKS] Found {len(yaml_files)} yaml file(s): "
          f"{[p.name for p in yaml_files]}")

    for path in yaml_files:
        try:
            with path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except Exception as error:
            print(f"[TRACKS] !! Failed to parse {path.name}: {error}")
            continue

        if not isinstance(config, dict):
            print(f"[TRACKS] !! {path.name} is not a mapping, skipping")
            continue

        track_id, name, location = _extract_track_info(config, path)

        if track_id in seen_ids:
            print(f"[TRACKS] Duplicate id '{track_id}' in {path.name}, skipping")
            continue

        seen_ids.add(track_id)

        tracks.append({
            "id": track_id,
            "name": name,
            "location": location,
            "file": path.name,
        })

        print(f"[TRACKS]   + id={track_id!r} name={name!r} "
              f"file={path.name}")

    if not tracks:
        print("[TRACKS] !! No valid tracks found")

    return tracks


def find_track_path(track_id):
    if not TRACKS_DIR.exists():
        return None

    seen_ids = set()

    for path in sorted(TRACKS_DIR.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}
        except Exception:
            continue

        if not isinstance(config, dict):
            continue

        candidate_id, _, _ = _extract_track_info(config, path)

        if candidate_id in seen_ids:
            continue

        seen_ids.add(candidate_id)

        if candidate_id == track_id:
            return path

    return None


def set_active_track(track_id):
    global TRACK_CONFIG, current_track_id

    if not track_id:
        raise ValueError("Nessuna pista selezionata")

    path = find_track_path(track_id)

    if path is None:
        raise ValueError(f"Track '{track_id}' non trovata")

    with path.open("r", encoding="utf-8") as file:
        TRACK_CONFIG = yaml.safe_load(file) or {}

    current_track_id = track_id

    print(f"[TRACKS] Active track set to '{track_id}' "
          f"({TRACK_CONFIG.get('track', {}).get('name', track_id)})")


# =============================================================

def sector_count():
    return len(TRACK_CONFIG.get("sectors", [])) + 1


def new_session_state():
    count = sector_count()

    return {
        "status": "idle",
        "session_guid": None,
        "driver": None,
        "started_at": None,
        "paused_at": None,
        "paused_total_s": 0.0,
        "ended_at": None,
        "data_age_at_start_s": None,

        "mqtt_topic": None,
        "events": [],

        "track_state": "pitlane",
        "previous_track_state": None,
        "pit_lane_exit_seen": False,
        "warmup_started_at": None,
        "cooldown_active": False,

        "current_lap_number": 0,
        "current_lap_start_epoch": None,
        "sector_started_epoch": None,
        "current_sector_number": 1,
        "current_lap_sectors_s": [],
        "current_lap_sector_statuses": [None] * count,
        "current_lap_timer_running": False,
        "current_lap_track_points": [],
        "current_lap_events": [],
        "current_lap_distance_m": 0.0,

        "laps": [],

        "best_lap_time_s": None,
        "best_sector_times_s": [None] * count,
        "ideal_lap_time_s": None,
        "last_completed_lap_time_s": None,
        "last_completed_lap_at_epoch": None,
        "last_completed_lap_sectors_s": [None] * count,
        "last_aborted_lap": None,
        "pit_lane_entry_detected_at_epoch": None,
        "last_sector_result": None,
        "sector_event_id": 0,

        "reference_lap_track_points": [],
        "reference_lap_time_s": None,
        "reference_lap_number": None,
        "delta_live_s": None,

        "warmup_and_pitlane_raw": [],

        "session_sample_count": 0,
        "data_age_samples": [],
        "session_valid_gps_count": 0,
        "session_track_point_count": 0,

        "previous_timing_sample": None,
    }


session = new_session_state()


def append_event(event_name, **data):
    event = {
        "event": event_name,
        "at": now_iso(),
        **data,
    }

    if session.get("current_lap_timer_running") and "lap_number" not in event:
        event["lap_number"] = session.get("current_lap_number")

    session["events"].append(event)

    if (
        session.get("current_lap_timer_running")
        and event_name in LAP_SCOPED_EVENTS
    ):
        session["current_lap_events"].append(deepcopy(event))

    with EVENTS_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def session_elapsed_s():
    if session["status"] == "idle" or not session["started_at"]:
        return 0.0

    started = datetime.fromisoformat(session["started_at"])

    ended = (
        datetime.fromisoformat(session["ended_at"])
        if session["ended_at"]
        else now_local()
    )

    elapsed = (ended - started).total_seconds() - session["paused_total_s"]

    if session["status"] == "paused" and session["paused_at"]:
        paused_at = datetime.fromisoformat(session["paused_at"])
        elapsed -= (now_local() - paused_at).total_seconds()

    return max(0.0, elapsed)


def haversine_distance_m(lat_a, lon_a, lat_b, lon_b):
    radius_m = 6371000.0

    lat_a_rad = math.radians(lat_a)
    lon_a_rad = math.radians(lon_a)
    lat_b_rad = math.radians(lat_b)
    lon_b_rad = math.radians(lon_b)

    delta_lat = lat_b_rad - lat_a_rad
    delta_lon = lon_b_rad - lon_a_rad

    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a_rad)
        * math.cos(lat_b_rad)
        * math.sin(delta_lon / 2.0) ** 2
    )

    return 2.0 * radius_m * math.asin(math.sqrt(value))


def local_xy_m(lat, lon, origin_lat, origin_lon):
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(origin_lat))

    return (
        (lon - origin_lon) * meters_per_deg_lon,
        (lat - origin_lat) * meters_per_deg_lat,
    )


def latlon_from_local_xy_m(x, y, origin_lat, origin_lon):
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(origin_lat))

    return (
        origin_lat + y / meters_per_deg_lat,
        origin_lon + x / meters_per_deg_lon,
    )


def point_to_segment_distance_m(point, segment_start, segment_end):
    origin_lat = (segment_start[0] + segment_end[0]) / 2.0
    origin_lon = (segment_start[1] + segment_end[1]) / 2.0

    px, py = local_xy_m(point[0], point[1], origin_lat, origin_lon)
    ax, ay = local_xy_m(
        segment_start[0], segment_start[1], origin_lat, origin_lon
    )
    bx, by = local_xy_m(
        segment_end[0], segment_end[1], origin_lat, origin_lon
    )

    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay

    length_squared = abx * abx + aby * aby

    if length_squared <= 0.000001:
        return math.hypot(px - ax, py - ay), 0.0

    fraction = (apx * abx + apy * aby) / length_squared
    clamped_fraction = max(0.0, min(1.0, fraction))

    closest_x = ax + clamped_fraction * abx
    closest_y = ay + clamped_fraction * aby

    return math.hypot(px - closest_x, py - closest_y), clamped_fraction


def point_in_polygon(lat, lon, polygon):
    """Ray casting: True se (lat, lon) è dentro il poligono."""
    if not polygon or len(polygon) < 3:
        return False

    inside = False
    n = len(polygon)
    j = n - 1

    for i in range(n):
        lat_i = float(polygon[i]["lat"])
        lon_i = float(polygon[i]["lon"])
        lat_j = float(polygon[j]["lat"])
        lon_j = float(polygon[j]["lon"])

        if (lat_i > lat) != (lat_j > lat):
            denominator = lat_j - lat_i
            if abs(denominator) < 1e-12:
                j = i
                continue
            intersection_lon = (
                (lon_j - lon_i) * (lat - lat_i) / denominator + lon_i
            )
            if lon < intersection_lon:
                inside = not inside

        j = i

    return inside


def distance_to_polygon_m(lat, lon, polygon):
    """Distanza minima dal punto al perimetro del poligono (0 se dentro)."""
    if not polygon or len(polygon) < 3:
        return None

    min_distance = float("inf")
    n = len(polygon)

    for i in range(n):
        a = (float(polygon[i]["lat"]), float(polygon[i]["lon"]))
        b = (
            float(polygon[(i + 1) % n]["lat"]),
            float(polygon[(i + 1) % n]["lon"]),
        )

        distance_m, _ = point_to_segment_distance_m((lat, lon), a, b)
        if distance_m < min_distance:
            min_distance = distance_m

    return min_distance


def extend_line_endpoints(point_a, point_b, extension_each_side_m):
    lat_a = float(point_a["lat"])
    lon_a = float(point_a["lon"])
    lat_b = float(point_b["lat"])
    lon_b = float(point_b["lon"])

    origin_lat = (lat_a + lat_b) / 2.0
    origin_lon = (lon_a + lon_b) / 2.0

    ax, ay = local_xy_m(lat_a, lon_a, origin_lat, origin_lon)
    bx, by = local_xy_m(lat_b, lon_b, origin_lat, origin_lon)

    dx = bx - ax
    dy = by - ay
    length = math.hypot(dx, dy)

    if length <= 0.001:
        return (lat_a, lon_a), (lat_b, lon_b)

    unit_x = dx / length
    unit_y = dy / length

    extension = max(0.0, safe_float(extension_each_side_m, 0.0))

    return (
        latlon_from_local_xy_m(
            ax - unit_x * extension,
            ay - unit_y * extension,
            origin_lat,
            origin_lon,
        ),
        latlon_from_local_xy_m(
            bx + unit_x * extension,
            by + unit_y * extension,
            origin_lat,
            origin_lon,
        ),
    )


def cross_2d(ax, ay, bx, by):
    return ax * by - ay * bx


def valid_coordinates(sample):
    latitude = safe_float(sample.get("latitude"), 0.0)
    longitude = safe_float(sample.get("longitude"), 0.0)

    return (
        -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
        and not (latitude == 0.0 and longitude == 0.0)
    )


def valid_timing_sample(sample):
    timing = TRACK_CONFIG.get("timing", {})

    return (
        valid_coordinates(sample)
        and sample.get("fix_valid") is True
        and safe_int(sample.get("fix_type"), 0)
        >= safe_int(timing.get("min_fix_type"), 3)
        and safe_int(sample.get("satellites_used"), 0)
        >= safe_int(timing.get("min_satellites"), 4)
        and safe_float(sample.get("hdop"), 999.0)
        <= safe_float(timing.get("max_hdop"), 3.5)
    )


def point_from_sample(sample):
    return (float(sample["latitude"]), float(sample["longitude"]))


def line_crossing_event(previous_sample, current_sample, line_config):
    if (
        not line_config
        or not line_config.get("a")
        or not line_config.get("b")
    ):
        return None

    if (
        not valid_timing_sample(previous_sample)
        or not valid_timing_sample(current_sample)
    ):
        return None

    line_a, line_b = extend_line_endpoints(
        line_config["a"],
        line_config["b"],
        safe_float(line_config.get("line_extension_each_side_m"), 16.0),
    )

    origin_lat = (line_a[0] + line_b[0]) / 2.0
    origin_lon = (line_a[1] + line_b[1]) / 2.0

    p0x, p0y = local_xy_m(
        float(previous_sample["latitude"]),
        float(previous_sample["longitude"]),
        origin_lat,
        origin_lon,
    )

    p1x, p1y = local_xy_m(
        float(current_sample["latitude"]),
        float(current_sample["longitude"]),
        origin_lat,
        origin_lon,
    )

    a0x, a0y = local_xy_m(line_a[0], line_a[1], origin_lat, origin_lon)
    a1x, a1y = local_xy_m(line_b[0], line_b[1], origin_lat, origin_lon)

    rx = p1x - p0x
    ry = p1y - p0y
    sx = a1x - a0x
    sy = a1y - a0y

    denominator = cross_2d(rx, ry, sx, sy)

    line_id = line_config.get("id", "?")

    if abs(denominator) < 1e-7:
        return None

    qpx = a0x - p0x
    qpy = a0y - p0y

    movement_fraction = cross_2d(qpx, qpy, sx, sy) / denominator
    line_fraction = cross_2d(qpx, qpy, rx, ry) / denominator

    # ---- DEBUG: log quando il movimento passa vicino ma fuori range ----
    if abs(denominator) > 1e-5:
        if (
            (-0.3 <= line_fraction <= 1.3)
            and (-0.3 <= movement_fraction <= 1.3)
        ):
            inside = (
                0.0 <= movement_fraction <= 1.0
                and 0.0 <= line_fraction <= 1.0
            )
            print(
                f"[LINE {line_id}] movement_fraction={movement_fraction:.3f} "
                f"line_fraction={line_fraction:.3f} inside={inside}"
            )
    # -------------------------------------------------------------------

    if not (0.0 <= movement_fraction <= 1.0 and 0.0 <= line_fraction <= 1.0):
        return None

    previous_epoch = float(previous_sample["received_at_epoch"])
    current_epoch = float(current_sample["received_at_epoch"])

    result = {
        "latitude": (
            float(previous_sample["latitude"])
            + movement_fraction
            * (
                float(current_sample["latitude"])
                - float(previous_sample["latitude"])
            )
        ),
        "longitude": (
            float(previous_sample["longitude"])
            + movement_fraction
            * (
                float(current_sample["longitude"])
                - float(previous_sample["longitude"])
            )
        ),
        "crossing_epoch": (
            previous_epoch
            + movement_fraction * (current_epoch - previous_epoch)
        ),
    }

    print(
        f"[LINE {line_id}] CROSS at "
        f"({result['latitude']:.7f}, {result['longitude']:.7f})"
    )

    return result


def sector_crossing_fallback(
    previous_sample, current_sample, line_config, tolerance_m
):
    """
    Fallback: se il kart passa entro `tolerance_m` dal segmento A-B
    (e si sta avvicinando), consideralo come attraversamento anche se
    la vera intersezione geometrica è mancata per jitter GPS.
    """
    if (
        not line_config
        or not line_config.get("a")
        or not line_config.get("b")
    ):
        return None

    if (
        not valid_timing_sample(previous_sample)
        or not valid_timing_sample(current_sample)
    ):
        return None

    if not direction_is_valid(previous_sample, current_sample, line_config):
        return None

    line_a = (
        float(line_config["a"]["lat"]),
        float(line_config["a"]["lon"]),
    )
    line_b = (
        float(line_config["b"]["lat"]),
        float(line_config["b"]["lon"]),
    )

    distance_curr, _ = point_to_segment_distance_m(
        (
            float(current_sample["latitude"]),
            float(current_sample["longitude"]),
        ),
        line_a,
        line_b,
    )
    distance_prev, _ = point_to_segment_distance_m(
        (
            float(previous_sample["latitude"]),
            float(previous_sample["longitude"]),
        ),
        line_a,
        line_b,
    )

    if distance_curr > tolerance_m:
        return None

    # consideralo attraversato solo se ci stiamo avvicinando
    # (distanza che cala)
    if distance_prev <= distance_curr:
        return None

    return {
        "latitude": float(current_sample["latitude"]),
        "longitude": float(current_sample["longitude"]),
        "crossing_epoch": float(current_sample["received_at_epoch"]),
    }


def bearing_between_points(point_a, point_b):
    lat_1 = math.radians(float(point_a["lat"]))
    lon_1 = math.radians(float(point_a["lon"]))
    lat_2 = math.radians(float(point_b["lat"]))
    lon_2 = math.radians(float(point_b["lon"]))

    delta_lon = lon_2 - lon_1

    x = math.sin(delta_lon) * math.cos(lat_2)
    y = (
        math.cos(lat_1) * math.sin(lat_2)
        - math.sin(lat_1) * math.cos(lat_2) * math.cos(delta_lon)
    )

    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def movement_bearing(previous_sample, current_sample):
    return bearing_between_points(
        {
            "lat": previous_sample["latitude"],
            "lon": previous_sample["longitude"],
        },
        {
            "lat": current_sample["latitude"],
            "lon": current_sample["longitude"],
        },
    )


def angular_difference_deg(angle_a, angle_b):
    return abs((angle_a - angle_b + 180.0) % 360.0 - 180.0)


def direction_is_valid(previous_sample, current_sample, line_config):
    reference = line_config.get("direction_reference", {})
    before = reference.get("before")
    after = reference.get("after")

    if not before or not after:
        return True

    expected_heading = bearing_between_points(before, after)
    actual_heading = movement_bearing(previous_sample, current_sample)

    return angular_difference_deg(expected_heading, actual_heading) <= 100.0


def pit_lane_points():
    pit_lane = TRACK_CONFIG.get("pit_lane", {})
    start = pit_lane.get("start")
    end = pit_lane.get("end")

    if (
        not start
        or not end
        or start.get("lat") is None
        or start.get("lon") is None
        or end.get("lat") is None
        or end.get("lon") is None
    ):
        return None, None

    return (
        (float(start["lat"]), float(start["lon"])),
        (float(end["lat"]), float(end["lon"])),
    )


def is_inside_box(sample):
    if not valid_coordinates(sample):
        return False, None

    pit_box = TRACK_CONFIG.get("pit_box", {})
    polygon = pit_box.get("polygon")

    lat = float(sample["latitude"])
    lon = float(sample["longitude"])

    # Caso 1: pit_box definito come poligono
    if polygon and len(polygon) >= 3:
        if point_in_polygon(lat, lon, polygon):
            return True, 0.0

        distance_m = distance_to_polygon_m(lat, lon, polygon)
        return False, distance_m

    # Caso 2 (fallback): pit_box definito come centro + raggio
    center = pit_box.get("center", {})

    if center.get("lat") is None or center.get("lon") is None:
        return False, None

    distance_m = haversine_distance_m(
        lat,
        lon,
        float(center["lat"]),
        float(center["lon"]),
    )

    radius_m = safe_float(pit_box.get("radius_m"), 15.0)

    return distance_m <= radius_m, distance_m


def pit_lane_position(sample):
    start, end = pit_lane_points()

    if start is None or end is None or not valid_coordinates(sample):
        return None

    distance_m, fraction = point_to_segment_distance_m(
        point_from_sample(sample), start, end
    )

    return {"distance_m": distance_m, "fraction": fraction}


def dashboard_location_flags(sample):
    inside_box, box_distance_m = is_inside_box(sample)

    if inside_box:
        return {
            "in_box": True,
            "in_pit_lane": False,
            "box_distance_m": box_distance_m,
            "pit_lane_distance_m": None,
            "pit_lane_fraction": None,
        }

    pit_position = pit_lane_position(sample)

    pit_lane_max_distance_m = safe_float(
        TRACK_CONFIG.get("pit_lane", {}).get("dashboard_active_distance_m"),
        20.0,
    )

    in_pit_lane = (
        pit_position is not None
        and pit_position["distance_m"] <= pit_lane_max_distance_m
    )

    return {
        "in_box": False,
        "in_pit_lane": in_pit_lane,
        "box_distance_m": box_distance_m,
        "pit_lane_distance_m": (
            pit_position["distance_m"] if pit_position is not None else None
        ),
        "pit_lane_fraction": (
            pit_position["fraction"] if pit_position is not None else None
        ),
    }


def is_moving_toward_pit_lane_start(previous_sample, current_sample):
    start, end = pit_lane_points()

    if (
        start is None
        or end is None
        or not valid_coordinates(previous_sample)
        or not valid_coordinates(current_sample)
    ):
        return False

    expected_heading = bearing_between_points(
        {"lat": end[0], "lon": end[1]},
        {"lat": start[0], "lon": start[1]},
    )

    actual_heading = movement_bearing(previous_sample, current_sample)

    return angular_difference_deg(expected_heading, actual_heading) <= 75.0


def entered_pit_lane(previous_sample, current_sample):
    previous_position = pit_lane_position(previous_sample)
    current_position = pit_lane_position(current_sample)

    if previous_position is None or current_position is None:
        return False, None

    if (
        previous_position["distance_m"] > 22.0
        and current_position["distance_m"] > 22.0
    ):
        return False, current_position

    if not is_moving_toward_pit_lane_start(previous_sample, current_sample):
        return False, current_position

    entered = (
        previous_position["fraction"] >= 0.78
        and current_position["fraction"] <= 0.78
    )

    return entered, current_position


# =============================================================
# Accumulo punti giro corrente
# =============================================================

def append_lap_point(sample, point_epoch=None):
    if not session.get("current_lap_timer_running"):
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None or not valid_timing_sample(sample):
        return

    epoch = (
        float(point_epoch)
        if point_epoch is not None
        else float(sample["received_at_epoch"])
    )

    lap_elapsed_s = epoch - float(lap_start_epoch)

    if lap_elapsed_s < -0.05:
        return

    point = _new_point_from_sample(sample, epoch, lap_elapsed_s)
    points = session["current_lap_track_points"]

    if points:
        previous = points[-1]

        distance_m = haversine_distance_m(
            previous["latitude"],
            previous["longitude"],
            point["latitude"],
            point["longitude"],
        )

        elapsed_s = point["lap_elapsed_s"] - previous["lap_elapsed_s"]

        if elapsed_s <= 0.0 or distance_m < 0.05:
            return

        session["current_lap_distance_m"] += distance_m

    point["lap_distance_m"] = round(session["current_lap_distance_m"], 4)
    points.append(point)

    point["delta_live_s"] = calculate_delta_live(epoch)

    session["session_track_point_count"] += 1


def append_crossing_point(crossing, crossing_epoch):
    if not session.get("current_lap_timer_running"):
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return

    lap_elapsed_s = float(crossing_epoch) - float(lap_start_epoch)

    if lap_elapsed_s < 0.0:
        return

    point = {
        "latitude": float(crossing["latitude"]),
        "longitude": float(crossing["longitude"]),
        "received_at_epoch": float(crossing_epoch),
        "lap_elapsed_s": lap_elapsed_s,
        "speed_kmph": 0.0,
        "satellites_used": None,
        "hdop": None,
        "temperature_c": None,
        "as5600_rpm": None,
        "ir_rpm": None,
        "delta_live_s": None,
        "lap_distance_m": 0.0,
    }

    points = session["current_lap_track_points"]

    if points:
        previous = points[-1]

        distance_m = haversine_distance_m(
            previous["latitude"],
            previous["longitude"],
            point["latitude"],
            point["longitude"],
        )

        if distance_m >= 0.05:
            session["current_lap_distance_m"] += distance_m

    point["lap_distance_m"] = round(session["current_lap_distance_m"], 4)

    if (
        not points
        or point["lap_elapsed_s"] > float(points[-1]["lap_elapsed_s"])
    ):
        points.append(point)
        point["delta_live_s"] = calculate_delta_live(crossing_epoch)
        session["session_track_point_count"] += 1


def append_warmup_point(sample):
    if not valid_coordinates(sample):
        return

    points = session["warmup_and_pitlane_raw"]

    if len(points) >= 20000:
        return

    if points:
        previous = points[-1]

        distance_m = haversine_distance_m(
            previous["latitude"],
            previous["longitude"],
            sample["latitude"],
            sample["longitude"],
        )

        elapsed_s = max(
            0.0,
            float(sample["received_at_epoch"])
            - float(previous["received_at_epoch"]),
        )

        speed_kmph = safe_float(sample.get("speed_kmph"), 0.0)

        stationary = safe_float(
            FILTER_CONFIG.get("stationary_speed_kmph"), 2.0
        )
        dup_dist = safe_float(
            FILTER_CONFIG.get("duplicate_distance_m"), 0.75
        )
        moving_min = safe_float(
            FILTER_CONFIG.get("moving_min_distance_m"), 1.5
        )
        keepalive_s = safe_float(
            FILTER_CONFIG.get("stationary_keepalive_s"), 5.0
        )
        max_gap_s = safe_float(
            FILTER_CONFIG.get("max_track_gap_s"), 1.0
        )

        if elapsed_s < max_gap_s:
            if speed_kmph < stationary:
                if elapsed_s < keepalive_s and distance_m < dup_dist:
                    return
            elif distance_m < moving_min:
                return

    points.append({
        "received_at_epoch": float(sample["received_at_epoch"]),
        "latitude": float(sample["latitude"]),
        "longitude": float(sample["longitude"]),
        "speed_kmph": safe_float(sample.get("speed_kmph"), 0.0),
        "satellites_used": safe_int(sample.get("satellites_used"), 0),
        "hdop": safe_float(sample.get("hdop"), None),
        "temperature_c": safe_float(sample.get("temperature_c"), None),
        "as5600_rpm": safe_float(sample.get("as5600_rpm"), None),
        "ir_rpm": safe_int(sample.get("ir_rpm"), None),
    })

    session["session_track_point_count"] += 1


def normalize_reference_lap_points(points):
    cleaned = []

    for point in points:
        distance_m = safe_float(point.get("lap_distance_m"), None)
        elapsed_s = safe_float(point.get("lap_elapsed_s"), None)

        if distance_m is None or elapsed_s is None:
            continue

        if cleaned:
            previous = cleaned[-1]

            if distance_m <= previous["lap_distance_m"]:
                continue

            if elapsed_s <= previous["lap_elapsed_s"]:
                continue

        cleaned.append({
            "latitude": safe_float(point.get("latitude")),
            "longitude": safe_float(point.get("longitude")),
            "lap_distance_m": round(distance_m, 4),
            "lap_elapsed_s": round(elapsed_s, 4),
        })

    return cleaned


def reference_time_at_distance(reference_points, target_distance_m):
    if len(reference_points) < 2:
        return None

    target_distance_m = max(0.0, float(target_distance_m))

    first = reference_points[0]
    last = reference_points[-1]

    if target_distance_m <= float(first["lap_distance_m"]):
        return float(first["lap_elapsed_s"])

    if target_distance_m >= float(last["lap_distance_m"]):
        return float(last["lap_elapsed_s"])

    for index in range(1, len(reference_points)):
        point_a = reference_points[index - 1]
        point_b = reference_points[index]

        distance_a = float(point_a["lap_distance_m"])
        distance_b = float(point_b["lap_distance_m"])

        if target_distance_m > distance_b:
            continue

        time_a = float(point_a["lap_elapsed_s"])
        time_b = float(point_b["lap_elapsed_s"])

        segment_distance_m = distance_b - distance_a

        if segment_distance_m <= 0.0001:
            return time_b

        fraction = (target_distance_m - distance_a) / segment_distance_m

        return time_a + fraction * (time_b - time_a)

    return float(last["lap_elapsed_s"])


def calculate_delta_live(now_epoch=None):
    if session.get("track_state") != "on_track":
        return None

    if not session.get("current_lap_timer_running"):
        return None

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return None

    reference_points = session.get("reference_lap_track_points", [])
    current_points = session.get("current_lap_track_points", [])

    if len(reference_points) < 2 or len(current_points) < 2:
        return None

    if now_epoch is None:
        now_epoch = time.time()

    last_point = current_points[-1]

    current_distance_m = safe_float(last_point.get("lap_distance_m"), 0.0)
    current_sample_time_s = safe_float(last_point.get("lap_elapsed_s"), 0.0)

    time_after_last_sample_s = max(
        0.0,
        float(now_epoch) - float(last_point["received_at_epoch"]),
    )
    time_after_last_sample_s = min(time_after_last_sample_s, 1.0)

    current_lap_time_s = current_sample_time_s + time_after_last_sample_s

    reference_time_s = reference_time_at_distance(
        reference_points, current_distance_m
    )

    if reference_time_s is None:
        return None

    delta_s = current_lap_time_s - reference_time_s

    if not math.isfinite(delta_s) or abs(delta_s) > 60.0:
        return None

    return round(delta_s, 3)


def update_delta_live(sample=None):
    sample_epoch = None

    if sample is not None:
        sample_epoch = sample.get("received_at_epoch")

    session["delta_live_s"] = calculate_delta_live(sample_epoch)


# =============================================================
# Chiusura / aborto giro
# =============================================================

def abort_current_lap(reason, at_epoch):
    if (
        not session["current_lap_timer_running"]
        or session.get("current_lap_start_epoch") is None
    ):
        return

    lap_number = session.get("current_lap_number", 0)
    elapsed_s = max(
        0.0,
        float(at_epoch) - float(session["current_lap_start_epoch"]),
    )

    append_event(
        "lap_aborted",
        lap_number=lap_number,
        elapsed_s=round(elapsed_s, 3),
        reason=reason,
        sectors_s=deepcopy(session.get("current_lap_sectors_s", [])),
    )

    session["laps"].append({
        "lap_number": lap_number,
        "lap_time_s": round(elapsed_s, 3),
        "valid": False,
        "aborted_reason": reason,
        "sectors_s": deepcopy(session.get("current_lap_sectors_s", [])),
        "started_at_epoch": session["current_lap_start_epoch"],
        "ended_at_epoch": float(at_epoch),
        "lap_distance_m": round(session["current_lap_distance_m"], 2),
        "events": list(session["current_lap_events"]),
        "track_points": columnar(
            session["current_lap_track_points"], TRACK_POINT_FIELDS
        ),
    })

    session["last_aborted_lap"] = {
        "lap_number": lap_number,
        "elapsed_s": round(elapsed_s, 3),
        "reason": reason,
        "sectors_s": deepcopy(session.get("current_lap_sectors_s", [])),
    }

    session["current_lap_timer_running"] = False
    session["current_lap_start_epoch"] = None
    session["sector_started_epoch"] = None
    session["current_sector_number"] = 1
    session["current_lap_track_points"] = []
    session["current_lap_events"] = []
    session["current_lap_distance_m"] = 0.0
    session["delta_live_s"] = None
    session["current_lap_sector_statuses"] = [None] * sector_count()


def stop_timing_for_cooldown(at_epoch, sample=None):
    if session.get("current_lap_timer_running"):
        abort_current_lap("cooldown_requested", at_epoch)

    session["previous_track_state"] = session.get("track_state")
    session["cooldown_active"] = True
    session["track_state"] = "cooldown"
    session["current_lap_timer_running"] = False
    session["current_lap_start_epoch"] = None
    session["sector_started_epoch"] = None
    session["current_sector_number"] = 1
    session["current_lap_sectors_s"] = [None] * sector_count()
    session["current_lap_sector_statuses"] = [None] * sector_count()
    session["current_lap_track_points"] = []
    session["current_lap_events"] = []
    session["current_lap_distance_m"] = 0.0
    session["delta_live_s"] = None

    event_data = {"reason": "mqtt_cooldown"}

    if sample and valid_coordinates(sample):
        event_data.update(
            latitude=round(float(sample["latitude"]), 7),
            longitude=round(float(sample["longitude"]), 7),
        )

    append_event("cooldown_started", **event_data)


def resume_timing_after_cooldown(sample):
    session["cooldown_active"] = False
    session["track_state"] = "on_track"
    session["previous_track_state"] = "cooldown"
    session["warmup_started_at"] = None
    session["pit_lane_exit_seen"] = True
    session["current_sector_number"] = 1

    append_event(
        "cooldown_ended",
        reason="crossed_start_finish",
        latitude=round(float(sample["latitude"]), 7),
        longitude=round(float(sample["longitude"]), 7),
    )


def set_track_state(next_state, reason=None, sample=None):
    previous_state = session.get("track_state")

    if previous_state == next_state:
        return

    session["previous_track_state"] = previous_state
    session["track_state"] = next_state

    event_data = {
        "from_state": previous_state,
        "to_state": next_state,
    }

    if reason:
        event_data["reason"] = reason

    if sample and valid_coordinates(sample):
        event_data.update(
            latitude=round(float(sample["latitude"]), 7),
            longitude=round(float(sample["longitude"]), 7),
        )

    append_event("track_state_changed", **event_data)


def update_position_state(previous_sample, sample):
    if session.get("cooldown_active"):
        return "cooldown"

    inside_box, box_distance_m = is_inside_box(sample)
    current_state = session.get("track_state", "pitlane")

    if inside_box:
        if current_state != "box":
            abort_current_lap("entered_box", sample["received_at_epoch"])

            set_track_state("box", "inside_pit_box", sample)

            append_event(
                "box_entered",
                distance_from_box_m=round(box_distance_m, 2)
                if box_distance_m is not None
                else None,
            )

        return "box"

    pit_box = TRACK_CONFIG.get("pit_box", {})
    hysteresis_m = safe_float(pit_box.get("exit_hysteresis_m"), 4.0)
    has_polygon = bool(pit_box.get("polygon"))

    if current_state == "box" and box_distance_m is not None:
        if has_polygon:
            still_inside = box_distance_m <= hysteresis_m
        else:
            radius_m = safe_float(pit_box.get("radius_m"), 15.0)
            still_inside = box_distance_m < radius_m + hysteresis_m

        if still_inside:
            return "box"

        set_track_state("pitlane", "left_pit_box", sample)

        append_event(
            "pit_lane_entered",
            distance_from_box_m=round(box_distance_m, 2),
        )

        session["pit_lane_exit_seen"] = False
        session["pit_lane_entry_detected_at_epoch"] = None
        session["current_sector_number"] = 1

        return "pitlane"

    if current_state == "on_track":
        detected, position = entered_pit_lane(previous_sample, sample)

        if detected:
            abort_current_lap(
                "entered_pit_lane", sample["received_at_epoch"]
            )

            set_track_state("pitlane", "entered_pit_lane", sample)

            session["pit_lane_exit_seen"] = False
            session["pit_lane_entry_detected_at_epoch"] = (
                sample["received_at_epoch"]
            )
            session["current_sector_number"] = 1

            append_event(
                "pit_lane_entry_detected",
                distance_from_pit_lane_centerline_m=round(
                    position["distance_m"], 2
                ),
                position_on_pit_lane=round(position["fraction"], 3),
            )

            return "pitlane"

    return current_state


def mark_warmup(sample, reason):
    current_state = session.get("track_state", "pitlane")

    if current_state in {"box", "pitlane"}:
        set_track_state("warmup", reason, sample)

        session["warmup_started_at"] = sample["received_at"]
        session["pit_lane_exit_seen"] = True
        session["current_sector_number"] = 1

        append_event("warmup_started", reason=reason)


def start_new_lap(crossing_epoch, lap_number):
    session["current_lap_number"] = lap_number
    session["current_lap_start_epoch"] = crossing_epoch
    session["sector_started_epoch"] = crossing_epoch
    session["current_sector_number"] = 1
    session["current_lap_sectors_s"] = [None] * sector_count()
    session["current_lap_sector_statuses"] = [None] * sector_count()
    session["current_lap_timer_running"] = True
    session["current_lap_track_points"] = []
    session["current_lap_events"] = []
    session["current_lap_distance_m"] = 0.0

    if not session.get("reference_lap_track_points"):
        session["delta_live_s"] = None

    append_event(
        "lap_started",
        lap_number=lap_number,
        sector_number=1,
        reference_lap_number=session.get("reference_lap_number"),
    )


def ideal_lap_time_s():
    values = session.get("best_sector_times_s", [])

    if not values or any(value is None for value in values):
        return None

    return round(sum(float(value) for value in values), 3)


def build_sector_result(
    sector_number, sector_time_s, best_sector_s, reference_sector_s
):
    if best_sector_s is None:
        return {
            "sector_number": sector_number,
            "sector_time_s": round(sector_time_s, 3),
            "delta_to_best_s": None,
            "delta_to_reference_s": None,
            "status": "best",
            "label": "BEST SETTORE",
            "completed_at_epoch": time.time(),
        }

    delta_to_best = sector_time_s - float(best_sector_s)

    if delta_to_best < -0.001:
        return {
            "sector_number": sector_number,
            "sector_time_s": round(sector_time_s, 3),
            "delta_to_best_s": round(delta_to_best, 3),
            "delta_to_reference_s": None,
            "status": "best",
            "label": "BEST SETTORE",
            "completed_at_epoch": time.time(),
        }

    if reference_sector_s is not None:
        delta_to_reference = sector_time_s - float(reference_sector_s)

        if delta_to_reference < -0.001:
            return {
                "sector_number": sector_number,
                "sector_time_s": round(sector_time_s, 3),
                "delta_to_best_s": round(delta_to_best, 3),
                "delta_to_reference_s": round(delta_to_reference, 3),
                "status": "improved",
                "label": "MIGLIORATO",
                "completed_at_epoch": time.time(),
            }
        else:
            return {
                "sector_number": sector_number,
                "sector_time_s": round(sector_time_s, 3),
                "delta_to_best_s": round(delta_to_best, 3),
                "delta_to_reference_s": round(delta_to_reference, 3),
                "status": "slower",
                "label": "PIÙ LENTO",
                "completed_at_epoch": time.time(),
            }

    return {
        "sector_number": sector_number,
        "sector_time_s": round(sector_time_s, 3),
        "delta_to_best_s": round(delta_to_best, 3),
        "delta_to_reference_s": None,
        "status": "slower",
        "label": "PIÙ LENTO",
        "completed_at_epoch": time.time(),
    }


def close_current_sector(crossing_epoch, completed_sector_number):
    if (
        not session["current_lap_timer_running"]
        or session.get("current_sector_number") != completed_sector_number
        or session.get("sector_started_epoch") is None
    ):
        return False

    sector_time_s = max(
        0.0,
        float(crossing_epoch) - float(session["sector_started_epoch"]),
    )

    values = list(session.get("current_lap_sectors_s", []))

    while len(values) < sector_count():
        values.append(None)

    best_sector_s = session["best_sector_times_s"][
        completed_sector_number - 1
    ]

    reference_sector_s = session["last_completed_lap_sectors_s"][
        completed_sector_number - 1
    ]

    is_new_best = (
        best_sector_s is None
        or sector_time_s < float(best_sector_s) - 0.001
    )

    result = build_sector_result(
        completed_sector_number,
        sector_time_s,
        best_sector_s,
        reference_sector_s,
    )

    values[completed_sector_number - 1] = round(sector_time_s, 3)

    session["current_lap_sectors_s"] = values
    session["current_lap_sector_statuses"][
        completed_sector_number - 1
    ] = result["status"]

    if is_new_best:
        session["best_sector_times_s"][
            completed_sector_number - 1
        ] = round(sector_time_s, 3)

        session["ideal_lap_time_s"] = ideal_lap_time_s()

    session["sector_started_epoch"] = crossing_epoch
    session["last_sector_result"] = result
    session["sector_event_id"] += 1
    result["id"] = session["sector_event_id"]

    append_event(
        "sector_completed",
        sector_number=completed_sector_number,
        sector_time_s=round(sector_time_s, 3),
        best_sector_s=best_sector_s,
        reference_sector_s=reference_sector_s,
        delta_to_best_s=result["delta_to_best_s"],
        delta_to_reference_s=result["delta_to_reference_s"],
        status=result["status"],
        is_new_best=is_new_best,
    )

    return True


def update_reference_lap(lap_number, lap_time_s, lap_points, lap_is_valid):
    if not lap_is_valid:
        return

    normalized_points = normalize_reference_lap_points(lap_points)

    if len(normalized_points) < 2:
        append_event(
            "reference_lap_ignored",
            lap_number=lap_number,
            reason="not_enough_reference_points",
        )
        return

    previous_reference_time = session.get("reference_lap_time_s")

    if (
        previous_reference_time is None
        or float(lap_time_s) < float(previous_reference_time)
    ):
        session["reference_lap_track_points"] = normalized_points
        session["reference_lap_time_s"] = round(lap_time_s, 3)
        session["reference_lap_number"] = lap_number

        append_event(
            "reference_lap_updated",
            lap_number=lap_number,
            lap_time_s=round(lap_time_s, 3),
            points=len(normalized_points),
        )


def finish_current_lap(crossing_epoch, finish_crossing):
    if not session["current_lap_timer_running"]:
        return

    expected_final_sector = sector_count()
    current_sector = session.get("current_sector_number") or 1

    lap_start_epoch = session.get("current_lap_start_epoch")
    if lap_start_epoch is None:
        return

    lap_time_s = float(crossing_epoch) - float(lap_start_epoch)

    minimum_lap_time_s = safe_float(
        TRACK_CONFIG.get("timing", {}).get("minimum_lap_time_s"),
        30.0,
    )

    if lap_time_s < minimum_lap_time_s:
        append_event(
            "finish_cross_ignored",
            reason="lap_too_short",
            lap_time_s=round(lap_time_s, 3),
            minimum_lap_time_s=minimum_lap_time_s,
        )
        return

    append_crossing_point(finish_crossing, crossing_epoch)

    missing_sectors = False

    if current_sector != expected_final_sector:
        missing_sectors = True

        append_event(
            "lap_completed_with_missing_sectors",
            current_sector_number=current_sector,
            expected_sector_number=expected_final_sector,
        )

        # marchiamo come "incomplete" il settore corrente e tutti
        # quelli dopo di lui (che non sono stati tagliati)
        statuses = list(session.get("current_lap_sector_statuses", []))
        while len(statuses) < expected_final_sector:
            statuses.append(None)
        for i in range(current_sector - 1, len(statuses)):
            if statuses[i] is None:
                statuses[i] = "incomplete"
        session["current_lap_sector_statuses"] = statuses
    else:
        if not close_current_sector(crossing_epoch, expected_final_sector):
            missing_sectors = True

    lap_number = session.get("current_lap_number", 0)
    sectors_s = list(session["current_lap_sectors_s"])

    statuses_for_validity = list(
        session.get("current_lap_sector_statuses", [])
    )
    while len(statuses_for_validity) < expected_final_sector:
        statuses_for_validity.append(None)

    lap_is_valid = (
        not missing_sectors
        and all(value is not None for value in sectors_s)
        and all(
            s not in (None, "incomplete")
            for s in statuses_for_validity
        )
    )

    append_event(
        "lap_completed",
        lap_number=lap_number,
        lap_time_s=round(lap_time_s, 3),
        valid=lap_is_valid,
        sectors_s=sectors_s,
        track_points=len(session["current_lap_track_points"]),
        lap_distance_m=round(session["current_lap_distance_m"], 2),
        missing_sectors=missing_sectors,
    )

    lap_data = {
        "lap_number": lap_number,
        "lap_time_s": round(lap_time_s, 3),
        "valid": lap_is_valid,
        "aborted_reason": "missing_sectors" if missing_sectors else None,
        "sectors_s": sectors_s,
        "started_at_epoch": lap_start_epoch,
        "ended_at_epoch": crossing_epoch,
        "lap_distance_m": round(session["current_lap_distance_m"], 2),
        "events": list(session["current_lap_events"]),
        "track_points": columnar(
            session["current_lap_track_points"], TRACK_POINT_FIELDS
        ),
    }

    session["laps"].append(lap_data)
    session["last_completed_lap_time_s"] = lap_data["lap_time_s"]
    session["last_completed_lap_at_epoch"] = time.time()
    session["last_completed_lap_sectors_s"] = list(sectors_s)

    if lap_is_valid:
        best_lap = session.get("best_lap_time_s")

        if best_lap is None or lap_time_s < float(best_lap):
            session["best_lap_time_s"] = round(lap_time_s, 3)

        update_reference_lap(
            lap_number=lap_number,
            lap_time_s=lap_time_s,
            lap_points=deepcopy(session["current_lap_track_points"]),
            lap_is_valid=lap_is_valid,
        )

    start_new_lap(crossing_epoch, lap_number + 1)


def crossing_allowed(line_id, crossing_epoch):
    cooldown_s = safe_float(
        TRACK_CONFIG.get("timing", {}).get("crossing_cooldown_s"), 12.0
    )

    crossings = session.setdefault("last_line_crossing_epochs", {})

    previous_epoch = crossings.get(line_id)

    if (
        previous_epoch is not None
        and float(crossing_epoch) - float(previous_epoch) < cooldown_s
    ):
        return False

    crossings[line_id] = crossing_epoch
    return True


def advance_warmup_sector(completed_sector_number):
    total_sectors = sector_count()
    next_sector = completed_sector_number + 1

    if next_sector > total_sectors:
        next_sector = 1

    session["current_sector_number"] = next_sector


def process_timing(sample, previous):
    if previous is None:
        return

    state = update_position_state(previous, sample)

    if (
        state == "box"
        or not valid_timing_sample(previous)
        or not valid_timing_sample(sample)
    ):
        return

    default_min_speed = safe_float(
        TRACK_CONFIG.get("timing", {}).get("crossing_minimum_speed_kmph"),
        5.0,
    )
    current_speed = safe_float(sample.get("speed_kmph"), 0.0)

    sectors = TRACK_CONFIG.get("sectors", [])
    start_finish = TRACK_CONFIG.get("start_finish", {})

    if not start_finish:
        return

    if state != "cooldown":
        pit_position = pit_lane_position(sample)

        if (
            state == "pitlane"
            and pit_position is not None
            and pit_position["distance_m"] <= 20.0
            and pit_position["fraction"] >= 0.78
        ):
            mark_warmup(sample, "reached_pit_lane_end")

        for split_index, sector_config in enumerate(sectors):
            line_id = sector_config.get("id", f"S{split_index + 1}")
            completed_sector_number = split_index + 1

            # ---- speed check per-settore (override del default globale) ----
            sector_min_speed = safe_float(
                sector_config.get("crossing_minimum_speed_kmph"),
                default_min_speed,
            )
            if current_speed < sector_min_speed:
                continue

            crossing = line_crossing_event(previous, sample, sector_config)

            # ---- fallback per prossimità (S2 in curva, velocità bassa) ----
            if crossing is None:
                tolerance_m = safe_float(
                    sector_config.get("proximity_tolerance_m"),
                    0.0,
                )

                if tolerance_m > 0.0:
                    crossing = sector_crossing_fallback(
                        previous,
                        sample,
                        sector_config,
                        tolerance_m,
                    )

                    if crossing is not None:
                        print(
                            f"[SECTOR] {line_id} crossed via proximity "
                            f"fallback (tol={tolerance_m:.1f} m)"
                        )

            if crossing is None:
                continue

            crossing_epoch = float(crossing["crossing_epoch"])

            if not crossing_allowed(line_id, crossing_epoch):
                continue

            current_state = session.get("track_state")

            if current_state in {"pitlane", "box"}:
                mark_warmup(sample, f"crossed_{line_id}")
                return

            if current_state == "warmup":
                advance_warmup_sector(completed_sector_number)
                return

            if current_state == "on_track":
                expected_sector_number = session.get("current_sector_number")

                if expected_sector_number != completed_sector_number:
                    append_event(
                        "sector_cross_ignored",
                        crossed_split=line_id,
                        expected_sector_number=expected_sector_number,
                    )
                    # ⚠️ FIX: era `return`, così usciva dal loop e perdeva S2.
                    continue

                append_crossing_point(crossing, crossing_epoch)

                if close_current_sector(
                    crossing_epoch,
                    completed_sector_number,
                ):
                    if completed_sector_number < sector_count():
                        session["current_sector_number"] = (
                            completed_sector_number + 1
                        )

                # ⚠️ FIX: era `return`, così poteva saltare altri settori.
                continue

    # ---------------- START / FINISH ----------------
    finish_crossing = line_crossing_event(previous, sample, start_finish)

    if finish_crossing is None:
        return

    if not direction_is_valid(previous, sample, start_finish):
        append_event("finish_cross_ignored", reason="wrong_direction")
        return

    crossing_epoch = float(finish_crossing["crossing_epoch"])
    current_state = session.get("track_state")

    # ---- speed check SF: solo quando stiamo chiudendo un giro ----
    if current_state == "on_track":
        sf_min_speed = safe_float(
            start_finish.get("crossing_minimum_speed_kmph"),
            default_min_speed,
        )
        if current_speed < sf_min_speed:
            return

    if current_state == "cooldown":
        crossings = session.setdefault("last_line_crossing_epochs", {})
        crossings["SF"] = crossing_epoch

        resume_timing_after_cooldown(sample)

        start_new_lap(
            crossing_epoch, session.get("current_lap_number", 0) + 1
        )
        return

    if not crossing_allowed("SF", crossing_epoch):
        return

    if current_state == "warmup":
        session["current_sector_number"] = 1

    if current_state in {"pitlane", "warmup"}:
        set_track_state("on_track", "crossed_start_finish", sample)

        session["warmup_started_at"] = None

        start_new_lap(crossing_epoch, 1)
        return

    if current_state == "on_track":
        finish_current_lap(crossing_epoch, finish_crossing)


def normalise_payload(payload):
    return {
        "latitude": safe_float(payload.get("latitude")),
        "longitude": safe_float(payload.get("longitude")),
        "speed_kmph": safe_float(payload.get("speed_kmph")),
        "satellites_used": safe_int(payload.get("satellites_used")),
        "fix_valid": bool(payload.get("fix_valid", False)),
        "fix_type": safe_int(payload.get("fix_type")),
        "hdop": safe_float(payload.get("hdop"), None),
        "temperature_c": safe_float(payload.get("temperature_c"), None),
        "as5600_angle_deg": safe_float(
            payload.get("as5600_angle_deg"), None
        ),
        "as5600_rpm": safe_float(payload.get("as5600_rpm"), None),
        "as5600_magnet_ok": bool(payload.get("as5600_magnet_ok", False)),
        "ir_rpm": safe_int(payload.get("ir_rpm"), None),
        "ir_total_pulses": safe_int(payload.get("ir_total_pulses"), None),
    }


def serializable_session():
    current_lap_in_progress = None

    if session.get("current_lap_timer_running"):
        current_lap_in_progress = {
            "lap_number": session["current_lap_number"],
            "started_at_epoch": session["current_lap_start_epoch"],
            "current_sector_number": session["current_sector_number"],
            "current_lap_distance_m": round(
                session["current_lap_distance_m"], 3
            ),
            "current_lap_sectors_s": deepcopy(
                session["current_lap_sectors_s"]
            ),
            "events": list(session["current_lap_events"]),
            "track_points": columnar(
                session["current_lap_track_points"], TRACK_POINT_FIELDS
            ),
        }

    laps_completed = sum(1 for lap in session["laps"] if lap.get("valid"))
    laps_aborted = sum(1 for lap in session["laps"] if not lap.get("valid"))

    raw_count = session.get("session_sample_count", 0)
    tp_count = session.get("session_track_point_count", 0)

    return {
        "schema_version": 13,
        "saved_at": now_iso(),
        "status": session["status"],
        "session_guid": session.get("session_guid"),
        "driver": deepcopy(session["driver"]),
        "track": deepcopy(TRACK_CONFIG.get("track", {})),
        "track_config": deepcopy(TRACK_CONFIG),
        "track_id": current_track_id,
        "mqtt_topic": session.get("mqtt_topic"),
        "started_at": session["started_at"],
        "paused_at": session["paused_at"],
        "paused_total_s": round(session["paused_total_s"], 3),
        "ended_at": session["ended_at"],
        "data_age_at_start_s": session["data_age_at_start_s"],
        "data_age_samples": columnar(
            session["data_age_samples"], ["at_epoch", "data_age_s"]
        ),
        "session_elapsed_s": round(session_elapsed_s(), 3),
        "summary": {
            "raw_samples_received": raw_count,
            "track_points_saved": tp_count,
            "valid_gps_samples": session.get("session_valid_gps_count", 0),
            "filtered_out_samples": max(0, raw_count - tp_count),
            "laps_completed": laps_completed,
            "laps_aborted": laps_aborted,
            "data_age_samples_recorded": len(session["data_age_samples"]),
        },
        "best_lap_time_s": session["best_lap_time_s"],
        "best_sector_times_s": deepcopy(session["best_sector_times_s"]),
        "ideal_lap_time_s": session["ideal_lap_time_s"],
        "reference_lap_number": session["reference_lap_number"],
        "reference_lap_time_s": session["reference_lap_time_s"],
        "laps": deepcopy(session["laps"]),
        "current_lap_in_progress": current_lap_in_progress,
        "warmup_and_pitlane_raw": columnar(
            session.get("warmup_and_pitlane_raw", []), WARMUP_POINT_FIELDS
        ),
        "track_state": session["track_state"],
        "cooldown_active": session["cooldown_active"],
        "delta_live_s": session["delta_live_s"],
        "last_sector_result": deepcopy(session["last_sector_result"]),
        "last_aborted_lap": deepcopy(session["last_aborted_lap"]),
    }


def write_json_atomic(path, data):
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    temporary_path.replace(path)


def autosave_if_due(force=False):
    global last_autosave_epoch

    if session["status"] not in {"running", "paused"}:
        return

    now_epoch = time.time()

    interval_s = safe_float(
        SESSION_CONFIG.get("autosave_interval_s"), 10.0
    )

    if not force and now_epoch - last_autosave_epoch < interval_s:
        return

    write_json_atomic(AUTOSAVE_PATH, serializable_session())

    last_autosave_epoch = now_epoch


last_autosave_epoch = 0.0


def data_age_sampling_loop():
    while True:
        tick_started = time.monotonic()
        with lock:
            if session.get("status") == "running":
                now_epoch = time.time()
                last_message_epoch = runtime.get(
                    "mqtt_last_message_epoch", 0.0
                )
                data_age_s = (
                    round(now_epoch - last_message_epoch, 3)
                    if last_message_epoch
                    else None
                )
                session["data_age_samples"].append({
                    "at_epoch": now_epoch,
                    "data_age_s": data_age_s,
                })
                try:
                    autosave_if_due()
                except Exception as error:
                    runtime["last_error"] = (
                        f"Session autosave failed: {error}"
                    )
                    print(f"[SESSION] autosave failed: {error}")

        remaining = DATA_AGE_SAMPLE_INTERVAL_S - (
            time.monotonic() - tick_started
        )
        if remaining > 0:
            time.sleep(remaining)


def reset_session():
    global session

    session = new_session_state()

    if AUTOSAVE_PATH.exists():
        AUTOSAVE_PATH.unlink()

    if EVENTS_PATH.exists():
        EVENTS_PATH.unlink()


def start_recording_session(driver, source="mqtt"):
    session["events"] = []
    session["warmup_and_pitlane_raw"] = []
    session["session_sample_count"] = 0
    session["session_valid_gps_count"] = 0
    session["session_track_point_count"] = 0
    session["data_age_samples"] = []
    session["previous_timing_sample"] = None
    session["mqtt_topic"] = None

    session["status"] = "running"
    session["session_guid"] = str(uuid.uuid4())
    session["driver"] = deepcopy(driver)
    start_epoch = time.time()
    session["started_at"] = now_iso()
    session["paused_at"] = None
    session["paused_total_s"] = 0.0
    session["ended_at"] = None
    last_message_epoch = runtime.get("mqtt_last_message_epoch", 0.0)
    session["data_age_at_start_s"] = (
        round(start_epoch - last_message_epoch, 3)
        if last_message_epoch
        else None
    )
    if session["data_age_at_start_s"] is not None:
        session["data_age_samples"].append({
            "at_epoch": start_epoch,
            "data_age_s": session["data_age_at_start_s"],
        })

    try:
        if EVENTS_PATH.exists():
            EVENTS_PATH.unlink()
    except OSError as error:
        print(f"[SESSION] Could not delete events file: {error}")

    try:
        append_event(
            "session_started",
            session_guid=session["session_guid"],
            driver_id=driver["id"],
            driver_name=driver["name"],
            track_id=current_track_id,
            source=source,
        )
    except Exception as error:
        print(f"[SESSION] append_event('session_started') failed: {error}")

    publish_session_control("session_started")

    try:
        autosave_if_due(force=True)
    except Exception as error:
        print(f"[SESSION] autosave failed: {error}")


def stop_recording_session(reason=None):
    if session["status"] not in ("running", "paused"):
        return None

    if session["status"] == "paused" and session["paused_at"]:
        try:
            paused_at = datetime.fromisoformat(session["paused_at"])
            session["paused_total_s"] += (
                now_local() - paused_at
            ).total_seconds()
        except Exception as error:
            print(f"[SESSION] pause accounting failed: {error}")
        session["paused_at"] = None

    session["ended_at"] = now_iso()
    session["status"] = "stopped"
    publish_session_control("session_stopped")

    try:
        if reason:
            append_event("session_stopped", reason=reason)
        else:
            append_event("session_stopped")
    except Exception as error:
        print(f"[SESSION] append_event('session_stopped') failed: {error}")

    final_path = None

    try:
        final_data = serializable_session()

        driver_id = (
            session["driver"]["id"] if session["driver"] else "unknown"
        )

        track_id = (
            TRACK_CONFIG.get("track", {}).get("id")
            or current_track_id
            or "unknown-track"
        )

        stamp = now_local().strftime("%Y-%m-%d_%H-%M-%S")

        final_path = SESSIONS_DIR / (
            f"{stamp}_{driver_id}_{track_id}_"
            f"{session['session_guid']}.json"
        )

        write_json_atomic(final_path, final_data)
        write_json_atomic(AUTOSAVE_PATH, final_data)
    except Exception as error:
        print(f"[SESSION] final save failed: {error}")

    return final_path


def get_location_status(gps):
    flags = dashboard_location_flags(gps)

    if flags["in_box"]:
        status = "box"
    elif flags["in_pit_lane"]:
        status = "pit_lane"
    else:
        status = "track"

    return {
        "status": status,
        "in_box": flags["in_box"],
        "in_pit_lane": flags["in_pit_lane"],
        "box_distance_m": (
            round(flags["box_distance_m"], 2)
            if flags["box_distance_m"] is not None
            else None
        ),
        "distance_from_pit_lane_centerline_m": (
            round(flags["pit_lane_distance_m"], 2)
            if flags["pit_lane_distance_m"] is not None
            else None
        ),
        "pit_lane_fraction": (
            round(flags["pit_lane_fraction"], 3)
            if flags["pit_lane_fraction"] is not None
            else None
        ),
        "box_radius_m": safe_float(
            TRACK_CONFIG.get("pit_box", {}).get("radius_m"), 15.0
        ),
    }


def live_snapshot():
    with lock:
        gps = deepcopy(latest_sensors)
        runtime_snapshot = deepcopy(runtime)

        last_message_epoch = runtime_snapshot.get(
            "mqtt_last_message_epoch", 0.0
        )

        runtime_snapshot["data_age_s"] = (
            round(time.time() - last_message_epoch, 3)
            if last_message_epoch
            else None
        )

        now_epoch = time.time()

        current_lap_time_s = None
        current_sector_elapsed_s = None

        if (
            session.get("current_lap_timer_running")
            and session.get("current_lap_start_epoch") is not None
        ):
            current_lap_time_s = round(
                now_epoch - float(session["current_lap_start_epoch"]), 3
            )

        if (
            session.get("current_lap_timer_running")
            and session.get("sector_started_epoch") is not None
            and session.get("current_sector_number") is not None
        ):
            current_sector_elapsed_s = round(
                now_epoch - float(session["sector_started_epoch"]), 3
            )

        ideal_time = session.get("ideal_lap_time_s") or ideal_lap_time_s()

        delta_live_s = calculate_delta_live(now_epoch)
        session["delta_live_s"] = delta_live_s

        display_lap_time_s = current_lap_time_s

        if (
            session.get("last_completed_lap_at_epoch") is not None
            and session.get("last_completed_lap_time_s") is not None
            and now_epoch - float(session["last_completed_lap_at_epoch"])
            < 1.5
        ):
            display_lap_time_s = session["last_completed_lap_time_s"]

        last_lap = session["laps"][-1] if session["laps"] else None

        return {
            "gps": gps,
            "location": get_location_status(gps),
            "runtime": runtime_snapshot,
            "session": {
                "status": session["status"],
                "driver": deepcopy(session["driver"]),
                "started_at": session["started_at"],
                "paused_at": session["paused_at"],
                "session_elapsed_s": round(session_elapsed_s(), 3),
                "samples_recorded": session.get("session_sample_count", 0),
                "track_points_recorded": session.get(
                    "session_track_point_count", 0
                ),
                "track_state": session["track_state"],
                "cooldown_active": session["cooldown_active"],
                "current_lap_number": session["current_lap_number"],
                "current_lap_time_s": current_lap_time_s,
                "display_lap_time_s": display_lap_time_s,
                "current_lap_timer_running": session[
                    "current_lap_timer_running"
                ],
                "current_sector_number": session["current_sector_number"],
                "current_sector_elapsed_s": current_sector_elapsed_s,
                "current_lap_sectors_s": deepcopy(
                    session["current_lap_sectors_s"]
                ),
                "current_lap_sector_statuses": deepcopy(
                    session.get("current_lap_sector_statuses", [])
                ),
                "current_lap_distance_m": round(
                    session["current_lap_distance_m"], 3
                ),
                "best_sector_times_s": deepcopy(
                    session["best_sector_times_s"]
                ),
                "ideal_lap_time_s": ideal_time,
                "delta_live_s": delta_live_s,
                "reference_lap_number": session["reference_lap_number"],
                "reference_lap_time_s": session["reference_lap_time_s"],
                "reference_lap_points": len(
                    session["reference_lap_track_points"]
                ),
                "last_sector_result": deepcopy(
                    session["last_sector_result"]
                ),
                "last_lap": deepcopy(last_lap),
                "last_aborted_lap": deepcopy(session["last_aborted_lap"]),
                "best_lap_time_s": session["best_lap_time_s"],
                "last_completed_lap_time_s": session[
                    "last_completed_lap_time_s"
                ],
                "warmup_started_at": session["warmup_started_at"],
            },
            "track": deepcopy(TRACK_CONFIG.get("track", {})),
            "track_id": current_track_id,
            "dashboard": deepcopy(DASHBOARD_CONFIG),
        }


def watchdog_loop():
    while True:
        time.sleep(1)

        now_epoch = time.time()

        with lock:
            for component, timestamp in last_seen.items():
                if (
                    timestamp is not None
                    and now_epoch - timestamp > SENSOR_OFFLINE_TIMEOUT_S
                ):
                    sensor_status[component] = False


def on_mqtt_connect(
    client, userdata, flags, reason_code, properties=None
):
    client.subscribe(MQTT_CONFIG["topic"])
    client.subscribe("sensors2mqtt-glo2/esp32/status/#")

    with lock:
        runtime["mqtt_connected"] = True
        runtime["last_error"] = None
        last_seen["mqtt"] = time.time()
        sensor_status["mqtt"] = True

    print("[MQTT] Connected. Subscribed to topics")
    with lock:
        if session.get("session_guid"):
            if session["status"] in {"running", "paused"}:
                publish_session_control("session_started")
            elif session["status"] == "stopped":
                publish_session_control("session_stopped")
        else:
            publish_session_control("session_stopped")


def publish_session_control(event):
    client = mqtt_client
    if client is None:
        runtime["last_error"] = (
            "Session started/stopped but MQTT session control is unavailable"
        )
        print(f"[MQTT] {runtime['last_error']}")
        return False

    payload = {
        "event": event,
        "session_guid": session.get("session_guid"),
    }
    try:
        # NON usare retain=True per evitare messaggi stale al riavvio del Raspberry
        result = client.publish(
            MQTT_CONFIG["session_control_topic"],
            json.dumps(payload),
            qos=1,
            retain=False,  # ← Cambiato da True a False
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish returned rc={result.rc}")

        # Pubblica anche lo stato sessione per sincronizzare il Command Center
        publish_session_status()

        return True
    except Exception as error:
        runtime["last_error"] = f"Session control publish failed: {error}"
        print(f"[MQTT] {runtime['last_error']}")
        return False


def publish_session_status():
    """Pubblica lo stato corrente della sessione su MQTT per sincronizzare il Command Center."""
    client = mqtt_client
    if client is None:
        return False

    payload = {
        "status": session.get("status", "idle"),
        "session_guid": session.get("session_guid"),
        "driver": session.get("driver", {}).get("id") if session.get("driver") else None,
        "track_id": current_track_id,
        "started_at": session.get("started_at"),
        "timestamp": datetime.now().isoformat(),
    }

    try:
        result = client.publish(
            MQTT_CONFIG["session_status_topic"],
            json.dumps(payload),
            qos=1,
            retain=True,
        )
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] Published session status: {session.get('status')}")
            return True
        return False
    except Exception as error:
        print(f"[MQTT] Session status publish failed: {error}")
        return False


def on_mqtt_disconnect(
    client, userdata, disconnect_flags, reason_code, properties=None
):
    with lock:
        runtime["mqtt_connected"] = False
        runtime["last_error"] = f"MQTT disconnected: {reason_code}"
        sensor_status["mqtt"] = False

    print(f"[MQTT] Disconnected: {reason_code}")


def on_mqtt_message(client, userdata, message):
    try:
        now_epoch = time.time()
        topic = message.topic

        if topic.startswith("sensors2mqtt-glo2/esp32/status/"):
            if message.retain:
                return

            payload = json.loads(
                message.payload.decode("utf-8", errors="replace")
            )

            component = payload.get("sensor")
            present = payload.get("present", False)

            if component in sensor_status:
                with lock:
                    if present:
                        last_seen[component] = now_epoch
                        sensor_status[component] = True
                    else:
                        sensor_status[component] = False

            return

        payload = json.loads(
            message.payload.decode("utf-8", errors="replace")
        )

        if not isinstance(payload, dict):
            return

        if payload.get("session_toggle") is True:
            with lock:
                if session["status"] in ("idle", "stopped"):
                    driver = get_driver(DEFAULT_DRIVER_ID)

                    if not driver:
                        runtime["last_error"] = (
                            f"Session toggle: default driver "
                            f"'{DEFAULT_DRIVER_ID}' not found"
                        )
                        print(
                            "[MQTT] session_toggle: default driver "
                            f"'{DEFAULT_DRIVER_ID}' not found"
                        )
                    else:
                        start_recording_session(driver, source="mqtt")

                        print(
                            "[MQTT] Session REC started via MQTT "
                            f"for driver '{driver['id']}'"
                        )

                elif session["status"] in ("running", "paused"):
                    final_path = stop_recording_session()

                    print(
                        "[MQTT] Session REC stopped via MQTT, "
                        f"saved '{final_path.name if final_path else '?'}'"
                    )

            return

        cooldown_requested = payload.get("cooldown") is True
        sample = normalise_payload(payload)

        sample["received_at"] = now_iso()
        sample["received_at_epoch"] = now_epoch
        sample["topic"] = topic

        with lock:
            latest_sensors.update(sample)

            last_seen["hotspot"] = now_epoch
            sensor_status["hotspot"] = True

            if sample.get("temperature_c") is not None:
                last_seen["ntc"] = now_epoch
                sensor_status["ntc"] = True

            if (
                sample.get("as5600_rpm") is not None
                or sample.get("as5600_angle_deg") is not None
            ):
                last_seen["as5600"] = now_epoch
                sensor_status["as5600"] = True

            if sample.get("ir_rpm") is not None:
                last_seen["ir_rpm"] = now_epoch
                sensor_status["ir_rpm"] = True

            if sample.get("latitude") and sample.get("longitude"):
                last_seen["gps"] = now_epoch
                sensor_status["gps"] = True

            runtime["mqtt_last_message_at"] = sample["received_at"]
            runtime["mqtt_last_message_epoch"] = sample["received_at_epoch"]
            runtime["mqtt_topic"] = topic
            runtime["mqtt_messages"] += 1
            runtime["telemetry_payload_received"] = True

            if not session.get("mqtt_topic"):
                session["mqtt_topic"] = topic

            if session["status"] == "running":
                session["session_sample_count"] += 1
                if sample.get("fix_valid") is True:
                    session["session_valid_gps_count"] += 1

            # Senza una pista attiva non c'è timing: salviamo solo
            # i campioni grezzi e ignoriamo la logica di settori/box.
            if not active_track_loaded():
                session["previous_timing_sample"] = sample
                return

            if cooldown_requested:
                stop_timing_for_cooldown(now_epoch, sample)

            previous_sample = session.get("previous_timing_sample")
            process_timing(sample, previous_sample)

            if (
                session.get("track_state") == "on_track"
                and session.get("current_lap_timer_running")
                and valid_timing_sample(sample)
            ):
                append_lap_point(sample)
            elif (
                session["status"] == "running"
                and session.get("track_state")
                in {"pitlane", "box", "warmup"}
                and valid_coordinates(sample)
            ):
                append_warmup_point(sample)

            session["previous_timing_sample"] = sample

            update_delta_live(sample)

            if session["status"] == "running":
                autosave_if_due()

    except Exception as error:
        with lock:
            runtime["last_error"] = str(error)

        print(f"[MQTT] Message error: {error}")


def start_mqtt():
    global mqtt_client

    # Usa clean_session=False per mantenere la sessione persistente
    # e bufferizzare i messaggi durante le disconnessioni
    # NOTA: clean_session=False richiede un client_id per identificare la sessione
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="kart-performance-monitor-server",
        clean_session=False,
    )

    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message

    # Configura retry automatico con backoff esponenziale
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    # Configura le code per bufferizzare messaggi durante disconnessioni
    # max_queued: fino a 200 messaggi in coda (sufficiente per ~20s di GPS a 10Hz)
    # max_inflight: massimo 20 messaggi in transito simultaneamente
    client.max_queued_messages_set(200)
    client.max_inflight_messages_set(20)

    client.connect_async(
        MQTT_CONFIG["broker"],
        MQTT_CONFIG["port"],
        MQTT_CONFIG.get("keepalive_s", 60),
    )

    mqtt_client = client
    client.loop_start()

    return client


# =============================================================
# HTML — SETUP PAGE
# =============================================================

HTML_SETUP = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>Kart Performance Monitor — Setup</title>

<style>
:root{
    --white:#f4f4f4;
    --muted:#c8c8c8;
    --purple:#d500f9;
    --green:#00a651;
    --yellow:#d9a300;
    --red:#ff2222;
}

*{box-sizing:border-box;}

html,body{
    margin:0;
    width:100%;
    height:100%;
    overflow:hidden;
    background:#000;
    color:#fff;
    font-family:Arial,Helvetica,sans-serif;
}

body{display:grid;place-items:center;}

.setup{
    width:100vw;
    height:100vh;
    max-width:932px;
    max-height:430px;
    padding:10px 20px 12px;
    display:grid;
    grid-template-columns:1fr 1fr 1fr;
    grid-template-rows:21px minmax(0,1fr);
    gap:12px;
    background:#000;
}

.topbar{
    grid-column:1/-1;
    height:21px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    font-size:11px;
    font-weight:700;
    white-space:nowrap;
    overflow:hidden;
}

.topbar-left{
    margin-left:10px;
    overflow:hidden;
    text-overflow:ellipsis;
}

.topbar-right{
    margin-right:10px;
    display:flex;
    align-items:center;
    gap:8px;
    flex-shrink:0;
}

.topbar-right .sep{
    color:var(--muted);
    font-weight:300;
}

.status-badge{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    padding:2px;
    border:2px solid #fff;
    border-radius:999px;
    background:transparent;
    line-height:1;
}

.status-badge .status-fill{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    padding:2px 8px;
    border-radius:999px;
    background:#000;
    color:#fff;
    font-size:11px;
    line-height:1;
    font-weight:800;
    white-space:nowrap;
    transition:background-color .25s, color .25s;
}

.status-badge.active .status-fill{
    background:#fff;
    color:#000;
}

.column{
    grid-row:2;
    min-width:0;
    min-height:0;
    display:flex;
    flex-direction:column;
}

.panel{
    position:relative;
    min-height:0;
    height:100%;
    border:2px solid var(--white);
    border-radius:23px;
    display:flex;
    flex-direction:column;
    padding:24px 18px 16px;
}

.panel-title{
    position:absolute;
    z-index:3;
    top:-10px;
    left:50%;
    transform:translateX(-50%);
    padding:0 10px;
    background:#000;
    white-space:nowrap;
    font-size:12px;
    font-weight:800;
}

.panel-body{
    flex:1;
    min-height:0;
    display:flex;
    flex-direction:column;
    justify-content:center;
    align-items:stretch;
    gap:8px;
}

.black-select{
    width:100%;
    background:#000;
    color:#fff;
    border:2px solid #fff;
    border-radius:16px;
    padding:10px 34px 10px 14px;
    font-size:16px;
    font-weight:800;
    font-family:inherit;
    appearance:none;
    -webkit-appearance:none;
    text-align:center;
    text-align-last:center;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'><path d='M0 0 L5 6 L10 0' fill='none' stroke='white' stroke-width='1.5'/></svg>");
    background-repeat:no-repeat;
    background-position:right 12px center;
    background-size:10px 6px;
    text-overflow:ellipsis;
}

.black-select option{
    background:#000;
    color:#fff;
    font-weight:800;
}

.big-button{
    width:100%;
    flex:0 0 auto;
    min-height:56px;
    background:#000;
    color:#fff;
    border:2px solid #fff;
    border-radius:16px;
    font-family:inherit;
    font-size:16px;
    font-weight:900;
    letter-spacing:1px;
    cursor:pointer;
    padding:14px 8px;
    transition:background-color .15s, color .15s;
    text-align:center;
}

.big-button:active,
.big-button:hover{
    background:#fff;
    color:#000;
}

.big-button:disabled{
    opacity:.45;
    cursor:not-allowed;
}

.small-button{
    width:100%;
    flex:0 0 auto;
    background:transparent;
    color:var(--muted);
    border:2px solid var(--muted);
    border-radius:12px;
    font-family:inherit;
    font-size:11px;
    font-weight:800;
    letter-spacing:.5px;
    padding:8px;
    cursor:pointer;
    text-align:center;
}

.small-button:hover{
    background:#fff;
    color:#000;
    border-color:#fff;
}

.hint{
    font-size:10px;
    color:var(--muted);
    text-align:center;
    font-weight:700;
    letter-spacing:.4px;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
}

@media(max-height:390px){
    .setup{
        padding-top:6px;
        grid-template-rows:18px minmax(0,1fr);
    }

    .topbar{height:18px;}
    .panel{padding-top:20px;}
    .big-button{font-size:15px;min-height:50px;padding:12px 6px;}
    .black-select{font-size:14px;}
}

@media(max-width:700px){
    .setup{
        padding-left:12px;
        padding-right:12px;
        gap:8px;
    }

    .topbar-left{margin-left:7px;}
    .topbar-right{margin-right:7px;gap:6px;}
}
</style>
</head>

<body>
<main class="setup">
    <div class="topbar">
        <div class="topbar-left">
            <span>KART PERFORMANCE MONITOR &mdash; SETUP</span>
        </div>

        <div class="topbar-right">
            <span>FIX: <span id="gpsFix">--</span></span>
            <span class="sep">|</span>
            <span>SAT: <span id="gpsSat">0</span></span>
            <span class="sep">|</span>
            <span>HDOP: <span id="gpsHdop">--</span></span>
            <span class="sep">|</span>
            <span id="dataAge">--s</span>
            <span class="sep">|</span>
            <span class="status-badge" id="sessionBadge">
                <span class="status-fill" id="sessionBadgeText">IDLE</span>
            </span>
        </div>
    </div>

    <section class="column">
        <div class="panel">
            <div class="panel-title">DRIVER</div>
            <div class="panel-body">
                <select id="driverSelect" class="black-select"></select>
                <div class="hint" id="driverHint">Seleziona il pilota</div>
            </div>
        </div>
    </section>

    <section class="column">
        <div class="panel">
            <div class="panel-title">TRACK</div>
            <div class="panel-body">
                <select id="trackSelect" class="black-select"></select>
                <div class="hint" id="trackHint">Seleziona il circuito</div>
            </div>
        </div>
    </section>

    <section class="column">
        <div class="panel">
            <div class="panel-title">READY</div>
            <div class="panel-body">
                <button id="startBtn" class="big-button" disabled>START SESSION</button>
                <button id="goToDashboardBtn" class="big-button">
                    GO TO DASHBOARD
                </button>
                <button id="stopBtn" class="small-button" style="display:none">
                    STOP CURRENT SESSION
                </button>
            </div>
        </div>
    </section>
</main>

<script>
const FIX_TYPE_LABELS = {
    0: "No Fix",
    1: "GPS",
    2: "DGPS",
    3: "PPS",
    4: "RTK",
    5: "Float RTK",
    6: "Estimated",
    7: "Manual",
    8: "Simulation"
};

const driverSelect       = document.getElementById("driverSelect");
const trackSelect        = document.getElementById("trackSelect");
const startBtn           = document.getElementById("startBtn");
const goToDashboardBtn   = document.getElementById("goToDashboardBtn");
const stopBtn            = document.getElementById("stopBtn");
const badge              = document.getElementById("sessionBadge");
const badgeText          = document.getElementById("sessionBadgeText");
const driverHint         = document.getElementById("driverHint");
const trackHint          = document.getElementById("trackHint");

let sessionStatus = "idle";
let hasTracks = false;

async function loadDrivers(){
    const res  = await fetch("/api/drivers", {cache:"no-store"});
    const data = await res.json();

    driverSelect.innerHTML = "";

    (data.drivers || []).forEach(function(d){
        const opt = document.createElement("option");
        opt.value = d.id;
        opt.textContent = d.name || d.id;
        driverSelect.appendChild(opt);
    });

    const freeOpt = document.createElement("option");
    freeOpt.value = "__free__";
    freeOpt.textContent = "Free Practice (no driver)";
    driverSelect.appendChild(freeOpt);

    if (driverSelect.options.length){
        driverSelect.selectedIndex = 0;
    }
}

async function loadTracks(){
    const res  = await fetch("/api/tracks", {cache:"no-store"});
    const data = await res.json();

    trackSelect.innerHTML = "";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— Seleziona circuito —";
    trackSelect.appendChild(placeholder);

    const tracks = data.tracks || [];

    tracks.forEach(function(t){
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.name + (t.location ? " · " + t.location : "");
        trackSelect.appendChild(opt);
    });

    hasTracks = tracks.length > 0;

    trackSelect.value = "";

    if (!hasTracks){
        trackHint.textContent = "Nessuna pista disponibile in config/tracks/";
    } else {
        trackHint.textContent = "Seleziona il circuito per abilitare START";
    }

    updateStartButtonState();
}

function updateStartButtonState(){
    const running = (sessionStatus === "running" || sessionStatus === "paused");
    const trackSelected = !!trackSelect.value;

    if (running){
        startBtn.disabled = true;
        startBtn.textContent = "SESSION RUNNING";
    } else if (!trackSelected){
        startBtn.disabled = true;
        startBtn.textContent = "SELECT A TRACK";
    } else {
        startBtn.disabled = false;
        startBtn.textContent = "START SESSION";
    }
}

function applySessionStatus(status){
    sessionStatus = status || "idle";

    const running = (sessionStatus === "running" || sessionStatus === "paused");

    badge.classList.toggle("active", running);
    badgeText.textContent = sessionStatus.toUpperCase();

    stopBtn.style.display = running ? "" : "none";

    updateStartButtonState();
}

function applyGps(gps, dataAge){
    const g = gps || {};

    document.getElementById("gpsFix").textContent =
        g.fix_valid
            ? (FIX_TYPE_LABELS[g.fix_type] || ("T" + g.fix_type))
            : "No Fix";

    document.getElementById("gpsSat").textContent =
        g.satellites_used || 0;

    document.getElementById("gpsHdop").textContent =
        (g.hdop !== null && g.hdop !== undefined)
            ? Number(g.hdop).toFixed(2)
            : "--";

    document.getElementById("dataAge").textContent =
        dataAge !== null && dataAge !== undefined
            ? Number(dataAge).toFixed(1) + "s"
            : "--s";
}

async function refreshStatus(){
    try{
        const res  = await fetch("/api/live", {cache:"no-store"});
        const data = await res.json();
        applySessionStatus(data.session && data.session.status);
        applyGps(
            data.gps,
            data.runtime ? data.runtime.data_age_s : null
        );
    }catch(e){
        console.error(e);
    }
}

async function handleStart(){
    if (sessionStatus === "running" || sessionStatus === "paused"){
        return;
    }

    const trackVal = trackSelect.value;

    if (!trackVal){
        alert("Seleziona un circuito prima di iniziare.");
        return;
    }

    startBtn.disabled = true;
    const old = startBtn.textContent;
    startBtn.textContent = "STARTING...";

    try{
        const driverVal = driverSelect.value;

        const res = await fetch("/api/session/start", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                driver_id: driverVal === "__free__" ? null : driverVal,
                track_id:  trackVal
            })
        });

        const data = await res.json();

        if (data.ok){
            window.location.href = "/dashboard";
        } else {
            startBtn.disabled = false;
            startBtn.textContent = old;
            alert("Errore: " + (data.error || "unknown"));
        }
    }catch(e){
        startBtn.disabled = false;
        startBtn.textContent = old;
        alert("Errore di rete: " + e.message);
    }
}

async function handleGoToDashboard(){
    const driverVal = driverSelect.value;
    const trackVal  = trackSelect.value;

    if (!trackVal){
        alert("Seleziona un circuito prima di andare alla dashboard.");
        return;
    }

    goToDashboardBtn.disabled = true;
    const old = goToDashboardBtn.textContent;
    goToDashboardBtn.textContent = "LOADING...";

    try{
        const res = await fetch("/api/session/preview", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                driver_id: driverVal === "__free__" ? null : driverVal,
                track_id:  trackVal
            })
        });

        const data = await res.json();

        if (!data.ok){
            goToDashboardBtn.disabled = false;
            goToDashboardBtn.textContent = old;
            alert("Errore: " + (data.error || "unknown"));
            return;
        }
    }catch(e){
        goToDashboardBtn.disabled = false;
        goToDashboardBtn.textContent = old;
        alert("Errore di rete: " + e.message);
        return;
    }

    window.location.href = "/dashboard";
}

async function handleStop(){
    if (!confirm("Fermare la sessione corrente?")) return;

    try{
        await fetch("/api/session/stop", {method:"POST"});
        await refreshStatus();
    }catch(e){
        alert("Errore di rete: " + e.message);
    }
}

trackSelect.addEventListener("change", updateStartButtonState);
startBtn.addEventListener("click", handleStart);
goToDashboardBtn.addEventListener("click", handleGoToDashboard);
stopBtn.addEventListener("click", handleStop);

(async function init(){
    try{
        await Promise.all([loadDrivers(), loadTracks()]);
    }catch(e){
        console.error(e);
    }

    await refreshStatus();

    setInterval(refreshStatus, 250);
})();
</script>
</body>
</html>
"""


# =============================================================
# HTML — DASHBOARD
# =============================================================

HTML = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>Kart Performance Monitor</title>

<style>
:root{
    --white:#f4f4f4;
    --muted:#c8c8c8;
    --purple:#d500f9;
    --green:#00a651;
    --yellow:#d9a300;
    --red:#ff2222;
}

*{
    box-sizing:border-box;
}

html,
body{
    margin:0;
    width:100%;
    height:100%;
    overflow:hidden;
    background:#000;
    color:#fff;
    font-family:Arial,Helvetica,sans-serif;
}

body{
    display:grid;
    place-items:center;
}

.dashboard{
    width:100vw;
    height:100vh;
    max-width:932px;
    max-height:430px;
    padding:10px 20px 4px;
    display:grid;
    grid-template-columns:1fr 1.46fr 1fr;
    grid-template-rows:21px minmax(0,1fr);
    gap:12px;
    background:#000;
}

.column{
    grid-row:2;
    min-width:0;
    display:flex;
    flex-direction:column;
}

.topbar{
    grid-column:1/-1;
    height:21px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    font-size:11px;
    line-height:13px;
    font-weight:700;
    white-space:nowrap;
    overflow:hidden;
}

.topbar-left,
.topbar-right{
    display:flex;
    align-items:center;
}

.topbar-left{
    gap:18px;
    margin-left:10px;
    overflow:hidden;
    text-overflow:ellipsis;
}

.topbar-right{
    gap:8px;
    margin-right:10px;
    flex-shrink:0;
}

.topbar-right span.sep{
    color:var(--muted);
    font-weight:300;
}

.status-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: fit-content;
    height: auto;
    padding: 2px;
    border: 2px solid #fff;
    border-radius: 999px;
    background: transparent;
    line-height: 1;
    transition: background-color .25s, color .25s;
}

.status-badge .status-fill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 2px 7px;
    border-radius: 999px;
    background: #000;
    color: #fff;
    font-size: 11px;
    line-height: 1;
    font-weight: 800;
    white-space: nowrap;
    transition: background-color .25s, color .25s;
}

.status-badge.active .status-fill {
    background: #fff;
    color: #000;
}

.panel{
    position:relative;
    min-height:0;
    height:100%;
    border:2px solid var(--white);
    border-radius:23px;
}

.panel-title{
    position:absolute;
    z-index:3;
    top:-10px;
    left:50%;
    transform:translateX(-50%);
    padding:0 10px;
    background:#000;
    white-space:nowrap;
    font-size:12px;
    font-weight:800;
    display:flex;
    align-items:center;
    gap:6px;
}

.lap-count-badge{
    display:inline-block;
    min-width:24px;
    height:24px;
    padding:0 6px;
    background:#000;
    color:#fff;
    border:2px solid #fff;
    border-radius:50%;
    text-align:center;
    line-height:20px;
    font-size:12px;
    font-weight:800;
}

.left-panel{
    display:flex;
    flex-direction:column;
    align-items:center;
    gap:0;
    padding:29px 14px 9px;
}

.box{
    border:2px solid var(--white);
    border-radius:20px;
    padding:10px 15px;
    min-height:64px;
}

.box-title{
    margin:0;
    text-align:center;
    font-size:11px;
    font-weight:800;
}

.delta-box{
    width:100%;
    margin:0;
    transition: background-color .3s ease;
}

.delta-box-negative{
    background-color: var(--green);
}

.delta-box-positive{
    background-color: var(--yellow);
}

.delta-box-negative .box-title,
.delta-box-positive .box-title{
    color:#000;
}

#delta{
    padding-top:8px;
    text-align:center;
    font-size:18px;
    font-weight:800;
    font-variant-numeric:tabular-nums;
}

.delta-box-negative #delta,
.delta-box-positive #delta{
    color:#000;
}

.delta-positive{
    color:var(--red);
}

.delta-negative{
    color:#00ff55;
}

.delta-neutral{
    color:var(--white);
}

.sector{
    width:calc(100% - 34px);
    height:100px;
    min-height:100px;
    margin:auto 0;
    padding:10px 13px 13px;
}

.sector .box-title{
    margin-top:0;
    line-height:13px;
}

.sector-values{
    width:100%;
    margin-top:7px;
    display:grid;
    gap:3px;
    padding-bottom:4px;
}

.sector-row{
    display:grid;
    grid-template-columns:32px 1fr;
    align-items:center;
    column-gap:10px;
    min-height:18px;
}

.sector-badge{
    width:32px;
    height:18px;
    padding:2px;
    border:2px solid var(--white);
    border-radius:7px;
    background:#000;
    transition: background-color .3s;
}

.sector-badge .sector-fill{
    width:100%;
    height:100%;
    border-radius:3px;
    display:grid;
    place-items:center;
    background:#000;
    color:#fff;
    font-size:11px;
    line-height:1;
    font-weight:800;
    font-variant-numeric:tabular-nums;
    transition:
        background-color .20s ease,
        color .20s ease;
}

.sector-badge.active .sector-fill{
    background:#fff;
    color:#000;
}

.sector-badge.best .sector-fill{
    background:var(--purple);
    color:#000;
}

.sector-badge.improved .sector-fill{
    background:var(--green);
    color:#000;
}

.sector-badge.slower .sector-fill{
    background:var(--yellow);
    color:#000;
}

.sector-time{
    display:block;
    width:100%;
    text-align:right;
    font-size:17px;
    line-height:18px;
    font-weight:800;
    font-variant-numeric:tabular-nums;
    white-space:nowrap;
    transition: color .3s;
}

.sector-time.best{
    color:var(--purple);
}

.sector-time.improved{
    color:var(--green);
}

.sector-time.slower{
    color:var(--yellow);
}

.modes{
    width:100%;
    display:grid;
    gap:7px;
    margin-bottom:11px;
}

.mode{
    width:100%;
    height:25px;
    padding:3px;
    border:2px solid var(--white);
    border-radius:14px;
    background:transparent;
}

.mode-fill{
    width:100%;
    height:100%;
    border-radius:9px;
    display:grid;
    place-items:center;
    background:#000;
    color:#fff;
    font-size:11px;
    font-weight:800;
    letter-spacing:.1px;
    transition:
        background-color .25s,
        color .25s;
}

.mode.active .mode-fill{
    background:#fff;
    color:#000;
}

.telemetry-panel{
    padding:27px 30px 9px;
    display:flex;
    flex-direction:column;
    align-items:center;
}

.gauge{
    width:100%;
}

.gauge-track{
    height:32px;
    position:relative;
    border:2px solid var(--white);
    background:linear-gradient(
        90deg,
        #fff 0 var(--fill,0%),
        transparent var(--fill,0%)
    );
}

.gauge-track:after{
    content:"";
    position:absolute;
    inset:0;
    background:repeating-linear-gradient(
        90deg,
        transparent 0 13px,
        #fff 13px 15px
    );
    opacity:.82;
}

.needle{
    position:absolute;
    z-index:2;
    top:-10px;
    height:46px;
    border-left:3px solid #fff;
    transition:left .25s linear;
}

.needle:before{
    content:"";
    position:absolute;
    top:-8px;
    left:-7px;
    border-left:7px solid transparent;
    border-right:7px solid transparent;
    border-top:9px solid #fff;
}

.gauge-labels{
    display:flex;
    justify-content:space-between;
    margin-top:4px;
    font-size:11px;
    font-weight:700;
}

.unit{
    text-align:center;
    margin-top:2px;
    color:var(--muted);
    font-size:10px;
    font-weight:700;
}

.speed-row{
    display:flex;
    align-items:baseline;
    gap:13px;
    margin:auto 0 14px;
}

#speed{
    font-size:124px;
    letter-spacing:-7px;
    line-height:.7;
    font-weight:800;
}

.speed-caption{
    font-size:20px;
    line-height:20px;
    font-weight:800;
}

.speed-caption small{
    font-size:18px;
    font-weight:500;
}

.temp-gauge{
    width:92%;
    margin-top:auto;
    position:relative;
}

.temp-gauge .gauge-track{
    height:16px;
}

.temp-gauge .needle{
    top:-7px;
    height:30px;
}

.temp-gauge .gauge-labels{
    font-size:10px;
}

.status-badges{
    position:absolute;
    left:2px;
    top:-40px;
    display:flex;
    gap:6px;
}

.status-chip{
    width:38px;
    height:22px;
    padding:3px;
    border:2px solid #fff;
    border-radius:8px;
    background:transparent;
}

.status-chip .chip-fill{
    width:100%;
    height:100%;
    border-radius:4px;
    display:grid;
    place-items:center;
    font-size:9px;
    font-weight:800;
    letter-spacing:.3px;
    background:#000;
    color:#fff;
    transition:
        background-color .25s,
        color .25s;
}

.status-chip.active .chip-fill{
    background:#fff;
    color:#000;
}

.right-panel{
    padding:29px 13px;
    display:flex;
    flex-direction:column;
    gap:7px;
}

.lap-box{
    flex:1;
    min-height:0;
    display:flex;
    flex-direction:column;
    justify-content:center;
}

.lap-time{
    margin-top:4px;
    text-align:center;
    font-size:17px;
    font-weight:800;
    font-variant-numeric:tabular-nums;
}

.sector-popup{
    position:fixed;
    z-index:30;
    left:50%;
    top:50%;
    width:min(53vw,495px);
    height:min(27vw,240px);
    min-width:380px;
    min-height:178px;
    transform:translate(-50%,-50%) scale(.92);
    display:grid;
    place-items:center;
    text-align:center;
    border:5px solid #fff;
    border-radius:30px;
    opacity:0;
    visibility:hidden;
    pointer-events:none;
    box-shadow:0 0 38px rgba(255,255,255,.30);
    transition:
        opacity .18s ease,
        transform .18s ease,
        visibility .18s;
}

.sector-popup.show{
    opacity:1;
    visibility:visible;
    transform:translate(-50%,-50%) scale(1);
}

.sector-popup.purple{
    background:var(--purple);
}

.sector-popup.green{
    background:var(--green);
}

.sector-popup.yellow{
    background:var(--yellow);
}

.popup-result{
    padding:8px 18px;
    color:#fff;
    font-size:clamp(55px,10vw,94px);
    line-height:1;
    font-weight:900;
    letter-spacing:-2px;
    font-variant-numeric:tabular-nums;
    white-space:nowrap;
    text-shadow:0 3px 1px rgba(0,0,0,.2);
}

@media(max-height:390px){
    .dashboard{
        padding-top:6px;
        grid-template-rows:18px minmax(0,1fr);
    }

    .topbar{
        height:18px;
    }

    .left-panel{
        padding-top:25px;
    }

    .telemetry-panel{
        padding-top:24px;
    }

    .right-panel{
        padding:25px 13px;
    }

    #speed{
        font-size:114px;
    }

    .lap-box{
        height:53px !important;
        margin-bottom:7px !important;
    }

    .mode{
        height:21px;
        padding:3px;
    }

    .mode-fill{
        font-size:10px;
    }

    .modes{
        gap:4px;
        margin-bottom:8px;
    }

    .sector{
        width:calc(100% - 28px);
        height:91px;
        min-height:91px;
        margin:auto 0;
        padding:9px 10px 12px;
    }

    .sector-values{
        margin-top:6px;
        gap:2px;
        padding-bottom:3px;
    }

    .sector-row{
        grid-template-columns:29px 1fr;
        column-gap:8px;
        min-height:16px;
    }

    .sector-badge{
        width:29px;
        height:16px;
        padding:2px;
        border-radius:6px;
    }

    .sector-badge .sector-fill{
        font-size:10px;
    }

    .sector-time{
        font-size:16px;
        line-height:16px;
    }

    .sector-popup{
        height:185px;
    }

    .status-badges{
        top:-36px;
        gap:4px;
    }

    .status-chip{
        width:32px;
        height:19px;
        padding:2px;
    }

    .status-chip .chip-fill{
        font-size:8px;
    }
}

@media(max-width:700px){
    .dashboard{
        padding-left:12px;
        padding-right:12px;
        gap:8px;
    }

    .topbar-left{
        gap:10px;
        margin-left:7px;
    }

    .topbar-right{
        gap:6px;
        margin-right:7px;
    }

    .sector-popup{
        width:84vw;
        height:48vw;
        min-width:0;
        min-height:0;
    }

    .popup-result{
        font-size:17vw;
    }
}
</style>
</head>

<body>
<main class="dashboard">
    <div class="topbar">
        <div class="topbar-left">
            <span>
                Circuit:
                <span id="circuit"></span>
            </span>

            <span>
                Driver:
                <span id="driver"></span>
            </span>
        </div>

        <div class="topbar-right">
            <span>FIX: <span id="fix"></span></span>
            <span class="sep">|</span>
            <span>SAT: <span id="sat"></span></span>
            <span class="sep">|</span>
            <span>HDOP: <span id="hdop"></span></span>
            <span class="sep">|</span>
            <span id="dataAge"></span>
            <span class="sep">|</span>
            <span class="status-badge" id="sessionStatusBadge"><span class="status-fill" id="sessionStatusText">IN ATTESA</span></span>
        </div>
    </div>

    <section class="column">
        <div class="panel left-panel">
            <div class="panel-title">SECTOR</div>

            <div class="box delta-box" id="deltaBox">
                <h2 class="box-title">
                    DELTA TIME
                    <span style="font-size:8px;letter-spacing:.6px">
                        LIVE
                    </span>
                </h2>

                <div id="delta" class="delta-neutral">
                    --
                </div>
            </div>

            <div class="box sector">
                <h2 class="box-title">LAST SECTOR</h2>

                <div class="sector-values">
                    <div class="sector-row">
                        <div
                            class="sector-badge"
                            id="sector1Badge"
                        >
                            <div class="sector-fill">S1</div>
                        </div>

                        <span id="s1" class="sector-time">
                            --
                        </span>
                    </div>

                    <div class="sector-row">
                        <div
                            class="sector-badge"
                            id="sector2Badge"
                        >
                            <div class="sector-fill">S2</div>
                        </div>

                        <span id="s2" class="sector-time">
                            --
                        </span>
                    </div>

                    <div class="sector-row">
                        <div
                            class="sector-badge"
                            id="sector3Badge"
                        >
                            <div class="sector-fill">S3</div>
                        </div>

                        <span id="s3" class="sector-time">
                            --
                        </span>
                    </div>
                </div>
            </div>

            <div class="modes">
                <div class="mode" id="modeWarmup">
                    <div class="mode-fill">WARM UP</div>
                </div>

                <div class="mode" id="modeHammer">
                    <div class="mode-fill">HAMMER TIME</div>
                </div>

                <div class="mode" id="modeCooldown">
                    <div class="mode-fill">COOL DOWN</div>
                </div>
            </div>
        </div>
    </section>

    <section class="column">
        <div class="panel telemetry-panel">
            <div class="panel-title">TELEMETRY</div>

            <div class="gauge">
                <div class="gauge-track" id="rpmTrack">
                    <div class="needle" id="rpmNeedle"></div>
                </div>

                <div class="gauge-labels">
                    <span>0</span>
                    <span>2</span>
                    <span>4</span>
                    <span>6</span>
                    <span>8</span>
                    <span>10</span>
                    <span>12</span>
                    <span>14</span>
                    <span>16</span>
                    <span>18</span>
                    <span>20</span>
                </div>

                <div class="unit">RPM (x1000)</div>
            </div>

            <div class="speed-row">
                <strong id="speed">0</strong>

                <div class="speed-caption">
                    SPEED<br>
                    <small>km/h</small>
                </div>
            </div>

            <div class="gauge temp-gauge">
                <div class="status-badges">
                    <div class="status-chip" id="pitBadge">
                        <div class="chip-fill">PIT</div>
                    </div>

                    <div class="status-chip" id="boxBadge">
                        <div class="chip-fill">BOX</div>
                    </div>
                </div>

                <div class="gauge-track" id="tempTrack">
                    <div class="needle" id="tempNeedle"></div>
                </div>

                <div class="gauge-labels">
                    <span>20°C</span>
                    <span>40°C</span>
                    <span>60°C</span>
                    <span>80°C</span>
                    <span>100°C</span>
                    <span>120°C</span>
                    <span>150°C</span>
                </div>

                <div class="unit">TEMP (°C)</div>
            </div>
        </div>
    </section>

    <section class="column">
        <div class="panel right-panel">
            <div class="panel-title">
                LAP
                <span id="lapCountTitle" class="lap-count-badge">0</span>
            </div>

            <div class="box lap-box">
                <h2 class="box-title">CURRENT LAP</h2>
                <div id="currentLap" class="lap-time">
                    --:--.---
                </div>
            </div>

            <div class="box lap-box">
                <h2 class="box-title">BEST LAP</h2>
                <div id="bestLap" class="lap-time">
                    --:--.---
                </div>
            </div>

            <div class="box lap-box">
                <h2 class="box-title">LAST LAP</h2>
                <div id="lastLap" class="lap-time">
                    --:--.---
                </div>
            </div>

            <div class="box lap-box">
                <h2 class="box-title">IDEAL LAP</h2>
                <div id="idealLap" class="lap-time">
                    --:--.---
                </div>
            </div>
        </div>
    </section>
</main>

<div id="sectorPopup" class="sector-popup">
    <div id="popupResult" class="popup-result">
        +0.000 s
    </div>
</div>

<script>
const fmt = function(value) {
    if (
        value === null ||
        value === undefined ||
        value === 0
    ) {
        return "--:--.---";
    }

    const minutes = Math.floor(value / 60);
    const seconds = value - minutes * 60;

    return (
        minutes
        + ":"
        + seconds.toFixed(3).padStart(6, "0")
    );
};

const gauge = function(track, needle, percent) {
    const clamped = Math.max(
        0,
        Math.min(100, percent)
    );

    track.style.setProperty(
        "--fill",
        clamped + "%"
    );

    needle.style.left = clamped + "%";
};

let lastEventId = 0;
let popupTimer = null;

function updateDelta(value) {
    const deltaBox = document.getElementById("deltaBox");
    const delta = document.getElementById("delta");

    deltaBox.classList.remove("delta-box-negative", "delta-box-positive");
    delta.className = "delta-neutral";

    if (
        value === null ||
        value === undefined ||
        !Number.isFinite(Number(value))
    ) {
        delta.textContent = "--";
        return;
    }

    const numericValue = Number(value);

    delta.textContent =
        (numericValue >= 0 ? "+" : "")
        + numericValue.toFixed(3)
        + " s";

    if (numericValue < -0.005) {
        deltaBox.classList.add("delta-box-negative");
        delta.className = "delta-negative";
    } else if (numericValue > 0.005) {
        deltaBox.classList.add("delta-box-positive");
        delta.className = "delta-positive";
    } else {
        delta.className = "delta-neutral";
    }
}

function showPopup(event) {
    if (
        !event ||
        event.delta === null ||
        event.delta === undefined
    ) {
        return;
    }

    const popup = document.getElementById(
        "sectorPopup"
    );

    const popupResult = document.getElementById(
        "popupResult"
    );

    const numericDelta = Number(event.delta);

    if (!Number.isFinite(numericDelta)) {
        return;
    }

    const color =
        event.type === "best"
            ? "purple"
            : numericDelta < 0
                ? "green"
                : "yellow";

    popup.className = "sector-popup " + color;

    popupResult.textContent =
        (numericDelta >= 0 ? "+" : "")
        + numericDelta.toFixed(3)
        + " s";

    requestAnimationFrame(function() {
        popup.classList.add("show");
    });

    clearTimeout(popupTimer);

    popupTimer = setTimeout(function() {
        popup.classList.remove("show");
    }, 5000);
}

function updateModes(trackState) {
    document.getElementById("modeWarmup").className =
        "mode"
        + (
            trackState === "warmup"
            ? " active"
            : ""
        );

    document.getElementById("modeHammer").className =
        "mode"
        + (
            trackState === "on_track"
            ? " active"
            : ""
        );

    document.getElementById("modeCooldown").className =
        "mode"
        + (
            trackState === "cooldown"
            ? " active"
            : ""
        );
}

function updateStatusBadges(location) {
    const safeLocation = location || {};

    const inBox = safeLocation.in_box === true;

    const inPitLane =
        safeLocation.in_pit_lane === true
        && !inBox;

    document
        .getElementById("pitBadge")
        .classList.toggle(
            "active",
            inPitLane
        );

    document
        .getElementById("boxBadge")
        .classList.toggle(
            "active",
            inBox
        );
}

function updateSectorBadges(trackState, currentSector, sectorStatuses) {
    const normalizedState = String(
        trackState || ""
    ).toLowerCase();

    const numericSector = Number(currentSector);

    for (
        let sectorNumber = 1;
        sectorNumber <= 3;
        sectorNumber++
    ) {
        const badge = document.getElementById(
            "sector" + sectorNumber + "Badge"
        );
        const timeSpan = document.getElementById(
            "s" + sectorNumber
        );

        if (!badge || !timeSpan) {
            continue;
        }

        badge.classList.remove("best", "improved", "slower");
        timeSpan.classList.remove("best", "improved", "slower");

        const shouldBeActive =
            (
                normalizedState === "warmup"
                || normalizedState === "on_track"
            )
            && numericSector === sectorNumber;

        badge.classList.toggle("active", shouldBeActive);

        if (sectorStatuses && sectorStatuses[sectorNumber - 1]) {
            const status = sectorStatuses[sectorNumber - 1];
            if (status === "best" || status === "improved" || status === "slower") {
                badge.classList.add(status);
                timeSpan.classList.add(status);
            }
        }
    }
}

function sectorValue(value) {
    if (
        value === null ||
        value === undefined
    ) {
        return "--";
    }

    return Number(value).toFixed(3) + " s";
}

async function refresh() {
    try {
        const response = await fetch(
            "/api/telemetry",
            {
                cache: "no-store",
            }
        );

        const data = await response.json();

        document.getElementById("speed").textContent =
            data.speed;

        document.getElementById("circuit").textContent =
            data.circuit;

        document.getElementById("driver").textContent =
            data.driver;

        document.getElementById("fix").textContent =
            data.fix_label || "No Fix";

        document.getElementById("sat").textContent =
            data.sat || 0;

        const hdop = data.hdop;
        document.getElementById("hdop").textContent =
            hdop !== null && hdop !== undefined
                ? hdop.toFixed(2)
                : "--";

        const dataAge = data.data_age_s;
        document.getElementById("dataAge").textContent =
            dataAge !== null && dataAge !== undefined
                ? dataAge.toFixed(1) + "s"
                : "--s";

        const statusBadge = document.getElementById("sessionStatusBadge");
        const sessionStatus = data.session_status || "idle";
        const statusLabels = {
            idle: "IN ATTESA",
            running: "RUNNING",
            paused: "IN PAUSA",
            stopped: "STOP"
        };
        document.getElementById("sessionStatusText").textContent =
            statusLabels[sessionStatus] || statusLabels.idle;
        statusBadge.classList.toggle(
            "active",
            sessionStatus === "running" || sessionStatus === "paused"
        );

        updateDelta(data.delta);

        document.getElementById("s1").textContent =
            sectorValue(data.sectors[0]);

        document.getElementById("s2").textContent =
            sectorValue(data.sectors[1]);

        document.getElementById("s3").textContent =
            sectorValue(data.sectors[2]);

        document.getElementById(
            "currentLap"
        ).textContent = fmt(data.current_lap_time_s);

        document.getElementById(
            "bestLap"
        ).textContent = fmt(data.best_lap);

        document.getElementById(
            "lastLap"
        ).textContent = fmt(data.last_lap);

        document.getElementById(
            "idealLap"
        ).textContent = fmt(data.ideal_lap);

        document.getElementById("lapCountTitle").textContent =
            data.lap_count || 0;

        const trackState = data.track_state || "pitlane";

        const currentSector = Number(
            data.current_sector_number || 1
        );

        const sectorStatuses = data.sector_statuses || [null, null, null];

        updateModes(trackState);

        updateStatusBadges(data.location);

        updateSectorBadges(
            trackState,
            currentSector,
            sectorStatuses
        );

        gauge(
            document.getElementById("rpmTrack"),
            document.getElementById("rpmNeedle"),
            (data.rpm || 0) / 20000 * 100
        );

        gauge(
            document.getElementById("tempTrack"),
            document.getElementById("tempNeedle"),
            ((data.temp || 20) - 20) / 130 * 100
        );

        if (
            data.sector_event
            && data.sector_event.id !== lastEventId
        ) {
            lastEventId = data.sector_event.id;
            showPopup(data.sector_event);
        }
    } catch (error) {
        console.error(error);
    }
}

refresh();
setInterval(refresh, 250);
</script>
</body>
</html>
"""


# =============================================================
# ROUTES
# =============================================================

@app.get("/")
def setup_page():
    return render_template_string(HTML_SETUP)


@app.get("/dashboard")
def dashboard_page():
    return render_template_string(HTML)


@app.get("/api/telemetry")
def api_telemetry():
    with lock:
        speed = latest_sensors.get("speed_kmph", 0.0)
        sess_status = session.get("status", "idle")

        rpm = latest_sensors.get("ir_rpm")

        if rpm is None:
            rpm = latest_sensors.get("as5600_rpm", 0)

        temp = latest_sensors.get("temperature_c", 20.0)

        now_epoch = time.time()

        delta = calculate_delta_live(now_epoch)
        session["delta_live_s"] = delta

        sectors = list(session.get("current_lap_sectors_s", []))

        while len(sectors) < sector_count():
            sectors.append(None)

        if (
            session.get("current_lap_timer_running")
            and session.get("sector_started_epoch") is not None
        ):
            current_sector_idx = (
                session.get("current_sector_number", 1) - 1
            )

            if 0 <= current_sector_idx < sector_count():
                live_time = now_epoch - float(
                    session["sector_started_epoch"]
                )

                sectors[current_sector_idx] = round(live_time, 3)

        while len(sectors) < 3:
            sectors.append(None)

        best_lap = session.get("best_lap_time_s")

        last_lap = session.get("last_completed_lap_time_s")

        best_sectors = session.get("best_sector_times_s", [])

        if (
            best_sectors
            and all(value is not None for value in best_sectors)
        ):
            ideal_lap = round(sum(best_sectors), 3)
        else:
            ideal_lap = None

        if (
            session.get("current_lap_timer_running")
            and session.get("current_lap_start_epoch")
        ):
            current_lap_time_s = round(
                now_epoch
                - float(session["current_lap_start_epoch"]),
                3,
            )
        else:
            current_lap_time_s = None

        circuit = (
            TRACK_CONFIG.get("track", {}).get("name")
            or current_track_id
            or "—"
        )

        if session.get("driver"):
            driver_name = session["driver"].get("name", "Unknown Driver")
        elif preview.get("driver"):
            driver_name = preview["driver"].get("name", "Unknown Driver")
        else:
            driver_name = "—"

        satellites = latest_sensors.get("satellites_used", 0)

        fix_valid = latest_sensors.get("fix_valid", False)
        fix_type = latest_sensors.get("fix_type", 0)
        if not fix_valid:
            fix_label = "No Fix"
        else:
            fix_label = FIX_TYPE_LABELS.get(fix_type, f"T{fix_type}")

        hdop = latest_sensors.get("hdop")

        last_msg_epoch = runtime.get("mqtt_last_message_epoch", 0.0)
        if last_msg_epoch:
            data_age_s = round(now_epoch - last_msg_epoch, 3)
        else:
            data_age_s = None

        sector_event_data = None

        if session.get("last_sector_result"):
            internal_status = session["last_sector_result"]["status"]
            if internal_status == "best":
                event_type = "best"
            elif internal_status == "improved":
                event_type = "improved"
            else:
                event_type = "slower"

            sector_event_data = {
                "id": session.get("sector_event_id", 0),
                "type": event_type,
                "delta": (
                    session["last_sector_result"]["delta_to_reference_s"]
                    or session["last_sector_result"]["delta_to_best_s"]
                ),
            }

        location = dashboard_location_flags(latest_sensors)

        lap_count = sum(
            1 for lap in session.get("laps", []) if lap.get("valid")
        )

        sector_statuses = list(
            session.get("current_lap_sector_statuses", [])
        )
        while len(sector_statuses) < 3:
            sector_statuses.append(None)

        return jsonify({
            "speed": int(speed) if speed else 0,
            "rpm": int(rpm) if rpm else 0,
            "temp": temp,
            "delta": delta,
            "sectors": sectors[:3],
            "sector_statuses": sector_statuses[:3],
            "current_sector_number": session.get("current_sector_number"),
            "best_lap": best_lap,
            "last_lap": last_lap,
            "ideal_lap": ideal_lap,
            "current_lap_time_s": current_lap_time_s,
            "reference_lap_number": session.get("reference_lap_number"),
            "reference_lap_time_s": session.get("reference_lap_time_s"),
            "current_lap_distance_m": round(
                session.get("current_lap_distance_m", 0.0), 3
            ),
            "circuit": circuit,
            "driver": driver_name,
            "sat": satellites,
            "fix_label": fix_label,
            "hdop": hdop,
            "data_age_s": data_age_s,
            "session_status": sess_status,
            "track_state": session.get("track_state", "pitlane"),
            "cooldown_active": session.get("cooldown_active", False),
            "location": {
                "in_box": location["in_box"],
                "in_pit_lane": location["in_pit_lane"],
                "box_distance_m": (
                    round(location["box_distance_m"], 2)
                    if location["box_distance_m"] is not None
                    else None
                ),
                "pit_lane_distance_m": (
                    round(location["pit_lane_distance_m"], 2)
                    if location["pit_lane_distance_m"] is not None
                    else None
                ),
            },
            "sector_event": sector_event_data,
            "lap_count": lap_count,
        })


@app.get("/api/live")
def api_live():
    return jsonify(live_snapshot())


@app.get("/api/latest")
def api_latest_legacy():
    return jsonify(live_snapshot())


@app.get("/api/drivers")
def api_drivers():
    return jsonify({"drivers": drivers})


@app.get("/api/tracks")
def api_tracks():
    return jsonify({"tracks": list_available_tracks()})


@app.post("/api/session/preview")
def api_session_preview():
    data = request.get_json(silent=True) or {}
    driver_id = data.get("driver_id")
    track_id = data.get("track_id")

    if not track_id:
        return jsonify({"ok": False, "error": "Seleziona un circuito."}), 400

    with lock:
        try:
            set_active_track(track_id)
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

        if driver_id:
            driver = get_driver(driver_id)
            if not driver:
                return jsonify({
                    "ok": False,
                    "error": f"Driver '{driver_id}' non trovato",
                }), 400
        else:
            driver = {"id": "free", "name": "Free Practice"}

        preview["driver"] = deepcopy(driver)

    return jsonify({"ok": True, "track_id": current_track_id})


@app.post("/api/session/start")
def api_session_start():
    data = request.get_json(silent=True) or {}
    driver_id = data.get("driver_id")
    track_id = data.get("track_id")

    with lock:
        if session["status"] in ("running", "paused"):
            return jsonify({
                "ok": False,
                "error": "Una sessione è già in corso. Fermala prima.",
            }), 409

        if not track_id:
            return jsonify({
                "ok": False,
                "error": "Seleziona un circuito prima di iniziare.",
            }), 400

        try:
            set_active_track(track_id)
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400

        if driver_id:
            driver = get_driver(driver_id)
            if not driver:
                return jsonify({
                    "ok": False,
                    "error": f"Driver '{driver_id}' non trovato",
                }), 400
        else:
            driver = {"id": "free", "name": "Free Practice"}

        reset_session()
        start_recording_session(driver, source="web")

    return jsonify({
        "ok": True,
        "track_id": current_track_id,
        "session_guid": session["session_guid"],
    })


@app.post("/api/session/pause")
def api_session_pause():
    with lock:
        if session["status"] != "running":
            return jsonify({
                "ok": False,
                "error": "La sessione non è in corso.",
            }), 409

        session["status"] = "paused"
        session["paused_at"] = now_iso()

        append_event("session_paused")
        autosave_if_due(force=True)

    return jsonify({
        "ok": True,
        "session": live_snapshot()["session"],
    })


@app.post("/api/session/resume")
def api_session_resume():
    with lock:
        if session["status"] != "paused":
            return jsonify({
                "ok": False,
                "error": "La sessione non è in pausa.",
            }), 409

        paused_at = datetime.fromisoformat(session["paused_at"])

        session["paused_total_s"] += (
            now_local() - paused_at
        ).total_seconds()

        session["paused_at"] = None
        session["status"] = "running"

        append_event("session_resumed")
        autosave_if_due(force=True)

    return jsonify({
        "ok": True,
        "session": live_snapshot()["session"],
    })


@app.post("/api/session/stop")
def api_session_stop():
    with lock:
        if session["status"] not in {"running", "paused"}:
            return jsonify({
                "ok": False,
                "error": "Nessuna sessione da fermare.",
            }), 409

        final_path = stop_recording_session()

    return jsonify({
        "ok": True,
        "session": live_snapshot()["session"],
        "saved_file": final_path.name if final_path else None,
    })


def handle_shutdown_signal(signum, frame):
    raise KeyboardInterrupt


def shutdown_application(mqtt_client):
    with lock:
        if session["status"] in {"running", "paused"}:
            final_path = stop_recording_session(reason="application_shutdown")
            if final_path:
                print(f"[SESSION] Saved active session to '{final_path}'")

    if mqtt_client is not None:
        try:
            mqtt_client.disconnect()
        except Exception as error:
            print(f"[MQTT] Disconnect during shutdown failed: {error}")
        try:
            mqtt_client.loop_stop()
        except Exception as error:
            print(f"[MQTT] MQTT loop shutdown failed: {error}")


if __name__ == "__main__":
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=data_age_sampling_loop, daemon=True).start()

    mqtt_client = start_mqtt()

    print()
    print("=== Kart Performance Monitor ===")
    print(f"Setup:     http://127.0.0.1:{SERVER_CONFIG['port']}/")
    print(f"Dashboard: http://127.0.0.1:{SERVER_CONFIG['port']}/dashboard")
    print("Apri da iPhone usando l'IP locale del Mac e la stessa porta.")
    print()

    print("[STARTUP] Tracks directory:", TRACKS_DIR)
    try:
        available = list_available_tracks()
        print(f"[STARTUP] {len(available)} track(s) available:")
        for t in available:
            print(f"[STARTUP]   - id={t['id']!r} "
                  f"name={t['name']!r} "
                  f"file={t.get('file')}")
    except Exception as error:
        print(f"[STARTUP] !! Failed to list tracks: {error}")

    print()
    print("SETUP da browser: scegli driver + pista e premi START SESSION.")
    print()

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

    try:
        app.run(
            host=SERVER_CONFIG["host"],
            port=SERVER_CONFIG["port"],
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except KeyboardInterrupt:
        print("[SHUTDOWN] Received stop signal; saving active session.")
    finally:
        shutdown_application(mqtt_client)