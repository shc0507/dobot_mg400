import math
import socket
import threading
from unittest.mock import Mock

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl
from mg400_agent.keyboard_jog import KeyboardJogSession

START = (30.0, 0.0, 10.0, 0.0)
POSE = (250.0, 10.0, 40.0, 30.0)


@pytest.fixture
def rig(monkeypatch):
    class Rig:
        def __init__(self):
            self.now = 10.0
            self.sample = Feedback(START, 5, 0, False)
            self.pose = POSE
            self.commands = []
            self.moves = []
            self.hook = lambda text: None
            self.reply_hook = lambda text: None
            self.connect_hook = lambda port: None
            self.stale = False
            self.finish_moves = True
            self.reset_failure = False
            self.solution = START
            self.release_after = 1
            self.session = None

        def snapshot(self):
            return self.sample, self.now - (1.0 if self.stale else 0)

        def sleep(self, duration):
            self.now += duration

        def command(self, text):
            self.now += 0.01
            self.commands.append(text)
            self.hook(text)
            if text == "RobotMode()":
                return str(self.sample.mode)
            if text == "GetErrorID()":
                return "[[],[],[],[],[]]"
            if text == "GetAngle()":
                return ",".join(map(str, self.sample.joints_deg))
            if text == "GetPose(0,0)":
                if self.release_after and len(self.moves) >= self.release_after:
                    self.session.release()
                return ",".join(map(str, self.pose))
            if text.startswith("InverseSolution("):
                x, y, z, r = map(float, text.split("(")[1].split(",")[:4])
                self.solution = (START[0], (x - POSE[0]) / 10, 10 + (z - POSE[2]) / 10,
                                 r - START[0])
                return ",".join(map(str, self.solution))
            if text.startswith(("MovL(", "JointMovJ(")):
                self.moves.append(text)
                target = tuple(map(float, text.split("(")[1].split(",")[:4]))
                if text.startswith("MovL("):
                    joints, self.pose = self.solution, target
                else:
                    joints = target
                    self.pose = (*self.pose[:3], joints[0] + joints[3])
                if self.finish_moves:
                    self.sample = Feedback(joints, 5, 0, False)
                else:
                    self.sample = Feedback(self.sample.joints_deg, 7, 0, False)
                return ""
            if text == "ResetRobot()":
                if self.reset_failure:
                    raise ConnectionError("Lost stop acknowledgement")
                self.sample = Feedback(self.sample.joints_deg, 5, 0, False)
                return ""
            raise AssertionError(f"Unexpected command: {text}")

    robot = Rig()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def sendall(self, data):
            text = data.decode()
            self.text = text
            value = robot.command(text)
            self.reply = f"0,{{{value}}},{text};".encode()

        def recv(self, size):
            robot.reply_hook(self.text)
            return self.reply

    def connect(address, timeout):
        robot.connect_hook(address[1])
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr("mg400_agent.keyboard_jog.time.monotonic", lambda: robot.now)
    monkeypatch.setattr("mg400_agent.keyboard_jog.time.sleep", robot.sleep)
    control = JointControl("robot.test", robot.snapshot)
    robot.session = KeyboardJogSession(control)
    return robot


def start_thread(function):
    outcome = []

    def worker():
        try:
            outcome.append(function())
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, outcome


def finish_thread(thread, outcome):
    thread.join(3)
    assert not thread.is_alive()
    assert len(outcome) == 1
    return outcome[0]


def test_prepare_is_read_only_and_shares_controller_locks(rig):
    assert not rig.session.ready
    assert not rig.commands
    assert rig.session.prepare() == POSE
    assert rig.session.ready
    assert rig.commands == ["RobotMode()", "GetErrorID()", "GetAngle()", "GetPose(0,0)"]
    rig.session.release()
    rig.session.release()
    assert rig.session.ready
    assert len(rig.commands) == 4


