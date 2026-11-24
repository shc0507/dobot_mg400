"""Trusted Console writes and agent credentials share one operator boundary."""

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request

from labkit.errors import ServiceError
from orchestrator.agent_connect.access import EPOCH_HEADER, install_access
from orchestrator.web.control_socket import _Handler, _Server

AGENT_TOKEN = "agent-session-credential-1234567890"
EPOCH = "boot-a:1"
PAGE = "motion-page-1"


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str


class Authority:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = self.lookups = 0
        self.epoch = EPOCH
        self.agent_controls = False
        self.sessions = {AGENT_TOKEN: Principal("agent", "agent-session")}

    def authenticate(self, token):
        self.lookups += 1
        if token not in self.sessions:
            raise ServiceError(401, "Session expired.")
        return self.sessions[token]

    @contextmanager
    def human_guard(self, epoch=None):
        with self.lock:
            if epoch is not None and epoch != self.epoch:
                raise ServiceError(409, "Control changed.")
            if self.agent_controls:
                raise ServiceError(403, "The agent holds control.")
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1


@pytest.fixture
def rig():
    app = Flask(__name__)
    app.testing = True
    authority = Authority()
    app.extensions["agent_authority"] = lambda: authority
    install_access(app, "/lab")

    @app.errorhandler(ServiceError)
    def error(value):
        return jsonify(error=value.message), value.status

    @app.route("/lab/api/<path:name>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def endpoint(name):
        if name == "refused-write":
            assert authority.depth == 1
            raise ServiceError(409, "The device refused this write.")
        return jsonify(
            path=name,
            client=request.headers.get("X-MG400-Client"),
            guarded=authority.depth,
        )

    return app, authority, app.test_client()


def headers(token=None, epoch=None, **extra):
    value = {"X-MG400-Client": PAGE}
    if token is not None:
        value["Authorization"] = "Bearer " + token
    if epoch is not None:
        value[EPOCH_HEADER] = epoch
    return {**value, **extra}


def relay(app):
    """Use the socket dispatcher without opening a network listener."""
    server = SimpleNamespace(app=app, prefix="/lab")
    server.call = partial(_Server.call, server)
    return server


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_trusted_human_writes_preserve_page_identity_and_hold_dispatch_guard(rig, method):
    _app, authority, client = rig
    path = "/lab/api/console/command/stage"
    for epoch in (None, EPOCH):
        response = client.open(path, method=method, json={}, headers=headers(epoch=epoch))
        assert response.status_code == 200
        assert response.json["client"] == PAGE
        assert response.json["guarded"] == 1
        assert authority.depth == 0
    assert authority.lookups == 0


@pytest.mark.parametrize("epoch", ["old:1", "", "x" * 257])
def test_supplied_invalid_or_old_control_epoch_rejects_human_writes(rig, epoch):
    _app, authority, client = rig
    response = client.post("/lab/api/command", json={}, headers=headers(epoch=epoch))
    assert response.status_code == 409
    assert authority.depth == 0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_trusted_human_cannot_write_while_agent_holds_control(rig, method):
    _app, authority, client = rig
    authority.agent_controls = True
    for epoch in (None, EPOCH):
        response = client.open(
            "/lab/api/console/command/stage", method=method, json={}, headers=headers(epoch=epoch)
        )
        assert response.status_code == 403
    assert authority.lookups == 0
    assert authority.depth == 0


def test_device_rejection_releases_human_dispatch_guard(rig):
    _app, authority, client = rig
    assert client.post("/lab/api/refused-write", json={}, headers=headers()).status_code == 409
    assert authority.depth == 0
    assert client.post("/lab/api/command", json={}, headers=headers()).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/console/command/furnace",
        "/api/console/command/stage",
        "/api/console/control/furnace/take",
        "/api/command",
        "/api/heartbeat",
        "/api/runs",
        "/api/runs/example/resume",
        "/api/console/ws/sample-transfer/position/move",
        "/api/console/ws/sample-transfer/profile/upload",
        "/api/console/ws/process/turret/begin",
        "/api/console/ws/position-finder/enabled",
        "/api/future-write-route",
    ],
)
def test_agent_operator_cannot_bypass_phase4_through_other_write_routes(rig, path):
    _app, authority, client = rig
    authority.agent_controls = True
    response = client.post(
        "/lab" + path, json={"action": "start_sequence"}, headers=headers(AGENT_TOKEN, EPOCH)
    )
    assert response.status_code == 403
    assert "command catalog" in response.json["error"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_agent_observer_cannot_send_normal_equipment_writes(rig, method):
    _app, _authority, client = rig
    response = client.open(
        "/lab/api/console/command/stage", method=method, json={}, headers=headers(AGENT_TOKEN)
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "authorization",
    ["", "garbage", "Bearer short", "Bearer unknown-session-credential-00000000"],
)
def test_invalid_supplied_bearer_never_falls_back_to_trusted_human(rig, authorization):
    _app, _authority, client = rig
    response = client.post(
        "/lab/api/command", json={}, headers=headers(Authorization=authorization)
    )
    assert response.status_code == 401


def test_cookies_do_not_change_human_or_agent_identity(rig):
    _app, authority, client = rig
    client.set_cookie("mg400_owner", "obsolete-owner-credential")
    client.set_cookie("agent_session", AGENT_TOKEN)
    path = "/lab/api/console/command/furnace"
    response = client.post(path, json={}, headers=headers())
    assert response.status_code == 200
    assert response.json["client"] == PAGE
    assert authority.lookups == 0
    assert client.post(path, json={}, headers=headers(AGENT_TOKEN)).status_code == 403
    assert client.post(path, json={}, headers=headers(Authorization="garbage")).status_code == 401


@pytest.mark.parametrize(
    "origin",
    ["https://other.example", "null", "http://localhost@other.example", "http://localhost/path"],
)
def test_foreign_browser_writes_fail_for_trusted_human_requests(rig, origin):
    _app, _authority, client = rig
    assert (
        client.post("/lab/api/command", json={}, headers=headers(Origin=origin)).status_code == 403
    )


def test_same_origin_and_server_clients_keep_original_page_identity(rig):
    _app, _authority, client = rig
    for extra in ({"Origin": "http://localhost"}, {}):
        response = client.post("/lab/api/command", json={}, headers=headers(**extra))
        assert response.status_code == 200
        assert response.json["client"] == PAGE


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/stop", {}),
        ("/api/dashboard/motion", {}),
        ("/api/dashboard/heat", {"confirmed": True}),
        ("/api/dashboard/gas", {"confirmed": True}),
        ("/api/console/stop/furnace", {}),
        ("/api/console/ws/process/gas-stop", {"channel_index": 0}),
        ("/api/console/ws/sample-transfer/stop", {}),
        ("/api/console/ws/motion/stage/release", {"id": "hold-test"}),
        ("/api/console/ws/motion/keys", {"seq": 9, "held": []}),
    ],
)
def test_stop_does_not_lookup_session_or_require_epoch(rig, path, body):
    app, authority, client = rig

    def unavailable():
        raise AssertionError("Stop touched authority storage.")

    app.extensions["agent_authority"] = unavailable
    response = client.post("/lab" + path, json=body, headers={"Authorization": "expired"})
    assert response.status_code == 200
    assert response.json["client"] == "stop-unverified"
    assert authority.lookups == 0


