"""Lease adapters use strict renewal and isolate cleanup failures."""

import copy
import threading
import time
from unittest.mock import Mock

import pytest

from labkit.errors import ServiceError
from labkit.lease import Lease
from orchestrator.clients import Clients, RobotAdapter, ServiceAdapter, from_settings


def lease_adapter(monkeypatch):
    now = [10.0]
    lease = Lease(clock=lambda: now[0])
    paths = []
    adapter = ServiceAdapter("http://127.0.0.1:1", "", "orch-unique-run")

    def request(path, data=None):
        paths.append(path)
        if path == "api/describe":
            return {"contract": 1, "lease_seconds": 3}
        if path == "api/heartbeat":
            return {"lease": lease.acquire(adapter.identity)}
        if path == "api/renew":
            return {"lease": lease.renew(adapter.identity)}
        if path == "api/state":
            return {
                "lease": lease.snapshot(),
                "busy": False,
                "locked": False,
                "fault": None,
                "connected": True,
            }
        if path == "api/stop":
            return {"stop": "confirmed"}
        if path == "api/release":
            return {"lease": lease.release(adapter.identity)}
        pytest.fail(f"Unexpected offline request: {path}")

    monkeypatch.setattr(adapter._http, "_request", request)
    return adapter, lease, now, paths


@pytest.mark.parametrize("loss", ["expired", "released", "other-holder"])
def test_atomic_renewal_never_reacquires_a_lost_lease(monkeypatch, loss):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    adapter.describe()
    adapter.acquire()
    now[0] += 1
    adapter.heartbeat()
    assert paths.count("api/heartbeat") == 1 and paths.count("api/renew") == 1
    if loss == "expired":
        now[0] += 4
    else:
        lease.release(adapter.identity)
        if loss == "other-holder":
            lease.acquire("other")
    with pytest.raises(ServiceError, match="Acquire the control lease"):
        adapter.heartbeat()
    assert paths.count("api/heartbeat") == 1
    assert lease.holder == ("other" if loss == "other-holder" else None)


def test_missing_renew_route_fails_closed_without_heartbeat_fallback(monkeypatch):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    adapter.describe()
    adapter.acquire()
    original = adapter._http._request

    def legacy(path, data=None):
        if path == "api/renew":
            raise ServiceError(404, "missing renew route")
        return original(path, data)

    monkeypatch.setattr(adapter._http, "_request", legacy)
    with pytest.raises(ServiceError) as error:
        adapter.heartbeat()
    assert error.value.status == 404 and paths.count("api/heartbeat") == 1


def test_renew_loss_is_terminal_even_if_another_process_restores_ownership(monkeypatch):
    adapter, lease, now, paths = lease_adapter(monkeypatch)
    pool = Clients({"stage": adapter})
    pool.acquire(("stage",), cancelled=threading.Event())
    try:
        now[0] += 4
        deadline = time.monotonic() + 2
        while pool.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.error is not None
        lease.acquire(adapter.identity)
        pool.stop_all()
        with pytest.raises(RuntimeError, match="Lease failure cannot be resumed"):
            pool.reconcile()
        assert paths.count("api/heartbeat") == 1
        assert paths.count("api/renew") == 1
    finally:
        while pool.release() is False:
            time.sleep(0.01)


def test_release_and_close_errors_do_not_skip_other_services():
    calls = []

    class Service:
        def __init__(self, name):
            self.identity = "one-shared-run-identity"
            self.name = name

        def release(self):
            calls.append((self.name, "release"))
            raise RuntimeError("release failed")

        def close(self):
            calls.append((self.name, "close"))
            raise RuntimeError("close failed")

    pool = Clients({name: Service(name) for name in ("a", "b")})
    pool.acquired = ["a", "b"]
    errors = pool.release()
    assert calls == [("a", "release"), ("b", "release"), ("a", "close"), ("b", "close")]
    assert set(errors) == {"a", "b", "close:a", "close:b"}


