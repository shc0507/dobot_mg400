import math
import socket
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl, PreparationRejectedError, TargetRejectedError
from mg400_agent.joint_limits import validate_joints
from mg400_agent.keyboard_jog import KeyboardJogSession

START = (30.0, 0.0, 10.0, 0.0)
POSE = (250.0, 10.0, 40.0, 30.0)


@pytest.fixture
def rig(monkeypatch):
    # Exercise the retained finite-trajectory fallback. Continuous jogging has
    # separate transport, cancellation, and feedback tests.
    monkeypatch.setattr("mg400_agent.keyboard_jog.MOVEJOG_COMMANDS", {})

    class Rig:
        def __init__(self):
            self.now = 10.0
            self.sample = Feedback(START, 5, 0, False)
            self.pose = POSE
            self.commands = []
            self.moves = []
            self.connections = []
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
            active = self.session._active if self.session else None
            pending = active._pending if active else None
            if (
                self.release_after
                and len(self.moves) >= self.release_after
                and self.moves[-1].startswith("JointMovJ(")
                and pending is not None
                and pending["done"].is_set()
                and not self.session._revoked.is_set()
            ):
                self.session.release()
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
            if text.startswith("SpeedFactor("):
                self.sample = replace(self.sample, speed_scaling_raw=float(text[12:-1]))
                return ""
            if text == "GetPose(0,0)":
                if self.release_after and len(self.moves) >= self.release_after:
                    self.session.release()
                return ",".join(map(str, self.pose))
            if text.startswith("InverseSolution("):
                x, y, z, r = map(float, text.split("(")[1].split(",")[:4])
                self.solution = (
                    START[0],
                    (x - POSE[0]) / 10,
                    10 + (z - POSE[2]) / 10,
                    r - START[0],
                )
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
                    self.sample = replace(self.sample, joints_deg=joints, mode=5)
                else:
                    self.sample = replace(self.sample, mode=7)
                return ""
            if text == "ResetRobot()":
                if self.reset_failure:
                    raise ConnectionError("Lost stop acknowledgement")
                self.sample = replace(self.sample, mode=5)
                return ""
            raise AssertionError(f"Unexpected command: {text}")

    robot = Rig()

    class Connection:
        def __init__(self):
            self.shutdown_event = threading.Event()

        def shutdown(self, how):
            self.shutdown_event.set()

        def close(self):
            self.shutdown_event.set()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def settimeout(self, value):
            pass

        def setblocking(self, value):
            pass

        def connect_ex(self, address):
            robot.connect_hook(address[1])
            return 0

        def sendall(self, data):
            text = data.decode()
            self.text = text
            value = robot.command(text)
            self.reply = f"0,{{{value}}},{text};".encode()

        def recv(self, size):
            robot.reply_hook(self.text)
            if self.shutdown_event.is_set():
                raise ConnectionError("Connection was shut down")
            return self.reply

    def connect(address, timeout):
        robot.connect_hook(address[1])
        connection = Connection()
        robot.connections.append(connection)
        return connection

    def make_socket(*args):
        connection = Connection()
        robot.connections.append(connection)
        return connection

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(socket, "socket", make_socket)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("robot.test", 29999)),
        ],
    )
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


def test_prepare_sets_low_speed_without_motion_and_shares_controller_locks(rig):
    assert not rig.session.ready
    assert not rig.commands
    assert rig.session.prepare() == POSE
    assert rig.session.ready
    assert rig.commands == [
        "RobotMode()",
        "GetErrorID()",
        "GetAngle()",
        "GetPose(0,0)",
        "SpeedFactor(1)",
    ]
    rig.session.release()
    rig.session.release()
    assert rig.session.ready
    assert len(rig.commands) == 5


@pytest.mark.parametrize("key", ["W", "S"])
@pytest.mark.parametrize("speed", [25, 100])
def test_finite_radial_motion_uses_the_confirmed_selected_speed(rig, key, speed):
    rig.session = KeyboardJogSession(rig.session.control, speed_percent=speed)
    rig.session.prepare()
    rig.session.run(key, threading.Event())
    assert f"SpeedFactor({speed})" in rig.commands
    assert len(rig.moves) == 1
    assert rig.moves[0].endswith("User=0,Tool=0,SpeedL=2,AccL=20)")
    assert rig.sample.speed_scaling_raw == speed
    assert rig.session.ready and not rig.session.control.stopped


