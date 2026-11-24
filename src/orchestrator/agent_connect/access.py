"""Apply Console authority to legacy write routes without blocking emergency stops."""

import hashlib
import re
from urllib.parse import urlsplit

from flask import g, request

from labkit.errors import ServiceError
from labkit.lease import validate_client

EPOCH_HEADER = "X-MG400-Control-Epoch"
METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEVICES = {"robot", "stage", "thermal", "vision", "furnace", "microscope"}
STOP_PATHS = {
    "/api/stop",
    "/api/agent/control/stop",
    "/api/dashboard/motion",
    "/api/dashboard/heat",
    "/api/dashboard/gas",
    "/api/console/ws/process/gas-stop",
    "/api/console/ws/sample-transfer/stop",
    "/api/console/ws/motion/stage/release",
}


def is_stop(path, data=None):
    """Recognize fixed emergency routes, never a caller's action-name substring."""
    if path in STOP_PATHS:
        return True
    if path.startswith("/api/console/stop/"):
        return path.removeprefix("/api/console/stop/") in DEVICES
    return (
        path == "/api/console/ws/motion/keys"
        and isinstance(data, dict)
        and set(data) == {"seq", "held"}
        and type(data["seq"]) is int
        and data["seq"] >= 0
        and data["held"] == []
    )


def same_origin():
    """Reject browser writes from another origin."""
    origin = request.headers.get("Origin")
    if origin is None:
        return
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.lower() != request.host.lower()
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
    ):
        raise ServiceError(403, "Send this request from the Console origin.")


def _credential():
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        # An invalid supplied credential must never become a trusted Console call.
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{16,256})", authorization)
        if match is None:
            raise ServiceError(401, "Send a valid Console session credential.")
        return match[1]
    return None


def _identity(principal):
    value = "agent-" + principal.id
    return validate_client(value)


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _bind_identity(app, principal, token):
    g.agent_principal = principal
    identity = _identity(principal)
    request.environ["HTTP_X_MG400_CLIENT"] = identity
    identities = app.extensions.setdefault("agent_stop_identities", {})
    identities[_token_key(token)] = identity
    if len(identities) > 512:
        identities.pop(list(identities)[0], None)


def _stop_identity(app):
    # A released or expired session may still stop its earlier hold. This cache
    # grants no write access and never takes the authority lock.
    try:
        token = _credential()
    except ServiceError:
        return None
    if not isinstance(token, str) or len(token) > 256:
        return None
    return app.extensions.get("agent_stop_identities", {}).get(_token_key(token))


def protect_request(app, prefix):
    """Check one request. install_access also releases dispatch guards at teardown."""
    prefix = prefix.rstrip("/")
    if prefix and not request.path.startswith(prefix + "/"):
        return
    path = request.path[len(prefix) :]
    if path == "/api/agent/control" or path.startswith("/api/agent/control/"):
        return  # The management routes validate their own identities and input.
    getter = app.extensions.get("agent_authority")
    if getter is None:
        return
    write = request.method in METHODS
    if write and request.method == "POST" and is_stop(path, request.get_json(silent=True)):
        # Stop must not wait for the authority lock, session lookup or a database.
        identity = _stop_identity(app)
        if identity is not None:
            request.environ["HTTP_X_MG400_CLIENT"] = identity
        try:
            validate_client(request.headers.get("X-MG400-Client"))
        except ValueError:
            request.environ["HTTP_X_MG400_CLIENT"] = "stop-unverified"
        return
    if write:
        same_origin()
    try:
        token = _credential()
    except ServiceError:
        if write:
            raise
        return
    if not write and not token:
        return  # Existing public read streams do not require an owner session.
    authority = getter() if callable(getter) else getter
    principal = None
    if token:
        try:
            principal = authority.authenticate(token)
        except ServiceError:
            if write:
                raise
            return
        _bind_identity(app, principal, token)
    if not write:
        return
    if principal is not None:
        raise ServiceError(403, "Send agent commands through the documented command catalog.")
    epoch = request.headers.get(EPOCH_HEADER)
    if epoch is not None and (not epoch or len(epoch) > 256):
        raise ServiceError(409, "Refresh Console control before sending a new command.")
    # Preserve the existing page identity and device leases for trusted humans.
    # Share this dispatch lock with management's idle check and handover.
    guard = authority.human_guard(epoch)
    guard.__enter__()
    g.agent_dispatch_guard = guard


def install_access(app, prefix):
    """Install once, before the legacy request validator binds page identities."""
    if app.extensions.get("agent_access_installed"):
        return
    app.extensions["agent_access_installed"] = True

    @app.before_request
    def agent_access():
        protect_request(app, prefix)

    @app.teardown_request
    def release_guard(error):
        guard = g.pop("agent_dispatch_guard", None)
        if guard is not None:
            guard.__exit__(type(error) if error is not None else None, error, None)
