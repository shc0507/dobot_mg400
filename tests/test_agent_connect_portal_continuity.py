"""PC restart retains pairing and intent, but never retains a control grant."""

import base64
import json
import threading

import pytest
from flask import Flask

from labkit.errors import ServiceError
from orchestrator.agent_connect import portal_continuity, portal_store
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.control_portal import (
    ORIGINS,
    PortalControl,
    register_control_portal,
)
from orchestrator.agent_connect.portal_store import PortalPairingStore


class Protector:
    def protect(self, value):
        return b"test-ciphertext:" + value[::-1]

    def unprotect(self, value):
        assert value.startswith(b"test-ciphertext:")
        return value.removeprefix(b"test-ciphertext:")[::-1]


class Source:
    def __init__(self):
        self.authority = Authority()
        self.calls = []
        self.offline = False
        self.started = threading.Event()
        self.release = None

    def __call__(self, source, action, data=None, *, token=None):
        self.calls.append((source, action, data, token))
        if self.offline:
            raise ServiceError(503, "Offline")
        authority = self.authority
        if action == "connect":
            self.started.set()
            if self.release is not None:
                assert self.release.wait(3)
            return authority.connect(data["credential"])
        if action == "pair":
            return authority.pair(data["invitation"], data["name"])
        if action == "pair/status":
            return authority.credential_status(data["credential"])
        if action == "status":
            return authority.snapshot(authority.authenticate(token) if token else None)
        if action == "heartbeat":
            return authority.heartbeat(authority.authenticate(token))
        if action == "disconnect":
            return authority.disconnect(authority.authenticate(token))
        pytest.fail("Restoration must not send control or equipment actions: " + action)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_continuity, "RESTORE_INTERVAL_S", 0.001)
    source = Source()
    controls = []
    path = tmp_path / "pairing.json"

    def make():
        control = PortalControl(source, pairing_store=PortalPairingStore(path, Protector()))
        controls.append(control)
        return control

    yield make, source, path
    if source.release is not None:
        source.release.set()
    for control in controls:
        control.close()
    source.authority.close()


def pair(control, source, *, connect=True, target="live"):
    invitation = source.authority.create_invitation(source.authority.human)["invitation"]
    control.action(target, "pair", {"invitation": invitation, "name": "PC agent"})
    if connect:
        return control.action(target, "connect", {})


def restored(control):
    control.start_restore()
    if control.continuity.thread is not None:
        control.continuity.thread.join(timeout=3)
        assert not control.continuity.thread.is_alive()
    return control.local_status("live")["continuity"]


def test_restart_creates_observer_even_when_previous_session_is_operator(rig):
    make, source, path = rig
    first = make()
    connected = pair(first, source)
    old_token = first.records["live"]["session_token"]
    source.authority.grant(source.authority.human, connected["principal"]["id"])
    # Model an abrupt PC process exit: no remote disconnect and no old session token.
    first.sessions["live"].stop()
    first.sessions["live"].join()
    first.continuity.close()
    before = len(source.calls)
    second = make()
    assert second.sessions == {}
    assert "session_token" not in second.records["live"]
    assert restored(second)["state"] == "connected"
    state = second.status("live")
    assert state["connected"] and state["operator"] is None
    assert state["principal"]["id"] != connected["principal"]["id"]
    assert second.records["live"]["session_token"] != old_token
    assert {call[1] for call in source.calls[before:]} <= {
        "connect",
        "status",
        "pair/status",
        "heartbeat",
    }
    raw = path.read_text()
    assert old_token not in raw and first.records["live"]["credential"] not in raw
    saved = json.loads(Protector().unprotect(base64.b64decode(json.loads(raw)["protected"])))
    assert set(saved["pairing"]) == {
        "client_id",
        "credential",
        "name",
        "matching_code",
        "connect_intent",
    }
    assert "operator" not in json.dumps(saved) and "session_token" not in json.dumps(saved)


