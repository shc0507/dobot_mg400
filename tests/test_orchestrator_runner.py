"""Fail-closed coordination with fake services and real private SQLite records."""

import copy
import json
import threading
import time
from contextlib import contextmanager

import pytest

from orchestrator.clients import Clients
from orchestrator.recipe import validate
from orchestrator.runner import Revoked, Runner
from orchestrator.skills import Registry
from orchestrator.store import RunStore


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for offline coordination")
        time.sleep(0.005)


class FakeService:
    timeout = 0.02

    def __init__(self, name="robot"):
        self.identity = "orch-test-unique"
        self.name = name
        self.events = []
        self.value = {
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "holder": self.identity,
        }
        self.stop_value = {"stop": "confirmed"}

    def describe(self):
        self.events.append("describe")
        return {"contract_version": 1}

    def acquire(self):
        self.events.append("acquire")

    def heartbeat(self):
        self.events.append("renew")

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("holder") == self.identity

    def healthy(self, state):
        return state.get("connected") is True

    def command(self, action, **params):
        self.events.append(action)
        return {"ok": True}

    def run(self, action, *, timeout, **params):
        return self.command(action, **params)

    def stop(self):
        self.events.append("stop")
        return self.stop_value.copy()

    def release(self):
        self.events.append("release")

    def close(self):
        self.events.append("close")


def prepared(registry, names, *, timeout=1, on_fail="hold"):
    return validate(
        {
            "name": "offline safety",
            "version": 1,
            "params": {"chip_id": {"type": "string", "default": "chip-001"}},
            "steps": [
                {"skill": name, "params": {}, "timeout_s": timeout, "on_fail": on_fail}
                for name in names
            ],
        },
        registry,
    )


@contextmanager
def running(tmp_path, registry, names, services, *, gates=(), **kwargs):
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(prepared(registry, names, **kwargs), registry, services, store)
        runner.start()
        try:
            yield runner, store
        finally:
            runner.abort()
            for gate in gates:
                gate.set()
            runner._thread.join(6)
            assert not runner._thread.is_alive(), runner.snapshot()


def settled(runner):
    until(lambda: runner.status == "held" and runner._settled)


def test_success_is_sequential_acquires_required_only_and_keeps_chip_identity(tmp_path):
    registry, service, unrelated = Registry(), FakeService(), FakeService("stage")

    def first(ctx):
        ctx.clients["robot"].command("first")
        ctx.store.event("measurement", {"value": 1})
        ctx.store.artifact("result.txt", b"result")

    def second(ctx):
        ctx.clients["robot"].command("second")

    registry.register("first", first, services=("robot",))
    registry.register("second", second, services=("robot",))
    with running(
        tmp_path, registry, ["first", "second"], {"robot": service, "stage": unrelated}
    ) as (
        runner,
        store,
    ):
        assert runner.wait() == "completed"
        assert unrelated.events == []
        assert service.events.count("acquire") == 1
        assert service.events.index("first") < service.events.index("second")
        assert service.events.index("second") < service.events.index("release")
        row = store.get(runner.run_id)
        assert [step["status"] for step in row["steps"]] == ["completed", "completed"]
        assert row["artifacts"][0]["chip_id"] == "chip-001"
        event = next(event for event in row["events"] if event["kind"] == "measurement")
        assert event["chip_id"] == "chip-001"


def test_resume_requires_manual_completion_and_fresh_stop_after_repair(tmp_path):
    registry, service = Registry(), FakeService()
    service.stop_value = {"stop": "unconfirmed"}
    calls = []

    def failed(ctx):
        calls.append("failed")
        raise RuntimeError("manual repair required")

    def after(ctx):
        calls.append("after")

    registry.register("failed", failed, services=("robot",))
    registry.register("after", after, services=("robot",))
    with running(tmp_path, registry, ["failed", "after"], {"robot": service}) as (runner, store):
        settled(runner)
        assert calls == ["failed"] and "release" not in service.events
        with pytest.raises(ValueError):
            runner.resume(note="I inspected it")
        with pytest.raises(ValueError):
            runner.resume(manually_completed=True, note=" ")
        with pytest.raises(RuntimeError, match="confirmed stop"):
            runner.resume(manually_completed=True, note="manual repair completed")
        before = service.events.count("stop")
        service.stop_value = {"stop": "confirmed"}
        runner.resume(manually_completed=True, note="failed operation completed manually")
        until(lambda: runner.status == "completed")
        assert service.events.count("stop") > before
        assert calls == ["failed", "after"]
        row = store.get(runner.run_id)
        assert row["steps"][0]["status"] == "manually_completed"
        resolution = next(e for e in row["events"] if e["kind"] == "manual-resolution")
        assert json.loads(resolution["data_json"])["note"] == "failed operation completed manually"


