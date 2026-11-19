"""Stage contract at the service base path.

The canonical prefix is ``<base>/api/``. The existing ``<base>/contract/api/``
prefix aliases the same handlers, lease and rotation units. Camera references
are inactive with encoder firmware.
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
    base = normalize_base_path(base_path).rstrip("/")
    prefix = base
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

    dispatch = app.wsgi_app
    legacy_prefix = base + "/contract/api/"

    def legacy_api(environ, start_response):
        # Rewrite before routing so both prefixes share every handler and access check.
        path = environ.get("PATH_INFO", "")
        if path.startswith(legacy_prefix):
            environ["PATH_INFO"] = prefix + "/api/" + path[len(legacy_prefix) :]
        return dispatch(environ, start_response)

    app.wsgi_app = legacy_api
    return app