@pytest.mark.parametrize("disconnect", [False, True])
def test_pair_only_and_explicit_disconnect_do_not_restore_connection(rig, disconnect):
    make, source, _path = rig
    first = make()
    pair(first, source, connect=disconnect)
    if disconnect:
        first.action("live", "disconnect", {})
    first.close()
    calls = len(source.calls)
    second = make()
    assert restored(second)["state"] == "disconnected"
    assert second.records["live"]["credential"]
    assert len(source.calls) == calls
    assert second.action("live", "connect", {})["connected"]


def test_clean_shutdown_preserves_user_connect_intent(rig):
    make, source, _path = rig
    first = make()
    pair(first, source)
    first.close()
    assert source.authority.snapshot()["sessions"] == []
    second = make()
    assert restored(second)["state"] == "connected"
    assert source.authority.snapshot()["operator"] is None


def test_startup_offline_retry_is_bounded_and_status_is_visible_without_network(rig):
    make, source, _path = rig
    first = make()
    pair(first, source)
    first.close()
    source.offline = True
    before = len(source.calls)
    second = make()
    status = restored(second)
    assert status["state"] == "offline" and status["paired"]
    assert status["attempts"] == portal_continuity.RESTORE_ATTEMPTS
    assert len(source.calls) - before == portal_continuity.RESTORE_ATTEMPTS
    assert all(call[1] == "connect" for call in source.calls[before:])
    assert second.records["live"]["connect_intent"] is True
    source.offline = False
    assert second.action("live", "connect", {})["connected"]


def test_disconnect_cancels_pending_restore_and_persists_disconnected_intent(rig):
    make, source, _path = rig
    first = make()
    pair(first, source)
    first.close()
    source.started.clear()
    source.release = threading.Event()
    second = make()
    second.start_restore()
    assert source.started.wait(1)
    assert second.local_status("live")["continuity"]["state"] == "connecting"
    state = second.action("live", "disconnect", {})
    assert not state["connected"]
    assert second.records["live"]["connect_intent"] is False
    source.release.set()
    second.continuity.thread.join(timeout=2)
    assert second.sessions == {}
    assert source.authority.snapshot()["sessions"] == []
    second.close()
    third = make()
    assert restored(third)["state"] == "disconnected"


def test_shutdown_keeps_pairing_lock_until_late_restore_and_cleanup_finish(rig, monkeypatch):
    make, source, path = rig
    first = make()
    pair(first, source)
    first.close()
    source.started.clear()
    source.release = threading.Event()
    second = make()
    second.start_restore()
    assert source.started.wait(1)
    monkeypatch.setattr(portal_continuity, "RESTORE_CLOSE_WAIT_S", 0.01)
    second.close()
    assert second.continuity.thread.is_alive()
    competing = PortalPairingStore(path, Protector())
    try:
        assert not competing.status()["storage_available"]
        assert "Another portal" in competing.status()["storage_message"]
        assert competing.load(ORIGINS["live"]) == {}
    finally:
        competing.close()
    source.release.set()
    second.continuity.thread.join(timeout=2)
    assert not second.continuity.thread.is_alive()
    assert source.authority.snapshot()["sessions"] == []
    third = make()
    assert third.continuity.snapshot()["storage_available"]
    assert restored(third)["state"] == "connected"
    assert third.status("live")["connected"]


@pytest.mark.parametrize("invalid", [False, True])
def test_revoked_or_invalid_saved_credentials_are_forgotten(rig, invalid):
    make, source, _path = rig
    first = make()
    paired = pair(first, source)
    first.close()
    if invalid:
        source.authority._clients.clear()
    else:
        source.authority.revoke(source.authority.human, paired["client_id"])
    second = make()
    status = restored(second)
    assert status["state"] == "forgotten" and not status["paired"]
    assert not second.records["live"].get("credential")
    second.close()
    third = make()
    assert third.records["live"] == {}


