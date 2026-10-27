import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mg400_agent import keyboard_jog
from mg400_agent.feedback import Feedback
from mg400_agent.keyboard_jog import KeyboardJogSession

START = (30.0, 0.0, 10.0, 0.0)
POSE = (250.0, 10.0, 40.0, 30.0)
COMMANDS = {
    "A": "MoveJog(J1+)",
    "D": "MoveJog(J1-)",
    "Q": "MoveJog(J4+)",
    "E": "MoveJog(J4-)",
    "R": "MoveJog(Z+,CoordType=0,User=0,Tool=0)",
    "F": "MoveJog(Z-,CoordType=0,User=0,Tool=0)",
}


class DeferredThread:
    """Run a worker at join. Tests invoke watcher ticks explicitly."""

    def __init__(self, *, target, args, **kwargs):
        self.target, self.args = target, args
        self.finished = False

    def start(self):
        pass

    def join(self):
        if not self.finished:
            self.finished = True
            self.target(*self.args)


class OneTick:
    def __init__(self):
        self.used = False

    def wait(self, timeout):
        used, self.used = self.used, True
        return used


class Operation:
    def __init__(self, control, send_guard):
        self.control, self.send_guard = control, send_guard
        self.cancelled = False

    @property
    def stopped(self):
        return self.cancelled or self.control.stopped

    def cancel(self):
        self.cancelled = True

    def _fresh(self, modes):
        control = self.control
        if control.feedback_error:
            raise RuntimeError(control.feedback_error)
        if control.sample.mode not in modes or control.sample.error_status:
            raise RuntimeError("Unexpected robot state")
        received = control.receipt(control.now)
        control.receipts.append(received)
        return control.sample, received

    def _preflight(self, modes):
        return self._fresh(modes)[0].mode, self.control.sample.joints_deg

    def _read_pose_measurement(self, modes):
        self._fresh(modes)
        self.control.pose_reads += 1
        return SimpleNamespace(pose=POSE, requested_at=self.control.now)

    def _guarded_command(self, text, **kwargs):
        self.control.before_send(text)
        if self.send_guard:
            self.send_guard()
        self._fresh(kwargs["send_modes"])
        self.control.commands.append(text)
        if text.startswith("SpeedFactor("):
            self.control.speed_ack = self.control.now
            self.control.sample = replace(
                self.control.sample, speed_scaling_raw=self.control.scaling_after_ack
            )
        elif text.startswith("MoveJog("):
            self.control.sample = replace(self.control.sample, mode=11)
        else:
            raise AssertionError(f"Unexpected movement: {text}")

    def wait_for_command(self):
        return True

    def stop_after_command(self, *, jog, on_error, jog_speed_percent=1):
        self.control.stop_calls.append(jog)
        self.control.stop_speeds.append(jog_speed_percent)
        normal_jog = jog and not self.stopped
        self.control.commands.append("MoveJog()" if normal_jog else "ResetRobot()")
        self.control.sample = replace(self.control.sample, mode=5)
        return self.control.stop_result

    def finish_operation(self):
        self.control.busy = False


@pytest.fixture
def rig(monkeypatch):
    class Rig:
        def __init__(self):
            self.now = 10.0
            self.sample = Feedback(START, 5, 0, False, 1)
            self.commands, self.stop_calls, self.receipts = [], [], []
            self.stop_speeds = []
            self.pose_reads = self.ticks = 0
            self.stopped = self.busy = False
            self.feedback_error = None
            self.speed_ack = None
            self.scaling_after_ack = 1
            self.receipt = lambda now: now
            self.before_send = lambda text: None
            self.on_tick = lambda: self.session.release() if self.ticks == 20 else None
            self.stop_result = {"ok": True, "stationary_verified": True}
            self.session = KeyboardJogSession(self)

        def cancel(self):
            self.stopped = True

        def begin_operation(self, *, command_timeout, send_guard):
            assert not self.busy
            assert command_timeout == 0.5
            self.busy = True
            return Operation(self, send_guard)

        def sleep(self, duration):
            self.now += duration
            if self.session._phase == "running":
                self.ticks += 1
                assert self.ticks <= 2000, "A fake hold did not stop"
                self.on_tick()
                if not self.session._revoked.is_set():
                    self.session._watch(self.session._active, self.session._released, OneTick())

    def no_network(*args, **kwargs):
        pytest.fail("Network access is forbidden in keyboard simulation tests")

    for name in ("socket", "create_connection", "socketpair", "getaddrinfo"):
        monkeypatch.setattr(socket, name, no_network)
    robot = Rig()
    monkeypatch.setattr(
        keyboard_jog, "time", SimpleNamespace(monotonic=lambda: robot.now, sleep=robot.sleep)
    )
    monkeypatch.setattr(
        keyboard_jog,
        "threading",
        SimpleNamespace(Lock=threading.Lock, Event=threading.Event, Thread=DeferredThread),
    )
    return robot