@pytest.mark.parametrize("key,axis,change", [
    ("W", 0, 5 * math.cos(math.radians(30))),
    ("S", 0, -5 * math.cos(math.radians(30))),
    ("R", 2, 5), ("F", 2, -5), ("Q", 3, 1), ("E", 3, -1),
])
def test_cartesian_keys_use_bounded_measured_targets(rig, key, axis, change):
    rig.session.prepare()
    result = rig.session.run(key, threading.Event())
    assert result[axis] == pytest.approx(POSE[axis] + change, abs=1e-6)
    if key in "WS":
        direction = 1 if key == "W" else -1
        assert result[1] == pytest.approx(POSE[1] + direction * 2.5)
    assert len(rig.moves) == 1
    assert rig.moves[0].endswith("User=0,Tool=0,SpeedL=2,AccL=20)")
    assert rig.commands.count("RobotMode()") == 1
    assert rig.commands.count("GetErrorID()") == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready
    assert not rig.session.control.stopped


@pytest.mark.parametrize("key,change", [("A", 1), ("D", -1)])
def test_base_keys_use_one_degree_joint_moves(rig, key, change):
    rig.session.prepare()
    rig.session.run(key, threading.Event())
    assert rig.sample.joints_deg == (START[0] + change, *START[1:])
    assert rig.moves == [
        f"JointMovJ({START[0] + change:.6f},0.000000,10.000000,0.000000,SpeedJ=5,AccJ=20)"
    ]
    assert not any(command.startswith("InverseSolution(") for command in rig.commands)


def test_holding_uses_measured_steps_without_repeated_preflight(rig, monkeypatch):
    rig.session.prepare()
    rig.release_after = 3
    stable = Mock(wraps=JointControl._wait_stable)
    monkeypatch.setattr(JointControl, "_wait_stable", lambda self, *a, **kw: stable(self, *a, **kw))
    rig.session.run("R", threading.Event())
    assert [float(command.split(",")[2]) for command in rig.moves] == [45, 50, 55]
    assert stable.call_count == 1  # Stop verification only; no per-step dwell.
    assert rig.commands.count("GetErrorID()") == 1
    rig.release_after = 4
    assert rig.session.run("F", threading.Event())[2] == 50
    assert rig.session.ready


def test_release_during_preparation_cannot_arm_or_send(rig):
    def release(text):
        if text == "GetPose(0,0)":
            rig.session.release()

    rig.hook = release
    with pytest.raises(RuntimeError, match="cancelled"):
        rig.session.prepare()
    assert not rig.session.ready
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    assert not rig.session.control.stopped
    rig.hook = lambda text: None
    assert rig.session.prepare() == POSE


def test_release_during_motion_connection_prevents_send(rig):
    rig.session.prepare()
    rig.connect_hook = lambda port: rig.session.release() if port == 30003 else None
    rig.session.run("W", threading.Event())
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready


def test_release_during_ik_prevents_send(rig):
    rig.session.prepare()
    rig.hook = lambda text: rig.session.release() if text.startswith("InverseSolution(") else None
    rig.session.run("W", threading.Event())
    assert not rig.moves
    assert rig.session.ready


def test_release_before_pending_registration_blocks_delayed_registration(rig, monkeypatch):
    rig.session.prepare()
    original = JointControl._guarded_command

    def guarded(active, *args, **kwargs):
        rig.session.release()
        return original(active, *args, **kwargs)

    monkeypatch.setattr(JointControl, "_guarded_command", guarded)
    rig.session.run("W", threading.Event())
    assert not rig.moves
    assert rig.session.control._pending is None
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready


def test_release_between_send_guard_and_send_waits_for_successful_ack(rig):
    rig.session.prepare()
    rig.hook = lambda text: rig.session.release() if text.startswith("MovL(") else None
    rig.session.run("W", threading.Event())
    assert len(rig.moves) == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready
    assert not rig.session.control.stopped
    assert not rig.session.control._stop_uncertain