@pytest.mark.parametrize("when", ["before_run", "before_send"])
def test_finite_speed_mismatch_prevents_movement(rig, when):
    rig.session = KeyboardJogSession(rig.session.control, speed_percent=25)
    rig.session.prepare()

    def mismatch():
        rig.sample = replace(rig.sample, speed_scaling_raw=1)

    if when == "before_run":
        mismatch()
    else:
        rig.connect_hook = lambda port: mismatch() if port == 30003 else None
    with pytest.raises(RuntimeError, match="speed changed"):
        rig.session.run("W", threading.Event())
    assert not rig.moves
    assert rig.session.control.stopped and not rig.session.ready


def test_finite_speed_change_during_movement_stops_and_revokes_readiness(rig, monkeypatch):
    rig.session = KeyboardJogSession(rig.session.control, speed_percent=25)
    rig.session.prepare()
    rig.release_after = None
    rig.finish_moves = False
    sleep = rig.sleep

    def change_speed(duration):
        if rig.moves:
            rig.sample = replace(rig.sample, speed_scaling_raw=1)
        sleep(duration)

    monkeypatch.setattr("mg400_agent.keyboard_jog.time.sleep", change_speed)
    with pytest.raises(RuntimeError, match="speed changed"):
        rig.session.run("W", threading.Event())
    assert rig.moves and "ResetRobot()" in rig.commands
    assert rig.session.control.stopped and not rig.session.ready


@pytest.mark.parametrize(
    "key,axis,change",
    [
        ("W", 0, 50 * math.cos(math.radians(30))),
        ("S", 0, -50 * math.cos(math.radians(30))),
        ("R", 2, 50),
        ("F", 2, -50),
        ("Q", 3, 10),
        ("E", 3, -10),
    ],
)
def test_cartesian_keys_first_segment_uses_measured_pose(rig, key, axis, change):
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


def test_repeated_hold_stops_do_not_replace_a_later_parent_stop(rig):
    rig.session.prepare()
    for count, key in enumerate(("R", "F"), 1):
        rig.release_after = count
        rig.session.run(key, threading.Event())
        assert len(rig.moves) == count
        assert rig.commands.count("ResetRobot()") == count
        assert rig.session.ready
        assert not rig.session.busy and not rig.session.control.busy
        assert not rig.session.control.stopped

    result = rig.session.control.stop()
    assert result["ok"] and result["stationary_verified"]
    assert rig.commands.count("ResetRobot()") == 3
    assert rig.session.control.stopped
    assert not rig.session.ready
    assert rig.session.control.stop() == result
    assert rig.commands.count("ResetRobot()") == 3


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


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "expired",
        "future",
        "control",
        "before_mode",
        "after_mode",
        "before_alarm",
        "after_alarm",
        "query_changed",
        "current_changed",
        "combined_drift",
    ],
)
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
        measurement = replace(
            measurement,
            **{field: replace(getattr(measurement, field), mode=4 if field == "before" else 7)},
        )
    elif invalid.endswith("alarm"):
        field = invalid.split("_")[0]
        measurement = replace(
            measurement, **{field: replace(getattr(measurement, field), error_status=True)}
        )
    elif invalid == "query_changed":
        measurement = replace(
            measurement,
            before=replace(measurement.before, joints_deg=(START[0] - 0.03, *START[1:])),
        )
    elif invalid == "current_changed":
        rig.sample = replace(rig.sample, joints_deg=(START[0] + 0.03, *START[1:]))
    else:
        measurement = replace(
            measurement,
            before=replace(measurement.before, joints_deg=(START[0] - 0.02, *START[1:])),
        )
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
def test_base_keys_first_segment_is_ten_degrees(rig, key, change):
    rig.session.prepare()
    rig.session.run(key, threading.Event())
    assert rig.sample.joints_deg == (START[0] + change, *START[1:])
    assert rig.moves == [
        f"JointMovJ({START[0] + change:.6f},0.000000,10.000000,0.000000,SpeedJ=5,AccJ=20)"
    ]
    assert not any(command.startswith("InverseSolution(") for command in rig.commands)


