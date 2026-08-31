#!/usr/bin/env python3

import argparse
import json
import math
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml


BASE_DIR = Path(__file__).resolve().parent
APP_CONFIG_PATH = BASE_DIR / "config" / "app.yaml"
TRACK_CONFIG_PATH = BASE_DIR / "config" / "tracks" / "prima-pista.yaml"

A = (41.273531, 13.154795)
B = (41.272110, 13.155612)
C = (41.272289, 13.156320)
D = (41.273787, 13.155719)


def load_yaml(path):
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def yaml_point(value):
    if not isinstance(value, dict):
        return None

    if value.get("lat") is None or value.get("lon") is None:
        return None

    return float(value["lat"]), float(value["lon"])


def distance_m(point_a, point_b):
    earth_radius_m = 6371000.0

    latitude_a = math.radians(point_a[0])
    longitude_a = math.radians(point_a[1])
    latitude_b = math.radians(point_b[0])
    longitude_b = math.radians(point_b[1])

    delta_latitude = latitude_b - latitude_a
    delta_longitude = longitude_b - longitude_a

    value = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_a)
        * math.cos(latitude_b)
        * math.sin(delta_longitude / 2.0) ** 2
    )

    return 2.0 * earth_radius_m * math.asin(math.sqrt(value))


def interpolate(point_a, point_b, fraction):
    return (
        point_a[0] + (point_b[0] - point_a[0]) * fraction,
        point_a[1] + (point_b[1] - point_a[1]) * fraction,
    )


def bearing(point_a, point_b):
    latitude_a = math.radians(point_a[0])
    longitude_a = math.radians(point_a[1])
    latitude_b = math.radians(point_b[0])
    longitude_b = math.radians(point_b[1])

    delta_longitude = longitude_b - longitude_a

    x = math.sin(delta_longitude) * math.cos(latitude_b)
    y = (
        math.cos(latitude_a) * math.sin(latitude_b)
        - math.sin(latitude_a)
        * math.cos(latitude_b)
        * math.cos(delta_longitude)
    )

    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination(origin, heading_deg, distance_meters):
    earth_radius_m = 6371000.0

    latitude_1 = math.radians(origin[0])
    longitude_1 = math.radians(origin[1])
    heading = math.radians(heading_deg)
    angular_distance = distance_meters / earth_radius_m

    latitude_2 = math.asin(
        math.sin(latitude_1) * math.cos(angular_distance)
        + math.cos(latitude_1)
        * math.sin(angular_distance)
        * math.cos(heading)
    )

    longitude_2 = longitude_1 + math.atan2(
        math.sin(heading)
        * math.sin(angular_distance)
        * math.cos(latitude_1),
        math.cos(angular_distance)
        - math.sin(latitude_1) * math.sin(latitude_2),
    )

    return math.degrees(latitude_2), math.degrees(longitude_2)


def midpoint(point_a, point_b):
    return (
        (point_a[0] + point_b[0]) / 2.0,
        (point_a[1] + point_b[1]) / 2.0,
    )


def crossing_pair(line_a, line_b, incoming, outgoing, offset_m=14.0):
    line_center = midpoint(line_a, line_b)
    line_heading = bearing(line_a, line_b)

    normal_1 = (line_heading + 90.0) % 360.0
    normal_2 = (line_heading - 90.0) % 360.0
    route_heading = bearing(incoming, outgoing)

    def angular_error(candidate):
        return abs((route_heading - candidate + 180.0) % 360.0 - 180.0)

    crossing_heading = (
        normal_1
        if angular_error(normal_1) <= angular_error(normal_2)
        else normal_2
    )

    before = destination(
        line_center,
        (crossing_heading + 180.0) % 360.0,
        offset_m,
    )

    after = destination(
        line_center,
        crossing_heading,
        offset_m,
    )

    return before, after


def append_straight(route, start, end, speed_kmph, hz, phase, minimum=3):
    length_m = distance_m(start, end)
    duration_s = length_m / max(speed_kmph / 3.6, 0.5)
    count = max(minimum, math.ceil(duration_s * hz))

    for index in range(1, count + 1):
        point = interpolate(start, end, index / count)

        route.append({
            "latitude": point[0],
            "longitude": point[1],
            "speed_kmph": speed_kmph,
            "phase": phase,
        })

    return end


