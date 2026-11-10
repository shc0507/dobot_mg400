"""In-process HTTP and lifecycle tests; instruments are injected fakes."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from contract_helpers import assert_error, check_contract

from orchestrator.app import (
    DEMO_MARKER,
    Catalog,
    OrchestratorService,
    Recovery,
    create_app,
    demo_environment,
    demo_isolated,
)
from orchestrator.clients import Borrowed, Clients
from orchestrator.skills import default_registry


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Instrument:
    def __init__(self, name, identity):
        self.name, self.identity, self.timeout = name, identity, 0.05
        self.calls = []
        self.holder = None
        self.closed = False

    def describe(self):
        return {"contract": 1, "demo": True, "lease_seconds": 3}

    def acquire(self):
        self.calls.append("acquire")
        self.holder = self.identity

    def heartbeat(self):
        if self.holder != self.identity:
            raise RuntimeError("Fake lease lost")

    def state(self):
        return {
            "instance": "fake-instance",
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "lease": {"holder": self.holder},
        }

    def owned(self, state):
        return state["lease"]["holder"] == self.identity

    def healthy(self, state):
        return state["connected"] is True

    def stop(self):
        self.calls.append("stop")
        return {"stop": "confirmed"}

    def recovery_stop(self, expected_instance):
        assert expected_instance == "fake-instance"
        self.calls.append("recovery-stop")
        return {"stop": "confirmed"}

    def release(self):
        self.calls.append("release")
        self.holder = None

    def close(self):
        self.calls.append("close")
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Orchestrator app tests must not open a connection")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rig(tmp_path, clock):
    catalog = tmp_path / "recipes"
    catalog.mkdir()
    registry = default_registry()
    created = []

    def fail(ctx):
        raise RuntimeError("Synthetic interrupted step")

    registry.register("test.fail", fail, services=("stage",))

    def factory(required, identity):
        result = {name: Instrument(name, identity) for name in required}
        created.append(result)
        return result

    service = OrchestratorService(
        recipes_dir=catalog,
        demo=True,
        settings={},
        registry=registry,
        client_factory=factory,
        clock=clock,
    ).start(background=False)
    service.created = created
    yield service
    service.close()


def recipe(service, *, fail=False, seconds=0.0, zone=None):
    text = 'name = "HTTP recipe"\nversion = 1\n[params]\n[[steps]]\n'
    if fail:
        text += 'skill = "test.fail"\nparams = {}\n'
    else:
        text += f'skill = "wait"\nparams = {{ seconds = {float(seconds)} }}\n'
    text += "timeout_s = 10\n"
    if zone:
        text += f'zone = "{zone}"\n'
    (service.catalog.directory / "recipe.toml").write_text(text)


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            pytest.fail("Offline operation did not settle")
        time.sleep(0.005)


def post(client, path, data=None, *, actor="operator"):
    headers = {"X-MG400-Client": actor}
    return client.post(path, json={} if data is None else data, headers=headers)


def start_run(service, *, fail=False, seconds=0, zone=None):
    recipe(service, fail=fail, seconds=seconds, zone=zone)
    client = create_app(service).test_client()
    assert post(client, "/api/heartbeat").status_code == 200
    reply = post(client, "/api/runs", {"recipe": "recipe", "params": {}})
    assert reply.status_code == 202, reply.get_json()
    wait_for(lambda: service.current is not None and service.job["done"].is_set())
    return client, service.current


def test_imports_are_inert():
    code = """
