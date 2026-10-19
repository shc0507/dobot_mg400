import math
import socket
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl, TargetRejectedError
from mg400_agent.joint_limits import validate_joints
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
    ("W", 0, 50 * math.cos(math.radians(30))),
    ("S", 0, -50 * math.cos(math.radians(30))),
    ("R", 2, 50), ("F", 2, -50), ("Q", 3, 10), ("E", 3, -10),
])
def test_cartesian_keys_use_one_bounded_trajectory_from_measured_pose(rig, key, axis, change):
    rig.session.prepare()
    result = rig.session.run(key, threading.Event())
    assert result[axis] == pytest.approx(POSE[axis] + change, abs=1e-6)
    if key in "WS":
        direction = 1 if key == "W" else -1
        assert result[1] == pytest.approx(POSE[1] + direction * 25)
    assert len(rig.moves) == 1
    assert rig.moves[0].endswith("User=0,Tool=0,SpeedL=2,AccL=20)")
    assert rig.commands.count("RobotMode()") == 1
    assert rig.commands.count("GetErrorID()") == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready
    assert not rig.session.control.stopped


@pytest.mark.parametrize("key", ["W", "A"])
def test_fresh_pose_measurement_skips_only_the_initial_pose_query(rig, key):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement()
    rig.commands.clear()
    rig.session.run(key, threading.Event(), initial_pose=measurement)
    first_move = rig.commands.index(rig.moves[0])
    assert "GetPose(0,0)" not in rig.commands[:first_move]
    assert any(command.startswith("InverseSolution(") for command in rig.commands) is (key == "W")
    assert "GetPose(0,0)" in rig.commands[first_move:]
    assert len(rig.moves) == 1
    assert rig.session.ready


@pytest.mark.parametrize("invalid", [
    "missing", "expired", "future", "control", "before_mode", "after_mode",
    "before_alarm", "after_alarm", "query_changed", "current_changed", "combined_drift",
])
def test_unusable_pose_measurement_falls_back_to_current_pose_query(rig, invalid):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement()
    if invalid == "missing":
        measurement = None
    elif invalid == "expired":
        measurement = replace(measurement, requested_at=rig.now - 0.251)
    elif invalid == "future":
        measurement = replace(measurement, requested_at=rig.now + 0.01)
    elif invalid == "control":
        measurement = replace(measurement, control=JointControl("robot.test", rig.snapshot))
    elif invalid.endswith("mode"):
        field = invalid.split("_")[0]
        measurement = replace(measurement, **{
            field: replace(getattr(measurement, field), mode=4 if field == "before" else 7)
        })
    elif invalid.endswith("alarm"):
        field = invalid.split("_")[0]
        measurement = replace(measurement, **{
            field: replace(getattr(measurement, field), error_status=True)
        })
    elif invalid == "query_changed":
        measurement = replace(measurement, before=replace(
            measurement.before, joints_deg=(START[0] - 0.03, *START[1:])
        ))
    elif invalid == "current_changed":
        rig.sample = replace(rig.sample, joints_deg=(START[0] + 0.03, *START[1:]))
    else:
        measurement = replace(measurement, before=replace(
            measurement.before, joints_deg=(START[0] - 0.02, *START[1:])
        ))
        rig.sample = replace(rig.sample, joints_deg=(START[0] + 0.02, *START[1:]))
    rig.commands.clear()
    rig.session.run("W", threading.Event(), initial_pose=measurement)
    first_move = rig.commands.index(rig.moves[0])
    assert rig.commands[:first_move].count("GetPose(0,0)") == 1
    assert len(rig.moves) == 1
    assert rig.session.ready


def test_reused_pose_does_not_bypass_changed_joints_at_motion_send(rig):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement()

    def move_before_send(port):
        if port == 30003:
            rig.sample = replace(rig.sample, joints_deg=(START[0] + 0.2, *START[1:]))

    rig.connect_hook = move_before_send
    with pytest.raises(RuntimeError, match="Robot moved before command was sent"):
        rig.session.run("W", threading.Event(), initial_pose=measurement)
    assert not rig.moves
    assert rig.session.control.stopped