def test_adapter_construction_makes_no_requests_and_disables_ambient_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://production.invalid:9")
    clients = from_settings(
        {"MG400_ORCH_STAGE_URL": "http://127.0.0.1:1/contract"}, identity="orch-config-only"
    )
    assert set(clients) == {"stage"}
    handlers = clients["stage"]._http._opener.handlers
    assert not any(getattr(handler, "proxies", {}) for handler in handlers)
    redirect = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "", {}, "http://other.invalid") is None


def reconciliation_pool(monkeypatch, name, state):
    """An already-owned, stopped run; only adapter state reads are replaced."""
    identity = "orch-recovery-test"
    adapter_type = RobotAdapter if name == "robot" else ServiceAdapter
    adapter = adapter_type("http://127.0.0.1:1", "", identity)
    adapter._lease_seconds = 3
    monkeypatch.setattr(adapter, "state", lambda: copy.deepcopy(state))
    pool = Clients({name: adapter})
    pool.acquired = [name]
    pool._instances[name] = state.get("instance")
    pool.stop_round = pool._stop_rounds[name] = 1
    pool.stop_results[name] = {"stop": "confirmed"}
    return pool


@pytest.fixture
def robot_reconciliation(monkeypatch):
    # RobotPanelState.snapshot uses these fields, without generic connected/fault.
    state = {
        "mode": "robot",
        "instance": "robot-process-instance",
        "controller_id": "orch-recovery-test",
        "syncing": True,
        "feedback_fresh": True,
        "busy": False,
        "locked": False,
        "robot_error": False,
        "stop_unconfirmed": False,
    }
    return reconciliation_pool(monkeypatch, "robot", state), state


def test_reconcile_accepts_the_kept_robot_state_without_generic_fields(robot_reconciliation):
    pool, state = robot_reconciliation
    assert "fault" not in state and "connected" not in state
    assert pool.reconcile() == {"robot": state}


_ABSENT = object()


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", _ABSENT),
        ("mode", "read-only"),
        ("controller_id", _ABSENT),
        ("controller_id", "other-holder"),
        ("syncing", _ABSENT),
        ("syncing", False),
        ("syncing", 1),
        ("feedback_fresh", _ABSENT),
        ("feedback_fresh", False),
        ("feedback_fresh", 1),
        ("busy", _ABSENT),
        ("busy", True),
        ("busy", 0),
        ("locked", _ABSENT),
        ("locked", True),
        ("locked", 0),
        ("robot_error", _ABSENT),
        ("robot_error", None),
        ("robot_error", True),
        ("robot_error", 0),
        ("stop_unconfirmed", _ABSENT),
        ("stop_unconfirmed", None),
        ("stop_unconfirmed", True),
        ("stop_unconfirmed", 0),
        ("instance", _ABSENT),
        ("instance", None),
        ("instance", ""),
        ("instance", " "),
        ("instance", 0),
        ("instance", "replacement-process"),
        ("fault", {"code": "unknown", "message": "A reported fault"}),
        ("fault", False),
    ],
)
def test_reconcile_robot_requires_known_safe_owned_same_instance_state(
    robot_reconciliation, field, value
):
    pool, state = robot_reconciliation
    if value is _ABSENT:
        state.pop(field)
    else:
        state[field] = value
    with pytest.raises(RuntimeError, match="manual recovery"):
        pool.reconcile()


@pytest.mark.parametrize("state", [None, [], "invalid state"])
def test_reconcile_robot_refuses_malformed_state(robot_reconciliation, monkeypatch, state):
    pool, _ = robot_reconciliation
    monkeypatch.setattr(pool.services["robot"], "state", lambda: state)
    with pytest.raises(RuntimeError, match="manual recovery"):
        pool.reconcile()


@pytest.fixture
def generic_reconciliation(monkeypatch):
    state = {
        "connected": True,
        "busy": False,
        "locked": False,
        "fault": None,
        "lease": {"holder": "orch-recovery-test", "remaining_s": 2},
    }
    return reconciliation_pool(monkeypatch, "stage", state), state


