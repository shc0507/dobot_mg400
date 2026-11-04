"""HTTP/entrypoint checks with simulated serial, fake servers, and no sockets."""

import signal
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import minimalmodbus
import pytest
import serial
from contract_helpers import assert_error, check_contract

from furnace import controller as p
from furnace.__main__ import main
from furnace.app import create_app, make_service

TOKEN = "furnace-test-access-token-12345"
THREAD_START = threading.Thread.start


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1_000_000 + self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def offline(monkeypatch, clock):
    def forbidden(*args, **kwargs):
        raise AssertionError("A furnace HTTP test attempted real I/O or thread startup")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(p, "time", SimpleNamespace(time=clock.wall, sleep=clock.advance))
    monkeypatch.setattr(
        minimalmodbus, "time", SimpleNamespace(monotonic=clock, sleep=lambda _: None)
    )
    monkeypatch.setattr(minimalmodbus, "_latest_read_times", {})
    monkeypatch.setattr(minimalmodbus, "_serialports", {})


@pytest.fixture
def service(clock):
    value = make_service(demo=True, clock=clock, wall_clock=clock.wall)
    value.start(background=False)
    yield value
    value.close()


def post(client, path, body=None, *, actor="page-test", token=None, method="POST", headers=None):
    request_headers = {"X-MG400-Client": actor, **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    return client.open(
        path, method=method, json={} if body is None else body, headers=request_headers
    )


def test_imports_are_inert():
    code = """
import builtins, os, socket, ssl, threading, serial
from pathlib import Path
def forbidden(*args, **kwargs): raise AssertionError("import side effect")
serial.Serial = socket.socket = threading.Thread.start = os.open = forbidden
Path.write_text = Path.write_bytes = builtins.open = forbidden
import furnace.app
import furnace.__main__
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("base", ["/", "/lab/furnace/"])
def test_demo_contract(service, clock, base):
    expired = []
    original = service.lifecycle.lease.on_expire

    def expiry():
        expired.append(True)
        original()

    service.lifecycle.lease.on_expire = expiry
    client = create_app(service, token=TOKEN, base_path=base).test_client()
    check_contract(
        client,
        advance=clock.advance,
        expiry_count=lambda: len(expired),
        base_path=base,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_removed_page_and_legacy_routes_are_not_served(service):
    client = create_app(service, token=TOKEN, base_path="/heat/").test_client()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/heat/", "/heat/static/app.js", "/heat/api/status", "/heat/video_feed"):
        assert_error(client.get(path, headers=auth), 404)
    for path in ("/heat/api/furnace/apply", "/heat/api/sequence/start", "/heat/api/flow/connect"):
        assert_error(post(client, path, token=TOKEN), 404)


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/api/state", "GET", None),
        ("/api/furnace/acknowledge", "POST", {}),
        ("/api/flow/gas/stop", "POST", {"channel_index": 0}),
        ("/api/stop", "POST", {}),
    ],
)
def test_legacy_and_contract_access_are_uniform(service, path, method, body):
    client = create_app(service, token=TOKEN).test_client()
    assert_error(post(client, path, body, method=method), 401)
    assert_error(
        post(client, path, body, method=method, token=TOKEN, headers={"Origin": "null"}), 403
    )
    if method != "GET":
        assert_error(
            client.open(
                path,
                method=method,
                data="{}",
                content_type="text/plain",
                headers={"Authorization": f"Bearer {TOKEN}", "X-MG400-Client": "test"},
            ),
            415,
        )
        assert_error(
            client.open(path, method=method, json={}, headers={"Authorization": f"Bearer {TOKEN}"}),
            400,
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "remote.example"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "host=localhost"},
        {"Host": "localhost,remote"},
    ],
)
def test_no_token_loopback_and_forwarding_rules(service, headers):
    client = create_app(service).test_client()
    assert_error(client.get("/api/state", headers=headers), 403)


def test_acknowledge_needs_the_lease_holder_or_a_free_lease(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service.lifecycle, "acknowledge", lambda *, client: calls.append(client))
    client = create_app(service).test_client()
    assert post(client, "/api/heartbeat", actor="automation").status_code == 200
    assert_error(post(client, "/api/furnace/acknowledge", actor="page-test"), 409)
    assert_error(post(client, "/api/furnace/acknowledge", {"x": 1}, actor="automation"), 400)
    assert calls == []
    assert post(client, "/api/furnace/acknowledge", actor="automation").status_code == 200
    assert calls == ["automation"]
    assert service.lifecycle.lease.snapshot()["holder"] == "automation"
    assert service.status()["manual_client"] is None
    assert post(client, "/api/furnace/stop").status_code == 200
    assert post(client, "/api/release", actor="automation").status_code == 200
    assert post(client, "/api/furnace/acknowledge").status_code == 200
    assert calls == ["automation", "page-test"]
    assert service.status()["manual_client"] == "page-test"


def test_same_id_contract_acquisition_removes_manual_provenance(service):
    client = create_app(service).test_client()
    assert post(client, "/api/manual/heartbeat").status_code == 200
    assert post(client, "/api/heartbeat").status_code == 200
    assert_error(post(client, "/api/manual/renew"), 409)
    assert post(client, "/api/stop").status_code == 200


def test_expired_manual_renew_never_reacquires_or_steals_automation(service, clock):
    client = create_app(service).test_client()
    assert post(client, "/api/manual/heartbeat").status_code == 200
    clock.advance(4)
    assert_error(post(client, "/api/manual/renew"), 409)
    assert service.lifecycle.lease.snapshot()["holder"] is None
    assert post(client, "/api/heartbeat", actor="automation").status_code == 200
    assert_error(post(client, "/api/manual/renew"), 409)
    assert service.lifecycle.lease.snapshot()["holder"] == "automation"


def test_sequence_commands_preserve_editor_and_gas_plan(service):
    client = create_app(service).test_client()
    body = dict(
        steps=[dict(temperature_c=30, ramp_rate_c_per_min=5, hold_minutes=1, gas={"0": 10})],
        end_gas={"0": 0},
        end_gas_until_c=26,
    )
    assert post(client, "/api/heartbeat").status_code == 200
    for action in ("set_sequence", "start_sequence"):
        assert post(client, "/api/command", {"action": action, **body}).status_code == 202
    state = client.get("/api/state").get_json()
    assert state["busy"] and state["sequence"]["running"]
    assert state["last_command"]["result"] == "ok"
    assert state["flow"]["gas_channels"][0]["target_percent"] == 10
    assert post(client, "/api/sequence/stop", actor="bystander").get_json()["stop"] == "confirmed"


def test_stop_stays_available_after_expiry_callback_failure(service, clock):
    client = create_app(service).test_client()
    post(client, "/api/heartbeat", actor="automation")

    def failed():
        raise RuntimeError("expiry failed")

    service.lifecycle.lease.on_expire = failed
    clock.advance(4)
    with pytest.raises(RuntimeError):
        service.lifecycle.lease.poll()
    assert post(client, "/api/stop", actor="other").status_code == 200
    assert (
        post(client, "/api/flow/gas/stop", {"channel_index": 0}, actor="other").status_code == 200
    )


def test_stop_refuses_false_confirmation_for_silent_gas(service):
    service.lifecycle.connect_flow_controller(
        client=service.lifecycle.lease.acquire("setup")["holder"]
    )
    service.lifecycle.flow_controller.serial.reply_ascii = False
    client = create_app(service).test_client()
    response = post(client, "/api/stop", actor="other")
    assert response.status_code == 200
    assert response.get_json()["stop"] == "unconfirmed"


def test_contract_command_has_completed_id_and_events(service):
    client = create_app(service).test_client()
    post(client, "/api/heartbeat", actor="automation")
    response = post(
        client,
        "/api/command",
        {"action": "apply_setpoint", "temperature_c": 30, "ramp_rate_c_per_min": 5},
        actor="automation",
    )
    assert response.status_code == 202
    result = response.get_json()
    assert result["command_id"] == result["state"]["last_command"]["id"]
    assert result["state"]["last_command"]["result"] == "ok"
    assert client.get("/api/events").get_json()["events"]


@pytest.mark.parametrize(
    "body", [[], {"wait_s": 10**1000}, {"wait_s": True}, {"wait_s": float("nan")}]
)
def test_malformed_stop_is_rejected_before_device_access(service, body):
    client = create_app(service).test_client()
    assert_error(post(client, "/api/stop", body), 400)


def test_configuration_errors_do_not_create_a_server_or_service():
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise AssertionError("unexpected startup")

    with pytest.raises(SystemExit) as result:
        main(
            ["--bind", "0.0.0.0", "--demo"],
            environ={},
            service_factory=factory,
            server_factory=factory,
        )
    assert result.value.code == 2 and calls == []


def test_bad_base_path_cleanup_never_opens_configured_devices(monkeypatch):
    with pytest.raises(SystemExit) as result:
        main(
            ["--base-path", "//invalid"],
            environ={
                "MG400_FURNACE_SERIAL": "/dev/must-not-open",
                "MG400_MFC_SERIAL": "/dev/must-not-open-mfc",
            },
            server_factory=lambda *a, **k: (_ for _ in ()).throw(AssertionError("server created")),
        )
    assert result.value.code == 2


def test_cli_demo_signals_stop_and_close_fake_server(service, monkeypatch):
    events = []
    original_start = service.start
    monkeypatch.setattr(service, "start", lambda: original_start(background=False))

    class Server:
        def run(self):
            events.append("run")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def close(self):
            events.append("close")

    def server(app, **options):
        assert options["port"] == 8769 and options["host"] == "127.0.0.1"
        assert options["clear_untrusted_proxy_headers"] is False
        return Server()

    assert (
        main(["--demo"], environ={}, service_factory=lambda **kw: service, server_factory=server)
        == 0
    )
    assert events == ["run", "close"]
    assert service.lifecycle.status()["run_state_code"] == 1


def test_manual_dispatch_retains_lease_during_racing_automation_acquire(service, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", THREAD_START)
    entered, release, attempted, acquired = (threading.Event() for _ in range(4))
    original = service.lifecycle.acknowledge
    results = {}
    app = create_app(service)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.lifecycle, "acknowledge", blocked)

    def manual():
        results["manual"] = post(app.test_client(), "/api/furnace/acknowledge").status_code

    def automation():
        attempted.set()
        results["automation"] = post(
            app.test_client(), "/api/heartbeat", actor="automation"
        ).status_code
        acquired.set()

    first, second = threading.Thread(target=manual), threading.Thread(target=automation)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert attempted.wait(2)
        assert not acquired.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert results == {"manual": 200, "automation": 409}
    assert post(app.test_client(), "/api/manual/release").status_code == 200
    assert post(app.test_client(), "/api/heartbeat", actor="automation").status_code == 200
    assert_error(post(app.test_client(), "/api/manual/heartbeat", actor="automation"), 409)


def test_stop_finishes_while_expiry_callback_is_pending(service, clock, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", THREAD_START)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = service.lifecycle.lease.on_expire

    def expiry():
        entered.set()
        assert release.wait(2)
        original()

    service.lifecycle.lease.on_expire = expiry
    service.lease.acquire("automation")
    clock.advance(4)

    def poll():
        try:
            service.lease.poll()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=poll)
    worker.start()
    try:
        assert entered.wait(2)
        response = post(create_app(service).test_client(), "/api/stop", actor="bystander")
        assert response.status_code == 200 and response.get_json()["stop"] == "confirmed"
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not errors
