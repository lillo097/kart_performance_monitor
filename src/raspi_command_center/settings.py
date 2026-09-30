MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_KEEPALIVE_S = 60

DEVICE_ID = "kart-rasp-01"
MQTT_TOPIC_PREFIX = "kart-performance-monitor"

SERVICE_UNITS = {
    "kart-telemetry": "kart-telemetry.service",
    "sync-logs": "sync-logs.service",
}

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8081

CONFIG = {
    "device_id": DEVICE_ID,
    "topic_prefix": MQTT_TOPIC_PREFIX,
    "mqtt": {
        "broker": MQTT_BROKER,
        "port": MQTT_PORT,
        "keepalive_s": MQTT_KEEPALIVE_S,
    },
    "services": SERVICE_UNITS,
}