def test_reconcile_generic_service_keeps_optional_instance_compatibility(generic_reconciliation):
    pool, state = generic_reconciliation
    assert "instance" not in state
    assert pool.reconcile() == {"stage": state}


@pytest.mark.parametrize(
    "field,value",
    [
        ("fault", _ABSENT),
        ("fault", False),
        ("fault", {}),
        ("fault", {"code": "unknown", "message": "Stop unconfirmed"}),
        ("connected", _ABSENT),
        ("connected", False),
        ("connected", 1),
        ("instance", "unexpected-process"),
        ("stop_unconfirmed", True),
    ],
)
def test_reconcile_generic_service_still_requires_its_contract(
    generic_reconciliation, field, value
):
    pool, state = generic_reconciliation
    # Robot-shaped additions cannot opt a generic service out of its own contract.
    state.update(mode="robot", robot_error=False, stop_unconfirmed=False)
    if value is _ABSENT:
        state.pop(field)
    else:
        state[field] = value
    with pytest.raises(RuntimeError, match="manual recovery"):
        pool.reconcile()


@pytest.fixture
def disabled_robot(robot_reconciliation, monkeypatch):
    pool, state = robot_reconciliation
    state.update(
        robot_mode=4,
        enabled=False,
        keyboard_enabled=False,
        keyboard_arming=False,
        keyboard_active_key=None,
        gripper_busy=False,
        last_command={
            "id": "manual-disable",
            "action": "disable",
            "client": "operator",  # Safety disable need not come from the run holder.
            "result": "ok",
            "message": "Robot disable and rest are confirmed.",
            "finished_at": 1.0,  # No arbitrary age cutoff; current feedback proves rest.
        },
    )
    stop = Mock(return_value={"stop": "confirmed"})
    monkeypatch.setattr(pool.services["robot"]._robot, "_http", stop)
    return pool, state, stop


def test_recovery_only_confirmation_uses_fresh_explicit_manual_disable(disabled_robot):
    pool, state, stop = disabled_robot
    result = pool.services["robot"].recovery_stop(pool._instances["robot"])
    assert result == {
        "stop": "confirmed",
        "state": state,
        "recovery": "verified-disabled-rest",
    }
    stop.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance", _ABSENT),
        ("instance", "other-instance"),
        ("mode", "read-only"),
        ("controller_id", "other-holder"),
        ("syncing", False),
        ("feedback_fresh", False),
        ("robot_mode", _ABSENT),
        ("robot_mode", 5),
        ("robot_mode", 4.0),
        ("enabled", _ABSENT),
        ("enabled", True),
        ("busy", True),
        ("locked", True),
        ("robot_error", True),
        ("stop_unconfirmed", True),
        ("keyboard_enabled", _ABSENT),
        ("keyboard_enabled", True),
        ("keyboard_arming", _ABSENT),
        ("keyboard_arming", True),
        ("keyboard_active_key", _ABSENT),
        ("keyboard_active_key", "ArrowUp"),
        ("gripper_busy", _ABSENT),
        ("gripper_busy", True),
        ("fault", "unknown outcome"),
        ("last_command", _ABSENT),
        ("last_command", []),
    ],
)
def test_unproven_recovery_state_sends_real_stop(disabled_robot, field, value):
    pool, state, stop = disabled_robot
    if value is _ABSENT:
        state.pop(field)
    else:
        state[field] = value
    assert pool.services["robot"].recovery_stop(pool._instances["robot"]) == {"stop": "confirmed"}
    stop.assert_called_once_with("stop", {"wait_s": 1})


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", _ABSENT),
        ("action", "enable"),
        ("result", _ABSENT),
        ("result", "stopped"),
        ("id", _ABSENT),
        ("id", ""),
        ("id", 1),
        ("client", _ABSENT),
        ("client", " "),
        ("client", None),
        ("finished_at", _ABSENT),
        ("finished_at", 0),
        ("finished_at", -1),
        ("finished_at", True),
        ("finished_at", "1"),
        ("finished_at", float("inf")),
        ("finished_at", float("nan")),
        ("finished_at", 10**1000),
    ],
)
def test_recovery_requires_valid_latest_disable_metadata(disabled_robot, field, value):
    pool, state, stop = disabled_robot
    if value is _ABSENT:
        state["last_command"].pop(field)
    else:
        state["last_command"][field] = value
    assert pool.services["robot"].recovery_stop(pool._instances["robot"]) == {"stop": "confirmed"}
    stop.assert_called_once()


