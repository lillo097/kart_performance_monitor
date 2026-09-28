#!/usr/bin/env python3
"""Session logger: boot, raw, events, and application logs per each service run."""

import json
import logging
import os
import platform
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "logs"
SESSIONS_DIR = LOGS_DIR / "sessions"

RETENTION_DAYS = 30
RAW_ROTATE_BYTES = 50 * 1024 * 1024
BOOT_UPDATE_INTERVAL_S = 30.0


def _iso_now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_json(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"_serialization_error": True}, ensure_ascii=False)


def _atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class SessionLogger:
    """Gestisce i tre file di log di una sessione (un boot del servizio)."""

    def __init__(self, app_version="2.0.0"):
        self._lock = threading.RLock()
        self._app_version = app_version
        self._log = logging.getLogger("session")

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_sessions()

        created_at = datetime.now()
        stamp = created_at.strftime("%Y-%m-%d_%H-%M-%S")
        session_dir = SESSIONS_DIR / stamp
        suffix = 1
        while True:
            try:
                session_dir.mkdir()
                break
            except FileExistsError:
                session_dir = SESSIONS_DIR / f"{stamp}_{suffix:02d}"
                suffix += 1

        stamp = session_dir.name
        self.session_id = stamp

        self.session_dir = session_dir
        self._boot_path = session_dir / "boot.json"
        self._raw_path = session_dir / "raw.jsonl"
        self._events_path = session_dir / "events.jsonl"
        self._log_path = session_dir / "glo2-telemetry.log"

        self._raw_handle = open(self._raw_path, "a", encoding="utf-8", buffering=1)
        self._events_handle = open(self._events_path, "a", encoding="utf-8", buffering=1)

        self._started_epoch = time.time()
        self._started_iso = _iso_now()
        self._ended_iso = None
        self._last_boot_update = 0.0

        self._counters = {
            "samples_published": 0,
            "valid_gps_samples": 0,
            "mqtt_reconnects": 0,
            "bt_reconnects": 0,
            "nmea_timeouts": 0,
            "raw_rotations": 0,
        }

        self._sensors_at_boot = {}
        self._sensors_now = {}
        self._wifi = {}
        self._mqtt_meta = {}
        self._gps_meta = {}

        self._boot_status = "running"

        self._log.info(f"[SESSION] started id={stamp}")
        self._log.info(f"[SESSION] directory={self.session_dir}")
        self._log.info(f"[SESSION] boot={self._boot_path.name}")
        self._log.info(f"[SESSION] raw={self._raw_path.name}")
        self._log.info(f"[SESSION] events={self._events_path.name}")

    # ------------------------------------------------------------------
    # Meta (chiamati all'avvio da app.py)
    # ------------------------------------------------------------------
    def set_wifi(self, ssid, ip, mac, rssi=None):
        with self._lock:
            self._wifi = {"ssid": ssid, "ip": ip, "mac": mac, "rssi": rssi}

    def set_mqtt(self, broker, port, client_id, topic):
        with self._lock:
            self._mqtt_meta = {
                "broker": broker, "port": port,
                "client_id": client_id, "topic": topic,
            }

    def set_gps(self, mac, rfcomm_channel):
        with self._lock:
            self._gps_meta = {"mac": mac, "rfcomm_channel": rfcomm_channel}

    def set_sensors_at_boot(self, ntc, as5600, ir_rpm, gps):
        with self._lock:
            self._sensors_at_boot = {
                "ntc": bool(ntc), "as5600": bool(as5600),
                "ir_rpm": bool(ir_rpm), "gps": bool(gps),
            }

    def update_sensors_now(self, ntc=None, as5600=None, ir_rpm=None, gps=None):
        with self._lock:
            if ntc is not None:     self._sensors_now["ntc"] = bool(ntc)
            if as5600 is not None:  self._sensors_now["as5600"] = bool(as5600)
            if ir_rpm is not None:  self._sensors_now["ir_rpm"] = bool(ir_rpm)
            if gps is not None:     self._sensors_now["gps"] = bool(gps)

    def bump(self, counter, delta=1):
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + delta

    # ------------------------------------------------------------------
    # Eventi discreti
    # ------------------------------------------------------------------
    def log_event(self, event_name, **data):
        with self._lock:
            try:
                rec = {
                    "ts_epoch": round(time.time(), 3),
                    "ts_iso": _iso_now(),
                    "event": event_name,
                    **data,
                }
                self._events_handle.write(_safe_json(rec) + "\n")
            except Exception as e:
                self._log.error(f"[SESSION] log_event failed: {e}")

    # ------------------------------------------------------------------
    # Campioni grezzi
    # ------------------------------------------------------------------
    def log_raw_sample(self, payload):
        with self._lock:
            try:
                rec = {
                    "ts_epoch": round(time.time(), 3),
                    "ts_iso": _iso_now(),
                    "payload": payload,
                }
                self._raw_handle.write(_safe_json(rec) + "\n")
                self._counters["samples_published"] += 1

                # GPS valido?
                if payload.get("fix_valid") and payload.get("latitude") and payload.get("longitude"):
                    self._counters["valid_gps_samples"] += 1

                # Rotazione
                try:
                    if self._raw_handle.tell() >= RAW_ROTATE_BYTES:
                        self._rotate_raw()
                except Exception:
                    pass

            except Exception as e:
                self._log.error(f"[SESSION] log_raw_sample failed: {e}")

    def _rotate_raw(self):
        try:
            self._raw_handle.close()
            n = self._counters["raw_rotations"] + 1
            rotated = self._raw_path.with_suffix(f".jsonl.{n}")
            self._raw_path.rename(rotated)
            self._raw_handle = open(self._raw_path, "a", encoding="utf-8", buffering=1)
            self._counters["raw_rotations"] = n
            self._log.info(f"[SESSION] raw rotated -> {rotated.name}")
        except Exception as e:
            self._log.error(f"[SESSION] raw rotation failed: {e}")

    # ------------------------------------------------------------------
    # boot.json (identico per struttura a serializable_session())
    # ------------------------------------------------------------------
    def _build_boot_payload(self):
        elapsed = round(time.time() - self._started_epoch, 3)

        with self._lock:
            return {
                "schema_version": 12,
                "client_side": True,
                "saved_at": _iso_now(),
                "status": self._boot_status,
                "driver": {},
                "track": {},
                "track_config": {},
                "track_id": None,
                "mqtt_topic": self._mqtt_meta.get("topic"),
                "started_at": self._started_iso,
                "paused_at": None,
                "paused_total_s": 0.0,
                "ended_at": self._ended_iso,
                "data_age_at_start_s": None,
                "data_age_samples": {"at_epoch": [], "data_age_s": []},
                "session_elapsed_s": elapsed,
                "summary": {
                    "raw_samples_received": self._counters["samples_published"],
                    "track_points_saved": 0,
                    "valid_gps_samples": self._counters["valid_gps_samples"],
                    "filtered_out_samples": 0,
                    "laps_completed": 0,
                    "laps_aborted": 0,
                    "data_age_samples_recorded": 0,
                },
                "best_lap_time_s": None,
                "best_sector_times_s": [],
                "ideal_lap_time_s": None,
                "reference_lap_number": None,
                "reference_lap_time_s": None,
                "laps": [],
                "current_lap_in_progress": None,
                "warmup_and_pitlane_raw": {
                    "received_at_epoch": [], "latitude": [], "longitude": [],
                    "speed_kmph": [], "satellites_used": [], "hdop": [],
                    "temperature_c": [], "as5600_rpm": [], "ir_rpm": [],
                },
                "track_state": "pitlane",
                "cooldown_active": False,
                "delta_live_s": None,
                "last_sector_result": None,
                "last_aborted_lap": None,
                "client_meta": {
                    "hostname": platform.node(),
                    "python_version": platform.python_version(),
                    "app_version": self._app_version,
                    "mqtt_client_id": self._mqtt_meta.get("client_id"),
                    "wifi": dict(self._wifi),
                    "gps": dict(self._gps_meta),
                    "sensors_at_boot": dict(self._sensors_at_boot),
                    "sensors_now": dict(self._sensors_now),
                    "counters": dict(self._counters),
                },
                "raw_stream_file": self._raw_path.name,
                "events_file": self._events_path.name,
                "log_file": self._log_path.name,
            }

    def update_boot_json(self, force=False):
        now = time.time()
        if not force and (now - self._last_boot_update) < BOOT_UPDATE_INTERVAL_S:
            return
        try:
            _atomic_write_json(self._boot_path, self._build_boot_payload())
            self._last_boot_update = now
        except Exception as e:
            self._log.error(f"[SESSION] boot update failed: {e}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def _cleanup_old_sessions(self):
        if not SESSIONS_DIR.exists():
            return
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        removed = 0
        for path in SESSIONS_DIR.iterdir():
            try:
                if path.stat().st_mtime < cutoff.timestamp():
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.is_file():
                        path.unlink()
                    else:
                        continue
                    removed += 1
            except OSError as error:
                self._log.warning(f"[SESSION] could not clean old path {path}: {error}")
        if removed:
            self._log.info(
                f"[SESSION] cleaned {removed} old sessions/files "
                f"(>{RETENTION_DAYS}d)"
            )

    # ------------------------------------------------------------------
    # Chiusura
    # ------------------------------------------------------------------
    def close(self, reason="shutdown"):
        with self._lock:
            self._boot_status = "stopped"
            self._ended_iso = _iso_now()
            try:
                self.log_event("service_stop", reason=reason,
                               uptime_s=round(time.time() - self._started_epoch, 3),
                               samples_published=self._counters["samples_published"])
            except Exception:
                pass
            try:
                self._raw_handle.close()
            except Exception:
                pass
            try:
                self._events_handle.close()
            except Exception:
                pass
            try:
                _atomic_write_json(self._boot_path, self._build_boot_payload())
            except Exception as e:
                self._log.error(f"[SESSION] final boot write failed: {e}")
            self._log.info(f"[SESSION] closed id={self.session_id} reason={reason}")