@pytest.mark.parametrize("key", ["R", "Q", "A"])
def test_long_hold_continues_beyond_five_seconds_and_one_segment(rig, monkeypatch, key):
    rig.session.prepare()
    prepared_at = rig.now
    rig.release_after = 3
    original_step = rig.session._step
    stable = Mock(wraps=JointControl._wait_stable)
    monkeypatch.setattr(JointControl, "_wait_stable", lambda self, *a, **kw: stable(self, *a, **kw))

    def step(*args):
        result = original_step(*args)
        rig.now += 3.0
        return result

    monkeypatch.setattr(rig.session, "_step", step)
    released = threading.Event()
    rig.session.run(key, released)
    assert rig.now - prepared_at > 5
    assert len(rig.moves) == 3
    if key == "R":
        assert rig.pose[2] == POSE[2] + 150
    elif key == "Q":
        assert rig.pose[3] == POSE[3] + 30
    else:
        assert rig.sample.joints_deg[0] == START[0] + 30
    assert stable.call_count == 1  # Stop verification only; no per-segment preflight.
    assert rig.commands.count("GetErrorID()") == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert released.is_set()
    assert rig.session.ready and not rig.session.control.stopped


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
    preparation_pending = rig.session.control._pending
    original = JointControl._guarded_command

    def guarded(active, *args, **kwargs):
        rig.session.release()
        return original(active, *args, **kwargs)

    monkeypatch.setattr(JointControl, "_guarded_command", guarded)
    rig.session.run("W", threading.Event())
    assert not rig.moves
    assert rig.session.control._pending is preparation_pending
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
    assert not rig.session.busy and not rig.session.control.busy
    commands = rig.commands.copy()
    with pytest.raises(RuntimeError):
        rig.session.run("W", threading.Event())
    with pytest.raises(RuntimeError, match="stopped"):
        rig.session.control.move(START)
    assert rig.commands == commands
    assert not rig.session.control.busy


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


def test_close_during_motion_stops_and_prevents_any_later_hold(rig):
    rig.session.prepare()
    rig.release_after = None
    rig.finish_moves = False

    def close(text):
        if text == "GetPose(0,0)" and rig.moves:
            rig.session.close()

    rig.hook = close
    rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert not rig.session.ready
    assert not rig.session.control.stopped
    with pytest.raises(RuntimeError, match="closed"):
        rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1


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
    rig.sample = Feedback((0, -22, 2, 0), 5, 0, False)
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
    assert solve.call_count == 10
    assert rig.session.limit_reason == "The controller rejected the target"
    assert not released.is_set()
    assert rig.session.fault is None
    assert rig.session.ready
    assert not rig.session.control.stopped
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("S", threading.Event())


@pytest.mark.parametrize(
    "key,distances",
    [
        ("W", [50, 25, 12.5, 6.25, 3.125, 1.5625, 0.78125, 0.390625, 0.1953125, 0.1]),
        ("Q", [10, 5, 2.5, 1.25, 0.625, 0.3125, 0.15625, 0.1]),
    ],
)
def test_rejected_targets_shrink_to_minimum_before_one_trajectory(rig, monkeypatch, key, distances):
    rig.session.prepare()
    solve = JointControl.solve_pose
    attempted = []

    def reject_larger(active, target):
        distance = (
            math.hypot(target[0] - POSE[0], target[1] - POSE[1])
            if key == "W"
            else abs(target[3] - POSE[3])
        )
        attempted.append(distance)
        assert not rig.moves
        if len(attempted) < len(distances):
            raise TargetRejectedError("Target is outside the workspace")
        return solve(active, target)

    monkeypatch.setattr(JointControl, "solve_pose", reject_larger)
    released = threading.Event()
    assert rig.session.run(key, released) == rig.pose
    assert attempted == pytest.approx(distances)
    assert len(rig.moves) == 1
    assert rig.session.limit_reason is None
    assert not rig.session.control.stopped and rig.session.fault is None
    assert rig.commands.count("ResetRobot()") == 1
    assert released.is_set() and rig.session.ready