@pytest.mark.parametrize("connected", [False, True])
def test_status_forgets_revoked_pairing_without_waiting_for_restart(rig, connected):
    make, source, _path = rig
    first = make()
    pair(first, source, connect=connected)
    source.authority.revoke(source.authority.human, first.records["live"]["client_id"])
    status = first.status("live")
    assert status["client_status"] == "revoked" and not status["paired"]
    first.close()
    assert make().records["live"] == {}


def test_bad_connect_credential_is_forgotten_after_manual_connect(rig):
    make, source, _path = rig
    first = make()
    pair(first, source, connect=False)
    source.authority._clients.clear()
    with pytest.raises(ServiceError):
        first.action("live", "connect", {})
    first.close()
    assert make().records["live"] == {}


def test_restore_refuses_operator_response_and_disconnects_only_that_session(rig):
    make, source, _path = rig
    first = make()
    pair(first, source)
    first.close()
    second = make()

    def unexpected_operator(*args, **kwargs):
        result = source(*args, **kwargs)
        if args[1] == "connect":
            result["state"]["operator"] = result["state"]["principal"]
        return result

    second.transport = unexpected_operator
    before = len(source.calls)
    assert restored(second)["state"] == "failed"
    assert second.sessions == {}
    assert source.authority.snapshot()["sessions"] == []
    assert [call[1] for call in source.calls[before:]] == ["connect", "disconnect"]


def test_failed_disconnect_save_removes_old_connected_intent(rig, monkeypatch):
    make, source, path = rig
    first = make()
    pair(first, source)

    def denied(*args):
        raise OSError("Disk error")

    monkeypatch.setattr(portal_store.os, "replace", denied)
    first.action("live", "disconnect", {})
    assert not path.exists()
    assert first.local_status("live")["continuity"]["state"] == "unavailable"
    first.close()
    second = make()
    assert restored(second)["state"] == "disconnected"


def test_mock_credentials_and_connect_intent_never_enter_live_store(rig):
    make, source, path = rig
    first = make()
    pair(first, source, target="mock")
    assert not path.exists()
    first.close()
    second = make()
    assert restored(second)["state"] == "disconnected"
    assert second.records == {"live": {}, "mock": {}}


def test_store_binds_credentials_to_console_origin_and_owns_process_lock(tmp_path):
    path = tmp_path / "pairing.json"
    first = PortalPairingStore(path, Protector())
    second = PortalPairingStore(path, Protector())
    assert first.status()["storage_available"]
    assert not second.status()["storage_available"]
    assert "Another portal" in second.status()["storage_message"]
    assert second.load(ORIGINS["live"]) == {}
    first.save(
        "http://different-console",
        {
            "credential": "secret",
            "client_id": "id",
            "name": "name",
            "matching_code": "ABC123",
            "connect_intent": True,
        },
    )
    first.close()
    second.close()
    with_store = PortalPairingStore(path, Protector())
    try:
        assert with_store.load(ORIGINS["live"]) == {}
        assert not with_store.status()["storage_available"]
        assert "different Console" in with_store.status()["storage_message"]
    finally:
        with_store.close()


def test_corrupt_store_is_visible_and_is_not_overwritten(tmp_path):
    path = tmp_path / "pairing.json"
    path.write_bytes(b"broken")
    store = PortalPairingStore(path, Protector())
    try:
        assert store.load(ORIGINS["live"]) == {}
        assert not store.status()["storage_available"]
        assert path.read_bytes() == b"broken"
    finally:
        store.close()


def test_offline_route_returns_local_restore_status_without_secrets(rig):
    make, source, path = rig
    first = make()
    pair(first, source)
    first.close()
    source.offline = True
    app = Flask(__name__)
    control = register_control_portal(
        app, source, pairing_store=PortalPairingStore(path, Protector())
    )
    try:
        response = app.test_client().get("/api/agent-control/status")
        assert response.status_code == 503
        assert response.json["local_agent"]["continuity"]["paired"]
        assert "credential" not in response.text and "session_token" not in response.text
        latest = app.test_client().get("/api/agent-control/latest")
        assert latest.status_code == 200 and latest.json["continuity"]["connect_intent"]
    finally:
        control.close()