def test_stop_exceptions_do_not_include_generic_or_similarly_named_commands(rig):
    _app, _authority, client = rig
    for path, body in [
        ("/api/console/command/furnace", {"action": "stop_fake"}),
        ("/api/console/stop/unknown-device", {}),
        ("/api/console/ws/motion/keys", {"seq": 1, "held": ["W"]}),
        ("/api/console/ws/motion/keys", {"seq": 1, "held": [], "action": "move"}),
    ]:
        assert (
            client.post("/lab" + path, json=body, headers=headers(AGENT_TOKEN)).status_code == 403
        )


def test_reads_remain_public_and_agent_read_identity_ignores_page_claim(rig):
    _app, authority, client = rig
    authority.agent_controls = True
    path = "/lab/api/console/state"
    assert client.get(path).status_code == 200
    assert client.get(path, headers=headers(Authorization="garbage")).status_code == 200
    assert client.get(path, headers=headers("expired-session-credential-0000")).status_code == 200
    result = client.get(path, headers=headers(AGENT_TOKEN))
    assert result.status_code == 200
    assert result.json["client"] == "agent-agent-session"
    assert result.json["guarded"] == 0


def test_public_read_never_needs_authority_storage(rig):
    app, authority, client = rig

    def unavailable():
        raise AssertionError("Public reads touched authority storage.")

    app.extensions["agent_authority"] = unavailable
    assert client.get("/lab/api/agent/v1/state").status_code == 200
    assert authority.lookups == 0


def test_management_routes_use_their_own_validation(rig):
    _app, authority, client = rig
    response = client.post("/lab/api/agent/control/pair", json={})
    assert response.status_code == 200
    assert authority.lookups == 0
    assert response.json["guarded"] == 0