def test_failed_recovery_read_still_attempts_independent_stop(disabled_robot, monkeypatch):
    pool, _, stop = disabled_robot
    adapter = pool.services["robot"]
    monkeypatch.setattr(adapter, "state", Mock(side_effect=OSError("unavailable feedback")))
    assert adapter.recovery_stop(pool._instances["robot"]) == {"stop": "confirmed"}
    stop.assert_called_once()


@pytest.mark.parametrize("state", [None, [], "invalid state"])
def test_malformed_recovery_state_still_attempts_independent_stop(
    disabled_robot, monkeypatch, state
):
    pool, _, stop = disabled_robot
    adapter = pool.services["robot"]
    monkeypatch.setattr(adapter, "state", lambda: state)
    assert adapter.recovery_stop(pool._instances["robot"]) == {"stop": "confirmed"}
    stop.assert_called_once()


def test_generic_recovery_still_requests_fresh_stop(generic_reconciliation, monkeypatch):
    pool, _ = generic_reconciliation
    adapter = pool.services["stage"]
    stop = Mock(return_value={"stop": "pending"})
    monkeypatch.setattr(adapter, "stop", stop)
    assert adapter.recovery_stop(None) == {"stop": "pending"}
    stop.assert_called_once()


def test_default_stop_round_always_cancels_even_with_valid_disabled_proof(disabled_robot):
    pool, _, stop = disabled_robot
    round_id = pool.stop_all()
    assert pool.round_done(round_id) and pool.confirmed
    stop.assert_called_once_with("stop", {"wait_s": 1})
    assert "recovery" not in pool.stop_snapshot()["robot"]


def test_lease_failure_cannot_select_recovery_proof(disabled_robot):
    pool, _, stop = disabled_robot
    pool._failed("Lease renewal failed")
    pool.recovery_stop_all()
    stop.assert_called_once_with("stop", {"wait_s": 1})


def test_recovery_mode_survives_async_round_progress_and_new_round_defaults_to_stop():
    entered, finish = threading.Event(), threading.Event()
    calls = []

    class Service:
        timeout = 0

        def __init__(self, name):
            self.name = name
            self.first = True

        def stop(self):
            calls.append((self.name, "stop"))
            if self.name == "a" and self.first:
                self.first = False
                entered.set()
                assert finish.wait(5), "offline stop did not drain"
            return {"stop": "confirmed"}

        def recovery_stop(self, expected_instance):
            assert expected_instance == self.name + "-instance"
            calls.append((self.name, "recovery"))
            return {"stop": "confirmed"}

    pool = Clients({name: Service(name) for name in ("a", "b")})
    pool.acquired = ["a", "b"]
    pool._instances = {name: name + "-instance" for name in pool.acquired}
    try:
        pool.stop_all()
        assert entered.is_set()
        recovery_round = pool.recovery_stop_all()
        assert not pool.round_done(recovery_round)
        assert calls.count(("b", "recovery")) == 1
        finish.set()
        pool._stoppers["a"].join(1)
        pool.stop_all(recovery_round)
        assert pool.round_done(recovery_round) and pool.confirmed
        assert calls.count(("a", "recovery")) == 1
        assert calls.count(("a", "stop")) == 1
        pool.stop_all()
        assert calls.count(("a", "stop")) == 2 and calls.count(("b", "stop")) == 2
        assert calls.count(("a", "recovery")) == calls.count(("b", "recovery")) == 1
    finally:
        finish.set()
        for worker in pool._stoppers.values():
            worker.join(1)
            assert not worker.is_alive()
