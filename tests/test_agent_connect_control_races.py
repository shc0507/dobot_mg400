"""Serialize trusted Console handover with device work without owner sign-in."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request

from labkit.errors import ServiceError
from orchestrator.agent_connect.access import install_access
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.control import install_control

BASE = "/lab/api/agent/control"
TAKE = "/lab/api/console/control/stage/take"


@pytest.fixture
def make_rig():
    hosts = []

    def make():
        app = Flask(__name__)
        app.testing = True
        authority = Authority()
        console = SimpleNamespace(lock=threading.RLock(), sessions={}, reserved=set())
        service = SimpleNamespace(demo=False, console=console)
        arrivals = {}
        entered, continue_write = threading.Event(), threading.Event()
        continue_write.set()

        @app.before_request
        def arrived():
            if event := arrivals.get(request.path):
                event.set()

        host = install_control(app, service, authority=authority)
        install_access(app, "/lab")
        hosts.append(host)

        @app.errorhandler(ServiceError)
        def error(exc):
            return jsonify(error=exc.message), exc.status

        @app.post(TAKE)
        def acquire_device():
            # Model the persistent session left by an accepted device acquisition.
            # No adapter, socket, or hardware is used by this fixture.
            entered.set()
            assert continue_write.wait(5), "The test did not release the admitted write."
            with console.lock:
                console.sessions["stage"] = "active-device-session"
            return jsonify(ok=True)

        return SimpleNamespace(
            app=app,
            authority=authority,
            host=host,
            console=console,
            arrivals=arrivals,
            entered=entered,
            continue_write=continue_write,
        )

    yield make
    for host in hosts:
        host.close()


def connect_agent(owner):
    invitation = owner.post(BASE + "/invitations", json={}).json["invitation"]
    paired = owner.post(
        BASE + "/pair", json={"invitation": invitation, "name": "Concurrent request test"}
    ).json
    assert paired["status"] == "approved"
    connected = owner.post(BASE + "/connect", json={"credential": paired["credential"]})
    assert connected.status_code == 200
    return connected.json["session_id"]


@pytest.mark.parametrize("action", ["grant", "take-human", "release"])
def test_idle_check_and_ownership_change_exclude_concurrent_device_acquisition(
    make_rig, monkeypatch, action
):
    rig = make_rig()
    owner = rig.app.test_client()
    state = owner.post(BASE + "/take-human", json={}).json
    epoch = state["control_epoch"]
    transition_owner, body = rig.app.test_client(), {}
    if action == "grant":
        body = {"session_id": connect_agent(owner)}
    elif action == "take-human":
        state = owner.post(BASE + "/grant", json={"session_id": connect_agent(owner)}).json
        epoch = state["control_epoch"]
    elif action == "release":
        body = {"control_epoch": epoch}
    writer = rig.app.test_client()
    idle_checked, continue_transition = threading.Event(), threading.Event()
    write_arrived = rig.arrivals[TAKE] = threading.Event()
    original_idle = rig.host.idle

    def paused_idle():
        original_idle()
        idle_checked.set()
        assert continue_transition.wait(5), "The test did not release the transition."

    monkeypatch.setattr(rig.host, "idle", paused_idle)
    with ThreadPoolExecutor(max_workers=2) as executor:
        transition = executor.submit(transition_owner.post, BASE + "/" + action, json=body)
        try:
            assert idle_checked.wait(2)
            write = executor.submit(
                writer.post, TAKE, json={}, headers={"X-MG400-Control-Epoch": epoch}
            )
            assert write_arrived.wait(2)
            assert not rig.entered.wait(0.1), "A write entered after the idle check."
        finally:
            continue_transition.set()
        assert transition.result(timeout=2).status_code == 200
        assert write.result(timeout=2).status_code in {401, 403, 409}
    assert not rig.entered.is_set()
    assert rig.console.sessions == {}


def test_grant_waits_for_admitted_human_write_then_refuses_busy_device(make_rig):
    rig = make_rig()
    console = rig.app.test_client()
    session_id = connect_agent(console)
    rig.continue_write.clear()
    grant_arrived = rig.arrivals[BASE + "/grant"] = threading.Event()
    writer, manager = rig.app.test_client(), rig.app.test_client()
    grant_done = threading.Event()

    def grant():
        try:
            return manager.post(BASE + "/grant", json={"session_id": session_id})
        finally:
            grant_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        write = executor.submit(writer.post, TAKE, json={})
        try:
            assert rig.entered.wait(2)
            granting = executor.submit(grant)
            assert grant_arrived.wait(2)
            assert not grant_done.wait(0.1), "Grant passed an admitted device write."
        finally:
            rig.continue_write.set()
        assert write.result(timeout=2).status_code == 200
        assert granting.result(timeout=2).status_code == 409
    assert rig.authority.snapshot()["operator"] is None
    assert rig.console.sessions == {"stage": "active-device-session"}


def test_agent_disconnect_cannot_bypass_busy_human_handover(make_rig):
    rig = make_rig()
    console = rig.app.test_client()
    state = console.post(BASE + "/take-human", json={}).json
    rig.console.sessions["stage"] = "active-device-session"
    assert console.post(BASE + "/disconnect", json={}).status_code == 403
    assert (
        console.post(BASE + "/release", json={"control_epoch": state["control_epoch"]}).status_code
        == 409
    )
    after = console.get(BASE + "/status").json
    assert after["management_available"] is True
    assert after["operator"] == state["operator"]
    assert after["control_epoch"] == state["control_epoch"]
    assert rig.console.sessions == {"stage": "active-device-session"}
