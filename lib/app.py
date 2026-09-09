#!/usr/bin/env python3

import json
import math
import threading
import time
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


APP_CONFIG = load_yaml("../config/app.yaml")
DRIVERS_CONFIG = load_yaml("../config/drivers.yaml")
TRACK_CONFIG = load_yaml("../config/tracks/prima-pista.yaml")

SERVER_CONFIG = APP_CONFIG["server"]
MQTT_CONFIG = APP_CONFIG["mqtt"]
SESSION_CONFIG = APP_CONFIG["session"]
DASHBOARD_CONFIG = APP_CONFIG["dashboard"]
FILTER_CONFIG = APP_CONFIG["telemetry_filter"]

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
    "last_error": None,
}

session = {}
last_autosave_epoch = 0.0


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


def sector_count():
    return len(TRACK_CONFIG.get("sectors", [])) + 1


def new_session_state():
    count = sector_count()

    return {
        "status": "idle",
        "driver": None,
        "started_at": None,
        "paused_at": None,
        "paused_total_s": 0.0,
        "ended_at": None,
        "telemetry_raw": [],
        "track_points": [],
        "events": [],
        "track_state": "pitlane",
        "previous_track_state": None,
        "pit_lane_exit_seen": False,
        "warmup_started_at": None,
        "cooldown_active": False,
        "current_lap_number": 0,
        "current_lap_start_epoch": None,
        "current_lap_start_index": None,
        "sector_started_epoch": None,
        "current_sector_number": 1,
        "current_lap_sectors_s": [],
        "current_lap_sector_statuses": [None] * count,  # NEW
        "current_lap_timer_running": False,
        "current_lap_track_points": [],
        "current_lap_distance_m": 0.0,
        "last_line_crossing_epochs": {},
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
    }


session = new_session_state()


def append_event(event_name, **data):
    event = {
        "event": event_name,
        "at": now_iso(),
        **data,
    }

    session["events"].append(event)

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

    elapsed = (
        ended - started
    ).total_seconds() - session["paused_total_s"]

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
    meters_per_deg_lon = (
        111320.0 * math.cos(math.radians(origin_lat))
    )

    return (
        (lon - origin_lon) * meters_per_deg_lon,
        (lat - origin_lat) * meters_per_deg_lat,
    )


def latlon_from_local_xy_m(x, y, origin_lat, origin_lon):
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = (
        111320.0 * math.cos(math.radians(origin_lat))
    )

    return (
        origin_lat + y / meters_per_deg_lat,
        origin_lon + x / meters_per_deg_lon,
    )


def point_to_segment_distance_m(point, segment_start, segment_end):
    origin_lat = (segment_start[0] + segment_end[0]) / 2.0
    origin_lon = (segment_start[1] + segment_end[1]) / 2.0

    px, py = local_xy_m(
        point[0],
        point[1],
        origin_lat,
        origin_lon,
    )

    ax, ay = local_xy_m(
        segment_start[0],
        segment_start[1],
        origin_lat,
        origin_lon,
    )

    bx, by = local_xy_m(
        segment_end[0],
        segment_end[1],
        origin_lat,
        origin_lon,
    )

    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay

    length_squared = abx * abx + aby * aby

    if length_squared <= 0.000001:
        return math.hypot(px - ax, py - ay), 0.0

    fraction = (
        apx * abx + apy * aby
    ) / length_squared

    clamped_fraction = max(0.0, min(1.0, fraction))

    closest_x = ax + clamped_fraction * abx
    closest_y = ay + clamped_fraction * aby

    return (
        math.hypot(px - closest_x, py - closest_y),
        clamped_fraction,
    )


def extend_line_endpoints(point_a, point_b, extension_each_side_m):
    lat_a = float(point_a["lat"])
    lon_a = float(point_a["lon"])
    lat_b = float(point_b["lat"])
    lon_b = float(point_b["lon"])

    origin_lat = (lat_a + lat_b) / 2.0
    origin_lon = (lon_a + lon_b) / 2.0

    ax, ay = local_xy_m(
        lat_a,
        lon_a,
        origin_lat,
        origin_lon,
    )

    bx, by = local_xy_m(
        lat_b,
        lon_b,
        origin_lat,
        origin_lon,
    )

    dx = bx - ax
    dy = by - ay
    length = math.hypot(dx, dy)

    if length <= 0.001:
        return (lat_a, lon_a), (lat_b, lon_b)

    unit_x = dx / length
    unit_y = dy / length

    extension = max(
        0.0,
        safe_float(extension_each_side_m, 0.0),
    )

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
    return (
        float(sample["latitude"]),
        float(sample["longitude"]),
    )


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
        safe_float(
            line_config.get("line_extension_each_side_m"),
            16.0,
        ),
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

    a0x, a0y = local_xy_m(
        line_a[0],
        line_a[1],
        origin_lat,
        origin_lon,
    )

    a1x, a1y = local_xy_m(
        line_b[0],
        line_b[1],
        origin_lat,
        origin_lon,
    )

    rx = p1x - p0x
    ry = p1y - p0y
    sx = a1x - a0x
    sy = a1y - a0y

    denominator = cross_2d(rx, ry, sx, sy)

    if abs(denominator) < 1e-7:
        return None

    qpx = a0x - p0x
    qpy = a0y - p0y

    movement_fraction = (
        cross_2d(qpx, qpy, sx, sy)
        / denominator
    )

    line_fraction = (
        cross_2d(qpx, qpy, rx, ry)
        / denominator
    )

    if not (
        0.0 <= movement_fraction <= 1.0
        and 0.0 <= line_fraction <= 1.0
    ):
        return None

    previous_epoch = float(previous_sample["received_at_epoch"])
    current_epoch = float(current_sample["received_at_epoch"])

    return {
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
            + movement_fraction
            * (current_epoch - previous_epoch)
        ),
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
        - math.sin(lat_1)
        * math.cos(lat_2)
        * math.cos(delta_lon)
    )

    return (
        math.degrees(math.atan2(x, y))
        + 360.0
    ) % 360.0


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
    return abs(
        (angle_a - angle_b + 180.0) % 360.0
        - 180.0
    )


