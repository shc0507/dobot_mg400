"""Run complete recipes against owned loopback services, never lab devices."""

import ipaddress
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from orchestrator.clients import RobotAdapter, ServiceAdapter
from orchestrator.recipe import load_recipe, validate
from orchestrator.rehearsal import Rehearsal
from orchestrator.runner import Runner
from orchestrator.skills import default_registry
from orchestrator.store import RunStore

RECIPES = sorted((Path(__file__).resolve().parents[1] / "recipes").glob("*.toml"))


@pytest.fixture(autouse=True)
def numeric_loopback_only(monkeypatch):
    import serial
    import usb.core

    original = socket.socket.connect
    connected = []

    def connect(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback
        connected.append(address)
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

    def no_device(*args, **kwargs):
        pytest.fail("Rehearsal attempted real device access")

    monkeypatch.setattr(serial, "Serial", no_device)
    monkeypatch.setattr(usb.core, "find", no_device)
    return connected


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Owned rehearsal did not reach expected state")
        time.sleep(0.01)


@pytest.mark.parametrize("path", RECIPES, ids=lambda path: path.name)
def test_every_recipe_rehearses_without_hardware(tmp_path, path):
    assert RECIPES
    registry = default_registry()
    recipe = load_recipe(path, registry)
    identity = "orch-" + uuid.uuid4().hex
    with (
        RunStore(tmp_path / "runs") as store,
        Rehearsal(tmp_path / "demos", recipe.services, identity=identity) as demo,
    ):
        runner = Runner(recipe, registry, demo.clients, store, time_scale=0.01).start()
        try:
            assert runner.wait(15) == "completed", runner.snapshot()
            row = store.get(runner.run_id)
            assert all(step["status"] == "completed" for step in row["steps"])
            if path.name == "example-rehearsal.toml":
                assert row["samples"][0]["chip_id"] == recipe.params["chip_id"]
                assert demo.simulator.controller.pose == (230, 0, 20, 0)
        finally:
            runner.abort()
            runner._thread.join(5)
            assert not runner._thread.is_alive()


def test_failing_step_holds_owned_robot_and_resumes_after_explicit_manual_recovery(tmp_path):
    registry = default_registry()
    calls = []

    def fail_with_motion(ctx):
        # Arm keyboard control through the public robot service, then fail
        # before the next recipe step. No direct controller writes.
        robot = ctx.clients["robot"]
        robot.command("keyboard", enabled=True, speed=1, seq=1)
        calls.append("failed-step")
        raise RuntimeError("synthetic failure while robot control is armed")

    def forbidden(ctx):
        calls.append("later-step")

    registry.register("test.fail", fail_with_motion, services=("robot",))
    registry.register("test.later", forbidden, services=("robot",))
    recipe = validate(
        {
            "name": "Failure rehearsal",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": name, "params": {}, "timeout_s": 5}
                for name in ("robot.enable", "test.fail", "test.later")
            ],
        },
        registry,
    )
    with (
        RunStore(tmp_path / "runs") as store,
        Rehearsal(tmp_path / "demos", recipe.services, identity="orch-failure-demo") as demo,
    ):
        runner = Runner(recipe, registry, demo.clients, store).start()
        try:
            assert runner.wait(10) == "held", runner.snapshot()
            wait_for(lambda: runner._settled)
            assert calls == ["failed-step"]
            assert runner.snapshot()["stops"]["robot"]["stop"] == "confirmed"
            state = demo.robot_state.snapshot()
            assert state["busy"] is False and state["keyboard_enabled"] is False
            assert state["controller_id"] == "orch-failure-demo"
            assert "ResetRobot()" in demo.simulator.controller.sent()
            assert store.get(runner.run_id)["steps"][2]["status"] == "pending"
            assert "fault" not in state and "connected" not in state
            assert state["locked"] is True
            with pytest.raises(RuntimeError, match="manual recovery"):
                runner.clients.reconcile()
            # An explicit operator-equivalent recovery on this owned simulator
            # confirms disable/rest. Reconciliation never clears the lock itself.
            demo.clients["robot"].run("disable", timeout=3)
            reconciled = runner.clients.reconcile()["robot"]
            assert reconciled["instance"] == state["instance"]
            assert reconciled["robot_error"] is False
            assert reconciled["stop_unconfirmed"] is False
            assert reconciled["locked"] is False and reconciled["busy"] is False
            assert calls == ["failed-step"]
            runner.resume(manually_completed=True, note="Disabled and completed the step manually.")
            wait_for(lambda: runner.status == "completed")
            assert calls == ["failed-step", "later-step"]
            assert store.get(runner.run_id)["steps"][1]["status"] == "manually_completed"
            stops = [
                event["data_json"]
                for event in store.get(runner.run_id)["events"]
                if event["kind"] == "stop"
            ]
            assert any("verified-disabled-rest" in event for event in stops)
        finally:
            runner.abort()
            runner._thread.join(5)
            assert not runner._thread.is_alive()


