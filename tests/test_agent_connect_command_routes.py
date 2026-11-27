"""Exercise Phase 5 HTTP routes through native simulated Console handlers."""

import socket
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

from labkit.errors import ServiceError
from orchestrator.agent_connect.access import install_access
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.command_store import TERMINAL
from orchestrator.agent_connect.control import install_control

BASE = "/lab/api/agent/control"


@pytest.mark.parametrize("raises", [False, True])
def test_missing_simulator_feedback_never_returns_phase_zero_position(raises):
    from orchestrator.agent_connect.control import ControlHost
    from orchestrator.agent_connect.mock import MockConsole
    from orchestrator.agent_connect.simulation import Simulation

    def observations():
        if raises:
            raise RuntimeError("Simulator state read failed")
        return {}

    host = ControlHost(mock=True)
    host._command_backend = SimpleNamespace(observations=observations)
    simulation = Simulation(MockConsole())
    simulation.command_observations = host.command_observations
    try:
        stage = simulation()["devices"]["stage"]
        assert stage["quality"] == "unavailable"
        assert stage["values"]["axes"] == []
        assert stage["instance"] is None
        assert stage["age_s"] is None
    finally:
        simulation.close()
        host.close()


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    opened = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("Agent command integration tests must not contact physical services.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    def create(*, physical=False, mock=None):
        app = Flask(__name__)
        app.testing = True
        authority = Authority(tmp_path / f"authority-{len(opened)}.db")
        service = None
        if physical:
            service = SimpleNamespace(
                demo=False,
                runs_dir=tmp_path / "runs",
                dashboard_configured=forbidden,
                dashboard_client=forbidden,
                console_client=forbidden,
                console=SimpleNamespace(
                    lock=threading.RLock(),
                    sessions={},
                    reserved=set(),
                    read=forbidden,
                    command=forbidden,
                    take=forbidden,
                    stop=forbidden,
                ),
            )
        host = install_control(
            app, service, authority=authority, mock=not physical if mock is None else mock
        )
        install_access(app, "/lab")
        writes = []

        @app.errorhandler(ServiceError)
        def error(exc):
            return jsonify(error=exc.message), exc.status

        @app.post("/lab/api/console/command/<device>")
        def raw_command(device):
            writes.append(device)
            return jsonify(ok=True)

        @app.post("/lab/api/runs")
        def recipe():
            writes.append("recipe")
            return jsonify(ok=True)

        @app.post("/lab/api/console/stop/furnace")
        @app.post("/lab/api/dashboard/heat")
        @app.post("/lab/api/dashboard/gas")
        def emergency():
            writes.append("emergency")
            return jsonify(ok=True)

        rig = SimpleNamespace(
            app=app,
            host=host,
            authority=authority,
            writes=writes,
            owner=app.test_client(),
            agent=app.test_client(),
        )
        opened.append(rig)
        return rig

    yield create
    for rig in reversed(opened):
        rig.host.close()


def connect(rig, *, grant=True):
    invitation = rig.owner.post(BASE + "/invitations", json={})
    assert invitation.status_code == 200
    paired = rig.agent.post(
        BASE + "/pair", json={"invitation": invitation.json["invitation"], "name": "Test PC"}
    )
    assert paired.status_code == 200
    assert paired.json["status"] == "approved"
    session = rig.agent.post(BASE + "/connect", json={"credential": paired.json["credential"]})
    assert session.status_code == 200
    assert session.json["state"]["operator"] is None or not grant
    headers = {"Authorization": "Bearer " + session.json["session_token"]}
    if grant:
        granted = rig.owner.post(BASE + "/grant", json={"session_id": session.json["session_id"]})
        assert granted.status_code == 200
        assert granted.json["operator"]["id"] == session.json["session_id"]
    return paired.json, session.json, headers


def envelope(rig, action, params=None, request_id="operation-1"):
    catalog = rig.agent.get(BASE + "/commands/catalog")
    assert catalog.status_code == 200
    return {
        "request_id": request_id,
        "boot_id": rig.authority.boot_id,
        "contract_id": catalog.json["contract_id"],
        "control_epoch": rig.authority.control_epoch,
        "action": action,
        "params": params or {},
    }


def finish(rig, request_id, headers):
    deadline = time.monotonic() + 25
    heartbeat = 0
    while time.monotonic() < deadline:
        if time.monotonic() >= heartbeat:
            assert rig.agent.post(BASE + "/heartbeat", json={}, headers=headers).status_code == 200
            heartbeat = time.monotonic() + 1
        response = rig.agent.get(BASE + "/commands/request/" + request_id, headers=headers)
        assert response.status_code == 200
        if response.json["status"] in TERMINAL:
            return response.json
        time.sleep(0.01)
    pytest.fail("The simulated operation did not finish within the test deadline.")


def submit(rig, headers, request):
    response = rig.agent.post(BASE + "/commands", json=request, headers=headers)
    assert response.status_code == 202, response.json
    assert response.json["status"] == "accepted"
    return finish(rig, request["request_id"], headers)


def test_mock_catalog_contract_and_real_stage_handlers_update_observations(make_rig):
    rig = make_rig()
    paired, session, headers = connect(rig)
    contract = rig.agent.get(BASE + "/contract").json
    assert contract["version"] == 6
    assert contract["phase"] == 8
    assert contract["commands_enabled"] is True
    assert contract["physical_commands_enabled"] is False
    assert contract["furnace_control_enabled"] is False
    assert contract["pairing"] == {"approval": "invitation", "operator_grant": "explicit"}
    catalog = rig.agent.get(contract["command_catalog_url"]).json
    actions = {row["id"]: row for row in catalog["actions"]}
    assert catalog["version"] == 1
    assert catalog["contract_id"].startswith("sha256:")
    assert actions["stage.calibrate"]["available"]
    assert actions["stage.position"]["available"]
    assert not actions["robot.move"]["available"]

    calibration = envelope(rig, "stage.calibrate", {"motor": "insert", "speed": 0.25})
    calibrated = submit(rig, headers, calibration)
    assert calibrated["status"] == "succeeded", calibrated
    assert calibrated["dispatch_count"] == 1
    assert calibrated["client_id"] == paired["client_id"]
    assert calibrated["session_id"] == session["session_id"]
    raw = rig.host._command_backend.observations()["stage"]
    insert = next(motor for motor in raw["motors"] if motor["id"] == "insert")
    assert insert["calibration"] == "done"

    movement = envelope(rig, "stage.position", {"motor": "insert", "position": 0.1}, "operation-2")
    moved = submit(rig, headers, movement)
    assert moved["status"] == "succeeded", moved
    observed = rig.host.command_observations()["stage"]
    axis = next(row for row in observed["values"]["axes"] if row["id"] == "insert")
    assert observed["quality"] == "valid"
    assert axis["position_rotations"] == pytest.approx(0.1, abs=0.003)
    assert axis["reference_valid"] is True
    assert axis["quality"] == "valid"
    assert rig.writes == []

    last_command = observed["values"]["last_command"]
    assert rig.owner.post(BASE + "/take-human", json={}).status_code == 200
    duplicate = rig.agent.post(BASE + "/commands", json=movement, headers=headers)
    assert duplicate.status_code == 202
    assert duplicate.json == moved
    assert rig.host.command_observations()["stage"]["values"]["last_command"] == last_command
    changed = {**movement, "params": {"motor": "insert", "position": 0.2}}
    assert rig.agent.post(BASE + "/commands", json=changed, headers=headers).status_code == 409
    assert rig.host.operation_summaries()[0]["request_id"] == "operation-2"


def test_http_history_is_client_private_and_survives_reconnect(make_rig):
    rig = make_rig()
    paired, _, headers = connect(rig)
    request = envelope(rig, "transfer.profile.new", {"name": "HTTP simulation"})
    result = submit(rig, headers, request)
    assert result["status"] == "succeeded", result
    assert result["params"]["name"] == "HTTP simulation"
    _, _, observer = connect(rig, grant=False)
    assert rig.agent.get(BASE + "/commands/history", headers=observer).json == {
        "items": [],
        "count": 0,
    }
    assert (
        rig.agent.get(BASE + "/commands/request/operation-1", headers=observer).status_code == 404
    )
    assert (
        rig.agent.post(
            BASE + "/commands/cancel", json={"request_id": "operation-1"}, headers=observer
        ).status_code
        == 404
    )
    assert rig.agent.post(BASE + "/commands", json=request, headers=observer).status_code == 403
    history = rig.agent.get(BASE + "/commands/history", headers=headers).json
    assert history["count"] == 1
    assert "params" not in history["items"][0]
    assert "result" not in history["items"][0]
    assert rig.owner.get(BASE + "/commands/history").json["count"] == 1

    reconnected = rig.agent.post(BASE + "/connect", json={"credential": paired["credential"]}).json
    new_headers = {"Authorization": "Bearer " + reconnected["session_token"]}
    assert rig.agent.get(BASE + "/commands/history", headers=headers).status_code == 401
    assert rig.agent.get(BASE + "/commands/request/operation-1", headers=new_headers).json == result
    retried = rig.agent.post(BASE + "/commands", json=request, headers=new_headers)
    assert retried.status_code == 202
    assert retried.json == result


@pytest.mark.parametrize("mock", [False, True])
def test_physical_console_catalogue_never_calls_services_or_dispatches(make_rig, mock):
    rig = make_rig(physical=True, mock=mock)
    _, _, headers = connect(rig)
    contract = rig.owner.get(BASE + "/contract").json
    assert contract["commands_enabled"] is False
    assert contract["physical_commands_enabled"] is False
    assert contract["equipment_mode"] == "disabled"
    catalog = rig.owner.get(BASE + "/commands/catalog").json
    assert catalog["actions"]
    assert all(row["available"] is False for row in catalog["actions"])
    request = envelope(rig, "stage.calibrate", {"motor": "insert", "speed": 0.25})
    response = rig.agent.post(BASE + "/commands", json=request, headers=headers)
    assert response.status_code == 409
    assert "disabled" in response.json["error"]
    assert rig.agent.get(BASE + "/commands/history", headers=headers).json["count"] == 0
    assert rig.host.command_observations() == {}


@pytest.mark.parametrize("physical", [False, True])
def test_furnace_raw_dispatch_and_recipes_stay_blocked_but_emergency_routes_work(
    make_rig, physical
):
    rig = make_rig(physical=physical)
    _, _, headers = connect(rig)
    for route, params in (
        ("/lab/api/console/command/furnace", {"action": "start_sequence"}),
        ("/lab/api/console/command/stage", {"action": "move_to_position"}),
        ("/lab/api/runs", {"recipe": "heat-sample"}),
    ):
        assert rig.agent.post(route, json=params, headers=headers).status_code == 403
    for action in ("furnace.start_sequence", "recipe.run", "dispatch", "robot.raw"):
        request = envelope(rig, action)
        assert rig.agent.post(BASE + "/commands", json=request, headers=headers).status_code in {
            403,
            404,
            409,
        }
    assert rig.writes == []
    for route in (
        "/lab/api/console/stop/furnace",
        "/lab/api/dashboard/heat",
        "/lab/api/dashboard/gas",
    ):
        assert (
            rig.agent.post(route, json={}, headers={"Authorization": "invalid"}).status_code == 200
        )
    assert rig.writes == ["emergency"] * 3


def test_commands_http_requires_json_strict_fields_and_same_origin(make_rig):
    rig = make_rig()
    _, _, headers = connect(rig)
    request = envelope(rig, "turret.begin", {"new": True, "name": "HTTP simulation"})
    assert rig.agent.post(BASE + "/commands", data="{}", headers=headers).status_code == 415
    assert (
        rig.agent.post(
            BASE + "/commands", json={**request, "enable_physical": True}, headers=headers
        ).status_code
        == 400
    )
    assert (
        rig.agent.post(
            BASE + "/commands", json=request, headers={**headers, "Origin": "https://example.test"}
        ).status_code
        == 403
    )
    assert (
        rig.agent.post(
            BASE + "/commands", json=request, headers={"Authorization": "invalid"}
        ).status_code
        == 401
    )
    assert rig.owner.get(BASE + "/commands/history").json["count"] == 0
