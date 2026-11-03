"""Supervised calibration uses scripted people and in-memory firmware only."""

import csv
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from microscope import experiments
from microscope.aruco_switch import SwitchSettings
from microscope.calibrate import (
    _V4L2_CAP_DEVICE_CAPS,
    _V4L2_CAP_STREAMING,
    _V4L2_CAP_VIDEO_CAPTURE,
    _V4L2_CAPABILITY,
    _VIDIOC_QUERYCAP,
    NativeSideCamera,
    NativeUVCCapture,
    ReferenceSettings,
    SupervisedSession,
    validated_focus_settings,
)
from microscope.motors import MotorController
from microscope.service import _DemoSerial
from microscope.side_capture import SideCaptureCancelled


class ScriptedOperator:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def require_interactive(self):
        return None

    def confirm(self, reason, check):
        self.prompts.append(reason)
        check()
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer != "CLEAR":
            raise RuntimeError("Operator confirmation was refused")
        check()


class FakeCamera:
    is_open = True
    last_error = None
    stabilization_enabled = False
    sdk_camera = SimpleNamespace(get_AutoExpoEnable=lambda: False)

    def __init__(self):
        self.packet = (1, 1.0, np.zeros((4, 4, 3), np.uint8))

    def get_frame_packet(self, **_kwargs):
        if isinstance(self.packet, BaseException):
            raise self.packet
        return self.packet

    def get_frame(self, **kwargs):
        packet = self.get_frame_packet(**kwargs)
        return None if packet is None else packet[2]

    def set_stabilization(self, value):
        self.stabilization_enabled = value

    def set_auto_exposure(self, value):
        pass


@pytest.fixture
def supervised_rig():
    rigs = []

    def make(*answers, home=False, reference=None):
        port = _DemoSerial(home_switch=home)
        motor = MotorController(serial_factory=lambda **_: port)
        motor.connect("/dev/serial/by-id/offline-scripted", reset_seconds=0)
        operator = ScriptedOperator(*answers)
        camera = FakeCamera()
        session = SupervisedSession(
            motor,
            camera,
            None,
            reference or ReferenceSettings(-8000, 8000, 0, home),
            operator,
        )
        rig = SimpleNamespace(
            session=session, raw=motor, port=port, operator=operator, camera=camera
        )
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.session.cancel_event.set()
        if rig.raw.connected:
            rig.raw.safe_stop_disable()
        rig.raw.disconnect()


def test_reference_coordinates_scale_inward_without_rounding_clearance():
    settings = ReferenceSettings(-5, 5, 0, False)
    assert settings.scaled(1) == (-1, 1, 0)
    assert settings.scaled(2) == (-2, 2, 0)
    assert settings.scaled(16) == (-20, 20, 0)
    with pytest.raises(ValueError, match="no representable"):
        ReferenceSettings(-3, 3, 0, False).scaled(1)
    with pytest.raises(ValueError, match="exactly representable"):
        ReferenceSettings(-20, 20, 3, True).scaled(1)
    with pytest.raises(ValueError, match="must be zero"):
        ReferenceSettings(-20, 20, 4, False)


def test_measured_reference_configuration_has_no_hardware_defaults():
    for missing in (
        "MG400_MICROSCOPE_FOCUS_LIMITS",
        "MG400_MICROSCOPE_SWITCH_FOCUS",
        "MG400_MICROSCOPE_HOME_SWITCH",
    ):
        settings = {
            "MG400_MICROSCOPE_FOCUS_LIMITS": "-8000,8000",
            "MG400_MICROSCOPE_SWITCH_FOCUS": "0",
            "MG400_MICROSCOPE_HOME_SWITCH": "0",
        }
        settings.pop(missing)
        with pytest.raises(ValueError, match="required"):
            ReferenceSettings.from_env(settings)


def test_microstep_and_focus_disable_each_require_clearance_and_new_confirmation(supervised_rig):
    rig = supervised_rig("CLEAR", "CLEAR", "CLEAR")
    motor = rig.session.motor
    assert not rig.operator.prompts  # construction performs no reference or movement
    motor.focus_move(40)
    before = len(rig.port.commands)
    motor.set_microstep(4)
    commands = rig.port.commands[before:]
    assert commands.index("FOCUS MOVE -40") < commands.index("MICRO 4")
    assert commands.index("MICRO 4") < commands.index("STOP ALL") < commands.index("FOCUS ZERO")
    assert "FOCUS LIMIT -8000 8000" in commands
    assert len(rig.operator.prompts) == 2
    motor.set_microstep(4)
    assert len(rig.operator.prompts) == 2
    motor.focus_move(16)
    before = len(rig.port.commands)
    motor.focus_disable()
    commands = rig.port.commands[before:]
    assert commands.index("FOCUS MOVE -16") < commands.index("FOCUS DISABLE")
    assert (
        commands.index("FOCUS DISABLE") < commands.index("STOP ALL") < commands.index("FOCUS ZERO")
    )
    assert len(rig.operator.prompts) == 3
    state = rig.raw.read_status()
    assert state.zeroed and state.limited and state.focus.position == 0


@pytest.mark.parametrize("answer", ["no", TimeoutError("scripted confirmation timeout")])
def test_reference_refusal_or_timeout_disables_and_never_rezeros_or_retries(supervised_rig, answer):
    rig = supervised_rig("CLEAR", answer)
    rig.session.motor.focus_move(20)
    before = len(rig.port.commands)
    with pytest.raises((RuntimeError, TimeoutError)):
        rig.session.motor.set_microstep(4)
    commands = rig.port.commands[before:]
    assert "FOCUS MOVE -20" in commands and "MICRO 4" in commands
    assert "FOCUS ZERO" not in commands and "FOCUS HOME" not in commands
    assert "STOP ALL" in commands and "OBJ DISABLE" in commands and "FOCUS DISABLE" in commands
    assert rig.session.cancel_event.is_set()
    before = len(rig.port.commands)
    with pytest.raises(SideCaptureCancelled):
        rig.session.motor.focus_move(1)
    assert len(rig.port.commands) == before and len(rig.operator.prompts) == 2


def test_home_switch_reference_never_falls_back_to_zero(supervised_rig):
    rig = supervised_rig("CLEAR", home=True)
    rig.session.motor.focus_move(1)
    assert "FOCUS HOME" in rig.port.commands and "FOCUS ZERO" not in rig.port.commands
    assert rig.raw.read_status().zeroed


@pytest.mark.parametrize("loss", ["reset", "disconnect", "reference"])
def test_session_or_reference_loss_aborts_without_automatic_recovery(supervised_rig, loss):
    rig = supervised_rig("CLEAR")
    rig.session.motor.focus_move(1)
    before = len(rig.port.commands)
    if loss == "reset":
        rig.port.emit(rig.port.sim.reset())
        deadline = time.monotonic() + 1
        while not rig.raw.fault and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        assert rig.raw.fault
    elif loss == "disconnect":
        rig.raw.disconnect()
    else:
        rig.raw.focus_disable()
    with pytest.raises(SideCaptureCancelled):
        rig.session.motor.focus_move(1)
    assert rig.session.cancel_event.is_set()
    assert "FOCUS ZERO" not in rig.port.commands[before:]
    assert len(rig.operator.prompts) == 1


def test_focus_limits_and_unrestricted_reference_commands_are_refused(supervised_rig):
    rig = supervised_rig("CLEAR")
    rig.session.motor.focus_move(1)
    before = len(rig.port.commands)
    with pytest.raises(ValueError, match="Raw commands"):
        rig.session.motor.send_and_wait("FOCUS ZERO", "OK FOCUS ZERO")
    assert len(rig.port.commands) == before
    with pytest.raises(ValueError, match="measured limits"):
        rig.session.motor.focus_move(100000)
    assert not any(command == "FOCUS MOVE 100000" for command in rig.port.commands)


@pytest.mark.parametrize("packet", [None, TimeoutError("fake frame timeout")])
def test_missing_or_failed_frame_aborts_the_supervised_session(supervised_rig, packet):
    rig = supervised_rig()
    rig.camera.packet = packet
    with pytest.raises((RuntimeError, TimeoutError)):
        rig.session.camera.get_frame_packet(wait=True)
    assert rig.session.cancel_event.is_set()
    assert "STOP ALL" in rig.port.commands
    assert "FOCUS ZERO" not in rig.port.commands and not rig.operator.prompts


def test_focus_settings_preserve_source_defaults_and_reject_nonfinite_or_wrong_types():
    settings = validated_focus_settings()
    assert [
        settings[key] for key in ("half_range", "coarse_step", "fine_step", "ultrafine_step")
    ] == [5000, 1000, 100, 10]
    assert settings["camera_width"] == 2592 and settings["camera_height"] == 1944
    for overrides in (
        {"speed": True},
        {"settle": float("nan")},
        {"roi": 2},
        {"raw_command": "HOME"},
    ):
        with pytest.raises(ValueError):
            validated_focus_settings(overrides)


