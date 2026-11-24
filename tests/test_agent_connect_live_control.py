"""Test live authority and HTTP dispatch with recording doubles and no sockets."""

import copy
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify
from test_agent_connect_command_routes import BASE, connect, envelope, submit

from labkit.errors import ServiceError
from orchestrator.agent_connect import action_backend, emergency
from orchestrator.agent_connect.access import install_access
from orchestrator.agent_connect.actions import SPECS, validate
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.control import install_control


class RecordingBackend:
    """Record reviewed actions while the real CommandHost enforces authority."""

    def __init__(self, service, enabled):
        self.service = service
        self.enabled = self.physical = self.furnace_enabled = enabled
        self.simulated = False
        self.mode = "physical" if enabled else "disabled"
        self.prepared = []
        self.writes = []
        self.stopped = []
        self.cancelled = []
        self.released = []

    def catalog(self):
        return [
            {
                **{
                    key: copy.deepcopy(spec[key])
                    for key in (
                        "id",
                        "label",
                        "description",
                        "params",
                        "defaults",
                        "device",
                        "revision",
                    )
                },
                "available": self.enabled,
                "reason": "" if self.enabled else "Physical agent commands are disabled.",
                "simulated": False,
            }
            for spec in (SPECS["stage.position"], SPECS["furnace.apply_setpoint"])
        ]

    def prepare(self, action, params, preconditions, page):
        validate(params, SPECS[action]["params"])
        self.prepared.append((action, copy.deepcopy(params), page))
        return {"action": action, "params": copy.deepcopy(params), "page": page}

    def _write(self, plan):
        self.writes.append(copy.deepcopy(plan))
        device = SPECS[plan["action"]]["device"]
        with self.service.console.lock:
            self.service.console.sessions[device] = SimpleNamespace(
                page=plan["page"], lent=False, state="held"
            )

    def execute(self, plan, context):
        context.dispatch(self._write, plan)
        return {"status": "succeeded", "result": {"simulated": False, "action": plan["action"]}}

    def cancel(self, plan, page):
        self.cancelled.append((plan, page))
        return {"status": "stopped"}

    def release_page(self, page):
        self.released.append(page)
        with self.service.console.lock:
            devices = {
                device: "released"
                for device, session in self.service.console.sessions.items()
                if session.page == page
            }
            for device in devices:
                del self.service.console.sessions[device]
        return {"outcome": "released", "devices": devices}

    def stop(self, device):
        self.stopped.append(device)
        return {"device": device, "outcome": "confirmed", "simulated": False}


@pytest.fixture
def live_rig(tmp_path, monkeypatch):
    opened = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("Live control tests must not contact equipment or open a socket.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    def create(*, allow_physical=False, mock=False):
        app = Flask(__name__)
        app.testing = True
        root = tmp_path / str(len(opened))
        root.mkdir()
        authority = Authority(root / "authority.db")
        service = SimpleNamespace(
            demo=False,
            runs_dir=root / "runs",
            dashboard_client=forbidden,
            console_client=forbidden,
            dashboard_configured=forbidden,
            console=SimpleNamespace(
                lock=threading.RLock(),
                sessions={},
                reserved=set(),
                command=forbidden,
                take=forbidden,
                read=forbidden,
                stop=forbidden,
            ),
        )
        backend = RecordingBackend(service, allow_physical)
        created = []

        def factory(received, *, mock=False, source=None, allow_physical=False):
            assert received is service
            created.append({"mock": mock, "allow_physical": allow_physical})
            backend.enabled = backend.physical = backend.furnace_enabled = allow_physical
            return backend

        monkeypatch.setattr(action_backend, "create_backend", factory)
        monkeypatch.setattr(emergency, "PhysicalEmergency", lambda: backend)
        host = install_control(
            app, service, authority=authority, mock=mock, allow_physical=allow_physical
        )
        install_access(app, "/lab")

        @app.errorhandler(ServiceError)
        def error(exc):
            return jsonify(error=exc.message), exc.status

        rig = SimpleNamespace(
            app=app,
            host=host,
            authority=authority,
            service=service,
            backend=backend,
            created=created,
            owner=app.test_client(),
            agent=app.test_client(),
        )
        opened.append(rig)
        return rig

    yield create
    for rig in reversed(opened):
        rig.host.close()


def wait_for(check):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.01)
    pytest.fail("The local control operation did not reach the expected state.")


@pytest.mark.parametrize("enabled", [False, True])
def test_server_opt_in_controls_physical_contract_status_and_catalog(live_rig, enabled):
    rig = live_rig(allow_physical=enabled, mock=True)
    contract = rig.owner.get(BASE + "/contract").json
    status = rig.owner.get(BASE + "/status").json
    for report in (contract, status):
        assert report["commands_enabled"] is enabled
        assert report["physical_commands_enabled"] is enabled
        assert report["furnace_control_enabled"] is enabled
        assert report["stop_enabled"] is enabled
        assert report["equipment_mode"] == ("physical" if enabled else "disabled")
    assert contract["furnace"] == {"read": True, "operate": enabled}
    assert contract["scope"] == "this-console-only"
    catalog = rig.owner.get(contract["command_catalog_url"]).json
    assert all(row["available"] is enabled and not row["simulated"] for row in catalog["actions"])
    assert status["command_contract_id"] == catalog["contract_id"]
    assert rig.created == [{"mock": False, "allow_physical": enabled}]
    assert rig.backend.prepared == rig.backend.writes == rig.backend.stopped == []


