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

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


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


@pytest.mark.parametrize("port", ["65536", "invalid"])
def test_cli_rejects_bad_settings_before_construction(port):
    with pytest.raises(SystemExit) as error:
        microscope_main.main(
            ["--port", port],
            environ={},
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


def test_real_focus_configuration_uses_the_fixed_measured_values():
    from microscope.service import MicroscopeConfig

    # The host environment cannot change the focus limits or the clearance.
    config = MicroscopeConfig.from_env(
        {
            "MG400_MICROSCOPE_FOCUS_LIMITS": "-1,1",
            "MG400_MICROSCOPE_SWITCH_FOCUS": "1",
            "MG400_MICROSCOPE_HOME_SWITCH": "1",
        }
    )
    assert (config.focus_limits, config.switch_focus, config.home_switch) == ((0, 10000), 0, False)
    config.validate()
    with pytest.raises(ValueError, match="Measured"):
        MicroscopeConfig().validate()


CAMERA_DEVICES = ",".join(
    f"/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.{port}:1.0-video-index0"
    for port in ("1.2", "1.3", "1.4", "2.1")
)


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        ("640x480@30,640x480@30,640x480@30,640x480@30", (640, 480, 30)),
        ("640x480@30,640x480@30,3840x2160@30,640x480@30", (3840, 2160, 30)),
        ("", (3840, 2160, 30)),
    ],
    ids=["tested-olares-modes", "explicit-4k-side", "default-side-mode"],
)
def test_side_reader_expects_the_configured_side_stream_mode(monkeypatch, modes, expected):
    import cameras.uvc
    from microscope.service import MicroscopeConfig, MicroscopeService

    config = MicroscopeConfig.from_env(
        {
            "MG400_CAMERA_DEVICES": CAMERA_DEVICES,
            "MG400_CAMERA_MODES": modes,
            "MG400_MICROSCOPE_SIDE_STREAM": "camera3",
        }
    )
    assert config.side_mode == expected
    created = []

    def capture(stream, resolution=None, fps=None, **kwargs):
        created.append((stream, resolution, fps))
        return SimpleNamespace()

    monkeypatch.setattr(cameras.uvc, "USBCamera", capture)
    MicroscopeService(config)._side_camera()
    assert created == [("camera3", expected[:2], float(expected[2]))]


def test_side_stream_outside_the_camera_list_fails_closed():
    from microscope.service import MicroscopeConfig

    with pytest.raises(ValueError, match="configured camera stream"):
        MicroscopeConfig.from_env(
            {"MG400_CAMERA_DEVICES": CAMERA_DEVICES, "MG400_MICROSCOPE_SIDE_STREAM": "camera5"}
        )


def test_microscope_dependency_group_covers_the_furnace_import_chain():
    import tomllib
    from pathlib import Path

    # microscope.service imports furnace.recordings, whose package imports the drivers.
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    microscope = project["dependency-groups"]["microscope"]
    assert any(item.startswith("minimalmodbus") for item in microscope)


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


def test_discovery_advertises_focus_zero_for_the_lease_holder():
    from microscope.service import MicroscopeService

    service = MicroscopeService(demo=True)
    zero = service.describe()["commands"]["set_focus_zero"]
    assert zero["params"]["properties"] == {} and zero["any_client"] is False
    response = (
        create_app(service)
        .test_client()
        .post(
            "/api/command", json={"action": "set_focus_zero"}, headers={"X-MG400-Client": "robot"}
        )
    )
    assert response.status_code == 409
    assert service._job is None


