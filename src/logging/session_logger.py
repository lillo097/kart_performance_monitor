#!/usr/bin/env python3
"""Session logger: boot, raw, events, and application logs per each service run."""

import json
import logging
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "system_logs"
SESSIONS_DIR = LOGS_DIR / "sessions"
APP_SESSIONS_DIR = BASE_DIR / "sessions"

RETENTION_DAYS = 30
RAW_ROTATE_BYTES = 50 * 1024 * 1024
BOOT_UPDATE_INTERVAL_S = 30.0
TEXT_BUFFER_MAX_LINES = 100_000

_TEXT_FMT = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-8s] %(message)s"
_TEXT_DATE = "%Y-%m-%d %H:%M:%S"

# Singleton: permette ai hook di cattura (stdout/stderr/warnings) di
# raggiungere l'istanza di SessionLogger senza importare il main.
_current_session_logger = None


def _thread_excepthook(args):
    """threading.excepthook: cattura le eccezioni non gestite dei worker
    thread e le logga come [CRITICAL] nel log di sessione.

    Sostituisce l'handler di default (che stampa solo su stderr) così da
    non emettere il traceback due volte: qui lo scriviamo una volta su
    journalctl (stream originale) e una volta nel log di sessione.
    """
    tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                 args.exc_traceback))
    thread_name = args.thread.name if args.thread else "unknown"
    # Journalctl: scrivi sullo stream originale (evitando il wrapper di cattura)
    err = sys.stderr
    while hasattr(err, "_original"):
        err = err._original
    try:
        err.write(f"Exception in thread {thread_name!r}:\n{tb_text}")
        err.flush()
    except Exception:
        pass
    sl = _current_session_logger
    if sl is not None:
        sl.append_exception(args.exc_type, args.exc_value, args.exc_traceback,
                            thread_name=thread_name)


