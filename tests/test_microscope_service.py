"""Candidate MIC-03 adapter tests. All transports and the CLI server are fake."""

from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.lease import Lease
from microscope import __main__ as microscope_main
from microscope.app import create_app


class AdapterService:
    def __init__(self, *_args, **_kwargs):
        self.lease = Lease(on_expire=self.on_lease_expired)
        self.events = EventLog()
        self.calls = []
        self.started = self.closed = False

    def on_lease_expired(self):
        self.calls.append(("expired",))

    def describe(self):
        return {
            "service": "microscope",
            "version": "1",
            "contract": 1,
            "demo": True,
            "zones": [],
            "lease_seconds": 3,
            "on_lease_expiry": "stop and disable",
            "commands": {"set_focus_zero": {"params": {"type": "object"}, "any_client": False}},
        }

    def snapshot(self):
        return {"locked": False, "demo": True}

    def command(self, action, args, client):
        raise ServiceError(403, "Use the explicit operator action")

    def manual_heartbeat(self, client, *, renew_only=False):
        self.calls.append(("heartbeat", client, renew_only))
        return self.lease.renew(client) if renew_only else self.lease.acquire(client)

    def manual_release(self, client):
        self.calls.append(("release", client))
        return self.lease.release(client)

    def manual_command(self, action, args, client):
        with self.lease.control(client):
            self.calls.append((action, args, client))
            return "offline-command"

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


def test_adapter_manual_zero_requires_explicit_confirmation_and_service_lease():
    service = AdapterService()
    client = create_app(service, base_path="/scope").test_client()
    headers = {"X-MG400-Client": "operator"}
    route = "/scope/api/manual/set_focus_zero"
    assert client.post(route, json={"confirm": True}, headers=headers).status_code == 409
    assert client.post("/scope/api/manual/heartbeat", json={}, headers=headers).status_code == 200
    for body in (
        {},
        {"confirm": False},
        {"confirm": 1},
        {"confirm": "true"},
        {"confirm": True, "x": 1},
    ):
        assert client.post(route, json=body, headers=headers).status_code == 400
    assert all(call[0] != "set_focus_zero" for call in service.calls)
    reply = client.post(route, json={"confirm": True}, headers=headers)
    assert reply.status_code == 202
    assert reply.json["command_id"] == "offline-command"
    assert reply.json["state"]["lease"]["holder"] == "operator"
    assert service.calls[-1] == ("set_focus_zero", {"confirm": True}, "operator")
    assert client.post("/scope/api/manual/renew", json={}, headers=headers).status_code == 200
    assert client.post("/scope/api/manual/release", json={}, headers=headers).status_code == 200
    assert client.post("/scope/api/manual/renew", json={}, headers=headers).status_code == 409
    assert not service.started


