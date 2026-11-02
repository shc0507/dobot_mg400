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


def test_automation_discovery_does_not_advertise_operator_zero():
    from microscope.service import MicroscopeService

    service = MicroscopeService(demo=True)
    assert "set_focus_zero" not in service.describe()["commands"]
    response = (
        create_app(service)
        .test_client()
        .post(
            "/api/command", json={"action": "set_focus_zero"}, headers={"X-MG400-Client": "robot"}
        )
    )
    assert response.status_code == 400
    assert service._job is None


@pytest.fixture
def demo_rig(monkeypatch, tmp_path):
    from microscope.service import MicroscopeConfig, MicroscopeService

    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    services = []

    def create(*, home_switch=False, **kwargs):
        clock = SimpleNamespace(now=0.0)
        service = MicroscopeService(
            MicroscopeConfig(home_switch=home_switch),
            demo=True,
            clock=lambda: clock.now,
            **kwargs,
        )
        services.append(service)
        service.start(background=False)
        client = create_app(service).test_client()
        return SimpleNamespace(service=service, client=client, clock=clock, port=service._demo_port)

    yield create
    for service in services:
        service.close()


def service_post(rig, route, data=None, actor="operator"):
    return rig.client.post(
        "/api/" + route, json={} if data is None else data, headers={"X-MG400-Client": actor}
    )


def wait_command(rig, command_id, *, timeout=5):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = rig.service.snapshot()
        if (
            not state["busy"]
            and state["last_command"]
            and state["last_command"]["id"] == command_id
        ):
            return state
        time.sleep(0.002)
    pytest.fail(f"Offline microscope command did not finish: {rig.service.snapshot()}")


def establish_manual_reference(rig):
    assert service_post(rig, "manual/heartbeat").status_code == 200
    response = service_post(rig, "manual/set_focus_zero", {"confirm": True})
    assert response.status_code == 202, response.json
    state = wait_command(rig, response.json["command_id"])
    assert state["last_command"]["result"] == "ok", state
    assert state["focus_zero_known"]
    return state


def acquire_automation(rig):
    assert service_post(rig, "heartbeat").status_code == 200


def dispatch(rig, action, **args):
    response = service_post(rig, "command", {"action": action, **args})
    assert response.status_code == 202, response.json
    return wait_command(rig, response.json["command_id"])


def test_demo_contract_conformance_and_unleased_expiry(demo_rig):
    from contract_helpers import check_contract

    rig = demo_rig()

    def advance(seconds):
        rig.clock.now += seconds

    check_contract(rig.client, advance=advance, expiry_count=lambda: rig.service.expiry_count)
    assert rig.service._stop_done.wait(1)
    assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled


@pytest.mark.parametrize(
    "action,args",
    [
        ("focus_move", {"steps": 1}),
        ("autofocus", {"mode": "full"}),
        ("select_objective", {"id": 1}),
    ],
)
def test_motion_refuses_unknown_focus_zero(demo_rig, action, args):
    rig = demo_rig()
    acquire_automation(rig)
    before = list(rig.port.commands)
    response = service_post(rig, "command", {"action": action, **args})
    assert response.status_code == 409 and "focus position unknown" in response.json["error"]
    assert rig.port.commands == before
    assert not rig.service.snapshot()["focus_zero_known"]


