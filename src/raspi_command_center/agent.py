import json
import subprocess
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from .settings import CONFIG
SYSTEMD_PROPERTIES = (
    "ActiveState",
    "SubState",
    "MainPID",
    "ActiveEnterTimestamp",
    "ExecMainStartTimestamp",
    "Result",
)
COMMAND_MAX_AGE_S = 30
STATUS_INTERVAL_S = 5
LOG_COMMANDS = {
    "kart-telemetry": "kart-telemetry.service",
    "sync-logs": "sync-logs.service",
}


class RaspberryAgent:
    def __init__(self, config):
        self.config = config
        self.device_id = config["device_id"]
        self.services = config["services"]
        self.topic_root = f"{config['topic_prefix']}/{self.device_id}"
        self.command_topic = f"{self.topic_root}/command"
        self.status_topic = f"{self.topic_root}/status"
        self.result_topic = f"{self.topic_root}/result"
        self.log_topic = f"{self.topic_root}/logs"
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._seen_nonces = {}
        self._journal_processes = []
        mqtt_config = config["mqtt"]
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"kart-command-agent-{self.device_id}-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._mqtt_config = mqtt_config

    def run(self):
        self._client.connect(
            self._mqtt_config["broker"],
            self._mqtt_config["port"],
            self._mqtt_config.get("keepalive_s", 60),
        )
        self._client.loop_start()
        journal_threads = [
            threading.Thread(
                target=self._follow_journal,
                args=(service_id,),
                daemon=True,
            )
            for service_id in LOG_COMMANDS
        ]
        for thread in journal_threads:
            thread.start()
        try:
            while not self._stopping.wait(STATUS_INTERVAL_S):
                self._publish_status()
        finally:
            self.stop()

    def stop(self):
        self._stopping.set()
        for process in self._journal_processes:
            if process.poll() is None:
                process.terminate()
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "value", reason_code) != 0:
            return
        client.subscribe(self.command_topic, qos=1)
        self._publish_status()
        print(
            f"[command-agent] connected to "
            f"{self._mqtt_config['broker']} as {self.device_id}",
            flush=True,
        )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        print(f"[command-agent] MQTT disconnected: {reason_code}", flush=True)

    def _publish(self, topic, payload, qos=1):
        info = self._client.publish(
            topic,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            qos=qos,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"[command-agent] MQTT publish failed rc={info.rc}", flush=True)
            return False
        return True

    def _publish_status(self):
        now = time.time()
        self._publish(self.status_topic, {
            "version": 1,
            "device_id": self.device_id,
            "timestamp": now,
            "services": self._collect_status(),
        })

    def _collect_status(self):
        result = {}
        for service_id, unit in self.services.items():
            try:
                completed = subprocess.run(
                    [
                        "systemctl",
                        "show",
                        "--no-page",
                        f"--property={','.join(SYSTEMD_PROPERTIES)}",
                        unit,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        completed.stderr.strip() or "systemctl show failed"
                    )
                properties = dict(
                    line.split("=", 1)
                    for line in completed.stdout.splitlines()
                    if "=" in line
                )
                result[service_id] = {
                    "active_state": properties.get("ActiveState", "unknown"),
                    "sub_state": properties.get("SubState", "unknown"),
                    "main_pid": properties.get("MainPID", "0"),
                    "active_since": properties.get("ActiveEnterTimestamp", ""),
                    "last_started": properties.get("ExecMainStartTimestamp", ""),
                    "result": properties.get("Result", ""),
                }
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
                result[service_id] = {
                    "active_state": "error",
                    "sub_state": str(error),
                    "main_pid": "0",
                    "active_since": "",
                    "last_started": "",
                    "result": "error",
                }
        return result

    def _on_message(self, client, userdata, message):
        if message.retain:
            print("[command-agent] ignored retained command", flush=True)
            return

        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            print("[command-agent] ignored invalid JSON command", flush=True)
            return
        if not isinstance(payload, dict):
            return
        if payload.get("device_id") != self.device_id:
            return

        request_id = payload.get("request_id")
        nonce = payload.get("nonce")
        timestamp = payload.get("timestamp")
        if (
            not isinstance(nonce, str)
            or not 1 <= len(nonce) <= 128
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or abs(time.time() - timestamp) > COMMAND_MAX_AGE_S
        ):
            return

        with self._lock:
            self._seen_nonces = {
                saved_nonce: saved_at
                for saved_nonce, saved_at in self._seen_nonces.items()
                if time.time() - saved_at <= COMMAND_MAX_AGE_S
            }
            if nonce in self._seen_nonces:
                return
            self._seen_nonces[nonce] = time.time()

        service_id = payload.get("service")
        action = payload.get("action")
        if action == "logs":
            self._publish_log_snapshot(service_id, payload.get("lines", 250))
            return

        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            return

        if service_id not in self.services or action not in {
            "start",
            "stop",
            "restart",
        }:
            self._publish_result(
                request_id,
                service_id,
                action,
                False,
                "Azione o servizio non consentito.",
            )
            return

        unit = self.services[service_id]
        try:
            completed = subprocess.run(
                ["sudo", "-n", "systemctl", action, unit],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            success = completed.returncode == 0
            detail = completed.stderr.strip() or completed.stdout.strip()
            if not success and not detail:
                detail = f"systemctl terminato con codice {completed.returncode}."
        except (OSError, subprocess.TimeoutExpired) as error:
            success = False
            detail = str(error)

        self._publish_result(
            request_id,
            service_id,
            action,
            success,
            detail,
        )
        self._publish_status()

    def _publish_log_snapshot(self, service_id, count):
        if (
            service_id not in LOG_COMMANDS
            or not isinstance(count, int)
            or not 1 <= count <= 500
        ):
            return
        try:
            completed = subprocess.run(
                [
                    "journalctl",
                    "--no-pager",
                    "--output=short-iso",
                    f"--lines={count}",
                    "--unit",
                    self.services[service_id],
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                output = completed.stderr.strip() or (
                    f"journalctl terminato con codice {completed.returncode}"
                )
                self._publish_log(service_id, output)
                return
            for line in completed.stdout.splitlines():
                self._publish_log(service_id, line)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._publish_log(service_id, f"journalctl snapshot error: {error}")

    def _publish_log(self, service_id, line):
        self._publish(self.log_topic, {
            "version": 1,
            "device_id": self.device_id,
            "timestamp": time.time(),
            "service": service_id,
            "line": line,
        }, qos=0)

    def _publish_result(self, request_id, service, action, success, detail):
        self._publish(self.result_topic, {
            "version": 1,
            "device_id": self.device_id,
            "request_id": request_id,
            "timestamp": time.time(),
            "service": service,
            "action": action,
            "success": success,
            "message": detail,
        })

    def _follow_journal(self, service_id):
        unit = self.services[service_id]
        while not self._stopping.is_set():
            try:
                process = subprocess.Popen(
                    [
                        "journalctl",
                        "--no-pager",
                        "--output=short-iso",
                        "--lines=250",
                        "--follow",
                        "--unit",
                        unit,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._journal_processes.append(process)
                for line in process.stdout:
                    if self._stopping.is_set():
                        break
                    self._publish_log(service_id, line.rstrip())
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(
                    f"[command-agent] journal stream {service_id} failed: {error}",
                    flush=True,
                )
            if not self._stopping.wait(2):
                continue


def main():
    agent = RaspberryAgent(CONFIG)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()


if __name__ == "__main__":
    main()