@pytest.mark.parametrize(
    "change",
    [
        lambda state: state.pop("busy"),
        lambda state: state.update(busy=None),
        lambda state: state.update(locked=0),
        lambda state: state.pop("fault"),
        lambda state: state.update(fault="unknown outcome"),
        lambda state: state.update(holder="other"),
        lambda state: state.update(instance="replacement"),
        lambda state: state.update(stop_unconfirmed=True),
    ],
)
def test_resume_refuses_missing_or_unsafe_state(tmp_path, change):
    registry, service = Registry(), FakeService()

    def failed(ctx):
        raise RuntimeError("failed")

    registry.register("failed", failed, services=("robot",))
    with running(tmp_path, registry, ["failed"], {"robot": service}) as (runner, store):
        settled(runner)
        change(service.value)
        with pytest.raises(RuntimeError, match="manual recovery"):
            runner.resume(manually_completed=True, note="manual completion declared")
        assert runner.status == "held" and "release" not in service.events


def test_timeout_revokes_late_client_and_store_calls_before_release(tmp_path):
    registry, service = Registry(), FakeService()
    entered, finish = threading.Event(), threading.Event()
    rejected = []

    def blocked(ctx):
        entered.set()
        finish.wait()
        for operation in (
            lambda: ctx.clients["robot"].command("late-motion"),
            lambda: ctx.store.event("late-event", {}),
            lambda: ctx.store.artifact("late.txt", b"late"),
            lambda: ctx.store.sample("chip-001", {}),
        ):
            try:
                operation()
            except Revoked:
                rejected.append(True)

    registry.register("blocked", blocked, services=("robot",))
    with running(
        tmp_path, registry, ["blocked"], {"robot": service}, timeout=0.04, gates=(finish,)
    ) as (runner, store):
        assert entered.wait(1)
        assert runner.wait() == "held"
        runner.abort()
        assert runner.status not in {"completed", "aborted"}
        assert "release" not in service.events
        finish.set()
        until(lambda: runner.status == "aborted")
        assert len(rejected) == 4 and "late-motion" not in service.events
        assert store.get(runner.run_id)["artifacts"] == []
        assert service.events[-2:] == ["release", "close"]


@pytest.mark.parametrize("skill_returns", [False, True])
def test_inflight_helper_drains_and_is_stopped_before_any_next_step(tmp_path, skill_returns):
    registry, service = Registry(), FakeService()
    entered, finish = threading.Event(), threading.Event()
    helpers = []

    def slow_command(action, **params):
        entered.set()
        finish.wait()
        service.events.append("late-write")

    service.command = slow_command

    def helper(ctx):
        try:
            ctx.clients["robot"].command("write")
        except Revoked:
            pass

    def first(ctx):
        if skill_returns:
            worker = threading.Thread(target=helper, args=(ctx,))
            helpers.append(worker)
            worker.start()
            assert entered.wait(1)
        else:
            helper(ctx)

    def next_step(ctx):
        service.events.append("next-step")

    registry.register("first", first, services=("robot",))
    registry.register("next", next_step, services=("robot",))
    with running(
        tmp_path, registry, ["first", "next"], {"robot": service}, timeout=0.05, gates=(finish,)
    ) as (runner, store):
        assert entered.wait(1)
        assert runner.wait() == "held"
        with pytest.raises(RuntimeError, match="finish before recovery"):
            runner.resume(manually_completed=True, note="not yet safe")
        assert "next-step" not in service.events and "release" not in service.events
        finish.set()
        for worker in helpers:
            worker.join(1)
        settled(runner)
        assert service.events.index("late-write") < len(service.events) - 1
        assert service.events[-1] == "stop"
        runner.resume(manually_completed=True, note="late operation inspected and completed")
        until(lambda: runner.status == "completed")
        assert service.events.index("late-write") < service.events.index("next-step")