def direction_is_valid(previous_sample, current_sample, line_config):
    reference = line_config.get("direction_reference", {})
    before = reference.get("before")
    after = reference.get("after")

    if not before or not after:
        return True

    expected_heading = bearing_between_points(before, after)

    actual_heading = movement_bearing(
        previous_sample,
        current_sample,
    )

    return (
        angular_difference_deg(
            expected_heading,
            actual_heading,
        )
        <= 100.0
    )


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
        float(start["lat"]),
        float(start["lon"]),
    ), (
        float(end["lat"]),
        float(end["lon"]),
    )


def is_inside_box(sample):
    center = TRACK_CONFIG.get("pit_box", {}).get("center", {})

    if (
        center.get("lat") is None
        or center.get("lon") is None
        or not valid_coordinates(sample)
    ):
        return False, None

    distance_m = haversine_distance_m(
        float(sample["latitude"]),
        float(sample["longitude"]),
        float(center["lat"]),
        float(center["lon"]),
    )

    radius_m = safe_float(
        TRACK_CONFIG.get("pit_box", {}).get("radius_m"),
        15.0,
    )

    return distance_m <= radius_m, distance_m


def pit_lane_position(sample):
    start, end = pit_lane_points()

    if (
        start is None
        or end is None
        or not valid_coordinates(sample)
    ):
        return None

    distance_m, fraction = point_to_segment_distance_m(
        point_from_sample(sample),
        start,
        end,
    )

    return {
        "distance_m": distance_m,
        "fraction": fraction,
    }


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
        TRACK_CONFIG.get("pit_lane", {}).get(
            "dashboard_active_distance_m"
        ),
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
            pit_position["distance_m"]
            if pit_position is not None
            else None
        ),
        "pit_lane_fraction": (
            pit_position["fraction"]
            if pit_position is not None
            else None
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

    actual_heading = movement_bearing(
        previous_sample,
        current_sample,
    )

    return (
        angular_difference_deg(
            expected_heading,
            actual_heading,
        )
        <= 75.0
    )


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

    if not is_moving_toward_pit_lane_start(
        previous_sample,
        current_sample,
    ):
        return False, current_position

    entered = (
        previous_position["fraction"] >= 0.78
        and current_position["fraction"] <= 0.78
    )

    return entered, current_position


def append_lap_point(sample, point_epoch=None):
    if not session.get("current_lap_timer_running"):
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return

    if not valid_timing_sample(sample):
        return

    epoch = (
        float(point_epoch)
        if point_epoch is not None
        else float(sample["received_at_epoch"])
    )

    lap_elapsed_s = epoch - float(lap_start_epoch)

    if lap_elapsed_s < -0.05:
        return

    point = {
        "latitude": float(sample["latitude"]),
        "longitude": float(sample["longitude"]),
        "received_at_epoch": epoch,
        "lap_elapsed_s": max(0.0, lap_elapsed_s),
        "speed_kmph": safe_float(sample.get("speed_kmph"), 0.0),
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

        elapsed_s = (
            point["lap_elapsed_s"]
            - previous["lap_elapsed_s"]
        )

        if elapsed_s <= 0.0:
            return

        if distance_m < 0.05:
            return

        session["current_lap_distance_m"] += distance_m

    point["lap_distance_m"] = round(
        session["current_lap_distance_m"],
        4,
    )

    points.append(point)


def append_crossing_point(crossing, crossing_epoch):
    if not session.get("current_lap_timer_running"):
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return

    lap_elapsed_s = (
        float(crossing_epoch)
        - float(lap_start_epoch)
    )

    if lap_elapsed_s < 0.0:
        return

    point = {
        "latitude": float(crossing["latitude"]),
        "longitude": float(crossing["longitude"]),
        "received_at_epoch": float(crossing_epoch),
        "lap_elapsed_s": lap_elapsed_s,
        "speed_kmph": 0.0,
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

    point["lap_distance_m"] = round(
        session["current_lap_distance_m"],
        4,
    )

    if (
        not points
        or point["lap_elapsed_s"]
        > float(points[-1]["lap_elapsed_s"])
    ):
        points.append(point)


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

        fraction = (
            target_distance_m - distance_a
        ) / segment_distance_m

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

    reference_points = session.get(
        "reference_lap_track_points",
        [],
    )

    current_points = session.get(
        "current_lap_track_points",
        [],
    )

    if len(reference_points) < 2 or len(current_points) < 2:
        return None

    if now_epoch is None:
        now_epoch = time.time()

    last_point = current_points[-1]

    current_distance_m = safe_float(
        last_point.get("lap_distance_m"),
        0.0,
    )

    current_sample_time_s = safe_float(
        last_point.get("lap_elapsed_s"),
        0.0,
    )

    time_after_last_sample_s = max(
        0.0,
        float(now_epoch)
        - float(last_point["received_at_epoch"]),
    )

    time_after_last_sample_s = min(
        time_after_last_sample_s,
        1.0,
    )

    current_lap_time_s = (
        current_sample_time_s
        + time_after_last_sample_s
    )

    reference_time_s = reference_time_at_distance(
        reference_points,
        current_distance_m,
    )

    if reference_time_s is None:
        return None

    delta_s = current_lap_time_s - reference_time_s

    if not math.isfinite(delta_s):
        return None

    if abs(delta_s) > 60.0:
        return None

    return round(delta_s, 3)


def update_delta_live(sample=None):
    sample_epoch = None

    if sample is not None:
        sample_epoch = sample.get("received_at_epoch")

    session["delta_live_s"] = calculate_delta_live(sample_epoch)


def abort_current_lap(reason, at_epoch):
    if (
        not session["current_lap_timer_running"]
        or session.get("current_lap_start_epoch") is None
    ):
        return

    elapsed_s = max(
        0.0,
        float(at_epoch)
        - float(session["current_lap_start_epoch"]),
    )

    aborted = {
        "lap_number": session.get("current_lap_number", 0),
        "elapsed_s": round(elapsed_s, 3),
        "reason": reason,
        "sectors_s": deepcopy(
            session.get("current_lap_sectors_s", [])
        ),
    }

    session["last_aborted_lap"] = aborted
    session["current_lap_timer_running"] = False
    session["current_lap_start_epoch"] = None
    session["current_lap_start_index"] = None
    session["sector_started_epoch"] = None
    session["current_sector_number"] = 1
    session["current_lap_track_points"] = []
    session["current_lap_distance_m"] = 0.0
    session["delta_live_s"] = None
    session["current_lap_sector_statuses"] = [None] * sector_count()  # Reset

    append_event("lap_aborted", **aborted)


def stop_timing_for_cooldown(at_epoch, sample=None):
    if session.get("current_lap_timer_running"):
        abort_current_lap(
            "cooldown_requested",
            at_epoch,
        )

    session["last_line_crossing_epochs"].pop("SF", None)

    session["previous_track_state"] = session.get(
        "track_state"
    )
    session["cooldown_active"] = True
    session["track_state"] = "cooldown"
    session["current_lap_timer_running"] = False
    session["current_lap_start_epoch"] = None
    session["current_lap_start_index"] = None
    session["sector_started_epoch"] = None
    session["current_sector_number"] = 1
    session["current_lap_sectors_s"] = [None] * sector_count()
    session["current_lap_sector_statuses"] = [None] * sector_count()
    session["current_lap_track_points"] = []
    session["current_lap_distance_m"] = 0.0
    session["delta_live_s"] = None

    event_data = {
        "reason": "mqtt_cooldown",
    }

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
            abort_current_lap(
                "entered_box",
                sample["received_at_epoch"],
            )

            set_track_state(
                "box",
                "inside_pit_box",
                sample,
            )

            append_event(
                "box_entered",
                distance_from_box_m=round(box_distance_m, 2),
            )

        return "box"

    pit_box = TRACK_CONFIG.get("pit_box", {})
    radius_m = safe_float(pit_box.get("radius_m"), 15.0)
    hysteresis_m = safe_float(
        pit_box.get("exit_hysteresis_m"),
        4.0,
    )

    if current_state == "box" and box_distance_m is not None:
        if box_distance_m < radius_m + hysteresis_m:
            return "box"

        set_track_state(
            "pitlane",
            "left_pit_box",
            sample,
        )

        append_event(
            "pit_lane_entered",
            distance_from_box_m=round(box_distance_m, 2),
        )

        session["pit_lane_exit_seen"] = False
        session["pit_lane_entry_detected_at_epoch"] = None
        session["current_sector_number"] = 1

        return "pitlane"

    if current_state == "on_track":
        detected, position = entered_pit_lane(
            previous_sample,
            sample,
        )

        if detected:
            abort_current_lap(
                "entered_pit_lane",
                sample["received_at_epoch"],
            )

            set_track_state(
                "pitlane",
                "entered_pit_lane",
                sample,
            )

            session["pit_lane_exit_seen"] = False
            session["pit_lane_entry_detected_at_epoch"] = (
                sample["received_at_epoch"]
            )
            session["current_sector_number"] = 1

            append_event(
                "pit_lane_entry_detected",
                distance_from_pit_lane_centerline_m=round(
                    position["distance_m"],
                    2,
                ),
                position_on_pit_lane=round(
                    position["fraction"],
                    3,
                ),
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


def start_new_lap(crossing_epoch, sample_index, lap_number):
    session["current_lap_number"] = lap_number
    session["current_lap_start_epoch"] = crossing_epoch
    session["current_lap_start_index"] = sample_index
    session["sector_started_epoch"] = crossing_epoch
    session["current_sector_number"] = 1
    session["current_lap_sectors_s"] = [None] * sector_count()
    session["current_lap_sector_statuses"] = [None] * sector_count()  # NEW
    session["current_lap_timer_running"] = True
    session["current_lap_track_points"] = []
    session["current_lap_distance_m"] = 0.0

    if not session.get("reference_lap_track_points"):
        session["delta_live_s"] = None

    append_event(
        "lap_started",
        lap_number=lap_number,
        sector_number=1,
        reference_lap_number=session.get(
            "reference_lap_number"
        ),
    )


def ideal_lap_time_s():
    values = session.get("best_sector_times_s", [])

    if not values or any(value is None for value in values):
        return None

    return round(
        sum(float(value) for value in values),
        3,
    )


def build_sector_result(
    sector_number,
    sector_time_s,
    best_sector_s,
    reference_sector_s,
):
    """
    Logica stile F1 per i colori dei settori:

    - best (fucsia):   nuovo record assoluto del settore (batte best_sector_s)
    - improved (verde): più veloce del riferimento (ultimo giro), ma non record
    - slower (giallo):  più lento o uguale al riferimento
    """

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


def close_current_sector(
    crossing_epoch,
    completed_sector_number,
):
    if (
        not session["current_lap_timer_running"]
        or session.get("current_sector_number")
        != completed_sector_number
        or session.get("sector_started_epoch") is None
    ):
        return False

    sector_time_s = max(
        0.0,
        float(crossing_epoch)
        - float(session["sector_started_epoch"]),
    )

    values = list(
        session.get("current_lap_sectors_s", [])
    )

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

    values[completed_sector_number - 1] = round(
        sector_time_s,
        3,
    )

    session["current_lap_sectors_s"] = values

    # Store status for the sector
    session["current_lap_sector_statuses"][completed_sector_number - 1] = result["status"]

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


def update_reference_lap(
    lap_number,
    lap_time_s,
    lap_points,
    lap_is_valid,
):
    if not lap_is_valid:
        return

    normalized_points = normalize_reference_lap_points(
        lap_points
    )

    if len(normalized_points) < 2:
        append_event(
            "reference_lap_ignored",
            lap_number=lap_number,
            reason="not_enough_reference_points",
        )
        return

    previous_reference_time = session.get(
        "reference_lap_time_s"
    )

    if (
        previous_reference_time is None
        or float(lap_time_s) < float(previous_reference_time)
    ):
        session["reference_lap_track_points"] = normalized_points
        session["reference_lap_time_s"] = round(
            lap_time_s,
            3,
        )
        session["reference_lap_number"] = lap_number

        append_event(
            "reference_lap_updated",
            lap_number=lap_number,
            lap_time_s=round(lap_time_s, 3),
            points=len(normalized_points),
        )


def finish_current_lap(
    crossing_epoch,
    sample_index,
    finish_crossing,
):
    if not session["current_lap_timer_running"]:
        return

    expected_final_sector = sector_count()

    if (
        session.get("current_sector_number")
        != expected_final_sector
    ):
        append_event(
            "finish_cross_ignored",
            reason="missing_or_out_of_order_sectors",
            expected_sector_number=session.get(
                "current_sector_number"
            ),
        )
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return

    lap_time_s = (
        float(crossing_epoch)
        - float(lap_start_epoch)
    )

    minimum_lap_time_s = safe_float(
        TRACK_CONFIG.get("timing", {}).get(
            "minimum_lap_time_s"
        ),
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

    append_crossing_point(
        finish_crossing,
        crossing_epoch,
    )

    if not close_current_sector(
        crossing_epoch,
        expected_final_sector,
    ):
        return

    lap_number = session.get("current_lap_number", 0)
    sectors_s = list(session["current_lap_sectors_s"])

    lap_is_valid = all(
        value is not None
        for value in sectors_s
    )

    lap_data = {
        "lap_number": lap_number,
        "lap_time_s": round(lap_time_s, 3),
        "valid": lap_is_valid,
        "sectors_s": sectors_s,
        "started_at_epoch": lap_start_epoch,
        "ended_at_epoch": crossing_epoch,
        "start_sample_index": session.get(
            "current_lap_start_index"
        ),
        "end_sample_index": sample_index,
        "track_points": len(
            session["current_lap_track_points"]
        ),
        "lap_distance_m": round(
            session["current_lap_distance_m"],
            2,
        ),
    }

    session["laps"].append(lap_data)
    session["last_completed_lap_time_s"] = (
        lap_data["lap_time_s"]
    )
    session["last_completed_lap_at_epoch"] = time.time()

    session["last_completed_lap_sectors_s"] = list(sectors_s)

    if lap_is_valid:
        best_lap = session.get("best_lap_time_s")

        if (
            best_lap is None
            or lap_time_s < float(best_lap)
        ):
            session["best_lap_time_s"] = round(
                lap_time_s,
                3,
            )

    completed_points = deepcopy(
        session["current_lap_track_points"]
    )

    update_reference_lap(
        lap_number=lap_number,
        lap_time_s=lap_time_s,
        lap_points=completed_points,
        lap_is_valid=lap_is_valid,
    )

    append_event(
        "lap_completed",
        lap_number=lap_data["lap_number"],
        lap_time_s=lap_data["lap_time_s"],
        valid=lap_data["valid"],
        sectors_s=sectors_s,
        track_points=lap_data["track_points"],
        lap_distance_m=lap_data["lap_distance_m"],
    )

    start_new_lap(
        crossing_epoch,
        sample_index,
        lap_number + 1,
    )


def crossing_allowed(line_id, crossing_epoch):
    cooldown_s = safe_float(
        TRACK_CONFIG.get("timing", {}).get(
            "crossing_cooldown_s"
        ),
        12.0,
    )

    previous_epoch = session[
        "last_line_crossing_epochs"
    ].get(line_id)

    if (
        previous_epoch is not None
        and float(crossing_epoch)
        - float(previous_epoch)
        < cooldown_s
    ):
        return False

    session["last_line_crossing_epochs"][line_id] = (
        crossing_epoch
    )

    return True


def advance_warmup_sector(completed_sector_number):
    total_sectors = sector_count()
    next_sector = completed_sector_number + 1

    if next_sector > total_sectors:
        next_sector = 1

    session["current_sector_number"] = next_sector


def process_timing(sample):
    if len(session["telemetry_raw"]) < 2:
        return

    previous = session["telemetry_raw"][-2]

    state = update_position_state(previous, sample)

    if (
        state == "box"
        or not valid_timing_sample(previous)
        or not valid_timing_sample(sample)
    ):
        return

    minimum_speed_kmph = safe_float(
        TRACK_CONFIG.get("timing", {}).get(
            "crossing_minimum_speed_kmph"
        ),
        5.0,
    )

    if (
        safe_float(sample.get("speed_kmph"), 0.0)
        < minimum_speed_kmph
    ):
        return

    sample_index = len(session["telemetry_raw"]) - 1
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
            mark_warmup(
                sample,
                "reached_pit_lane_end",
            )

        for split_index, sector_config in enumerate(sectors):
            crossing = line_crossing_event(
                previous,
                sample,
                sector_config,
            )

            if crossing is None:
                continue

            line_id = sector_config.get(
                "id",
                f"S{split_index + 1}",
            )

            crossing_epoch = float(
                crossing["crossing_epoch"]
            )

            if not crossing_allowed(
                line_id,
                crossing_epoch,
            ):
                continue

            current_state = session.get("track_state")
            completed_sector_number = split_index + 1

            if current_state in {"pitlane", "box"}:
                mark_warmup(
                    sample,
                    f"crossed_{line_id}",
                )
                return

            if current_state == "warmup":
                advance_warmup_sector(
                    completed_sector_number
                )
                return

            if current_state == "on_track":
                expected_sector_number = session.get(
                    "current_sector_number"
                )

                if expected_sector_number != completed_sector_number:
                    append_event(
                        "sector_cross_ignored",
                        crossed_split=line_id,
                        expected_sector_number=(
                            expected_sector_number
                        ),
                    )
                    return

                append_crossing_point(
                    crossing,
                    crossing_epoch,
                )

                if close_current_sector(
                    crossing_epoch,
                    completed_sector_number,
                ):
                    if completed_sector_number < sector_count():
                        session["current_sector_number"] = (
                            completed_sector_number + 1
                        )

                return

    finish_crossing = line_crossing_event(
        previous,
        sample,
        start_finish,
    )

    if finish_crossing is None:
        return

    if not direction_is_valid(
        previous,
        sample,
        start_finish,
    ):
        append_event(
            "finish_cross_ignored",
            reason="wrong_direction",
        )
        return

    crossing_epoch = float(
        finish_crossing["crossing_epoch"]
    )

    current_state = session.get("track_state")

    if current_state == "cooldown":
        session["last_line_crossing_epochs"]["SF"] = (
            crossing_epoch
        )

        resume_timing_after_cooldown(sample)

        start_new_lap(
            crossing_epoch,
            sample_index,
            session.get("current_lap_number", 0) + 1,
        )
        return

    if not crossing_allowed("SF", crossing_epoch):
        return

    if current_state == "warmup":
        session["current_sector_number"] = 1

    if current_state in {"pitlane", "warmup"}:
        set_track_state(
            "on_track",
            "crossed_start_finish",
            sample,
        )

        session["warmup_started_at"] = None

        start_new_lap(
            crossing_epoch,
            sample_index,
            1,
        )
        return

    if current_state == "on_track":
        finish_current_lap(
            crossing_epoch,
            sample_index,
            finish_crossing,
        )


def decide_track_point(sample):
    if not valid_coordinates(sample):
        return False, "invalid_or_missing_fix", None

    if not session["track_points"]:
        return True, None, None

    previous = session["track_points"][-1]

    distance_m = haversine_distance_m(
        previous["latitude"],
        previous["longitude"],
        sample["latitude"],
        sample["longitude"],
    )

    elapsed_s = max(
        0.0,
        sample["received_at_epoch"]
        - previous["received_at_epoch"],
    )

    speed_kmph = safe_float(sample.get("speed_kmph"), 0.0)

    stationary_speed_kmph = safe_float(
        FILTER_CONFIG.get("stationary_speed_kmph"),
        2.0,
    )

    duplicate_distance_m = safe_float(
        FILTER_CONFIG.get("duplicate_distance_m"),
        0.75,
    )

    moving_min_distance_m = safe_float(
        FILTER_CONFIG.get("moving_min_distance_m"),
        1.5,
    )

    stationary_keepalive_s = safe_float(
        FILTER_CONFIG.get("stationary_keepalive_s"),
        5.0,
    )

    max_track_gap_s = safe_float(
        FILTER_CONFIG.get("max_track_gap_s"),
        1.0,
    )

    if elapsed_s >= max_track_gap_s:
        return True, "saved_after_track_gap", distance_m

    if speed_kmph < stationary_speed_kmph:
        if elapsed_s >= stationary_keepalive_s:
            return True, "stationary_keepalive", distance_m

        if distance_m < duplicate_distance_m:
            return False, "stationary_duplicate", distance_m

        return True, "stationary_position_changed", distance_m

    if distance_m >= moving_min_distance_m:
        return True, None, distance_m

    return False, "moving_point_too_close", distance_m


def normalise_payload(payload):
    return {
        "latitude": safe_float(payload.get("latitude")),
        "longitude": safe_float(payload.get("longitude")),
        "speed_kmph": safe_float(
            payload.get("speed_kmph")
        ),
        "satellites_used": safe_int(
            payload.get("satellites_used")
        ),
        "fix_valid": bool(payload.get("fix_valid", False)),
        "fix_quality": safe_int(
            payload.get("fix_quality")
        ),
        "fix_type": safe_int(payload.get("fix_type")),
        "hdop": safe_float(payload.get("hdop"), None),
        "temperature_c": safe_float(
            payload.get("temperature_c"),
            None,
        ),
        "as5600_angle_deg": safe_float(
            payload.get("as5600_angle_deg"),
            None,
        ),
        "as5600_rpm": safe_float(
            payload.get("as5600_rpm"),
            None,
        ),
        "as5600_magnet_ok": bool(
            payload.get("as5600_magnet_ok", False)
        ),
        "ir_rpm": safe_int(
            payload.get("ir_rpm"),
            None,
        ),
        "ir_total_pulses": safe_int(
            payload.get("ir_total_pulses"),
            None,
        ),
    }


def serializable_session():
    raw = session["telemetry_raw"]
    track_points = session["track_points"]

    return {
        "schema_version": 10,
        "saved_at": now_iso(),
        "status": session["status"],
        "driver": deepcopy(session["driver"]),
        "track": deepcopy(TRACK_CONFIG.get("track", {})),
        "track_config": deepcopy(TRACK_CONFIG),
        "started_at": session["started_at"],
        "paused_at": session["paused_at"],
        "paused_total_s": round(
            session["paused_total_s"],
            3,
        ),
        "ended_at": session["ended_at"],
        "session_elapsed_s": round(
            session_elapsed_s(),
            3,
        ),
        "summary": {
            "raw_samples_received": len(raw),
            "track_points_saved": len(track_points),
            "valid_gps_samples": sum(
                1
                for item in raw
                if item.get("fix_valid") is True
            ),
            "filtered_out_samples": sum(
                1
                for item in raw
                if not item.get("track_point_saved")
            ),
        },
        "telemetry_raw": deepcopy(raw),
        "track_points": deepcopy(track_points),
        "events": deepcopy(session["events"]),
        "laps": deepcopy(session["laps"]),
        "best_lap_time_s": session["best_lap_time_s"],
        "best_sector_times_s": deepcopy(
            session["best_sector_times_s"]
        ),
        "ideal_lap_time_s": session["ideal_lap_time_s"],
        "track_state": session["track_state"],
        "cooldown_active": session["cooldown_active"],
        "current_lap_number": session[
            "current_lap_number"
        ],
        "current_sector_number": session[
            "current_sector_number"
        ],
        "current_lap_sectors_s": deepcopy(
            session["current_lap_sectors_s"]
        ),
        "current_lap_sector_statuses": deepcopy(
            session.get("current_lap_sector_statuses", [])
        ),
        "current_lap_distance_m": round(
            session["current_lap_distance_m"],
            3,
        ),
        "delta_live_s": session["delta_live_s"],
        "reference_lap_number": session[
            "reference_lap_number"
        ],
        "reference_lap_time_s": session[
            "reference_lap_time_s"
        ],
        "reference_lap_points": len(
            session["reference_lap_track_points"]
        ),
        "last_sector_result": deepcopy(
            session["last_sector_result"]
        ),
        "last_aborted_lap": deepcopy(
            session["last_aborted_lap"]
        ),
    }


def write_json_atomic(path, data):
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_path.replace(path)


def autosave_if_due(force=False):
    global last_autosave_epoch

    if session["status"] not in {"running", "paused"}:
        return

    now_epoch = time.time()

    interval_s = safe_float(
        SESSION_CONFIG.get("autosave_interval_s"),
        10.0,
    )

    if (
        not force
        and now_epoch - last_autosave_epoch < interval_s
    ):
        return

    write_json_atomic(
        AUTOSAVE_PATH,
        serializable_session(),
    )

    last_autosave_epoch = now_epoch


def reset_session():
    global session

    session = new_session_state()

    if AUTOSAVE_PATH.exists():
        AUTOSAVE_PATH.unlink()

    if EVENTS_PATH.exists():
        EVENTS_PATH.unlink()


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
        "distance_from_box_center_m": (
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
            TRACK_CONFIG.get("pit_box", {}).get(
                "radius_m"
            ),
            15.0,
        ),
    }


def live_snapshot():
    with lock:
        gps = deepcopy(latest_sensors)
        runtime_snapshot = deepcopy(runtime)

        last_message_epoch = runtime_snapshot.get(
            "mqtt_last_message_epoch",
            0.0,
        )

        runtime_snapshot["data_age_s"] = (
            round(
                time.time() - last_message_epoch,
                3,
            )
            if last_message_epoch
            else None
        )

        now_epoch = time.time()

        current_lap_time_s = None
        current_sector_elapsed_s = None

        if (
            session["status"] == "running"
            and session.get("current_lap_timer_running")
            and session.get("current_lap_start_epoch")
            is not None
        ):
            current_lap_time_s = round(
                now_epoch
                - float(session["current_lap_start_epoch"]),
                3,
            )

        if (
            session["status"] == "running"
            and session.get("current_lap_timer_running")
            and session.get("sector_started_epoch")
            is not None
            and session.get("current_sector_number")
            is not None
        ):
            current_sector_elapsed_s = round(
                now_epoch
                - float(session["sector_started_epoch"]),
                3,
            )

        ideal_time = (
            session.get("ideal_lap_time_s")
            or ideal_lap_time_s()
        )

        delta_live_s = calculate_delta_live(now_epoch)
        session["delta_live_s"] = delta_live_s

        display_lap_time_s = current_lap_time_s

        if (
            session.get("last_completed_lap_at_epoch")
            is not None
            and session.get("last_completed_lap_time_s")
            is not None
            and now_epoch
            - float(
                session["last_completed_lap_at_epoch"]
            )
            < 1.5
        ):
            display_lap_time_s = session[
                "last_completed_lap_time_s"
            ]

        last_lap = (
            session["laps"][-1]
            if session["laps"]
            else None
        )

        return {
            "gps": gps,
            "location": get_location_status(gps),
            "runtime": runtime_snapshot,
            "session": {
                "status": session["status"],
                "driver": deepcopy(session["driver"]),
                "started_at": session["started_at"],
                "paused_at": session["paused_at"],
                "session_elapsed_s": round(
                    session_elapsed_s(),
                    3,
                ),
                "samples_recorded": len(
                    session["telemetry_raw"]
                ),
                "track_points_recorded": len(
                    session["track_points"]
                ),
                "track_state": session["track_state"],
                "cooldown_active": session["cooldown_active"],
                "current_lap_number": session[
                    "current_lap_number"
                ],
                "current_lap_time_s": current_lap_time_s,
                "display_lap_time_s": display_lap_time_s,
                "current_lap_timer_running": session[
                    "current_lap_timer_running"
                ],
                "current_sector_number": session[
                    "current_sector_number"
                ],
                "current_sector_elapsed_s": (
                    current_sector_elapsed_s
                ),
                "current_lap_sectors_s": deepcopy(
                    session["current_lap_sectors_s"]
                ),
                "current_lap_sector_statuses": deepcopy(
                    session.get("current_lap_sector_statuses", [])
                ),
                "current_lap_distance_m": round(
                    session["current_lap_distance_m"],
                    3,
                ),
                "best_sector_times_s": deepcopy(
                    session["best_sector_times_s"]
                ),
                "ideal_lap_time_s": ideal_time,
                "delta_live_s": delta_live_s,
                "reference_lap_number": session[
                    "reference_lap_number"
                ],
                "reference_lap_time_s": session[
                    "reference_lap_time_s"
                ],
                "reference_lap_points": len(
                    session["reference_lap_track_points"]
                ),
                "last_sector_result": deepcopy(
                    session["last_sector_result"]
                ),
                "last_lap": deepcopy(last_lap),
                "last_aborted_lap": deepcopy(
                    session["last_aborted_lap"]
                ),
                "best_lap_time_s": session[
                    "best_lap_time_s"
                ],
                "last_completed_lap_time_s": session[
                    "last_completed_lap_time_s"
                ],
                "warmup_started_at": session[
                    "warmup_started_at"
                ],
            },
            "track": deepcopy(
                TRACK_CONFIG.get("track", {})
            ),
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
                    and now_epoch - timestamp
                    > SENSOR_OFFLINE_TIMEOUT_S
                ):
                    sensor_status[component] = False


def on_mqtt_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None,
):
    client.subscribe(MQTT_CONFIG["topic"])
    client.subscribe("sensors2mqtt-glo2/esp32/status/#")

    with lock:
        runtime["mqtt_connected"] = True
        runtime["last_error"] = None
        last_seen["mqtt"] = time.time()
        sensor_status["mqtt"] = True

    print("[MQTT] Connected. Subscribed to topics")


def on_mqtt_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):
    with lock:
        runtime["mqtt_connected"] = False
        runtime["last_error"] = (
            f"MQTT disconnected: {reason_code}"
        )
        sensor_status["mqtt"] = False

    print(f"[MQTT] Disconnected: {reason_code}")


def on_mqtt_message(client, userdata, message):
    try:
        now_epoch = time.time()
        topic = message.topic

        if topic.startswith(
            "sensors2mqtt-glo2/esp32/status/"
        ):
            if message.retain:
                return

            payload = json.loads(
                message.payload.decode(
                    "utf-8",
                    errors="replace",
                )
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
            message.payload.decode(
                "utf-8",
                errors="replace",
            )
        )

        if not isinstance(payload, dict):
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
                or sample.get("as5600_angle_deg")
                is not None
            ):
                last_seen["as5600"] = now_epoch
                sensor_status["as5600"] = True

            if sample.get("ir_rpm") is not None:
                last_seen["ir_rpm"] = now_epoch
                sensor_status["ir_rpm"] = True

            if (
                sample.get("latitude")
                and sample.get("longitude")
            ):
                last_seen["gps"] = now_epoch
                sensor_status["gps"] = True

            runtime["mqtt_last_message_at"] = sample[
                "received_at"
            ]

            runtime["mqtt_last_message_epoch"] = sample[
                "received_at_epoch"
            ]

            runtime["mqtt_topic"] = topic
            runtime["mqtt_messages"] += 1

            if session["status"] != "running":
                return

            sample["session_elapsed_s"] = round(
                session_elapsed_s(),
                3,
            )

            if cooldown_requested:
                stop_timing_for_cooldown(
                    now_epoch,
                    sample,
                )

            should_save, reason, distance_m = (
                decide_track_point(sample)
            )

            sample["track_point_saved"] = should_save

            sample["discard_reason"] = (
                None if should_save else reason
            )

            sample[
                "distance_from_previous_track_point_m"
            ] = (
                round(distance_m, 3)
                if distance_m is not None
                else None
            )

            session["telemetry_raw"].append(
                deepcopy(sample)
            )

            process_timing(sample)

            if (
                session.get("track_state") == "on_track"
                and session.get("current_lap_timer_running")
                and valid_timing_sample(sample)
            ):
                append_lap_point(sample)

            update_delta_live(sample)

            sample["track_state"] = session["track_state"]

            sample["current_sector_number"] = session.get(
                "current_sector_number"
            )

            sample["delta_live_s"] = session.get(
                "delta_live_s"
            )

            sample["current_lap_distance_m"] = round(
                session.get("current_lap_distance_m", 0.0),
                3,
            )

            if should_save:
                track_point = deepcopy(sample)
                track_point["track_save_reason"] = reason

                session["track_points"].append(track_point)

            autosave_if_due()

    except Exception as error:
        with lock:
            runtime["last_error"] = str(error)

        print(f"[MQTT] Message error: {error}")


def start_mqtt():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2
    )

    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message

    client.reconnect_delay_set(
        min_delay=1,
        max_delay=30,
    )

    client.connect_async(
        MQTT_CONFIG["broker"],
        MQTT_CONFIG["port"],
        MQTT_CONFIG.get("keepalive_s", 60),
    )

    client.loop_start()

    return client


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
}

