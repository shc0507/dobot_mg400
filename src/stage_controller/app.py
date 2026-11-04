"""Stage contract plus the Lab Console operator routes.

Motor and serial behavior live outside the web framework. The operator routes
share the contract prefix ``<base>/contract/api/``, so one client URL reaches
both. Operator routes that change the stage need JSON,
``X-MG400-Client`` and the caller's current stage lease.
"""

import re

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from labkit.errors import ServiceError
from labkit.flask import contract_blueprint
from labkit.lease import validate_client

from .controller import MotorController
from .protocol import ControllerError, LinkError


def normalize_base_path(value):
    """Return a literal URL prefix with one trailing slash."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "//" in value
        or any(
            part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._~-]+", part)
            for part in value.strip("/").split("/")
            if value != "/"
        )
    ):
        raise ValueError("Base path must be an absolute URL path with literal segments.")
    return value.rstrip("/") + "/"


def create_app(controller: MotorController | None = None, *, base_path="/"):
    prefix = normalize_base_path(base_path).rstrip("/") + "/contract"
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=4096)
    controller = controller or MotorController()
    app.extensions["motor_controller"] = controller
    app.register_blueprint(contract_blueprint(controller, base_path=prefix, lease=controller.lease))

    @app.before_request
    def protect_local_control():
        # The contract enforces its own client and JSON checks.
        if request.blueprint == "labkit_contract":
            return
        if request.method != "GET":
            if not request.is_json:
                return jsonify(error="Expected a JSON request."), 415
            validate_client(request.headers.get("X-MG400-Client"))

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
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

    def client():
        return request.headers["X-MG400-Client"]

    def state_response(state):
        # Controller operations have released their device lock before this reads the lease.
        return jsonify({**state, "lease": controller.lease.snapshot()})

    @app.get(prefix + "/api/ports")
    def ports():
        return jsonify(ports=controller.ports())

    @app.post(prefix + "/api/connect")
    def connect():
        data = body()
        port = data.get("port")
        if set(data) != {"port"} or not isinstance(port, str) or not port:
            raise ValueError("Select a serial port.")
        return state_response(controller.connect(port, client=client()))

    @app.get(prefix + "/api/motors/<motor>/reference")
    def reference(motor):
        return jsonify(controller.preview_reference(motor))

    @app.post(prefix + "/api/motors/<motor>/teach_reference")
    def teach_reference(motor):
        data = body()
        if set(data) != {"confirm"}:
            raise ValueError("Send confirm only.")
        return state_response(controller.teach_reference(motor, data["confirm"], client=client()))

    return app
