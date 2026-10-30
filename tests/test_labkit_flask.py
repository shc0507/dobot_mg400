import json
import threading

import pytest
from contract_helpers import assert_error, check_contract
from flask import Flask

from labkit.describe import command, describe
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.flask import contract_blueprint
from labkit.lease import Lease

TOKEN = "t" * 24


class DemoService:
    def __init__(self):
        self.now = 10
        self.expiry_count = 0
        self.locked = False
        self.calls = []
        self.stop_outcome = "confirmed"
        self.events = EventLog(clock=lambda: self.now)
        self.error = None

    def advance(self, seconds):
        self.now += seconds

    def describe(self):
        return describe(
            service="demo",
            version="1.0",
            demo=True,
            zones=[],
            on_lease_expiry="stop motion",
            commands={"move": command(), "reset": command(any_client=True)},
        )

    def snapshot(self):
        return dict(
            service="demo",
            seq=len(self.calls),
            ts_unix=self.now,
            demo=True,
            connected=True,
            busy=False,
            locked=self.locked,
            fault={"code": "unknown", "message": "Check device"} if self.locked else None,
            last_command=None,
        )

    def command(self, action, args, client):
        if self.error:
            raise self.error
        if self.locked:
            raise ServiceError(409, "locked")
        self.calls.append((action, args, client))
        return str(len(self.calls))

    def stop(self, client, wait_s):
        self.calls.append(("stop", wait_s, client))
        return self.stop_outcome

    def on_lease_expired(self):
        self.expiry_count += 1
        self.events.add("warning", "lease expired")


def make_app(*, token=None, base_path="/", service=None):
    service = service or DemoService()
    lease = Lease(3, lambda: service.now)
    app = Flask(__name__)
    app.register_blueprint(
        contract_blueprint(service, token=token, base_path=base_path, lease=lease)
    )
    return app, service, lease


@pytest.mark.parametrize("token", [None, TOKEN])
@pytest.mark.parametrize("base_path", ["/", "/instrument/"])
def test_demo_contract_conformance(token, base_path):
    app, service, _ = make_app(token=token, base_path=base_path)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    check_contract(
        app.test_client(),
        advance=service.advance,
        expiry_count=lambda: service.expiry_count,
        base_path=base_path,
        headers=headers,
    )


def test_commands_require_lease_and_locked_state_refuses_any_client_commands():
    app, service, lease = make_app()
    client = app.test_client()
    headers = {"X-MG400-Client": "a"}
    assert_error(client.post("/api/command", json={"action": "move"}, headers=headers), 409)
    reset = client.post("/api/command", json={"action": "reset", "value": 4}, headers=headers)
    assert reset.status_code == 202
    assert reset.json["command_id"] == "1"
    assert service.calls == [("reset", {"value": 4}, "a")]
    lease.acquire("a")
    move = client.post("/api/command", json={"action": "move"}, headers=headers)
    assert move.status_code == 202
    service.locked = True
    for action in ("move", "reset"):
        assert_error(client.post("/api/command", json={"action": action}, headers=headers), 409)
    stop = client.post("/api/stop", json={"wait_s": 15}, headers={"X-MG400-Client": "b"})
    assert stop.status_code == 200
    assert stop.json["state"]["locked"] is True
    assert lease.holder == "a"
    assert service.calls[-1] == ("stop", 15, "b")


@pytest.mark.parametrize("outcome", ["confirmed", "pending", "unconfirmed"])
def test_stop_outcome_is_preserved(outcome):
    app, service, _ = make_app()
    service.stop_outcome = outcome
    result = app.test_client().post("/api/stop", json={}, headers={"X-MG400-Client": "b"})
    assert result.json["stop"] == outcome


def test_stop_runs_during_expiry_callback_without_taking_lease_lock():
    entered, finish = threading.Event(), threading.Event()
    service = DemoService()

    def expire():
        service.expiry_count += 1
        entered.set()
        assert finish.wait(2)

    service.on_lease_expired = expire
    app, _, lease = make_app(service=service)
    lease.acquire("a")
    service.advance(3)
    watcher = threading.Thread(target=lease.poll)
    watcher.start()
    try:
        assert entered.wait(2)
        result = app.test_client().post("/api/stop", json={}, headers={"X-MG400-Client": "b"})
        assert result.status_code == 200
        assert service.calls == [("stop", 0, "b")]
        assert_error(
            app.test_client().post("/api/heartbeat", json={}, headers={"X-MG400-Client": "b"}), 409
        )
    finally:
        finish.set()
        watcher.join(2)
    assert not watcher.is_alive()


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
def test_tokenless_access_rejects_forwarding_even_empty(header):
    client = make_app()[0].test_client()
    assert_error(client.get("/api/state", headers={header: ""}), 403)


@pytest.mark.parametrize(
    "host", ["lab.invalid", "localhost.evil", "user@localhost", "127.0.0.1/path"]
)
def test_tokenless_access_rejects_nonloopback_or_invalid_host(host):
    assert_error(make_app()[0].test_client().get("/api/state", headers={"Host": host}), 403)


@pytest.mark.parametrize("token", ["x" * 23, "x" * 23 + "\n", " x" * 12, "é" * 24])
def test_bad_token_configuration_fails(token):
    with pytest.raises(ValueError):
        make_app(token=token)


