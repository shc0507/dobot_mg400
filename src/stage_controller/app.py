"""Stage contract at the service base path.

Motor and serial behavior live outside the web framework. The only route outside
the contract is ``GET api/motors/<motor>/reference``: a camera check of one axis,
too large for the state.
"""

import re

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from labkit.flask import contract_blueprint

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
    prefix = normalize_base_path(base_path).rstrip("/")
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=4096)
    controller = controller or MotorController()
    app.extensions["motor_controller"] = controller
    app.register_blueprint(
        contract_blueprint(controller, base_path=prefix or "/", lease=controller.lease)
    )

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
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

    @app.get(prefix + "/api/motors/<motor>/reference")
    def reference(motor):
        return jsonify(controller.preview_reference(motor))

    return app