import builtins, os, socket, ssl, threading
from pathlib import Path
def forbidden(*args, **kwargs): raise AssertionError('import side effect')
socket.socket = threading.Thread.start = os.open = forbidden
Path.write_text = Path.write_bytes = builtins.open = forbidden
import orchestrator.app
import orchestrator.__main__
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("base", ["/", "/lab/orch/"])
def test_shared_contract(rig, clock, base):
    expired = []
    callback = rig.lease.on_expire
    rig.lease.on_expire = lambda: (expired.append(True), callback())
    client = create_app(rig, base_path=base).test_client()
    check_contract(
        client,
        advance=clock.advance,
        expiry_count=lambda: len(expired),
        base_path=base,
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/recipes", "GET"),
        ("/api/runs", "GET"),
        ("/api/runs/id", "GET"),
        ("/api/runs", "POST"),
        ("/api/runs/id/resume", "POST"),
        ("/api/runs/id/abort", "POST"),
        ("/api/runs/id/recovery", "POST"),
        ("/api/runs/id/recovery/confirm", "POST"),
        ("/api/runs/id/recovery/abort", "POST"),
        ("/api/heartbeat", "POST"),
        ("/api/renew", "POST"),
        ("/api/release", "POST"),
        ("/api/command", "POST"),
        ("/api/stop", "POST"),
    ],
)
def test_all_routes_use_uniform_request_rules(rig, path, method):
    client = create_app(rig).test_client()
    response = client.open(path, method=method, json={}, headers={"Host": "remote.invalid"})
    assert response.status_code != 403
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    if method == "POST":
        headers = {"X-MG400-Client": "test"}
        assert_error(client.post(path, data="{}", content_type="text/plain", headers=headers), 415)
        assert_error(client.post(path, json={}), 400)


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs",
        "/api/command",
        "/api/release",
        "/api/heartbeat",
    ],
)
@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        '{"x":9223372036854775808}',
        '{"x":' + "[" * 22 + "0" + "]" * 22 + "}",
    ],
)
def test_strict_json_precedes_all_mutation(rig, path, raw):
    client = create_app(rig).test_client()
    assert_error(
        client.post(
            path, data=raw, content_type="application/json", headers={"X-MG400-Client": "operator"}
        ),
        400,
    )
    assert rig.store.runs() == [] and rig.created == []


def test_body_limit_and_invalid_client(rig):
    client = create_app(rig).test_client()
    assert_error(
        client.post(
            "/api/runs",
            data=json.dumps({"x": "a" * 70000}),
            content_type="application/json",
            headers={"X-MG400-Client": "test"},
        ),
        413,
    )
    for actor in ("", "a" * 129, "\u00e9"):
        assert_error(post(client, "/api/heartbeat", actor=actor), 400)


def test_catalog_preflight_and_control_before_admission(rig):
    recipe(rig)
    client = create_app(rig).test_client()
    assert client.get("/api/recipes").get_json()["recipes"][0]["id"] == "recipe"
    assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
    assert post(client, "/api/heartbeat").status_code == 200
    for data in (
        {"recipe": "../recipe"},
        {"recipe": "recipe", "params": {"extra": 1}},
        {"recipe": "recipe", "path": "/tmp/no"},
        {"recipe": "recipe", "params": []},
    ):
        assert_error(post(client, "/api/runs", data), 400)
    assert not rig.created and not rig.store.runs()


def test_catalog_rejects_symlinks_and_nonregular_files(tmp_path):
    directory = tmp_path / "catalog"
    directory.mkdir()
    target = tmp_path / "outside.toml"
    target.write_text("[params]\n")
    (directory / "link.toml").symlink_to(target)
    with pytest.raises(OSError):
        Catalog(directory).read("link")
    os.mkfifo(directory / "fifo.toml")
    with pytest.raises(ValueError, match="regular"):
        Catalog(directory).read("fifo")


def test_run_is_persisted_and_history_is_never_replayed(rig):
    client, runner = start_run(rig)
    assert runner.wait_closed(3)
    result = client.get("/api/runs/" + runner.run_id).get_json()
    assert result["status"] == "completed" and result["live"]["closed"]
    assert result["steps"][0]["status"] == "completed"
    assert_error(
        post(
            client,
            f"/api/runs/{runner.run_id}/resume",
            {"manually_completed": True, "note": "No replay"},
        ),
        409,
    )


def test_held_and_draining_runs_keep_admission(rig):
    client, runner = start_run(rig, seconds=5)
    assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
    assert post(client, "/api/stop", actor="other").status_code == 200
    wait_for(lambda: runner.snapshot()["settled"])
    assert runner.status == "held"
    assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
    assert post(client, f"/api/runs/{runner.run_id}/abort").status_code == 202
    assert runner.wait_closed(3)
    assert rig.store.get(runner.run_id)["status"] == "aborted"


