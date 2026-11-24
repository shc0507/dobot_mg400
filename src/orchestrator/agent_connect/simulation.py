"""Sample and stream demonstrations. This module has no device adapters."""

import copy
import time
import uuid
from types import SimpleNamespace

from flask import Blueprint, jsonify, render_template, request, send_from_directory

from .protocol import bundle
from .routes import register_routes
from .samples import SampleTracker
from .stream import BroadcastHub


class Simulation:
    def __init__(self, source):
        self.source = source
        self.commands = lambda: []
        self.command_observations = lambda: {}
        self.tracker = SampleTracker.memory()
        self.seed()
        self.hub = BroadcastHub(self, server_release="local-simulation")

    def seed(self):
        for chip, target in (
            ("chip-001", {"holder": "tray_slot", "tray_id": "T1", "slot_id": "A1"}),
            ("chip-002", {"holder": "stage_insert", "container_id": "crucible-1"}),
        ):
            self.tracker.record_event(
                chip,
                "observation",
                location=target,
                observation={
                    "observed_at": time.time(),
                    "source": "simulated-camera",
                    "chip_id": chip,
                    "identity_source": "exact_id",
                    "frame_id": "demo-seed",
                },
            )

    def __call__(self):
        raw = self.source.snapshot()
        common = {
            "quality": "stale" if raw["quality"] == "stale" else "valid",
            "sampled_at": raw["sampled_at"],
            "age_s": raw["age_s"],
            "observed_at": raw["sampled_at"],
            "instance": raw["stage"]["instance"],
        }
        return {
            "mode": "MOCK",
            "devices": {
                "stage": {
                    **common,
                    "values": {
                        "reference_revision": raw["stage"]["reference_revision"],
                        "axes": [
                            {
                                "id": "insert",
                                "position_rotations": raw["stage"]["position"],
                                "unit": "rotation",
                                "quality": common["quality"],
                                "reference_valid": True,
                                "encoder_age_s": raw["age_s"],
                            }
                        ],
                    },
                },
                "furnace": {**common, "values": {"pv_c": raw["temperature"]["value"]}},
                **self.command_observations(),
            },
            "operations": {"items": self.commands(), "available": True},
        }

    def samples(self, **kwargs):
        return self.tracker.samples(**kwargs)

    def control(self, action, chip="chip-001", target=None):
        target = target or {"holder": "stage_insert", "container_id": "crucible-1"}
        if action == "expire-events":
            self.hub.publish("simulation.expired", {})
            self.hub.expire_history()
            return {"expired": True}
        if action == "report":
            row = self.tracker.manual_report(
                chip, actor={"kind": "human", "id": "local-demo"}, location=target
            )
        elif action == "unknown":
            row = self.tracker.manual_report(
                chip, actor={"kind": "human", "id": "local-demo"}, unknown=True
            )
        elif action == "pick":
            row = self.tracker.record_event(
                chip,
                "accepted",
                operation_id="demo-" + uuid.uuid4().hex,
                action="pick",
                destination=target,
                may_start=True,
            )
        elif action == "place":
            current = self.tracker.samples(selection="ids", chip_ids=[chip])["items"][0]
            pending = current.get("pending_operation") or {}
            row = self.tracker.record_event(
                chip,
                "completed",
                operation_id=pending.get("id"),
                location=target,
                workflow_supports_location=True,
            )
        elif action in {"confirm", "old-frame", "conflict"}:
            row = self.tracker.record_event(
                chip,
                "observation",
                location=target,
                observation={
                    "observed_at": time.time() - (3600 if action == "old-frame" else 0),
                    "source": "simulated-camera",
                    "frame_id": uuid.uuid4().hex,
                    "identity_source": "exact_id",
                    "chip_id": "other-chip" if action == "conflict" else chip,
                },
            )
        elif action == "entry":
            row = self.tracker.record_event(
                chip,
                "entry",
                mapping_valid=True,
                reference_valid=True,
                aligned=True,
                inside_furnace=True,
                observed_at=time.time(),
            )
        elif action == "lose-reference":
            row = self.tracker.record_event(chip, "partial", scope="entry")
        elif action == "reset":
            old = self.tracker
            self.tracker = SampleTracker.memory()
            self.seed()
            old.close()
            self.hub.restart()
            return {"reset": True}
        else:
            raise ValueError("Choose a documented sample test.")
        self.hub.publish("sample.changed", {"chip_id": chip, "action": action})
        return row

    def source_changed(self, action, value=None):
        if action in {"restart", "reset"}:
            self.hub.restart()
        elif action == "fixture":
            definition = bundle()
            if value == "compatible":
                definition["version"] = "1.1.0"
                definition["schemas"]["state"]["properties"]["source_label"] = {"type": "string"}
            elif value == "incompatible":
                definition["version"] = "2.0.0"
                definition["semantics"]["units"]["position_rotations"] = "mm"
            elif value == "unsupported-dialect":
                definition["$schema"] = "urn:unsupported-schema-dialect"
            self.hub.change_contract(copy.deepcopy(definition))
        else:
            self.hub.publish("simulation.changed", {"action": action})

    def close(self):
        self.hub.close()
        self.tracker.close()


def register_simulation(app, source):
    simulation = Simulation(source)
    app.config["AGENT_SIMULATION"] = simulation
    register_routes(app, simulation.hub)
    from ..web.passport import register_passport
    from .mock import ASSETS

    class PassportSource:
        dashboard = SimpleNamespace(secrets=())

        @property
        def store(self):
            store = simulation.tracker.store
            store.chip_history = lambda *args, **kwargs: {"items": [], "next_offset": None}
            return store

    web = ASSETS.parent.parent / "web"
    passport = Blueprint(
        "mock_passport", __name__, url_prefix="/lab", template_folder=str(web / "templates")
    )
    register_passport(passport, PassportSource())

    @passport.get("/chip")
    @passport.get("/chip/<path:chip_id>")
    def chip_page(chip_id=""):
        return render_template("passport.html", base_path="/lab")

    @passport.get("/dashboard-static/<name>")
    def passport_asset(name):
        if name not in {"passport.js", "passport.css"}:
            return "", 404
        return send_from_directory(web / "static", name)

    @passport.get("/console-static/shell.css")
    def shell_asset():
        return send_from_directory(web / "static" / "console", "shell.css")

    app.register_blueprint(passport)

    @app.post("/mock/samples")
    def sample_action():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"action", "chip_id", "location"}:
            return jsonify(error="Send a sample test and its chip ID."), 400
        try:
            return jsonify(
                simulation.control(
                    data.get("action"), data.get("chip_id", "chip-001"), data.get("location")
                )
            )
        except (TypeError, ValueError) as error:
            return jsonify(error=str(error)), 400

    return simulation
