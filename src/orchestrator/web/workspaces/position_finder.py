"""Legacy Position Finder workspace routes; use existing Console request identity."""

import time

from flask import Response, jsonify, request

from labkit.lease import validate_client

from . import Workspace

# Keep the detector and image libraries unloaded until this legacy view is enabled.
MAX_PROFILE_BYTES = 1024 * 1024


def register(blueprint, service):
    @blueprint.before_app_request
    def upload_limit():
        if request.endpoint == blueprint.name + ".profile_upload":
            # The application parses JSON before workspace actions run. A JSON
            # string can escape each profile byte as 6 ASCII bytes.
            request.max_content_length = 6 * MAX_PROFILE_BYTES + 1024

    def console():
        manager = getattr(service, "_twin_console", None)
        if manager is None:
            raise RuntimeError("Activate Position Finder (legacy) first.")
        return manager

    def page():
        client = validate_client(request.headers.get("X-MG400-Client"))
        current = console().pages.get(client)
        if current is None:
            raise RuntimeError("Activate Position Finder (legacy) first.")
        return current

    def require_active(current, client):
        if console().pages.get(client) is not current:
            raise RuntimeError("Activate Position Finder (legacy) first.")
        current.touched = time.monotonic()

    @blueprint.post("/enabled")
    def enabled():
        data = request.get_json()
        if (
            not isinstance(data, dict)
            or set(data) != {"enabled"}
            or type(data["enabled"]) is not bool
        ):
            raise ValueError("Send enabled as true or false.")
        client = validate_client(request.headers.get("X-MG400-Client"))
        if data["enabled"]:
            from orchestrator.web.twin import get_twin_console

            manager = get_twin_console(service)
            current = manager.page(client)
            with current.lock:
                return jsonify(**manager.state(current), enabled=True, client=client)
        manager = getattr(service, "_twin_console", None)
        if manager is not None:
            with manager.lock:
                current = manager.pages.pop(client, None)
                if current is not None:
                    # An accepted camera read may finish, but no further attempts start.
                    current.cancelled.set()
        return jsonify(enabled=False)

    @blueprint.get("/state")
    def state():
        client = validate_client(request.headers.get("X-MG400-Client"))
        manager = getattr(service, "_twin_console", None)
        if manager is None or client not in manager.pages:
            return jsonify(enabled=False)
        current = page()
        with current.lock:
            require_active(current, client)
            return jsonify(**console().state(current), enabled=True, client=client)

    @blueprint.get("/frame")
    def frame():
        client = validate_client(
            request.headers.get("X-MG400-Client") or request.args.get("client")
        )
        manager = console()
        current = manager.pages.get(client)
        if current is None:
            raise RuntimeError("Activate Position Finder (legacy) first.")
        with current.lock:
            require_active(current, client)
            data, _ = manager.read(request.args.get("camera", "camera1"))
            return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    def action(name, allowed):
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Send the supported Position Finder fields as a JSON object.")
        current = page()
        with current.lock:
            require_active(current, request.headers.get("X-MG400-Client"))
            if name == "measure":
                result = console().measure(current, data)
            elif name == "capture":
                result = console().capture(current, data)
            elif name == "tuning/cancel":
                current.policy.cancel()
                result = console().state(current)
            else:
                result = console().mutate(current, name, data)
            if "cameras" in result:
                result.update(enabled=True, client=request.headers.get("X-MG400-Client"))
            if "state" in result:
                result["state"].update(enabled=True, client=request.headers.get("X-MG400-Client"))
            return jsonify(result)

    fields = {
        "source": {"camera"},
        "profile/new": {"name"},
        "profile/edit": {"name", "travel_mm"},
        "profile/select": {"id"},
        "profile/save": {"name", "travel_mm"},
        "profile/upload": {"profile"},
        "profile/delete": {"id"},
        "clear": {"endpoint"},
        "capture": {"endpoint", "automatic", "max_frames"},
        "measure": {"automatic", "max_frames", "tune"},
        "tuning/cancel": set(),
    }
    for name, allowed in fields.items():
        blueprint.add_url_rule(
            "/" + name,
            endpoint=name.replace("/", "_"),
            view_func=lambda name=name, allowed=allowed: action(name, allowed),
            methods=["POST"],
        )


WORKSPACE = Workspace(
    id="position-finder",
    order=100,
    title={"en": "Optional", "zh": "可选"},
    register=register,
)
