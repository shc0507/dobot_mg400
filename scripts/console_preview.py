"""Serve the Console UI with offline calibration and Sample Transfer previews.

Run ``python scripts/console_preview.py`` and open ``http://127.0.0.1:8790/lab/#motion``.
Profiles persist in ``work/console-preview``. Physical devices remain unavailable.
Open ``#process`` for synthetic turret capture and profile management.
Open ``#sample-transfer`` for simulated rotation targets and saved-position moves.
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labkit.errors import ServiceError  # noqa: E402
from labkit.events import EventLog  # noqa: E402
from orchestrator.clients import SERVICES  # noqa: E402
from orchestrator.devices import Devices  # noqa: E402
from orchestrator.web.console import Console  # noqa: E402
from orchestrator.web.console_routes import console_blueprints  # noqa: E402
from orchestrator.web.history_preview import PreviewHistory  # noqa: E402
from orchestrator.web.sample_transfer import LiveTransferAdapter  # noqa: E402
from orchestrator.web.sample_transfer_preview import (  # noqa: E402
    PreviewStage,
    PreviewStageFrames,
)
from orchestrator.web.stage_preview import StagePreview  # noqa: E402
from orchestrator.web.turret_preview import TurretPreview  # noqa: E402
from orchestrator.web.workspaces.cameras import SourceError  # noqa: E402

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

    from orchestrator.web.twin import ProfileStore
    from vision.stage_twin import ENDPOINTS, capture_endpoint, new_profile

    profile = new_profile(name="Mock stage")
    profile["camera"].update(device_id="demo:camera1", label="Stage", dimensions=[3840, 2880])
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    moving = {"x": 18, "y": 14, "insert": 22}
    for endpoint, definition in ENDPOINTS.items():
        pixels = np.full((2880, 3840), 255, dtype=np.uint8)
        offset = -45 if definition["limit"] == "min" else 45
        for marker, x, y in ((10, 40, 40), (14, 180, 160), (18, 320, 160), (22, 460, 300)):
            if marker == moving[definition["axis"]]:
                if definition["axis"] == "y":
                    y += offset
                else:
                    x += offset
            x, y = x * 6, y * 6
            pixels[y : y + 360, x : x + 360] = cv2.aruco.generateImageMarker(
                dictionary, marker, 360
            )
        encoded = cv2.imencode(".jpg", pixels)[1].tobytes()
        profile["endpoints"][endpoint] = capture_endpoint(encoded, endpoint)
    ProfileStore(root).save(profile)


def create_app(storage_root=ROOT / "work" / "console-preview"):
    """Mount production workspaces with a simulated Stage and local profiles."""
    operations = []
    stage = StagePreview()

    def dashboard_snapshot():
        state = stage.state()
        return {
            "services": [
                {
                    "id": name,
                    "phase": "ready" if name == "stage" and state["connected"] else "unavailable",
                    "connected": name == "stage" and state["connected"],
                    "demo": True,
                }
                for name in SERVICES
            ],
            "cameras": [],
            "orchestrator": {"stale": False},
            "run": None,
            "operations": list(operations),
        }

    dashboard = SimpleNamespace(
        refresh=lambda: None,
        snapshot=dashboard_snapshot,
        text=lambda value: str(value)[:512],
    )
    service = SimpleNamespace(
        demo=True,
        settings={},
        # The Twin demo route uses this name as its storage root. It is not temporary here.
        temporary=SimpleNamespace(name=str(storage_root)),
        events=EventLog(),
        store=PreviewHistory(),
        dashboard=dashboard,
        camera_sources=OfflineCameras(),
        turret_preview=TurretPreview(
            Path(storage_root) / "calibration" / "microscope" / "turret_profiles"
        ),
        dashboard_configured=lambda name: name == "stage",
        console_client=stage.client,
        console_start_demo=unavailable,
    )
    service.console = Console(service)
    service.devices = Devices(service, on_events=service.console.pull_events)
    service.console.commit = "local preview"
    preview_stage = PreviewStage()

    def legacy_frames():
        seed_profile(Path(storage_root) / "calibration" / "twin")
        return PreviewStageFrames(preview_stage)

    service.twin_frames_factory = legacy_frames
    service.sample_transfer_adapter = LiveTransferAdapter(service)
    app = Flask(__name__)
    app.extensions["preview_service"] = service
    from orchestrator.agent_connect.access import install_access
    from orchestrator.agent_connect.control import install_control

    control = install_control(app, service, mock=True)
    install_access(app, "/lab")

    def close():
        control.close()
        service.devices.close()
        service.console.close()
        transfer = getattr(service, "sample_transfer", None)
        if transfer is not None:
            transfer.close()
        stage.close()

    app.extensions["preview_close"] = close
    app.extensions["preview_stage"] = stage

    for blueprint in console_blueprints(service, "/lab"):
        app.register_blueprint(blueprint)

    @app.get("/")
    def index():
        return redirect("/lab/#motion")

    @app.post("/lab/api/dashboard/<action>")
    def stop_all(action):
        if action not in {"motion", "heat", "gas"}:
            abort(404)
        targets = ("robot", "stage", "microscope") if action == "motion" else ("furnace",)
        stage_stop = service.console.stop("stage") if action == "motion" else None
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
        if stage_stop is not None:
            operation["results"]["stage"] = {
                "status": stage_stop["outcome"],
                "message": "Preview stage stopped.",
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
    print(f"Local preview: http://127.0.0.1:{args.port}/lab/#motion", flush=True)
    try:
        serve(app, host="127.0.0.1", port=args.port)
    finally:
        app.extensions["preview_close"]()


if __name__ == "__main__":
    main()