def test_prepare_sends_only_speed_and_requires_three_post_ack_receipts(rig):
    assert rig.session.prepare() == POSE
    assert rig.commands == ["SpeedFactor(1)"]
    assert len({received for received in rig.receipts if received > rig.speed_ack}) == 3
    assert rig.now - rig.speed_ack >= 0.015
    assert rig.session.ready and not rig.busy


@pytest.mark.parametrize("speed", [0, 101, -1, True, 1.0, 1.5, "25", None, float("nan")])
def test_invalid_selected_speed_is_rejected_before_any_command(rig, speed):
    with pytest.raises(ValueError, match="integer from 1 to 100"):
        KeyboardJogSession(rig, speed_percent=speed)
    assert not rig.commands and not rig.busy


@pytest.mark.parametrize("speed", [25, 100])
def test_selected_speed_is_confirmed_and_passed_to_stop(rig, speed):
    rig.session = KeyboardJogSession(rig, speed_percent=speed)
    rig.scaling_after_ack = speed
    rig.session.prepare()
    assert rig.commands == [f"SpeedFactor({speed})"]
    with pytest.raises(AttributeError):
        rig.session.speed_percent = 1
    rig.session.run("Q", threading.Event())
    assert rig.commands == [f"SpeedFactor({speed})", COMMANDS["Q"], "MoveJog()"]
    assert rig.stop_speeds == [speed]
    assert rig.session.ready and not rig.stopped


def test_selected_speed_mismatch_blocks_preparation_and_movement(rig):
    rig.session = KeyboardJogSession(rig, speed_percent=25)
    with pytest.raises(RuntimeError, match=r"SpeedFactor\(25\)"):
        rig.session.prepare()
    assert rig.commands == ["SpeedFactor(25)"]
    assert not rig.session.ready and rig.stopped
    with pytest.raises(RuntimeError):
        rig.session.run("Q", threading.Event())


@pytest.mark.parametrize("receipt", ["pre_ack", "one_post_ack"])
def test_prepare_rejects_old_or_repeated_speed_feedback(rig, receipt):
    rig.receipt = lambda now: 10.0 if receipt == "pre_ack" else min(now, 10.005)
    with pytest.raises(TimeoutError, match="speed feedback"):
        rig.session.prepare()
    assert not rig.session.ready and rig.stopped
    assert rig.commands == ["SpeedFactor(1)"]


@pytest.mark.parametrize("speed", [None, 68, float("nan")])
def test_prepare_rejects_unconfirmed_speed(rig, speed):
    rig.scaling_after_ack = speed
    with pytest.raises(RuntimeError, match="SpeedFactor"):
        rig.session.prepare()
    assert rig.stopped and not rig.session.ready
    assert rig.commands == ["SpeedFactor(1)"]


@pytest.mark.parametrize("key,command", COMMANDS.items())
def test_six_keys_send_one_continuous_command_and_stop_without_pose_queries(rig, key, command):
    rig.session.prepare()
    rig.commands.clear()
    reads = rig.pose_reads
    rig.on_tick = lambda: rig.session.release() if rig.ticks == 1000 else None
    assert rig.session.run(key, threading.Event()) is None
    assert rig.commands == [command, "MoveJog()"]
    assert rig.ticks == 1000
    assert rig.pose_reads == reads
    assert rig.stop_calls == [True]
    assert rig.session.ready and not rig.stopped and not rig.busy