def append_line_crossing(
    route,
    start,
    line_a,
    line_b,
    next_waypoint,
    speed_kmph,
    hz,
    phase,
):
    before, after = crossing_pair(
        line_a,
        line_b,
        start,
        next_waypoint,
    )

    append_straight(
        route,
        start,
        before,
        speed_kmph,
        hz,
        phase + "_approach",
    )

    append_straight(
        route,
        before,
        after,
        speed_kmph,
        hz,
        phase + "_cross",
        minimum=16,
    )

    append_straight(
        route,
        after,
        next_waypoint,
        speed_kmph,
        hz,
        phase + "_exit",
    )

    return next_waypoint


def get_geometry(track_config):
    start_finish = track_config.get("start_finish", {})
    sectors = track_config.get("sectors", [])
    pit_box = track_config.get("pit_box", {})
    pit_lane = track_config.get("pit_lane", {})

    if len(sectors) < 2:
        raise RuntimeError(
            "Il simulatore richiede almeno S1 e S2 nel YAML."
        )

    geometry = {
        "box": yaml_point(pit_box.get("center")),
        "pit_lane_start": yaml_point(pit_lane.get("start")),
        "pit_lane_end": yaml_point(pit_lane.get("end")),
        "sf": (
            yaml_point(start_finish.get("a")),
            yaml_point(start_finish.get("b")),
        ),
        "s1": (
            yaml_point(sectors[0].get("a")),
            yaml_point(sectors[0].get("b")),
        ),
        "s2": (
            yaml_point(sectors[1].get("a")),
            yaml_point(sectors[1].get("b")),
        ),
    }

    required = [
        geometry["box"],
        geometry["pit_lane_start"],
        geometry["pit_lane_end"],
        geometry["sf"][0],
        geometry["sf"][1],
        geometry["s1"][0],
        geometry["s1"][1],
        geometry["s2"][0],
        geometry["s2"][1],
    ]

    if not all(required):
        raise RuntimeError(
            "Geometria YAML incompleta: servono box, pit_lane.start, "
            "pit_lane.end, SF, S1 e S2."
        )

    if distance_m(geometry["pit_lane_end"], A) > 3.0:
        raise RuntimeError(
            "pit_lane.end deve coincidere con Corner A per questa simulazione."
        )

    return geometry


def append_lap(route, geometry, hz, phase, speeds):
    current = A

    current = append_line_crossing(
        route,
        current,
        geometry["s1"][0],
        geometry["s1"][1],
        B,
        speeds["s1"],
        hz,
        phase + "_s1",
    )

    current = append_straight(
        route,
        current,
        C,
        speeds["b_to_c"],
        hz,
        phase + "_b_to_c",
    )

    current = append_line_crossing(
        route,
        current,
        geometry["s2"][0],
        geometry["s2"][1],
        D,
        speeds["s2"],
        hz,
        phase + "_s2",
    )

    append_line_crossing(
        route,
        current,
        geometry["sf"][0],
        geometry["sf"][1],
        A,
        speeds["finish"],
        hz,
        phase + "_finish",
    )


def append_stationary(route, point, seconds, hz, phase):
    for _ in range(max(1, int(seconds * hz))):
        route.append({
            "latitude": point[0],
            "longitude": point[1],
            "speed_kmph": 0.0,
            "phase": phase,
        })


