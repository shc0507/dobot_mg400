"""The local process owns heartbeats and bounded reads without browser polling."""

import json
import threading
import time

import pytest
from flask import Flask

from labkit.errors import ServiceError
from orchestrator.agent_connect import authority as authority_module
from orchestrator.agent_connect import portal_runtime
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.control_portal import PortalControl, register_control_portal
from orchestrator.agent_connect.live import STATE


class Source:
    def __init__(self):
        self.authority = Authority()
        self.calls = []
        self.reads = []
        self.heartbeats = threading.Condition()
        self.heartbeat_count = 0
        self.failure = None
        self.block_token = None
        self.blocked = threading.Event()
        self.release = threading.Event()

    def __call__(self, source, action, data=None, *, token=None):
        self.calls.append((source, action, token))
        authority = self.authority
        if action == "status":
            try:
                principal = authority.authenticate(token) if token else None
            except ServiceError:
                principal = None
            return authority.snapshot(principal)
        if action == "pair":
            return authority.pair(data["invitation"], data["name"])
        if action == "pair/status":
            return authority.credential_status(data["credential"])
        if action == "connect":
            return authority.connect(data["credential"])
        if action == "heartbeat":
            if token == self.block_token:
                self.blocked.set()
                assert self.release.wait(2)
            if self.failure:
                raise self.failure
            result = authority.heartbeat(authority.authenticate(token))
            with self.heartbeats:
                self.heartbeat_count += 1
                self.heartbeats.notify_all()
            return result
        if action == "disconnect":
            return authority.disconnect(authority.authenticate(token))
        pytest.fail("The background agent sent an equipment or control command: " + action)

    def read(self, path):
        assert path == STATE
        self.reads.append(path)
        return {
            "type": "state",
            "boot_id": "read-stream-boot",
            "contract_id": "unverified-contract",
            "seq": len(self.reads),
            "devices": {"stage": {"position": len(self.reads)}},
        }

    def wait_heartbeats(self, count):
        with self.heartbeats:
            assert self.heartbeats.wait_for(lambda: self.heartbeat_count >= count, timeout=3)


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(authority_module, "SESSION_TIMEOUT_S", 0.16)
    monkeypatch.setattr(portal_runtime, "SESSION_TIMEOUT_S", 0.16)
    monkeypatch.setattr(portal_runtime, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(portal_runtime, "READ_INTERVAL_S", 0.02)
    source = Source()
    control = PortalControl(source, live_reader=source.read)
    yield control, source
    source.release.set()
    control.close()
    source.authority.close()


def connect(control, source, origin="live"):
    invitation = source.authority.create_invitation(source.authority.human)["invitation"]
    control.action(origin, "pair", {"invitation": invitation, "name": "PC agent"})
    return control.action(origin, "connect", {})


def test_process_keeps_session_and_latest_read_past_timeout_without_browser(rig):
    control, source = rig
    connected = connect(control, source)
    token = control.records["live"]["session_token"]
    assert connected["operator"] is None
    start = time.monotonic()
    foreground_calls = len(source.calls)
    source.wait_heartbeats(12)
    assert time.monotonic() - start > authority_module.SESSION_TIMEOUT_S
    assert source.authority.authenticate(token).id == connected["principal"]["id"]
    assert all(action == "heartbeat" for _, action, _ in source.calls[foreground_calls:])
    assert source.authority.snapshot()["operator"] is None
    report = control.local_status("live", include_state=True)
    read = report["read_collection"]
    assert report["running"] and report["heartbeat_owner"] == "process"
    assert report["last_heartbeat_at"] is not None
    assert read["seq"] > 1 and read["received_at"] is not None
    assert read["raw_state"]["seq"] == read["seq"]
    assert read["quality"] == read["verification"] == "unverified"
    assert read["boot_id"] != connected["boot_id"]
    assert "credential" not in json.dumps(report) and token not in json.dumps(report)


@pytest.mark.parametrize("end", ["disconnect", "revoke", "restart", "expire"])
def test_terminal_connection_stops_heartbeats_and_collection(rig, end):
    control, source = rig
    connected = connect(control, source)
    session = control.sessions["live"]
    source.wait_heartbeats(1)
    if end == "disconnect":
        assert not control.action("live", "disconnect", {})["connected"]
    elif end == "revoke":
        source.authority.revoke(source.authority.human, connected["client_id"])
    elif end == "restart":
        source.authority.restart()
    else:
        with source.authority.lock:
            source.authority._sessions[connected["principal"]["id"]].deadline = 0
    assert session.stopped.wait(2)
    session.join()
    count = len(source.calls), len(source.reads)
    assert not control.local_status("live")["running"]
    assert "session_token" not in control.records["live"]
    assert not session.snapshot()["read_collection"]["running"]
    assert (len(source.calls), len(source.reads)) == count
    assert len([action for _, action, _ in source.calls if action == "connect"]) == 1


def test_pairing_and_status_do_not_start_background_work(rig):
    control, source = rig
    invitation = source.authority.create_invitation(source.authority.human)["invitation"]
    control.action("live", "pair", {"invitation": invitation, "name": "PC agent"})
    control.status("live")
    assert control.sessions == {}
    assert source.heartbeat_count == 0 and source.reads == []


def test_process_renews_an_existing_grant_without_replaying_work(rig):
    control, source = rig
    connected = connect(control, source)
    source.authority.grant(source.authority.human, connected["principal"]["id"])
    source.wait_heartbeats(12)
    assert source.authority.snapshot()["operator"]["id"] == connected["principal"]["id"]
    assert set(action for _, action, _ in source.calls) <= {
        "pair",
        "pair/status",
        "connect",
        "status",
        "heartbeat",
    }


def test_slow_read_does_not_delay_heartbeats_or_apply_after_disconnect(rig):
    control, source = rig
    blocked, release = threading.Event(), threading.Event()

    def read(path):
        blocked.set()
        assert release.wait(3)
        return source.read(path)

    control.live_reader = read
    connect(control, source)
    try:
        assert blocked.wait(2)
        source.wait_heartbeats(12)
        session = control.sessions["live"]
        control.action("live", "disconnect", {})
    finally:
        release.set()
    session.join()
    assert session.snapshot(include_state=True)["read_collection"]["raw_state"] is None


def test_late_heartbeat_from_old_connection_cannot_stop_new_connection(rig):
    control, source = rig
    connect(control, source)
    old = control.sessions["live"]
    source.block_token = old.token
    assert source.blocked.wait(2)
    control.action("live", "disconnect", {})
    reconnected = control.action("live", "connect", {})
    current = control.sessions["live"]
    assert current is not old and current.token != old.token
    source.release.set()
    old.join()
    source.wait_heartbeats(2)
    assert control.records["live"]["session_token"] == current.token
    assert current.snapshot()["running"]
    assert reconnected["operator"] is None


def test_transient_failure_retries_only_existing_heartbeat_then_stops(rig):
    control, source = rig
    connect(control, source)
    source.failure = ServiceError(503, "Offline")
    session = control.sessions["live"]
    assert session.stopped.wait(2)
    session.join()
    assert "expired" in session.snapshot()["error"]
    assert len([action for _, action, _ in source.calls if action == "connect"]) == 1


def test_shutdown_stops_workers_and_disconnects_without_commands(rig):
    control, source = rig
    connect(control, source)
    session = control.sessions["live"]
    source.wait_heartbeats(1)
    control.close()
    assert all(not thread.is_alive() for thread in session.threads)
    assert source.authority.snapshot()["sessions"] == []
    assert source.calls[-1][1] == "disconnect"
    with pytest.raises(ServiceError, match="stopping"):
        control.action("live", "connect", {})


def test_mock_connection_does_not_collect_live_state(rig):
    control, source = rig
    connect(control, source, "mock")
    source.wait_heartbeats(2)
    assert source.reads == []
    assert control.local_status("mock")["read_collection"]["running"] is False


def test_collection_keeps_one_bounded_raw_snapshot_and_marks_failed_reads(rig):
    control, source = rig
    oversized = threading.Event()

    def read(path):
        if oversized.is_set():
            return {"payload": "x" * portal_runtime.MAX_STATE_BYTES}
        return source.read(path)

    control.live_reader = read
    connect(control, source)
    source.wait_heartbeats(2)
    before = control.local_status("live", include_state=True)["read_collection"]
    oversized.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        after = control.local_status("live", include_state=True)["read_collection"]
        if after["error"]:
            break
        time.sleep(0.005)
    assert after["error"]
    assert after["quality"] == "last_received"
    assert after["raw_state"]["seq"] >= before["raw_state"]["seq"]
    assert "payload" not in after["raw_state"]
    after["raw_state"]["devices"].clear()
    assert control.local_status("live", include_state=True)["read_collection"]["raw_state"][
        "devices"
    ]


def test_latest_route_does_not_contact_console_or_expose_session(rig):
    _control, source = rig
    app = Flask(__name__)
    control = register_control_portal(app, source, live_reader=source.read)
    try:
        connect(control, source)
        source.wait_heartbeats(1)
        response = app.test_client().get("/api/agent-control/latest?source=live")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json["read_collection"]["raw_state"]["type"] == "state"
        assert "session_token" not in response.text and "credential" not in response.text
        assert app.test_client().get("/api/agent-control/latest?url=other").status_code == 400
    finally:
        control.close()