@pytest.mark.parametrize("cancel", ["release_event", "close", "parent_stop"])
def test_cancellation_stops_the_active_hold(rig, cancel):
    rig.session.prepare()
    released = threading.Event()
    actions = {"release_event": released.set, "close": rig.session.close, "parent_stop": rig.cancel}
    rig.on_tick = lambda: actions[cancel]() if rig.ticks == 3 else None
    if cancel == "parent_stop":
        with pytest.raises(RuntimeError):
            rig.session.run("Q", released)
    else:
        rig.session.run("Q", released)
    assert rig.stop_calls == [True]
    assert rig.commands[-1] == ("ResetRobot()" if cancel == "parent_stop" else "MoveJog()")
    assert not rig.busy
    assert rig.session.ready is (cancel == "release_event")


@pytest.mark.parametrize("speed", [1, 25])
@pytest.mark.parametrize("fault", ["feedback", "speed", "alarm"])
def test_feedback_faults_stop_and_revoke_readiness(rig, fault, speed):
    rig.session = KeyboardJogSession(rig, speed_percent=speed)
    rig.scaling_after_ack = speed
    rig.session.prepare()

    def inject():
        if rig.ticks != 3:
            return
        if fault == "feedback":
            rig.feedback_error = "Feedback is stale"
        elif fault == "speed":
            rig.sample = replace(rig.sample, speed_scaling_raw=68)
        else:
            rig.sample = replace(rig.sample, error_status=True)

    rig.on_tick = inject
    with pytest.raises(RuntimeError):
        rig.session.run("A", threading.Event())
    assert rig.stop_calls == [True] and rig.commands[-1] == "ResetRobot()"
    assert rig.stopped and not rig.session.ready and rig.session.fault
    with pytest.raises(RuntimeError):
        rig.session.run("D", threading.Event())


def test_soft_limit_stops_without_a_fault_and_requires_release(rig):
    rig.session.prepare()
    released = threading.Event()

    def reach_margin():
        if rig.ticks == 3:
            rig.sample = replace(rig.sample, joints_deg=(159.2, *START[1:]))

    rig.on_tick = reach_margin
    rig.session.run("A", released)
    assert rig.stop_calls == [True]
    assert rig.session.limit_reason and rig.session.fault is None and not rig.stopped
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("D", threading.Event())
    rig.session.release()
    assert released.is_set()


@pytest.mark.parametrize("when", ["before_run", "before_send"])
def test_released_input_never_sends_directional_jog(rig, when):
    rig.session.prepare()
    released = threading.Event()
    if when == "before_run":
        released.set()
    else:
        rig.before_send = lambda text: released.set()
    rig.session.run("Q", released)
    assert not any(command in COMMANDS.values() for command in rig.commands)
    assert rig.session.fault is None and not rig.busy


@pytest.mark.parametrize("when", ["before_run", "before_send"])
def test_speed_change_before_send_prevents_directional_jog(rig, when):
    rig.session.prepare()
    if when == "before_run":
        rig.sample = replace(rig.sample, speed_scaling_raw=68)
    else:

        def change_speed(text):
            rig.sample = replace(rig.sample, speed_scaling_raw=68)

        rig.before_send = change_speed
    with pytest.raises(RuntimeError, match="speed changed"):
        rig.session.run("Q", threading.Event())
    assert not any(command in COMMANDS.values() for command in rig.commands)
    assert rig.stop_calls == [True] and rig.commands[-1] == "ResetRobot()" and rig.stopped


def test_unconfirmed_stop_blocks_following_holds(rig):
    rig.session.prepare()
    rig.stop_result = {"ok": False, "error": "Lost stop acknowledgement"}
    with pytest.raises(RuntimeError, match="stop unconfirmed"):
        rig.session.run("Q", threading.Event())
    assert rig.stopped and not rig.session.ready and not rig.busy
    with pytest.raises(RuntimeError):
        rig.session.run("E", threading.Event())
