"""Disposable mock source. This module imports no device adapters."""

import copy
import math
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .contract import FIXTURES, PREFIX, digest, fixture

ASSETS = Path(__file__).with_name("static")
STALE_S = 3.0


class MockConsole:
    def __init__(self, *, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.lock = threading.RLock()
        self.events = deque(maxlen=100)
        self.bundles = {digest(fixture(name)): fixture(name) for name in FIXTURES}
        self.reset()

    def reset(self):
        with self.lock:
            self.boot_id = "mock-" + uuid.uuid4().hex
            self.instance = "mock-stage-" + uuid.uuid4().hex
            self.release = "baseline"
            self.paused = False
            self.reference_revision = 1
            self.seq = 0
            self.elapsed = 0.0
            self.sampled = self.clock()
            self.sampled_at = self.wall()
            self.position = 0.0
            self.temperature = 22.0
            self.events.clear()
            self.samples = [
                {
                    "chip_id": "chip-001",
                    "holder": "tray T1 / A1",
                    "basis": "mock observation",
                    "pending_action": None,
                    "last_run": "mock-run-1",
                    "last_step": "inspect",
                },
                {
                    "chip_id": "chip-002",
                    "holder": "crucible on Insert",
                    "basis": "mock placement",
                    "pending_action": None,
                    "last_run": "mock-run-2",
                    "last_step": "place",
                },
            ]
            self._event("reset", "Restored the initial sample fixtures and values.")

    def _event(self, kind, message):
        self.events.append({"kind": kind, "message": message, "at": self.wall()})

    def discovery(self):
        with self.lock:
            bundle = fixture(self.release)
            identity = digest(bundle)
            return {
                "discovery_version": 1,
                "mode": "MOCK",
                "phase": 0,
                "server_release": "mock-" + self.release,
                "boot_id": self.boot_id,
                "contract_version": bundle["version"],
                "contract_id": identity,
                "contract_url": PREFIX + "/contracts/" + identity[7:] + ".json",
                "endpoints": {"state": PREFIX + "/v1/state"},
                "normal_commands": False,
            }

    def snapshot(self):
        with self.lock:
            now = self.clock()
            delta = now - self.sampled
            if not self.paused and delta >= 0.25:
                self.elapsed += delta
                self.seq += 1
                self.position = round(math.sin(self.elapsed / 8) * 0.25, 4)
                self.temperature = round(22 + 3 * math.sin(self.elapsed / 12), 2)
                self.sampled, self.sampled_at = now, self.wall()
            age = max(0, now - self.sampled)
            quality = "stale" if age > STALE_S else "good"
            result = {
                "mode": "MOCK",
                "boot_id": self.boot_id,
                "contract_id": self.discovery()["contract_id"],
                "seq": self.seq,
                "sampled_at": self.sampled_at,
                "age_s": round(age, 3),
                "quality": quality,
                "paused": self.paused,
                "stage": {
                    "position": self.position,
                    "unit": "mm" if self.release == "incompatible" else "rotation",
                    "instance": self.instance,
                    "reference_revision": self.reference_revision,
                    "quality": quality,
                },
                "temperature": {"value": self.temperature, "unit": "degC", "quality": quality},
                "samples": copy.deepcopy(self.samples),
                "events": list(self.events),
            }
            if self.release == "compatible":
                result["source_label"] = "synthetic source"
            return result

    def control(self, action, value=None):
        with self.lock:
            if action == "reset":
                self.reset()
            elif action == "pause":
                self.snapshot()
                self.paused = True
            elif action == "resume":
                self.sampled = self.clock()
                self.sampled_at = self.wall()
                self.paused = False
            elif action == "stale":
                self.paused = True
                self.sampled -= STALE_S + 1
                self.sampled_at -= STALE_S + 1
            elif action == "reference-reset":
                self.reference_revision += 1
            elif action == "restart":
                self.boot_id = "mock-" + uuid.uuid4().hex
                self.instance = "mock-stage-" + uuid.uuid4().hex
                self.seq = 0
                self.reference_revision += 1
            elif action == "fixture" and value in FIXTURES:
                self.release = value
            else:
                raise ValueError("Unknown mock control or fixture.")
            self._event(action, f"Mock control: {action}" + (f" / {value}" if value else ""))
            return self.snapshot()


def create_mock_app(source=None, *, streaming=False):
    source = source or MockConsole()
    app = Flask(__name__, static_folder=None)
    app.config["MOCK_SOURCE"] = source
    if streaming:
        from .simulation import register_simulation

        register_simulation(app, source)

    @app.get("/lab/")
    def index():
        return send_from_directory(ASSETS, "console.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(ASSETS, name)

    @app.get(PREFIX + "/discovery")
    def discovery():
        return jsonify(source.discovery())

    @app.get(PREFIX + "/contracts/<identity>.json")
    def bundle(identity):
        value = source.bundles.get("sha256:" + identity)
        if value is None:
            abort(404)
        value = copy.deepcopy(value)
        if source.release == "broken-digest":
            value["phase"] = 99
        return jsonify(value)

    @app.get(PREFIX + "/v1/state")
    def state():
        return jsonify(source.snapshot())

    @app.post("/mock/control")
    def control():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"action", "value"}:
            return jsonify(error="Send a mock action and optional value."), 400
        try:
            result = source.control(data.get("action"), data.get("value"))
            if streaming:
                app.config["AGENT_SIMULATION"].source_changed(data.get("action"), data.get("value"))
            return jsonify(result)
        except ValueError as error:
            return jsonify(error=str(error)), 400

    return app
