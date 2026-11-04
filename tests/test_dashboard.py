"""Dashboard access and safety transactions with injected instruments only."""

import copy
import io
import json
import socket
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import orchestrator.clients as client_module
from labkit.client import ServiceClient
from labkit.errors import ServiceError
from orchestrator.app import OrchestratorService, create_app
from orchestrator.clients import FurnaceAdapter, from_settings
from orchestrator.rehearsal import Rehearsal
from orchestrator.web.backend import SERVICES, gas_confirmed

PRIVATE = "dashboard-private-instrument-token-0000000"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Offline dashboard work did not settle")
        time.sleep(0.005)


def gas_state(channel):
    return {
        "ok": True,
        "status": {
            "connected": True,
            "locked": False,
            "fault": None,
            "error": None,
            "gas_channels": [
                {"channel_index": channel, "available": True, "actual_percent": 0, "error": None}
            ],
            "gas_valves": [{"channel_index": channel, "open": False, "verified": True}],
        },
    }


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

            def stop(self):
                with device.lock:
                    device.stop_count += 1
                    count = device.stop_count
                    device.calls.append("stop")
                return device.stop(count) if device.stop else {"stop": "confirmed"}

            def gas_stop(self, channel):
                device.calls.append(("gas", channel))
                return device.gas(channel) if device.gas else gas_state(channel)

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
    rig.service.dashboard.refresh()
    wait_for(lambda: not rig.service.dashboard.reading)
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
    rig.service.dashboard.clock = lambda: time.monotonic() + 4
    assert all(item["phase"] == "stale" for item in report(rig)["services"])


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
        value = gas_state(channel)
        if channel == 1:
            value["status"]["gas_channels"][0]["available"] = False
        return value

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


@pytest.mark.parametrize(
    "change",
    [
        "disconnected",
        "locked",
        "fault",
        "missing-fault",
        "flow-unknown",
        "flow-nonzero",
        "flow-bool",
        "valve-open",
        "valve-unverified",
        "duplicate",
    ],
)
def test_gas_confirmation_requires_exact_target_feedback(change):
    value = gas_state(1)
    state = value["status"]
    if change == "disconnected":
        state["connected"] = False
    elif change == "locked":
        state["locked"] = True
    elif change == "fault":
        state["fault"] = {"code": "unknown"}
    elif change == "missing-fault":
        del state["fault"]
    elif change == "flow-unknown":
        state["gas_channels"][0]["available"] = False
    elif change == "flow-nonzero":
        state["gas_channels"][0]["actual_percent"] = 2
    elif change == "flow-bool":
        state["gas_channels"][0]["actual_percent"] = False
    elif change == "valve-open":
        state["gas_valves"][0]["open"] = True
    elif change == "valve-unverified":
        state["gas_valves"][0]["verified"] = False
    else:
        state["gas_channels"].append(copy.deepcopy(state["gas_channels"][0]))
    assert not gas_confirmed(value, 1)


@pytest.mark.parametrize("ok", [False, None, 1, "true"])
def test_explicit_or_malformed_gas_failure_is_never_confirmed(ok):
    value = gas_state(1)
    value["ok"] = ok
    assert not gas_confirmed(value, 1)
    del value["ok"]
    assert not gas_confirmed(value, 1)


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


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(reply)


def adapter(monkeypatch, replies):
    obj = FurnaceAdapter(
        "http://127.0.0.1:1/furnace/", "fake-credential-for-offline-tests", "review", timeout=0.25
    )
    opener = Opener(replies)
    monkeypatch.setattr(obj._http, "_opener", opener)
    monkeypatch.setattr(obj._gas_http, "_opener", opener)
    return obj, opener


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_exact_channel_stop_preserves_unconfirmed_result(monkeypatch, channel):
    reply = {"ok": False, "status": {"locked": True, "gas_channels": []}}
    obj, network = adapter(monkeypatch, [b'{"contract":1}', json.dumps(reply).encode()])
    assert obj.gas_stop(channel) == reply
    assert len(network.calls) == 2
    description, stop = [request for request, _ in network.calls]
    assert description.get_method() == "GET"
    assert description.full_url.endswith("/furnace/api/describe")
    assert stop.get_method() == "POST"
    assert stop.full_url.endswith("/furnace/api/flow/gas/stop")
    assert json.loads(stop.data) == {"channel_index": channel}
    assert stop.get_header("X-mg400-client") == "review"
    assert stop.get_header("Authorization") == "Bearer fake-credential-for-offline-tests"
    assert all(timeout == 0.25 for _, timeout in network.calls)


@pytest.mark.parametrize("channel", [True, False, -1, 3, 0.0, "0", None, {}, []])
def test_invalid_channel_never_describes_or_stops(monkeypatch, channel):
    obj, network = adapter(monkeypatch, [])
    with pytest.raises(ValueError):
        obj.gas_stop(channel)
    assert network.calls == []