def test_bearer_token_and_origin_rules_apply_to_reads_and_stops():
    client = make_app(token=TOKEN)[0].test_client()
    auth = {"Authorization": f"Bearer {TOKEN}", "X-MG400-Client": "a"}
    assert client.get("/api/state", headers={**auth, "Host": "lab.invalid"}).status_code == 200
    assert (
        client.get("/api/state", headers={**auth, "Origin": "http://localhost"}).status_code == 200
    )
    for route in ("/api/describe", "/api/state", "/api/events"):
        assert_error(client.get(route), 401)
        assert_error(client.get(route, headers={**auth, "Origin": "https://evil.invalid"}), 403)
        assert_error(client.get(route, headers={**auth, "Sec-Fetch-Site": "cross-site"}), 403)
    assert_error(client.post("/api/stop", json={}, headers={**auth, "Origin": "null"}), 403)
    reply = client.get("/api/state", headers=auth)
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("data", [None, [], "text", 1])
def test_nonobject_json_is_invalid(data):
    response = (
        make_app()[0]
        .test_client()
        .post(
            "/api/heartbeat",
            data=json.dumps(data),
            content_type="application/json",
            headers={"X-MG400-Client": "a"},
        )
    )
    assert_error(response, 400)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        15.1,
        True,
        "1",
        float("inf"),
        float("nan"),
        pytest.param(10**1000, id="huge-positive-integer"),
        pytest.param(-(10**1000), id="huge-negative-integer"),
    ],
)
def test_invalid_stop_wait(value):
    app, service, _ = make_app()
    response = app.test_client().post(
        "/api/stop", json={"wait_s": value}, headers={"X-MG400-Client": "a"}
    )
    assert_error(response, 400)
    assert service.calls == []


@pytest.mark.parametrize("client", [None, "", "x" * 129, "é", "\x7f"])
def test_post_requires_valid_client_header(client):
    headers = {} if client is None else {"X-MG400-Client": client}
    assert_error(make_app()[0].test_client().post("/api/stop", json={}, headers=headers), 400)


def test_json_media_type_malformed_json_actions_and_service_error_bodies():
    app, service, lease = make_app()
    client = app.test_client()
    headers = {"X-MG400-Client": "a"}
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "application/ld+json"):
        assert_error(
            client.post("/api/heartbeat", data="{}", content_type=content_type, headers=headers),
            415,
        )
    assert_error(
        client.post("/api/heartbeat", data="{", content_type="application/json", headers=headers),
        400,
    )
    for action in (None, "missing", []):
        assert_error(client.post("/api/command", json={"action": action}, headers=headers), 400)
    lease.acquire("a")
    for status in (400, 409, 503):
        service.error = ServiceError(status, "fixture error")
        assert_error(client.post("/api/command", json={"action": "move"}, headers=headers), status)


def test_event_cursor_and_read_access_do_not_acquire_lease():
    app, service, lease = make_app()
    service.events.add("info", "first")
    service.events.add("info", "second")
    client = app.test_client()
    result = client.get("/api/events?since=1")
    assert result.json["next"] == 2
    assert [event["message"] for event in result.json["events"]] == ["second"]
    assert lease.holder is None
    for since in ("-1", "nan", "1.5", "", "١"):
        assert_error(client.get("/api/events", query_string={"since": since}), 400)


def test_stop_survives_failed_expiry_and_does_not_trigger_a_pending_expiry():
    service = DemoService()

    def expire():
        service.expiry_count += 1
        service.locked = True
        raise RuntimeError("unknown stop outcome")

    service.on_lease_expired = expire
    app, _, lease = make_app(service=service)
    lease.acquire("a")
    service.advance(3)
    client = app.test_client()
    reply = client.post("/api/stop", json={}, headers={"X-MG400-Client": "b"})
    assert reply.status_code == 200
    assert reply.json["stop"] == "confirmed"
    assert service.expiry_count == 0
    with pytest.raises(RuntimeError):
        lease.poll()
    reply = client.post("/api/stop", json={}, headers={"X-MG400-Client": "b"})
    assert reply.status_code == 200
    assert reply.json["state"]["locked"] is True
    assert service.expiry_count == 1
    assert_error(client.post("/api/heartbeat", json={}, headers={"X-MG400-Client": "b"}), 409)


def test_any_client_command_cannot_bypass_running_or_failed_expiry():
    entered, finish = threading.Event(), threading.Event()
    failures = []
    service = DemoService()

    def expire():
        service.expiry_count += 1
        entered.set()
        assert finish.wait(2)
        raise RuntimeError("expiry failed before the service set locked")

    service.on_lease_expired = expire
    app, _, lease = make_app(service=service)
    lease.acquire("a")
    service.advance(3)

    def poll():
        try:
            lease.poll()
        except RuntimeError as error:
            failures.append(error)

    watcher = threading.Thread(target=poll)
    watcher.start()
    client = app.test_client()
    headers = {"X-MG400-Client": "b"}
    try:
        assert entered.wait(2)
        assert_error(client.post("/api/command", json={"action": "reset"}, headers=headers), 409)
        assert service.calls == []
        assert client.post("/api/stop", json={}, headers=headers).status_code == 200
    finally:
        finish.set()
        watcher.join(2)
    assert not watcher.is_alive()
    assert len(failures) == 1 and not service.locked
    assert_error(client.post("/api/command", json={"action": "reset"}, headers=headers), 409)
    assert client.post("/api/stop", json={}, headers=headers).status_code == 200
    assert service.calls == [("stop", 0, "b"), ("stop", 0, "b")]