@pytest.fixture
def demo_rig(monkeypatch, tmp_path):
    from microscope.service import MicroscopeConfig, MicroscopeService

    monkeypatch.setattr("microscope.paths.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("microscope.paths.CALIBRATION_ROOT", tmp_path / "calibration")
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
    assert service_post(rig, "heartbeat").status_code == 200
    response = service_post(rig, "command", {"action": "set_focus_zero"})
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
        ("objective_nudge", {"steps": 1}),
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


def test_zero_sets_fixed_microstep_then_limits_for_any_lease_holder(demo_rig):
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
    other = service_post(rig, "command", {"action": "set_focus_zero"}, actor="orchestrator-step")
    assert other.status_code == 409
    assert commands == before
    state = dispatch(rig, "focus_move", steps=60)
    assert state["last_command"]["result"] == "ok" and state["focus_position_pulses"] == 60
    state = dispatch(rig, "set_focus_zero")
    assert state["last_command"]["result"] == "ok" and state["focus_position_pulses"] == 0
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
    finished = rig.service.events.since()["events"][-1]["fields"]
    assert finished["params"] == {"id": 2} and finished["client"] == state["last_command"]["client"]
    moves = [item for item in rig.port.commands if item.startswith(("FOCUS MOVE ", "OBJ MOVE "))]
    assert moves == [
        "FOCUS MOVE 60",
        "FOCUS MOVE -60",
        "OBJ MOVE 740",
        "FOCUS MOVE 0",
        "OBJ MOVE 740",
    ]
    assert state["focus_position_pulses"] == 0 and state["focus_zero_known"]


def test_turret_turns_never_cross_the_gap_between_the_last_and_first_objective(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    assert dispatch(rig, "select_objective", id=3)["objective_id"] == 3
    assert dispatch(rig, "select_objective", id=0)["objective_id"] == 0
    turns = [item for item in rig.port.commands if item.startswith("OBJ MOVE ")]
    assert turns == ["OBJ MOVE 740"] * 3 + ["OBJ MOVE -740"] * 3


def test_failed_turn_reports_the_objective_as_unknown(demo_rig, monkeypatch):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    assert dispatch(rig, "select_objective", id=1)["objective_id"] == 1
    factory = rig.service._switcher_factory

    def failing_switcher(*args, **kwargs):
        def fail(*_args, **_kwargs):
            raise RuntimeError("Objective identity could not be confirmed")

        switcher = factory(*args, **kwargs)
        switcher.run = fail
        return switcher

    monkeypatch.setattr(rig.service, "_switcher_factory", failing_switcher)
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "failed"
    assert state["objective_id"] is None
    assert state["objective_offset_steps"] is None


def refused_nudge(rig, steps):
    before = list(rig.port.commands)
    response = service_post(rig, "command", {"action": "objective_nudge", "steps": steps})
    assert response.status_code == 409, response.json
    assert rig.port.commands == before
    return response.json["error"]


def test_nudge_config_uses_the_measured_window_setting():
    from microscope.service import MicroscopeConfig, MicroscopeService

    assert MicroscopeConfig.from_env({}).nudge_steps == 20
    assert MicroscopeConfig.from_env({"MG400_MICROSCOPE_NUDGE_STEPS": "0"}).nudge_steps == 20
    with pytest.raises(ValueError, match="nudge window"):
        MicroscopeConfig(
            focus_limits=(0, 10), switch_focus=0, home_switch=False, nudge_steps=21
        ).validate()
    service = MicroscopeService(MicroscopeConfig(nudge_steps=0))
    with pytest.raises(ServiceError, match="nudges are off") as refused:
        service._check_nudge(1)
    assert refused.value.status == 409
    steps = MicroscopeService(demo=True).describe()["commands"]["objective_nudge"]
    assert steps["params"]["properties"]["steps"]["enum"] == [-10, -1, 1, 10]


def test_describe_names_each_objective():
    from microscope.service import OBJECTIVE_NAMES, MicroscopeService

    objective = MicroscopeService(demo=True).describe()["commands"]["select_objective"]
    ident = objective["params"]["properties"]["id"]
    assert ident["enum"] == [0, 1, 2, 3]
    assert ident["names"] == {str(i): name for i, name in OBJECTIVE_NAMES.items()}
    for i, name in OBJECTIVE_NAMES.items():
        assert f"{i} = {name}" in ident["description"]


def test_nudge_turns_full_steps_at_working_focus_inside_the_window(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    state = rig.service.snapshot()
    assert state["objective_offset_steps"] is None and state["objective_offset_limit_steps"] == 20
    assert "select another objective" in refused_nudge(rig, 1)
    assert dispatch(rig, "select_objective", id=1)["objective_offset_steps"] == 0
    assert dispatch(rig, "focus_move", steps=60)["focus_position_pulses"] == 60
    start = len(rig.port.commands)
    turret = rig.port.sim.objective.position
    for steps, offset in ((10, 10), (10, 20), (-1, 19), (-10, 9), (-10, -1), (-10, -11)):
        state = dispatch(rig, "objective_nudge", steps=steps)
        assert state["last_command"]["result"] == "ok", state
        assert state["objective_id"] == 1 and state["objective_offset_steps"] == offset
        assert state["last_result"] == {"objective_id": 1, "objective_offset_steps": offset}
    assert rig.port.sim.objective.position == turret - 44
    assert not rig.port.sim.objective.enabled
    assert state["focus_position_pulses"] == 60 and state["focus_zero_known"]
    sent = rig.port.commands[start:]
    assert not [line for line in sent if line.startswith("FOCUS MOVE")]
    assert [line for line in sent if line.startswith("OBJ MOVE")] == [
        "OBJ MOVE 40",
        "OBJ MOVE 40",
        "OBJ MOVE -4",
        "OBJ MOVE -40",
        "OBJ MOVE -40",
        "OBJ MOVE -40",
    ]
    assert sent[:7] == [
        "STATUS",
        "OBJ SPEED 40",
        "OBJ ENABLE",
        "STATUS",
        "OBJ MOVE 40",
        "STATUS",
        "OBJ DISABLE",
    ]
    assert "outside ±20 full steps" in refused_nudge(rig, -10)
    assert dispatch(rig, "objective_nudge", steps=-1)["objective_offset_steps"] == -12
    assert "-22, outside" in refused_nudge(rig, -10)


def test_select_objective_resets_the_offset_only_after_a_turn(demo_rig):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    assert dispatch(rig, "select_objective", id=1)["objective_offset_steps"] == 0
    assert dispatch(rig, "objective_nudge", steps=10)["objective_offset_steps"] == 10
    # The objective is already in place: nothing turns, so the count stays.
    state = dispatch(rig, "select_objective", id=1)
    assert state["last_result"]["switches"] == [] and state["objective_offset_steps"] == 10
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 2 and state["objective_offset_steps"] == 0
    assert dispatch(rig, "objective_nudge", steps=-10)["objective_offset_steps"] == -10
    # The side camera finds objective 3 in place, not the counted objective 2.
    with rig.port.lock:
        rig.port.sim.objective.position = 3 * 740
    state = dispatch(rig, "select_objective", id=3)
    assert state["last_result"]["switches"] == [] and state["objective_id"] == 3
    assert state["objective_offset_steps"] is None


def test_failed_nudge_reports_the_turret_offset_as_unknown(demo_rig, monkeypatch):
    from microscope.motors import CommandRejected

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    assert dispatch(rig, "select_objective", id=1)["objective_offset_steps"] == 0
    send_and_wait = rig.service._motor.send_and_wait

    def rejected(command, *args, **kwargs):
        if command.startswith("OBJ MOVE"):
            raise CommandRejected(f"{command}: ERR OBJ BUSY")
        return send_and_wait(command, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rig.service._motor, "send_and_wait", rejected)
        state = dispatch(rig, "objective_nudge", steps=1)
    assert state["last_command"]["result"] == "failed"
    assert state["objective_id"] is None and state["objective_offset_steps"] is None
    assert service_post(rig, "release").status_code == 200
    assert rig.service._stop_done.wait(1)
    establish_manual_reference(rig)
    assert "select another objective" in refused_nudge(rig, 1)


@pytest.mark.parametrize("steps", [2, 0, -20, True, "1", None])
def test_nudge_accepts_only_the_four_step_sizes(demo_rig, steps):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    response = service_post(rig, "command", {"action": "objective_nudge", "steps": steps})
    assert response.status_code == 400
    assert service_post(rig, "command", {"action": "objective_nudge"}).status_code == 400


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


@pytest.mark.parametrize("route", ["release"])
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
    assert service_post(rig, "heartbeat").status_code == 200
    response = service_post(rig, "command", {"action": "set_focus_zero"})
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
    assert service_post(rig, "release").status_code == 200
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
    assert service_post(rig, "release").status_code == 200
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
    assert service_post(rig, "release").status_code == 200
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


@pytest.mark.parametrize("trigger", ["stop", "expiry_without_poll", "expired_same_id_reacquire"])
def test_late_autofocus_enable_cannot_reenergize_after_stop(demo_rig, monkeypatch, trigger):
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
        if trigger == "stop":
            assert (
                service_post(rig, "stop", {"wait_s": 1}, actor="other").json["stop"] == "confirmed"
            )
            assert not rig.port.sim.focus.enabled
        else:
            rig.clock.now = 3.0
            if trigger == "expired_same_id_reacquire":
                assert service_post(rig, "heartbeat").status_code == 200
                assert rig.service._stop_done.wait(1)
                assert job.cancel.is_set()
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


def side_frame(height, width):
    import cv2
    import numpy as np

    frame = np.full((height, width), 255, np.uint8)
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 80
    )
    frame[60:140, 60:140] = marker
    return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)


def stopped_reader(**_kwargs):
    try:
        raise RuntimeError("Side-camera frame size differs from its reference size")
    except RuntimeError as error:
        raise RuntimeError("USB camera acquisition stopped") from error


def busy_reader():
    raise RuntimeError("Previous side-camera reader is still stopping")


@pytest.mark.parametrize(
    ("attribute", "replacement", "message"),
    [
        (
            "get_frame_packet",
            lambda **_: (1, 0.0, side_frame(480, 640)),
            "side frame 640x480 does not match the 200x200 references",
        ),
        (
            "get_frame_packet",
            lambda **_: (1, 0.0, side_frame(200, 200) * 0),
            "Objective identification failed: black",
        ),
        ("get_frame_packet", lambda **_: None, "No fresh side-camera frame"),
        ("get_frame_packet", stopped_reader, "unavailable: Side-camera frame size differs"),
        ("open", busy_reader, "unavailable: Previous side-camera reader is still stopping"),
    ],
    ids=["size-mismatch", "black-frame", "no-frame", "stopped-reader", "busy-reader"],
)
def test_unidentified_objective_refuses_before_motion_without_fault(
    demo_rig, monkeypatch, attribute, replacement, message
):
    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    before = len(rig.port.commands)
    with monkeypatch.context() as patch:
        patch.setattr(rig.service._side, attribute, replacement)
        state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "failed"
    assert message in state["last_command"]["message"]
    assert not state["locked"] and state["fault"] is None and state["focus_zero_known"]
    assert not [line for line in rig.port.commands[before:] if " MOVE " in line]
    assert not rig.service._side.is_open
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "ok" and state["objective_id"] == 2
    assert not rig.service._side.is_open and not state["cameras"]["side"]


def test_select_objective_uses_side_frames_scaled_from_the_reference_size(demo_rig, monkeypatch):
    import cv2

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    read = rig.service._side.get_frame_packet

    def half_size(*args, **kwargs):
        packet = read(*args, **kwargs)
        if packet is None:
            return None
        frame_id, received_at, frame = packet
        return frame_id, received_at, cv2.resize(frame, (100, 100), interpolation=cv2.INTER_AREA)

    monkeypatch.setattr(rig.service._side, "get_frame_packet", half_size)
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 2


def test_state_reports_the_side_reader_only_while_it_runs(demo_rig):
    rig = demo_rig()
    assert rig.service.snapshot()["cameras"] == {"microscope": True, "side": False}
    rig.service._side.open()
    assert rig.service.snapshot()["cameras"]["side"] is True
    rig.service._side.last_error = RuntimeError("Side-camera frame size differs")
    assert rig.service.snapshot()["cameras"]["side"] is False


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


@pytest.mark.parametrize("route", ["release"])
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


@pytest.mark.parametrize("route", ["heartbeat"])
def test_acquire_crossing_handoff_deadline_cannot_preserve_reference(demo_rig, monkeypatch, route):
    rig = demo_rig()
    establish_manual_reference(rig)
    rig.clock.now = 1
    assert service_post(rig, "release").status_code == 200
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


def test_heartbeat_renews_live_job_deadline_before_queued_enable(demo_rig, monkeypatch):
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
        rig.clock.now = 2.5
        assert service_post(rig, "renew").status_code == 200
        rig.clock.now = 3.1
        assert not job.cancel.is_set()
        resume.set()
        state = wait_command(rig, response.json["command_id"])
        assert state["last_command"]["result"] == "ok", state
        assert state["focus_zero_known"]
    finally:
        resume.set()
        assert job.done.wait(2)


def test_expired_job_token_is_irreversible_and_wait_preserves_settle_time(monkeypatch):
    from microscope import service as module

    clock = SimpleNamespace(now=0.0, wall=0.0)
    token = module._AuthorityCancel(lambda: clock.now, 1.0)

    class Event:
        marked = False

        def set(self):
            self.marked = True

        def is_set(self):
            return self.marked

        def wait(self, seconds):
            clock.wall += seconds
            token.renew(2.0)

    token._event = Event()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.wall))
    assert token.wait(0.2) is False and clock.wall == pytest.approx(0.2)
    clock.now = 2.0
    assert token.is_set()
    token.renew(100.0)
    assert token.is_set() and token.wait(0.2) is True