def get_current_session_logger():
    """Restituisce l'istanza corrente di SessionLogger (None se inesistente)."""
    return _current_session_logger


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

        # Stato della sessione
        self.session_guid = None
        self._session_active = False  # True solo quando la sessione è avviata via MQTT
        self._session_boot_stamp = None
        self.session_id = None
        self.session_dir = None

        # Buffer in memoria per eventi e campioni finché la sessione non è avviata
        self._events_buffer = []
        self._raw_buffer = []

        # Buffer testuale (righe di log/.stdout/stderr/warnings/exceptioni)
        # max TEXT_BUFFER_MAX_LINES: scartato se il servizio termina senza mai
        # aver avviato una sessione, altrimenti scritto retroattivamente.
        self._text_buffer = deque(maxlen=TEXT_BUFFER_MAX_LINES)
        self._text_handler = logging.Formatter(_TEXT_FMT, datefmt=_TEXT_DATE)

        # File handles (None finché la sessione non è avviata)
        self._raw_handle = None
        self._events_handle = None
        self._text_handle = None
        self._boot_path = None
        self._raw_path = None
        self._events_path = None
        self._log_path = None
        self._app_session_path = None

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
        self._warmup_samples = []

        self._sensors_at_boot = {}
        self._sensors_now = {}
        self._wifi = {}
        self._mqtt_meta = {}
        self._gps_meta = {}

        self._boot_status = "running"

        # Registra il singleton usato dagli hook di cattura
        global _current_session_logger
        _current_session_logger = self

        self._log.info("[SESSION] logger initialized (buffering mode - waiting for session start)")

    def start_session(self, session_guid):
        """Avvia una nuova sessione: crea la directory e salva il buffer su disco."""
        normalized_guid = str(uuid.UUID(str(session_guid)))

        with self._lock:
            # Se c'è già una sessione attiva con lo stesso GUID, ignora
            if self._session_active and self.session_guid == normalized_guid:
                self._log.warning(f"[SESSION] session already active with guid={normalized_guid}")
                return

            # Se c'è una sessione attiva diversa, chiudila prima
            if self._session_active and self.session_guid != normalized_guid:
                self._log.info(f"[SESSION] closing previous session before starting new one")
                self._close_session_files()

            # Crea directory di sessione con timestamp + GUID
            created_at = datetime.now()
            stamp = created_at.strftime("%Y-%m-%d_%H-%M-%S")
            session_dir = SESSIONS_DIR / f"{stamp}_{normalized_guid}"

            suffix = 1
            while session_dir.exists():
                session_dir = SESSIONS_DIR / f"{stamp}_{suffix:02d}_{normalized_guid}"
                suffix += 1

            try:
                session_dir.mkdir()
            except FileExistsError:
                self._log.error(f"[SESSION] failed to create directory {session_dir}")
                raise

            self._session_boot_stamp = stamp
            self.session_guid = normalized_guid
            self.session_id = session_dir.name
            self.session_dir = session_dir
            self._boot_path = session_dir / "boot.json"
            self._raw_path = session_dir / "raw.jsonl"
            self._events_path = session_dir / "events.jsonl"
            self._log_path = session_dir / "glo2-telemetry.log"
            self._app_session_path = APP_SESSIONS_DIR / f"{session_dir.name}_raspi.json"

            # Chiudi l'eventuale file di log della sessione precedente
            # (lasciato aperto da stop_session) per non perdere handle
            try:
                if self._text_handle:
                    self._text_handle.close()
                    self._text_handle = None
            except Exception:
                pass

            # Reset contatori per nuova sessione
            self._started_epoch = time.time()
            self._started_iso = _iso_now()
            self._ended_iso = None
            self._boot_status = "running"
            self._counters["raw_rotations"] = 0
            self._warmup_samples = []

            # Flush retroattivo, PRIMA di aprire gli handle, per non intrecciare
            # le righe bufferizzate con i log interni di questo metodo. L'ordine
            # cronologico nel file resta: boot -> ... -> "session started".
            self._flush_text_buffer()      # righe log/stdout/stderr/warnings pre-sessione
            self._flush_buffers_to_disk()  # eventi e campioni raw pre-sessione

            # Solo ora gli handle sono pronti: da qui in poi ogni riga (log,
            # stdout/stderr, warnings, eccezioni) va in coda al file di sessione
            self._raw_handle = open(self._raw_path, "a", encoding="utf-8", buffering=1)
            self._events_handle = open(self._events_path, "a", encoding="utf-8", buffering=1)
            self._text_handle = open(self._log_path, "a", encoding="utf-8", buffering=1)

            self._session_active = True

            # Scrivi evento di associazione sessione + inizio sessione
            self.log_event("session_associated", session_guid=normalized_guid)

            # Aggiorna boot.json
            self.update_boot_json(force=True)

            self._log.info(f"[SESSION] started and saved to disk: {self.session_dir}")
            self._log.info(f"[SESSION] session_guid={normalized_guid}")

    def _flush_buffers_to_disk(self):
        """Scrive i buffer in memoria sui file su disco.

        Va invocata PRIMA di aprire `_raw_handle`/`_events_handle` (modalità
        "pre-apertura"): apre i file al momento, così `start_session()` può
        mantenere gli handle a `None` fino a flush completato ed evitare che i
        log interni vengano scritti fuori ordine.
        """
        try:
            if self._events_buffer:
                target = self._events_handle
                close_after = target is None
                if close_after:
                    target = open(self._events_path, "a", encoding="utf-8", buffering=1)
                for event_record in self._events_buffer:
                    target.write(_safe_json(event_record) + "\n")
                if close_after:
                    target.close()

            if self._raw_buffer:
                target = self._raw_handle
                close_after = target is None
                if close_after:
                    target = open(self._raw_path, "a", encoding="utf-8", buffering=1)
                for raw_record in self._raw_buffer:
                    target.write(_safe_json(raw_record) + "\n")
                if close_after:
                    target.close()

            self._log.info(f"[SESSION] flushed {len(self._events_buffer)} events and {len(self._raw_buffer)} raw samples to disk")

            # Svuota i buffer
            self._events_buffer.clear()
            self._raw_buffer.clear()
        except Exception as e:
            self._log.error(f"[SESSION] failed to flush buffers: {e}")

    def set_session_guid(self, session_guid):
        """Alias per start_session per compatibilità con codice esistente."""
        self.start_session(session_guid)

    # ------------------------------------------------------------------
    # Captured text lines (logging records, stdout/stderr, warnings,
    # unhandled exceptions) — scritte su disco o bufferizzate in RAM
    # ------------------------------------------------------------------
    @property
    def session_log_active(self):
        """True se il file di log di sessione è aperto e scrivibile.

        La condizione si basa su _text_handle (e non su _session_active)
        perché, per specifica, le righe loggate dopo session_stopped
        restano in coda al file di sessione corrente fino allo shutdown
        del servizio.
        """
        return self._text_handle is not None

    def _write_text_line(self, line):
        """Scrive una riga sul file di log di sessione, con auto-recupero.

        `_text_handle` è un file aperto in unbuffered; se un ramo di scrittura
        lo chiude inavvertitamente, `write()` solleva ValueError e la riga
        andrebbe persa. In quel caso riapriamo il file in append (stesso path)
        così il log di sessione continua a raccogliere le righe successive.

        Restituisce True se la riga è stata scritta, False se finisce nel buffer.
        """
        if line is None:
            return True
        line = str(line)
        if self._text_handle is not None:
            try:
                self._text_handle.write(line + "\n")
                return True
            except ValueError:
                # Handle chiuso inavvertitamente: prova a riaprirlo
                try:
                    self._text_handle = open(self._log_path, "a",
                                             encoding="utf-8", buffering=1)
                    self._text_handle.write(line + "\n")
                    return True
                except Exception:
                    self._text_handle = None
        self._text_buffer.append(line)
        return False

    def format_logging_record(self, record):
        """Formatta un record di logging nel formato usato dal file di sessione."""
        return self._text_handler.format(record)

    def append_formatted_line(self, line):
        """Appende una riga già formattata (record di logging) al log di sessione."""
        with self._lock:
            try:
                self._write_text_line(line)
            except Exception as e:
                self._log.error(f"[SESSION] failed to append log line: {e}")

    def append_captured_line(self, tag, line):
        """Appende una riga catturata (stdout/stderr/warnings) con tag al log di sessione.

        `tag` è "stdout", "stderr" o "py.warnings". La riga è salvata
        nell'ordine in cui è stata emessa, indipendentemente dallo stato
        della sessione (buffer in RAM se nessuna sessione attiva).
        """
        with self._lock:
            if not line:
                return
            formatted = f"{self._format_line_ts()} [{tag}] {line}"
            try:
                self._write_text_line(formatted)
            except Exception as e:
                self._log.error(f"[SESSION] failed to append captured line: {e}")

    def append_exception(self, exc_type, exc_value, exc_tb, thread_name="main"):
        """Logga un'eccezione non gestita (main o worker thread) come [CRITICAL]."""
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        formatted = (f"{self._format_line_ts()} [CRITICAL] [exception:{thread_name}] "
                     f"unhandled exception:\n" + "".join(tb_lines).rstrip())
        with self._lock:
            try:
                if self.session_log_active:
                    self._text_handle.write(formatted + "\n")
                else:
                    self._text_buffer.append(formatted)
            except Exception:
                pass

    def _format_line_ts(self):
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def install_thread_excepthook():
        """Abilita la cattura delle eccezioni non gestite dei worker thread.
        Chiamare una sola volta all'avvio del servizio."""
        threading.excepthook = _thread_excepthook

    def _flush_text_buffer(self):
        """Scrive le righe di testo bufferizzate sul file di sessione.

        Va invocata PRIMA di aprire `_text_handle` (modalità "pre-apertura"):
        scrive direttamente sul path e consuma il buffer sotto `_lock`, in modo
        che i writer concorrenti vedano lo stato già aggiornato e non restino
        righe intrappolate nel buffer (race con deque(maxlen)).
        """
        with self._lock:
            if not self._text_buffer:
                return
            # Snapshot + clear atomici: nessuno può appendere tra le due operazioni
            pending = list(self._text_buffer)
            self._text_buffer.clear()

            if self._text_handle:
                # Handle già aperto: scrivi su di esso (percorso usato solo se
                # questa funzione viene richiamata dopo l'apertura)
                target = self._text_handle
                for line in pending:
                    target.write(line + "\n")
            else:
                # Pre-apertura: apri in append, scrivi, chiudi subito, così le
                # righe finiscono in testa al file in ordine cronologico
                with open(self._log_path, "a", encoding="utf-8", buffering=1) as fh:
                    for line in pending:
                        fh.write(line + "\n")

            try:
                self._log.info(f"[SESSION] flushed {len(pending)} buffered log lines to session file")
            except Exception:
                pass

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

                # Se la sessione è attiva, scrivi su disco
                if self._session_active and self._events_handle:
                    self._events_handle.write(_safe_json(rec) + "\n")
                else:
                    # Altrimenti bufferizza in memoria
                    self._events_buffer.append(rec)
                    # Limita dimensione buffer (max 1000 eventi)
                    if len(self._events_buffer) > 1000:
                        self._events_buffer.pop(0)
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

                # Se la sessione è attiva, scrivi su disco
                if self._session_active and self._raw_handle:
                    self._raw_handle.write(_safe_json(rec) + "\n")
                else:
                    # Altrimenti bufferizza in memoria
                    self._raw_buffer.append(rec)
                    # Limita dimensione buffer (max 10000 campioni, ~2MB)
                    if len(self._raw_buffer) > 10000:
                        self._raw_buffer.pop(0)

                self._counters["samples_published"] += 1
                self._warmup_samples.append({
                    "received_at_epoch": rec["ts_epoch"],
                    "latitude": payload.get("latitude"),
                    "longitude": payload.get("longitude"),
                    "speed_kmph": payload.get("speed_kmph"),
                    "satellites_used": payload.get("satellites_used"),
                    "hdop": payload.get("hdop"),
                    "temperature_c": payload.get("temperature_c"),
                    "as5600_rpm": payload.get("as5600_rpm"),
                    "ir_rpm": payload.get("ir_rpm"),
                })

                # GPS valido?
                if payload.get("fix_valid") and payload.get("latitude") and payload.get("longitude"):
                    self._counters["valid_gps_samples"] += 1

                # Rotazione (solo se sessione attiva)
                if self._session_active and self._raw_handle:
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
                "schema_version": 13,
                "client_side": True,
                "session_guid": self.session_guid,
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

    def _build_app_session_payload(self):
        now = time.time()
        warmup_fields = (
            "received_at_epoch",
            "latitude",
            "longitude",
            "speed_kmph",
            "satellites_used",
            "hdop",
            "temperature_c",
            "as5600_rpm",
            "ir_rpm",
        )

        with self._lock:
            samples = {
                field: [sample[field] for sample in self._warmup_samples]
                for field in warmup_fields
            }
            return {
                "schema_version": 13,
                "session_guid": self.session_guid,
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
                "session_elapsed_s": round(now - self._started_epoch, 3),
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
                "warmup_and_pitlane_raw": samples,
                "track_state": None,
                "cooldown_active": None,
                "delta_live_s": None,
                "last_sector_result": None,
                "last_aborted_lap": None,
            }

    def update_boot_json(self, force=False):
        # Salta se la sessione non è attiva
        if not self._session_active:
            return

        now = time.time()
        if not force and (now - self._last_boot_update) < BOOT_UPDATE_INTERVAL_S:
            return
        try:
            _atomic_write_json(self._boot_path, self._build_boot_payload())
        except Exception as e:
            self._log.error(f"[SESSION] boot update failed: {e}")
        try:
            APP_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(
                self._app_session_path, self._build_app_session_payload()
            )
            self._last_boot_update = now
        except Exception as e:
            self._log.error(f"[SESSION] app session update failed: {e}")

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
    # Stop sessione (da comando MQTT)
    # ------------------------------------------------------------------
    def stop_session(self, reason="session_stopped"):
        """Stoppa la sessione corrente e salva i file su disco."""
        with self._lock:
            if not self._session_active:
                self._log.warning("[SESSION] no active session to stop")
                return

            self._boot_status = "stopped"
            self._ended_iso = _iso_now()

            # Scrivi evento di stop
            self.log_event("service_stop", reason=reason,
                          uptime_s=round(time.time() - self._started_epoch, 3),
                          samples_published=self._counters["samples_published"])

            # Chiudi i file (il file di log testuale resta aperto: le righe
            # successive devono restare in coda al file di questa sessione
            # finché il servizio non viene riavviato)
            self._close_session_files(close_text=False)

            # Scrivi boot.json finale
            try:
                _atomic_write_json(self._boot_path, self._build_boot_payload())
            except Exception as e:
                self._log.error(f"[SESSION] final boot write failed: {e}")

            # Scrivi app session file finale
            try:
                APP_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(
                    self._app_session_path, self._build_app_session_payload()
                )
            except Exception as e:
                self._log.error(f"[SESSION] final app session write failed: {e}")

            self._log.info(f"[SESSION] stopped and saved: {self.session_id} reason={reason}")

            # Reset stato per eventuale nuova sessione
            self._session_active = False
            self.session_guid = None
            self.session_id = None

            # Reset contatori
            self._started_epoch = time.time()
            self._started_iso = _iso_now()
            self._ended_iso = None
            self._boot_status = "running"
            self._counters = {
                "samples_published": 0,
                "valid_gps_samples": 0,
                "mqtt_reconnects": 0,
                "bt_reconnects": 0,
                "nmea_timeouts": 0,
                "raw_rotations": 0,
            }
            self._warmup_samples = []

            # Ricomincia a bufferizzare per eventuale prossima sessione
            self._log.info("[SESSION] ready for next session (buffering mode)")

    def _close_session_files(self, close_text=True):
        """Chiude i file handle della sessione corrente.

        `close_text=False` mantiene aperto il file di log testuale
        (usato da stop_session: le righe successive restano in coda al
        file della sessione appena fermata).
        """
        try:
            if self._raw_handle:
                self._raw_handle.close()
                self._raw_handle = None
        except Exception as e:
            self._log.error(f"[SESSION] failed to close raw handle: {e}")

        try:
            if self._events_handle:
                self._events_handle.close()
                self._events_handle = None
        except Exception as e:
            self._log.error(f"[SESSION] failed to close events handle: {e}")

        try:
            if self._text_handle and close_text:
                self._text_handle.close()
                self._text_handle = None
        except Exception as e:
            self._log.error(f"[SESSION] failed to close text handle: {e}")

    # ------------------------------------------------------------------
    # Chiusura (chiamato allo shutdown del servizio)
    # ------------------------------------------------------------------
    def close(self, reason="shutdown"):
        """Chiude il logger. Se c'è una sessione attiva, la salva; altrimenti scarta il buffer."""
        with self._lock:
            if self._session_active:
                # Sessione attiva: salva tutto
                self._log.info(f"[SESSION] saving active session before shutdown")
                self.stop_session(reason=reason)
            else:
                # Nessuna sessione attiva: scarta il buffer
                self._log.info(f"[SESSION] discarding buffered data (no active session)")
                self._events_buffer.clear()
                self._raw_buffer.clear()

            # Chiudi il file di log testuale (aperto sia con sessione attiva
            # che dopo session_stopped) e scarta il buffer residuo
            self._close_session_files()
            self._text_buffer.clear()

            self._log.info(f"[SESSION] logger closed, reason={reason}")