@pytest.mark.parametrize(
    "extra",
    [
        {"Host": "outside.invalid"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": ""},
        {"Origin": "https://outside.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_manual_routes_keep_shared_host_origin_and_forwarded_checks(extra):
    service = AdapterService()
    reply = (
        create_app(service)
        .test_client()
        .post("/api/manual/heartbeat", json={}, headers={"X-MG400-Client": "operator", **extra})
    )
    assert reply.status_code == 403 and set(reply.json) == {"error"}
    assert service.calls == []


def test_manual_routes_require_token_json_and_client_and_bound_payload():
    service = AdapterService()
    token = "t" * 24
    client = create_app(service, token=token).test_client()
    route = "/api/manual/heartbeat"
    assert client.post(route, json={}, headers={"X-MG400-Client": "operator"}).status_code == 401
    auth = {"Authorization": "Bearer " + token}
    assert client.post(route, json={}, headers=auth).status_code == 400
    headers = {**auth, "X-MG400-Client": "operator"}
    assert client.post(route, data="{}", headers=headers).status_code == 415
    assert client.post(route, json=[], headers=headers).status_code == 400
    assert client.post(route, json={"unexpected": True}, headers=headers).status_code == 400
    assert client.post(route, json={"data": "x" * 65536}, headers=headers).status_code == 413
    reply = client.post(route, json={}, headers=headers)
    assert reply.status_code == 200
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.headers["X-Content-Type-Options"] == "nosniff"
    assert (
        client.post("/api/command", json={"action": "set_focus_zero"}, headers=headers).status_code
        == 403
    )
    assert all(call[0] != "set_focus_zero" for call in service.calls)


class AdapterConfig:
    @classmethod
    def from_env(cls, env):
        return dict(env)


def fake_cli_signal(monkeypatch):
    handlers = {2: 0, 15: 0, 1: 0}

    def install(number, handler):
        old = handlers[number]
        handlers[number] = handler
        return old

    fake = SimpleNamespace(
        SIGINT=2, SIGTERM=15, SIGHUP=1, SIG_IGN=1, getsignal=handlers.__getitem__, signal=install
    )
    monkeypatch.setattr(microscope_main, "signal", fake)
    return handlers


@pytest.mark.parametrize("signum", [2, 15, 1])
def test_cli_signals_stop_service_close_server_and_restore_handlers(monkeypatch, signum):
    handlers = fake_cli_signal(monkeypatch)
    service, server = AdapterService(), SimpleNamespace(closed=False)
    settings = {}

    def make_server(app, **kwargs):
        settings.update(kwargs)
        server.run = lambda: handlers[signum](signum, None)
        server.close = lambda: setattr(server, "closed", True)
        return server

    assert (
        microscope_main.main(
            ["--demo"],
            environ={},
            config_factory=AdapterConfig,
            service_factory=lambda config, demo: service,
            server_factory=make_server,
        )
        == 0
    )
    assert service.started and service.closed and server.closed
    assert settings["host"] == "127.0.0.1" and settings["port"] == 8770
    assert settings["clear_untrusted_proxy_headers"] is False
    assert handlers == {2: 0, 15: 0, 1: 0}


@pytest.mark.parametrize(
    "environ",
    [
        {"MG400_MICROSCOPE_BIND": "0.0.0.0"},
        {"MG400_MICROSCOPE_TOKEN": "short"},
        {"MG400_MICROSCOPE_PORT": "65536"},
        {"MG400_MICROSCOPE_PORT": "invalid"},
    ],
)
def test_cli_rejects_bad_access_settings_before_construction(environ):
    with pytest.raises(SystemExit) as error:
        microscope_main.main(
            [],
            environ=environ,
            config_factory=AdapterConfig,
            service_factory=lambda *_args, **_kwargs: pytest.fail("service constructed"),
        )
    assert error.value.code == 2


@pytest.mark.parametrize("failure", ["startup", "server", "service_close"])
def test_cli_failure_still_cleans_both_resources_and_exits_two(monkeypatch, failure):
    handlers = fake_cli_signal(monkeypatch)
    service, server = AdapterService(), SimpleNamespace(closed=False)

    def fail():
        raise RuntimeError("offline failure")

    if failure == "startup":
        service.start = fail
    if failure == "service_close":
        service.close = fail

    def make_server(app, **kwargs):
        server.run = fail if failure == "server" else lambda: None
        server.close = lambda: setattr(server, "closed", True)
        return server

    with pytest.raises(SystemExit) as error:
        microscope_main.main(
            ["--demo"],
            environ={},
            config_factory=AdapterConfig,
            service_factory=lambda config, demo: service,
            server_factory=make_server,
        )
    assert error.value.code == 2 and server.closed
    assert service.closed or failure == "service_close"
    assert handlers == {2: 0, 15: 0, 1: 0}


@pytest.mark.parametrize("value", ["1", "1,2,3", "0.5,2", "-1,2;bad"])
def test_focus_config_refuses_noninteger_limits(value):
    from microscope.service import MicroscopeConfig

    with pytest.raises(ValueError):
        MicroscopeConfig.from_env({"MG400_MICROSCOPE_FOCUS_LIMITS": value})


def test_real_focus_configuration_has_no_invented_defaults():
    from microscope.service import MicroscopeConfig

    config = MicroscopeConfig.from_env({})
    assert config.focus_limits is config.switch_focus is config.home_switch is None
    with pytest.raises(ValueError, match="Measured"):
        config.validate()


def test_manual_reference_configuration_requires_clearance_zero():
    from microscope.service import MicroscopeConfig

    with pytest.raises(ValueError, match="clearance"):
        MicroscopeConfig(focus_limits=(-100, 100), switch_focus=20, home_switch=False).validate()
    assert MicroscopeConfig(focus_limits=(-100, 100), switch_focus=20, home_switch=True).validate()


def test_service_construction_is_inert(monkeypatch):
    import threading

    import serial

    from microscope.service import MicroscopeService

    monkeypatch.setattr(threading.Thread, "start", lambda *_: pytest.fail("thread started"))
    monkeypatch.setattr(serial, "Serial", lambda **_: pytest.fail("device opened"))
    service = MicroscopeService(demo=True)
    assert service.snapshot()["focus_zero_known"] is False
    assert service.snapshot()["connected"] is False
    assert service._motor is service._camera is service._side is None
