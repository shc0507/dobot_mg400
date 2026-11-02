"""Microscope contract routes and explicit operator focus-reference requests.

The service owns both lease classifications and every device operation. Creating
this app does not start the service, connect devices, or infer operator authority
from a client identifier.
"""

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from labkit.auth import bearer_matches, host_allowed, same_origin
from labkit.errors import ServiceError
from labkit.flask import contract_blueprint
from labkit.lease import validate_client


def create_app(service, *, token=None, base_path="/"):
    """Expose the shared contract plus the separate, confirmed operator action."""
    blueprint = contract_blueprint(service, token=token, base_path=base_path, lease=service.lease)
    prefix = base_path.rstrip("/")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.register_blueprint(blueprint)

    @app.before_request
    def access():
        if not host_allowed(request.headers, token):
            raise ServiceError(403, "Use a loopback Host without forwarded headers.")
        if token and not bearer_matches(request.headers.get("Authorization", ""), token):
            raise ServiceError(401, "Invalid service access token.")
        if not same_origin(request.headers):
            raise ServiceError(403, "The request origin is not allowed.")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.mimetype != "application/json":
                raise ServiceError(415, "Expected Content-Type: application/json.")
            validate_client(request.headers.get("X-MG400-Client"))

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def state():
        return {**service.snapshot(), "lease": service.lease.snapshot()}

    @app.post(prefix + "/api/manual/<operation>")
    def manual_lease(operation):
        if body():
            raise ValueError("Manual lease requests take an empty object.")
        client = request.headers["X-MG400-Client"]
        if operation == "heartbeat":
            value = service.manual_heartbeat(client)
        elif operation == "renew":
            value = service.manual_heartbeat(client, renew_only=True)
        elif operation == "release":
            value = service.manual_release(client)
        else:
            raise ValueError("Unknown manual lease operation.")
        return jsonify(lease=value)

    @app.post(prefix + "/api/manual/set_focus_zero")
    def set_focus_zero():
        args = body()
        if set(args) != {"confirm"} or args["confirm"] is not True:
            raise ValueError("Focus zero requires confirm=true from the operator.")
        command_id = service.manual_command(
            "set_focus_zero", args, request.headers["X-MG400-Client"]
        )
        return jsonify(command_id=command_id, state=state()), 202

    return app
