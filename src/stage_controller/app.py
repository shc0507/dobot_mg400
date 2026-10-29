"""Small HTTP API; motor and serial behavior live outside the web framework."""

import ipaddress
import secrets
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .controller import MotorController
from .protocol import ControllerError, LinkError

FORWARDED_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")
# The page is served without the access token: it shows layout only, never ports or events.
PAGE_STATE = ("demo", "baud_rate", "microstep_modes", "defaults", "limits")


def _host_port(value, default_port):
    try:
        parts = urlsplit("//" + value.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        return None
    return parts.hostname.lower(), port or default_port


def loopback_name(name):
    """Check that a host name or IP address names this machine's loopback interface."""
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return name.lower() == "localhost"


def loopback_host(value):
    """Check that a Host header names this machine's loopback interface."""
    parts = _host_port(value, 80)
    return parts is not None and loopback_name(parts[0])


def create_app(controller: MotorController | None = None, token: str | None = None):
    app = Flask(__name__)
    # Host names are checked below: with an access token, any host name is accepted.
    app.config.update(MAX_CONTENT_LENGTH=4096)
    controller = controller or MotorController()
    app.extensions["motor_controller"] = controller
    control_token = secrets.token_urlsafe(32)

    @app.before_request
    def protect_local_control():
        if not token:
            # A DNS rebinding page reaches a loopback server under its own host name,
            # and a proxy on this host can rewrite Host. Both need an access token.
            forwarded = any(request.headers.get(name) is not None for name in FORWARDED_HEADERS)
            if forwarded or not loopback_host(request.headers.get("Host", "")):
                return jsonify(error="Open the panel at 127.0.0.1 or localhost"), 403
        elif request.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("Authorization", "").encode("latin-1", "replace"),
            f"Bearer {token}".encode("latin-1", "replace"),
        ):
            return jsonify(error="Enter the panel access token"), 401
        if request.method != "GET":
            if not secrets.compare_digest(
                request.headers.get("X-Control-Token", "").encode("latin-1", "replace"),
                control_token.encode(),
            ):
                return jsonify(error="Reload this page before sending another command."), 403
            if not request.is_json:
                return jsonify(error="Expected a JSON request."), 415

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(ControllerError)
    def controller_error(error):
        return jsonify(error=str(error)), 503 if isinstance(error, LinkError) else 409

    @app.errorhandler(ValueError)
    def value_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    @app.get("/")
    def index():
        state = controller.snapshot()
        page = {key: state[key] for key in PAGE_STATE}
        page["motors"] = [{"id": motor["id"]} for motor in state["motors"]]
        return render_template("index.html", token=control_token, state=page)

    @app.get("/api/ports")
    def ports():
        return jsonify(ports=controller.ports())

    @app.get("/api/state")
    def state():
        return jsonify(controller.snapshot())

    @app.post("/api/connect")
    def connect():
        port = body().get("port")
        if not isinstance(port, str) or not port:
            raise ValueError("Select a serial port.")
        return jsonify(controller.connect(port))

    @app.post("/api/disconnect")
    def disconnect():
        return jsonify(controller.disconnect())

    @app.post("/api/motors/<motor>/move")
    def move(motor):
        data = body()
        return jsonify(
            controller.move(motor, data.get("direction"), data.get("speed"), data.get("steps"))
        )

    @app.post("/api/microstep")
    def microstep():
        return jsonify(controller.set_microstep(body().get("microstep")))

    @app.post("/api/stop")
    def stop():
        return jsonify(controller.stop_all())

    @app.post("/api/motors/<motor>/hold")
    def hold(motor):
        data = body()
        return jsonify(
            controller.hold(
                motor,
                data.get("direction"),
                data.get("speed"),
                data.get("hold_id"),
                data.get("sampled_at"),
            )
        )

    @app.post("/api/motors/<motor>/hold/keepalive")
    def keep_hold_alive(motor):
        return jsonify(controller.keep_hold_alive(motor, body().get("hold_id")))

    @app.post("/api/motors/<motor>/hold/stop")
    def release_hold(motor):
        return jsonify(controller.release_hold(motor, body().get("hold_id")))

    return app
