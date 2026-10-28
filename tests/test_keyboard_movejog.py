import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mg400_agent import keyboard_jog
from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl
from mg400_agent.joint_limits import MARGIN_DEG, validate_joints
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
    _four_values = staticmethod(JointControl._four_values)

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
        return SimpleNamespace(pose=self.control.pose, requested_at=self.control.now)

    @staticmethod
    def _near(first, second, tolerance=0.1):
        return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))

    def solve_pose(self, pose):
        return self.control.sample.joints_deg

    def _query(self, text):
        self.control.before_query(text)
        self.control.exchanges.append(text)
        assert text in ("CalcUser(0,1,{0,0,0,0})", "CalcUser(8,1,{0,0,0,0})")
        index = int(text.split("(")[1].split(",")[0])
        return ",".join(str(value) for value in self.control.frames[index])

    def _guarded_command(self, text, **kwargs):
        self.control.before_send(text)
        if self.send_guard:
            self.send_guard()
        self._fresh(kwargs["send_modes"])
        self.control.commands.append(text)
        self.control.exchanges.append(text)
        if text.startswith("SpeedFactor("):
            self.control.speed_ack = self.control.now
            self.control.sample = replace(
                self.control.sample, speed_scaling_raw=self.control.scaling_after_ack
            )
        elif text.startswith("MoveJog("):
            self.control.sample = replace(self.control.sample, mode=11)
        elif text.startswith("SetUser(8,{"):
            self.control.frames[8] = tuple(
                float(value) for value in text.split("{")[1][:-2].split(",")
            )
        elif text.startswith("MovL("):
            self.control.pose = tuple(float(value) for value in text[5:].split(",")[:4])
        elif text.startswith("JointMovJ("):
            joints = tuple(float(value) for value in text[10:].split(",")[:4])
            self.control.sample = replace(self.control.sample, joints_deg=joints, mode=5)
        else:
            raise AssertionError(f"Unexpected movement: {text}")

    def wait_for_command(self):
        return True

    def stop_after_command(self, *, jog, on_error, jog_speed_percent=1):
        self.control.stop_calls.append(jog)
        self.control.stop_speeds.append(jog_speed_percent)
        normal_jog = (
            jog
            and not self.stopped
            and bool(self.control.commands)
            and self.control.commands[-1].startswith("MoveJog(")
        )
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
            self.exchanges = []
            self.stop_speeds = []
            self.pose_reads = self.ticks = 0
            self.pose = POSE
            self.frames = {0: (0, 0, 0, 0), 8: (0, 0, 0, 0)}
            self.stopped = self.busy = False
            self.feedback_error = None
            self.speed_ack = None
            self.scaling_after_ack = 1
            self.receipt = lambda now: now
            self.before_send = lambda text: None
            self.before_query = lambda text: None
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


@pytest.mark.parametrize(
    "speed,after",
    [(25, 1), (1, None), (1, 68), (1, float("nan"))],
    ids=["selected_25_reads_1", "missing", "reads_68", "nan"],
)
def test_unconfirmed_speed_blocks_preparation_and_movement(rig, speed, after):
    rig.session = KeyboardJogSession(rig, speed_percent=speed)
    rig.scaling_after_ack = after
    with pytest.raises(RuntimeError, match=rf"SpeedFactor\({speed}\)"):
        rig.session.prepare()
    assert rig.commands == [f"SpeedFactor({speed})"]
    assert rig.stopped and not rig.session.ready
    with pytest.raises(RuntimeError):
        rig.session.run("Q", threading.Event())


@pytest.mark.parametrize("receipt", ["pre_ack", "one_post_ack"])
def test_prepare_rejects_old_or_repeated_speed_feedback(rig, receipt):
    rig.receipt = lambda now: 10.0 if receipt == "pre_ack" else min(now, 10.005)
    with pytest.raises(TimeoutError, match="speed feedback"):
        rig.session.prepare()
    assert not rig.session.ready and rig.stopped
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


