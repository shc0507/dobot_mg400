"""Independent fixed Stop/status process for simulated or explicitly enabled lab services."""

import argparse
import uuid
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import make_server

from labkit.errors import ServiceError

from .stops import StopHost

UNVERIFIED = {"kind": "unverified", "name": "Fallback caller", "role": "unknown", "verified": False}


def create_fallback_app(stage=None, *, path=None, physical=None):
    if stage is not None and physical is not None:
        raise ValueError("Choose a simulated or physical emergency adapter.")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    boot_id = uuid.uuid4().hex

    def dispatch(device):
        if physical is not None:
            return physical.stop(device)
        if stage is None:
            return {
                "device": device,
                "outcome": "disabled",
                "reason": "Physical agent commands are disabled.",
            }
        if device != "stage":
            return {
                "device": device,
                "outcome": "unknown",
                "reason": "No independent simulator for this device.",
            }
        # This adapter belongs to the in-process DEMO controller, never a lab URL.
        result = stage.client("stage", "agent-emergency-simulation").stop()
        return {"device": device, "outcome": result.get("stop", "unknown"), "simulated": True}

    stops = StopHost(dispatch, path=path)
    app.extensions["agent_stops"] = stops

    @app.errorhandler(ServiceError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @app.post("/stop")
    def stop():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ServiceError(400, "Send a Stop request ID and target as JSON.")
        return jsonify(stops.submit(body, UNVERIFIED, "fallback")), 202

    @app.get("/stops")
    def history():
        return jsonify(stops.history())

    @app.get("/stops/<identifier>")
    def detail(identifier):
        return jsonify(stops.get(identifier))

    @app.get("/status")
    def status():
        devices = {}
        if physical is not None:
            devices = physical.status()
        if stage is not None:
            try:
                devices["stage"] = stage.state()
            except Exception:
                devices["stage"] = {"available": False}
        return jsonify(
            service="agent-emergency",
            version=1,
            boot_id=boot_id,
            equipment_mode="physical"
            if physical is not None
            else "simulated"
            if stage
            else "disabled",
            stop_enabled=stage is not None or physical is not None,
            physical_commands_enabled=False,
            furnace_control_enabled=False,
            devices=devices,
            audit=stops.history().get("audit"),
        )

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulation", action="store_true")
    mode.add_argument("--physical", action="store_true")
    args = parser.parse_args()
    stage = None
    if args.simulation:
        from orchestrator.web.stage_preview import StagePreview

        stage = StagePreview()
    path = None if stage else Path("/var/lib/mg400/twin-test/runs/agent-connect/emergency-stops.db")
    from .emergency import PhysicalEmergency

    app = create_fallback_app(
        stage, path=path, physical=PhysicalEmergency() if args.physical else None
    )
    application = DispatcherMiddleware(app, {"/stage": stage.app}) if stage else app
    address, port = ("127.0.0.1", 8794) if stage else ("127.0.0.2", 8874)
    server = make_server(address, port, application, threaded=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.extensions["agent_stops"].close()
        if stage:
            stage.close()


if __name__ == "__main__":
    main()