.topbar-left,
.topbar-right{
    display:flex;
    align-items:center;
}

.topbar-left{
    gap:18px;
    margin-left:10px;
}

.topbar-right{
    gap:13px;
    margin-right:10px;
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
        gap:8px;
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
            <span>
                SAT:
                <span id="sat"></span>
            </span>

            <span>
                FIX:
                <span id="fix"></span>
            </span>
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

    // Reset classes
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
        // Neutral: no special background
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

        // Reset status classes
        badge.classList.remove("best", "improved", "slower");
        timeSpan.classList.remove("best", "improved", "slower");

        // Apply active state
        const shouldBeActive =
            (
                normalizedState === "warmup"
                || normalizedState === "on_track"
            )
            && numericSector === sectorNumber;

        badge.classList.toggle("active", shouldBeActive);

        // Apply sector status color only if available (completed sector)
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

        document.getElementById("sat").textContent =
            data.sat;

        document.getElementById("fix").textContent =
            data.fix;

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


@app.get("/")
def dashboard_page():
    return render_template_string(HTML)


@app.get("/api/telemetry")
def api_telemetry():
    with lock:
        speed = latest_sensors.get("speed_kmph", 0.0)

        rpm = latest_sensors.get("ir_rpm")

        if rpm is None:
            rpm = latest_sensors.get(
                "as5600_rpm",
                0,
            )

        temp = latest_sensors.get("temperature_c", 20.0)

        now_epoch = time.time()

        delta = calculate_delta_live(now_epoch)
        session["delta_live_s"] = delta

        sectors = list(
            session.get("current_lap_sectors_s", [])
        )

        while len(sectors) < sector_count():
            sectors.append(None)

        # Live sector time for the current sector if timer is running
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

        # Ensure at least 3 entries for the frontend (assuming 3 sectors)
        while len(sectors) < 3:
            sectors.append(None)

        best_lap = session.get("best_lap_time_s")

        last_lap = session.get(
            "last_completed_lap_time_s"
        )

        best_sectors = session.get(
            "best_sector_times_s",
            [],
        )

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

        circuit = TRACK_CONFIG.get(
            "track",
            {},
        ).get("name", "Unknown Track")

        driver_name = (
            session.get("driver", {}).get(
                "name",
                "Unknown Driver",
            )
            if session.get("driver")
            else "Unknown Driver"
        )

        satellites = latest_sensors.get(
            "satellites_used",
            0,
        )

        fix_valid = latest_sensors.get("fix_valid", False)
        fix_type = latest_sensors.get("fix_type", 0)
        if not fix_valid:
            fix_label = "No Fix"
        else:
            fix_label = FIX_TYPE_LABELS.get(
                fix_type,
                f"T{fix_type}",
            )

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

        lap_count = len(session.get("laps", []))

        # Directly use stored statuses for completed sectors
        sector_statuses = list(session.get("current_lap_sector_statuses", []))
        while len(sector_statuses) < 3:
            sector_statuses.append(None)

        return jsonify({
            "speed": int(speed) if speed else 0,
            "rpm": int(rpm) if rpm else 0,
            "temp": temp,
            "delta": delta,
            "sectors": sectors[:3],
            "sector_statuses": sector_statuses[:3],
            "current_sector_number": session.get(
                "current_sector_number"
            ),
            "best_lap": best_lap,
            "last_lap": last_lap,
            "ideal_lap": ideal_lap,
            "current_lap_time_s": current_lap_time_s,
            "reference_lap_number": session.get(
                "reference_lap_number"
            ),
            "reference_lap_time_s": session.get(
                "reference_lap_time_s"
            ),
            "current_lap_distance_m": round(
                session.get("current_lap_distance_m", 0.0),
                3,
            ),
            "circuit": circuit,
            "driver": driver_name,
            "sat": satellites,
            "fix": fix_label,
            "track_state": session.get(
                "track_state",
                "pitlane",
            ),
            "cooldown_active": session.get(
                "cooldown_active",
                False,
            ),
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
    return jsonify({
        "drivers": drivers,
    })


@app.post("/api/session/start")
def api_session_start():
    body = request.get_json(silent=True) or {}
    driver = get_driver(body.get("driver_id"))

    if not driver:
        return jsonify({
            "ok": False,
            "error": "Pilota non valido.",
        }), 400

    with lock:
        if session["status"] in {"running", "paused"}:
            return jsonify({
                "ok": False,
                "error": "Una sessione è già attiva.",
            }), 409

        reset_session()

        session["status"] = "running"
        session["driver"] = deepcopy(driver)
        session["started_at"] = now_iso()

        append_event(
            "session_started",
            driver_id=driver["id"],
            driver_name=driver["name"],
        )

        autosave_if_due(force=True)

    return jsonify({
        "ok": True,
        "session": live_snapshot()["session"],
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

        paused_at = datetime.fromisoformat(
            session["paused_at"]
        )

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

        if (
            session["status"] == "paused"
            and session["paused_at"]
        ):
            paused_at = datetime.fromisoformat(
                session["paused_at"]
            )

            session["paused_total_s"] += (
                now_local() - paused_at
            ).total_seconds()

            session["paused_at"] = None

        session["ended_at"] = now_iso()
        session["status"] = "stopped"

        append_event("session_stopped")

        final_data = serializable_session()

        driver_id = (
            session["driver"]["id"]
            if session["driver"]
            else "unknown"
        )

        track_id = TRACK_CONFIG.get(
            "track",
            {},
        ).get("id", "unknown-track")

        stamp = now_local().strftime(
            "%Y-%m-%d_%H-%M-%S"
        )

        final_path = SESSIONS_DIR / (
            f"{stamp}_{driver_id}_{track_id}.json"
        )

        write_json_atomic(final_path, final_data)
        write_json_atomic(AUTOSAVE_PATH, final_data)

    return jsonify({
        "ok": True,
        "session": live_snapshot()["session"],
        "saved_file": final_path.name,
    })


if __name__ == "__main__":
    threading.Thread(
        target=watchdog_loop,
        daemon=True,
    ).start()

    start_mqtt()

    print()
    print("=== Kart Performance Monitor ===")
    print(
        f"Dashboard: "
        f"http://127.0.0.1:{SERVER_CONFIG['port']}"
    )
    print(
        "Apri da iPhone usando l'IP locale del Mac "
        "e la stessa porta."
    )
    print()

    app.run(
        host=SERVER_CONFIG["host"],
        port=SERVER_CONFIG["port"],
        debug=False,
        use_reloader=False,
        threaded=True,
    )