def test_pair_connect_and_operator_grant_do_not_dispatch_equipment(live_rig):
    rig = live_rig(allow_physical=True)
    _, session, headers = connect(rig, grant=False)
    assert rig.authority.snapshot()["operator"] is None
    assert rig.backend.prepared == rig.backend.writes == rig.backend.stopped == []
    request = envelope(
        rig, "furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 2}
    )
    assert rig.agent.post(BASE + "/commands", json=request, headers=headers).status_code == 403
    grant = rig.owner.post(BASE + "/grant", json={"session_id": session["session_id"]})
    assert grant.status_code == 200
    assert grant.json["operator"]["kind"] == "agent"
    assert rig.backend.prepared == rig.backend.writes == rig.backend.stopped == []
    assert rig.service.console.sessions == {}


def test_request_flag_cannot_override_disabled_server_policy(live_rig):
    rig = live_rig()
    _, _, headers = connect(rig)
    request = envelope(
        rig, "furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 2}
    )
    assert rig.agent.post(BASE + "/commands", json=request, headers=headers).status_code == 403
    request["allow_physical"] = True
    assert rig.agent.post(BASE + "/commands", json=request, headers=headers).status_code == 400
    assert rig.backend.prepared == rig.backend.writes == rig.backend.stopped == []


@pytest.mark.parametrize(
    "action,params",
    [
        ("stage.position", {"motor": "insert", "position": 0.1}),
        ("furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 2}),
    ],
)
def test_explicit_operator_request_dispatches_once_and_saves_physical_result(
    live_rig, action, params
):
    rig = live_rig(allow_physical=True)
    paired, session, headers = connect(rig)
    request = envelope(rig, action, params)
    result = submit(rig, headers, request)
    assert result["status"] == "succeeded" and result["dispatch_count"] == 1
    assert result["result"] == {"simulated": False, "action": action}
    assert result["client_id"] == paired["client_id"]
    assert result["session_id"] == session["session_id"]
    assert rig.backend.writes == [
        {"action": action, "params": params, "page": "agent-" + session["session_id"]}
    ]
    duplicate = rig.agent.post(BASE + "/commands", json=request, headers=headers)
    assert duplicate.status_code == 202 and duplicate.json == result
    assert len(rig.backend.writes) == 1


def test_old_epoch_and_revoked_credentials_cannot_dispatch_new_requests(live_rig):
    rig = live_rig(allow_physical=True)
    paired, session, headers = connect(rig)
    old = envelope(rig, "stage.position", {"motor": "insert", "position": 0.1}, "old-epoch")
    assert rig.owner.post(BASE + "/take-human", json={}).status_code == 200
    assert (
        rig.owner.post(BASE + "/grant", json={"session_id": session["session_id"]}).status_code
        == 200
    )
    assert rig.agent.post(BASE + "/commands", json=old, headers=headers).status_code == 409
    fresh = envelope(rig, "stage.position", old["params"], "revoked-session")
    assert (
        rig.owner.post(BASE + "/revoke", json={"client_id": paired["client_id"]}).status_code == 200
    )
    assert rig.agent.post(BASE + "/commands", json=fresh, headers=headers).status_code == 401
    assert rig.backend.prepared == rig.backend.writes == []


@pytest.mark.parametrize(
    "target,devices",
    [
        ("furnace", {"furnace"}),
        ("gas_all", {"gas_0", "gas_1", "gas_2"}),
        ("all_motion", {"stage", "robot", "microscope"}),
    ],
)
def test_observer_stop_needs_no_operator_or_command_initialization(live_rig, target, devices):
    rig = live_rig(allow_physical=True)
    _, _, headers = connect(rig, grant=False)
    assert rig.host._commands is None and rig.host._command_backend is None
    response = rig.agent.post(
        BASE + "/stop", json={"request_id": "observer-stop", "target": target}, headers=headers
    )
    assert response.status_code == 202
    record = wait_for(
        lambda: row if (row := rig.host.stops.get("observer-stop"))["status"] != "pending" else None
    )
    assert record["status"] == "confirmed"
    assert record["sender"]["role"] == "observer" and record["sender"]["verified"]
    assert set(rig.backend.stopped) == devices
    assert rig.backend.prepared == rig.backend.writes == []
    assert rig.authority.snapshot()["operator"] is None
    assert rig.host._commands is None


def test_handover_with_own_agent_lease_drains_only_that_page(live_rig):
    rig = live_rig(allow_physical=True)
    _, session, headers = connect(rig)
    submit(
        rig,
        headers,
        envelope(rig, "furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 2}),
    )
    assert rig.service.console.sessions["furnace"].page == "agent-" + session["session_id"]
    response = rig.owner.post(BASE + "/take-human", json={})
    assert response.status_code == 200 and response.json["operator"]["kind"] == "human"
    wait_for(lambda: rig.backend.released)
    assert rig.backend.released == ["agent-" + session["session_id"]]
    assert rig.service.console.sessions == {}
    assert rig.backend.stopped == rig.backend.cancelled == []


@pytest.mark.parametrize("foreign", ["human-page", "agent-other-session", "lent", "reserved"])
def test_handover_refuses_foreign_lent_or_reserved_device_control(live_rig, foreign):
    rig = live_rig(allow_physical=True)
    _, session, _ = connect(rig)
    if foreign == "reserved":
        rig.service.console.reserved.add("stage")
    else:
        rig.service.console.sessions["stage"] = SimpleNamespace(
            page="agent-" + session["session_id"] if foreign == "lent" else foreign,
            lent=foreign == "lent",
            state="held",
        )
    response = rig.owner.post(BASE + "/take-human", json={})
    assert response.status_code == 409
    assert rig.authority.snapshot()["operator"]["id"] == session["session_id"]
    assert rig.backend.writes == rig.backend.stopped == rig.backend.released == []