@pytest.mark.parametrize("payload", [b'{"contract":2}', b'{"contract":true}', b"{}"])
def test_wrong_contract_never_stops(monkeypatch, payload):
    obj, network = adapter(monkeypatch, [payload])
    with pytest.raises(ServiceError):
        obj.gas_stop(0)
    assert len(network.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        b"[]",
        b"null",
        b"invalid",
        urllib.error.HTTPError("http://127.0.0.1:1/", 403, "Denied", {}, io.BytesIO(b"{}")),
    ],
)
def test_failure_does_not_fallback_to_broader_stop(monkeypatch, reply):
    obj, network = adapter(monkeypatch, [b'{"contract":1}', reply])
    with pytest.raises(ServiceError):
        obj.gas_stop(1)
    assert len(network.calls) == 2
    assert network.calls[-1][0].full_url.endswith("/api/flow/gas/stop")


def test_selection_never_uses_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    obj = from_settings(
        {"MG400_ORCH_FURNACE_URL": "http://127.0.0.1:1/furnace"}, identity="review"
    )["furnace"]
    assert isinstance(obj, FurnaceAdapter)
    assert obj._gas_http._opener is obj._http._opener
    handlers = obj._http._opener.handlers
    assert not any(getattr(handler, "proxies", {}) for handler in handlers)
    redirect = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert redirect.redirect_request(None, None, 307, "", {}, "http://other.invalid") is None


GAS_BODY_LIMIT = 256 * 1024


class GasBoundBody(io.BytesIO):
    def __init__(self, payload, *, clock=None, tick=0, chunk=65536, failure=None):
        super().__init__(payload)
        self.clock, self.tick, self.chunk, self.failure = clock, tick, chunk, failure
        self.requests = []
        self.returned = 0

    def read(self, *args):
        raise AssertionError("Gas responses must use bounded read1 calls")

    def read1(self, size):
        assert 0 < size <= 65536
        self.requests.append(size)
        if self.clock is not None:
            self.clock.now += self.tick
        if self.failure is not None:
            raise self.failure
        block = io.BytesIO.read(self, min(size, self.chunk))
        self.returned += len(block)
        return block


class GasBoundOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def gas_bounded_adapter(monkeypatch, responses, *, clock=None):
    obj = FurnaceAdapter(
        "http://127.0.0.1:1/furnace/",
        "offline-gas-response-test-token",
        "bounded-gas",
        timeout=0.25,
    )
    opener = GasBoundOpener(responses)
    monkeypatch.setattr(obj._gas_http, "_opener", opener)
    if clock is not None:
        monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return obj, opener


def gas_padded_payload(value, size):
    encoded = json.dumps(value).encode()
    assert len(encoded) <= size
    return encoded + b" " * (size - len(encoded))


def gas_responses_for_phase(phase, response):
    if phase == "describe":
        return [response]
    return [GasBoundBody(b'{"contract":1}'), response]


def gas_assert_only_expected_requests(network, phase):
    paths = [request.full_url for request, _ in network.calls]
    expected = ["http://127.0.0.1:1/furnace/api/describe"]
    if phase == "stop":
        expected.append("http://127.0.0.1:1/furnace/api/flow/gas/stop")
    assert paths == expected
    assert all(timeout == 0.25 for _, timeout in network.calls)


def test_gas_body_exact_limit_is_accepted_for_discovery_and_stop(monkeypatch):
    expected = {"ok": False, "status": {"locked": True}}
    describe = GasBoundBody(gas_padded_payload({"contract": 1}, GAS_BODY_LIMIT))
    stop = GasBoundBody(gas_padded_payload(expected, GAS_BODY_LIMIT))
    obj, network = gas_bounded_adapter(monkeypatch, [describe, stop])
    assert obj.gas_stop(2) == expected
    for stream in (describe, stop):
        assert stream.closed
        assert stream.returned == GAS_BODY_LIMIT
        assert stream.requests == [65536, 65536, 65536, 65536, 1]
    gas_assert_only_expected_requests(network, "stop")


@pytest.mark.parametrize("phase", ["describe", "stop"])
@pytest.mark.parametrize("http_error", [False, True])
def test_gas_body_over_limit_stops_reading_and_closes(monkeypatch, phase, http_error):
    stream = GasBoundBody(b"x" * (GAS_BODY_LIMIT + 4096))
    response = stream
    if http_error:
        response = urllib.error.HTTPError("http://127.0.0.1:1/", 503, "Unavailable", {}, stream)
    obj, network = gas_bounded_adapter(monkeypatch, gas_responses_for_phase(phase, response))
    with pytest.raises(ServiceError) as raised:
        obj.gas_stop(0)
    assert raised.value.status == (503 if http_error else 502)
    assert ("Unavailable" if http_error else "exceeds 256 KiB") in str(raised.value)
    assert stream.closed and stream.returned == GAS_BODY_LIMIT + 1
    assert stream.requests == [65536, 65536, 65536, 65536, 1]
    gas_assert_only_expected_requests(network, phase)