def test_socket_human_requests_preserve_page_and_recheck_control_per_message(rig):
    app, authority, _client = rig
    server = relay(app)
    credentials = {"Host": "localhost", "Cookie": "obsolete-owner=ignored"}
    path = "/api/console/ws/motion/stage/hold"
    status, body = server.call(
        PAGE, path, {}, "127.0.0.1", credential_headers=credentials, epoch=EPOCH
    )
    assert status == 200 and body["client"] == PAGE and body["guarded"] == 1
    authority.epoch = "boot-a:2"
    assert (
        server.call(PAGE, path, {}, "127.0.0.1", credential_headers=credentials, epoch=EPOCH)[0]
        == 409
    )
    authority.agent_controls = True
    assert server.call(PAGE, path, {}, "127.0.0.1", credential_headers=credentials)[0] == 403
    assert (
        server.call(
            PAGE,
            "/api/console/stop/stage",
            {},
            "127.0.0.1",
            credential_headers=credentials,
            epoch="old",
        )[0]
        == 200
    )


def test_socket_agent_credentials_cannot_become_human_after_expiry(rig):
    app, authority, _client = rig
    server = relay(app)
    credentials = {"Host": "localhost", "Authorization": "Bearer " + AGENT_TOKEN}
    path = "/api/console/ws/motion/stage/hold"
    assert server.call(PAGE, path, {}, "127.0.0.1", credential_headers=credentials)[0] == 403
    authority.sessions.clear()
    assert server.call(PAGE, path, {}, "127.0.0.1", credential_headers=credentials)[0] == 401


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/api/console/ws/motion/stage/release", {"id": "hold1234567890123456"}),
        ("/api/console/ws/motion/keys", {"seq": 2, "held": []}),
    ],
)
def test_human_release_preserves_hold_page_without_authority_lookup(rig, path, data):
    app, _authority, client = rig
    result = client.post("/lab/api/console/ws/motion/stage/hold", json={}, headers=headers())
    assert result.status_code == 200

    def blocked():
        raise AssertionError("Stop must never wait for authority.")

    app.extensions["agent_authority"] = blocked
    result = client.post("/lab" + path, json=data, headers=headers())
    assert result.status_code == 200
    assert result.json["client"] == PAGE


def test_agent_stop_uses_cached_identity_after_session_expiry_without_lookup(rig):
    app, authority, client = rig
    result = client.get("/lab/api/console/state", headers=headers(AGENT_TOKEN))
    assert result.json["client"] == "agent-agent-session"
    authority.sessions.clear()

    def blocked():
        raise AssertionError("Stop must never wait for authority.")

    app.extensions["agent_authority"] = blocked
    result = client.post("/lab/api/console/stop/stage", json={}, headers=headers(AGENT_TOKEN))
    assert result.status_code == 200
    assert result.json["client"] == "agent-agent-session"
    assert AGENT_TOKEN not in str(app.extensions["agent_stop_identities"])


def test_socket_messages_cannot_drop_agent_credentials_and_stop_skips_busy_slots(rig):
    app, authority, _client = rig
    replies, answered = [], threading.Event()

    def send(text):
        replies.append(json.loads(text))
        answered.set()

    handler = object.__new__(_Handler)
    handler.server = relay(app)
    handler.client_address = ("127.0.0.1", 1234)
    handler.credential_headers = {"Host": "localhost", "Authorization": "Bearer " + AGENT_TOKEN}
    connection = SimpleNamespace(send_text=send)
    slots = threading.BoundedSemaphore(1)
    for seq, expected in [(1, 403), (2, 401)]:
        answered.clear()
        handler._message(
            connection,
            PAGE,
            json.dumps(
                {
                    "seq": seq,
                    "path": "/api/console/ws/motion/stage/hold",
                    "body": {},
                    "control_epoch": EPOCH,
                    "headers": {"Authorization": "", "X-MG400-Client": "trusted-human"},
                    "Authorization": None,
                    "Cookie": "obsolete-owner=ignored",
                }
            ),
            slots,
        )
        assert answered.wait(2)
        assert replies[-1]["status"] == expected
        authority.sessions.clear()
    answered.clear()
    assert slots.acquire(blocking=False)
    handler._message(
        connection,
        PAGE,
        json.dumps(
            {
                "seq": 3,
                "path": "/api/console/stop/stage",
                "body": {},
                "control_epoch": {"invalid": True},
            }
        ),
        slots,
    )
    assert answered.wait(2)
    assert replies[-1]["status"] == 200
    slots.release()