def test_hung_stop_does_not_block_another_services_final_stop(tmp_path):
    registry, a, b = Registry(), FakeService("a"), FakeService("b")
    finish_stop, finish_write, entered = threading.Event(), threading.Event(), threading.Event()

    def stop_a():
        a.events.append("stop")
        finish_stop.wait()
        return {"stop": "confirmed"}

    def late_write(action, **params):
        entered.set()
        finish_write.wait()
        b.events.append("late-write")

    a.stop, b.command = stop_a, late_write

    def blocked(ctx):
        ctx.clients["b"].command("write")

    registry.register("blocked", blocked, services=("a", "b"))
    with running(
        tmp_path,
        registry,
        ["blocked"],
        {"a": a, "b": b},
        timeout=0.04,
        gates=(finish_stop, finish_write),
    ) as (runner, store):
        assert entered.wait(1)
        assert runner.wait() == "held"
        finish_write.set()
        until(lambda: "late-write" in b.events and b.events[-1] == "stop")
        assert not finish_stop.is_set()
        assert "release" not in a.events and "release" not in b.events
        runner.abort()
        finish_stop.set()
        until(lambda: runner.status == "aborted")


@pytest.mark.parametrize("failure_method", ["event", "status", "step"])
def test_audit_failure_never_releases_under_a_live_worker(tmp_path, monkeypatch, failure_method):
    registry, service = Registry(), FakeService()
    entered, finish = threading.Event(), threading.Event()

    def blocked(ctx):
        entered.set()
        finish.wait()
        service.events.append("worker-drained")

    registry.register("blocked", blocked, services=("robot",))
    with running(
        tmp_path, registry, ["blocked"], {"robot": service}, timeout=0.1, gates=(finish,)
    ) as (runner, store):
        assert entered.wait(1)

        def full(*args, **kwargs):
            raise OSError("audit disk full")

        monkeypatch.setattr(store, failure_method, full)
        until(lambda: runner.persistence_error is not None)
        assert runner.snapshot()["persistence_error"] == "audit disk full"
        assert "release" not in service.events and runner.status != "aborted"
        finish.set()
        until(lambda: runner.status == "aborted")
        assert service.events.index("worker-drained") < service.events.index("release")
        assert "stop" in service.events[service.events.index("worker-drained") + 1 :]


def test_completion_audit_failure_cannot_report_success(tmp_path, monkeypatch):
    registry, service = Registry(), FakeService()
    entered, finish = threading.Event(), threading.Event()

    def step(ctx):
        entered.set()
        finish.wait()

    registry.register("step", step, services=("robot",))
    with running(tmp_path, registry, ["step"], {"robot": service}, gates=(finish,)) as (
        runner,
        store,
    ):
        assert entered.wait(1)
        original = store.status

        def status(run_id, value, detail=""):
            if value == "completed":
                raise OSError("completion record failed")
            return original(run_id, value, detail)

        monkeypatch.setattr(store, "status", status)
        finish.set()
        until(lambda: runner.status == "aborted")
        assert runner.snapshot()["persistence_error"] == "completion record failed"