@pytest.mark.parametrize("phase", ["describe", "stop"])
@pytest.mark.parametrize("http_error", [False, True])
def test_gas_dripping_body_reaches_deadline_between_reads(monkeypatch, phase, http_error):
    clock = SimpleNamespace(now=0.0)
    stream = GasBoundBody(b" " * 1000, clock=clock, tick=0.1, chunk=8)
    response = stream
    if http_error:
        response = urllib.error.HTTPError("http://127.0.0.1:1/", 502, "Bad Gateway", {}, stream)
    obj, network = gas_bounded_adapter(
        monkeypatch, gas_responses_for_phase(phase, response), clock=clock
    )
    with pytest.raises(ServiceError) as raised:
        obj.gas_stop(1)
    assert raised.value.status == (502 if http_error else 0)
    assert ("Bad Gateway" if http_error else "body deadline exceeded") in str(raised.value)
    assert stream.closed and stream.returned == 24 and len(stream.requests) == 3
    assert clock.now == pytest.approx(0.3)
    gas_assert_only_expected_requests(network, phase)


@pytest.mark.parametrize("phase", ["describe", "stop"])
@pytest.mark.parametrize("http_error", [False, True])
def test_gas_body_read_failure_closes_and_preserves_http_error(monkeypatch, phase, http_error):
    stream = GasBoundBody(b"", failure=TimeoutError("offline socket timeout"))
    response = stream
    if http_error:
        response = urllib.error.HTTPError("http://127.0.0.1:1/", 403, "Forbidden", {}, stream)
    obj, network = gas_bounded_adapter(monkeypatch, gas_responses_for_phase(phase, response))
    with pytest.raises(ServiceError) as raised:
        obj.gas_stop(2)
    assert raised.value.status == (403 if http_error else 0)
    assert ("Forbidden" if http_error else "offline socket timeout") in str(raised.value)
    assert stream.closed and len(stream.requests) == 1
    gas_assert_only_expected_requests(network, phase)


def test_gas_bounded_http_error_keeps_json_detail(monkeypatch):
    stream = GasBoundBody(b'{"error":"Denied by existing guard"}')
    error = urllib.error.HTTPError("http://127.0.0.1:1/", 403, "Forbidden", {}, stream)
    obj, network = gas_bounded_adapter(monkeypatch, gas_responses_for_phase("stop", error))
    with pytest.raises(ServiceError, match="Denied by existing guard") as raised:
        obj.gas_stop(2)
    assert raised.value.status == 403 and stream.closed
    gas_assert_only_expected_requests(network, "stop")


def test_gas_bounds_do_not_change_other_adapter_reads_or_skip_own_discovery(monkeypatch):
    description = {"contract": 1, "lease_seconds": 5}
    obj, network = gas_bounded_adapter(
        monkeypatch, [GasBoundBody(b'{"contract":1}'), GasBoundBody(b'{"ok":true}')]
    )
    assert type(obj._http) is ServiceClient
    ordinary = GasBoundOpener([io.BytesIO(gas_padded_payload(description, GAS_BODY_LIMIT + 1))])
    monkeypatch.setattr(obj._http, "_opener", ordinary)
    assert obj.describe() == description
    assert obj.gas_stop(0) == {"ok": True}
    gas_assert_only_expected_requests(network, "stop")
    assert len(ordinary.calls) == 1


@pytest.fixture
def inprocess_furnace(monkeypatch):
    """Run the real owned factory with Flask requests and simulated serial only."""
    from urllib.parse import urlsplit

    import werkzeug.serving

    apps = []
    constructor = FurnaceAdapter.__init__

    class LocalOpener:
        def __init__(self, app):
            self.app = app

        def open(self, request, timeout):
            url = urlsplit(request.full_url)
            assert url.hostname == "127.0.0.1" and url.port == 1
            response = self.app.test_client().open(
                url.path,
                method=request.get_method(),
                data=request.data,
                headers=dict(request.header_items()),
                base_url="http://127.0.0.1:1",
            )
            body = io.BytesIO(response.get_data())
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, response.status_code, "Offline service error", {}, body
                )
            return body

    def server(host, port, app, *, threaded):
        assert (host, port, threaded) == ("127.0.0.1", 0, True)
        apps.append(app)
        return SimpleNamespace(server_port=1)

    def adapter_init(self, *args, **kwargs):
        constructor(self, *args, **kwargs)
        self._http._opener = self._gas_http._opener = LocalOpener(apps[-1])

    monkeypatch.setattr(werkzeug.serving, "make_server", server)
    monkeypatch.setattr(Rehearsal, "_serve", lambda self, server: None)
    monkeypatch.setattr(FurnaceAdapter, "__init__", adapter_init)
    return apps