@pytest.mark.parametrize("late", [False, True])
def test_terminal_discards_queued_answers_and_rejects_late_clear(late):
    import os
    import pty

    from microscope.calibrate import TerminalOperator

    master, slave = pty.openpty()  # Owned pseudo-terminal, never a lab device.
    prompted = threading.Event()
    clock = [0.0]
    calls = [0]

    class Output:
        def isatty(self):
            return True

        def write(self, _text):
            pass

        def flush(self):
            prompted.set()

    def answer():
        assert prompted.wait(2)
        os.write(master, b"CLEAR\n" if late else b"no\n")

    def check():
        calls[0] += 1
        if late and calls[0] == 2:
            clock[0] = 60.001

    try:
        with os.fdopen(slave, "r") as source:
            # This earlier answer must never authorize a new reference event.
            os.write(master, b"CLEAR\nCLEAR\n")
            worker = threading.Thread(target=answer)
            worker.start()
            operator = TerminalOperator(source, Output(), clock=lambda: clock[0])
            with pytest.raises(TimeoutError if late else RuntimeError, match="timed out|refused"):
                operator.confirm("new reference", check)
            worker.join(2)
            assert not worker.is_alive()
    finally:
        os.close(master)


def test_terminal_flush_failure_prevents_prompt_or_confirmation(monkeypatch):
    import microscope.calibrate as module

    writes = []
    tty = SimpleNamespace(isatty=lambda: True, fileno=lambda: 123)
    output = SimpleNamespace(isatty=lambda: True, write=writes.append)

    def reject(*_args):
        raise OSError("terminal input could not be discarded")

    monkeypatch.setattr(module.termios, "tcflush", reject)
    with pytest.raises(OSError, match="discarded"):
        module.TerminalOperator(tty, output).confirm("reference", lambda: None)
    assert writes == []


@pytest.fixture
def trial_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", str(tmp_path / "runs"))
    calls = []
    cancel = threading.Event()

    class Motor:
        def send_and_wait(self, text, prefix, **kwargs):
            assert kwargs["cancel_event"] is cancel
            calls.append(("command", text))
            return prefix

        def focus_speed(self, value):
            calls.append(("speed", value))

        def focus_enable(self):
            calls.append(("enable",))

    class Session:
        camera = SimpleNamespace(
            get_frame_packet=lambda **_: (1, 1.0, np.zeros((4, 4, 3), np.uint8))
        )
        side = object()
        motor = Motor()
        cancel_event = cancel

        def check(self):
            if cancel.is_set():
                raise RuntimeError("Trial cancelled")

        def move_to_clearance(self):
            self.check()
            calls.append(("clearance",))

        def move_focus_to(self, value):
            self.check()
            calls.append(("focus_start", value))

        def abort(self, reason):
            calls.append(("abort", reason))
            cancel.set()

    class Focuser:
        failure = False
        cancel_on_return = False

        def __init__(self, camera, motor, **kwargs):
            self.measurements = []
            self.position = 0
            assert kwargs["cancel_event"] is cancel

        def run_local(self, **kwargs):
            calls.append(("local", kwargs))
            return SimpleNamespace(position=0, score=1.0)

        def run(self, **kwargs):
            calls.append(("full", kwargs))
            if self.failure:
                raise RuntimeError("Synthetic focus failure")
            if self.cancel_on_return:
                cancel.set()
            return SimpleNamespace(position=0, score=1.0)

        def _move_to(self, position, **kwargs):
            calls.append(("relative", position))
            self.position = position

    class Switcher:
        current = 0

        def __init__(self, side, motor, **kwargs):
            self.before_move = kwargs["before_move"]

        def calibrate_current(self, path, *, marker_id):
            if marker_id is not None:
                self.current = marker_id
            return SimpleNamespace(target_id=self.current, dx_px=0, dy_px=0)

        def run(self, direction, path, *, source_id):
            self.before_move()
            self.current = source_id + direction
            calls.append(("switch", source_id, self.current))
            return SimpleNamespace(target_id=self.current, dx_px=0, dy_px=0)

    monkeypatch.setattr(experiments, "AutoFocuser", Focuser)
    monkeypatch.setattr(experiments, "ArucoObjectiveSwitcher", Switcher)
    switch = replace(SwitchSettings(), enable_settle_seconds=0, release_settle_seconds=0)
    return SimpleNamespace(
        session=Session(), calls=calls, focus=Focuser, switch=switch, root=tmp_path / "runs"
    )


def test_focus_trials_preserve_offsets_and_one_stable_start(trial_rig):
    rig = trial_rig
    result = experiments.run_trials(
        "focus",
        rig.session,
        focus_settings={},
        focus_start=12000,
        output="focus",
        progress=lambda _: None,
    )
    assert [row["offset"] for row in result["rows"] if row["stage"] == "autofocus"] == [
        -2000,
        -1000,
        1000,
        2000,
    ]
    assert [call for call in rig.calls if call[0] == "focus_start"] == [("focus_start", 12000)]
    assert rig.calls[0] == ("command", "MICRO 1")
    assert {
        call[1] for call in rig.calls if call[0] == "command" and call[1].startswith("MICRO")
    } == {"MICRO 1"}
    assert [call[1] for call in rig.calls if call[0] == "relative"] == [
        -2000,
        2000,
        -1000,
        1000,
        1000,
        -1000,
        2000,
        -2000,
    ]
    assert len([call for call in rig.calls if call[0] == "local"]) == 1
    assert len([call for call in rig.calls if call[0] == "full"]) == 4
    assert all(path.stat().st_mode & 0o007 == 0 for path in result["directory"].iterdir())


def test_switch_trials_preserve_both_directions_and_microstep_offsets(trial_rig):
    rig = trial_rig
    result = experiments.run_trials(
        "switch",
        rig.session,
        focus_settings=None,
        switch_settings=rig.switch,
        ids=(0, 1),
        output="switch",
        progress=lambda _: None,
    )
    trials = [row for row in result["rows"] if row["stage"] == "switch"]
    assert [(row["source"], row["target"], row["offset_full_steps"]) for row in trials] == [
        (a, b, offset) for a, b in ((0, 1), (1, 0)) for offset in (0, -50, 50, -100, 100)
    ]
    assert [
        call[1] for call in rig.calls if call[0] == "command" and call[1].startswith("OBJ MOVE")
    ] == [f"OBJ MOVE {offset}" for _ in range(2) for offset in (-50, 50, -100, 100)]
    assert rig.calls.count(("command", "MICRO 1")) == 8


