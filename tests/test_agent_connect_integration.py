"""Phase 2/3 composition checks using only simulated sources."""

import json
from types import SimpleNamespace

from flask import Flask
from websockets.sync.client import connect

from orchestrator.agent_connect.mock import create_mock_app
from orchestrator.agent_connect.routes import register_routes
from orchestrator.agent_connect.runtime import create_hub, publish_device_events
from orchestrator.agent_connect.samples import SampleTracker
from orchestrator.web.control_socket import ControlSocket


def test_sample_demonstrations_and_shared_stream_pages():
    app = create_mock_app(streaming=True)
    simulation = app.config["AGENT_SIMULATION"]
    http = app.test_client()

    def action(name, **kwargs):
        result = http.post("/mock/samples", json={"action": name, **kwargs})
        assert result.status_code == 200, result.json
        return result.json

    try:
        definition = http.get("/lab/api/agent/discovery").json
        assert definition["mode"] == "MOCK"
        assert http.get("/lab/api/agent-fixtures/discovery").json["phase"] == 0
        assert action("pick")["location"]["holder"] == "unknown"
        assert action("place")["basis"] == "inferred"
        assert action("confirm")["basis"] == "observed"
        assert action("entry")["location"]["inside_furnace"] is True
        partial = action("lose-reference")
        assert partial["location"]["holder"] == "stage_insert"
        assert partial["location"]["inside_furnace"] is None
        assert action("report", location={"holder": "robot"})["basis"] == "reported"
        assert action("old-frame")["location"]["holder"] == "robot"
        assert action("conflict")["location"]["holder"] == "unknown"
        page = http.get("/lab/api/agent/v1/samples?selection=all&limit=1").json
        assert page["count"] == 1 and page["total"] == 2 and not page["complete"]
        location = http.get("/lab/api/chip/location?id=chip-001").json
        assert location["sample"]["conflict"]
        action("expire-events")
        assert not simulation.hub.events and simulation.hub.event_seq > 0
        action("reset")
        assert simulation.samples(selection="all")["total"] == 2
    finally:
        simulation.close()


def test_real_socket_uses_same_simulated_snapshot_and_rejects_foreign_origins():
    app = create_mock_app(streaming=True)
    simulation = app.config["AGENT_SIMULATION"]
    server = ControlSocket(app, host="127.0.0.1", port=0, base_path="/lab").start()
    try:
        with connect(
            f"ws://127.0.0.1:{server.port}/lab/api/agent/v1/stream?selection=ids&chip_id=chip-001",
            origin="http://127.0.0.1:8792",
            proxy=None,
        ) as ws:
            first = json.loads(ws.recv(timeout=2))
            assert first["samples"]["items"][0]["chip_id"] == "chip-001"
            simulation.control("pick")
            event = json.loads(ws.recv(timeout=2))
            assert event["kind"] == "sample.changed"
            simulation.hub.tick()
    finally:
        server.close()
        simulation.close()


def test_live_read_routes_preserve_cache_and_do_not_enable_commands():
    tracker = SampleTracker.memory()
    cache = SimpleNamespace(entries={}, get=lambda name: None)
    service = SimpleNamespace(store=tracker.store, devices=cache, demo=False)
    hub = create_hub(service)
    service.agent_stream = hub
    app = Flask(__name__)
    register_routes(app, hub)
    http = app.test_client()
    try:
        assert http.get("/lab/api/agent/discovery").json["mode"] == "live"
        for path in ("commands", "control", "report-location"):
            assert http.post("/lab/api/agent/v1/" + path, json={}).status_code == 404
        row = ("stage", 2, 100.0, "info", "Reference changed", {}, "stage-a")
        publish_device_events(service, "stage", [row], ("stage-a", 1), "stage-a", {"next": 2})
        assert hub.events[-1][0]["data"]["source_seq"] == 2
        publish_device_events(service, "stage", [row], ("stage-a", 2), "stage-a", {"next": 2})
        assert len(hub.events) == 1
    finally:
        hub.close()
        tracker.close()