def test_released_key_cannot_start_from_reused_pose(rig):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement()
    released = threading.Event()
    released.set()
    rig.session.run("W", released, initial_pose=measurement)
    assert not rig.moves
    assert not any(command.startswith("InverseSolution(") for command in rig.commands)
    assert rig.session.ready


@pytest.mark.parametrize("key,change", [("A", 10), ("D", -10)])
def test_base_keys_use_one_ten_degree_joint_trajectory(rig, key, change):
    rig.session.prepare()
    rig.session.run(key, threading.Event())
    assert rig.sample.joints_deg == (START[0] + change, *START[1:])
    assert rig.moves == [
        f"JointMovJ({START[0] + change:.6f},0.000000,10.000000,0.000000,SpeedJ=5,AccJ=20)"
    ]
    assert not any(command.startswith("InverseSolution(") for command in rig.commands)


def test_travel_limit_ends_hold_without_queuing_another_trajectory(rig, monkeypatch):
    rig.session.prepare()
    prepared_at = rig.now
    rig.release_after = None
    released = threading.Event()
    stable = Mock(wraps=JointControl._wait_stable)
    monkeypatch.setattr(JointControl, "_wait_stable", lambda self, *a, **kw: stable(self, *a, **kw))
    assert rig.session.run("R", released)[2] == 90
    assert rig.session.measured_pose[0] == rig.pose
    assert prepared_at < rig.session.measured_pose[1] <= rig.now
    assert [float(command.split(",")[2]) for command in rig.moves] == [90]
    assert stable.call_count == 0
    assert rig.commands.count("GetErrorID()") == 1
    assert "ResetRobot()" not in rig.commands
    assert rig.session.limit_reason == "Travel limit reached."
    assert not rig.session.expired
    assert not released.is_set()
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("F", threading.Event())
    assert len(rig.moves) == 1
    rig.session.release()
    assert released.is_set()
    assert rig.session.run("F", threading.Event())[2] == 40
    assert len(rig.moves) == 2
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


@pytest.mark.parametrize("reuse_pose", [False, True])
def test_release_during_motion_connection_prevents_send(rig, reuse_pose):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement() if reuse_pose else None
    rig.connect_hook = lambda port: rig.session.release() if port == 30003 else None
    rig.session.run("W", threading.Event(), initial_pose=measurement)
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready


@pytest.mark.parametrize("reuse_pose", [False, True])
def test_release_during_ik_prevents_send(rig, reuse_pose):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement() if reuse_pose else None
    rig.hook = lambda text: rig.session.release() if text.startswith("InverseSolution(") else None
    rig.session.run("W", threading.Event(), initial_pose=measurement)
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
    rig.finish_moves = False
    released = threading.Event()

    monkeypatch.setattr("mg400_agent.keyboard_jog.HOLD_SECONDS", 0.045)
    rig.session.run("R", released)
    assert rig.session.expired
    assert not released.is_set()
    assert len(rig.moves) == 1
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("R", threading.Event())
    rig.session.release()
    monkeypatch.setattr("mg400_agent.keyboard_jog.HOLD_SECONDS", 5.0)
    rig.finish_moves = True
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
    assert rig.session.run("W", threading.Event()) == POSE
    assert "outside" in rig.session.limit_reason
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    assert rig.session.fault is None
    assert not rig.session.control.stopped


def test_cartesian_ik_crossing_invalid_joint_region_is_rejected(rig, monkeypatch):
    rig.sample = Feedback((0, -20, 2, 0), 5, 0, False)
    rig.session.prepare()
    original = rig.command

    def command(text):
        if text.startswith("InverseSolution("):
            return "0,-17,-2,0"
        return original(text)

    monkeypatch.setattr(rig, "command", command)
    assert rig.session.run("W", threading.Event()) == POSE
    assert "outside" in rig.session.limit_reason
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    assert rig.session.fault is None
    assert not rig.session.control.stopped