def test_manual_zero_sets_fixed_microstep_then_limits_and_blocks_automation_zero(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    commands = rig.port.commands
    assert (
        commands.index("MICRO 4")
        < commands.index("FOCUS ZERO")
        < commands.index("FOCUS LIMIT -200 200")
    )
    acquire_automation(rig)
    before = list(commands)
    assert service_post(rig, "command", {"action": "set_focus_zero"}).status_code == 400
    assert service_post(rig, "manual/set_focus_zero", {"confirm": True}).status_code == 409
    assert commands == before
    state = dispatch(rig, "focus_move", steps=60)
    assert state["last_command"]["result"] == "ok" and state["focus_position_pulses"] == 60


def test_real_autofocus_algorithm_runs_with_generated_camera_and_stays_in_limits(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    state = dispatch(rig, "autofocus", mode="full")
    assert state["last_command"]["result"] == "ok", state
    assert -200 <= state["focus_position_pulses"] <= 200
    assert state["last_result"]["measurement"]["score"] > 0
    assert not rig.port.sim.objective.enabled


def test_turret_turns_follow_clearance_moves_and_fresh_reference_checks(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    assert dispatch(rig, "focus_move", steps=60)["last_command"]["result"] == "ok"
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 2 and len(state["last_result"]["switches"]) == 2
    moves = [item for item in rig.port.commands if item.startswith(("FOCUS MOVE ", "OBJ MOVE "))]
    assert moves == [
        "FOCUS MOVE 60",
        "FOCUS MOVE -60",
        "OBJ MOVE 740",
        "FOCUS MOVE 0",
        "OBJ MOVE 740",
    ]
    assert state["focus_position_pulses"] == 0 and state["focus_zero_known"]


def test_snapshot_is_exclusive_private_png_in_run_store(demo_rig, tmp_path):
    import stat

    import cv2

    rig = demo_rig()
    acquire_automation(rig)
    state = dispatch(rig, "snapshot", path="experiment/frame.png")
    assert state["last_command"]["result"] == "ok", state
    path = tmp_path / "runs/experiment/frame.png"
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o037 == 0
    assert cv2.imread(str(path)).shape == (200, 200, 3)
    response = service_post(rig, "command", {"action": "snapshot", "path": "../outside.png"})
    assert response.status_code == 400 and not (tmp_path / "outside.png").exists()


def test_home_requires_configured_switch_and_never_substitutes_manual_zero(demo_rig):
    missing = demo_rig()
    acquire_automation(missing)
    assert service_post(missing, "command", {"action": "home_focus"}).status_code == 409
    rig = demo_rig(home_switch=True)
    acquire_automation(rig)
    state = dispatch(rig, "home_focus")
    assert state["last_command"]["result"] == "ok" and state["focus_zero_known"], state
    assert "FOCUS HOME" in rig.port.commands and "FOCUS ZERO" not in rig.port.commands


@pytest.mark.parametrize("route", ["release", "manual/release"])
def test_active_release_cancels_old_job_before_same_client_can_reacquire(
    demo_rig, monkeypatch, route
):
    import threading

    rig = demo_rig()
    entered, resume = threading.Event(), threading.Event()
    original = rig.service._reference_focus

    def delayed(job):
        entered.set()
        assert resume.wait(2)
        return original(job)

    monkeypatch.setattr(rig.service, "_reference_focus", delayed)
    assert service_post(rig, "manual/heartbeat").status_code == 200
    response = service_post(rig, "manual/set_focus_zero", {"confirm": True})
    assert response.status_code == 202 and entered.wait(1)
    job = rig.service._job
    try:
        assert service_post(rig, route, actor="other").status_code == 409
        assert not job.cancel.is_set()
        assert service_post(rig, route).status_code == 200
        assert job.cancel.is_set() and rig.service._stop_done.wait(1)
        before = list(rig.port.commands)
        assert service_post(rig, "heartbeat").status_code == 200
        assert service_post(rig, "command", {"action": "snapshot"}).status_code == 409
        resume.set()
        state = wait_command(rig, response.json["command_id"])
        assert state["last_command"]["result"] == "stopped" and not state["focus_zero_known"]
        assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled
        assert "FOCUS ZERO" not in rig.port.commands[len(before) :]
        assert not any(line.endswith("ENABLE") for line in rig.port.commands[len(before) :])
    finally:
        resume.set()
        assert job.done.wait(2)


def test_idle_manual_reference_handoff_to_different_automation_client(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 1
    before = list(rig.port.commands)
    assert service_post(rig, "manual/release").status_code == 200
    assert rig.service.snapshot()["focus_zero_known"]
    assert "FOCUS DISABLE" not in rig.port.commands[len(before) :]
    rig.clock.now = 2.99
    assert service_post(rig, "heartbeat", actor="orchestrator-step").status_code == 200
    response = service_post(
        rig, "command", {"action": "focus_move", "steps": 10}, actor="orchestrator-step"
    )
    assert response.status_code == 202
    state = wait_command(rig, response.json["command_id"])
    assert state["last_command"]["result"] == "ok" and state["focus_position_pulses"] == 10


@pytest.mark.parametrize("actor", ["operator", "orchestrator-step"])
@pytest.mark.parametrize("deadline", [3.0, 3.1])
def test_handoff_acquisition_cannot_revive_reference_at_expiry_without_poll(
    demo_rig, actor, deadline
):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 1
    assert service_post(rig, "manual/release").status_code == 200
    rig.clock.now = deadline
    assert service_post(rig, "heartbeat", actor=actor).status_code == 200
    assert rig.service._stop_done.wait(1)
    assert rig.service.expiry_count == 1
    assert not rig.service.snapshot()["focus_zero_known"] and not rig.port.sim.focus.enabled
    response = service_post(rig, "command", {"action": "focus_move", "steps": 1}, actor=actor)
    assert response.status_code == 409


def test_idle_reference_expiry_and_any_client_stop_always_disable(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 3.0
    rig.client.get("/api/state")
    assert rig.service._stop_done.wait(1)
    assert not rig.service.snapshot()["focus_zero_known"] and not rig.port.sim.focus.enabled
    establish_manual_reference(rig)
    response = service_post(rig, "stop", {"wait_s": 1}, actor="someone-else")
    assert response.status_code == 200 and response.json["stop"] == "confirmed"
    assert not response.json["state"]["focus_zero_known"]
    assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled


def test_unsafe_idle_release_never_hands_off_a_moving_axis(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    with rig.port.lock:
        # Change only the simulator, representing an unexpected physical state.
        rig.port.sim.objective.enabled = True
        rig.port.sim.objective.moving = True
        rig.port.sim.objective.remaining = 100000
    assert service_post(rig, "manual/release").status_code == 200
    assert rig.service._stop_done.wait(1)
    assert not rig.service.snapshot()["focus_zero_known"]
    assert not rig.port.sim.objective.enabled


def test_reset_revokes_reference_and_never_rezeros_automatically(demo_rig):
    import time

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    before = len(rig.port.commands)
    with rig.port.lock:
        rig.port.emit(rig.port.sim.reset())
    deadline = time.monotonic() + 1
    while rig.service._motor.fault is None:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    assert not rig.service.snapshot()["focus_zero_known"]
    response = service_post(rig, "command", {"action": "focus_move", "steps": 1})
    assert response.status_code == 409
    assert "FOCUS ZERO" not in rig.port.commands[before:]
    assert "FOCUS ENABLE" not in rig.port.commands[before:]


def test_late_autofocus_enable_cannot_reenergize_after_stop(demo_rig, monkeypatch):
    import threading

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    entered, resume = threading.Event(), threading.Event()
    enable = rig.service._motor.focus_enable

    def delayed_enable(**kwargs):
        entered.set()
        assert resume.wait(2)
        return enable(**kwargs)

    monkeypatch.setattr(rig.service._motor, "focus_enable", delayed_enable)
    response = service_post(rig, "command", {"action": "autofocus", "mode": "local"})
    assert response.status_code == 202 and entered.wait(1)
    job = rig.service._job
    try:
        assert service_post(rig, "stop", {"wait_s": 1}, actor="other").json["stop"] == "confirmed"
        assert not rig.port.sim.focus.enabled
        after_stop = len(rig.port.commands)
        resume.set()
        state = wait_command(rig, response.json["command_id"])
        assert state["last_command"]["result"] == "stopped"
        assert "FOCUS ENABLE" not in rig.port.commands[after_stop:]
        assert not rig.port.sim.focus.enabled
    finally:
        resume.set()
        assert job.done.wait(2)


@pytest.mark.parametrize("trigger", ["stop", "expiry", "release"])
def test_active_focus_completion_is_interrupted_and_outputs_disabled(
    demo_rig, monkeypatch, trigger
):
    import time

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    monkeypatch.setattr(rig.port.sim, "advance", lambda seconds: [])
    response = service_post(rig, "command", {"action": "focus_move", "steps": 100})
    assert response.status_code == 202
    deadline = time.monotonic() + 1
    while not rig.port.sim.focus.moving:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    if trigger == "stop":
        assert service_post(rig, "stop", {"wait_s": 1}, actor="other").json["stop"] == "confirmed"
    elif trigger == "expiry":
        rig.clock.now = 3
        rig.service.lease.poll()
    else:
        assert service_post(rig, "release").status_code == 200
    state = wait_command(rig, response.json["command_id"])
    assert state["last_command"]["result"] == "stopped", state
    assert not state["focus_zero_known"]
    assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled
    assert not rig.port.sim.focus.moving


def test_unknown_stop_outcome_locks_and_never_claims_confirmation(demo_rig, monkeypatch):
    rig = demo_rig()
    establish_manual_reference(rig)
    with monkeypatch.context() as patch:

        def failed_cleanup():
            raise RuntimeError("offline acknowledgement lost")

        patch.setattr(rig.service._motor, "safe_stop_disable", failed_cleanup)
        response = service_post(rig, "stop", {"wait_s": 1}, actor="other")
        assert response.json["stop"] == "unconfirmed"
        state = response.json["state"]
        assert state["locked"] and state["fault"] and not state["focus_zero_known"]
        acquire_automation(rig)
        assert service_post(rig, "command", {"action": "snapshot"}).status_code == 409
    assert service_post(rig, "stop", {"wait_s": 1}, actor="other").json["stop"] == "confirmed"
    assert rig.service.snapshot()["locked"]


def test_demo_marker_correction_preserves_positive_pulse_leftward_feedback(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    with rig.port.lock:
        rig.port.sim.objective.position = 80
    state = dispatch(rig, "select_objective", id=1)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 1
    assert abs(rig.port.sim.objective.position - 740) <= 40
    assert any(line.startswith("OBJ MOVE -") for line in rig.port.commands)


def test_real_start_with_missing_measurements_never_opens_devices(monkeypatch):
    import serial

    from microscope.service import MicroscopeService

    monkeypatch.setattr(serial, "Serial", lambda **_: pytest.fail("real serial opened"))
    service = MicroscopeService()
    with pytest.raises(ValueError, match="Measured"):
        service.start()
    assert service._motor is service._camera is service._side is None
    service.close()


def test_cli_can_run_and_close_actual_demo_service_without_a_socket():
    server = SimpleNamespace(run=lambda: None, close=lambda: None)
    assert microscope_main.main(["--demo"], environ={}, server_factory=lambda *a, **kw: server) == 0


@pytest.mark.parametrize("route", ["release", "manual/release"])
def test_handoff_deadline_is_anchored_before_remaining_interval_is_sampled(
    demo_rig, monkeypatch, route
):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 1
    snapshot = rig.service._lease.snapshot

    def delayed_snapshot():
        value = snapshot()
        rig.clock.now += 0.25
        return value

    with monkeypatch.context() as patch:
        patch.setattr(rig.service._lease, "snapshot", delayed_snapshot)
        assert service_post(rig, route).status_code == 200
    assert rig.service._handoff_deadline <= 3.0
    rig.clock.now = 3
    assert service_post(rig, "heartbeat", actor="next").status_code == 200
    assert rig.service._stop_done.wait(1)
    assert not rig.service.snapshot()["focus_zero_known"]


@pytest.mark.parametrize("route", ["heartbeat", "manual/heartbeat"])
def test_acquire_crossing_handoff_deadline_cannot_preserve_reference(demo_rig, monkeypatch, route):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 1
    assert service_post(rig, "manual/release").status_code == 200
    rig.clock.now = 2.99
    acquire = rig.service._lease.acquire

    def crosses_deadline(client):
        value = acquire(client)
        rig.clock.now = 3.01
        return value

    with monkeypatch.context() as patch:
        patch.setattr(rig.service._lease, "acquire", crosses_deadline)
        assert service_post(rig, route, actor="next").status_code == 200
    assert rig.service._stop_done.wait(1)
    assert not rig.service.snapshot()["focus_zero_known"] and not rig.port.sim.focus.enabled
    assert rig.service.expiry_count == 1


@pytest.mark.parametrize("mode", ["full", "local"])
def test_autofocus_from_clearance_at_limit_endpoint_uses_a_bounded_window(demo_rig, mode):
    from dataclasses import replace

    rig = demo_rig()
    rig.service.config = replace(rig.service.config, focus_limits=(-120, 0)).validate()
    establish_manual_reference(rig)
    acquire_automation(rig)
    state = dispatch(rig, "autofocus", mode=mode)
    assert state["last_command"]["result"] == "ok", state
    position = 0
    for text in rig.port.commands:
        if text.startswith("FOCUS MOVE "):
            position += int(text.split()[-1])
            assert -120 <= position <= 0
    assert position == state["focus_position_pulses"]
