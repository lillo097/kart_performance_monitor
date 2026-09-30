import atexit
import sys
from pathlib import Path

from flask import Flask

if __package__:
    from .bridge import MqttBridge
    from .routes import command_center, configure_bridge
    from .settings import CONFIG, DASHBOARD_HOST, DASHBOARD_PORT
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.raspi_command_center.bridge import MqttBridge
    from src.raspi_command_center.routes import command_center, configure_bridge
    from src.raspi_command_center.settings import CONFIG, DASHBOARD_HOST, DASHBOARD_PORT


def create_app():
    application = Flask(__name__, template_folder="templates")
    application.register_blueprint(command_center)
    try:
        bridge = MqttBridge(CONFIG)
        bridge.start()
        configure_bridge(bridge)
        atexit.register(bridge.stop)
    except (ValueError, RuntimeError) as error:
        configure_bridge(error=str(error))
        application.logger.error("Command center MQTT setup failed: %s", error)
    return application


app = create_app()


if __name__ == "__main__":
    app.run(
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