def test_base_target_shortens_below_one_degree_at_joint_limit(rig, monkeypatch):
    rig.sample = Feedback((158.5, *START[1:]), 5, 0, False)
    rig.pose = (*POSE[:3], 158.5)
    rig.session.prepare()
    attempted = []

    def validate(target):
        if target[0] != 158.5:
            attempted.append(target[0] - 158.5)
        return validate_joints(target)

    monkeypatch.setattr("mg400_agent.keyboard_jog.validate_joints", validate)
    rig.session.run("A", threading.Event())
    assert attempted == pytest.approx([10, 5, 2.5, 1.25, 0.625, 0.3125])
    assert len(rig.moves) == 1
    assert rig.sample.joints_deg[0] == pytest.approx(158.8125)
    assert rig.session.limit_reason is None
    assert not rig.session.control.stopped


@pytest.mark.parametrize("reason", ["release", "close"])
def test_release_or_close_during_target_shortening_prevents_motion(rig, monkeypatch, reason):
    rig.session.prepare()
    attempts = []

    def reject(active, target):
        attempts.append(target)
        if len(attempts) == 2:
            if reason == "release":
                rig.session.release()
            else:
                rig.session.close()
        raise TargetRejectedError("Target rejected")

    monkeypatch.setattr(JointControl, "solve_pose", reject)
    rig.session.run("W", threading.Event())
    assert len(attempts) == 2
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert rig.session.ready is (reason == "release")
    assert not rig.session.control.stopped


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("Unexpected controller response"),
        TimeoutError("Controller acknowledgement timed out"),
        ValueError("Invalid inverse solution data"),
    ],
)
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
    rig.sample = Feedback((158.95, *START[1:]), 5, 0, False)
    rig.session.prepare()
    rig.session.run("A", threading.Event())
    assert "J1" in rig.session.limit_reason
    assert not rig.moves
    assert "ResetRobot()" not in rig.commands
    assert rig.session.fault is None
    assert not rig.session.control.stopped


@pytest.mark.parametrize("stop_fails", [False, True])
def test_release_during_final_pose_read_waits_for_stop_confirmation(rig, monkeypatch, stop_fails):
    rig.session.prepare()
    monkeypatch.setattr(
        JointControl, "solve_pose", Mock(side_effect=TargetRejectedError("Target rejected"))
    )
    reset_received, allow_ack = threading.Event(), threading.Event()

    stopped_pose = (*POSE[:3], POSE[3] + 0.02)

    def release(text):
        if text == "GetPose(0,0)" and rig.session.limit_reason:
            rig.session.release()
        elif text == "ResetRobot()":
            rig.pose = stopped_pose

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
    if not stop_fails:
        assert result == stopped_pose
        assert rig.session.measured_pose[0] == stopped_pose
        reset = rig.commands.index("ResetRobot()")
        assert "GetPose(0,0)" in rig.commands[reset + 1 :]
    assert not rig.moves


@pytest.mark.parametrize("reason", ["release", "close", "disable", "stale", "stop_failure"])
def test_target_rejection_does_not_bypass_concurrent_stop_or_fault(rig, monkeypatch, reason):
    rig.session.prepare()

    def reject(active, target):
        if reason == "release":
            rig.session.release()
        elif reason == "close":
            rig.session.close()
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
        assert rig.session.ready is (reason != "close")
    assert rig.session.limit_reason is None
    assert not rig.moves
    assert rig.commands.count("ResetRobot()") == 1


def test_invalid_measured_joints_remain_a_fault(rig):
    rig.session.prepare()
    rig.sample = replace(rig.sample, joints_deg=(200, *START[1:]))
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


@pytest.mark.parametrize("completed_holds", [0, 1])
def test_no_second_hold_or_absolute_move_can_queue_while_hold_is_active(rig, completed_holds):
    rig.session.prepare()
    if completed_holds:
        rig.session.run("R", threading.Event())
        rig.release_after = None
    previous_moves = rig.moves.copy()
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
    assert rig.moves == previous_moves
    assert rig.commands.count("ResetRobot()") == completed_holds + 1
    assert rig.session.ready
    assert not rig.session.busy and not rig.session.control.busy