def test_owned_furnace_gas_stop_keeps_the_existing_heat_water_and_holder(
    tmp_path, inprocess_furnace, monkeypatch
):
    import furnace.app

    original = furnace.app.make_service
    created = []

    def create(*args, **kwargs):
        assert kwargs == {"demo": True}
        service = original(*args, **kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(furnace.app, "make_service", create)
    with Rehearsal(tmp_path, ("furnace",), identity="owned-demo-holder") as demo:
        initial = demo.clients["furnace"]
        assert isinstance(initial, FurnaceAdapter)
        initial.acquire()
        try:
            initial.command("connect_flow")
            initial.command("apply_setpoint", temperature_c=50, ramp_rate_c_per_min=5)
            initial.command("set_gas_flow", channel_index=1, flow_percent=5)
            initial.command("set_gas_valve", channel_index=1, open=True)
            before = initial.state()
            assert before["run_state_code"] == 0 and before["target_c"] == 50
            fresh = demo.new_clients("dashboard-gas-stop")["furnace"]
            result = fresh.gas_stop(1)
            after = fresh.state()
            assert len(created) == 1
            assert initial.identity == "owned-demo-holder"
            assert fresh.identity == "dashboard-gas-stop"
            assert before["lease"]["holder"] == after["lease"]["holder"] == initial.identity
            assert after["run_state_code"] == before["run_state_code"]
            assert after["target_c"] == before["target_c"]
            assert after["flow"]["water_channels"] == before["flow"]["water_channels"]
            assert result["ok"] is True
            channel = result["status"]["gas_channels"][1]
            valve = result["status"]["gas_valves"][1]
            assert channel["actual_percent"] == 0
            assert valve["open"] is False and valve["verified"] is True
            fresh.close()
            with pytest.raises(RuntimeError, match="no longer active"):
                fresh.gas_stop(1)
            # Closing a client does not replace the same active demo resource.
            other = demo.new_clients("another-gas-stop")["furnace"]
            assert other.gas_stop(1)["ok"] is True
            assert len(created) == 1 and other.state()["target_c"] == 50
        finally:
            initial.stop()
            initial.release()
    with pytest.raises(RuntimeError, match="no longer active"):
        other.gas_stop(1)
    assert inprocess_furnace


@pytest.mark.parametrize("closing", ["client", "context"])
def test_gas_stop_rejects_new_calls_and_drains_accepted_calls_before_close(
    tmp_path, inprocess_furnace, monkeypatch, closing
):
    entered, finish, closed = (threading.Event() for _ in range(3))
    calls, results, errors = [], [], []
    original = FurnaceAdapter.gas_stop

    def blocked(self, channel):
        if self.identity == "blocked-gas-stop":
            calls.append(channel)
            entered.set()
            assert finish.wait(4)
            return {"ok": False, "status": {"locked": True, "fault": {"code": "offline"}}}
        return original(self, channel)

    monkeypatch.setattr(FurnaceAdapter, "gas_stop", blocked)
    demo = Rehearsal(tmp_path, ("furnace",), identity="lifetime-owner")
    demo.__enter__()
    fresh = demo.new_clients("blocked-gas-stop")["furnace"]
    saved_call = fresh.gas_stop

    def request():
        try:
            results.append(saved_call(2))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=request)
    closer = None
    worker.start()
    try:
        assert entered.wait(2)
        # This factory call would block if gas_stop retained the context lock.
        other = demo.new_clients("independent-gas-stop")["furnace"]
        assert other.describe()["demo"] is True

        def close():
            if closing == "client":
                fresh.close()
            else:
                demo.__exit__(None, None, None)
            closed.set()

        closer = threading.Thread(target=close)
        closer.start()

        def refuses_new_calls():
            try:
                fresh.describe()
            except RuntimeError as error:
                assert "no longer active" in str(error)
                return True
            return False

        wait_for(refuses_new_calls)
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match="no longer active"):
            saved_call(0)
        if closing == "client":
            assert other.gas_stop(0)["ok"] is True
        else:
            with pytest.raises(RuntimeError, match="no longer active"):
                other.gas_stop(0)
        assert calls == [2]
        finish.set()
        worker.join(2)
        closer.join(2)
        assert not worker.is_alive() and not closer.is_alive()
        assert closed.is_set() and errors == []
        assert results == [{"ok": False, "status": {"locked": True, "fault": {"code": "offline"}}}]
    finally:
        finish.set()
        worker.join(3)
        if closer is not None:
            closer.join(3)
        demo.__exit__(None, None, None)