@pytest.mark.parametrize("loss", ["release", "expiry", "stop"])
def test_queued_resume_cannot_survive_control_loss(rig, clock, monkeypatch, loss):
    client, runner = start_run(rig, fail=True)
    wait_for(lambda: runner.snapshot()["settled"])
    entered, proceed = threading.Event(), threading.Event()
    original = runner.resume

    def delayed(**kwargs):
        entered.set()
        assert proceed.wait(3)
        return original(**kwargs)

    monkeypatch.setattr(runner, "resume", delayed)
    try:
        assert (
            post(
                client,
                f"/api/runs/{runner.run_id}/resume",
                {"manually_completed": True, "note": "Manual step done"},
            ).status_code
            == 202
        )
        assert entered.wait(2)
        assert post(client, "/api/renew").status_code == 200
        if loss == "expiry":
            clock.advance(4)
            rig.poll_once()
        elif loss == "release":
            assert post(client, "/api/release").status_code == 200
            assert post(client, "/api/heartbeat").status_code == 200
        else:
            assert post(client, "/api/stop", actor="stranger").status_code == 200
        proceed.set()
        wait_for(lambda: rig.job["done"].is_set())
        assert rig.job["error"] and runner.status == "held"
        assert rig.store.get(runner.run_id)["steps"][0]["status"] != "manually_completed"
    finally:
        proceed.set()


def test_polling_expires_without_http_requests(rig, clock):
    _, runner = start_run(rig, seconds=5)
    clock.advance(4)
    rig.poll_once()
    wait_for(lambda: runner.snapshot()["settled"])
    assert runner.status == "held" and rig.lease.snapshot()["holder"] is None


def test_stop_available_after_expiry_callback_failure(rig, clock):
    client, runner = start_run(rig, seconds=5)

    def failed():
        raise RuntimeError("Synthetic expiry callback failure")

    rig.lease.on_expire = failed
    clock.advance(4)
    with pytest.raises(RuntimeError):
        rig.lease.poll()
    assert post(client, "/api/stop", actor="stranger").status_code == 200
    wait_for(lambda: runner.snapshot()["settled"])


def test_close_holds_drains_and_persists_without_rewriting_abort(rig):
    _, runner = start_run(rig, seconds=5)
    path = rig.runs_dir
    rig.close()
    assert runner.wait_closed(0) and runner.status == "held"
    assert not path.exists()


def terminal_run(rig):
    config = Path(rig.settings["MG400_ORCH_ZONES_FILE"])
    config.write_text('[zones.loading]\nowner = "stage"\nconditions = []\n')
    client, runner = start_run(rig, fail=True, zone="loading")
    wait_for(lambda: runner.snapshot()["settled"])
    assert runner.snapshot()["zones"]["loading"]["holder"] == runner.run_id
    assert post(client, f"/api/runs/{runner.run_id}/abort").status_code == 202
    assert runner.wait_closed(3)
    return client, runner


def test_terminal_recovery_retains_new_leases_until_confirmation(rig):
    client, runner = terminal_run(rig)
    path = f"/api/runs/{runner.run_id}/recovery"
    assert post(client, path).status_code == 202
    wait_for(lambda: rig.recovery is not None and rig.recovery.phase == "awaiting-clearance")
    recovery = rig.recovery
    old, fresh = rig.created[-2]["stage"], rig.created[-1]["stage"]
    assert old.identity != fresh.identity and old.closed and not fresh.closed
    assert fresh.holder == fresh.identity
    assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
    assert_error(
        post(client, path + "/confirm", {"physically_clear": False, "note": "Checked"}), 400
    )
    assert (
        post(
            client, path + "/confirm", {"physically_clear": True, "note": "Verified departure"}
        ).status_code
        == 202
    )
    assert recovery.closed.wait(4)
    assert recovery.error is None and fresh.closed
    assert runner.snapshot()["zones"] == {} and runner.status == "aborted"
    assert "recovery-stop" in fresh.calls
    assert fresh.calls[-2:] == ["release", "close"]


@pytest.mark.parametrize("loss", ["stop", "release", "expiry", "abort"])
def test_recovery_cancellation_retains_claim_and_drains_accepted_confirmation(rig, clock, loss):
    client, runner = terminal_run(rig)
    path = f"/api/runs/{runner.run_id}/recovery"
    assert post(client, path).status_code == 202
    wait_for(lambda: rig.recovery is not None and rig.recovery.phase == "awaiting-clearance")
    recovery = rig.recovery
    fresh = rig.created[-1]["stage"]
    entered, proceed = threading.Event(), threading.Event()
    original = fresh.recovery_stop

    def delayed(instance):
        entered.set()
        assert proceed.wait(3)
        return original(instance)

    fresh.recovery_stop = delayed
    try:
        assert (
            post(
                client, path + "/confirm", {"physically_clear": True, "note": "Verified"}
            ).status_code
            == 202
        )
        assert entered.wait(2)
        if loss == "expiry":
            clock.advance(4)
            rig.poll_once()
        else:
            endpoint = {"stop": "/api/stop", "release": "/api/release", "abort": path + "/abort"}[
                loss
            ]
            assert post(client, endpoint).status_code in {200, 202}
        assert recovery.cancelled.is_set()
        assert not recovery.closed.is_set() and not fresh.closed
        proceed.set()
        assert recovery.closed.wait(4)
        assert runner.snapshot()["zones"]["loading"]["holder"] == runner.run_id
        assert fresh.closed and runner.status == "aborted"
    finally:
        proceed.set()