def test_combined_trials_restore_explicit_focus_start_after_each_switch(trial_rig):
    rig = trial_rig
    result = experiments.run_trials(
        "combined",
        rig.session,
        focus_settings={},
        switch_settings=rig.switch,
        ids=(0, 1),
        focus_start=12000,
        output="combined",
        progress=lambda _: None,
    )
    assert [row["target"] for row in result["rows"] if row["stage"] == "combined"] == [0, 1]
    assert rig.calls.count(("focus_start", 12000)) == 2
    assert len([call for call in rig.calls if call[0] == "local"]) == 2
    assert not [call for call in rig.calls if call[0] == "full"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_trial_failure_or_swallowed_cancel_aborts_and_retains_terminal_rows(trial_rig, cancelled):
    rig = trial_rig
    rig.focus.failure = not cancelled
    rig.focus.cancel_on_return = cancelled
    with pytest.raises(RuntimeError, match="cancelled|Synthetic focus failure"):
        experiments.run_trials(
            "focus",
            rig.session,
            focus_settings={},
            focus_start=12000,
            output="failed",
            progress=lambda _: None,
        )
    assert rig.session.cancel_event.is_set()
    assert len([call for call in rig.calls if call[0] == "abort"]) == 1
    with (rig.root / "failed" / "summary.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[-1]["stage"] == "terminal"
    assert rows[-1]["status"] == ("stopped" if cancelled else "failed")
    assert not [row for row in rows if row["stage"] == "autofocus"]


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_final_trial_frame_failure_aborts_without_success(trial_rig, failure):
    rig = trial_rig

    def frame(**kwargs):
        if failure == "timeout":
            raise TimeoutError("Synthetic frame timeout")
        return None

    rig.session.camera = SimpleNamespace(get_frame_packet=frame)
    with pytest.raises((RuntimeError, TimeoutError), match="final microscope frame|frame timeout"):
        experiments.run_trials(
            "focus",
            rig.session,
            focus_settings={},
            focus_start=12000,
            output="failed-frame",
            progress=lambda _: None,
        )
    assert rig.session.cancel_event.is_set()
    assert len([call for call in rig.calls if call[0] == "abort"]) == 1
    with (rig.root / "failed-frame" / "summary.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["stage"] == "terminal" and rows[0]["status"] == "failed"
    assert not list((rig.root / "failed-frame").glob("*.png"))
    assert (rig.root / "failed-frame" / "reference_curve.csv").is_file()


@pytest.fixture
def session_factories():
    import microscope.calibrate as module

    events = []
    port = _DemoSerial()

    class Motor(MotorController):
        def __init__(self):
            events.append("motor construct")
            super().__init__(serial_factory=lambda **_: port)

        def connect(self, serial):
            events.append("serial connect")
            return super().connect(serial, reset_seconds=0)

        def disconnect(self):
            if self.connected:
                events.append("serial close")
            super().disconnect()

        def safe_stop_disable(self, **kwargs):
            events.append("stop disable")
            return super().safe_stop_disable(**kwargs)

    class Camera(FakeCamera):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.name = "side" if args else "main"
            self.is_open = False
            self.failed_close = False
            events.append(f"{self.name} construct")

        def open(self):
            events.append(f"{self.name} open")
            self.is_open = True

        def get_frame_packet(self, **kwargs):
            events.append(f"{self.name} frame")
            return super().get_frame_packet(**kwargs)

        def close(self):
            events.append(f"{self.name} close")
            if self.failed_close:
                raise RuntimeError("camera reader still draining")
            self.is_open = False

    settings = {
        "MG400_MICROSCOPE_SERIAL": "/dev/serial/by-id/fake-supervised",
        "MG400_MICROSCOPE_FOCUS_LIMITS": "-8000,8000",
        "MG400_MICROSCOPE_SWITCH_FOCUS": "0",
        "MG400_MICROSCOPE_HOME_SWITCH": "0",
        "MG400_CAMERA_DEVICES": "/dev/v4l/by-path/fake-side",
        "MG400_MICROSCOPE_SIDE_STREAM": "camera1",
    }
    rig = SimpleNamespace(
        events=events,
        port=port,
        settings=settings,
        factories={"motor": Motor, "camera": Camera, "side": Camera},
        context=module.supervised_session,
        camera=Camera,
    )
    yield rig
    port.close()


def test_owned_camera_first_frames_precede_motor_reference_and_cleanup_retains_order(
    session_factories,
):
    rig = session_factories
    with rig.context(
        need_side=True,
        environ=rig.settings,
        factories=rig.factories,
        operator=ScriptedOperator("CLEAR"),
    ) as session:
        assert not any("ZERO" in command or "HOME" in command for command in rig.port.commands)
        assert (
            rig.events.index("main frame")
            < rig.events.index("side frame")
            < rig.events.index("serial connect")
        )
        session.motor.focus_move(10)
    assert "FOCUS MOVE -10" in rig.port.commands
    assert (
        rig.events.index("stop disable")
        < rig.events.index("side close")
        < rig.events.index("main close")
        < rig.events.index("serial close")
    )


def test_image_only_reference_capture_has_no_reference_or_motion(
    session_factories, tmp_path, monkeypatch
):
    from microscope.calibrate import run_calibration

    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    rig = session_factories
    settings = {
        name: value
        for name, value in rig.settings.items()
        if name
        not in {
            "MG400_MICROSCOPE_FOCUS_LIMITS",
            "MG400_MICROSCOPE_SWITCH_FOCUS",
            "MG400_MICROSCOPE_HOME_SWITCH",
        }
    }
    operator = ScriptedOperator()
    with rig.context(
        environ=settings, factories=rig.factories, operator=operator, motion=False
    ) as session:
        output = run_calibration("reference", session)
        assert output.is_file() and output.stat().st_mode & 0o037 == 0
    assert not operator.prompts
    assert not any(
        command.startswith(("FOCUS MOVE", "OBJ MOVE", "FOCUS ZERO", "FOCUS HOME", "MICRO"))
        for command in rig.port.commands
    )


@pytest.mark.parametrize("gate", ["terminal", "reference", "side", "main-frame", "serial"])
def test_startup_rejection_never_grants_motion_and_releases_admitted_owners(
    session_factories, gate
):
    rig = session_factories
    operator = ScriptedOperator()
    if gate == "terminal":
        operator.require_interactive = lambda: (_ for _ in ()).throw(RuntimeError("no TTY"))
    elif gate == "reference":
        rig.settings.pop("MG400_MICROSCOPE_FOCUS_LIMITS")
    elif gate == "side":
        rig.factories["side"] = lambda *_: (_ for _ in ()).throw(RuntimeError("queue busy"))
    elif gate == "main-frame":

        class Empty(rig.camera):
            def get_frame_packet(self, **kwargs):
                return None

        rig.factories["camera"] = Empty
    else:
        rig.factories["motor"] = lambda: (_ for _ in ()).throw(RuntimeError("serial busy"))
    with pytest.raises((ValueError, RuntimeError)):
        with rig.context(
            need_side=True, environ=rig.settings, factories=rig.factories, operator=operator
        ):
            pytest.fail("startup must be refused")
    assert not rig.port.commands and not operator.prompts
    if gate in {"terminal", "reference"}:
        assert not rig.events
    else:
        assert "main close" in rig.events


def test_undrained_side_owner_prevents_other_owners_from_being_released(session_factories):
    rig = session_factories
    held = None
    with pytest.raises(RuntimeError, match="draining"):
        with rig.context(
            need_side=True,
            environ=rig.settings,
            factories=rig.factories,
            operator=ScriptedOperator(),
            motion=False,
        ) as session:
            held = session
            session._owners[1].failed_close = True
    assert held._raw_motor.connected and all(owner.is_open for owner in held._owners)
    assert (
        "stop disable" in rig.events
        and "main close" not in rig.events
        and "serial close" not in rig.events
    )
    held._owners[1].failed_close = False
    held._owners[1].close()
    held._owners[0].close()
    held._raw_motor.disconnect()


def test_missing_fresh_frame_cannot_reuse_previous_camera_packet(supervised_rig):
    rig = supervised_rig()
    assert rig.session.camera.get_frame_packet()[0] == 1
    with pytest.raises(RuntimeError, match="stale"):
        rig.session.camera.get_frame_packet()
    assert rig.session.cancel_event.is_set()


def test_stalled_camera_publication_aborts_even_without_a_scoring_request(supervised_rig):
    rig = supervised_rig()
    rig.session._frame_progress[id(rig.camera)] = (1, time.monotonic() - 3)
    rig.session.start_watch()
    try:
        assert rig.session.cancel_event.wait(1)
        rig.session.finish_watch()
        assert "stale" in rig.session._failure
        assert "STOP ALL" in rig.port.commands and "FOCUS DISABLE" in rig.port.commands
    finally:
        rig.session.finish_watch()


@pytest.mark.parametrize("owner", ["camera", "side"])
@pytest.mark.parametrize("bad", ["shape", "dtype", "id", "time", "arity"])
def test_malformed_first_frame_is_rejected_before_motor_construction(session_factories, owner, bad):
    rig = session_factories
    packets = {
        "shape": (1, 1.0, np.zeros((4, 4), np.uint8)),
        "dtype": (1, 1.0, np.zeros((4, 4, 3), np.float32)),
        "id": (True, 1.0, np.zeros((4, 4, 3), np.uint8)),
        "time": (1, float("nan"), np.zeros((4, 4, 3), np.uint8)),
        "arity": (1, 1.0),
    }

    class Malformed(rig.camera):
        def get_frame_packet(self, **kwargs):
            return packets[bad]

    rig.factories[owner] = Malformed
    with pytest.raises(RuntimeError, match="invalid"):
        with rig.context(
            need_side=True,
            environ=rig.settings,
            factories=rig.factories,
            operator=ScriptedOperator("CLEAR"),
        ):
            pytest.fail("invalid packet must never grant a reference")
    assert "motor construct" not in rig.events and not rig.port.commands
    assert "main close" in rig.events


@pytest.mark.parametrize("kind", ["objective", "light"])
def test_calibration_worker_keeps_gui_defaults_and_guarded_callbacks(
    kind, supervised_rig, tmp_path, monkeypatch
):
    import microscope.calibrate as module

    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    rig = supervised_rig()
    created, runs = [], []

    class Algorithm:
        def __init__(self, camera, motor, **kwargs):
            self.measurements = []
            if camera is not None:
                assert camera is rig.session.camera and motor is rig.session.motor
                created.append(kwargs)

        def run(self, **kwargs):
            runs.append(kwargs)
            return "source result"

    monkeypatch.setattr(
        module,
        "ObjectiveCalibrator" if kind == "objective" else "LightObjectiveCalibrator",
        Algorithm,
    )
    output = module.run_calibration(
        kind, rig.session, reference_frame=np.zeros((4, 4, 3), np.uint8), progress=lambda _: None
    )
    assert output.is_file() and output.stat().st_mode & 0o037 == 0
    assert len(created) == len(runs) == 1
    options = created[0]
    assert options["cancel_event"] is rig.session.cancel_event
    assert options["before_move"] == rig.session.move_to_clearance
    assert options["speed"] == 200
    if kind == "objective":
        assert options["reference_threshold"] == 0.2
        assert options["stable_threshold"] == options["atlas_merge_distance_px"] == 10
        assert runs[0] == dict(
            half_range=30,
            coarse_step=5,
            stagger_offset=2,
            refine_radius=5,
            fine_step=1,
            verification_repeats=3,
            lock_exposure=True,
        )
    else:
        assert options["reference_magnification"] == 10 and options["current_magnification"] == 20
        assert runs[0]["coarse_microstep"] == 2 and runs[0]["fine_microstep"] == 16
        assert runs[0]["focus_half_range"] == 1500 and runs[0]["focus_step"] == 100
        assert runs[0]["focus_settle_seconds"] == 0.35 and runs[0]["focus_frames"] == 3
        assert runs[0]["focus_frame_interval"] == 0 and runs[0]["focus_roi_fraction"] == 1
    assert not rig.operator.prompts  # Spy algorithm performed no motion.


def test_calibration_refused_handoff_cannot_be_hidden_by_source_cleanup(
    supervised_rig, tmp_path, monkeypatch
):
    import microscope.calibrate as module

    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    rig = supervised_rig("no")

    class Algorithm:
        def __init__(self, camera, motor, **kwargs):
            self.motor = motor
            self.measurements = []

        def run(self, **kwargs):
            try:
                self.motor.set_microstep(4)
            except RuntimeError:
                pass  # Source cleanup can swallow an exception; authority cannot recover.
            return "incorrect success"

    monkeypatch.setattr(module, "LightObjectiveCalibrator", Algorithm)
    with pytest.raises(SideCaptureCancelled):
        module.run_calibration("light", rig.session, reference_frame=np.zeros((4, 4, 3), np.uint8))
    assert rig.session.cancel_event.is_set()
    assert (tmp_path / "calibration" / "light_objective_calibration.csv").is_file()
    assert not any(
        command in {"FOCUS ZERO", "FOCUS HOME", "MICRO 4"} for command in rig.port.commands
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"calibrator": {"speed": True}},
        {"scan": {"coarse_microstep": 3}},
        {"calibrator": {"minimum_overlap": float("inf")}},
        {"scan": {"lock_exposure": 1}},
        {"focus": {"frames": 0}},
        {"scan": {"raw_command": "FOCUS ZERO"}},
    ],
)
def test_invalid_calibration_settings_fail_before_device_startup(settings, monkeypatch):
    import microscope.calibrate as module

    called = []
    monkeypatch.setattr(module, "load_settings", lambda _: settings)
    monkeypatch.setattr(module, "supervised_session", lambda **_: called.append("device"))
    assert module.main(["light"]) == 1
    assert called == []


def test_reference_png_reader_rejects_links_and_oversized_header(tmp_path, monkeypatch):
    import os

    import cv2

    import microscope.calibrate as module

    root = tmp_path / "calibration"
    root.mkdir(mode=0o750)
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(root))
    good = root / "reference.png"
    encoded, data = cv2.imencode(".png", np.zeros((4, 4, 3), np.uint8))
    assert encoded
    good.write_bytes(data.tobytes())
    good.chmod(0o640)
    assert module.read_reference("reference.png").shape == (4, 4, 3)
    (root / "link.png").symlink_to(good)
    with pytest.raises(OSError):
        module.read_reference("link.png")
    hard = root / "hard.png"
    os.link(good, hard)
    with pytest.raises(ValueError, match="regular"):
        module.read_reference("hard.png")
    hard.unlink()
    header = bytearray(good.read_bytes())
    header[16:20] = (5_000_000).to_bytes(4, "big")
    good.write_bytes(header)
    with pytest.raises(ValueError, match="dimensions"):
        module.read_reference("reference.png")


