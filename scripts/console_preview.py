"""Serve the real Console UI and offline Digital Twin on native Windows or Linux.

Run ``python scripts/console_preview.py`` and open ``http://127.0.0.1:8790/lab/#twin``.
Profiles persist in ``work/console-preview``. Device controls remain unavailable.
The fixed camera scene supports UI checks, not device motion or live calibration.
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labkit.errors import ServiceError  # noqa: E402
from orchestrator.clients import SERVICES  # noqa: E402
from orchestrator.web.console_routes import console_blueprints  # noqa: E402
from orchestrator.web.twin import ProfileStore  # noqa: E402
from orchestrator.web.workspaces.cameras import SourceError  # noqa: E402
from vision.stage_twin import ENDPOINTS, capture_endpoint, new_profile  # noqa: E402

UNAVAILABLE = "Devices are unavailable in the local preview."


def unavailable(*args, **kwargs):
    raise ServiceError(503, UNAVAILABLE)


class OfflineCameras:
    """Prevent the camera workspace from opening upstream connections."""

    def configured(self, view):
        return False

    def modes(self):
        return {}

    preview_fps = modes

    def unavailable(self, *args):
        raise SourceError(UNAVAILABLE)

    open_stream = still = thermal_frame = thermal_describe = detections = unavailable


def seed_profile(root):
    """Create a synthetic calibration only when the profile directory is absent."""
    if root.exists():
        return
    import cv2
    import numpy as np

    profile = new_profile(name="Mock stage")
    profile["camera"].update(device_id="demo:camera1", label="Stage", dimensions=[640, 480])
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    moving = {"x": 18, "y": 14, "insert": 22}
    for endpoint, definition in ENDPOINTS.items():
        pixels = np.full((480, 640), 255, dtype=np.uint8)
        offset = -45 if definition["limit"] == "min" else 45
        for marker, x, y in ((10, 40, 40), (14, 180, 160), (18, 320, 160), (22, 460, 300)):
            if marker == moving[definition["axis"]]:
                if definition["axis"] == "y":
                    y += offset
                else:
                    x += offset
            pixels[y : y + 60, x : x + 60] = cv2.aruco.generateImageMarker(dictionary, marker, 60)
        encoded = cv2.imencode(".jpg", pixels)[1].tobytes()
        profile["endpoints"][endpoint] = capture_endpoint(encoded, endpoint)
    ProfileStore(root).save(profile)


def create_app(storage_root=ROOT / "work" / "console-preview"):
    """Mount the production workspaces with local profiles and disconnected devices."""
    seed_profile(Path(storage_root) / "calibration" / "twin")
    operations = []
    console = SimpleNamespace(
        commit="local preview",
        clock=time.monotonic,
        lock=threading.Lock(),
        raw={},
        events_at=None,
        refresh_events=lambda: None,
        control=lambda page: [],
        event_report=lambda: {"events": [], "errors": {}},
        stop=lambda name: {"outcome": "unavailable", "error": UNAVAILABLE},
    )
    for name in (
        "describe",
        "read",
        "take",
        "release",
        "release_all",
        "heartbeat",
        "command",
        "operator",
        "keys",
        "stage_hold",
        "stage_release",
    ):
        setattr(console, name, unavailable)
    dashboard = SimpleNamespace(
        refresh=lambda: None,
        snapshot=lambda: {
            "services": [
                {"id": name, "phase": "unavailable", "connected": False, "demo": True}
                for name in SERVICES
            ],
            "cameras": [],
            "orchestrator": {"stale": False},
            "run": None,
            "operations": list(operations),
        },
    )
    stage = SimpleNamespace(
        state=lambda: {
            "connected": False,
            "demo": True,
            "motors": [
                {"id": name, "mode": "idle", "available": False}
                for name in ("bottom", "top", "insert")
            ],
        },
        close=lambda: None,
    )
    console.read = lambda name, path: (
        {"ports": []} if name == "stage" and path == "api/ports" else unavailable()
    )
    service = SimpleNamespace(
        demo=True,
        settings={},
        # The Twin demo route uses this name as its storage root. It is not temporary here.
        temporary=SimpleNamespace(name=str(storage_root)),
        console=console,
        dashboard=dashboard,
        camera_sources=OfflineCameras(),
        dashboard_configured=lambda name: name == "stage",
        console_client=lambda name, *args: stage if name == "stage" else None,
    )
    app = Flask(__name__)

    for blueprint in console_blueprints(service, "/lab"):
        app.register_blueprint(blueprint)

    @app.get("/")
    def index():
        return redirect("/lab/#twin")

    @app.post("/lab/api/dashboard/<action>")
    def stop_all(action):
        if action not in {"motion", "heat", "gas"}:
            abort(404)
        targets = ("robot", "stage", "microscope") if action == "motion" else ("furnace",)
        operation = {
            "id": str(uuid4()),
            "action": action,
            "phase": "done",
            "done": True,
            "hold": "unavailable",
            "results": {
                name: {"status": "unavailable", "message": UNAVAILABLE} for name in targets
            },
        }
        operations.append(operation)
        del operations[:-20]
        return jsonify(operation_id=operation["id"]), 202

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(RuntimeError)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(OSError)
    def storage_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    from waitress import serve

    app = create_app()
    print(f"Local preview: http://127.0.0.1:{args.port}/lab/#twin", flush=True)
    serve(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