def test_recovery_cleanup_does_not_mistake_empty_error_map_for_pending():
    device = Instrument("stage", "fresh-recovery")
    pool = Clients({"stage": device})
    recovery = Recovery("run", pool, ("stage",), lambda *args: None)
    recovery.begin()
    wait_for(lambda: recovery.phase == "awaiting-clearance")
    recovery.cancel()
    assert recovery.closed.wait(3) and recovery.release_errors == {}
    assert device.closed


def test_demo_environment_drops_hostile_settings_even_with_spoofed_marker():
    hostile = {
        "MG400_ORCH_ROBOT_URL": "http://lab.invalid",
        "MG400_ORCH_ROBOT_TOKEN": "secret",
        "HTTP_PROXY": "http://proxy.invalid",
        "PYTHONPATH": "/untrusted",
        "MG400_ORCH_RUNS_DIR": "/var/lib/mg400/runs",
        "MG400_ORCH_ZONE_STATE_FILE": "/host/state",
        DEMO_MARKER: "1",
        "MG400_ORCH_RECIPES_DIR": "/catalog",
        "MG400_ORCH_STATIONS_FILE": "/read-only/stations.toml",
    }
    assert not demo_isolated(hostile)
    clean = demo_environment(hostile)
    assert demo_isolated(clean)
    assert set(clean) == {DEMO_MARKER, "MG400_ORCH_RECIPES_DIR", "MG400_ORCH_STATIONS_FILE"}


def test_real_demo_requires_isolated_environment_before_creating_files(tmp_path):
    service = OrchestratorService(demo=True, runs_dir=tmp_path / "unused")
    with pytest.raises(ValueError, match="isolated"):
        service.start()
    assert service.temporary is None and not list(tmp_path.iterdir())
    service.close()


@pytest.mark.parametrize("loss", ["stop", "release"])
def test_startup_revocation_keeps_admission_and_prevents_acquisition(rig, loss):
    entered, proceed = threading.Event(), threading.Event()
    original = rig.runner_factory

    def blocked(*args, **kwargs):
        runner = original(*args, **kwargs)
        entered.set()
        assert proceed.wait(3)
        return runner

    rig.runner_factory = blocked
    recipe(rig, fail=True)
    client = create_app(rig).test_client()
    assert post(client, "/api/heartbeat").status_code == 200
    try:
        assert post(client, "/api/runs", {"recipe": "recipe"}).status_code == 202
        assert entered.wait(2)
        assert post(client, "/api/" + loss).status_code == 200
        if loss == "release":
            assert post(client, "/api/heartbeat").status_code == 200
        assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
        proceed.set()
        wait_for(lambda: rig.job["done"].is_set())
        runner = rig.current
        assert runner.wait_closed(3) and runner.status == "held"
        assert all("acquire" not in device.calls for device in rig.created[0].values())
    finally:
        proceed.set()


def test_release_fences_before_same_identity_can_reacquire(rig, monkeypatch):
    client, runner = start_run(rig, seconds=5)
    entered, proceed, reacquired = (threading.Event() for _ in range(3))
    original = runner.hold

    def held(reason):
        entered.set()
        assert proceed.wait(3)
        return original(reason)

    monkeypatch.setattr(runner, "hold", held)
    release = threading.Thread(target=lambda: rig.lease.release("operator"))
    acquire = threading.Thread(target=lambda: (rig.lease.acquire("operator"), reacquired.set()))
    try:
        release.start()
        assert entered.wait(2)
        acquire.start()
        assert not reacquired.wait(0.05)
        proceed.set()
        release.join(2)
        acquire.join(2)
        assert reacquired.is_set()
        wait_for(lambda: runner.snapshot()["settled"])
        assert runner.status == "held"
    finally:
        proceed.set()
        release.join(2)
        if acquire.ident:
            acquire.join(2)