def test_signal_cancellation_stops_devices_and_restores_handlers(session_factories):
    import signal

    rig = session_factories
    previous = {
        number: signal.getsignal(number)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    with pytest.raises(KeyboardInterrupt):
        with rig.context(
            environ=rig.settings, factories=rig.factories, operator=ScriptedOperator(), motion=False
        ):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    assert "stop disable" in rig.events and "serial close" in rig.events
    assert all(signal.getsignal(number) == handler for number, handler in previous.items())


DEVICE = "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1.2:1.0-video-index0"
MODE = (320, 240, 30)


class FakeCV:
    CAP_V4L2 = 200
    CAP_PROP_FOURCC = 6
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    CAP_PROP_FPS = 5
    CAP_PROP_BUFFERSIZE = 38

    @staticmethod
    def VideoWriter_fourcc(*letters):
        return sum(ord(letter) << (8 * index) for index, letter in enumerate(letters))


class FakeDevice:
    def __init__(self):
        self.calls = []
        self.descriptors = set()
        self._next = 40
        self.info = SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_dev=5, st_ino=7, st_rdev=81)
        self.path_info = self.info
        self.driver, self.bus = b"uvcvideo", b"usb-0000:00:14.0-1.2"
        self.caps = _V4L2_CAP_DEVICE_CAPS | _V4L2_CAP_STREAMING | _V4L2_CAP_VIDEO_CAPTURE
        self.device_caps = _V4L2_CAP_STREAMING | _V4L2_CAP_VIDEO_CAPTURE

    def open(self, path, flags):
        assert path == DEVICE and flags == os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC
        self._next += 1
        self.descriptors.add(self._next)
        self.calls.append(("open", path))
        return self._next

    def fstat(self, descriptor):
        assert descriptor in self.descriptors
        return self.info

    def stat(self, path):
        assert path == DEVICE
        return self.path_info

    def close(self, descriptor):
        self.calls.append(("close", descriptor))
        self.descriptors.remove(descriptor)

    def ioctl(self, descriptor, operation, data, mutate):
        assert descriptor in self.descriptors and operation == _VIDIOC_QUERYCAP
        assert mutate and len(data) == 104
        self.calls.append(("query", descriptor))
        data[:] = _V4L2_CAPABILITY.pack(
            self.driver, b"Fake AC410", self.bus, 0x060C00, self.caps, self.device_caps, 0, 0, 0
        )
        return 0


class FakeVideo:
    def __init__(self, queue, path, backend):
        assert path.startswith("/proc/self/fd/") and backend == FakeCV.CAP_V4L2
        self.queue, self.path = queue, path
        self.values = {}
        self.opened = queue.owner is None
        self.released = False
        self.release_error = None
        self.read_error = None
        self.backend = "V4L2"
        self.set_ok = True
        self.read_result = (True, np.zeros((MODE[1], MODE[0], 3), dtype=np.uint8))
        self.entered = self.proceed = None
        if self.opened:
            queue.owner = self
        queue.videos.append(self)
        queue.calls.append(("construct", self))

    def isOpened(self):
        return self.opened and not self.released

    def getBackendName(self):
        return self.backend

    def set(self, key, value):
        self.queue.calls.append(("set", key, value))
        self.values[key] = value
        return self.set_ok

    def get(self, key):
        return self.values[key]

    def read(self):
        assert self.queue.owner is self and not self.released
        self.queue.calls.append(("read", self))
        if self.entered is not None:
            self.entered.set()
            assert self.proceed.wait(3)
        if self.read_error is not None:
            raise self.read_error
        return self.read_result

    def release(self):
        self.queue.calls.append(("release", self))
        if self.release_error is not None:
            raise self.release_error
        if self.queue.owner is self:
            self.queue.owner = None
        self.released = True


class FakeQueue:
    def __init__(self):
        self.owner = None
        self.calls = []
        self.videos = []
        self.configure = lambda video: None

    def video(self, path, backend):
        video = FakeVideo(self, path, backend)
        self.configure(video)
        return video


def make_capture(device, queue):
    return NativeUVCCapture(
        DEVICE, MODE, system=device, ioctl=device.ioctl, cv=FakeCV, video_factory=queue.video
    )


def test_native_constructor_is_inert_and_querycap_layout_is_exact():
    device, queue = FakeDevice(), FakeQueue()
    capture = make_capture(device, queue)
    assert not device.calls and not queue.calls
    assert _V4L2_CAPABILITY.size == 104 and _VIDIOC_QUERYCAP == 0x80685600
    capture.release()
    assert not device.calls and not queue.calls


@pytest.mark.parametrize("path", ["/dev/video0", "camera1", DEVICE + "/../video0"])
def test_native_rejects_nonstable_path_without_open(path):
    device, queue = FakeDevice(), FakeQueue()
    with pytest.raises(ValueError):
        NativeUVCCapture(path, MODE, system=device, ioctl=device.ioctl, cv=FakeCV)
    assert not device.calls and not queue.calls


@pytest.mark.parametrize("change", ["regular", "driver", "bus", "capture", "streaming", "identity"])
def test_native_querycap_identity_failures_precede_capture_creation(change):
    device, queue = FakeDevice(), FakeQueue()
    if change == "regular":
        device.info.st_mode = stat.S_IFREG | 0o600
    elif change == "driver":
        device.driver = b"v4l2loopback"
    elif change == "bus":
        device.bus = b""
    elif change == "capture":
        device.device_caps &= ~_V4L2_CAP_VIDEO_CAPTURE
    elif change == "streaming":
        device.device_caps &= ~_V4L2_CAP_STREAMING
    else:
        device.path_info = SimpleNamespace(**{**vars(device.info), "st_ino": 8})
    with pytest.raises(RuntimeError):
        make_capture(device, queue).open()
    assert not queue.calls and not device.descriptors


def test_native_legacy_capability_field_is_used_without_device_caps_flag():
    device, queue = FakeDevice(), FakeQueue()
    device.caps &= ~_V4L2_CAP_DEVICE_CAPS
    device.device_caps = 0
    capture = make_capture(device, queue).open()
    capture.release()
    assert not device.descriptors and queue.owner is None