def test_ik_rejection_ends_hold_without_motion_or_stop(rig, monkeypatch):
    rig.session.prepare()
    solve = Mock(side_effect=TargetRejectedError("The controller rejected the target"))
    monkeypatch.setattr(JointControl, "solve_pose", solve)
    released = threading.Event()
    assert rig.session.run("W", released) == POSE
    assert solve.call_count == 5
    assert rig.session.limit_reason == "The controller rejected the target"
    assert not released.is_set()
    assert rig.session.fault is None
    assert rig.session.ready
    assert not rig.session.control.stopped
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("S", threading.Event())


@pytest.mark.parametrize("key,distances", [
    ("W", [50, 25, 12.5, 6.25, 5]),
    ("Q", [10, 5, 2.5, 1.25, 1]),
])
def test_rejected_targets_shrink_to_minimum_before_one_trajectory(rig, monkeypatch, key, distances):
    rig.session.prepare()
    rig.release_after = None
    solve = JointControl.solve_pose
    attempted = []

    def reject_larger(active, target):
        distance = (
            math.hypot(target[0] - POSE[0], target[1] - POSE[1])
            if key == "W" else abs(target[3] - POSE[3])
        )
        attempted.append(distance)
        assert not rig.moves
        if len(attempted) < len(distances):
            raise TargetRejectedError("Target is outside the workspace")
        return solve(active, target)

    monkeypatch.setattr(JointControl, "solve_pose", reject_larger)
    released = threading.Event()
    result = rig.session.run(key, released)
    assert attempted == pytest.approx(distances)
    assert result == rig.pose
    assert len(rig.moves) == 1
    assert rig.session.limit_reason == "Travel limit reached."
    assert not rig.session.control.stopped
    assert rig.session.fault is None
    assert "ResetRobot()" not in rig.commands
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run(key, threading.Event())
    rig.session.release()
    assert released.is_set()
    assert rig.session.ready


def test_base_target_shortens_to_one_degree_at_joint_limit(rig, monkeypatch):
    rig.sample = Feedback((157.8, *START[1:]), 5, 0, False)
    rig.pose = (*POSE[:3], 157.8)
    rig.release_after = None
    rig.session.prepare()
    attempted = []

    def validate(target):
        if target[0] != 157.8:
            attempted.append(target[0] - 157.8)
        return validate_joints(target)

    monkeypatch.setattr("mg400_agent.keyboard_jog.validate_joints", validate)
    rig.session.run("A", threading.Event())
    assert attempted == pytest.approx([10, 5, 2.5, 1.25, 1])
    assert len(rig.moves) == 1
    assert rig.sample.joints_deg[0] == pytest.approx(158.8)
    assert rig.session.limit_reason == "Travel limit reached."
    assert not rig.session.control.stopped


@pytest.mark.parametrize("reason", ["release", "expiry"])
def test_release_or_expiry_during_target_shortening_prevents_motion(rig, monkeypatch, reason):
    rig.session.prepare()
    attempts = []

    def reject(active, target):
        attempts.append(target)
        if len(attempts) == 2:
            if reason == "release":
                rig.session.release()
            else:
                rig.now += 5.1
        raise TargetRejectedError("Target rejected")

    monkeypatch.setattr(JointControl, "solve_pose", reject)
    rig.session.run("W", threading.Event())
    assert len(attempts) == 2
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert rig.session.expired is (reason == "expiry")
    assert rig.session.ready
    assert not rig.session.control.stopped


@pytest.mark.parametrize("failure", [
    RuntimeError("Unexpected controller response"),
    TimeoutError("Controller acknowledgement timed out"),
    ValueError("Invalid inverse solution data"),
])
def test_fault_during_target_shortening_does_not_try_another_target(rig, monkeypatch, failure):
    rig.session.prepare()
    solve = Mock(side_effect=[TargetRejectedError("Target rejected"), failure])
    monkeypatch.setattr(JointControl, "solve_pose", solve)
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    assert solve.call_count == 2
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert rig.session.fault is not None
    assert rig.session.control.stopped
    assert not rig.session.ready


