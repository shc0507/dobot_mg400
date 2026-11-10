"""Digital Twin workspace routes; use existing Console request identity."""

import threading
from pathlib import Path

from flask import Response, jsonify, request

from labkit.lease import validate_client
from orchestrator.web.twin import PROFILE_ROOT, CameraFrames, TwinConsole
from vision.stage_twin import MAX_PROFILE_BYTES

from . import Workspace


def register(blueprint, service):
    manager = None
    lock = threading.Lock()

    @blueprint.before_app_request
    def upload_limit():
        if request.endpoint == blueprint.name + ".profile_upload":
            # The application parses JSON before workspace actions run. A JSON
            # string can escape each profile byte as 6 ASCII bytes.
            request.max_content_length = 6 * MAX_PROFILE_BYTES + 1024

    def console():
        nonlocal manager
        with lock:
            if manager is None:
                demo = bool(getattr(service, "demo", False))
                if demo and getattr(service, "temporary", None) is None:
                    raise RuntimeError("The demo service has not started.")
                root = (
                    Path(service.temporary.name) / "calibration" / "twin" if demo else PROFILE_ROOT
                )
                manager = TwinConsole(
                    root, CameraFrames(getattr(service, "settings", {}), demo=demo)
                )
            return manager

    def page():
        return console().page(validate_client(request.headers.get("X-MG400-Client")))

    @blueprint.get("/state")
    def state():
        current = page()
        with current.lock:
            return jsonify(console().state(current))

    @blueprint.get("/frame")
    def frame():
        data, _ = console().read(request.args.get("camera", "camera1"))
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    def action(name, allowed):
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Send the supported Digital Twin fields as a JSON object.")
        current = page()
        with current.lock:
            if name == "measure":
                result = console().measure(current, data)
            elif name == "capture":
                result = console().capture(current, data)
            elif name == "tuning/cancel":
                current.policy.cancel()
                result = console().state(current)
            else:
                result = console().mutate(current, name, data)
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
    id="twin", order=35, title={"en": "Digital Twin", "zh": "数字孪生"}, register=register
)
