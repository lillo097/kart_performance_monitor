import json
import math
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml
from flask import Flask, jsonify, render_template, request


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

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR.parent / "templates"),
    static_folder=str(BASE_DIR.parent / "static"),
)

lock = threading.RLock()

drivers = DRIVERS_CONFIG.get("drivers", [])

latest_gps = {
    "latitude": 0.0,
    "longitude": 0.0,
    "speed_kmph": 0.0,
    "satellites_used": 0,
    "fix_valid": False,
    "fix_quality": 0,
    "fix_type": 0,
    "hdop": None,
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
        "current_lap_number": 0,
        "current_lap_start_epoch": None,
        "current_lap_start_index": None,
        "sector_started_epoch": None,
        "current_sector_number": None,
        "current_lap_sectors_s": [],
        "current_lap_timer_running": False,
        "last_line_crossing_epochs": {},
        "laps": [],
        "best_lap_time_s": None,
        "best_sector_times_s": [None] * count,
        "last_completed_lap_time_s": None,
        "last_completed_lap_at_epoch": None,
        "last_aborted_lap": None,
        "pit_lane_entry_detected_at_epoch": None,
        "last_sector_result": None,
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

    fraction = (apx * abx + apy * aby) / length_squared
    clamped_fraction = max(0.0, min(1.0, fraction))

    closest_x = ax + clamped_fraction * abx
    closest_y = ay + clamped_fraction * aby

    return math.hypot(px - closest_x, py - closest_y), clamped_fraction


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

    start_x = ax - unit_x * extension
    start_y = ay - unit_y * extension
    end_x = bx + unit_x * extension
    end_y = by + unit_y * extension

    return (
        latlon_from_local_xy_m(
            start_x,
            start_y,
            origin_lat,
            origin_lon,
        ),
        latlon_from_local_xy_m(
            end_x,
            end_y,
            origin_lat,
            origin_lon,
        ),
    )


def cross_2d(ax, ay, bx, by):
    return ax * by - ay * bx


def line_crossing_event(previous_sample, current_sample, line_config):
    if not line_config:
        return None

    point_a = line_config.get("a")
    point_b = line_config.get("b")

    if not point_a or not point_b:
        return None

    if not valid_timing_sample(previous_sample):
        return None

    if not valid_timing_sample(current_sample):
        return None

    extension_m = safe_float(
        line_config.get("line_extension_each_side_m"),
        16.0,
    )

    line_a, line_b = extend_line_endpoints(
        point_a,
        point_b,
        extension_m,
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

    movement_fraction = cross_2d(qpx, qpy, sx, sy) / denominator
    line_fraction = cross_2d(qpx, qpy, rx, ry) / denominator

    if not 0.0 <= movement_fraction <= 1.0:
        return None

    if not 0.0 <= line_fraction <= 1.0:
        return None

    previous_epoch = float(previous_sample["received_at_epoch"])
    current_epoch = float(current_sample["received_at_epoch"])

    return {
        "latitude": float(previous_sample["latitude"])
        + movement_fraction
        * (
            float(current_sample["latitude"])
            - float(previous_sample["latitude"])
        ),
        "longitude": float(previous_sample["longitude"])
        + movement_fraction
        * (
            float(current_sample["longitude"])
            - float(previous_sample["longitude"])
        ),
        "crossing_epoch": previous_epoch
        + movement_fraction
        * (current_epoch - previous_epoch),
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

    expected = bearing_between_points(before, after)
    actual = movement_bearing(previous_sample, current_sample)

    return angular_difference_deg(expected, actual) <= 100.0


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


def pit_lane_points():
    pit_lane = TRACK_CONFIG.get("pit_lane", {})
    start = pit_lane.get("start")
    end = pit_lane.get("end")

    if not start or not end:
        return None, None

    if start.get("lat") is None or start.get("lon") is None:
        return None, None

    if end.get("lat") is None or end.get("lon") is None:
        return None, None

    return (
        (float(start["lat"]), float(start["lon"])),
        (float(end["lat"]), float(end["lon"])),
    )


def is_inside_box(sample):
    pit_box = TRACK_CONFIG.get("pit_box", {})
    center = pit_box.get("center", {})

    if center.get("lat") is None or center.get("lon") is None:
        return False, None

    if not valid_coordinates(sample):
        return False, None

    distance_m = haversine_distance_m(
        float(sample["latitude"]),
        float(sample["longitude"]),
        float(center["lat"]),
        float(center["lon"]),
    )

    return (
        distance_m <= safe_float(pit_box.get("radius_m"), 15.0),
        distance_m,
    )


def pit_lane_position(sample):
    start, end = pit_lane_points()

    if start is None or end is None or not valid_coordinates(sample):
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


def is_moving_toward_pit_lane_start(previous_sample, current_sample):
    start, end = pit_lane_points()

    if start is None or end is None:
        return False

    if not valid_coordinates(previous_sample):
        return False

    if not valid_coordinates(current_sample):
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

    corridor_m = 22.0

    if (
        previous_position["distance_m"] > corridor_m
        and current_position["distance_m"] > corridor_m
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


def abort_current_lap(reason, at_epoch):
    if not session["current_lap_timer_running"]:
        return

    lap_start_epoch = session.get("current_lap_start_epoch")

    if lap_start_epoch is None:
        return

    elapsed_s = max(0.0, float(at_epoch) - float(lap_start_epoch))

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
    session["current_sector_number"] = None

    append_event("lap_aborted", **aborted)


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
        event_data["latitude"] = round(float(sample["latitude"]), 7)
        event_data["longitude"] = round(float(sample["longitude"]), 7)

    append_event("track_state_changed", **event_data)


def update_position_state(previous_sample, sample):
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
            session["pit_lane_entry_detected_at_epoch"] = sample[
                "received_at_epoch"
            ]

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

    return session.get("track_state", "pitlane")


def mark_warmup(sample, reason):
    current_state = session.get("track_state", "pitlane")

    if current_state in {"box", "pitlane"}:
        set_track_state("warmup", reason, sample)
        session["warmup_started_at"] = sample["received_at"]
        session["pit_lane_exit_seen"] = True
        append_event("warmup_started", reason=reason)


def start_new_lap(crossing_epoch, sample_index, lap_number):
    session["current_lap_number"] = lap_number
    session["current_lap_start_epoch"] = crossing_epoch
    session["current_lap_start_index"] = sample_index
    session["sector_started_epoch"] = crossing_epoch
    session["current_sector_number"] = 1
    session["current_lap_sectors_s"] = [None] * sector_count()
    session["current_lap_timer_running"] = True

    append_event(
        "lap_started",
        lap_number=lap_number,
        sector_number=1,
    )


def build_sector_result(
    sector_number,
    sector_time_s,
    reference_time_s,
):
    if reference_time_s is None:
        return {
            "sector_number": sector_number,
            "sector_time_s": round(sector_time_s, 3),
            "delta_s": None,
            "status": "best",
            "label": "BEST SETTORE",
            "completed_at_epoch": time.time(),
        }

    delta_s = sector_time_s - reference_time_s

    if delta_s < -0.001:
        return {
            "sector_number": sector_number,
            "sector_time_s": round(sector_time_s, 3),
            "delta_s": round(delta_s, 3),
            "status": "improved",
            "label": "MIGLIORATO",
            "completed_at_epoch": time.time(),
        }

    if delta_s > 0.001:
        return {
            "sector_number": sector_number,
            "sector_time_s": round(sector_time_s, 3),
            "delta_s": round(delta_s, 3),
            "status": "slower",
            "label": "PIÙ LENTO",
            "completed_at_epoch": time.time(),
        }

    return {
        "sector_number": sector_number,
        "sector_time_s": round(sector_time_s, 3),
        "delta_s": 0.0,
        "status": "equal",
        "label": "UGUALE",
        "completed_at_epoch": time.time(),
    }


def close_current_sector(crossing_epoch, completed_sector_number):
    if not session["current_lap_timer_running"]:
        return False

    if session.get("current_sector_number") != completed_sector_number:
        return False

    sector_start_epoch = session.get("sector_started_epoch")

    if sector_start_epoch is None:
        return False

    sector_time_s = max(
        0.0,
        crossing_epoch - float(sector_start_epoch),
    )

    values = list(session.get("current_lap_sectors_s", []))

    while len(values) < sector_count():
        values.append(None)

    previous_best = session["best_sector_times_s"][
        completed_sector_number - 1
    ]

    result = build_sector_result(
        completed_sector_number,
        sector_time_s,
        previous_best,
    )

    values[completed_sector_number - 1] = round(sector_time_s, 3)
    session["current_lap_sectors_s"] = values

    if previous_best is None or sector_time_s < float(previous_best):
        session["best_sector_times_s"][
            completed_sector_number - 1
        ] = round(sector_time_s, 3)

    session["sector_started_epoch"] = crossing_epoch
    session["last_sector_result"] = result

    append_event(
        "sector_completed",
        sector_number=completed_sector_number,
        sector_time_s=round(sector_time_s, 3),
        previous_best_s=previous_best,
        delta_s=result["delta_s"],
        status=result["status"],
    )

    return True


def finish_current_lap(crossing_epoch, sample_index):
    if not session["current_lap_timer_running"]:
        return

    expected_final_sector = sector_count()

    if session.get("current_sector_number") != expected_final_sector:
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

    lap_time_s = crossing_epoch - float(lap_start_epoch)

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

    if not close_current_sector(
        crossing_epoch,
        expected_final_sector,
    ):
        return

    lap_number = session.get("current_lap_number", 0)
    sectors_s = list(session["current_lap_sectors_s"])

    lap_data = {
        "lap_number": lap_number,
        "lap_time_s": round(lap_time_s, 3),
        "valid": all(value is not None for value in sectors_s),
        "sectors_s": sectors_s,
        "started_at_epoch": lap_start_epoch,
        "ended_at_epoch": crossing_epoch,
        "start_sample_index": session.get(
            "current_lap_start_index"
        ),
        "end_sample_index": sample_index,
    }

    session["laps"].append(lap_data)
    session["last_completed_lap_time_s"] = lap_data["lap_time_s"]
    session["last_completed_lap_at_epoch"] = time.time()

    if lap_data["valid"]:
        best_lap = session.get("best_lap_time_s")

        if best_lap is None or lap_time_s < float(best_lap):
            session["best_lap_time_s"] = lap_time_s

    append_event(
        "lap_completed",
        lap_number=lap_data["lap_number"],
        lap_time_s=lap_data["lap_time_s"],
        valid=lap_data["valid"],
        sectors_s=sectors_s,
    )

    start_new_lap(
        crossing_epoch=crossing_epoch,
        sample_index=sample_index,
        lap_number=lap_number + 1,
    )


def crossing_allowed(line_id, crossing_epoch):
    cooldown_s = safe_float(
        TRACK_CONFIG.get("timing", {}).get(
            "crossing_cooldown_s"
        ),
        12.0,
    )

    previous_epoch = session["last_line_crossing_epochs"].get(
        line_id
    )

    if previous_epoch is not None:
        if crossing_epoch - float(previous_epoch) < cooldown_s:
            return False

    session["last_line_crossing_epochs"][line_id] = crossing_epoch

    return True


def process_timing(sample):
    if len(session["telemetry_raw"]) < 2:
        return

    previous = session["telemetry_raw"][-2]
    state = update_position_state(previous, sample)

    if state == "box":
        return

    if not valid_timing_sample(previous):
        return

    if not valid_timing_sample(sample):
        return

    minimum_speed_kmph = safe_float(
        TRACK_CONFIG.get("timing", {}).get(
            "crossing_minimum_speed_kmph"
        ),
        5.0,
    )

    if safe_float(sample.get("speed_kmph"), 0.0) < minimum_speed_kmph:
        return

    sample_index = len(session["telemetry_raw"]) - 1
    sectors = TRACK_CONFIG.get("sectors", [])
    start_finish = TRACK_CONFIG.get("start_finish", {})

    if not start_finish:
        return

    pit_position = pit_lane_position(sample)

    if (
        state == "pitlane"
        and pit_position is not None
        and pit_position["distance_m"] <= 20.0
        and pit_position["fraction"] >= 0.78
    ):
        mark_warmup(sample, "reached_pit_lane_end")

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

        crossing_epoch = float(crossing["crossing_epoch"])

        if not crossing_allowed(line_id, crossing_epoch):
            continue

        current_state = session.get("track_state")

        if current_state in {"pitlane", "box"}:
            mark_warmup(sample, f"crossed_{line_id}")
            return

        if current_state == "warmup":
            return

        if current_state == "on_track":
            completed_sector_number = split_index + 1
            expected_sector_number = session.get(
                "current_sector_number"
            )

            if expected_sector_number != completed_sector_number:
                append_event(
                    "sector_cross_ignored",
                    crossed_split=line_id,
                    expected_sector_number=expected_sector_number,
                )
                return

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

    if not direction_is_valid(previous, sample, start_finish):
        append_event(
            "finish_cross_ignored",
            reason="wrong_direction",
        )
        return

    crossing_epoch = float(finish_crossing["crossing_epoch"])

    if not crossing_allowed("SF", crossing_epoch):
        return

    current_state = session.get("track_state")

    if current_state in {"pitlane", "warmup"}:
        set_track_state(
            "on_track",
            "crossed_start_finish",
            sample,
        )

        session["warmup_started_at"] = None

        start_new_lap(
            crossing_epoch=crossing_epoch,
            sample_index=sample_index,
            lap_number=1,
        )

        return

    if current_state == "on_track":
        finish_current_lap(
            crossing_epoch=crossing_epoch,
            sample_index=sample_index,
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
        "speed_kmph": safe_float(payload.get("speed_kmph")),
        "satellites_used": safe_int(payload.get("satellites_used")),
        "fix_valid": bool(payload.get("fix_valid", False)),
        "fix_quality": safe_int(payload.get("fix_quality")),
        "fix_type": safe_int(payload.get("fix_type")),
        "hdop": safe_float(payload.get("hdop"), None),
        "simulated": bool(payload.get("simulated", False)),
        "simulation_phase": payload.get("simulation_phase"),
    }


def serializable_session():
    raw = session["telemetry_raw"]
    track_points = session["track_points"]

    return {
        "schema_version": 5,
        "saved_at": now_iso(),
        "status": session["status"],
        "driver": deepcopy(session["driver"]),
        "track": deepcopy(TRACK_CONFIG.get("track", {})),
        "track_config": deepcopy(TRACK_CONFIG),
        "started_at": session["started_at"],
        "paused_at": session["paused_at"],
        "paused_total_s": round(session["paused_total_s"], 3),
        "ended_at": session["ended_at"],
        "session_elapsed_s": round(session_elapsed_s(), 3),
        "summary": {
            "raw_samples_received": len(raw),
            "track_points_saved": len(track_points),
            "valid_gps_samples": sum(
                1 for item in raw if item.get("fix_valid") is True
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
        "track_state": session["track_state"],
        "current_lap_number": session["current_lap_number"],
        "current_sector_number": session[
            "current_sector_number"
        ],
        "current_lap_sectors_s": deepcopy(
            session["current_lap_sectors_s"]
        ),
        "last_sector_result": deepcopy(
            session["last_sector_result"]
        ),
        "last_aborted_lap": deepcopy(
            session["last_aborted_lap"]
        ),
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
        SESSION_CONFIG.get("autosave_interval_s"),
        10.0,
    )

    if not force and now_epoch - last_autosave_epoch < interval_s:
        return

    write_json_atomic(AUTOSAVE_PATH, serializable_session())
    last_autosave_epoch = now_epoch


def reset_session():
    global session

    session = new_session_state()

    if AUTOSAVE_PATH.exists():
        AUTOSAVE_PATH.unlink()

    if EVENTS_PATH.exists():
        EVENTS_PATH.unlink()


def get_location_status(gps):
    if not valid_coordinates(gps):
        return {
            "status": "unknown",
            "distance_from_box_center_m": None,
        }

    inside_box, distance_m = is_inside_box(gps)

    return {
        "status": "box" if inside_box else "pit_lane",
        "distance_from_box_center_m": round(distance_m, 2)
        if distance_m is not None
        else None,
        "box_radius_m": safe_float(
            TRACK_CONFIG.get("pit_box", {}).get("radius_m"),
            15.0,
        ),
    }


def live_snapshot():
    with lock:
        gps = deepcopy(latest_gps)
        runtime_snapshot = deepcopy(runtime)

        last_message_epoch = runtime_snapshot.get(
            "mqtt_last_message_epoch",
            0.0,
        )

        runtime_snapshot["data_age_s"] = (
            round(time.time() - last_message_epoch, 3)
            if last_message_epoch
            else None
        )

        current_lap_time_s = None
        current_sector_elapsed_s = None

        if (
            session["status"] == "running"
            and session.get("current_lap_timer_running")
            and session.get("current_lap_start_epoch") is not None
        ):
            current_lap_time_s = round(
                time.time()
                - float(session["current_lap_start_epoch"]),
                3,
            )

        if (
            session["status"] == "running"
            and session.get("current_lap_timer_running")
            and session.get("sector_started_epoch") is not None
            and session.get("current_sector_number") is not None
        ):
            current_sector_elapsed_s = round(
                time.time()
                - float(session["sector_started_epoch"]),
                3,
            )

        display_lap_time_s = current_lap_time_s

        if (
            session.get("last_completed_lap_at_epoch") is not None
            and session.get("last_completed_lap_time_s") is not None
            and time.time()
            - float(session["last_completed_lap_at_epoch"])
            < 1.5
        ):
            display_lap_time_s = session[
                "last_completed_lap_time_s"
            ]

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
                "session_elapsed_s": round(
                    session_elapsed_s(),
                    3,
                ),
                "samples_recorded": len(session["telemetry_raw"]),
                "track_points_recorded": len(
                    session["track_points"]
                ),
                "track_state": session["track_state"],
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
                "best_sector_times_s": deepcopy(
                    session["best_sector_times_s"]
                ),
                "last_sector_result": deepcopy(
                    session["last_sector_result"]
                ),
                "last_lap": deepcopy(last_lap),
                "last_aborted_lap": deepcopy(
                    session["last_aborted_lap"]
                ),
                "best_lap_time_s": session["best_lap_time_s"],
                "last_completed_lap_time_s": session[
                    "last_completed_lap_time_s"
                ],
                "warmup_started_at": session[
                    "warmup_started_at"
                ],
            },
            "track": deepcopy(TRACK_CONFIG.get("track", {})),
            "dashboard": deepcopy(DASHBOARD_CONFIG),
        }


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe(MQTT_CONFIG["topic"])

    with lock:
        runtime["mqtt_connected"] = True
        runtime["last_error"] = None

    print(f"[MQTT] Connected. Subscribed to {MQTT_CONFIG['topic']}")


def on_mqtt_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None,
):
    with lock:
        runtime["mqtt_connected"] = False
        runtime["last_error"] = f"MQTT disconnected: {reason_code}"

    print(f"[MQTT] Disconnected: {reason_code}")


def on_mqtt_message(client, userdata, message):
    try:
        payload = json.loads(
            message.payload.decode("utf-8", errors="replace")
        )

        if not isinstance(payload, dict):
            return

        sample = normalise_payload(payload)
        sample["received_at"] = now_iso()
        sample["received_at_epoch"] = time.time()
        sample["topic"] = message.topic

        with lock:
            latest_gps.update(sample)

            runtime["mqtt_last_message_at"] = sample["received_at"]
            runtime["mqtt_last_message_epoch"] = sample[
                "received_at_epoch"
            ]
            runtime["mqtt_topic"] = message.topic
            runtime["mqtt_messages"] += 1

            if session["status"] != "running":
                return

            sample["session_elapsed_s"] = round(
                session_elapsed_s(),
                3,
            )

            should_save, reason, distance_m = decide_track_point(sample)

            sample["track_point_saved"] = should_save
            sample["discard_reason"] = (
                None if should_save else reason
            )
            sample["distance_from_previous_track_point_m"] = (
                round(distance_m, 3)
                if distance_m is not None
                else None
            )

            session["telemetry_raw"].append(deepcopy(sample))

            process_timing(sample)

            sample["track_state"] = session["track_state"]
            sample["current_sector_number"] = session[
                "current_sector_number"
            ]

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
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message

    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.connect_async(
        MQTT_CONFIG["broker"],
        MQTT_CONFIG["port"],
        MQTT_CONFIG.get("keepalive_s", 60),
    )

    client.loop_start()

    return client


@app.get("/")
def driver_select_page():
    return render_template(
        "driver-select.html",
        drivers=drivers,
        track=TRACK_CONFIG.get("track", {}),
    )


@app.get("/dashboard")
def dashboard_page():
    return render_template(
        "dashboard.html",
        drivers=drivers,
        track=TRACK_CONFIG.get("track", {}),
    )


@app.get("/summary")
def session_summary_page():
    return render_template("session-summary.html")


@app.get("/debug")
def debug_page():
    return render_template("track-debug.html", track=TRACK_CONFIG)


@app.get("/track-map")
def track_map_debug():
    return render_template(
        "track-map-debug.html",
        track_config=TRACK_CONFIG,
    )


@app.get("/api/live")
def api_live():
    return jsonify(live_snapshot())


@app.get("/api/latest")
def api_latest_legacy():
    return jsonify(live_snapshot())


@app.get("/api/drivers")
def api_drivers():
    return jsonify({"drivers": drivers})


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

        if session["status"] == "paused" and session["paused_at"]:
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

        track_id = TRACK_CONFIG.get("track", {}).get(
            "id",
            "unknown-track",
        )

        stamp = now_local().strftime("%Y-%m-%d_%H-%M-%S")

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
    start_mqtt()

    print()
    print("=== Kart Performance Monitor ===")
    print(f"Dashboard: http://127.0.0.1:{SERVER_CONFIG['port']}")
    print("Apri da iPhone usando l'IP locale del Mac e la stessa porta.")
    print()

    app.run(
        host=SERVER_CONFIG["host"],
        port=SERVER_CONFIG["port"],
        debug=False,
        use_reloader=False,
        threaded=True,
    )
