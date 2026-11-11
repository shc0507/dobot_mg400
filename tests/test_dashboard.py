"""Dashboard access and safety transactions with injected instruments only."""

import copy
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.app import OrchestratorService, create_app
from orchestrator.web.backend import SERVICES

PRIVATE = "dashboard-private-instrument-token-0000000"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Offline dashboard work did not settle")
        time.sleep(0.005)


class Device:
    def __init__(self, name):
        self.name, self.calls = name, []
        self.stop_count = 0
        self.lock = threading.Lock()
        self.read = self.stop = self.gas = self.cleanup = None
        self.state = {
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "demo": True,
            "lease": {"holder": "operator"},
            "syncing": True,
            "feedback_fresh": True,
            "robot_error": False,
            "stop_unconfirmed": False,
            "controller_id": "operator",
        }

    def adapter(self, identity):
        device = self

        class Adapter:
            def describe(self):
                device.calls.append("describe")
                return {"contract": 1, "demo": True, "lease_seconds": 3}

            def state(self):
                device.calls.append("read")
                if device.read:
                    device.read()
                return copy.deepcopy(device.state)

            def stop(self, gas_channel=None):
                if gas_channel is not None:
                    device.calls.append(("gas", gas_channel))
                    return device.gas(gas_channel) if device.gas else {"stop": "confirmed"}
                with device.lock:
                    device.stop_count += 1
                    count = device.stop_count
                    device.calls.append("stop")
                return device.stop(count) if device.stop else {"stop": "confirmed"}

            def close(self):
                if device.cleanup and "dashboard-stop-" in identity:
                    device.cleanup()
                device.calls.append("close")

        return Adapter()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dashboard test attempted real I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def rig(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    devices = {name: Device(name) for name in SERVICES}

    def factory(required, identity):
        return {name: devices[name].adapter(identity) for name in required}

    service = OrchestratorService(
        settings={
            "MG400_ORCH_STAGE_TOKEN": PRIVATE,
            "MG400_ORCH_STAGE_URL": "http://private.invalid:9876",
        },
        recipes_dir=recipes,
        demo=True,
        client_factory=factory,
    ).start(background=False)
    app = create_app(service, base_path="/lab")
    yield SimpleNamespace(service=service, devices=devices, app=app, client=app.test_client())
    service.close()


def post(rig, action, data=None, actor="outsider"):
    return rig.client.post(
        "/lab/api/dashboard/" + action,
        json={} if data is None else data,
        headers={"X-MG400-Client": actor},
    )


def report(rig):
    return rig.service.dashboard.snapshot()


def stage_card(rig):
    return next(item for item in report(rig)["services"] if item["id"] == "stage")


def operation(rig, identity):
    return next(item for item in report(rig)["operations"] if item["id"] == identity)


def test_exact_public_shell_and_assets_need_no_token(rig):
    for path in ("/lab/", "/lab/chip"):
        response = rig.client.get(path, headers={"Origin": "http://foreign.invalid"})
        assert response.status_code == 200
        assert PRIVATE not in response.get_data(as_text=True)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    for path in ("/lab/api/services", "/lab/api/dashboard/state", "/lab/api/runs"):
        assert rig.client.get(path, headers={"Host": "remote.invalid"}).status_code == 200
    for path in (
        "/lab/dashboard/",
        "/lab/dashboard-static/dashboard.js",
        "/lab/dashboard-static/dashboard.css",
        "/lab/dashboard-static/../app.py",
    ):
        assert rig.client.get(path).status_code == 404
    assert rig.client.post("/lab/").status_code in {405, 415}


@pytest.mark.parametrize("action", ["motion", "heat", "gas", "hold"])
def test_safety_routes_require_json_identity_and_exact_confirmation(rig, action):
    url = "/lab/api/dashboard/" + action
    assert rig.client.post(url, json={}).status_code == 400
    headers = {"X-MG400-Client": "operator", "Content-Type": "application/json"}
    for body in ("[]", '{"confirmed":true,"confirmed":false}', '{"confirmed":NaN}'):
        assert rig.client.post(url, data=body, headers=headers).status_code == 400
    assert (
        rig.client.post(
            url, data="{}", headers={**headers, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    if action in {"heat", "gas"}:
        for body in (
            {},
            {"confirmed": 1},
            {"confirmed": "true"},
            {"confirmed": True, "url": "http://other.invalid"},
        ):
            assert post(rig, action, body).status_code == 400
    assert not report(rig)["operations"]
    assert all(not device.calls for device in rig.devices.values())


def test_state_is_cached_allowlisted_and_redacts_private_data(rig):
    device = rig.devices["stage"]
    device.state.update(
        token=PRIVATE, url="http://private.invalid:9876", fault={"message": PRIVATE}
    )
    device.state["lease"]["holder"] = "prefix " + PRIVATE + " http://private.invalid:9876 <script>"
    rig.service.devices.fresh("stage")
    data = report(rig)
    encoded = json.dumps(data)
    assert PRIVATE not in encoded and "http://private.invalid:9876" not in encoded
    assert len(data["services"]) == 6
    stage = next(item for item in data["services"] if item["id"] == "stage")
    assert stage["phase"] == "ready" and stage["connected"] is True
    assert stage["fault"] == "Service reports a fault or unknown fault state."
    assert "[redacted]" in stage["lease_holder"]
    assert "token" not in stage and "url" not in stage
    assert all(camera["url"] is None for camera in data["cameras"])
    rig.service.devices.close()
    rig.service.devices.clock = lambda: time.monotonic() + 4
    assert stage_card(rig)["phase"] == "stale"


IDLE_ROBOT = {"syncing": False, "feedback_fresh": False, "robot_error": None, "alarms": None}


@pytest.mark.parametrize(
    "change, connected, fault",
    [
        (IDLE_ROBOT, False, False),
        ({"feedback_fresh": False, "robot_error": None}, False, False),
        ({"robot_error": True}, True, True),
        ({**IDLE_ROBOT, "robot_error": True}, False, True),
        ({"alarms": [{"id": 22}]}, True, True),
        ({"robot_error": None}, True, True),
        ({"stop_unconfirmed": True}, True, True),
        ({**IDLE_ROBOT, "stop_unconfirmed": None}, False, True),
        ({**IDLE_ROBOT, "syncing": None}, None, True),
        ({**IDLE_ROBOT, "locked": True}, False, False),
    ],
)
def test_an_idle_robot_is_not_connected_and_only_real_faults_count(rig, change, connected, fault):
    rig.devices["robot"].state.update(change)
    rig.service.devices.fresh("robot")
    robot = next(item for item in report(rig)["services"] if item["id"] == "robot")
    assert robot["connected"] is connected
    assert (robot["fault"] is not None) is fault
    assert robot["locked"] is change.get("locked", False)


def test_blocked_state_read_does_not_delay_independent_stop_attempts(rig):
    blocked, release = threading.Event(), threading.Event()
    rig.devices["robot"].read = lambda: (blocked.set(), release.wait(3))
    try:
        rig.service.dashboard.refresh()
        assert blocked.wait(1)
        response = post(rig, "motion")
        assert response.status_code == 202
        wait_for(
            lambda: all(
                rig.devices[name].stop_count == 2 for name in ("robot", "stage", "microscope")
            )
        )
        identity = response.json["operation_id"]
        wait_for(lambda: operation(rig, identity)["done"])
        assert all(
            item["status"] == "confirmed" for item in operation(rig, identity)["results"].values()
        )
    finally:
        release.set()


def test_each_initial_stop_attempt_is_independent_and_final_failures_replace_confirmation(rig):
    blocked, release = threading.Event(), threading.Event()

    def robot_stop(count):
        if count == 1:
            blocked.set()
            assert release.wait(3)
            return {"stop": "confirmed"}
        return {"stop": "unconfirmed"}

    rig.devices["robot"].stop = robot_stop
    try:
        response = post(rig, "motion")
        assert response.status_code == 202 and blocked.wait(1)
        identity = response.json["operation_id"]
        wait_for(
            lambda: rig.devices["stage"].stop_count == rig.devices["microscope"].stop_count == 1
        )
        assert not operation(rig, identity)["done"]
        assert rig.service.dashboard.busy()
        assert post(rig, "motion").json["operation_id"] == identity
        release.set()
        wait_for(lambda: operation(rig, identity)["done"])
        assert operation(rig, identity)["results"]["robot"]["status"] == "unconfirmed"
        assert all(
            device.stop_count == 0
            for name, device in rig.devices.items()
            if name not in {"robot", "stage", "microscope"}
        )
    finally:
        release.set()


class HeldRunner:
    def __init__(self):
        self.run_id = "held-run"
        self.held = threading.Event()
        self.drained = threading.Event()
        self.closed = False
        self.calls = []

    def hold(self, reason):
        self.calls.append("hold")
        self.held.set()

    def cancel_resume(self, event, reason):
        self.calls.append("cancel-resume")
        event.set()
        self.hold(reason)

    def wait_closed(self, timeout):
        return self.closed

    def snapshot(self):
        return {
            "status": "held",
            "closed": self.closed,
            "settled": self.drained.is_set(),
            "draining": not self.drained.is_set(),
            "unconfirmed_services": [],
            "release_errors": {},
            "persistence_error": None,
        }

    def shutdown(self, reason):
        self.closed = True


def test_blocked_app_lock_and_late_old_write_keep_gate_until_fresh_final_stops(rig):
    runner = HeldRunner()
    rig.service.current = runner
    rig.service.runners[runner.run_id] = runner
    resume = rig.service.resume_cancel = threading.Event()
    locked, release = threading.Event(), threading.Event()

    def old_release():
        with rig.service.lock:
            locked.set()
            assert release.wait(3)

    worker = threading.Thread(target=old_release)
    worker.start()
    try:
        assert locked.wait(1)
        response = post(rig, "motion")
        assert response.status_code == 202
        identity = response.json["operation_id"]
        wait_for(
            lambda: all(
                rig.devices[name].stop_count == 1 for name in ("robot", "stage", "microscope")
            )
        )
        assert not runner.held.is_set() and not operation(rig, identity)["done"]
        rig.devices["robot"].calls.append("late-old-write")
        release.set()
        worker.join(1)
        assert runner.held.wait(1) and resume.is_set()
        with rig.service.lock, pytest.raises(ServiceError, match="still draining"):
            rig.service._schedule("resume", "operator", lambda job: pytest.fail("Readmitted"))
        assert rig.devices["robot"].stop_count == 1
        runner.drained.set()
        wait_for(lambda: operation(rig, identity)["done"])
        assert (
            rig.devices["robot"].calls.index("late-old-write") < len(rig.devices["robot"].calls) - 1
        )
        assert all(rig.devices[name].stop_count == 2 for name in ("robot", "stage", "microscope"))
    finally:
        release.set()
        runner.drained.set()
        worker.join(1)


def test_late_startup_runner_and_recovery_must_drain_before_final_stop(rig):
    job = {"cancelled": threading.Event(), "done": threading.Event()}
    rig.service.job = job
    response = post(rig, "motion")
    identity = response.json["operation_id"]
    wait_for(job["cancelled"].is_set)
    runner = HeldRunner()
    recovery = SimpleNamespace(
        closed=threading.Event(), cancel=lambda: None, snapshot=lambda: {"stops": {}}
    )
    with rig.service.lock:
        rig.service.current = runner
        rig.service.recovery = recovery
        job["done"].set()
    try:
        assert runner.held.wait(1)
        runner.drained.set()
        time.sleep(0.06)
        assert not operation(rig, identity)["done"]
        recovery.closed.set()
        wait_for(lambda: operation(rig, identity)["done"])
        assert all(rig.devices[name].stop_count == 2 for name in ("robot", "stage", "microscope"))
    finally:
        runner.drained.set()
        recovery.closed.set()


@pytest.mark.parametrize("failure", ["hold", "snapshot"])
def test_failed_hold_or_inspection_keeps_stop_pending_and_admission_closed(rig, failure):
    runner = HeldRunner()
    runner.drained.set()
    rig.service.current = runner
    failed, repaired = threading.Event(), threading.Event()
    original = getattr(runner, failure)

    def uncertain(*args):
        if not repaired.is_set():
            failed.set()
            raise OSError("Offline unavailable state")
        return original(*args)

    setattr(runner, failure, uncertain)
    try:
        identity = post(rig, "motion").json["operation_id"]
        assert failed.wait(1)
        wait_for(
            lambda: all(
                rig.devices[name].stop_count == 1 for name in ("robot", "stage", "microscope")
            )
        )

        def failure_published():
            # The fake signals before its exception reaches the status publisher.
            # Admission must stay closed throughout that asynchronous transition.
            result = operation(rig, identity)
            assert not result["done"]
            assert rig.service.dashboard.busy()
            return result["hold"] == "unconfirmed"

        wait_for(failure_published)
        result = operation(rig, identity)
        assert not result["done"] and result["hold"] == "unconfirmed"
        assert rig.service.dashboard.busy()
        with pytest.raises(ServiceError, match="safety request"):
            rig.service._schedule("offline", "operator", lambda job: None)
        repaired.set()
        wait_for(lambda: operation(rig, identity)["done"])
        assert all(rig.devices[name].stop_count == 2 for name in ("robot", "stage", "microscope"))
    finally:
        repaired.set()


@pytest.mark.parametrize("delay", [1, 4])
def test_cached_lease_display_ages_from_observation_not_response_time(rig, monkeypatch, delay):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(rig.service.dashboard, "clock", lambda: clock.now)

    def summary():
        clock.now += delay  # The durable run read can finish after the lease observation.
        return {"connected": True, "lease": {"holder": "operator", "remaining_s": 3}}, None

    monkeypatch.setattr(rig.service, "dashboard_summary", summary)
    rig.service.dashboard._summary()
    assert report(rig)["orchestrator"]["lease"]["remaining_s"] == max(0, 3 - delay)
    assert report(rig)["orchestrator"]["stale"] is (delay > 3)
    clock.now += 1
    assert report(rig)["orchestrator"]["lease"]["remaining_s"] == max(0, 2 - delay)
    clock.now += 3
    assert report(rig)["orchestrator"]["lease"]["remaining_s"] == 0
    assert report(rig)["orchestrator"]["stale"] is True


@pytest.mark.parametrize(
    "status,started,kind",
    [
        ("pending", None, "pending"),
        ("started", 1, "started"),
        ("failed", 1, "started"),
        ("failed", None, None),
    ],
)
def test_resume_guidance_uses_the_durable_current_step(rig, monkeypatch, status, started, kind):
    run = {
        "id": "held",
        "live": {"status": "held", "step": 1, "settled": True},
        "steps": [
            {"position": 0, "skill": "wait", "status": "completed", "started_at": 1},
            {"position": 1, "skill": "wait", "status": status, "started_at": started},
        ],
    }
    monkeypatch.setattr(rig.service, "dashboard_summary", lambda: ({"lease": {}}, run))
    rig.service.dashboard._summary()
    summary = report(rig)["run"]
    assert summary["step_position"] == 1 and summary["resume_kind"] == kind


def test_overlapping_heat_and_motion_hold_independent_gate_tokens(rig):
    entered, release = threading.Event(), threading.Event()
    rig.devices["furnace"].stop = lambda count: (
        entered.set(),
        release.wait(3),
        {"stop": "confirmed"},
    )[-1]
    try:
        heat = post(rig, "heat", {"confirmed": True}).json["operation_id"]
        assert entered.wait(1)
        motion = post(rig, "motion").json["operation_id"]
        wait_for(lambda: operation(rig, motion)["done"])
        assert not operation(rig, heat)["done"] and rig.service.dashboard.busy()
        release.set()
        wait_for(lambda: operation(rig, heat)["done"])
        assert not rig.service.dashboard.busy()
    finally:
        release.set()


def test_gas_off_attempts_all_channels_without_holding_run_or_broader_stop(rig):
    runner = HeldRunner()
    rig.service.current = runner

    def gas(channel):
        if channel == 0:
            raise OSError(PRIVATE)
        return {"stop": "unconfirmed" if channel == 1 else "confirmed"}

    rig.devices["furnace"].gas = gas
    response = post(rig, "gas", {"confirmed": True})
    assert response.status_code == 202
    identity = response.json["operation_id"]
    wait_for(lambda: operation(rig, identity)["done"])
    result = operation(rig, identity)
    assert result["hold"] == "not-required" and not runner.calls
    assert {key: value["status"] for key, value in result["results"].items()} == {
        "gas-0": "failed",
        "gas-1": "unconfirmed",
        "gas-2": "confirmed",
    }
    assert {call for call in rig.devices["furnace"].calls if isinstance(call, tuple)} == {
        ("gas", 0),
        ("gas", 1),
        ("gas", 2),
    }
    assert all(device.stop_count == 0 for device in rig.devices.values())
    assert PRIVATE not in json.dumps(result)


def test_owned_client_cleanup_is_part_of_gate_and_shutdown(rig):
    entered, release = threading.Event(), threading.Event()
    rig.devices["stage"].cleanup = lambda: (entered.set(), release.wait(3))
    response = post(rig, "motion")
    identity = response.json["operation_id"]
    assert entered.wait(1)
    closer = threading.Thread(target=rig.service.close)
    closer.start()
    try:
        time.sleep(0.05)
        assert closer.is_alive() and not operation(rig, identity)["done"]
        assert rig.service.store is not None
        release.set()
        closer.join(2)
        assert not closer.is_alive() and rig.service.store is None
    finally:
        release.set()
        closer.join(2)


def test_shutdown_cannot_join_a_registered_but_unstarted_safety_thread(rig, monkeypatch):
    registered, start_allowed = threading.Event(), threading.Event()
    original = threading.Thread.start
    errors, responses = [], []

    def delayed_start(worker):
        if worker.name == "dashboard-robot-stop" and not registered.is_set():
            registered.set()
            assert start_allowed.wait(3)
        return original(worker)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)

    def request():
        try:
            responses.append(post(rig, "motion"))
        except BaseException as error:
            errors.append(error)

    def close():
        try:
            rig.service.close()
        except BaseException as error:
            errors.append(error)

    requester = threading.Thread(target=request)
    requester.start()
    assert registered.wait(1)
    closer = threading.Thread(target=close)
    closer.start()
    try:
        time.sleep(0.05)
        assert closer.is_alive() and not errors
        start_allowed.set()
        requester.join(2)
        closer.join(2)
        assert not requester.is_alive() and not closer.is_alive()
        assert not errors and responses[0].status_code == 202
        assert rig.service.store is None
    finally:
        start_allowed.set()
        requester.join(2)
        closer.join(2)


def test_one_unstarted_stop_worker_does_not_suppress_other_targets(rig, monkeypatch):
    original = threading.Thread.start

    def fail_robot(worker):
        if worker.name == "dashboard-robot-stop":
            raise RuntimeError("Synthetic thread failure")
        return original(worker)

    monkeypatch.setattr(threading.Thread, "start", fail_robot)
    response = post(rig, "motion")
    assert response.status_code == 202
    identity = response.json["operation_id"]
    wait_for(lambda: operation(rig, identity)["done"])
    result = operation(rig, identity)
    assert result["results"]["robot"]["status"] == "failed"
    assert rig.devices["stage"].stop_count == rig.devices["microscope"].stop_count == 2


def test_camera_links_are_absent_and_private_endpoints_stay_hidden(tmp_path):
    service = OrchestratorService(
        settings={
            "MG400_DASH_CAMERA_URL": "https://lab.example/camera/",
            "MG400_ORCH_THERMAL_URL": "http://private.invalid:1",
            "MG400_ORCH_THERMAL_TOKEN": PRIVATE,
        },
        recipes_dir=tmp_path,
    )
    assert all(item["url"] is None for item in service.dashboard.snapshot()["cameras"])
    assert PRIVATE not in json.dumps(service.dashboard.snapshot())
    service.close()