@pytest.mark.parametrize("kind", ["backend", "configuration", "readback", "fourcc"])
def test_native_rejects_backend_or_mode_failure_and_releases(kind):
    device, queue = FakeDevice(), FakeQueue()

    def configure(video):
        if kind == "backend":
            video.backend = "FFMPEG"
        elif kind == "configuration":
            video.set_ok = False
        elif kind == "readback":
            video.get = lambda key: 0
        else:
            video.get = lambda key: video.values[key] + (key == FakeCV.CAP_PROP_FOURCC)

    queue.configure = configure
    capture = make_capture(device, queue)
    with pytest.raises(RuntimeError):
        capture.open()
    assert queue.videos[0].released and queue.owner is None and not device.descriptors
    with pytest.raises(RuntimeError, match="reopened"):
        capture.open()


def test_native_retains_queue_and_never_reconfigures_after_first_frame():
    device, queue = FakeDevice(), FakeQueue()
    capture = make_capture(device, queue).open()
    try:
        assert capture.read()[0]
        first_read = next(index for index, call in enumerate(queue.calls) if call[0] == "read")
        assert sum(call[0] == "set" for call in queue.calls[:first_read]) == 5
        assert queue.owner is queue.videos[0]
        for _ in range(3):
            assert capture.read()[0]
        assert all(call[0] == "read" for call in queue.calls[first_read:])
        contender = make_capture(device, queue)
        with pytest.raises(RuntimeError, match="V4L2 backend"):
            contender.open()
        assert queue.owner is queue.videos[0] and not queue.videos[0].released
        assert queue.videos[1].released
    finally:
        capture.release()
    assert not device.descriptors and queue.owner is None
    successor = make_capture(device, queue).open()
    successor.release()
    with pytest.raises(RuntimeError, match="reopened"):
        capture.open()


def test_native_existing_stream_owner_blocks_startup_without_disturbing_it():
    device, queue = FakeDevice(), FakeQueue()
    existing_owner = object()
    queue.owner = existing_owner
    with pytest.raises(RuntimeError, match="V4L2 backend"):
        make_capture(device, queue).open()
    assert queue.owner is existing_owner and not device.descriptors
    assert [call[0] for call in queue.calls] == ["construct", "release"]


@pytest.mark.parametrize("phase", ["first", "live"])
@pytest.mark.parametrize("failure", ["busy", "none", "shape", "dtype", "exception", "identity"])
def test_native_frame_failure_is_latched_without_retry_or_queue_release(failure, phase):
    device, queue = FakeDevice(), FakeQueue()
    capture = make_capture(device, queue).open()
    video = queue.videos[0]
    if phase == "live":
        assert capture.read()[0]
    if failure == "busy":
        video.read_result = False, None
    elif failure == "none":
        video.read_result = True, None
    elif failure == "shape":
        video.read_result = True, np.zeros((2, 2, 3), dtype=np.uint8)
    elif failure == "dtype":
        video.read_result = True, np.zeros((MODE[1], MODE[0], 3), dtype=np.float32)
    elif failure == "exception":
        video.read_error = OSError("disconnected")
    else:
        device.path_info = SimpleNamespace(**{**vars(device.info), "st_rdev": 82})
    try:
        with pytest.raises((RuntimeError, OSError)):
            capture.read()
        reads = sum(call[0] == "read" for call in queue.calls)
        with pytest.raises(RuntimeError, match="unavailable"):
            capture.read()
        assert sum(call[0] == "read" for call in queue.calls) == reads
        assert queue.owner is video and device.descriptors and not video.released
    finally:
        capture.release()


def test_native_identity_change_during_read_rejects_returned_frame():
    device, queue = FakeDevice(), FakeQueue()
    capture = make_capture(device, queue).open()

    def replaced():
        device.path_info = SimpleNamespace(**{**vars(device.info), "st_ino": 8})
        return True, np.zeros((MODE[1], MODE[0], 3), dtype=np.uint8)

    queue.videos[0].read = replaced
    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            capture.read()
    finally:
        capture.release()


def test_native_side_publishes_frame_from_retained_capture_and_closes_after_reader():
    device, queue = FakeDevice(), FakeQueue()
    side = NativeSideCamera(
        DEVICE, MODE, "camera2", capture_factory=lambda *args: make_capture(device, queue)
    )
    assert not device.calls
    try:
        side.open()
        packet = side.get_frame_packet(timeout=1)
        assert packet is not None and packet[2].shape == (MODE[1], MODE[0], 3)
        assert queue.owner is queue.videos[0]
    finally:
        side.close()
    assert queue.owner is None and not device.descriptors
    side.close()
    with pytest.raises(RuntimeError, match="reopened"):
        side.open()


def test_native_side_blocked_read_retains_queue_on_close_timeout():
    device, queue = FakeDevice(), FakeQueue()
    entered, proceed = threading.Event(), threading.Event()

    def block(video):
        video.entered, video.proceed = entered, proceed

    queue.configure = block
    side = NativeSideCamera(
        DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue), close_timeout=0.01
    )
    side.open()
    try:
        assert entered.wait(1)
        assert side.get_frame_packet(wait=False) is None
        with pytest.raises(RuntimeError, match="ownership retained"):
            side.close()
        assert side.is_open and queue.owner is queue.videos[0] and device.descriptors
        assert not queue.videos[0].released
        with pytest.raises(RuntimeError, match="reopened"):
            side.open()
        with pytest.raises(RuntimeError, match="stopped"):
            side.get_frame_packet(wait=False)
        with pytest.raises(RuntimeError):
            make_capture(device, queue).open()
    finally:
        proceed.set()
        side._thread.join(1)
        side.close()
    assert queue.owner is None and not device.descriptors


def test_native_side_retains_owner_when_native_release_fails():
    device, queue = FakeDevice(), FakeQueue()
    side = NativeSideCamera(DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue))
    side.open()
    video = queue.videos[0]
    try:
        assert side.get_frame_packet(timeout=1) is not None
        video.release_error = RuntimeError("native release failed")
        with pytest.raises(RuntimeError, match="native release failed"):
            side.close()
        assert side.is_open and side._native_owner is not None and device.descriptors
        assert queue.owner is video and side._thread is None
    finally:
        video.release_error = None
        side.close()
    assert queue.owner is None and not device.descriptors


def test_native_failed_startup_retains_owner_when_native_cleanup_fails():
    device, queue = FakeDevice(), FakeQueue()

    def fail(video):
        video.set_ok = False
        video.release_error = RuntimeError("native cleanup failed")

    queue.configure = fail
    side = NativeSideCamera(DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue))
    with pytest.raises(RuntimeError, match="native cleanup failed"):
        side.open()
    assert side._native_owner is not None and side._thread is None
    assert queue.owner is queue.videos[0] and device.descriptors
    with pytest.raises(RuntimeError, match="reopened"):
        side.open()
    queue.videos[0].release_error = None
    side.close()
    assert queue.owner is None and not device.descriptors


@pytest.mark.parametrize("cause", ["camera-timeout", "refusal", "interrupt"])
def test_trial_terminal_status_preserves_real_guard_fault_versus_operator_stop(
    trial_rig, supervised_rig, monkeypatch, cause
):
    trial, rig = trial_rig, supervised_rig("no")
    # The trial source still receives its original cancellation object; the real
    # guard owns the same event and reports its first cause separately.
    rig.session.cancel_event = trial.session.cancel_event
    trial.session.check = rig.session.check
    trial.session.abort = rig.session.abort
    monkeypatch.setattr(
        type(trial.session), "failure", property(lambda _: rig.session.failure), raising=False
    )
    monkeypatch.setattr(
        type(trial.session),
        "operator_stopped",
        property(lambda _: rig.session.operator_stopped),
        raising=False,
    )
    if cause == "camera-timeout":
        rig.camera.packet = TimeoutError("Synthetic camera transport timeout")
        trial.session.camera = rig.session.camera
        expected = "failed"
        match = "transport timeout"
    else:

        def fail(_self, **kwargs):
            if cause == "interrupt":
                raise KeyboardInterrupt("Operator interrupt")
            rig.session.confirm_reference("scripted reference")

        monkeypatch.setattr(trial.focus, "run_local", fail)
        expected = "stopped"
        match = "interrupt|refused"
    with pytest.raises((RuntimeError, TimeoutError, KeyboardInterrupt), match=match):
        experiments.run_trials(
            "focus",
            trial.session,
            focus_settings={},
            focus_start=12000,
            output="guard-failure",
            progress=lambda _: None,
        )
    with (trial.root / "guard-failure" / "summary.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[-1]["stage"] == "terminal" and rows[-1]["status"] == expected
    assert "STOP ALL" in rig.port.commands and "FOCUS DISABLE" in rig.port.commands
    assert rig.session.cancel_event.is_set()
    if cause == "camera-timeout":
        assert rows[-1]["error"] == "Synthetic camera transport timeout"


@pytest.mark.parametrize("transition", ["microstep", "abort"])
def test_reference_check_revalidates_snapshot_across_transition(
    supervised_rig, monkeypatch, transition
):
    rig = supervised_rig("CLEAR", "CLEAR")
    rig.session.motor.focus_move(1)
    entered, proceed = threading.Event(), threading.Event()
    original = MotorController.reference_epoch.fget
    errors = []

    def epoch(motor):
        if threading.current_thread().name == "paused-reference-check":
            entered.set()
            assert proceed.wait(2)
        return original(motor)

    def check():
        try:
            rig.session.check()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(MotorController, "reference_epoch", property(epoch))
    worker = threading.Thread(target=check, name="paused-reference-check")
    worker.start()
    try:
        assert entered.wait(1)
        # No state lock may be held over the property read. Both transitions
        # must complete while the other thread retains its earlier token.
        if transition == "microstep":
            rig.session.motor.set_microstep(4)
        else:
            rig.session.abort("explicit test stop", operator=True)
    finally:
        proceed.set()
        worker.join(2)
    assert not worker.is_alive()
    if transition == "microstep":
        assert errors == [] and not rig.session.cancel_event.is_set()
        rig.session.check()
    else:
        assert len(errors) == 1 and isinstance(errors[0], SideCaptureCancelled)
        assert rig.session.failure == "explicit test stop"


def test_native_unstarted_reader_cleanup_can_retry_release_failure(monkeypatch):
    device, queue = FakeDevice(), FakeQueue()
    queue.configure = lambda video: setattr(video, "release_error", RuntimeError("release failed"))
    side = NativeSideCamera(DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue))

    def start(_thread):
        raise RuntimeError("start failed")

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="release failed"):
        side.open()
    assert side._thread is None and side._native_owner is not None
    assert queue.owner is queue.videos[0] and device.descriptors
    assert not any(call[0] == "read" for call in queue.calls)
    queue.videos[0].release_error = None
    side.close()
    assert queue.owner is None and not device.descriptors