def test_close_cancels_hold_during_blocking_query(rig):
    rig.session.prepare()
    reached, unblock = threading.Event(), threading.Event()

    def block(text):
        if text.startswith("InverseSolution("):
            reached.set()
            assert unblock.wait(2)

    rig.hook = block
    thread, outcome = start_thread(lambda: rig.session.run("W", threading.Event()))
    assert reached.wait(1)
    rig.session.close()
    assert rig.session._revoked.wait(1)
    unblock.set()
    assert not isinstance(finish_thread(thread, outcome), Exception)
    assert not rig.moves
    assert not rig.session.ready


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
    rig.connect_hook = lambda port: rig.session.control.cancel() if port == 30003 else None
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


@pytest.mark.parametrize("j4", [-220.0, 220.0])
def test_prepare_accepts_j4_within_manufacturer_target_range_without_motion(rig, j4):
    rig.sample = replace(rig.sample, joints_deg=(*START[:3], j4))
    rig.pose = (*POSE[:3], START[0] + j4)
    assert rig.session.prepare() == rig.pose
    assert rig.session.ready
    assert not rig.moves
    assert rig.commands == [
        "RobotMode()",
        "GetErrorID()",
        "GetAngle()",
        "GetPose(0,0)",
        "SpeedFactor(1)",
    ]


@pytest.mark.parametrize("when", ["before", "preflight"])
def test_prepare_rejects_j4_outside_measured_range_without_keyboard_readiness(rig, when):
    outside = (*START[:3], -361.0)
    if when == "before":
        rig.sample = replace(rig.sample, joints_deg=outside)
    else:
        # Initial feedback is inside the range. Both preflight sources then change.
        def hook(text):
            if text == "RobotMode()":
                rig.sample = replace(rig.sample, joints_deg=outside)

        rig.hook = hook
    with pytest.raises(ValueError, match="J4"):
        rig.session.prepare()
    assert not rig.session.ready
    assert not rig.moves
    assert "GetPose(0,0)" not in rig.commands
    assert not any(text.startswith(("Enable", "ClearError")) for text in rig.commands)


@pytest.mark.parametrize("query", ["GetAngle()", "GetPose(0,0)"])
@pytest.mark.parametrize("failure_type", [TimeoutError, ConnectionError])
def test_prepare_read_query_failure_can_retry_without_parent_stop(rig, query, failure_type):
    def fail(text):
        if text == query:
            raise failure_type("Read query failed")

    rig.hook = fail
    with pytest.raises(PreparationRejectedError):
        rig.session.prepare()
    assert not rig.session.ready and not rig.session.control.stopped
    assert rig.session.fault is None and rig.session.control._pending is None
    assert not rig.moves and "ResetRobot()" not in rig.commands
    rig.hook = lambda text: None
    assert rig.session.prepare() == POSE
    assert rig.session.ready


@pytest.mark.parametrize("query", ["GetPose(", "InverseSolution("])
def test_hold_query_failure_requires_release_then_allows_retry(rig, query):
    rig.session.prepare()
    rig.commands.clear()
    released = threading.Event()

    def fail(text):
        if text.startswith(query):
            raise TimeoutError("Read query failed")

    rig.hook = fail
    assert rig.session.run("W", released) == POSE
    assert "Retry the action" in rig.session.limit_reason
    assert not rig.moves and "ResetRobot()" not in rig.commands
    assert rig.session.ready and rig.session.fault is None
    assert not rig.session.control.stopped
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("W", threading.Event())
    rig.hook = lambda text: None
    rig.session.release()
    rig.session.run("W", threading.Event())
    assert len(rig.moves) == 1 and rig.session.ready


@pytest.mark.parametrize(
    "axis,start,key,end",
    [
        (0, 159, "D", 149),
        (3, 359, "E", 349),
        (3, -359, "Q", -349),
    ],
)
def test_keyboard_measured_margin_band_allows_inward_move(rig, axis, start, key, end):
    joints = list(START)
    joints[axis] = start
    rig.sample = Feedback(tuple(joints), 5, 0, False)
    rig.pose = (*POSE[:3], joints[0] + joints[3])
    rig.session.prepare()
    rig.session.run(key, threading.Event())
    assert rig.sample.joints_deg[axis] == pytest.approx(end)
    assert len(rig.moves) == 1 and rig.session.ready
    assert not rig.session.control.stopped