@pytest.mark.parametrize("heading", [30.0, -40.0, 90.0])
@pytest.mark.parametrize("key,axis", [("W", "X+"), ("S", "X-")])
def test_radial_long_hold_uses_one_user8_jog_instead_of_finite_segments(rig, key, axis, heading):
    rig.sample = replace(rig.sample, joints_deg=(heading, *START[1:]))
    rig.session.prepare()
    rig.commands.clear()
    rig.exchanges.clear()
    reads = rig.pose_reads
    rig.on_tick = lambda: rig.session.release() if rig.ticks == 1000 else None
    rig.session.run(key, threading.Event())
    movement = [text for text in rig.commands if text.startswith(("MoveJog(", "MovL("))]
    assert movement == [f"MoveJog({axis},CoordType=0,User=8,Tool=0)", "MoveJog()"], (
        f"Expected one continuous radial jog, got {len(movement)} motion/stop commands"
    )
    assert rig.exchanges[:2] == ["CalcUser(0,1,{0,0,0,0})", "CalcUser(8,1,{0,0,0,0})"]
    assert rig.exchanges[2].startswith("SetUser(8,{")
    assert rig.frames[8] == (0, 0, 0, heading)
    assert rig.exchanges[3] == "CalcUser(8,1,{0,0,0,0})"
    assert rig.pose_reads == reads
    assert rig.ticks == 1000 and rig.stop_calls == [True]
    assert rig.session.ready and not rig.stopped and not rig.busy


def test_radial_frame_already_aligned_is_read_back_without_rewriting(rig):
    rig.frames[8] = (0, 0, 0, START[0])
    rig.session.prepare()
    rig.commands.clear()
    rig.exchanges.clear()
    rig.session.run("W", threading.Event())
    assert rig.commands == ["MoveJog(X+,CoordType=0,User=8,Tool=0)", "MoveJog()"]
    assert rig.exchanges[:2] == [
        "CalcUser(0,1,{0,0,0,0})",
        "CalcUser(8,1,{0,0,0,0})",
    ]


@pytest.mark.parametrize("phase", ["before_run", "query", "set_user", "readback", "jog_send"])
def test_radial_release_before_send_never_starts_movement(rig, phase):
    rig.session.prepare()
    rig.commands.clear()
    rig.exchanges.clear()
    released = threading.Event()
    if phase == "before_run":
        released.set()
    elif phase == "query":
        rig.before_query = lambda text: released.set()
    elif phase == "readback":

        def release_during_readback(text):
            if text.startswith("CalcUser(8,") and any(
                command.startswith("SetUser(") for command in rig.commands
            ):
                released.set()

        rig.before_query = release_during_readback
    else:

        def release_at_send(text):
            if text.startswith("SetUser(" if phase == "set_user" else "MoveJog("):
                released.set()

        rig.before_send = release_at_send
    rig.session.run("W", released)
    assert not any(text.startswith(("MovL(", "MoveJog(X")) for text in rig.commands)
    if phase not in ("jog_send", "readback"):
        assert not any(text.startswith("SetUser(") for text in rig.commands)
    if phase == "readback":
        assert released.is_set()
        assert len(rig.exchanges) == 4
        assert rig.exchanges[2].startswith("SetUser(8,{")
        assert rig.exchanges[3] == "CalcUser(8,1,{0,0,0,0})"
        assert rig.commands[-1] == "ResetRobot()"
        assert not rig.stopped
    assert rig.session.fault is None and not rig.busy


@pytest.mark.parametrize("outcome", ["ack_failure", "released_after_ack"])
def test_radial_set_user_completion_cannot_start_a_cancelled_hold(rig, monkeypatch, outcome):
    rig.session.prepare()
    rig.commands.clear()
    rig.exchanges.clear()
    released = threading.Event()
    original = Operation._guarded_command

    def complete_set_user(operation, text, **kwargs):
        original(operation, text, **kwargs)
        if text.startswith("SetUser("):
            if outcome == "ack_failure":
                raise TimeoutError("SetUser acknowledgement lost")
            released.set()

    monkeypatch.setattr(Operation, "_guarded_command", complete_set_user)
    if outcome == "ack_failure":
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            rig.session.run("W", released)
        assert rig.stopped and rig.session.fault
    else:
        rig.session.run("W", released)
        assert not rig.stopped and rig.session.fault is None
    assert len(rig.exchanges) == 3
    assert rig.exchanges[-1].startswith("SetUser(8,{")
    assert rig.commands[-1] == "ResetRobot()"
    assert not any(text.startswith(("MoveJog(X", "MovL(")) for text in rig.commands)


def test_radial_next_hold_reads_and_realigns_the_reserved_frame(rig):
    rig.session.prepare()
    rig.session.run("W", threading.Event())
    rig.ticks = 0
    rig.commands.clear()
    rig.exchanges.clear()
    rig.sample = replace(rig.sample, joints_deg=(40.0, *START[1:]))
    rig.session.run("S", threading.Event())
    assert rig.frames[8] == (0, 0, 0, 40.0)
    assert rig.exchanges[2].startswith("SetUser(8,{")
    assert rig.commands[-2:] == ["MoveJog(X-,CoordType=0,User=8,Tool=0)", "MoveJog()"]
    assert rig.session.ready and not rig.stopped


