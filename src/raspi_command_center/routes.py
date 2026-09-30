import json
import time

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)


command_center = Blueprint(
    "command_center",
    __name__,
    template_folder="templates",
)

bridge = None
bridge_error = "Bridge MQTT non inizializzato."


def configure_bridge(mqtt_bridge=None, error=None):
    global bridge, bridge_error
    bridge = mqtt_bridge
    bridge_error = error or "Configurazione MQTT non disponibile."


def _bridge_or_error():
    if bridge is None:
        return None, (jsonify({"ok": False, "error": bridge_error}), 503)
    return bridge, None


@command_center.get("/command-center")
@command_center.get("/")
def page():
    return render_template("command_center.html")


@command_center.get("/api/command-center/config")
def config():
    mqtt_bridge, error = _bridge_or_error()
    if error:
        return error
    return jsonify({
        "ok": True,
        "device_id": mqtt_bridge.device_id,
        "topic_root": mqtt_bridge.topic_root,
        "health": mqtt_bridge.health(),
    })


@command_center.get("/api/command-center/services")
def services():
    mqtt_bridge, error = _bridge_or_error()
    if error:
        return error
    return jsonify({
        "ok": True,
        "health": mqtt_bridge.health(),
        "services": mqtt_bridge.service_statuses(),
    })


@command_center.get("/api/command-center/logs")
def logs():
    mqtt_bridge, error = _bridge_or_error()
    if error:
        return error

    service_id = request.args.get("service", "kart-telemetry")
    if service_id not in mqtt_bridge.services:
        return jsonify({"ok": False, "error": "Servizio non consentito."}), 400

    try:
        count = int(request.args.get("lines", "150"))
    except ValueError:
        return jsonify({"ok": False, "error": "Numero righe non valido."}), 400
    if not 1 <= count <= 500:
        return jsonify({"ok": False, "error": "Le righe devono essere tra 1 e 500."}), 400

    entries = mqtt_bridge.latest_logs(service_id, count)
    return jsonify({
        "ok": True,
        "service": service_id,
        "lines": [entry["line"] for entry in entries],
    })


@command_center.get("/api/command-center/logs/live")
def live_logs():
    mqtt_bridge, error = _bridge_or_error()
    if error:
        return error

    service_id = request.args.get("service", "kart-telemetry")
    if service_id not in mqtt_bridge.services:
        return jsonify({"ok": False, "error": "Servizio non consentito."}), 400

    try:
        count = int(request.args.get("lines", "80"))
    except ValueError:
        return jsonify({"ok": False, "error": "Numero righe non valido."}), 400
    if not 0 <= count <= 500:
        return jsonify({"ok": False, "error": "Le righe devono essere tra 0 e 500."}), 400

    cursor = mqtt_bridge.latest_log_sequence(service_id)
    try:
        mqtt_bridge.request_log_snapshot(service_id, count)
    except RuntimeError as error:
        return jsonify({"ok": False, "error": str(error)}), 503

    @stream_with_context
    def generate():
        nonlocal cursor
        while True:
            entries = mqtt_bridge.logs_after(service_id, cursor, timeout=10)
            if not entries:
                yield f": keepalive {int(time.time())}\n\n"
                continue
            for entry in entries:
                cursor = max(cursor, entry["sequence"])
                yield f"data: {json.dumps({'line': entry['line']})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@command_center.post("/api/command-center/services/<service_id>/<action>")
def service_action(service_id, action):
    mqtt_bridge, error = _bridge_or_error()
    if error:
        return error
    if service_id not in mqtt_bridge.services:
        return jsonify({"ok": False, "error": "Servizio non consentito."}), 400
    if action not in {"start", "stop", "restart"}:
        return jsonify({"ok": False, "error": "Azione non consentita."}), 400

    try:
        result = mqtt_bridge.publish_command(service_id, action)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except TimeoutError as error:
        return jsonify({"ok": False, "error": str(error)}), 504
    except RuntimeError as error:
        return jsonify({"ok": False, "error": str(error)}), 503

    if not result.get("success"):
        return jsonify({
            "ok": False,
            "error": result.get("message") or "Il Raspberry non ha completato il comando.",
            "result": result,
        }), 502

    return jsonify({
        "ok": True,
        "service": service_id,
        "action": action,
        "message": result.get("message", ""),
        "completed_at": result.get("timestamp"),
    })
