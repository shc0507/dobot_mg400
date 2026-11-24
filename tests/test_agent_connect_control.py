"""Phase 4 approval and handover through HTTP, with zero equipment access."""

import threading
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

from labkit.errors import ServiceError
from orchestrator.agent_connect.access import install_access
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.control import install_control
from orchestrator.agent_connect.control_portal import PortalControl, register_control_portal

BASE = "/lab/api/agent/control"


@pytest.fixture
def rig(tmp_path):
    app = Flask(__name__)
    app.testing = True
    authority = Authority(tmp_path / "authority.db")
    host = install_control(app, authority=authority)
    install_access(app, "/lab")
    writes = []

    @app.errorhandler(ServiceError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @app.post("/lab/api/console/command/<device>")
    def command(device):
        writes.append(device)
        return jsonify(ok=True)

    @app.post("/lab/api/runs")
    def recipe():
        writes.append("recipe")
        return jsonify(ok=True)

    @app.post("/lab/api/console/stop/furnace")
    def stop():
        writes.append("emergency-stop")
        return jsonify(ok=True)

    yield app, authority, host, writes
    host.close()


def approved_agent(http, authority, name="Test PC"):
    invitation = http.post(BASE + "/invitations", json={}).json["invitation"]
    paired = http.post(BASE + "/pair", json={"invitation": invitation, "name": name}).json
    assert paired["status"] == "approved"
    connected = http.post(BASE + "/connect", json={"credential": paired["credential"]}).json
    return paired, connected


def test_console_approval_handover_cancels_next_step_without_hardware(rig):
    app, authority, host, writes = rig
    owner, agent = app.test_client(), app.test_client()
    paired, connection = approved_agent(owner, authority)
    token = {"Authorization": "Bearer " + connection["session_token"]}
    state = owner.post(BASE + "/grant", json={"session_id": connection["session_id"]}).json
    epoch = state["control_epoch"]
    first = agent.post(BASE + "/simulate", json={"control_epoch": epoch}, headers=token)
    assert first.status_code == 200
    operation = first.json["simulation"]["id"]
    step = {"operation_id": operation, "control_epoch": epoch}
    assert (
        agent.post(BASE + "/simulate/step", json=step, headers=token).json["simulation"][
            "completed_steps"
        ]
        == 1
    )
    takeover = owner.post(BASE + "/take-human", json={})
    assert takeover.status_code == 200
    assert takeover.json["simulation"]["status"] == "cancelled"
    assert agent.post(BASE + "/simulate/step", json=step, headers=token).status_code in {403, 409}
    assert host.simulation["completed_steps"] == 1
    assert writes == []
    reconnected = agent.post(BASE + "/connect", json={"credential": paired["credential"]}).json
    assert reconnected["state"]["operator"]["kind"] == "human"
    assert reconnected["session_id"] != connection["session_id"]


def test_agent_cannot_bypass_through_furnace_or_recipe_routes(rig):
    app, authority, _host, writes = rig
    owner, agent = app.test_client(), app.test_client()
    _paired, session = approved_agent(owner, authority)
    grant = owner.post(BASE + "/grant", json={"session_id": session["session_id"]}).json
    headers = {
        "Authorization": "Bearer " + session["session_token"],
        "X-MG400-Client": "owner-forged",
        "X-MG400-Control-Epoch": grant["control_epoch"],
    }
    for path, data in [
        ("/lab/api/console/command/furnace", {"action": "start_sequence"}),
        ("/lab/api/console/command/stage", {"action": "move_to_position"}),
        ("/lab/api/runs", {"recipe": "heat-sample"}),
    ]:
        assert agent.post(path, json=data, headers=headers).status_code == 403
        assert (
            agent.post(path, json=data, headers={"X-MG400-Client": "owner-forged"}).status_code
            == 403
        )
    assert writes == []
    assert (
        agent.post(
            "/lab/api/console/stop/furnace", json={}, headers={"Authorization": "bad"}
        ).status_code
        == 200
    )
    assert writes == ["emergency-stop"]


def test_direct_console_management_needs_no_password_cookie_or_tunnel(rig):
    app, authority, _host, writes = rig
    http = app.test_client()
    origin = "http://100.64.0.1:8082"
    state = http.get(BASE + "/status", base_url=origin)
    assert state.status_code == 200
    assert state.json["management_available"] is True
    assert state.json["principal"]["kind"] == "human"
    assert "Set-Cookie" not in state.headers
    assert not hasattr(authority, "owner_code")
    assert http.post(BASE + "/owner/login", json={}, base_url=origin).status_code == 404
    invite = http.post(BASE + "/invitations", json={}, base_url=origin)
    assert invite.status_code == 200
    assert len(invite.json["invitation"]) == 9
    assert invite.json["invitation"][4] == "-"
    assert http.post("/lab/api/console/command/stage", json={}, base_url=origin).status_code == 200
    assert writes == ["stage"]


def test_expired_owner_cookie_is_ignored_but_invalid_agent_token_is_rejected(rig):
    app, _authority, _host, writes = rig
    http = app.test_client()
    http.set_cookie("mg400_owner_old", "expired-owner-token")
    assert http.post(BASE + "/invitations", json={}).status_code == 200
    assert (
        http.post(
            BASE + "/invitations", json={}, headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )
    assert writes == []


def test_foreign_origin_cannot_manage_connections(rig):
    app, _authority, _host, _writes = rig
    assert (
        app.test_client()
        .post(BASE + "/invitations", json={}, headers={"Origin": "http://evil.example"})
        .status_code
        == 403
    )


def test_agent_grant_refuses_busy_real_console_without_stopping_it(rig):
    app, authority, host, writes = rig
    owner = app.test_client()
    _paired, session = approved_agent(owner, authority)
    host.service = SimpleNamespace(
        demo=False,
        console=SimpleNamespace(
            lock=threading.RLock(), sessions={"robot": object()}, reserved=set()
        ),
    )
    response = owner.post(BASE + "/grant", json={"session_id": session["session_id"]})
    assert response.status_code == 409
    assert authority.snapshot()["operator"] is None
    assert writes == []


def test_simulated_stop_and_contract_need_no_identity(rig):
    app, _authority, _host, writes = rig
    http = app.test_client()
    assert http.post(BASE + "/simulate/stop", json={}).status_code == 200
    definition = http.get(BASE + "/contract").json
    assert definition["version"] == 6
    assert definition["pairing"] == {"approval": "invitation", "operator_grant": "explicit"}
    assert definition["commands_enabled"] is False
    assert definition["furnace_control_enabled"] is False
    assert definition["furnace"]["operate"] is False
    assert writes == []


def test_portal_retains_secrets_only_in_server_memory(rig):
    app, authority, _host, writes = rig
    owner, upstream = app.test_client(), app.test_client()

    def transport(source, action, data=None, *, token=None):
        assert source in {"live", "mock"}
        headers = {"Authorization": "Bearer " + token} if token else {}
        response = (
            upstream.get(BASE + "/" + action, headers=headers)
            if data is None
            else upstream.post(BASE + "/" + action, json=data, headers=headers)
        )
        if response.status_code >= 400:
            raise ServiceError(response.status_code, response.json["error"])
        return response.json

    relay = PortalControl(transport)
    invitation = owner.post(BASE + "/invitations", json={}).json["invitation"]
    paired = relay.action("live", "pair", {"invitation": invitation, "name": "PC portal"})
    assert paired["client_status"] == "approved"
    assert "credential" not in paired and "session_token" not in paired
    assert relay.status("live")["client_status"] == "approved"
    connected = relay.action("live", "connect", {})
    assert connected["connected"] and connected["operator"] is None
    assert relay.action("live", "heartbeat", {})["connected"]
    assert not relay.action("live", "disconnect", {})["connected"]
    assert writes == []


def test_invitation_grants_connection_access_but_not_operator_control(rig):
    app, authority, _host, writes = rig
    http = app.test_client()
    paired, connection = approved_agent(http, authority)
    assert connection["state"]["operator"] is None
    token = {"Authorization": "Bearer " + connection["session_token"]}
    assert http.post(BASE + "/approve", json={"client_id": paired["client_id"]}).status_code == 404
    denied = http.post(
        BASE + "/simulate",
        json={"control_epoch": connection["state"]["control_epoch"]},
        headers=token,
    )
    assert denied.status_code == 403
    assert writes == []


def test_local_portal_rejects_foreign_browser_requests():
    app = Flask(__name__)
    register_control_portal(app, lambda *args, **kwargs: pytest.fail("No request may reach Olares"))
    response = app.test_client().post(
        "/api/agent-control/pair", json={}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