@pytest.mark.parametrize("fault", ["user0", "readback", "nonfinite_readback"])
def test_radial_invalid_frame_prevents_movement_and_revokes_readiness(rig, fault):
    rig.session.prepare()
    rig.commands.clear()
    if fault == "user0":
        rig.frames[0] = (1, 0, 0, 0)
    else:

        def corrupt_readback(text):
            if text.startswith("CalcUser(8,") and any(
                command.startswith("SetUser(") for command in rig.commands
            ):
                rig.frames[8] = (0, 0, 0, 31 if fault == "readback" else float("nan"))

        rig.before_query = corrupt_readback
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    assert not any(text.startswith(("MovL(", "MoveJog(X")) for text in rig.commands)
    if fault == "user0":
        assert not any(text.startswith("SetUser(") for text in rig.commands)
    assert rig.stopped and rig.session.fault and not rig.session.ready


@pytest.mark.parametrize("when", ["before_jog", "during_hold"])
def test_radial_j1_change_rejects_or_stops_the_stale_frame(rig, when):
    rig.session.prepare()
    rig.commands.clear()

    def turn_base():
        rig.sample = replace(rig.sample, joints_deg=(START[0] + 0.11, *START[1:]))

    if when == "before_jog":
        rig.before_send = lambda text: turn_base() if text.startswith("MoveJog(X") else None
    else:
        rig.on_tick = lambda: turn_base() if rig.ticks == 3 else None
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    jogs = [text for text in rig.commands if text.startswith("MoveJog(X")]
    assert len(jogs) == (0 if when == "before_jog" else 1)
    assert rig.commands[-1] == "ResetRobot()"
    assert rig.stopped and rig.session.fault and not rig.session.ready


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


@pytest.mark.parametrize("start,key", [(159.2, "D"), (-159.2, "A")])
def test_inward_retreat_switches_to_one_continuous_jog_in_the_same_hold(rig, start, key):
    rig.sample = replace(rig.sample, joints_deg=(start, *START[1:]))
    rig.session.prepare()
    rig.commands.clear()
    rig.session.run(key, threading.Event())
    finite = [text for text in rig.commands if text.startswith("JointMovJ(")]
    assert len(finite) == 1, f"Expected one finite retreat before jog, got {len(finite)}"
    target = tuple(float(value) for value in finite[0][10:].split(",")[:4])
    validate_joints(target)
    assert abs(target[0] - start) <= 1
    assert rig.commands == [finite[0], COMMANDS[key], "MoveJog()"]
    assert rig.stop_calls == [True]
    assert rig.session.ready and not rig.stopped and not rig.busy


def test_retreat_keeps_last_valid_target_when_next_half_is_inside_margin(rig):
    rig.sample = replace(rig.sample, joints_deg=(159.9, *START[1:]))
    rig.session.prepare()
    rig.commands.clear()
    rig.session.run("D", threading.Event())
    finite = [text for text in rig.commands if text.startswith("JointMovJ(")]
    assert len(finite) == 1
    target = tuple(float(value) for value in finite[0][10:].split(",")[:4])
    validate_joints(target)
    assert target[0] == pytest.approx(159.9 - 1.25)
    with pytest.raises(ValueError):
        validate_joints((159.9 - 0.625, *START[1:]))
    assert rig.commands == [finite[0], COMMANDS["D"], "MoveJog()"]
    assert rig.session.ready and not rig.stopped


def test_retreat_ik_query_failure_does_not_send_previous_valid_target(rig, monkeypatch):
    rig.sample = replace(rig.sample, joints_deg=(30.0, 84.5, 30.0, 0.0))
    queried = []

    def solve_then_fail(operation, pose):
        queried.append(pose)
        if len(queried) == 1:
            return (30.0, 83.0, 30.0, 0.0)
        raise TimeoutError("Inverse kinematics query failed during retreat refinement")

    monkeypatch.setattr(Operation, "solve_pose", solve_then_fail)
    rig.session.prepare()
    rig.commands.clear()
    with pytest.raises(RuntimeError, match="query failed during retreat refinement"):
        rig.session.run("W", threading.Event())
    assert len(queried) == 2
    assert rig.commands == ["ResetRobot()"]
    assert rig.stopped and rig.session.fault and not rig.session.ready