def test_native_start_exception_cancels_gate_before_reader_admission(monkeypatch):
    device, queue = FakeDevice(), FakeQueue()
    threads = []
    side = NativeSideCamera(DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue))
    original = threading.Thread.start

    def start(thread):
        threads.append(thread)
        original(thread)
        assert not any(call[0] == "read" for call in queue.calls)
        raise KeyboardInterrupt("start interrupted")

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(KeyboardInterrupt, match="start interrupted"):
        side.open()
    assert not threads[0].is_alive()
    assert not any(call[0] == "read" for call in queue.calls)
    assert queue.owner is None and not device.descriptors


def test_native_late_bootstrap_after_interrupted_start_cannot_read(monkeypatch):
    device, queue = FakeDevice(), FakeQueue()
    pending = []
    side = NativeSideCamera(DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue))

    class LateThread:
        ident = None

        def __init__(self, *, target, args, **kwargs):
            pending.append(lambda: target(*args))

        def start(self):
            raise KeyboardInterrupt("bootstrap pending")

        def join(self, **kwargs):
            raise AssertionError("An unstarted thread must not be joined")

    monkeypatch.setattr(threading, "Thread", LateThread)
    with pytest.raises(KeyboardInterrupt, match="bootstrap pending"):
        side.open()
    assert queue.owner is None and not device.descriptors
    pending[0]()
    assert not any(call[0] == "read" for call in queue.calls)


def test_native_interruption_after_gate_admission_retains_accepted_read(monkeypatch):
    device, queue = FakeDevice(), FakeQueue()
    entered, proceed = threading.Event(), threading.Event()
    side = NativeSideCamera(
        DEVICE, MODE, capture_factory=lambda *args: make_capture(device, queue), close_timeout=0.01
    )

    def block(video):
        video.entered, video.proceed = entered, proceed

    queue.configure = block
    original = threading.Event
    created = 0

    class InterruptedGate:
        def __init__(self):
            self.event = original()
            self.interrupted = False

        def wait(self):
            return self.event.wait()

        def set(self):
            self.event.set()
            if not self.interrupted:
                self.interrupted = True
                assert entered.wait(1)
                raise KeyboardInterrupt("interrupted after admission")

    def event():
        nonlocal created
        created += 1
        return InterruptedGate() if created == 2 else original()

    monkeypatch.setattr(threading, "Event", event)
    try:
        with pytest.raises(RuntimeError, match="ownership retained"):
            side.open()
        assert side._thread.is_alive() and side._native_owner is not None
        assert queue.owner is queue.videos[0] and not queue.videos[0].released
        assert device.descriptors
    finally:
        proceed.set()
        side._thread.join(1)
        side.close()
    assert queue.owner is None and not device.descriptors


@pytest.mark.parametrize("value,expected", [(None, 0), ("", 0), ("0", 0), ("1", 1), ("20", 20)])
def test_measured_nudge_window_accepts_only_explicit_bounded_integer(value, expected):
    from microscope.calibrate import nudge_steps

    environ = {} if value is None else {"MG400_MICROSCOPE_NUDGE_STEPS": value}
    assert nudge_steps(environ) == expected


@pytest.mark.parametrize("value", ["-1", "21", "1.0", " 1", "true", "NaN", 1, True])
def test_invalid_nudge_window_fails_before_hardware(value, monkeypatch):
    import microscope.calibrate as module

    with pytest.raises(ValueError, match="0..20"):
        module.nudge_steps({"MG400_MICROSCOPE_NUDGE_STEPS": value})
    monkeypatch.setenv("MG400_MICROSCOPE_NUDGE_STEPS", str(value))
    monkeypatch.setattr(module, "supervised_session", lambda **_: pytest.fail("device admitted"))
    assert module.main(["objective"]) == 1


@pytest.fixture
def at_focus_rig(supervised_rig, monkeypatch):
    import microscope.calibrate as module

    def make(*answers, start=101, window=2, home=False, reference=None):
        rig = supervised_rig(*(answers or ("CLEAR",)), home=home, reference=reference)
        calls = []

        class LocalFocus:
            def __init__(self, camera, motor, **kwargs):
                assert camera is rig.session.camera and motor is rig.session.motor
                calls.append(("init", kwargs))

            def run_local(self, **kwargs):
                calls.append(("local", kwargs))
                rig.session.motor.focus_move(2)

        monkeypatch.setattr(module, "AutoFocuser", LocalFocus)
        rig.session.prepare_at_focus(window, start, validated_focus_settings(), lambda _: None)
        rig.calls = calls
        return rig

    return make


def test_at_focus_initial_reference_and_one_pinned_local_scan(at_focus_rig):
    rig = at_focus_rig()
    commands = rig.port.commands
    assert (
        commands.index("MICRO 4") < commands.index("FOCUS ZERO") < commands.index("FOCUS MOVE 101")
    )
    assert len(rig.operator.prompts) == 1 and "working position" in rig.operator.prompts[0]
    assert [kind for kind, _ in rig.calls] == ["init", "local"]
    assert rig.calls[0][1]["frames_per_position"] == 2
    assert rig.calls[0][1]["precision_frames_per_position"] == 3
    assert rig.calls[1][1] == dict(
        half_range=1500, step=100, fine_radius=200, min_step=10, lock_exposure=True
    )
    assert rig.raw.read_status().focus.position == 103


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("direction", [-1, 1])
def test_at_focus_turret_edge_and_rejection_never_moves_to_clearance(
    at_focus_rig, generic, direction
):
    rig = at_focus_rig(window=2)
    rig.session.motor.objective_enable()
    before = len(rig.port.commands)
    if generic:
        rig.session.motor.send_and_wait(f"OBJ MOVE {8 * direction}", "DONE OBJ")
    else:
        rig.session.motor.objective_move(8 * direction)
        with rig.port.lock:
            rig.port.emit(rig.port.sim.advance(1))
    assert rig.session._turret_units == 32 * direction
    assert rig.raw.read_status().focus.position == 103
    with pytest.raises(ValueError, match="measured at-focus window"):
        rig.session.motor.objective_move(direction)
    commands = rig.port.commands[before:]
    assert f"OBJ MOVE {8 * direction}" in commands and f"OBJ MOVE {direction}" not in commands
    assert not any(command.startswith("FOCUS MOVE") for command in commands)
    assert commands[-1] == "FOCUS DISABLE"
    assert rig.session.cancel_event.is_set()


@pytest.mark.parametrize("start,expected", [(101, 52), (-101, -50)])
def test_reference_event_returns_nearest_focus_without_resetting_turret_budget(
    at_focus_rig, start, expected
):
    rig = at_focus_rig("CLEAR", "CLEAR", "CLEAR", start=start, window=2)
    motor = rig.session.motor
    motor.objective_enable()
    motor.send_and_wait("OBJ MOVE 4", "DONE OBJ")  # 1 full step at MICRO4.
    before = len(rig.port.commands)
    motor.set_microstep(2)
    commands = rig.port.commands[before:]
    assert commands.index(f"FOCUS MOVE {-start - 2}") < commands.index("MICRO 2")
    assert (
        commands.index("MICRO 2")
        < commands.index("FOCUS ZERO")
        < commands.index(f"FOCUS MOVE {expected}")
    )
    assert f"return to {expected} pulses at MICRO 2" in rig.operator.prompts[-1]
    assert rig.raw.read_status().focus.position == expected
    motor.send_and_wait("OBJ MOVE 2", "DONE OBJ")  # Another full step at MICRO2.
    motor.set_microstep(16)
    assert rig.session._turret_units == 32
    with pytest.raises(ValueError, match="window"):
        motor.objective_move(1)
    assert len(rig.operator.prompts) == 3