def build_route(track_config, hz):
    geometry = get_geometry(track_config)
    route = []

    append_stationary(
        route,
        geometry["box"],
        seconds=3.0,
        hz=hz,
        phase="box_stationary",
    )

    append_straight(
        route,
        geometry["box"],
        geometry["pit_lane_start"],
        speed_kmph=20.0,
        hz=hz,
        phase="pit_lane_exit_box_to_start",
    )

    append_straight(
        route,
        geometry["pit_lane_start"],
        geometry["pit_lane_end"],
        speed_kmph=20.0,
        hz=hz,
        phase="pit_lane_exit_start_to_A",
    )

    append_lap(
        route,
        geometry,
        hz,
        phase="warmup",
        speeds={
            "s1": 44.0,
            "b_to_c": 46.0,
            "s2": 45.0,
            "finish": 50.0,
        },
    )

    append_lap(
        route,
        geometry,
        hz,
        phase="lap_1",
        speeds={
            "s1": 55.0,
            "b_to_c": 57.0,
            "s2": 54.0,
            "finish": 61.0,
        },
    )

    append_lap(
        route,
        geometry,
        hz,
        phase="lap_2_fast",
        speeds={
            "s1": 59.0,
            "b_to_c": 61.0,
            "s2": 58.0,
            "finish": 65.0,
        },
    )

    append_straight(
        route,
        A,
        geometry["pit_lane_start"],
        speed_kmph=20.0,
        hz=hz,
        phase="pit_lane_entry_A_to_start",
    )

    append_straight(
        route,
        geometry["pit_lane_start"],
        geometry["box"],
        speed_kmph=20.0,
        hz=hz,
        phase="pit_lane_entry_start_to_box",
    )

    append_stationary(
        route,
        geometry["box"],
        seconds=4.0,
        hz=hz,
        phase="box_returned",
    )

    return route


def publish(route, mqtt_config, hz, multiplier):
    broker = mqtt_config["broker"]
    port = int(mqtt_config.get("port", 1883))
    topic = mqtt_config["topic"]

    connected = False

    def on_connect(client, userdata, flags, reason_code, properties=None):
        nonlocal connected
        connected = True

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"kart-sim-{int(time.time())}",
    )

    client.on_connect = on_connect
    client.connect(broker, port, keepalive=60)
    client.loop_start()

    started_at = time.time()

    while not connected:
        if time.time() - started_at > 8.0:
            client.loop_stop()
            client.disconnect()
            raise RuntimeError("Connessione MQTT non riuscita.")

        time.sleep(0.05)

    print()
    print("=== SIMULATORE KART PERFORMANCE MONITOR ===")
    print("BOX -> pit_lane.start -> A -> S1 -> B -> C -> S2 -> D -> SF -> A")
    print("warmup, lap_1, lap_2_fast, poi rientro A -> pit lane -> BOX")
    print()
    print(f"Campioni: {len(route)}")
    print(f"Frequenza: {hz:.1f} Hz")
    print(f"Moltiplicatore: {multiplier:.2f}x")
    print()

    interval_s = 1.0 / hz / multiplier
    last_phase = None

    try:
        for index, item in enumerate(route, start=1):
            phase = item["phase"]

            if phase != last_phase:
                print(
                    f"[{index:04d}/{len(route):04d}] "
                    f"{phase:36s} "
                    f"{item['speed_kmph']:5.1f} km/h"
                )
                last_phase = phase

            payload = {
                "latitude": round(item["latitude"], 7),
                "longitude": round(item["longitude"], 7),
                "speed_kmph": round(item["speed_kmph"], 1),
                "satellites_used": 10,
                "fix_valid": True,
                "fix_quality": 1,
                "fix_type": 3,
                "hdop": 0.75,
                "simulated": True,
                "simulation_phase": phase,
            }

            result = client.publish(
                topic,
                json.dumps(payload),
                qos=0,
                retain=False,
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(
                    f"Errore MQTT publish: {result.rc}"
                )

            time.sleep(interval_s)

    except KeyboardInterrupt:
        print("\nSimulazione interrotta.")

    finally:
        client.loop_stop()
        client.disconnect()

    print("=== SIMULAZIONE COMPLETATA ===")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--hz",
        type=float,
        default=10.0,
        help="Frequenza GPS simulata in Hz.",
    )

    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Moltiplicatore simulazione: 1=realtime, 2=doppia velocità.",
    )

    args = parser.parse_args()

    if args.hz <= 0.0:
        raise SystemExit("--hz deve essere maggiore di zero.")

    if args.speed <= 0.0:
        raise SystemExit("--speed deve essere maggiore di zero.")

    app_config = load_yaml(APP_CONFIG_PATH)
    track_config = load_yaml(TRACK_CONFIG_PATH)

    route = build_route(track_config, args.hz)

    publish(
        route=route,
        mqtt_config=app_config["mqtt"],
        hz=args.hz,
        multiplier=args.speed,
    )


if __name__ == "__main__":
    main()