def test_abort_reports_unconfirmed_stop_even_with_prior_step_and_audit_errors(
    tmp_path, monkeypatch
):
    registry, service = Registry(), FakeService()

    def fail(ctx):
        raise RuntimeError("initial step error")

    service.stop_value = {"stop": "pending"}
    service.value.update(locked=True, fault="unknown outcome")
    registry.register("fail", fail, services=("robot",))
    with running(tmp_path, registry, ["fail"], {"robot": service}) as (runner, store):
        settled(runner)
        monkeypatch.setattr(store, "event", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        runner.abort()
        until(lambda: runner.status == "aborted")
        snapshot = runner.snapshot()
        assert snapshot["unconfirmed_services"] == ["robot"]
        assert snapshot["stops"]["robot"]["stop"] == "pending"
        assert snapshot["persistence_error"] == "full" and "release" in service.events
        assert service.value["locked"] is True and service.value["fault"] == "unknown outcome"
        with pytest.raises(RuntimeError, match="held run"):
            runner.resume(manually_completed=True, note="cannot resume abort")


@pytest.mark.parametrize("method", ["run", "command"])
@pytest.mark.parametrize("action", ["start_sync", "stop_sync", "clear_alarm", "disable"])
def test_skills_cannot_change_ownership_or_clear_manual_recovery_locks(tmp_path, method, action):
    registry, service = Registry(), FakeService()

    def unsafe(ctx):
        getattr(ctx.clients["robot"], method)(action)

    registry.register("unsafe", unsafe, services=("robot",))
    with running(tmp_path, registry, ["unsafe"], {"robot": service}) as (runner, store):
        settled(runner)
        assert action not in service.events
        assert "Skills cannot change ownership" in runner.snapshot()["detail"]


def test_abort_before_start_cannot_acquire_or_run_a_skill_and_recipe_is_snapshotted(tmp_path):
    registry, service, calls = Registry(), FakeService(), []

    def step(ctx):
        calls.append(True)

    registry.register("step", step, services=("robot",))
    recipe = prepared(registry, ["step"])
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, {"robot": service}, store)
        recipe.params["chip_id"] = "changed-after-validation"
        assert runner.recipe.params["chip_id"] == "chip-001"
        runner.abort()
        runner.start()
        assert runner.wait() == "aborted"
        runner._thread.join(1)
        assert not calls and "acquire" not in service.events


@pytest.mark.parametrize("failure", ["construct", "start"])
def test_one_stop_thread_failure_does_not_skip_other_services(monkeypatch, failure):
    services = {name: FakeService(name) for name in ("a", "b")}
    pool = Clients(services)
    pool.acquired = ["a", "b"]
    original = threading.Thread

    def thread(*args, **kwargs):
        if kwargs.get("name") == "orch-stop-a":
            if failure == "construct":
                raise RuntimeError("cannot construct thread")
            worker = original(*args, **kwargs)
            worker.start = lambda: (_ for _ in ()).throw(RuntimeError("cannot start thread"))
            return worker
        return original(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", thread)
    round_id = pool.stop_all()
    assert pool.round_done(round_id)
    assert pool.stop_snapshot()["a"]["stop"] == "unconfirmed"
    assert pool.stop_snapshot()["b"]["stop"] == "confirmed"
    assert services["b"].events == ["stop"]


def test_abort_between_steps_cannot_publish_fresh_authority(tmp_path, monkeypatch):
    registry, service, calls = Registry(), FakeService(), []
    at_next, release_next = threading.Event(), threading.Event()

    def first(ctx):
        calls.append("first")

    def second(ctx):
        ctx.clients["robot"].command("forbidden-after-abort")
        calls.append("second")

    registry.register("first", first, services=("robot",))
    registry.register("second", second, services=("robot",))
    original = Runner._step

    def step(self, prepared_step):
        if prepared_step.skill == "second":
            at_next.set()
            release_next.wait()
        return original(self, prepared_step)

    monkeypatch.setattr(Runner, "_step", step)
    with running(
        tmp_path, registry, ["first", "second"], {"robot": service}, gates=(release_next,)
    ) as (runner, store):
        assert at_next.wait(1)
        runner.abort()
        release_next.set()
        until(lambda: runner.status == "aborted")
        assert calls == ["first"] and "forbidden-after-abort" not in service.events


def test_runner_thread_start_failure_is_terminal_and_never_acquires(tmp_path, monkeypatch):
    registry, service = Registry(), FakeService()

    def step(ctx):
        pytest.fail("Unstarted run dispatched a skill")

    registry.register("step", step, services=("robot",))
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(prepared(registry, ["step"]), registry, {"robot": service}, store)

        def fail_start(worker):
            raise RuntimeError("thread resources exhausted")

        monkeypatch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="resources exhausted"):
            runner.start()
        assert runner.status == "aborted" and service.events == []
        with pytest.raises(RuntimeError, match="only once"):
            runner.start()


