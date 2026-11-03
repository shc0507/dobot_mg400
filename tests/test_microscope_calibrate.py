"""Supervised calibration uses scripted people and in-memory firmware only."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from microscope.calibrate import ReferenceSettings, SupervisedSession, validated_focus_settings
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