def test_shared_command_snapshot_does_not_read_zone_files(rig, monkeypatch):
    client, runner = start_run(rig, seconds=5)

    def forbidden():
        raise AssertionError("Snapshot inspected storage under contract lease dispatch")

    monkeypatch.setattr(runner, "snapshot", forbidden)
    assert post(client, "/api/command", {"action": "hold"}).status_code == 202


def test_completion_publication_keeps_admission_and_uses_its_own_record(rig, monkeypatch):
    recipe(rig)
    entered, proceed = threading.Event(), threading.Event()
    messages = []

    def publish(level, message, **fields):
        messages.append((message, fields["command_id"]))
        entered.set()
        assert proceed.wait(3)

    monkeypatch.setattr(rig.events, "add", publish)
    client = create_app(rig).test_client()
    assert post(client, "/api/heartbeat").status_code == 200
    try:
        response = post(client, "/api/runs", {"recipe": "recipe"})
        command_id = response.get_json()["command_id"]
        assert entered.wait(2)
        assert not rig.job["done"].is_set()
        assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
        assert post(client, "/api/command", {"action": "hold"}).status_code == 202
        proceed.set()
        wait_for(lambda: rig.job["done"].is_set())
        assert messages == [("Completed.", command_id)]
        assert rig.last_command["id"] == command_id
    finally:
        proceed.set()


