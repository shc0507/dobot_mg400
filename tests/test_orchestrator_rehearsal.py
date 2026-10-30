"""Run complete recipes against owned loopback services, never lab devices."""

import ipaddress
import socket
import time
import uuid
from pathlib import Path

import pytest

from orchestrator.recipe import load_recipe, validate
from orchestrator.rehearsal import Rehearsal
from orchestrator.runner import Runner
from orchestrator.skills import default_registry
from orchestrator.store import RunStore

RECIPES = sorted((Path(__file__).resolve().parents[1] / "recipes").glob("*.toml"))


@pytest.fixture(autouse=True)
def numeric_loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


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


def test_failing_step_holds_run_and_owned_robot_reconciles_after_manual_recovery(tmp_path):
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
        finally:
            runner.abort()
            runner._thread.join(5)
            assert not runner._thread.is_alive()