@pytest.mark.parametrize("phase", ["finite_arrival", "jog_send", "close_window"])
def test_release_during_retreat_handoff_prevents_continuous_jog(rig, phase):
    rig.sample = replace(rig.sample, joints_deg=(159.2, *START[1:]))
    rig.session.prepare()
    rig.commands.clear()
    released = threading.Event()
    if phase == "finite_arrival":
        rig.on_tick = lambda: released.set() if rig.ticks == 1 else None
    elif phase == "close_window":
        rig.before_send = lambda text: (
            setattr(rig.session, "_closed", True) if text == COMMANDS["D"] else None
        )
    else:
        rig.before_send = lambda text: released.set() if text == COMMANDS["D"] else None
    rig.session.run("D", released)
    finite = [text for text in rig.commands if text.startswith("JointMovJ(")]
    assert len(finite) == 1
    assert released.is_set() or rig.session._closed
    assert rig.commands == [finite[0], "ResetRobot()"]
    assert rig.session.fault is None and not rig.stopped and not rig.busy


@pytest.mark.parametrize("fault", ["mode7", "stale"])
def test_retreat_handoff_requires_fresh_idle_feedback(rig, monkeypatch, fault):
    rig.sample = replace(rig.sample, joints_deg=(159.2, *START[1:]))
    rig.session.prepare()
    rig.commands.clear()
    original = Operation._fresh

    def reject_handoff(operation, modes):
        if modes == {5} and any(text.startswith("JointMovJ(") for text in rig.commands):
            if fault == "mode7":
                rig.sample = replace(rig.sample, mode=7)
            else:
                rig.feedback_error = "Feedback is stale"
        return original(operation, modes)

    monkeypatch.setattr(Operation, "_fresh", reject_handoff)
    with pytest.raises(RuntimeError):
        rig.session.run("D", threading.Event())
    assert sum(text.startswith("JointMovJ(") for text in rig.commands) == 1
    assert not any(text.startswith("MoveJog(") for text in rig.commands)
    assert rig.stopped and rig.session.fault and not rig.session.ready


@pytest.mark.parametrize("key,axis", [("W", "X+"), ("S", "X-")])
def test_radial_retreat_prepares_user8_then_resumes_continuous_hold(rig, monkeypatch, key, axis):
    rig.sample = replace(rig.sample, joints_deg=(30.0, 84.5, 30.0, 0.0))
    target_joints = (30.0, 83.0, 30.0, 0.0)
    monkeypatch.setattr(Operation, "solve_pose", lambda operation, pose: target_joints)
    original = Operation._guarded_command

    def complete_cartesian_retreat(operation, text, **kwargs):
        original(operation, text, **kwargs)
        if text.startswith("MovL("):
            rig.sample = replace(rig.sample, joints_deg=target_joints)

    monkeypatch.setattr(Operation, "_guarded_command", complete_cartesian_retreat)
    rig.session.prepare()
    rig.commands.clear()
    rig.exchanges.clear()
    rig.session.run(key, threading.Event())
    finite = [text for text in rig.commands if text.startswith("MovL(")]
    assert len(finite) == 1
    target_pose = tuple(float(value) for value in finite[0][5:].split(",")[:4])
    distance = sum((a - b) ** 2 for a, b in zip(target_pose[:3], POSE[:3], strict=True)) ** 0.5
    assert 0 < distance <= 2
    assert rig.exchanges[0] == finite[0]
    assert rig.exchanges[1:3] == ["CalcUser(0,1,{0,0,0,0})", "CalcUser(8,1,{0,0,0,0})"]
    assert rig.exchanges[3].startswith("SetUser(8,{")
    assert rig.exchanges[4] == "CalcUser(8,1,{0,0,0,0})"
    assert rig.commands[-2:] == [f"MoveJog({axis},CoordType=0,User=8,Tool=0)", "MoveJog()"]
    assert rig.stop_calls == [True] and rig.session.ready and not rig.stopped