def test_normal_release_waits_for_motion_ack_before_sending_reset(rig):
    rig.session.prepare()
    waiting_ack, allow_ack, reset_received = (threading.Event() for _ in range(3))

    def block_reply(text):
        if text.startswith("MovL("):
            waiting_ack.set()
            assert allow_ack.wait(2)
        elif text == "ResetRobot()":
            reset_received.set()

    rig.reply_hook = block_reply
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert waiting_ack.wait(1)
    rig.session.release()
    assert not reset_received.wait(0.03)
    assert not allow_ack.is_set()
    assert rig.session.busy
    assert not rig.session.control.stopped
    allow_ack.set()
    result = finish_thread(thread, outcome)
    assert not isinstance(result, Exception)
    assert reset_received.is_set()
    assert rig.session.ready
    assert not rig.session.control.stopped
    assert not rig.session.control._stop_uncertain
    assert len(rig.moves) == 1


@pytest.mark.parametrize("failure", [TimeoutError("ACK timed out"), ConnectionError("ACK lost")])
def test_normal_release_does_not_hide_failed_motion_ack(rig, failure):
    rig.session.prepare()

    def fail_ack(text):
        if text.startswith("MovL("):
            rig.session.release()
            raise failure

    rig.reply_hook = fail_ack
    with pytest.raises(RuntimeError, match="stop unconfirmed"):
        rig.session.run("W", threading.Event())
    assert len(rig.moves) == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert not rig.session.ready
    assert rig.session.control.stopped
    assert rig.session.control._stop_uncertain


def test_ack_wait_timeout_locks_even_if_successful_ack_arrives_later(rig):
    rig.session.prepare()
    waiting_ack, allow_ack, reset_received = (threading.Event() for _ in range(3))

    def block_reply(text):
        if text.startswith("MovL("):
            waiting_ack.set()
            assert allow_ack.wait(2)
        elif text == "ResetRobot()":
            reset_received.set()

    rig.reply_hook = block_reply
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert waiting_ack.wait(1)
    rig.session.release()
    # Let the stop worker take its pending snapshot before advancing its clock.
    assert not reset_received.wait(0.03)
    rig.now += 0.6
    assert reset_received.wait(1)
    allow_ack.set()
    result = finish_thread(thread, outcome)
    assert isinstance(result, RuntimeError)
    assert "acknowledgement did not finish" in str(result)
    assert rig.session.control.stopped
    assert rig.session.control._stop_uncertain
    assert not rig.session.ready
    assert len(rig.moves) == 1


def test_feedback_failure_during_release_ack_wait_stops_without_waiting(rig):
    rig.session.prepare()
    waiting_ack, allow_ack, reset_received = (threading.Event() for _ in range(3))

    def block_reply(text):
        if text.startswith("MovL("):
            waiting_ack.set()
            assert allow_ack.wait(2)
        elif text == "ResetRobot()":
            reset_received.set()

    rig.reply_hook = block_reply
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert waiting_ack.wait(1)
    rig.session.release()
    rig.stale = True
    assert reset_received.wait(0.3)
    allow_ack.set()
    result = finish_thread(thread, outcome)
    assert isinstance(result, RuntimeError)
    assert "stale" in str(result)
    assert rig.session.control.stopped
    assert not rig.session.ready


def test_release_during_flight_stops_instead_of_waiting_for_target(rig):
    rig.session.prepare()
    rig.finish_moves = False
    rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1
    assert rig.sample.joints_deg == START
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready


def test_expiry_stops_and_requires_a_real_release_before_next_hold(rig, monkeypatch):
    rig.session.prepare()
    rig.release_after = None
    released = threading.Event()

    monkeypatch.setattr("mg400_agent.keyboard_jog.HOLD_SECONDS", 0.045)
    rig.session.run("R", released)
    assert rig.session.expired
    assert not released.is_set()
    assert len(rig.moves) == 1
    with pytest.raises(RuntimeError, match="Release the expired key"):
        rig.session.run("R", threading.Event())
    rig.session.release()
    monkeypatch.setattr("mg400_agent.keyboard_jog.HOLD_SECONDS", 5.0)
    rig.release_after = 2
    rig.session.run("R", threading.Event())
    assert len(rig.moves) == 2