def test_active_focus_disable_is_stop_and_retains_reference(at_focus_rig):
    rig = at_focus_rig()
    before = len(rig.port.commands)
    epoch = rig.raw.reference_epoch
    rig.session.motor.focus_disable()
    commands = rig.port.commands[before:]
    assert "STOP FOCUS" in commands
    assert "FOCUS DISABLE" not in commands and "FOCUS ZERO" not in commands
    status = rig.raw.read_status()
    assert (
        status.focus.position == 103 and status.focus.enabled and status.zeroed and status.limited
    )
    assert rig.raw.reference_epoch == epoch and len(rig.operator.prompts) == 1


@pytest.mark.parametrize("event", ["refusal", "reset", "disconnect"])
def test_at_focus_failure_never_returns_or_restarts_motion(at_focus_rig, event):
    rig = at_focus_rig("CLEAR", "no")
    before = len(rig.port.commands)
    if event == "refusal":
        with pytest.raises(RuntimeError, match="refused"):
            rig.session.motor.set_microstep(2)
    else:
        if event == "reset":
            rig.port.emit(rig.port.sim.reset())
            deadline = time.monotonic() + 1
            while rig.raw.fault is None and time.monotonic() < deadline:
                time.sleep(0.001)
        else:
            rig.raw.disconnect()
        with pytest.raises(SideCaptureCancelled):
            rig.session.before_turret_move()
    assert rig.session.cancel_event.is_set()
    assert not any(command.startswith("OBJ MOVE") for command in rig.port.commands[before:])
    assert "FOCUS MOVE 52" not in rig.port.commands[before:]


def test_window_requires_focus_start_before_settings_or_device_access(monkeypatch):
    import microscope.calibrate as module

    monkeypatch.setenv("MG400_MICROSCOPE_NUDGE_STEPS", "1")
    monkeypatch.setattr(module, "load_settings", lambda _: pytest.fail("settings opened"))
    monkeypatch.setattr(module, "supervised_session", lambda **_: pytest.fail("device opened"))
    assert module.main(["light"]) == 1


@pytest.mark.parametrize("window", [0, 2])
def test_only_explicit_calibration_window_changes_algorithm_move_callback(
    window, supervised_rig, tmp_path, monkeypatch
):
    import microscope.calibrate as module

    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    rig = supervised_rig()
    entered, options = [], []
    monkeypatch.setattr(rig.session, "prepare_at_focus", lambda *args: entered.append(args))

    class Algorithm:
        def __init__(self, camera, motor, **kwargs):
            self.measurements = []
            if camera is not None:
                options.append(kwargs)

        def run(self, **kwargs):
            return "source result"

    monkeypatch.setattr(module, "ObjectiveCalibrator", Algorithm)
    module.run_calibration(
        "objective",
        rig.session,
        window=window,
        focus_start=100,
        reference_frame=np.zeros((4, 4, 3), np.uint8),
        progress=lambda _: None,
    )
    assert bool(entered) is bool(window)
    assert options[0]["before_move"] == (
        rig.session.before_turret_move if window else rig.session.move_to_clearance
    )


class SetupOperator(ScriptedOperator):
    def __init__(self, keys, *answers, controls=()):
        super().__init__(*answers)
        self.keys = iter(keys)
        self.controls = iter(controls)
        self.control_ready = lambda: True

    def key(self, check):
        check()
        return next(self.keys, None)

    @contextmanager
    def motion_controls(self, check):
        def poll():
            check()
            return next(self.controls, "") if self.control_ready() else ""

        yield poll


def test_setup_keys_counts_zero_unknown_stop_and_no_limits(supervised_rig):
    from microscope.calibrate import run_setup

    rig = supervised_rig()
    rig.session.operator = SetupOperator("azsxdcfvjluom0?aq", "CLEAR")
    output = []
    run_setup(rig.session, progress=output.append)
    commands = rig.port.commands
    for steps in (1, -1, 10, -10, 100, -100, 1000, -1000):
        assert f"FOCUS MOVE {steps}" in commands
    for steps in (-4, 4, -40, 40):
        assert f"OBJ MOVE {steps}" in commands
    assert "MICRO 4" in commands and "FOCUS SPEED 200" in commands and "OBJ SPEED 40" in commands
    assert commands.count("FOCUS ZERO") == 1
    assert not any(command.startswith("FOCUS LIMIT") for command in commands)
    assert "focus count: 1 MICRO 4" in output[-1]
    assert "turret displacement: 0 full steps" in output[-1]
    assert not rig.raw.read_status().focus.enabled and not rig.raw.read_status().objective.enabled


@pytest.mark.parametrize("key", [None, "q", "?"])
def test_setup_controls_stop_accepted_move_before_full_completion(supervised_rig, key, monkeypatch):
    from microscope.calibrate import run_setup

    rig = supervised_rig()
    rig.session.operator = SetupOperator("fq", controls=[key])
    rig.session.operator.control_ready = lambda: "FOCUS MOVE 1000" in rig.port.commands
    original = rig.port.sim.advance
    # Freeze fake movement; only STOP can make the accepted move idle.
    monkeypatch.setattr(rig.port.sim, "advance", lambda seconds: [])
    output = []
    run_setup(rig.session, progress=output.append)
    commands = rig.port.commands
    move = commands.index("FOCUS MOVE 1000")
    assert "STOP ALL" in commands[move + 1 :]
    assert not rig.raw.read_status().focus.moving
    assert "focus count: 0 MICRO 4" in output[-1]
    assert commands.count("FOCUS MOVE 1000") == 1
    monkeypatch.setattr(rig.port.sim, "advance", original)


def test_setup_ignores_queued_motion_controls(supervised_rig):
    from microscope.calibrate import run_setup

    rig = supervised_rig()
    rig.session.operator = SetupOperator("fq", controls=["a", "s", "m", "0", "?"])
    rig.session.operator.control_ready = lambda: "FOCUS MOVE 1000" in rig.port.commands
    run_setup(rig.session, progress=lambda _: None)
    assert [cmd for cmd in rig.port.commands if cmd.startswith("FOCUS MOVE")] == ["FOCUS MOVE 1000"]
    assert "FOCUS ZERO" not in rig.port.commands


def test_setup_context_needs_only_serial_and_never_constructs_cameras(session_factories):
    from microscope.calibrate import run_setup, setup_session

    rig = session_factories
    with setup_session(
        environ={"MG400_MICROSCOPE_SERIAL": rig.settings["MG400_MICROSCOPE_SERIAL"]},
        operator=SetupOperator("q"),
        motor_factory=rig.factories["motor"],
    ) as session:
        run_setup(session, progress=lambda _: None)
    assert rig.events == [
        "motor construct",
        "serial connect",
        "stop disable",
        "stop disable",
        "stop disable",
        "serial close",
    ]
    assert not any(cmd.startswith("FOCUS LIMIT") for cmd in rig.port.commands)


@pytest.mark.parametrize("answer", ["no", TimeoutError("setup timeout")])
def test_setup_refused_zero_stops_and_disables_without_zero(supervised_rig, answer):
    from microscope.calibrate import run_setup

    rig = supervised_rig()
    rig.session.operator = SetupOperator("0", answer)
    with pytest.raises((RuntimeError, TimeoutError)):
        run_setup(rig.session, progress=lambda _: None)
    assert "FOCUS ZERO" not in rig.port.commands
    assert not rig.raw.read_status().focus.enabled and not rig.raw.read_status().objective.enabled
    assert rig.session.cancel_event.is_set()


def test_nearest_focus_return_outside_inward_limit_aborts_before_microstep(at_focus_rig):
    rig = at_focus_rig(start=3, reference=ReferenceSettings(-5, 5, 0, False))
    assert rig.raw.read_status().focus.position == 5
    before = len(rig.port.commands)
    with pytest.raises(ValueError, match="inward-rounded"):
        rig.session.motor.set_microstep(2)
    assert "MICRO 2" not in rig.port.commands[before:]
    assert not any(cmd.startswith("FOCUS MOVE") for cmd in rig.port.commands[before:])
    assert rig.session.cancel_event.is_set()


def test_home_based_return_keeps_measured_origin_and_never_uses_zero(at_focus_rig):
    rig = at_focus_rig(
        "CLEAR", "CLEAR", start=100, home=True, reference=ReferenceSettings(-8000, 8000, 40, True)
    )
    rig.port.sim.set_home_switch(False)  # Simulate leaving the switch at working focus.
    rig.session.motor.set_microstep(2)
    status = rig.raw.read_status()
    assert status.focus.position == 51 and status.zeroed and status.limited
    assert rig.port.commands.count("FOCUS HOME") == 2
    assert "FOCUS ZERO" not in rig.port.commands
    assert "FOCUS MOVE -62" in rig.port.commands  # MICRO4 position102 to measured clearance40.


def test_environment_window_cannot_change_experiment_session_clearance(supervised_rig, monkeypatch):
    monkeypatch.setenv("MG400_MICROSCOPE_NUDGE_STEPS", "20")
    rig = supervised_rig("CLEAR", "CLEAR")
    rig.session.motor.set_microstep(4)
    rig.session.move_focus_to(100)
    before = len(rig.port.commands)
    rig.session.motor.objective_enable()
    rig.session.motor.send_and_wait("OBJ MOVE 4", "DONE OBJ")
    commands = rig.port.commands[before:]
    assert commands.index("FOCUS MOVE -100") < commands.index("OBJ MOVE 4")
    assert rig.session._nudge_window == 0 and rig.session._turret_units is None


