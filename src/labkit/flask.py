"""Flask routes for services that implement instrument contract version 1.

The service supplies describe(), snapshot(), command(action, args, client),
stop(client, wait_s), on_lease_expired(), and an EventLog named events. command
returns a command ID. stop returns confirmed, pending, or unconfirmed. The service
owns its device lock and updates last_command when a command finishes. command
must recheck locked and device state under that same lock before device access. Its poll
loop must call lease.poll(), including when no client sends HTTP requests.
"""

import math

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from .auth import bearer_matches, host_allowed, same_origin, valid_token_for_bind
from .errors import ServiceError
from .lease import validate_client


def contract_blueprint(service, *, token=None, base_path="/", lease):
    """Attach contract routes without starting threads or connecting to devices."""
    if not isinstance(token, (str, type(None))) or not valid_token_for_bind(token, "localhost"):
        raise ValueError("A configured token needs at least 24 printable ASCII characters.")
    if not isinstance(base_path, str) or not base_path.startswith("/"):
        raise ValueError("The service base path must start with a slash.")
    if any(part in base_path for part in ("?", "#", "//")) or ".." in base_path.split("/"):
        raise ValueError("Invalid service base path.")
    if lease.on_expire is None:
        lease.on_expire = service.on_lease_expired
    bp = Blueprint("labkit_contract", __name__, url_prefix=base_path.rstrip("/"))

    @bp.before_request
    def access():
        if not host_allowed(request.headers, token):
            raise ServiceError(403, "Use a loopback Host without forwarded headers.")
        if token and not bearer_matches(request.headers.get("Authorization", ""), token):
            raise ServiceError(401, "Invalid service access token.")
        if not same_origin(request.headers):
            raise ServiceError(403, "The request origin is not allowed.")
        if request.method == "POST":
            if request.mimetype != "application/json":
                raise ServiceError(415, "Expected Content-Type: application/json.")
            validate_client(request.headers.get("X-MG400-Client"))

    @bp.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @bp.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @bp.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify(error=str(error)), 400

    @bp.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def snapshot():
        lease_state = lease.snapshot()
        state = dict(service.snapshot())
        state["lease"] = lease_state
        return state

    @bp.get("/api/describe")
    def discovery():
        return jsonify(service.describe())

    @bp.get("/api/state")
    def state():
        lease.poll()
        return jsonify(snapshot())

    @bp.get("/api/events")
    def events():
        raw = request.args.get("since", "0")
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError("Event sequence must be a nonnegative integer.")
        return jsonify(service.events.since(int(raw)))

    @bp.post("/api/heartbeat")
    def heartbeat():
        body()
        return jsonify(lease=lease.acquire(request.headers["X-MG400-Client"]))

    @bp.post("/api/release")
    def release():
        body()
        return jsonify(lease=lease.release(request.headers["X-MG400-Client"]))

    @bp.post("/api/stop")
    def stop():
        wait_s = body().get("wait_s", 0)
        # JSON integers can exceed the floating-point range used by isfinite.
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not 0 <= wait_s <= 15
            or not math.isfinite(wait_s)
        ):
            raise ValueError("wait_s must be a finite number from 0 to 15.")
        # Stop does not wait for a lease transition or require its holder.
        outcome = service.stop(request.headers["X-MG400-Client"], wait_s)
        return jsonify(stop=outcome, state=snapshot())

    @bp.post("/api/command")
    def command():
        data = body()
        action = data.pop("action", None)
        commands = service.describe()["commands"]
        if not isinstance(action, str) or action not in commands:
            raise ValueError("Unknown command action.")
        client = request.headers["X-MG400-Client"]

        def dispatch():
            if service.snapshot()["locked"]:
                raise ServiceError(409, "The service is locked. Only stop is allowed.")
            return service.command(action, data, client)

        with lease.control(client, require_holder=not commands[action]["any_client"]):
            command_id = dispatch()
        return jsonify(command_id=command_id, state=snapshot()), 202

    return bp