def test_joint_target_limit_ends_hold_without_stopping_parent(rig):
    rig.sample = Feedback((158.5, *START[1:]), 5, 0, False)
    rig.session.prepare()
    rig.session.run("A", threading.Event())
    assert "J1" in rig.session.limit_reason
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    assert rig.session.fault is None
    assert not rig.session.control.stopped


@pytest.mark.parametrize("stop_fails", [False, True])
def test_release_during_final_pose_read_waits_for_stop_confirmation(
    rig, monkeypatch, stop_fails
):
    rig.session.prepare()
    monkeypatch.setattr(
        JointControl, "solve_pose", Mock(side_effect=TargetRejectedError("Target rejected"))
    )
    reset_received, allow_ack = threading.Event(), threading.Event()

    def release(text):
        if text == "GetPose(0,0)" and rig.session.limit_reason:
            rig.session.release()

    def block_reply(text):
        if text == "ResetRobot()":
            reset_received.set()
            assert allow_ack.wait(2)
            if stop_fails:
                raise ConnectionError("Lost stop acknowledgement")

    rig.hook, rig.reply_hook = release, block_reply
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert reset_received.wait(1)
    try:
        assert thread.is_alive()
        assert rig.session.busy
        assert rig.session.control.busy
        assert not rig.session.ready
    finally:
        allow_ack.set()
    result = finish_thread(thread, outcome)
    assert isinstance(result, RuntimeError) is stop_fails
    assert rig.session.control.stopped is stop_fails
    assert rig.session.ready is not stop_fails
    assert not rig.moves


@pytest.mark.parametrize("reason", ["release", "expiry", "disable", "stale", "stop_failure"])
def test_target_rejection_does_not_bypass_concurrent_stop_or_fault(rig, monkeypatch, reason):
    rig.session.prepare()

    def reject(active, target):
        if reason == "release":
            rig.session.release()
        elif reason == "expiry":
            rig.now += 5.1
        elif reason == "disable":
            rig.session.control.cancel(disabling=True)
        elif reason == "stale":
            rig.stale = True
        else:
            rig.reset_failure = True
            rig.session.release()
        raise TargetRejectedError("Target rejected")

    monkeypatch.setattr(JointControl, "solve_pose", reject)
    if reason in ("disable", "stale", "stop_failure"):
        with pytest.raises(RuntimeError):
            rig.session.run("W", threading.Event())
        assert rig.session.control.stopped
        assert not rig.session.ready
    else:
        rig.session.run("W", threading.Event())
        assert not rig.session.control.stopped
        assert rig.session.ready
    assert rig.session.limit_reason is None
    assert rig.session.expired is (reason == "expiry")
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1


def test_invalid_measured_joints_remain_a_fault(rig):
    rig.session.prepare()
    rig.sample = Feedback((200, *START[1:]), 5, 0, False)
    with pytest.raises(RuntimeError, match="outside"):
        rig.session.run("D", threading.Event())
    assert rig.session.control.stopped
    assert rig.session.limit_reason is None
    assert not rig.moves


@pytest.mark.parametrize("payload", ["1,2,3", "nan,0,10,0"])
def test_invalid_ik_reply_remains_a_fault(rig, monkeypatch, payload):
    rig.session.prepare()
    original = rig.command

    def command(text):
        if text.startswith("InverseSolution("):
            return payload
        return original(text)

    monkeypatch.setattr(rig, "command", command)
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    assert rig.session.control.stopped
    assert rig.session.fault is not None
    assert rig.session.limit_reason is None
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


@pytest.mark.parametrize("reuse_pose", [False, True])
def test_parent_cancel_before_send_prevents_child_motion(rig, reuse_pose):
    rig.session.prepare()
    measurement = rig.session.control.read_pose_measurement() if reuse_pose else None
    rig.connect_hook = (
        lambda port: rig.session.control.cancel() if port == 30003 else None
    )
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event(), initial_pose=measurement)
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
