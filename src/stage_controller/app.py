"""Small HTTP API; motor and serial behavior live outside the web framework."""

import secrets

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from labkit.auth import bearer_matches, host_allowed
from labkit.errors import ServiceError
from labkit.flask import contract_blueprint

from .controller import MotorController
from .protocol import ControllerError, LinkError

# The page is served without the access token: it shows layout only, never ports or events.
PAGE_STATE = ("demo", "baud_rate", "microstep_modes", "defaults", "limits")


def create_app(controller: MotorController | None = None, token: str | None = None):
    app = Flask(__name__)
    # Host names are checked below: with an access token, any host name is accepted.
    app.config.update(MAX_CONTENT_LENGTH=4096)
    controller = controller or MotorController()
    app.extensions["motor_controller"] = controller
    app.register_blueprint(
        contract_blueprint(controller, token=token, base_path="/contract", lease=controller.lease)
    )
    control_token = secrets.token_urlsafe(32)

    @app.before_request
    def protect_local_control():
        # The contract enforces its own client/JSON/access checks. Legacy requests
        # still require the page's control token, even when they send a client ID.
        if request.blueprint == "labkit_contract":
            return
        if not host_allowed(request.headers, token):
            return jsonify(error="Open the panel at 127.0.0.1 or localhost"), 403
        if (
            token
            and request.path.startswith("/api/")
            and not bearer_matches(request.headers.get("Authorization", ""), token)
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

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

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

    def state_response(state):
        # Controller operations have released their device lock before this reads the lease.
        return jsonify({**state, "lease": controller.lease.snapshot()})

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
        return jsonify(controller.panel_snapshot())

    @app.post("/api/connect")
    def connect():
        port = body().get("port")
        if not isinstance(port, str) or not port:
            raise ValueError("Select a serial port.")
        return state_response(controller.connect(port))

    @app.post("/api/disconnect")
    def disconnect():
        return state_response(controller.disconnect())

    @app.post("/api/motors/<motor>/move")
    def move(motor):
        data = body()
        return state_response(
            controller.manual(
                "move", motor, data.get("direction"), data.get("speed"), data.get("steps")
            )
        )

    @app.post("/api/microstep")
    def microstep():
        return state_response(controller.manual("set_microstep", body().get("microstep")))

    @app.post("/api/motors/<motor>/home")
    def home(motor):
        body()
        return state_response(controller.manual("home", motor))

    @app.post("/api/motors/<motor>/awake")
    def awake(motor):
        return state_response(controller.manual("awake", motor, body().get("seconds")))

    @app.post("/api/motors/<motor>/goto")
    def goto(motor):
        return state_response(controller.manual("goto", motor, body().get("name")))

    @app.post("/api/poses/<name>")
    def pose(name):
        body()
        return state_response(controller.manual("pose", name))

    @app.post("/api/stop")
    def stop():
        return state_response(controller.stop_all())

    @app.post("/api/motors/<motor>/hold")
    def hold(motor):
        data = body()
        return state_response(
            controller.manual(
                "hold",
                motor,
                data.get("direction"),
                data.get("speed"),
                data.get("hold_id"),
                data.get("sampled_at"),
            )
        )

    @app.post("/api/motors/<motor>/hold/keepalive")
    def keep_hold_alive(motor):
        return state_response(controller.manual("keep_hold_alive", motor, body().get("hold_id")))

    @app.post("/api/motors/<motor>/hold/stop")
    def release_hold(motor):
        return state_response(controller.release_hold(motor, body().get("hold_id")))

    return app