@pytest.mark.parametrize("case", ["promoted_jog", "promoted_old_margin", "unpromoted_jog"])
def test_watcher_handles_feedback_that_overlaps_retreat_promotion(rig, monkeypatch, case):
    rig.session.prepare()
    released = threading.Event()
    active = rig.session._start("running", released)
    assert not rig.session._continuous
    original = Operation._fresh

    def promotion_during_read(operation, modes):
        assert modes == {5, 7, 11}
        rig.session._continuous = case != "unpromoted_jog"
        rig.sample = replace(
            rig.sample,
            mode=7 if case == "promoted_old_margin" else 11,
            joints_deg=(159.2, *START[1:]) if case == "promoted_old_margin" else START,
        )
        return original(operation, modes)

    monkeypatch.setattr(Operation, "_fresh", promotion_during_read)
    rig.session._watch(active, released, OneTick())
    if case == "unpromoted_jog":
        assert rig.session.fault and rig.stopped and rig.session._revoked.is_set()
    else:
        assert rig.session.fault is None and not rig.stopped
        assert rig.session.limit_reason is None and not rig.session._revoked.is_set()
    rig.session._finish(active)


@pytest.mark.parametrize("selected,changed", [(1, 68), (25, 1)])
@pytest.mark.parametrize("when", ["before_run", "before_send"])
def test_speed_change_before_send_prevents_directional_jog(rig, when, selected, changed):
    rig.session = KeyboardJogSession(rig, speed_percent=selected)
    rig.scaling_after_ack = selected
    rig.session.prepare()
    if when == "before_run":
        rig.sample = replace(rig.sample, speed_scaling_raw=changed)
    else:

        def change_speed(text):
            rig.sample = replace(rig.sample, speed_scaling_raw=changed)

        rig.before_send = change_speed
    with pytest.raises(RuntimeError, match="speed changed"):
        rig.session.run("Q", threading.Event())
    assert not any(command in COMMANDS.values() for command in rig.commands)
    assert rig.stop_calls == [True] and rig.commands[-1] == "ResetRobot()" and rig.stopped
    assert not rig.session.ready


def test_unconfirmed_stop_blocks_following_holds(rig):
    rig.session.prepare()
    rig.stop_result = {"ok": False, "error": "Lost stop acknowledgement"}
    with pytest.raises(RuntimeError, match="stop unconfirmed"):
        rig.session.run("Q", threading.Event())
    assert rig.stopped and not rig.session.ready and not rig.busy
    with pytest.raises(RuntimeError):
        rig.session.run("E", threading.Event())


def fast_session(rig, joints, speed=40):
    rig.sample = replace(rig.sample, joints_deg=joints)
    rig.session = KeyboardJogSession(rig, speed_percent=speed)
    rig.scaling_after_ack = speed
    rig.session.prepare()
    rig.commands.clear()


NEAR_J1 = (157.0, *START[1:])


@pytest.mark.parametrize(
    "speed,start,key,step,last_tick,release_tick,limited",
    [
        pytest.param(1, START, "A", 126.0, 3, 20, False, id="band_small_at_1pct"),
        pytest.param(40, START, "A", 126.0, 3, 20, True, id="band_grows_at_40pct"),
        pytest.param(40, NEAR_J1, "A", 0.1, 6, 20, True, id="closing_on_bound"),
        pytest.param(40, NEAR_J1, "D", -0.1, 6, 20, False, id="leaving_bound"),
        # J3 is 3 degrees above its lower bound, inside the 4.25-degree band at 40%.
        pytest.param(40, (30.0, 0.0, -22.0, 0.0), "A", 1.0, 10, 20, False, id="other_bound"),
        pytest.param(40, NEAR_J1, "A", 0.005, 12, 30, True, id="slow_closing_over_noise"),
    ],
)
def test_continuous_soft_limit_band(rig, speed, start, key, step, last_tick, release_tick, limited):
    fast_session(rig, start, speed)
    assert rig.session.continuous_margin == pytest.approx(
        MARGIN_DEG + keyboard_jog.CONTINUOUS_MARGIN_DEG_PER_PERCENT * speed
    )

    def move():
        if 3 <= rig.ticks <= last_tick:
            joints = rig.sample.joints_deg
            rig.sample = replace(rig.sample, joints_deg=(joints[0] + step, *joints[1:]))
        elif rig.ticks == release_tick:
            rig.session.release()

    rig.on_tick = move
    rig.session.run(key, threading.Event())
    # The hold starts as one continuous jog in either direction.
    assert rig.commands == [COMMANDS[key], "MoveJog()"]
    assert rig.stop_calls == [True]
    assert bool(rig.session.limit_reason) is limited
    if limited:
        assert "closing on its reference limit 160" in rig.session.limit_reason
    else:
        assert rig.session.limit_reason is None
    assert rig.session.fault is None and not rig.stopped