def test_publication_failure_locks_new_authority_but_abort_and_stop_work(rig, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Synthetic journal failure")

    monkeypatch.setattr(rig.events, "add", fail)
    client, runner = start_run(rig, seconds=5)
    assert rig.fault and rig.last_command["result"] == "failed"
    assert_error(post(client, "/api/runs", {"recipe": "recipe"}), 409)
    assert_error(
        post(
            client,
            f"/api/runs/{runner.run_id}/resume",
            {"manually_completed": True, "note": "Checked"},
        ),
        409,
    )
    assert post(client, "/api/stop", actor="stranger").status_code == 200
    assert post(client, f"/api/runs/{runner.run_id}/abort").status_code == 202
    assert runner.wait_closed(3)


def test_failed_recovery_confirmation_completes_accepted_command_id(rig, monkeypatch):
    client, runner = terminal_run(rig)
    path = f"/api/runs/{runner.run_id}/recovery"
    assert post(client, path).status_code == 202
    wait_for(lambda: rig.job["done"].is_set())
    entered, proceed = threading.Event(), threading.Event()

    def rejected(*args, **kwargs):
        entered.set()
        assert proceed.wait(3)
        raise RuntimeError("Still needs manual recovery")

    monkeypatch.setattr(runner, "recover_terminal", rejected)
    try:
        reply = post(client, path + "/confirm", {"physically_clear": True, "note": "Checked"})
        command_id = reply.get_json()["command_id"]
        assert entered.wait(2)
        assert rig.last_command["id"] != command_id
        assert not rig.job["done"].is_set()
        assert post(client, "/api/renew").status_code == 200
        proceed.set()
        wait_for(lambda: rig.job["done"].is_set())
        assert rig.last_command["id"] == command_id
        assert rig.last_command["result"] == "failed"
        assert "manual recovery" in rig.last_command["message"]
        assert rig.recovery.phase == "awaiting-clearance"
        assert runner.snapshot()["zones"]
    finally:
        proceed.set()


def test_recovery_cleanup_start_failure_retains_cancel_fence_and_retries_cleanup(monkeypatch):
    device = Instrument("stage", "new-transaction")
    recovery = Recovery("run", Clients({"stage": device}), ("stage",), lambda *args: None)
    recovery.begin()
    wait_for(lambda: recovery.phase == "awaiting-clearance")
    original = recovery._thread
    attempts = []

    def fail_once(function, name):
        if name == "orch-recovery-cleanup" and not attempts:
            attempts.append(name)
            raise RuntimeError("Synthetic thread exhaustion")
        return original(function, name)

    monkeypatch.setattr(recovery, "_thread", fail_once)
    recovery.cancel()
    assert recovery.cancelled.is_set() and not recovery.closed.is_set()
    assert recovery.error and not device.closed
    recovery.cancel()
    assert recovery.closed.wait(3) and device.closed
    assert device.calls.count("acquire") == 1


def test_any_client_stop_reports_unconfirmed_recovery_outcome(rig):
    client, runner = terminal_run(rig)
    path = f"/api/runs/{runner.run_id}/recovery"
    assert post(client, path).status_code == 202
    wait_for(lambda: rig.job["done"].is_set())
    recovery = rig.recovery
    device = rig.created[-1]["stage"]
    device.stop = lambda: {"stop": "unconfirmed"}
    assert post(client, path + "/abort").status_code == 202
    assert recovery.closed.wait(3)
    response = post(client, "/api/stop", actor="stranger", data={"wait_s": 0})
    assert response.get_json()["stop"] == "unconfirmed"


class CLIService:
    def __init__(self, calls):
        from labkit.lease import Lease

        self.calls, self.lease = calls, Lease()
        self.on_lease_expired = lambda: None

    def start(self):
        self.calls.append("start")

    def close(self):
        self.calls.append("service-close")


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_serve_signal_drains_before_closing_server(signal_name):
    import signal

    from orchestrator.__main__ import main

    calls, arguments = [], []
    previous = signal.getsignal(getattr(signal, signal_name))

    def factory(**kwargs):
        arguments.append(kwargs)
        return CLIService(calls)

    class Server:
        def run(self):
            calls.append("run")
            signal.getsignal(getattr(signal, signal_name))(None, None)

        def close(self):
            calls.append("server-close")

    def server(app, **kwargs):
        calls.append("bind")
        assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8772
        assert kwargs["clear_untrusted_proxy_headers"] is False
        return Server()

    assert (
        main(
            ["serve", "--recipes-dir", "/explicit/catalog"],
            environ={},
            service_factory=factory,
            server_factory=server,
        )
        == 0
    )
    assert calls == ["bind", "start", "run", "service-close", "server-close"]
    assert arguments[0]["recipes_dir"] == "/explicit/catalog"
    assert signal.getsignal(getattr(signal, signal_name)) == previous


@pytest.mark.parametrize("phase,code", [("bind", 2), ("start", 2), ("run", 1)])
def test_serve_failures_use_correct_exit_and_cleanup(phase, code):
    from orchestrator.__main__ import serve

    calls = []

    class Service(CLIService):
        def start(self):
            super().start()
            if phase == "start":
                raise ValueError("Synthetic configuration failure")

    class Server:
        def run(self):
            calls.append("run")
            raise OSError("Synthetic serving failure")

        def close(self):
            calls.append("server-close")

    def server(app, **kwargs):
        calls.append("bind")
        if phase == "bind":
            raise OSError("Synthetic bind failure")
        return Server()

    with pytest.raises(SystemExit) as error:
        serve(
            [], environ={}, service_factory=lambda **kwargs: Service(calls), server_factory=server
        )
    assert error.value.code == code
    assert calls[-1] == ("service-close" if phase == "bind" else "server-close")
    if phase != "bind":
        assert calls[-2:] == ["service-close", "server-close"]


@pytest.mark.parametrize(
    "args,env",
    [
        (["--port", "0"], {}),
        (["--bind", " 127.0.0.1"], {}),
        (["--demo", "--runs-dir", "/host"], {}),
    ],
)
def test_serve_invalid_configuration_never_constructs_service(args, env):
    from orchestrator.__main__ import serve

    def forbidden(**kwargs):
        raise AssertionError("Service constructed before argument validation")

    with pytest.raises(SystemExit) as error:
        serve(args, environ=env, service_factory=forbidden)
    assert error.value.code == 2


def test_demo_serve_reexecs_clean_interpreter_before_service_construction():
    from orchestrator.__main__ import serve

    class Replaced(Exception):
        pass

    recorded = []

    def execute(executable, argv, environ):
        recorded.append((executable, argv, environ))
        raise Replaced

    def forbidden(**kwargs):
        raise AssertionError("Service constructed before isolated process")

    hostile = {
        DEMO_MARKER: "1",
        "MG400_ORCH_ROBOT_URL": "http://lab.invalid",
        "HTTPS_PROXY": "http://proxy.invalid",
        "MG400_ORCH_PORT": "8772",
        "PYTHONPATH": "/untrusted",
        "MG400_ORCH_RUNS_DIR": "/host/runs",
    }
    with pytest.raises(Replaced):
        serve(
            ["--demo", "--recipes-dir", "/catalog"],
            environ=hostile,
            service_factory=forbidden,
            exec_function=execute,
        )
    executable, argv, env = recorded[0]
    assert executable == sys.executable and argv[:5] == [
        sys.executable,
        "-I",
        "-m",
        "orchestrator",
        "serve",
    ]
    assert demo_isolated(env) and env["MG400_ORCH_PORT"] == "8772"
    assert "MG400_ORCH_ROBOT_URL" not in env and "MG400_ORCH_RUNS_DIR" not in env


def test_main_preserves_installed_terminal_dispatch(monkeypatch):
    from orchestrator import cli
    from orchestrator.__main__ import main

    calls = []
    out, read = object(), object()
    monkeypatch.setattr(cli, "main", lambda args, **kwargs: calls.append((args, kwargs)) or 7)
    assert main(["runs"], out=out, read=read) == 7
    assert calls == [(["runs"], {"out": out, "read": read})]


@pytest.mark.parametrize("bind_fails", [False, True])
def test_serve_runs_the_control_socket_beside_the_server(bind_fails):
    from orchestrator.__main__ import SOCKET_PORT, serve

    calls = []

    class Server:
        def run(self):
            calls.append("run")

        def close(self):
            calls.append("server-close")

    class Socket:
        def __init__(self, app, **kwargs):
            if bind_fails:
                raise OSError("Synthetic port in use")
            calls.append(("socket", kwargs))

        def start(self):
            calls.append("socket-start")

        def close(self):
            calls.append("socket-close")

    assert (
        serve(
            ["--base-path", "/lab/"],
            environ={},
            service_factory=lambda **kwargs: CLIService(calls),
            server_factory=lambda app, **kwargs: Server(),
            socket_factory=Socket,
        )
        == 0
    )
    if bind_fails:
        # Without the socket, the console still serves every request over HTTP.
        expected = ["start", "run"]
    else:
        socket = ("socket", {"host": "127.0.0.1", "port": SOCKET_PORT, "base_path": "/lab/"})
        expected = [socket, "start", "socket-start", "run", "socket-close"]
    assert calls == expected + ["service-close", "server-close"]


def test_serve_passes_the_server_options():
    from orchestrator.__main__ import SERVER_OPTIONS, main

    seen = {}

    class Server:
        def run(self):
            raise KeyboardInterrupt

        def close(self):
            pass

    def server(app, **kwargs):
        seen.update(kwargs)
        return Server()

    settings = {}

    def factory(**kwargs):
        settings.update(kwargs["settings"])
        return CLIService([])

    main(
        ["serve"],
        environ={"MG400_ORCH_STAGE_URL": "http://127.0.0.1:9000/", "MG400_ORCH_ROBOT_URL": ""},
        service_factory=factory,
        server_factory=server,
    )
    assert {key: seen[key] for key in SERVER_OPTIONS} == SERVER_OPTIONS
    # Fixed loopback service URLs apply unless a nonempty setting overrides one.
    assert settings["MG400_ORCH_ROBOT_URL"] == "http://127.0.0.1:8766/"
    assert settings["MG400_ORCH_STAGE_URL"] == "http://127.0.0.1:9000/"


def test_borrowed_runs_use_lent_adapters_and_return_them(rig, monkeypatch):
    recipe(rig, fail=True)
    client = create_app(rig).test_client()
    assert post(client, "/api/heartbeat").status_code == 200
    assert_error(post(client, "/api/runs", {"recipe": "recipe", "borrow": 1}), 400)
    # The client's console page holds no stage.
    assert_error(post(client, "/api/runs", {"recipe": "recipe", "borrow": True}), 409)
    held = Instrument("stage", "agent-x-0123456789ab")
    held.acquire()
    returned = []
    monkeypatch.setattr(
        rig.console,
        "lend",
        lambda page, devices: {
            name: Borrowed(held, lambda name=name: returned.append(name)) for name in devices
        },
    )
    reply = post(client, "/api/runs", {"recipe": "recipe", "borrow": True})
    assert reply.status_code == 202, reply.get_json()
    wait_for(lambda: rig.current is not None and rig.job["done"].is_set())
    runner = rig.current
    wait_for(lambda: runner.snapshot()["status"] == "held")
    assert post(client, f"/api/runs/{runner.run_id}/abort").status_code == 202
    assert runner.wait_closed(3)
    assert returned == ["stage"] and not rig.created
    assert held.holder == "agent-x-0123456789ab" and "release" not in held.calls