def test_stop_audit_failure_after_success_prevents_completed_verdict(tmp_path, monkeypatch):
    registry, service = Registry(), FakeService()
    entered, finish = threading.Event(), threading.Event()

    def step(ctx):
        entered.set()
        finish.wait()

    registry.register("step", step, services=("robot",))
    with running(tmp_path, registry, ["step"], {"robot": service}, gates=(finish,)) as (
        runner,
        store,
    ):
        assert entered.wait(1)
        original = store.event

        def event(run_id, kind, data, **kwargs):
            if kind == "stop":
                raise OSError("stop record failed")
            return original(run_id, kind, data, **kwargs)

        monkeypatch.setattr(store, "event", event)
        finish.set()
        until(lambda: runner.status == "aborted")
        assert runner.snapshot()["persistence_error"] == "stop record failed"
        assert service.events[-2:] == ["release", "close"]


def test_skill_thread_start_failure_records_failed_step_and_obeys_hold(tmp_path, monkeypatch):
    registry, service = Registry(), FakeService()

    def step(ctx):
        pytest.fail("Unstarted worker executed")

    registry.register("step", step, services=("robot",))
    original = threading.Thread.start

    def start(worker):
        if worker.name == "orch-skill":
            raise RuntimeError("skill thread resources exhausted")
        return original(worker)

    monkeypatch.setattr(threading.Thread, "start", start)
    with running(tmp_path, registry, ["step"], {"robot": service}) as (runner, store):
        settled(runner)
        row = store.get(runner.run_id)["steps"][0]
        assert row["status"] == "failed"
        assert row["error"] == "skill thread resources exhausted"
        assert "release" not in service.events


def test_known_lease_loss_between_steps_cannot_dispatch_motion(tmp_path, monkeypatch):
    registry, service, calls = Registry(), FakeService(), []
    at_next, release_next = threading.Event(), threading.Event()

    def first(ctx):
        calls.append("first")

    def second(ctx):
        calls.append("second")
        ctx.clients["robot"].command("motion-after-loss")

    registry.register("first", first, services=("robot",))
    registry.register("second", second, services=("robot",))
    original = Runner._step

    def step(self, prepared_step):
        if prepared_step.skill == "second":
            at_next.set()
            release_next.wait()
        return original(self, prepared_step)

    monkeypatch.setattr(Runner, "_step", step)
    with running(
        tmp_path, registry, ["first", "second"], {"robot": service}, gates=(release_next,)
    ) as (runner, store):
        assert at_next.wait(1)
        runner.clients._failed(RuntimeError("strict renewal failed"))
        release_next.set()
        settled(runner)
        assert calls == ["first"] and "motion-after-loss" not in service.events
        assert service.events.count("acquire") == 1
        with pytest.raises(RuntimeError, match="Lease failure cannot be resumed"):
            runner.resume(manually_completed=True, note="cannot repair ownership in this run")


def test_active_context_rejects_mutations_immediately_after_latched_lease_loss(tmp_path):
    registry, service = Registry(), FakeService()
    entered, act, inspected, finish = [threading.Event() for _ in range(4)]
    rejected = []

    def active(ctx):
        entered.set()
        act.wait()
        for operation in (
            lambda: ctx.clients["robot"].command("motion-after-loss"),
            lambda: ctx.store.event("event-after-loss", {}),
        ):
            try:
                operation()
            except Revoked:
                rejected.append(True)
        inspected.set()
        finish.wait()

    registry.register("active", active, services=("robot",))
    with running(tmp_path, registry, ["active"], {"robot": service}, gates=(act, finish)) as (
        runner,
        store,
    ):
        assert entered.wait(1)
        # Pause coordinator progress at its condition lock so this proves the
        # authority gate itself, before _hold has a chance to revoke the step.
        with runner._condition:
            runner.clients._failed(RuntimeError("strict renewal failed"))
            act.set()
            assert inspected.wait(1)
            assert rejected == [True, True]
            assert "motion-after-loss" not in service.events
        finish.set()
        settled(runner)
        assert not any(e["kind"] == "event-after-loss" for e in store.get(runner.run_id)["events"])