def test_fresh_clients_use_same_owned_endpoints_without_acquiring(
    tmp_path, monkeypatch, numeric_loopback_only
):
    endpoints = {}
    original = ServiceAdapter.__init__

    def construct(self, url, token, identity, **kwargs):
        endpoints.setdefault(identity, []).append(url)
        assert token == ""
        return original(self, url, token, identity, **kwargs)

    monkeypatch.setattr(ServiceAdapter, "__init__", construct)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://production.invalid:9")
    for name in ("ROBOT", "STAGE", "THERMAL"):
        monkeypatch.setenv(f"MG400_ORCH_{name}_URL", "http://production.invalid:9")
        monkeypatch.setenv(f"MG400_ORCH_{name}_TOKEN", "production-secret-must-not-be-used")
    with Rehearsal(tmp_path, ("robot", "stage", "thermal"), identity="initial") as demo:
        simulator = demo.simulator
        before = len(numeric_loopback_only)
        fresh = demo.new_clients("fresh-transaction")
        assert len(numeric_loopback_only) == before
        assert set(fresh) == set(demo.clients) == {"robot", "stage", "thermal"}
        assert endpoints["initial"] == endpoints["fresh-transaction"]
        assert demo.simulator is simulator
        for name, client in fresh.items():
            assert client is not demo.clients[name]
            assert (
                client.identity == "fresh-transaction" and demo.clients[name].identity == "initial"
            )
            assert client.describe()["demo"] is True
            assert not client.owned(client.state())
            client.acquire()
            assert client.owned(client.state())
            assert client.stop()["stop"] == "confirmed"
            client.release()
            client.close()
        with pytest.raises(ValueError, match="cannot be reused"):
            demo.new_clients("fresh-transaction")
    for client in fresh.values():
        for method in (client.acquire, client.state, client.stop, client.release):
            with pytest.raises(RuntimeError, match="no longer active"):
                method()
        client.close()


@pytest.mark.parametrize("identity", ["", "\n", "x" * 129, None, 1])
def test_fresh_client_identity_is_validated_before_any_startup(tmp_path, identity):
    demo = Rehearsal(tmp_path, (), identity="initial")
    with pytest.raises(ValueError):
        demo.new_clients(identity)


def test_fresh_factory_requires_active_context_and_rejects_initial_identity(tmp_path):
    demo = Rehearsal(tmp_path, (), identity="initial")
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("before")
    with demo:
        with pytest.raises(ValueError, match="cannot be reused"):
            demo.new_clients("initial")
        assert demo.new_clients("valid") == {}
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("after")
    with pytest.raises(RuntimeError, match="only once"):
        demo.__enter__()


def test_fresh_factory_is_unavailable_after_startup_failure(tmp_path, monkeypatch):
    def failure(self):
        raise RuntimeError("Synthetic startup failure")

    monkeypatch.setattr(Rehearsal, "_stage", failure)
    demo = Rehearsal(tmp_path, ("stage",), identity="initial")
    with pytest.raises(RuntimeError, match="startup failure"):
        demo.__enter__()
    with pytest.raises(RuntimeError, match="active rehearsal"):
        demo.new_clients("after-failure")


def test_partial_fresh_factory_failure_closes_only_its_new_adapters(tmp_path, monkeypatch):
    closed = []
    original_init, original_close = ServiceAdapter.__init__, RobotAdapter.close

    def construct(self, url, token, identity, **kwargs):
        if identity == "broken" and url.endswith("/contract"):
            raise ValueError("Synthetic adapter failure")
        return original_init(self, url, token, identity, **kwargs)

    def close(self):
        closed.append(self.identity)
        return original_close(self)

    monkeypatch.setattr(ServiceAdapter, "__init__", construct)
    monkeypatch.setattr(RobotAdapter, "close", close)
    with Rehearsal(tmp_path, ("robot", "stage"), identity="initial") as demo:
        with pytest.raises(ValueError, match="adapter failure"):
            demo.new_clients("broken")
        assert closed == ["broken"]
        assert demo.clients["robot"].describe()["demo"]
        assert set(demo.new_clients("next")) == {"robot", "stage"}


def test_context_closes_fresh_robot_workers_without_reusing_old_identity(tmp_path):
    with Rehearsal(tmp_path, ("robot",), identity="initial") as demo:
        old = demo.clients["robot"]
        old.acquire()
        instance = old.state()["instance"]
        old.stop()
        old.release()
        old.close()
        fresh = demo.new_clients("recovery")["robot"]
        previous_workers = set(threading.enumerate())
        fresh.acquire()
        assert fresh.state()["instance"] == instance
        assert fresh.owned(fresh.state())
        # Closing the original identity again cannot release the fresh owner.
        old.close()
        assert fresh.owned(fresh.state())
        workers = [
            worker
            for worker in threading.enumerate()
            if worker not in previous_workers and worker.name.startswith("RobotClient-")
        ]
        assert len(workers) == 2 and all(worker.is_alive() for worker in workers)
    assert all(not worker.is_alive() for worker in workers)
    with pytest.raises(RuntimeError, match="no longer active"):
        fresh.acquire()


def test_fresh_client_io_does_not_hold_factory_lock_and_exit_drains_registered_calls(
    tmp_path, monkeypatch
):
    entered, finish, stopped = threading.Event(), threading.Event(), threading.Event()
    original = ServiceAdapter.state

    def state(self):
        if self.identity == "blocked":
            entered.set()
            finish.wait()
        return original(self)

    monkeypatch.setattr(ServiceAdapter, "state", state)
    demo = Rehearsal(tmp_path, ("stage",), identity="initial")
    demo.__enter__()
    first = demo.new_clients("blocked")["stage"]
    errors = []

    def read():
        try:
            first.state()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=read)
    worker.start()
    closer = None
    try:
        assert entered.wait(1)
        second = demo.new_clients("independent")["stage"]
        assert second.stop()["stop"] == "confirmed"

        def close():
            demo.__exit__(None, None, None)
            stopped.set()

        closer = threading.Thread(target=close)
        closer.start()
        wait_for(lambda: not demo._active)
        assert not stopped.is_set()
        with pytest.raises(RuntimeError, match="no longer active"):
            second.acquire()
        finish.set()
        worker.join(2)
        closer.join(2)
        assert stopped.is_set() and errors == []
    finally:
        finish.set()
        worker.join(2)
        if closer is not None:
            closer.join(2)
        else:
            demo.__exit__(None, None, None)