@pytest.mark.parametrize("key", ["A", "W"])
@pytest.mark.parametrize("arrival", ["target", "offset", "stationary", "reverse", "perpendicular"])
def test_minimum_step_requires_actual_progress_before_continuation(rig, monkeypatch, key, arrival):
    from mg400_agent.joint_limits import MARGIN_DEG

    if key == "A":
        rig.sample = Feedback((160 - MARGIN_DEG - 0.1, *START[1:]), 5, 0, False)
        rig.pose = (*POSE[:3], rig.sample.joints_deg[0])
    else:
        original_solve = JointControl.solve_pose

        def solve(control, target):
            if math.dist(target[:3], POSE[:3]) > 0.100001:
                raise TargetRejectedError("Only the minimum target is reachable")
            return original_solve(control, target)

        monkeypatch.setattr(JointControl, "solve_pose", solve)
    rig.release_after = None
    rig.session.prepare()
    start_joints, start_pose = rig.sample.joints_deg, rig.pose
    original_command, original_snapshot = rig.command, rig.snapshot
    original_step = rig.session._step
    observations = 0
    target_state = None
    arrived = []

    def update():
        nonlocal observations
        active = rig.session._active
        if target_state is None or active.stopped or not active._pending["done"].is_set():
            return
        observations += 1
        if observations == 4:
            if arrival in {"target", "offset"}:
                rig.sample, rig.pose = target_state
                if arrival == "offset":
                    if key == "A":
                        rig.sample = replace(
                            rig.sample,
                            joints_deg=(
                                rig.sample.joints_deg[0] + 0.06,
                                *rig.sample.joints_deg[1:],
                            ),
                        )
                        rig.pose = (*rig.pose[:3], rig.pose[3] + 0.06)
                    else:
                        angle = math.radians(start_joints[0])
                        rig.pose = (
                            rig.pose[0] + 0.06 * math.cos(angle),
                            rig.pose[1] + 0.06 * math.sin(angle),
                            *rig.pose[2:],
                        )
            elif arrival == "reverse":
                if key == "A":
                    rig.sample = replace(
                        rig.sample,
                        joints_deg=(
                            start_joints[0] - 0.01,
                            *start_joints[1:],
                        ),
                    )
                else:
                    angle = math.radians(start_joints[0])
                    rig.pose = (
                        start_pose[0] - 0.01 * math.cos(angle),
                        start_pose[1] - 0.01 * math.sin(angle),
                        *start_pose[2:],
                    )
            elif arrival == "perpendicular":
                if key == "A":
                    rig.sample = replace(
                        rig.sample,
                        joints_deg=(
                            start_joints[0],
                            start_joints[1] + 0.06,
                            *start_joints[2:],
                        ),
                    )
                else:
                    rig.pose = (*start_pose[:2], start_pose[2] + 0.06, start_pose[3])
        if observations == 12:
            rig.session.release()

    def snapshot():
        if key == "A":
            update()
        return original_snapshot()

    def command(text):
        nonlocal target_state
        if key == "W" and text == "GetPose(0,0)":
            update()
        result = original_command(text)
        if text.startswith(("JointMovJ(", "MovL(")):
            target_state = rig.sample, rig.pose
            # ACK may precede actual movement; new idle feedback still has the start.
            rig.sample = Feedback(start_joints, 5, 0, False)
            rig.pose = start_pose
        return result

    def step(*args):
        result = original_step(*args)
        arrived.append(True)
        rig.session.release()
        return result

    rig.command = command
    monkeypatch.setattr(rig.session.control, "feedback_snapshot", snapshot)
    monkeypatch.setattr(rig.session, "_step", step)
    rig.session.run(key, threading.Event())
    assert len(rig.moves) == 1
    sent_target = tuple(map(float, rig.moves[0].split("(")[1].split(",")[:4]))
    sent_distance = (
        sent_target[0] - start_joints[0]
        if key == "A"
        else math.dist(sent_target[:3], start_pose[:3])
    )
    assert sent_distance == pytest.approx(0.1, abs=1e-6)
    assert observations >= 4
    assert bool(arrived) is (arrival in {"target", "offset"})
    assert rig.session.ready and not rig.session.control.stopped
    assert rig.commands.count("ResetRobot()") == 1


