import json
import threading
import time
import uuid
from collections import deque

import paho.mqtt.client as mqtt

from .settings import CONFIG
STATUS_STALE_S = 20
COMMAND_TIMEOUT_S = 25


class MqttBridge:
    def __init__(self, config):
        self.config = config
        self.device_id = config["device_id"]
        self.services = config["services"]
        self.topic_root = (
            f"{config['topic_prefix']}/{self.device_id}"
        )
        self.command_topic = f"{self.topic_root}/command"
        self.status_topic = f"{self.topic_root}/status"
        self.result_topic = f"{self.topic_root}/result"
        self.log_topic = f"{self.topic_root}/logs"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._connected = False
        self._last_status_at = 0.0
        self._statuses = {}
        self._results = {}
        self._log_sequence = 0
        self._logs = {
            service_id: deque(maxlen=2000)
            for service_id in self.services
        }
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"kart-command-ui-{self.device_id}-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self):
        mqtt_config = self.config["mqtt"]
        try:
            self._client.connect_async(
                mqtt_config["broker"],
                mqtt_config["port"],
                mqtt_config.get("keepalive_s", 60),
            )
            self._client.loop_start()
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Avvio MQTT fallito: {error}") from error

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "value", reason_code) != 0:
            with self._condition:
                self._connected = False
                self._condition.notify_all()
            return

        client.subscribe([
            (self.status_topic, 1),
            (self.result_topic, 1),
            (self.log_topic, 0),
        ])
        with self._condition:
            self._connected = True
            self._condition.notify_all()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        with self._condition:
            self._connected = False
            self._condition.notify_all()

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("device_id") != self.device_id:
            return

        if message.topic == self.status_topic:
            self._accept_status(payload)
        elif message.topic == self.result_topic:
            self._accept_result(payload)
        elif message.topic == self.log_topic:
            self._accept_log(payload)

    def _valid_timestamp(self, payload, max_age_s=60):
        timestamp = payload.get("timestamp")
        return (
            isinstance(timestamp, (int, float))
            and abs(time.time() - timestamp) <= max_age_s
        )

    def _accept_status(self, payload):
        services = payload.get("services")
        if not isinstance(services, dict) or not self._valid_timestamp(payload):
            return
        with self._condition:
            self._statuses = {
                service_id: dict(services.get(service_id, {}))
                for service_id in self.services
            }
            self._last_status_at = payload["timestamp"]
            self._condition.notify_all()

    def _accept_result(self, payload):
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not self._valid_timestamp(payload):
            return
        with self._condition:
            self._results[request_id] = payload
            self._condition.notify_all()

    def _accept_log(self, payload):
        service_id = payload.get("service")
        line = payload.get("line")
        if (
            service_id not in self._logs
            or not isinstance(line, str)
            or not self._valid_timestamp(payload, max_age_s=300)
        ):
            return
        with self._condition:
            self._log_sequence += 1
            self._logs[service_id].append({
                "sequence": self._log_sequence,
                "line": line,
            })
            self._condition.notify_all()

    def health(self):
        with self._lock:
            status_age = time.time() - self._last_status_at
            return {
                "mqtt_connected": self._connected,
                "agent_online": self._connected and status_age <= STATUS_STALE_S,
                "last_status_at": self._last_status_at or None,
                "device_id": self.device_id,
            }

    def service_statuses(self):
        with self._lock:
            status_age = time.time() - self._last_status_at
            agent_online = self._connected and status_age <= STATUS_STALE_S
            results = []
            for service_id, unit in self.services.items():
                status = self._statuses.get(service_id, {})
                results.append({
                    "id": service_id,
                    "unit": unit,
                    "active_state": status.get("active_state", "unknown")
                    if agent_online else "unknown",
                    "sub_state": status.get("sub_state", "offline")
                    if agent_online else "offline",
                    "main_pid": str(status.get("main_pid", "0")),
                    "active_since": status.get("active_since", ""),
                    "last_started": status.get("last_started", ""),
                    "result": status.get("result", ""),
                })
            return results

    def latest_logs(self, service_id, count):
        with self._lock:
            return list(self._logs[service_id])[-count:]

    def latest_log_sequence(self, service_id):
        with self._lock:
            entries = self._logs[service_id]
            return entries[-1]["sequence"] if entries else self._log_sequence

    def logs_after(self, service_id, sequence, timeout=10):
        with self._condition:
            self._condition.wait_for(
                lambda: any(
                    entry["sequence"] > sequence
                    for entry in self._logs[service_id]
                ),
                timeout=timeout,
            )
            entries = [
                entry for entry in self._logs[service_id]
                if entry["sequence"] > sequence
            ]
            return entries

    def publish_command(self, service_id, action):
        if service_id not in self.services:
            raise ValueError("Servizio non consentito.")
        if action not in {"start", "stop", "restart"}:
            raise ValueError("Azione non consentita.")
        with self._lock:
            if not self._connected:
                raise RuntimeError("Dashboard non connessa al broker MQTT.")
            if time.time() - self._last_status_at > STATUS_STALE_S:
                raise RuntimeError("Agente Raspberry non connesso al broker MQTT.")

        request_id = str(uuid.uuid4())
        command = {
            "version": 1,
            "device_id": self.device_id,
            "request_id": request_id,
            "timestamp": time.time(),
            "nonce": uuid.uuid4().hex,
            "service": service_id,
            "action": action,
        }
        info = self._client.publish(
            self.command_topic,
            json.dumps(command, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Invio comando MQTT fallito (rc={info.rc}).")

        deadline = time.monotonic() + COMMAND_TIMEOUT_S
        with self._condition:
            while request_id not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Nessuna conferma dall’agente Raspberry entro "
                        f"{COMMAND_TIMEOUT_S} secondi."
                    )
                self._condition.wait(remaining)
            return dict(self._results.pop(request_id))

    def request_log_snapshot(self, service_id, count):
        if service_id not in self.services:
            raise ValueError("Servizio non consentito.")
        if not 1 <= count <= 500:
            raise ValueError("Il numero di righe deve essere tra 1 e 500.")
        with self._lock:
            if not self._connected:
                raise RuntimeError("Dashboard non connessa al broker MQTT.")

        payload = {
            "version": 1,
            "device_id": self.device_id,
            "timestamp": time.time(),
            "nonce": uuid.uuid4().hex,
            "service": service_id,
            "action": "logs",
            "lines": count,
        }
        info = self._client.publish(
            self.command_topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Richiesta log MQTT fallita (rc={info.rc}).")