def test_command_worker_start_failure_returns_json_and_stops_outputs(demo_rig, monkeypatch):
    import threading

    rig = demo_rig()
    establish_manual_reference(rig)
    acquire_automation(rig)
    start = threading.Thread.start

    def fail_worker(thread):
        if thread.name == "microscope-command":
            raise RuntimeError("offline thread creation failure")
        return start(thread)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_worker)
        response = service_post(rig, "command", {"action": "focus_move", "steps": 1})
    assert response.status_code == 503 and set(response.json) == {"error"}
    assert rig.service._stop_done.wait(1)
    state = rig.service.snapshot()
    assert state["locked"] and not state["focus_zero_known"] and not state["busy"]
    assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled


@pytest.mark.parametrize("outcome", ["confirmed", "pending", "unconfirmed"])
def test_stop_wait_rechecks_rearmed_cleanup_with_original_budget(monkeypatch, outcome):
    import threading
    import time

    import microscope.service as module

    now, waits, replies = [0.0], [], []
    first_woke, resume_waiter = threading.Event(), threading.Event()
    second_entered, finish_second = threading.Event(), threading.Event()
    caller = None

    class Motor:
        calls = 0
        outputs_enabled = True

        def safe_stop_disable(self):
            self.calls += 1
            if self.calls == 2:
                second_entered.set()
                assert finish_second.wait(5)
                if outcome == "unconfirmed":
                    raise RuntimeError("Offline cleanup acknowledgement lost")
            self.outputs_enabled = False

    class Completion:
        def __init__(self):
            self.event = threading.Event()

        def set(self):
            self.event.set()

        def clear(self):
            self.event.clear()

        def is_set(self):
            return self.event.is_set()

        def wait(self, timeout=None):
            if threading.current_thread() is not caller:
                return self.event.wait(timeout)
            waits.append(timeout)
            if len(waits) == 1:
                assert self.event.wait(5)
                first_woke.set()
                assert resume_waiter.wait(5)
                return True
            if outcome == "pending":
                # The newer cleanup stays blocked through the original deadline.
                now[0] = 1.0
                return False
            finish_second.set()
            assert self.event.wait(5)
            now[0] = 0.5
            return True

    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time, sleep=time.sleep)
    )
    motor = Motor()
    service = module.MicroscopeService(demo=True, motor=motor)
    service._stop_done = Completion()
    service._stop_done.set()
    caller = threading.Thread(target=lambda: replies.append(service.stop("operator", 1)))
    caller.start()
    try:
        assert first_woke.wait(5)
        assert not motor.outputs_enabled and service._stop_outcome == "confirmed"
        now[0] = 0.25
        # The cancellation handler uses this same entry point after unwinding.
        assert service.stop("command-cancel", 0) == "pending"
        assert second_entered.wait(5)
        resume_waiter.set()
        caller.join(5)
        assert not caller.is_alive()
        assert replies == [outcome]
        assert waits == [1.0, 0.75]  # Never start a fresh one-second wait.
        assert not motor.outputs_enabled
        if outcome == "pending":
            assert not service._stop_done.is_set()
        if outcome == "unconfirmed":
            assert service._fault["code"] == "stop"
    finally:
        resume_waiter.set()
        finish_second.set()
        caller.join(5)
        service._stop_thread.join(5)
        assert not service._stop_thread.is_alive()