@pytest.mark.parametrize("key", ["A", "D"])
@pytest.mark.parametrize("query_fails", [False, True])
def test_base_motion_starts_while_background_pose_query_is_blocked(rig, key, query_fails):
    rig.session.prepare()
    rig.commands.clear()
    pose_waiting, allow_pose, motion_sent, allow_motion_ack = (threading.Event() for _ in range(4))
    blocked = False

    def reply(text):
        nonlocal blocked
        if text == "GetPose(0,0)" and not blocked:
            blocked = True
            pose_waiting.set()
            assert allow_pose.wait(2)
            if query_fails:
                raise TimeoutError("Background display query failed")
        elif text.startswith("JointMovJ("):
            assert allow_motion_ack.wait(2)

    rig.reply_hook = reply
    rig.hook = lambda text: motion_sent.set() if text.startswith("JointMovJ(") else None
    query_thread, query_outcome = start_thread(rig.session.control.read_pose_measurement)
    assert pose_waiting.wait(1)
    move_thread, move_outcome = start_thread(lambda: rig.session.run(key, threading.Event()))
    try:
        assert motion_sent.wait(1)
        assert not allow_pose.is_set()
        allow_pose.set()
        query_result = finish_thread(query_thread, query_outcome)
        assert isinstance(query_result, TimeoutError) is query_fails
    finally:
        allow_pose.set()
        allow_motion_ack.set()
    assert not isinstance(finish_thread(move_thread, move_outcome), Exception)
    assert len(rig.moves) == 1
    assert rig.commands.count("GetPose(0,0)") == 2  # Background query and final display refresh.
    first_motion = rig.commands.index(rig.moves[0])
    reset = rig.commands.index("ResetRobot()")
    assert "GetPose(0,0)" not in rig.commands[first_motion:reset]
    assert rig.session.ready and not rig.session.control.stopped


@pytest.mark.parametrize("key", ["A", "R"])
def test_release_between_segments_never_registers_or_sends_next_move(rig, monkeypatch, key):
    rig.session.prepare()
    rig.release_after = None
    original_step = rig.session._step
    completed = []

    def step(active, *args):
        result = original_step(active, *args)
        completed.append(active._pending)
        assert active._pending["done"].is_set() and active._pending["ok"]
        rig.session.release()
        return result

    monkeypatch.setattr(rig.session, "_step", step)
    rig.session.run(key, threading.Event())
    assert len(rig.moves) == len(completed) == 1
    assert rig.session.control._pending is completed[0]
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.ready and not rig.session.control.stopped


def test_next_segment_is_rejected_when_no_target_remains_inside_joint_limit(rig):
    rig.sample = Feedback((148.9, *START[1:]), 5, 0, False)
    rig.pose = (*POSE[:3], 148.9)
    rig.release_after = None
    rig.session.prepare()
    released = threading.Event()
    rig.session.run("A", released)
    assert len(rig.moves) == 1
    assert rig.sample.joints_deg[0] == pytest.approx(158.9)
    assert "J1" in rig.session.limit_reason
    assert "ResetRobot()" not in rig.commands
    assert rig.session.ready and not rig.session.control.stopped
    assert not released.is_set()
    with pytest.raises(RuntimeError, match="Release the current key"):
        rig.session.run("D", threading.Event())


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("IK query timed out"),
        ValueError("Invalid IK reply"),
    ],
)
def test_next_segment_ik_failure_stops_and_latches_after_a_successful_move(rig, failure):
    rig.session.prepare()
    rig.release_after = None

    def fail(text):
        if text.startswith("InverseSolution(") and rig.moves:
            raise failure

    rig.hook = fail
    with pytest.raises(RuntimeError, match=str(failure)):
        rig.session.run("R", threading.Event())
    assert len(rig.moves) == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert rig.session.control.stopped and not rig.session.ready
    assert not rig.session.control._stop_uncertain