@pytest.mark.parametrize("problem", ["stale", "alarm", "stop"])
def test_feedback_loss_and_stop_failure_lock_future_motion(rig, problem):
    rig.session.prepare()

    def fail(text):
        if text == "GetPose(0,0)" and rig.moves:
            if problem == "stale":
                rig.stale = True
            elif problem == "alarm":
                rig.sample = Feedback(rig.sample.joints_deg, 5, 0, True)
            else:
                rig.reset_failure = True

    rig.hook = fail
    with pytest.raises(RuntimeError):
        rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1
    assert not rig.session.ready
    assert rig.session.control.stopped


def test_bad_ik_never_sends_motion(rig, monkeypatch):
    rig.session.prepare()
    original = rig.command

    def command(text):
        if text.startswith("InverseSolution("):
            return "200,0,10,0"
        return original(text)

    monkeypatch.setattr(rig, "command", command)
    with pytest.raises(RuntimeError, match="outside"):
        rig.session.run("W", threading.Event())
    assert not rig.moves


def test_cartesian_ik_crossing_invalid_joint_region_is_rejected(rig, monkeypatch):
    rig.sample = Feedback((0, -20, 2, 0), 5, 0, False)
    rig.session.prepare()
    original = rig.command

    def command(text):
        if text.startswith("InverseSolution("):
            return "0,-17,-2,0"
        return original(text)

    monkeypatch.setattr(rig, "command", command)
    with pytest.raises(RuntimeError, match="outside"):
        rig.session.run("W", threading.Event())
    assert not rig.moves


def test_old_receipt_cannot_complete_a_step_or_allow_a_second_move(rig, monkeypatch):
    rig.session.prepare()
    rig.release_after = None
    receipt = rig.now
    monkeypatch.setattr(rig.session.control, "feedback_snapshot", lambda: (rig.sample, receipt))
    with pytest.raises(RuntimeError, match="stale"):
        rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1


def test_no_second_hold_or_absolute_move_can_queue_while_hold_is_active(rig):
    rig.session.prepare()
    reached, unblock = threading.Event(), threading.Event()

    def block(text):
        if text.startswith("InverseSolution("):
            reached.set()
            assert unblock.wait(2)

    rig.hook = block
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert reached.wait(1)
    assert rig.session.busy
    with pytest.raises(RuntimeError, match="already in progress"):
        rig.session.run("W", threading.Event())
    with pytest.raises(RuntimeError, match="operation is in progress"):
        rig.session.control.move(START)
    rig.session.release()
    unblock.set()
    assert not isinstance(finish_thread(thread, outcome), Exception)
    assert not rig.moves


def test_watchdog_cancels_expired_hold_during_blocking_query(rig):
    rig.session.prepare()
    reached, unblock = threading.Event(), threading.Event()

    def block(text):
        if text.startswith("InverseSolution("):
            reached.set()
            assert unblock.wait(2)

    rig.hook = block
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert reached.wait(1)
    rig.now += 5.1
    assert rig.session._revoked.wait(1)
    assert rig.session.expired
    unblock.set()
    assert not isinstance(finish_thread(thread, outcome), Exception)
    assert not rig.moves


def test_parent_cancel_revokes_child_and_exposes_pending(rig):
    rig.session.prepare()

    def cancel(text):
        if text.startswith("MovL("):
            assert rig.session.control._pending is rig.session._active._pending
            rig.session.control.cancel(disabling=True)

    rig.hook = cancel
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    assert len(rig.moves) == 1
    assert rig.session.control._stop_uncertain
    assert not rig.session.ready


def test_parent_cancel_before_send_prevents_child_motion(rig):
    rig.session.prepare()
    rig.connect_hook = (
        lambda port: rig.session.control.cancel() if port == 30003 else None
    )
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    assert not rig.moves
    assert not rig.session.ready


def test_close_is_permanent_and_prevents_motion(rig):
    rig.session.prepare()
    rig.session.close()
    assert not rig.session.ready
    with pytest.raises(RuntimeError, match="closed"):
        rig.session.run("R", threading.Event())
    assert not rig.moves


@pytest.mark.parametrize("key", ["", "Z", "X", "WW", None])
def test_invalid_keys_do_not_read_or_send(rig, key):
    with pytest.raises(ValueError):
        rig.session.run(key, threading.Event())
    assert not rig.commands