def test_at_focus_turret_refuses_moving_focus_without_dispatch(at_focus_rig, monkeypatch):
    rig = at_focus_rig()
    monkeypatch.setattr(rig.port.sim, "advance", lambda seconds: [])
    rig.raw.focus_move_async(100)
    before = len(rig.port.commands)
    with pytest.raises(SideCaptureCancelled, match="reference, measured limits or clearance"):
        rig.session.motor.objective_move(1)
    assert not any(cmd.startswith("OBJ MOVE") for cmd in rig.port.commands[before:])
    assert rig.session.cancel_event.is_set()


@pytest.mark.parametrize("failure", ["disconnect", "reset", "timeout"])
def test_setup_terminal_failure_never_dispatches_later_key(supervised_rig, failure):
    from microscope.calibrate import run_setup

    rig = supervised_rig()

    class FailureOperator(SetupOperator):
        def key(self, check):
            if failure == "disconnect":
                rig.raw.disconnect()
            elif failure == "reset":
                rig.port.emit(rig.port.sim.reset())
                deadline = time.monotonic() + 1
                while not rig.raw.fault and time.monotonic() < deadline:
                    time.sleep(0.001)
            else:
                raise TimeoutError("setup input expired")
            return "f"

    rig.session.operator = FailureOperator("")
    with pytest.raises((SideCaptureCancelled, TimeoutError)):
        run_setup(rig.session, progress=lambda _: None)
    assert "FOCUS MOVE 1000" not in rig.port.commands
    assert rig.session.cancel_event.is_set()


def test_setup_key_flushes_stale_input_and_restores_terminal_modes(monkeypatch):
    import pty
    import termios

    from microscope.calibrate import TerminalOperator

    master, slave = pty.openpty()  # Owned pseudo-terminal, no serial or lab device.
    ready = threading.Event()
    original = termios.tcsetattr

    def mode(fd, when, attrs):
        original(fd, when, attrs)
        if not attrs[3] & termios.ICANON:
            ready.set()

    monkeypatch.setattr(termios, "tcsetattr", mode)

    def send():
        assert ready.wait(2)
        os.write(master, b"a")

    try:
        with os.fdopen(slave, "r") as source:
            saved = termios.tcgetattr(source.fileno())
            operator = TerminalOperator(
                source,
                SimpleNamespace(isatty=lambda: True, write=lambda _: None, flush=lambda: None),
            )
            os.write(master, b"ffq")
            sender = threading.Thread(target=send)
            sender.start()
            assert operator.key(lambda: None) == "a"
            sender.join(2)
            assert not sender.is_alive()
            assert termios.tcgetattr(source.fileno()) == saved
            with pytest.raises(RuntimeError, match="scripted cancellation"):
                with operator.motion_controls(
                    lambda: (_ for _ in ()).throw(RuntimeError("scripted cancellation"))
                ) as poll:
                    poll()
            assert termios.tcgetattr(source.fileno()) == saved
    finally:
        os.close(master)


@pytest.mark.parametrize("phase", ["ack", "status"])
@pytest.mark.parametrize("control", ["?", "q", None])
def test_setup_controls_remain_live_during_blocked_ack_and_status(
    supervised_rig, phase, control, monkeypatch
):
    from microscope.calibrate import run_setup

    rig = supervised_rig()
    withheld = threading.Event()
    hold_status = threading.Event()
    emit = rig.port.emit

    def filtered(lines):
        if (
            phase == "status"
            and hold_status.is_set()
            and any(line.startswith("MICRO=") for line in lines)
        ):
            withheld.set()
            return
        kept = []
        for line in lines:
            if phase == "ack" and line == "OK FOCUS MOVE 1000":
                withheld.set()
            else:
                kept.append(line)
        emit(kept)

    monkeypatch.setattr(rig.port, "emit", filtered)

    class Operator(SetupOperator):
        def key(self, check):
            key = super().key(check)
            if key == "f":
                hold_status.set()
            return key

    rig.session.operator = Operator("fq", controls=[control])
    rig.session.operator.control_ready = withheld.is_set
    output = []
    if phase == "status":
        # The deliberately incomplete STATUS remains an unknown outcome; a Stop
        # must still dispatch immediately, without reporting successful recovery.
        with pytest.raises(RuntimeError, match="status is incomplete|unknown command outcome"):
            run_setup(rig.session, progress=output.append)
        assert rig.raw.fault is not None
    else:
        run_setup(rig.session, progress=output.append)
    assert withheld.is_set()
    commands = rig.port.commands
    if phase == "ack":
        move = commands.index("FOCUS MOVE 1000")
        assert "STOP ALL" in commands[move + 1 :]
        assert rig.port.sim.focus.position < 1000
    else:
        assert "FOCUS MOVE 1000" not in commands
        assert commands.count("STOP ALL") >= 2
    assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled
    assert not any(thread.name == "microscope-setup-action" for thread in threading.enumerate())


def test_setup_unknown_stop_revokes_waiting_status_before_later_enable(supervised_rig, monkeypatch):
    import microscope.calibrate as module

    rig = supervised_rig()
    entered, release = threading.Event(), threading.Event()
    original = rig.raw.read_status
    original_stop = rig.raw.stop_all

    def read_status(**kwargs):
        entered.set()
        assert release.wait(2)
        return original(**kwargs)

    def stop_all(**kwargs):
        reply = original_stop(**kwargs)
        release.set()
        return reply

    monkeypatch.setattr(rig.raw, "read_status", read_status)
    monkeypatch.setattr(rig.raw, "stop_all", stop_all)
    rig.session.operator = SetupOperator("", controls=["?"])
    rig.session.operator.control_ready = entered.is_set

    def action(operation):
        operation.call("read_status")
        operation.call("focus_enable")

    assert module._setup_io(rig.session, action) is module._SETUP_STOPPED
    assert "FOCUS ENABLE" not in rig.port.commands
    assert not rig.session.cancel_event.is_set()


def test_setup_late_thread_start_cannot_admit_after_interruption(supervised_rig, monkeypatch):
    import microscope.calibrate as module

    rig = supervised_rig()
    rig.session.operator = SetupOperator("")
    threads = []
    start = threading.Thread.start

    def fail(thread):
        if thread.name == "microscope-setup-action":
            threads.append(thread)
            raise KeyboardInterrupt("scripted start interruption")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(KeyboardInterrupt, match="scripted start interruption"):
        module._setup_io(rig.session, lambda operation: operation.call("focus_enable"))
    assert rig.session.cancel_event.is_set()
    start(threads[0])
    threads[0].join(2)
    assert not threads[0].is_alive()
    assert "FOCUS ENABLE" not in rig.port.commands


def test_setup_undrained_accepted_call_retains_serial_owner(session_factories, monkeypatch):
    import microscope.calibrate as module

    rig = session_factories
    entered, release = threading.Event(), threading.Event()
    real_join = threading.Thread.join

    def no_wait(thread, timeout=None):
        if thread.name == "microscope-setup-action":
            return None  # Deterministically represent expiry of the unchanged drain budget.
        return real_join(thread, timeout)

    monkeypatch.setattr(threading.Thread, "join", no_wait)
    operator = SetupOperator("", controls=["q"])
    operator.control_ready = entered.is_set
    kept = []
    try:
        with pytest.raises(RuntimeError, match="serial ownership retained"):
            with module.setup_session(
                environ=rig.settings, operator=operator, motor_factory=rig.factories["motor"]
            ) as session:
                kept.append(session)

                def action(operation):
                    operation.check()
                    entered.set()
                    assert release.wait(3)
                    operation.check()

                module._setup_io(session, action)
        assert kept[0]._raw_motor.connected
        assert "serial close" not in rig.events
        assert not rig.port.sim.focus.enabled and not rig.port.sim.objective.enabled
    finally:
        release.set()
        worker = kept[0]._setup_worker
        real_join(worker, 2)
        assert not worker.is_alive()
        kept[0]._raw_motor.disconnect()


@pytest.mark.parametrize("quit_key", ["q", None])
def test_setup_earlier_worker_failure_cannot_be_relabelled_by_later_quit(quit_key):
    from microscope.calibrate import _setup_io

    motor = SimpleNamespace(
        session=1, reference_epoch=1, connected=True, fault=None, safe_stop_disable=lambda: None
    )

    class Operator:
        @contextmanager
        def motion_controls(self, check):
            def poll():
                # Force the worker failure to finish before the queued quit.
                session._setup_worker.join(1)
                assert not session._setup_worker.is_alive()
                return quit_key

            yield poll

    session = SupervisedSession(motor, None, None, None, Operator())

    def action(operation):
        raise ValueError("worker failed before quit")

    with pytest.raises(ValueError, match="worker failed before quit"):
        _setup_io(session, action)
    assert session.failure == "worker failed before quit"
    assert session.operator_stopped is False
    assert session.cancel_event.is_set()