@pytest.mark.parametrize("expired", [False, True])
def test_next_segment_reuses_only_fresh_measured_arrival_pose(rig, monkeypatch, expired):
    rig.session.prepare()
    rig.release_after = 2
    original_step = rig.session._step
    completed = 0

    def reply(text):
        if text.startswith("MovL(") and len(rig.moves) == 1:
            # Actual arrival differs from the command, but remains inside tolerance.
            rig.pose = (*rig.pose[:2], rig.pose[2] + 0.06, rig.pose[3])

    def step(*args):
        nonlocal completed
        result = original_step(*args)
        completed += 1
        if completed == 1 and expired:
            rig.now += 0.3
        return result

    rig.reply_hook = reply
    monkeypatch.setattr(rig.session, "_step", step)
    rig.session.run("R", threading.Event())
    assert len(rig.moves) == 2
    first, second = (rig.commands.index(move) for move in rig.moves)
    between = rig.commands[first + 1 : second]
    assert between.count("GetPose(0,0)") == (2 if expired else 1)
    assert float(rig.moves[1].split(",")[2]) == pytest.approx(140.06)
    assert rig.session.ready and not rig.session.control.stopped


@pytest.mark.parametrize("key", ["A", "R"])
@pytest.mark.parametrize("problem", ["stale", "alarm", "conditional_limit"])
def test_later_segment_feedback_fault_prevents_any_further_segment(rig, key, problem):
    rig.session.prepare()
    rig.release_after = None

    def fault(text):
        if text.startswith(("JointMovJ(", "MovL(")) and len(rig.moves) == 2:
            if problem == "stale":
                rig.stale = True
            elif problem == "alarm":
                rig.sample = replace(rig.sample, error_status=True)
            else:
                # J2=-20 is only valid when J3>0; the measured branch is now invalid.
                rig.sample = replace(rig.sample, joints_deg=(rig.sample.joints_deg[0], -20, 0, 0))

    rig.reply_hook = fault
    with pytest.raises(RuntimeError):
        rig.session.run(key, threading.Event())
    assert len(rig.moves) == 2
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.limit_reason is None
    assert rig.session.control.stopped and not rig.session.ready


def test_second_segment_failed_ack_cannot_be_hidden_by_first_segment_success(rig):
    rig.session.prepare()
    rig.release_after = None

    def fail(text):
        if text.startswith("MovL(") and len(rig.moves) == 2:
            rig.session.release()
            raise ConnectionError("Second movement ACK lost")

    rig.reply_hook = fail
    with pytest.raises(RuntimeError, match="stop unconfirmed"):
        rig.session.run("R", threading.Event())
    assert len(rig.moves) == 2
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.session.control._stop_uncertain
    assert rig.session.control.stopped and not rig.session.ready


def test_base_release_stop_is_not_blocked_by_background_pose_query(rig):
    rig.session.prepare()
    rig.commands.clear()
    rig.release_after = None
    rig.finish_moves = False
    pose_waiting, allow_pose, motion_sent, reset_sent = (threading.Event() for _ in range(4))
    blocked = False

    def reply(text):
        nonlocal blocked
        if text == "GetPose(0,0)" and not blocked:
            blocked = True
            query_connection = next(
                connection
                for connection in rig.connections
                if getattr(connection, "text", None) == "GetPose(0,0)"
                and not connection.shutdown_event.is_set()
            )
            pose_waiting.set()
            while not allow_pose.is_set():
                if query_connection.shutdown_event.wait(0.01):
                    raise ConnectionError("Background query was interrupted")

    def sent(text):
        if text.startswith("JointMovJ("):
            motion_sent.set()
        elif text == "ResetRobot()":
            reset_sent.set()

    rig.reply_hook, rig.hook = reply, sent
    query_thread, query_outcome = start_thread(rig.session.control.read_pose_measurement)
    assert pose_waiting.wait(1)
    move_thread, move_outcome = start_thread(lambda: rig.session.run("A", threading.Event()))
    try:
        assert motion_sent.wait(1)
        rig.session.release()
        # A display query must not hold the stop request behind its response.
        assert reset_sent.wait(1), "Background GetPose prevented ResetRobot from being sent"
        assert not allow_pose.is_set()
    finally:
        allow_pose.set()
        query_thread.join(2)
        move_thread.join(2)
    assert not query_thread.is_alive() and len(query_outcome) == 1
    assert not isinstance(finish_thread(move_thread, move_outcome), Exception)
    assert len(rig.moves) == 1
    assert rig.commands.count("ResetRobot()") == 1
    assert rig.sample.mode == 5
    assert rig.session._stop_result["ok"]
    assert rig.session.ready and not rig.session.control.stopped
