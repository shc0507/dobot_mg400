import errno
import socket
import threading
import time
from dataclasses import replace
from unittest.mock import Mock, call

import pytest
from helpers import block_network, fake_connection

from mg400.controller_protocol import ControllerCommandError, parse_controller_response
from mg400.feedback import Feedback
from mg400.joint_control import (
    DashboardQueryCancelledError,
    JointControl,
    PreparationRejectedError,
    TargetRejectedError,
)
from mg400.keyboard_jog import KeyboardJogSession

START = (0.7, 0.04, 0.52, 23.08)
TARGET = (*START[:3], START[3] + 2)
MOVE = "JointMovJ(0.700000,0.040000,0.520000,25.080000,SpeedJ=5,AccJ=5)"
POSE = (250.0, 10.0, 40.0, 23.78)
LINEAR_TARGET = (251.0, 10.0, 40.0, 23.78)
LINEAR_MOVE = "MovL(251.000000,10.000000,40.000000,23.780000,User=0,Tool=0,SpeedL=1,AccL=1)"
REAL_SOCKET = socket.socket
REAL_CREATE_CONNECTION = socket.create_connection
REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    block_network(monkeypatch, message="Network prohibited")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("robot.test", 29999)),
            ]
        ),
    )


@pytest.fixture
def rig(monkeypatch):
    now = [10.0]
    sample = [Feedback(START, 5, 0, False)]
    monkeypatch.setattr("mg400.joint_control.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "mg400.joint_control.time.sleep",
        lambda duration: now.__setitem__(0, now[0] + duration),
    )
    control = JointControl("robot.test", lambda: (sample[0], now[0]))
    commands = []

    def command(text, port=29999, *, pending=None, stopping=False):
        commands.append((text, port))
        if pending is not None:
            pending["sent"] = True
        if text == "RobotMode()":
            return str(sample[0].mode)
        if text == "GetErrorID()":
            return "[[],[],[],[],[]]"
        if text == "GetAngle()":
            return ",".join(map(str, (*sample[0].joints_deg, 0, 0)))
        if text.startswith("JointMovJ("):
            target = tuple(float(value) for value in text[len("JointMovJ(") :].split(",")[:4])
            sample[0] = Feedback(target, 5, 0, False)
        elif text == "EnableRobot()":
            sample[0] = Feedback(sample[0].joints_deg, 5, 0, False)
        elif text == "DisableRobot()":
            sample[0] = Feedback(sample[0].joints_deg, 4, 0, False)
        elif text == "ResetRobot()":
            sample[0] = Feedback(sample[0].joints_deg, 5, 0, False)
        else:
            raise AssertionError(f"Unexpected command: {text}")
        return ""

    monkeypatch.setattr(control, "_command", command)
    return control, sample, now, commands


@pytest.fixture
def cartesian_rig(rig, monkeypatch):
    control, sample, now, commands = rig
    pose = [POSE]
    original = control._command

    def command(text, port=29999, *, pending=None, stopping=False):
        if text.startswith(("GetPose(", "InverseSolution(", "MovL(")):
            commands.append((text, port))
            if text == "GetPose(0,0)":
                return ",".join(map(str, pose[0]))
            if text.startswith("InverseSolution("):
                return ",".join(map(str, TARGET))
            if pending is not None:
                pending["sent"] = True
            pose[0] = tuple(float(value) for value in text[len("MovL(") :].split(",")[:4])
            sample[0] = Feedback(TARGET, 5, 0, False)
            return ""
        return original(text, port, pending=pending, stopping=stopping)

    monkeypatch.setattr(control, "_command", command)
    return control, sample, now, commands, pose


def test_construction_and_read_only_state_never_open_connections(rig):
    control, _, _, commands = rig
    assert not control.busy
    assert not control.stopped
    assert control.read_state().joints_deg == START
    assert not commands
    socket.create_connection.assert_not_called()


def test_operation_scope_excludes_other_actions_and_finishes_only_once(rig):
    control, _, _, commands = rig
    guard = Mock()
    active = control.begin_operation(send_guard=guard)
    assert active.busy and control.busy
    assert control.feedback_retention_reason() is not None
    guard.assert_not_called()
    for action in (control.begin_operation, control.enable, lambda: control.move(TARGET)):
        with pytest.raises(RuntimeError, match="Another robot operation"):
            action()
    assert control.busy
    assert active.wait_for_command(timeout=0)
    active.finish_operation()
    assert not control.busy
    assert control.feedback_retention_reason() is None

    next_active = control.begin_operation()
    active.finish_operation()
    assert next_active.busy and control.busy
    next_active.finish_operation()
    assert not control.busy and not control.stopped
    assert not commands


def test_cancelled_parent_rejects_operation_without_leaking_lock(rig):
    control, _, _, commands = rig
    control.cancel()
    with pytest.raises(RuntimeError, match="Control has been stopped"):
        control.begin_operation()
    assert not control.busy
    assert control.feedback_retention_reason() is not None
    assert not commands


def test_finished_child_stop_keeps_parent_stop_independent(rig, monkeypatch):
    control, _, _, commands = rig
    active = control.begin_operation()
    monkeypatch.setattr(active, "_command", control._command)
    assert active.stop_after_command()["ok"]
    assert control.feedback_retention_reason() is not None  # Operation still owns the lock.
    active.finish_operation()
    assert not control.stopped
    assert control.feedback_retention_reason() is None
    assert control.stop()["ok"]
    assert control.feedback_retention_reason() is None
    assert commands == [("ResetRobot()", 29999), ("ResetRobot()", 29999)]


def test_failed_child_stop_remains_visible_after_parent_stop(rig, monkeypatch):
    control, _, _, commands = rig
    active = control.begin_operation()
    monkeypatch.setattr(active, "_command", Mock(side_effect=ConnectionError("Lost stop reply")))
    assert not active.stop_after_command()["ok"]
    active.finish_operation()
    reason = control.feedback_retention_reason()
    assert "Lost stop reply" in reason
    assert control.stop()["ok"]
    assert control.feedback_retention_reason() == reason
    assert commands == [("ResetRobot()", 29999)]


@pytest.mark.parametrize("mode", [4, 5, 7])
def test_read_pose_uses_explicit_zero_frames_without_mutation(cartesian_rig, mode):
    control, sample, _, commands, _ = cartesian_rig
    sample[0] = Feedback(START, mode, 0, False)
    assert control.read_pose() == POSE
    assert commands == [("GetPose(0,0)", 29999)]


def test_pose_measurement_records_query_start_and_both_feedback_samples(cartesian_rig, monkeypatch):
    control, sample, now, commands, _ = cartesian_rig
    before = sample[0]
    started = now[0]
    after = Feedback((START[0] + 0.01, *START[1:]), 5, 1, False)
    original = control._command

    def command(text):
        now[0] += 0.1
        sample[0] = after
        return original(text)

    monkeypatch.setattr(control, "_command", command)
    measurement = control.read_pose_measurement()
    assert measurement.control is control
    assert measurement.pose == POSE
    assert measurement.requested_at == started
    assert measurement.before is before
    assert measurement.after is after
    assert commands == [("GetPose(0,0)", 29999)]


@pytest.mark.parametrize("raw", ["", "1,2,3", "1,2,3,4,0,0", "1,2,nan,4", "1,2,3,inf"])
def test_read_pose_rejects_malformed_or_nonfinite_values(rig, monkeypatch, raw):
    control, _, _, _ = rig
    monkeypatch.setattr(control, "_command", Mock(return_value=raw))
    with pytest.raises(ValueError):
        control.read_pose()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("method", ["read_pose", "read_pose_measurement"])
def test_read_pose_checks_feedback_freshness_before_and_after_query(rig, monkeypatch, when, method):
    control, sample, now, _ = rig
    stale = [when == "before"]
    control.feedback_snapshot = lambda: (sample[0], now[0] - (0.3 if stale[0] else 0))

    def query(text):
        stale[0] = True
        return ",".join(map(str, POSE))

    query = Mock(side_effect=query)
    monkeypatch.setattr(control, "_command", query)
    with pytest.raises(RuntimeError, match="stale"):
        getattr(control, method)()
    assert query.call_count == (0 if when == "before" else 1)


def test_solve_pose_checks_ik_without_sending_motion(cartesian_rig):
    control, sample, _, commands, pose = cartesian_rig
    assert control.solve_pose(LINEAR_TARGET) == TARGET
    assert commands == [("InverseSolution(251.000000,10.000000,40.000000,23.780000,0,0)", 29999)]
    assert sample[0].joints_deg == START
    assert pose[0] == POSE
    assert not control.busy
    assert not control.stopped


def test_linear_move_uses_controller_ik_and_verifies_pose_and_joints(cartesian_rig):
    control, _, now, commands, pose = cartesian_rig
    assert control.move_linear(LINEAR_TARGET).joints_deg == TARGET
    assert pose[0] == LINEAR_TARGET
    assert now[0] >= 10.8
    assert ("InverseSolution(251.000000,10.000000,40.000000,23.780000,0,0)", 29999) in commands
    mutations = [(text, port) for text, port in commands if text.startswith("MovL(")]
    assert mutations == [(LINEAR_MOVE, 30003)]
    assert not any(text.startswith(("Enable", "SpeedFactor", "CP(")) for text, _ in commands)
    assert not control.busy
    assert not control.stopped


@pytest.mark.parametrize(
    "target",
    [None, "1234", (1, 2, 3), (1, 2, 3, True), (1, 2, 3, float("nan")), (1, 2, 3, 10**400)],
)
def test_invalid_linear_target_does_not_read_or_send(rig, monkeypatch, target):
    control, _, _, commands = rig
    read = Mock(side_effect=AssertionError("Invalid target must not read feedback"))
    monkeypatch.setattr(control, "read_state", read)
    with pytest.raises(ValueError):
        control.move_linear(target)
    assert not commands
    read.assert_not_called()


def _inverse_payload(solution):
    return lambda text: solution


def _inverse_code(code):
    return lambda text: parse_controller_response(f"{code},{{0,0,0,0}},{text};".encode(), text)


def _inverse_raises(failure):
    def reply(text):
        raise failure

    return reply


INVERSE_PROTOCOL_FAILURES = [
    RuntimeError("-1,{0,0,0,0},InverseSolution(251,10,40,23.78,0,0);"),
    RuntimeError("Unexpected controller response: -1,{0,0,0,0},WrongCommand();"),
]


@pytest.mark.parametrize(
    "reply,expected,code,failure",
    [
        *(
            pytest.param(
                _inverse_payload(solution), ValueError, None, None, id=f"payload-{solution}"
            )
            for solution in ["1,2,3", "1,2,3,4,0,0", "0,0,0,nan", "0,0,0,inf"]
        ),
        *(
            pytest.param(_inverse_code(code), ControllerCommandError, code, None, id=f"code{code}")
            for code in [-2, -10000, 1]
        ),
        *(
            pytest.param(_inverse_raises(failure), RuntimeError, None, failure, id=f"protocol{i}")
            for i, failure in enumerate(INVERSE_PROTOCOL_FAILURES)
        ),
    ],
)
def test_inverse_solution_fault_stops_control_without_motion(
    cartesian_rig, monkeypatch, reply, expected, code, failure
):
    control, _, _, commands, _ = cartesian_rig
    original = control._command

    def command(text, *args, **kwargs):
        if text.startswith("InverseSolution("):
            return reply(text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(expected) as error:
        control.move_linear(LINEAR_TARGET)
    assert not isinstance(error.value, TargetRejectedError)
    if code is not None:
        assert error.value.code == code
    if failure is not None:
        assert error.value is failure
    assert not any(text.startswith("MovL(") for text, _ in commands)
    assert ("ResetRobot()", 29999) in commands
    assert control.stopped
    assert not control.busy


@pytest.mark.parametrize("prior_move", [False, True])
@pytest.mark.parametrize("rejection", ["controller", "joint_range"])
def test_rejected_linear_target_can_be_corrected_without_stopping_control(
    cartesian_rig, monkeypatch, prior_move, rejection
):
    control, sample, _, commands, pose = cartesian_rig
    if prior_move:
        control.move_linear(LINEAR_TARGET)
    before = sample[0]
    before_pose = pose[0]
    commands.clear()
    original = control._command

    def command(text, *args, **kwargs):
        if text.startswith("InverseSolution("):
            commands.append((text, 29999))
            if rejection == "joint_range":
                return "160,0,0,0"
            return parse_controller_response(f"-1,{{0,0,0,0}},{text};".encode(), text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(TargetRejectedError) as error:
        control.move_linear(LINEAR_TARGET)
    assert isinstance(error.value, ValueError)
    if rejection == "controller":
        assert isinstance(error.value.__cause__, ControllerCommandError)
    assert sample[0] == before
    assert pose[0] == before_pose
    assert not any(text.startswith(("MovL(", "ResetRobot(")) for text, _ in commands)
    assert not control.stopped
    assert not control.busy

    monkeypatch.setattr(control, "_command", original)
    corrected = (POSE[0] - 1, *POSE[1:])
    assert control.move_linear(corrected).joints_deg == TARGET
    assert pose[0] == corrected
    assert sum(text.startswith("MovL(") for text, _ in commands) == 1
    assert ("ResetRobot()", 29999) not in commands
    assert not control.stopped


@pytest.mark.parametrize("rejection", ["controller", "joint_range"])
@pytest.mark.parametrize("new_frame", [False, True])
def test_target_rejection_requires_feedback_newer_than_the_inverse_request(
    cartesian_rig, monkeypatch, rejection, new_frame
):
    control, sample, now, commands, _ = cartesian_rig
    original = control._command
    observed_new_frame = []

    def command(text, *args, **kwargs):
        if text.startswith("InverseSolution("):
            received_before_request = now[0]

            def feedback_snapshot():
                if new_frame and now[0] >= received_before_request + 0.1:
                    observed_new_frame.append(now[0])
                    return sample[0], now[0]
                return sample[0], received_before_request

            control.feedback_snapshot = feedback_snapshot
            now[0] += 0.01
            if rejection == "joint_range":
                return "160,0,0,0"
            return parse_controller_response(f"-1,{{0,0,0,0}},{text};".encode(), text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    expected = TargetRejectedError if new_frame else RuntimeError
    with pytest.raises(expected):
        control.move_linear(LINEAR_TARGET)
    assert bool(observed_new_frame) is new_frame
    assert control.stopped is not new_frame
    assert (("ResetRobot()", 29999) in commands) is not new_frame
    assert not any(text.startswith("MovL(") for text, _ in commands)
    assert not control.busy


@pytest.mark.parametrize("change", ["stale", "moving", "disabled", "alarm", "joints", "cancelled"])
def test_inverse_rejection_with_unconfirmed_stationary_state_remains_a_fault(
    cartesian_rig, monkeypatch, change
):
    control, sample, now, commands, _ = cartesian_rig
    original = control._command

    def command(text, *args, **kwargs):
        if text.startswith("InverseSolution("):
            if change == "stale":
                control.feedback_snapshot = lambda: (sample[0], now[0] - 0.3)
            elif change == "moving":
                sample[0] = Feedback(START, 7, 0, False)
            elif change == "disabled":
                sample[0] = Feedback(START, 4, 0, False)
            elif change == "alarm":
                sample[0] = Feedback(START, 5, 0, True)
            elif change == "joints":
                sample[0] = Feedback((START[0] + 0.2, *START[1:]), 5, 0, False)
            else:
                control.cancel()
            return parse_controller_response(f"-1,{{0,0,0,0}},{text};".encode(), text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError):
        control.move_linear(LINEAR_TARGET)
    assert not any(text.startswith("MovL(") for text, _ in commands)
    assert ("ResetRobot()", 29999) in commands
    assert control.stopped
    assert not control.busy


def test_linear_arrival_does_not_accept_idle_feedback_before_motion_starts(
    cartesian_rig, monkeypatch
):
    control, sample, now, _, pose = cartesian_rig
    original = control._command
    sent_at = [None]

    def command(text, *args, **kwargs):
        if text == "GetPose(0,0)" and sent_at[0] is not None:
            elapsed = now[0] - sent_at[0]
            pose[0] = POSE if elapsed < 0.8 else LINEAR_TARGET
            mode = 5 if elapsed < 0.5 or elapsed >= 1 else 7
            sample[0] = Feedback(START if elapsed < 0.8 else TARGET, mode, 0, False)
        result = original(text, *args, **kwargs)
        if text.startswith("MovL("):
            sent_at[0] = now[0]
            sample[0] = Feedback(START, 5, 0, False)
            pose[0] = POSE
        return result

    monkeypatch.setattr(control, "_command", command)
    assert control.move_linear(LINEAR_TARGET).joints_deg == TARGET
    assert now[0] - sent_at[0] >= 1.4


def test_linear_arrival_requires_new_feedback_receipts(cartesian_rig, monkeypatch):
    control, sample, _, _, pose = cartesian_rig
    sample[0] = Feedback(TARGET, 5, 0, False)
    pose[0] = LINEAR_TARGET
    monkeypatch.setattr(control, "_fresh", lambda modes: (sample[0], 10.0))
    with pytest.raises(TimeoutError):
        control._wait_linear(LINEAR_TARGET, TARGET, timeout=0.5)


def test_linear_final_pose_check_rejects_drift_after_stable_arrival(cartesian_rig, monkeypatch):
    control, _, _, commands, pose = cartesian_rig
    original = control._wait_linear

    def wait(*args):
        result = original(*args)
        pose[0] = (LINEAR_TARGET[0] + 0.2, *LINEAR_TARGET[1:])
        return result

    monkeypatch.setattr(control, "_wait_linear", wait)
    with pytest.raises(RuntimeError, match="Cartesian arrival"):
        control.move_linear(LINEAR_TARGET)
    assert sum(text.startswith("MovL(") for text, _ in commands) == 1
    assert control.stopped
    assert ("ResetRobot()", 29999) in commands


@pytest.mark.parametrize("initial_mode", [4, 5])
def test_explicit_enable_preserves_joints_and_does_not_move(rig, initial_mode):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, initial_mode, 0, False)
    actual = control.enable()
    assert actual.mode == 5
    assert actual.joints_deg == START
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == (["EnableRobot()"] if initial_mode == 4 else [])
    assert not control.busy
    assert not control.stopped


def test_move_uses_four_controller_angles_low_per_command_speed_and_verified_arrival(rig):
    control, _, now, commands = rig
    actual = control.move(TARGET)
    assert actual.joints_deg == TARGET
    assert now[0] >= 10.8  # Fresh stable preflight and fresh stable arrival.
    mutations = [(text, port) for text, port in commands if text.startswith("JointMovJ")]
    assert mutations == [(MOVE, 30003)]
    assert not any(text.startswith(("Enable", "SpeedFactor", "CP(")) for text, _ in commands)
    assert not control.busy


@pytest.mark.parametrize(
    "start,target,match",
    [
        pytest.param(START, (0, 0, 0), None, id="three-values"),
        pytest.param(START, (0, 0, 0, float("nan")), None, id="nan"),
        pytest.param(START, (0, 0, 0, True), None, id="bool"),
        pytest.param(START, (160, 0, 0, 0), "reference range", id="j1-out-of-range"),
        pytest.param(START, (0, 85, 50, 0), "reference range", id="j2-out-of-range"),
        pytest.param(START, (0, 0, 60, 0), "reference range", id="j3-out-of-range"),
        # Both endpoints satisfy their branch; crossing J3=0 violates the stricter J2 minimum.
        pytest.param((0, -22, 1, 0), (0, -17, -1, 0), "J2", id="segment-crosses-j3-zero"),
    ],
)
def test_invalid_joint_target_is_rejected_without_commands(rig, start, target, match):
    control, sample, _, commands = rig
    sample[0] = Feedback(start, 5, 0, False)
    with pytest.raises(ValueError, match=match):
        control.move(target)
    assert not commands
    assert not control.stopped


@pytest.mark.parametrize(
    "method,target", [("move", TARGET), ("move_linear", LINEAR_TARGET)], ids=["joint", "linear"]
)
def test_move_does_not_implicitly_enable_or_stop_disabled_robot(cartesian_rig, method, target):
    control, sample, _, commands, _ = cartesian_rig
    sample[0] = Feedback(START, 4, 0, False)
    with pytest.raises(RuntimeError, match="enabled and idle"):
        getattr(control, method)(target)
    assert not commands
    assert not control.stopped


def _with_axis(axis, value):
    joints = list(START)
    joints[axis] = value
    return tuple(joints)


@pytest.mark.parametrize(
    "start,target",
    [
        pytest.param(START, (*START[:3], START[3] + 90), id="j4-plus-90"),
        pytest.param(START, (0, 0, 0, 0), id="to-zero"),
        pytest.param((*START[:3], -200.0), (*START[:3], -199.0), id="j4-manufacturer-range"),
        pytest.param(_with_axis(0, 159), _with_axis(0, 158), id="margin-j1-159"),
        pytest.param(_with_axis(3, 359), _with_axis(3, 358), id="margin-j4-359"),
        pytest.param(_with_axis(3, -359), _with_axis(3, -358), id="margin-j4-minus-359"),
    ],
)
def test_valid_joint_move_sends_one_low_speed_joint_command(rig, start, target):
    control, sample, _, commands = rig
    sample[0] = Feedback(start, 5, 0, False)
    assert control.move(target).joints_deg == target
    moves = [(text, port) for text, port in commands if text.startswith("JointMovJ(")]
    values = ",".join(f"{value:.6f}" for value in target)
    assert moves == [(f"JointMovJ({values},SpeedJ=5,AccJ=5)", 30003)]
    assert not any(text.startswith(("Enable", "ClearError", "Reset")) for text, _ in commands)
    assert not control.stopped


def test_full_range_move_can_take_more_than_twenty_seconds(rig, monkeypatch):
    control, sample, now, _ = rig
    original_fresh = control._fresh

    def fresh(modes):
        if sample[0].joints_deg == TARGET and now[0] < 40:
            return Feedback(START, 7, 0, False), now[0]
        return original_fresh(modes)

    monkeypatch.setattr(control, "_fresh", fresh)
    assert control.move(TARGET).joints_deg == TARGET
    assert now[0] >= 40.4


def test_stop_does_not_wait_for_active_operation_lock(rig):
    control, _, _, commands = rig
    control._operation_lock.acquire()
    try:
        assert control.stop()["ok"]
        assert commands == [("ResetRobot()", 29999)]
    finally:
        control._operation_lock.release()


@pytest.mark.parametrize("initial_mode", [4, 5, 7])
def test_disable_verifies_mode_four_without_waiting_for_operation_lock(rig, initial_mode):
    control, sample, now, commands = rig
    sample[0] = Feedback(START, initial_mode, 0, False)
    with control._operation_lock:
        actual = control.disable()
    assert actual == Feedback(START, 4, 0, False)
    assert 10.0 < now[0] < 10.4
    assert control.disable() is actual
    assert commands == [
        ("DisableRobot()", 29999),
        ("RobotMode()", 29999),
        ("GetErrorID()", 29999),
        ("GetAngle()", 29999),
    ]
    assert control.stopped


def test_successful_disable_allows_explicit_enable_on_fresh_controller(rig, monkeypatch):
    control, _, _, commands = rig
    control.disable()
    fresh_control = JointControl(control.host, control.feedback_snapshot)
    monkeypatch.setattr(fresh_control, "_command", control._command)
    assert fresh_control.enable().mode == 5
    assert not fresh_control.stopped
    assert fresh_control.move(TARGET).joints_deg == TARGET
    for action in (
        control.enable,
        lambda: control.move(TARGET),
        lambda: control.move_linear(LINEAR_TARGET),
    ):
        with pytest.raises(RuntimeError, match="stopped"):
            action()
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == ["DisableRobot()", "EnableRobot()", MOVE]


@pytest.mark.parametrize("jitter_deg", [0.024, 0.026])
@pytest.mark.parametrize("jitter_stage", ["precheck", "after_command"])
def test_enable_after_disable_verifies_fresh_feedback_stationarity(
    rig,
    monkeypatch,
    jitter_deg,
    jitter_stage,
):
    control, sample, now, commands = rig
    settled = False

    def feedback():
        jitter_active = not settled and (
            jitter_stage == "precheck" or ("EnableRobot()", 29999) in commands
        )
        offset = jitter_deg if jitter_active and int((now[0] - 10.0) / 0.1) % 2 else 0.0
        sample[0] = Feedback((*START[:3], START[3] + offset), sample[0].mode, 0, False)
        return sample[0], now[0]

    control.feedback_snapshot = feedback
    assert control.disable().mode == 4
    fresh_control = JointControl(control.host, control.feedback_snapshot)
    monkeypatch.setattr(fresh_control, "_command", control._command)

    if jitter_deg < 0.025:
        assert fresh_control.enable().mode == 5
        assert not fresh_control.stopped
        expected_mutations = ["DisableRobot()", "EnableRobot()"]
    elif jitter_stage == "precheck":
        with pytest.raises(PreparationRejectedError, match="Wait until the robot") as failure:
            fresh_control.enable()
        diagnostic = str(failure.value.__cause__)
        assert "joint_span_deg=(0.0, 0.0, 0.0, 0.026)" in diagnostic
        assert not fresh_control.stopped and not fresh_control.busy
        assert fresh_control._pending is None and fresh_control._stop_result is None
        assert not any(text in {"EnableRobot()", "ResetRobot()"} for text, _ in commands)
        settled = True
        assert fresh_control.enable().mode == 5
        assert not fresh_control.stopped
        expected_mutations = ["DisableRobot()", "EnableRobot()"]
    else:
        with pytest.raises(RuntimeError) as failure:
            fresh_control.enable()
        diagnostic = str(failure.value)
        assert diagnostic.startswith("Robot did not reach a fresh, verified stationary state")
        assert diagnostic.count("Robot did not reach a fresh, verified stationary state") == 2
        assert "physical stop not confirmed" in diagnostic
        assert diagnostic.endswith("use the physical emergency stop")
        assert "joint_span_deg=(0.0, 0.0, 0.0, 0.026)" in diagnostic
        assert fresh_control.stopped
        assert not fresh_control.stop()["stationary_verified"]
        assert "stage=after EnableRobot()" in diagnostic
        assert fresh_control._pending["sent"] and fresh_control._pending["ok"]
        expected_mutations = ["DisableRobot()", "EnableRobot()", "ResetRobot()"]

    assert [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))] == (
        expected_mutations
    )


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "alarm",
        "mode",
        "source_difference",
        "cancelled",
        "disabling",
        "uncertain",
        "parent_uncertain",
        "new_pending",
        "prior_pending",
    ],
)
def test_disabled_stationary_timeout_rechecks_safety_before_allowing_retry(rig, monkeypatch, fault):
    control, sample, now, commands = rig
    if fault == "parent_uncertain":
        parent = control
        control = JointControl(parent.host, parent.feedback_snapshot, parent=parent)
        monkeypatch.setattr(control, "_command", parent._command)
    sample[0] = Feedback(START, 4, 0, False)
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    if fault == "prior_pending":
        control._pending = pending

    def feedback():
        offset = 0.026 if int((now[0] - 10.0) / 0.1) % 2 else 0.0
        sample[0] = Feedback((*START[:3], START[3] + offset), 4, 0, False)
        return sample[0], now[0]

    control.feedback_snapshot = feedback
    original_command = control._command
    mode_reads = 0

    def command(text, *args, **kwargs):
        nonlocal mode_reads
        result = original_command(text, *args, **kwargs)
        if text == "RobotMode()":
            mode_reads += 1
            if mode_reads == 2 and fault == "mode":
                return "5"
        if mode_reads == 2:
            if text == "GetErrorID()" and fault == "alarm":
                return "[[1],[],[],[],[]]"
            if text == "GetAngle()":
                if fault == "stale":
                    control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
                if fault == "source_difference":
                    return ",".join(map(str, (*START[:3], START[3] + 0.2, 0, 0)))
                if fault in {"cancelled", "disabling"}:
                    control.cancel(disabling=fault == "disabling")
                if fault == "uncertain":
                    control._stop_uncertain = True
                if fault == "parent_uncertain":
                    parent._stop_uncertain = True
                if fault == "new_pending":
                    control._pending = pending
        return result

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises((RuntimeError, TimeoutError)) as failure:
        control.enable()
    assert not isinstance(failure.value, PreparationRejectedError)
    assert control.stopped
    assert ("EnableRobot()", 29999) not in commands
    assert (("ResetRobot()", 29999) in commands) is (fault != "disabling")


def test_disabled_precheck_does_not_treat_other_timeouts_as_stationary_rejections(rig, monkeypatch):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, 4, 0, False)
    original_wait = control._wait_stable

    def wait(*args, **kwargs):
        if not kwargs.get("stopping"):
            raise TimeoutError("Unrelated timeout")
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(control, "_wait_stable", wait)
    with pytest.raises(TimeoutError, match="Unrelated timeout"):
        control.enable()
    assert control.stopped
    assert ("EnableRobot()", 29999) not in commands
    assert ("ResetRobot()", 29999) in commands


@pytest.mark.parametrize("action", ["enable", "move"])
def test_disable_cancels_active_operation_without_competing_failure_stop(rig, monkeypatch, action):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, 4 if action == "enable" else 5, 0, False)
    entered = threading.Event()
    original_wait = control._wait_stable
    errors = []

    def wait(*args, **kwargs):
        if control._pending is not None and not kwargs.get("stopping"):
            entered.set()
            assert control._stopped.wait(1)
            raise RuntimeError("Operation was cancelled")
        return original_wait(*args, **kwargs)

    def perform():
        try:
            control.enable() if action == "enable" else control.move(TARGET)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(control, "_wait_stable", wait)
    worker = threading.Thread(target=perform)
    worker.start()
    try:
        assert entered.wait(1)
        assert control.busy
        control.cancel(disabling=True)
        assert control.disable().mode == 4
    finally:
        control.cancel(disabling=True)
        worker.join(1)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert str(errors[0]) == "Operation was cancelled"
    assert not control.busy
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == ["EnableRobot()" if action == "enable" else MOVE, "DisableRobot()"]


def test_concurrent_disable_sends_once_and_returns_same_result(rig, monkeypatch):
    control, _, _, commands = rig
    entered, release = threading.Event(), threading.Event()
    original_command = control._command
    results = []

    def command(text, *args, **kwargs):
        if text == "DisableRobot()":
            entered.set()
            assert release.wait(1)
        return original_command(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    workers = [threading.Thread(target=lambda: results.append(control.disable())) for _ in range(2)]
    workers[0].start()
    try:
        assert entered.wait(1)
        workers[1].start()
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
    assert len(results) == 2
    assert results[0] is results[1]
    assert [text for text, _ in commands].count("DisableRobot()") == 1


@pytest.mark.parametrize(
    "failure",
    [
        "ack",
        "rejected_ack",
        "enabled",
        "dashboard",
        "feedback",
        "alarm",
        "dashboard_alarm",
        "final_mode",
        "final_feedback",
    ],
)
def test_unconfirmed_disable_stays_cancelled_and_uses_failure_stop_once(rig, monkeypatch, failure):
    control, sample, now, commands = rig
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "DisableRobot()" and failure == "ack":
            commands.append((text, 29999))
            raise TimeoutError("Disable acknowledgement was lost")
        if text == "DisableRobot()" and failure == "rejected_ack":
            commands.append((text, 29999))
            raise ControllerCommandError(-1, text, "Disable command was rejected")
        result = original_command(text, *args, **kwargs)
        if text == "DisableRobot()":
            if failure == "enabled":
                sample[0] = Feedback(START, 5, 0, False)
            elif failure == "feedback":
                control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
            elif failure == "alarm":
                sample[0] = Feedback(START, 4, 0, True)
        if text == "RobotMode()" and failure == "dashboard":
            return "5"
        if text == "GetErrorID()" and failure == "dashboard_alarm":
            return "[[1],[],[],[],[]]"
        if text == "GetAngle()":
            if failure == "final_mode":
                sample[0] = Feedback(START, 5, 0, False)
            elif failure == "final_feedback":
                control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
        return result

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises((RuntimeError, TimeoutError)) as first:
        control.disable()
    with pytest.raises(type(first.value)) as second:
        control.disable()
    assert first.value is second.value
    assert control.stopped
    assert control._disable_result is None
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == ["DisableRobot()", "ResetRobot()"]
    if failure == "enabled":
        assert 15.0 <= now[0] < 15.5
        assert "Fresh disabled robot feedback was not confirmed" in str(first.value)


@pytest.mark.parametrize("settles_after_reset", [False, True])
def test_disable_after_confirmed_stop_requires_new_stop_verification(
    rig,
    monkeypatch,
    settles_after_reset,
):
    control, sample, now, commands = rig
    previous_stop = control.stop()
    assert previous_stop["ok"]
    original_command = control._command
    drift_started = [None]

    def feedback():
        if drift_started[0] is not None:
            joints = (*START[:3], START[3] - (now[0] - drift_started[0]))
            sample[0] = Feedback(joints, 4, 0, False)
        return sample[0], now[0]

    def command(text, *args, **kwargs):
        if text == "ResetRobot()":
            commands.append((text, 29999))
            if settles_after_reset:
                drift_started[0] = None
            return ""
        result = original_command(text, *args, **kwargs)
        if text == "DisableRobot()":
            drift_started[0] = now[0]
        return result

    control.feedback_snapshot = feedback
    monkeypatch.setattr(control, "_command", command)
    disabled = control.disable()
    assert disabled.mode == 4
    assert control._stop_result is None
    assert control._disable_error is None
    result = control.stop()
    assert result is not previous_stop
    assert result["ok"] is settles_after_reset
    assert result["stationary_verified"] is settles_after_reset
    assert control.stopped
    assert control.disable() is disabled
    assert control.stop() is result
    assert commands == [
        ("ResetRobot()", 29999),
        ("DisableRobot()", 29999),
        ("RobotMode()", 29999),
        ("GetErrorID()", 29999),
        ("GetAngle()", 29999),
        ("ResetRobot()", 29999),
    ]
    with pytest.raises(RuntimeError, match="stopped"):
        control.enable()


def test_disable_during_drift_does_not_allow_enable_or_prove_rest(rig, monkeypatch):
    control, sample, now, commands = rig

    def feedback():
        sample[0] = Feedback((*START[:3], START[3] + now[0] - 10.0), sample[0].mode, 0, False)
        return sample[0], now[0]

    control.feedback_snapshot = feedback
    disabled = control.disable()
    assert disabled.mode == 4
    assert control._disable_error is None
    assert control._stop_result is None
    assert control.feedback_retention_reason() is None
    before_verification = list(commands)
    with pytest.raises(TimeoutError, match="stationary state"):
        control.verify_stationary()
    # The verdict describes command state. It cannot replace the rest check.
    assert control.feedback_retention_reason() is None
    assert commands == before_verification
    assert control._disable_result is disabled
    assert control._stop_result is None

    fresh_control = JointControl(control.host, control.feedback_snapshot)
    monkeypatch.setattr(fresh_control, "_command", control._command)
    with pytest.raises(PreparationRejectedError, match="not verified stationary"):
        fresh_control.enable()
    assert not fresh_control.stopped
    assert fresh_control._stop_result is None
    assert "EnableRobot()" not in [text for text, _ in commands]
    assert [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))] == [
        "DisableRobot()",
    ]


def test_disable_does_not_require_stationary_angle_agreement(rig, monkeypatch):
    control, sample, _, commands = rig
    original_command = control._command

    def command(text, *args, **kwargs):
        result = original_command(text, *args, **kwargs)
        if text == "GetAngle()":
            sample[0] = Feedback((*START[:3], START[3] + 0.2), 4, 0, False)
        return result

    monkeypatch.setattr(control, "_command", command)
    assert control.disable() is sample[0]
    assert control._disable_result.joints_deg[3] == START[3] + 0.2
    assert control._stop_result is None
    assert ("ResetRobot()", 29999) not in commands


@pytest.mark.parametrize("new_receipt", [False, True])
def test_disable_requires_feedback_received_after_ack(rig, monkeypatch, new_receipt):
    control, sample, now, commands = rig
    before_ack = now[0]

    def feedback():
        received = now[0] if new_receipt and now[0] >= before_ack + 0.1 else before_ack
        return sample[0], received

    control.feedback_snapshot = feedback
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "RobotMode()":
            assert now[0] >= before_ack + 0.1
        return original_command(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    if new_receipt:
        assert control.disable().mode == 4
    else:
        with pytest.raises(RuntimeError, match="stale"):
            control.disable()
        assert control._disable_result is None
        assert ("RobotMode()", 29999) not in commands


@pytest.mark.parametrize("mode", [4, 5])
@pytest.mark.parametrize("cancelled", [False, True])
def test_verify_stationary_is_read_only_and_preserves_cancellation(rig, mode, cancelled):
    control, sample, now, commands = rig
    sample[0] = Feedback(START, mode, 0, False)
    if cancelled:
        control._pending = {"done": threading.Event(), "sent": True, "ok": False}
        control.cancel()
    assert control.verify_stationary() is sample[0]
    assert now[0] >= 10.4
    assert control.stopped is cancelled
    assert control._stop_uncertain is cancelled
    assert control._stop_result is None
    assert not commands


@pytest.mark.parametrize("fault", ["stale", "alarm", "moving", "duplicate"])
def test_verify_stationary_rejects_invalid_feedback_without_commands(rig, monkeypatch, fault):
    control, sample, now, commands = rig
    if fault == "stale":
        control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
    elif fault == "duplicate":
        monkeypatch.setattr(control, "_fresh", lambda modes: (sample[0], 10.0))
    else:
        sample[0] = Feedback(START, 7 if fault == "moving" else 4, 0, fault == "alarm")
    with pytest.raises((RuntimeError, TimeoutError)):
        control.verify_stationary()
    assert not commands
    assert not control.stopped
    assert control._stop_result is None


def test_late_mutation_ack_cannot_confirm_disable_or_send_again(rig, monkeypatch):
    control, _, _, commands = rig
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    control._pending = pending
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "DisableRobot()":
            pending["ok"] = True
            pending["done"].set()
        return original_command(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError, match="ordering remains unknown"):
        control.disable()
    with pytest.raises(RuntimeError, match="ordering remains unknown"):
        control.disable()
    assert commands == [("DisableRobot()", 29999), ("ResetRobot()", 29999)]
    assert control.stopped


def test_cancel_is_immediate_even_when_command_and_operation_locks_are_held(rig):
    control, _, _, commands = rig
    with control._operation_lock, control._send_lock:
        control.cancel()
        assert control.stopped
    assert not commands
    assert control.stop()["ok"]


@pytest.mark.parametrize("ack_timing", ["after_cancel", "during_reset"])
def test_late_mutation_ack_cannot_clear_unknown_stop_ordering(rig, monkeypatch, ack_timing):
    control, _, _, commands = rig
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    control._pending = pending

    def acknowledge():
        pending["ok"] = True
        pending["done"].set()

    if ack_timing == "after_cancel":
        control.cancel()
        acknowledge()
    else:
        original = control._command

        def command(*args, **kwargs):
            acknowledge()
            return original(*args, **kwargs)

        monkeypatch.setattr(control, "_command", command)
    stable = Mock()
    monkeypatch.setattr(control, "_wait_stable", stable)
    result = control.stop()
    assert not result["ok"]
    assert not result["stationary_verified"]
    assert "unknown" in result["error"]
    assert control.stop() is result
    assert commands == [("ResetRobot()", 29999)]
    stable.assert_not_called()


@pytest.mark.parametrize("kind", ["stale", "future", "alarm", "mode"])
def test_read_rejects_stale_or_faulted_feedback_without_commands(rig, kind):
    control, sample, now, commands = rig
    age = 0.26 if kind == "stale" else -0.1 if kind == "future" else 0
    sample[0] = Feedback(START, 9 if kind == "mode" else 5, 0, kind == "alarm")
    control.feedback_snapshot = lambda: (sample[0], now[0] - age)
    with pytest.raises(RuntimeError):
        control.read_state()
    assert not commands


@pytest.mark.parametrize("raw", ["1,2,3", "1,2,3,4,nan,0", "1,2,3,inf"])
def test_dashboard_rejects_bad_joint_data_including_truncated_extras(rig, monkeypatch, raw):
    control, _, _, _ = rig
    monkeypatch.setattr(control, "_command", Mock(side_effect=["5", "[[],[]]", raw]))
    with pytest.raises(ValueError, match="Invalid actual"):
        control._dashboard_state()


@pytest.mark.parametrize("errors", ["[]", "{}", "[[],[1]]", "[[],0]"])
def test_dashboard_rejects_alarm_or_malformed_error_state(rig, monkeypatch, errors):
    control, _, _, _ = rig
    command = Mock(side_effect=["5", errors])
    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError, match="alarms"):
        control._dashboard_state()
    assert command.call_count == 2


def test_repeated_same_receipt_cannot_confirm_arrival(rig, monkeypatch):
    control, sample, _, _ = rig
    monkeypatch.setattr(control, "_fresh", lambda modes: (sample[0], 10.0))
    with pytest.raises(TimeoutError) as failure:
        control._wait_stable(START, modes={5}, final_modes={5}, timeout=0.5)
    assert "samples=1" in str(failure.value)
    assert "longest_stable_s=0.000" in str(failure.value)


def test_stationary_timeout_reports_observed_joint_span_and_stable_dwell(rig, monkeypatch):
    control, _, now, _ = rig

    def fresh(modes):
        step = int((now[0] - 10.0 + 0.000001) / 0.1)
        offset = (0.0, 0.01, 0.03, 0.04)[step]
        return Feedback((*START[:3], START[3] + offset), 4, 0, False), 10.0 + step * 0.1

    monkeypatch.setattr(control, "_fresh", fresh)
    with pytest.raises(TimeoutError) as failure:
        control._wait_stable(modes={4}, final_modes={4}, timeout=0.36)
    diagnostic = str(failure.value)
    assert "samples=4" in diagnostic
    assert "joint_span_deg=(0.0, 0.0, 0.0, 0.04)" in diagnostic
    assert "longest_stable_s=0.100" in diagnostic
    assert "required_stable_s=0.4" in diagnostic
    assert "tolerance_deg=0.025" in diagnostic


def test_arrival_requires_stable_dwell_and_final_idle_mode(rig, monkeypatch):
    control, _, now, _ = rig

    def fresh(modes):
        mode = 7 if now[0] < 10.2 else 5
        joint4 = TARGET[3] + (0.08 if now[0] < 10.3 else 0)
        return Feedback((*TARGET[:3], joint4), mode, 0, False), now[0]

    monkeypatch.setattr(control, "_fresh", fresh)
    assert control._wait_stable(TARGET, modes={5, 7}, final_modes={5}).joints_deg == TARGET
    assert now[0] >= 10.7


def test_stop_is_latched_and_repeated_calls_never_resend(rig):
    control, _, _, commands = rig
    result = control.stop()
    assert result == {
        "ok": True,
        "stationary_verified": True,
        "physical_stop_requested": True,
    }
    assert control.stop() is result
    for action in (control.enable, lambda: control.move(TARGET)):
        with pytest.raises(RuntimeError, match="stopped"):
            action()
    assert commands == [("ResetRobot()", 29999)]


def test_stop_ack_does_not_prove_stillness(rig, monkeypatch):
    control, _, _, commands = rig
    monkeypatch.setattr(control, "_wait_stable", Mock(side_effect=TimeoutError("Not stationary")))
    assert control.stop()["stationary_verified"] is False
    assert control.stop()["ok"] is False
    assert commands == [("ResetRobot()", 29999)]


@pytest.mark.parametrize("speed", [1, 25, 100])
def test_jog_release_reuses_child_connection_until_finish(rig, monkeypatch, speed):
    control, sample, now, _ = rig
    sample[0] = Feedback(START, 5, 0, False, speed_scaling_raw=speed)
    connection = fake_connection(monkeypatch, [b"0,{},MoveJog(J4+);", b"0,{},MoveJog();"])
    active = control.begin_operation()
    active._guarded_command(
        "MoveJog(J4+)", modes={5, 11}, send_modes={5}, expected=START, port=30003
    )
    sample[0] = Feedback(START, 11, 0, False, speed_scaling_raw=speed)

    def reply(size):
        now[0] += 0.185
        sample[0] = Feedback(START, 5, 0, False, speed_scaling_raw=speed)
        return b"0,{},MoveJog();"

    connection.recv.side_effect = reply
    before = now[0]
    result = active.stop_after_command(jog=True, jog_speed_percent=speed)
    assert result["ok"]
    assert 0.24 <= now[0] - before < 0.26
    assert active.stop(jog=True) is result
    assert connection.sendall.call_args_list == [call(b"MoveJog(J4+)"), call(b"MoveJog()")]
    assert socket.create_connection.call_count == 1
    connection.close.assert_not_called()
    active.finish_operation()
    connection.close.assert_called_once()
    assert not control.stopped
    assert control.feedback_retention_reason() is None


@pytest.mark.parametrize("fault", ["cancelled", "unknown_ack"])
def test_jog_fault_uses_reset_and_never_short_stop(rig, monkeypatch, fault):
    control, _, now, commands = rig
    active = control.begin_operation()
    monkeypatch.setattr(active, "_command", control._command)
    if fault == "unknown_ack":
        pending = {"done": threading.Event(), "sent": True, "ok": False}
        pending["done"].set()
        active._pending = pending
    else:
        active.cancel()
    result = active.stop_after_command(jog=True)
    assert commands == [("ResetRobot()", 29999)]
    if fault == "cancelled":
        assert result["ok"] and now[0] >= 10.4
    else:
        assert not result["ok"] and "unknown" in result["error"]
    active.finish_operation()


@pytest.mark.parametrize("speed", [1, 25])
@pytest.mark.parametrize("fault", ["lost_ack", "speed", "mode"])
def test_jog_stop_failure_requests_reset_but_keeps_parent_locked(rig, monkeypatch, fault, speed):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, 5, 0, False, speed_scaling_raw=speed)
    active = control.begin_operation()
    active._pending = {
        "command": "MoveJog(J4+)",
        "port": 30003,
        "sent": True,
        "ok": True,
        "done": threading.Event(),
    }
    active._pending["done"].set()

    def command(text, port=29999, **kwargs):
        commands.append((text, port))
        if text == "MoveJog()":
            if fault == "lost_ack":
                raise TimeoutError("Lost jog stop acknowledgement")
            sample[0] = Feedback(START, 11 if fault == "mode" else 5, 0, False, 2)
            if fault == "mode":
                sample[0] = Feedback(START, 11, 0, False, speed)
        else:
            assert text == "ResetRobot()"
            sample[0] = Feedback(START, 5, 0, False, 1)
        return ""

    monkeypatch.setattr(active, "_command", command)
    result = active.stop_after_command(jog=True, jog_speed_percent=speed)
    assert not result["ok"]
    assert commands == [("MoveJog()", 30003), ("ResetRobot()", 29999)]
    assert active.stop(jog=True) is result
    active.finish_operation()
    assert control.feedback_retention_reason() is not None
    assert control._stop_uncertain is (fault == "lost_ack")


def test_jog_stop_cannot_accept_ack_that_arrives_during_cancellation(rig, monkeypatch):
    control, _, _, commands = rig
    active = control.begin_operation()
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    active._pending = pending
    cancel = active.cancel

    def late_ack():
        cancel()
        pending["ok"] = True
        pending["done"].set()

    monkeypatch.setattr(active, "cancel", late_ack)
    monkeypatch.setattr(active, "_command", control._command)
    assert not active.stop(jog=True)["ok"]
    assert commands == [("ResetRobot()", 29999)]
    assert control._stop_uncertain
    active.finish_operation()
    assert control.feedback_retention_reason() is not None


@pytest.mark.parametrize("case", ["pre_ack", "repeat", "gap", "drift", "jog_mode"])
def test_short_jog_stop_requires_post_ack_dense_fresh_idle_feedback(rig, monkeypatch, case):
    control, _, now, _ = rig

    def fresh(modes):
        elapsed = now[0] - 10.0
        receipt = 10.0 + int((elapsed + 1e-9) / 0.008) * 0.008
        mode, offset = 5, 0
        if case == "pre_ack" and elapsed < 0.06:
            receipt = 9.99
        elif case == "repeat":
            receipt = 10.001
        elif case == "gap" and 0.025 <= elapsed < 0.064:
            receipt = 10.024
        elif case == "drift" and elapsed >= 0.04:
            offset = 0.03
        elif case == "jog_mode" and elapsed < 0.032:
            mode = 11
        return Feedback((*START[:3], START[3] + offset), mode, 0, False, 1), receipt

    monkeypatch.setattr(control, "_fresh", fresh)
    if case == "repeat":
        with pytest.raises(TimeoutError):
            control._wait_stable(modes={5, 11}, final_modes={5}, timeout=0.2, jog_stop_ack=10.0)
        return
    result = control._wait_stable(modes={5, 11}, final_modes={5}, timeout=0.2, jog_stop_ack=10.0)
    assert result.mode == 5
    minimum = {"pre_ack": 10.112, "gap": 10.12, "drift": 10.096, "jog_mode": 10.088}
    assert now[0] >= minimum[case]


def test_failed_jog_connection_cannot_reconnect(rig, monkeypatch):
    control, _, _, _ = rig
    connection = fake_connection(monkeypatch, [b"0,{},OtherCommand();"])
    active = control.begin_operation()
    with pytest.raises(RuntimeError, match="Unexpected controller response"):
        active._command("MoveJog(J4+)", port=30003)
    with pytest.raises(RuntimeError, match="new command is prohibited"):
        active._command("MoveJog(J4-)", port=30003)
    assert socket.create_connection.call_count == 1
    assert connection.sendall.call_args_list == [call(b"MoveJog(J4+)")]
    connection.close.assert_called_once()
    active.finish_operation()


def test_jog_connection_rejects_extra_reply_before_matching_echo(rig, monkeypatch):
    control, _, _, _ = rig
    active = control.begin_operation()
    connection = fake_connection(monkeypatch, [b"0,{},Other();0,{},MoveJog();"])
    with pytest.raises(RuntimeError, match="multiple controller responses"):
        active._command("MoveJog()", port=30003)
    assert active._jog_connection_failed
    connection.close.assert_called_once()
    active.finish_operation()


def test_release_during_initial_jog_connection_sends_no_motion_and_keeps_parent_ready(
    rig, monkeypatch
):
    control, sample, _, _ = rig
    sample[0] = Feedback(START, 5, 0, False, 1)
    released = threading.Event()
    motion = Mock()
    stop_socket = fake_connection(monkeypatch, [b"0,{},ResetRobot();"])

    def connect(address, **kwargs):
        if address[1] == 30003:
            released.set()
            return motion
        return stop_socket

    def guard():
        if released.is_set():
            raise RuntimeError("Keyboard hold cancelled")

    monkeypatch.setattr(socket, "create_connection", connect)
    active = control.begin_operation(send_guard=guard)
    with pytest.raises(RuntimeError, match="hold cancelled"):
        active._guarded_command(
            "MoveJog(J4+)", modes={5, 11}, send_modes={5}, expected=START, port=30003
        )
    assert not active._pending["sent"]
    assert active.stop_after_command(jog=True)["ok"]
    motion.sendall.assert_not_called()
    assert stop_socket.sendall.call_args_list == [call(b"ResetRobot()")]
    active.finish_operation()
    assert not control.stopped
    assert control.feedback_retention_reason() is None


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_stop_and_disable_allow_jog_mode_only_during_deceleration(rig, monkeypatch, action):
    control, sample, now, _ = rig
    final_mode = 4 if action == "disable" else 5

    def snapshot():
        sample[0] = Feedback(START, 11 if now[0] < 10.03 else final_mode, 0, False, 1)
        return sample[0], now[0]

    monkeypatch.setattr(control, "feedback_snapshot", snapshot)
    result = getattr(control, action)()
    if action == "stop":
        assert result["ok"] and now[0] >= 10.43
    else:
        assert result.mode == 4 and now[0] >= 10.03


def test_fault_during_pending_ack_requests_stop_without_retry(rig, monkeypatch):
    control, _, _, _ = rig
    entered, release = threading.Event(), threading.Event()
    commands = []
    original_fresh = control._fresh

    def command(text, port=29999, *, pending=None, stopping=False):
        commands.append(text)
        if pending is not None:
            pending["sent"] = True
            entered.set()
            assert release.wait(1)
        return ""

    def fresh(modes):
        if entered.is_set():
            raise RuntimeError("Feedback alarm while waiting for ACK")
        return original_fresh(modes)

    monkeypatch.setattr(control, "_command", command)
    monkeypatch.setattr(control, "_fresh", fresh)
    monkeypatch.setattr(control, "_preflight", lambda modes: (5, START))
    try:
        with pytest.raises(RuntimeError, match="physical stop not confirmed"):
            control.move(TARGET)
        assert commands == [MOVE, "ResetRobot()"]
        assert not release.is_set()
        assert control.stopped
        assert "unresolved" in control.stop()["error"]
        assert not control.busy
        assert control.command_pending
        assert not control.wait_for_command(timeout=0)
        reason = control.feedback_retention_reason()
        assert reason is not None
    finally:
        release.set()
        assert control.wait_for_command()
    assert not control.command_pending
    assert control.feedback_retention_reason() == reason


def test_delayed_pose_poll_after_failed_stop_never_reconnects(monkeypatch):
    connection = fake_connection(monkeypatch, [b"", b"0,{250,10,40,23.78},GetPose(0,0);"])
    control = JointControl("robot.test", lambda: (Feedback(START, 5, 0, False), time.monotonic()))
    assert not control.stop()["ok"]
    with pytest.raises(DashboardQueryCancelledError):
        control.read_pose()
    socket.socket.assert_not_called()
    assert connection.sendall.call_args_list == [call(b"ResetRobot()")]


def test_pose_poll_preserves_child_stop_failure_after_parent_stop_succeeds(monkeypatch):
    connection = fake_connection(
        monkeypatch,
        [
            b"",
            b"0,{},ResetRobot();",
            b"0,{250,10,40,23.78},GetPose(0,0);",
        ],
    )
    control = JointControl("robot.test", lambda: (Feedback(START, 5, 0, False), time.monotonic()))
    child = control.begin_operation()
    assert not child.stop()["ok"]
    child.finish_operation()
    assert control.stop()["ok"]
    assert control.feedback_retention_reason() is not None
    with pytest.raises(DashboardQueryCancelledError):
        control.read_pose()
    socket.socket.assert_not_called()
    assert connection.sendall.call_args_list == [call(b"ResetRobot()"), call(b"ResetRobot()")]


@pytest.mark.parametrize("phase", ("before", "dns", "connect"))
def test_pose_poll_rechecks_cancellation_before_connect_and_send(monkeypatch, phase):
    connection = fake_connection(monkeypatch, [b"0,{250,10,40,23.78},GetPose(0,0);"])
    control = JointControl("robot.test", lambda: (Feedback(START, 5, 0, False), time.monotonic()))
    if phase == "before":
        control.cancel()
    elif phase == "dns":
        addresses = socket.getaddrinfo.return_value
        socket.getaddrinfo.side_effect = lambda *args: (control.cancel(), addresses)[1]
    else:
        connection.connect_ex.side_effect = lambda *args: (control.cancel(), 0)[1]
    with pytest.raises(DashboardQueryCancelledError):
        control.read_pose_measurement()
    connection.sendall.assert_not_called()
    if phase != "connect":
        socket.socket.assert_not_called()


def test_transport_reassembles_reply_without_binding_host_source_ip(monkeypatch):
    connection = fake_connection(monkeypatch, [b"0,{1,2,", b"3,4,0,0},GetAngle();"])
    control = JointControl("robot.test", Mock())
    assert control._command("GetAngle()") == "1,2,3,4,0,0"
    connection.connect_ex.assert_called_once_with(("robot.test", 29999))
    socket.create_connection.assert_not_called()
    connection.sendall.assert_called_once_with(b"GetAngle()")


def test_dashboard_queries_wait_for_previous_connection_to_close(monkeypatch):
    second_command = "GetAngle()"
    first_received, release_first = threading.Event(), threading.Event()
    second_started, second_connected = threading.Event(), threading.Event()
    events, results, failures = [], [], []

    class Connection:
        def __init__(self, index):
            self.index = index

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close", self.index))

        def settimeout(self, timeout):
            pass

        def setblocking(self, value):
            pass

        def connect_ex(self, address):
            return 0

        def sendall(self, data):
            self.command = data.decode("ascii")

        def recv(self, size):
            if self.index == 0:
                first_received.set()
                assert release_first.wait(1)
            return f"0,{{}},{self.command};".encode("ascii")

    connections = []

    def connect(address, timeout):
        assert address == ("robot.test", 29999)
        connection = Connection(len(connections))
        connections.append(connection)
        events.append(("connect", connection.index))
        if connection.index == 1:
            second_connected.set()
        return connection

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(socket, "socket", lambda *args: connect(("robot.test", 29999), 2.0))
    control = JointControl("robot.test", Mock())

    def invoke(text):
        try:
            if text == second_command:
                second_started.set()
            results.append(control._command(text, stopping=text == "ResetRobot()"))
        except Exception as error:
            failures.append(error)

    first = threading.Thread(target=invoke, args=("GetPose(0,0)",))
    second = threading.Thread(target=invoke, args=(second_command,))
    try:
        first.start()
        assert first_received.wait(1)
        second.start()
        assert second_started.wait(1)
        assert not second_connected.wait(0.05)
    finally:
        release_first.set()
        first.join(1)
        if second.ident is not None:
            second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert not failures
    assert results == ["", ""]
    assert events == [("connect", 0), ("close", 0), ("connect", 1), ("close", 1)]


def test_dashboard_lock_wait_is_bounded_without_opening_another_connection(monkeypatch):
    control = JointControl("robot.test", Mock())
    lock = Mock()
    lock.acquire.return_value = False
    monkeypatch.setattr(control, "_dashboard_lock", lock)
    with pytest.raises(TimeoutError, match="Dashboard connection is busy"):
        control._command("GetPose(0,0)")
    lock.acquire.assert_called_once_with(timeout=2.0)
    lock.release.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("stop_command", ["ResetRobot()", "DisableRobot()"])
@pytest.mark.parametrize("phase", ["connect", "send", "recv", "recv_eof"])
def test_child_stop_interrupts_parent_query_and_closes_before_reconnecting(
    monkeypatch,
    stop_command,
    phase,
):
    entered, closed = threading.Event(), threading.Event()
    failures, events = [], []
    query = fake_connection(monkeypatch, [])

    def block(*args):
        entered.set()
        assert closed.wait(1)
        if phase == "recv_eof":
            return b""
        raise OSError("Query socket closed")

    query.close.side_effect = lambda: (events.append("close"), closed.set())
    query.shutdown.side_effect = lambda how: closed.set()
    query.__exit__.side_effect = lambda *args: query.close()
    if phase == "connect":
        query.connect_ex.return_value = errno.EINPROGRESS
        monkeypatch.setattr("mg400.joint_control.select.select", block)
    elif phase == "send":
        query.sendall.side_effect = block
    else:
        query.recv.side_effect = block
    stop_socket = Mock()
    stop_socket.__enter__ = Mock(return_value=stop_socket)
    stop_socket.__exit__ = Mock(return_value=False)
    stop_socket.recv.return_value = f"0,{{}},{stop_command};".encode("ascii")

    def connect(*args, **kwargs):
        assert "close" in events
        events.append("stop connect")
        return stop_socket

    monkeypatch.setattr(socket, "create_connection", connect)
    parent = JointControl("robot.test", Mock())
    child = JointControl("robot.test", Mock(), parent=parent, command_timeout=0.5)

    def read():
        try:
            parent._command("GetPose(0,0)")
        except OSError as error:
            failures.append(error)

    worker = threading.Thread(target=read)
    try:
        worker.start()
        assert entered.wait(1)
        assert child._command(stop_command, stopping=True) == ""
    finally:
        closed.set()
        worker.join(1)
    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], DashboardQueryCancelledError)
    assert parent._dashboard["stops"] == 0
    assert events.index("close") < events.index("stop connect")
    if phase == "connect":
        query.sendall.assert_not_called()


@pytest.mark.parametrize("phase", ["send", "recv"])
def test_uncancelled_query_oserror_is_not_mislabeled_as_stop_cancellation(monkeypatch, phase):
    connection = fake_connection(monkeypatch, [])
    error = OSError("Dashboard connection failed")
    getattr(connection, {"send": "sendall", "recv": "recv"}[phase]).side_effect = error
    control = JointControl("robot.test", Mock())
    with pytest.raises(OSError) as raised:
        control._command("GetPose(0,0)")
    assert raised.value is error
    assert not isinstance(raised.value, DashboardQueryCancelledError)
    connection.shutdown.assert_not_called()
    connection.__exit__.assert_called_once()


def test_query_connect_keeps_one_socket_until_original_deadline(monkeypatch):
    connection = fake_connection(monkeypatch, [b"0,{},GetPose(0,0);"])
    connection.connect_ex.return_value = errno.EINPROGRESS
    connection.getsockopt.return_value = 0
    now = [10.0]
    monkeypatch.setattr("mg400.joint_control.time.monotonic", lambda: now[0])

    def ready(read, write, error, timeout):
        now[0] += timeout
        return ([], [], []) if now[0] < 10.15 else ([], write, [])

    monkeypatch.setattr("mg400.joint_control.select.select", ready)
    assert JointControl("robot.test", Mock())._command("GetPose(0,0)") == ""
    assert now[0] >= 10.15
    socket.socket.assert_called_once()
    connection.connect_ex.assert_called_once()


def test_stop_does_not_interrupt_pending_dashboard_mutation_ack(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    connection = fake_connection(monkeypatch, [])
    sample = Feedback(START, 5, 0, False)
    control = JointControl("robot.test", lambda: (sample, time.monotonic()), command_timeout=0.05)
    pending = {
        "sent": False,
        "send_modes": {5},
        "expected": START,
        "done": threading.Event(),
        "ok": False,
    }
    control._pending = pending
    failures = []

    def reply(size):
        entered.set()
        assert release.wait(1)
        return b"0,{},EnableRobot();"

    def enable():
        try:
            control._command("EnableRobot()", pending=pending)
            pending["ok"] = True
        except Exception as error:
            failures.append(error)
        finally:
            pending["done"].set()

    connection.recv.side_effect = reply
    worker = threading.Thread(target=enable)
    try:
        worker.start()
        assert entered.wait(1)
        result = control.stop()
        assert not result["ok"]
        assert control._stop_uncertain
        connection.shutdown.assert_not_called()
        connection.close.assert_not_called()
        connection.sendall.assert_called_once_with(b"EnableRobot()")
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive() and not failures
    assert control._stop_uncertain


def test_stop_rejects_queries_resolving_before_or_during_stop(monkeypatch):
    resolving, release_dns = threading.Event(), threading.Event()
    stop_entered, release_stop = threading.Event(), threading.Event()
    failures = []
    connection = fake_connection(monkeypatch, [])
    control = JointControl("robot.test", Mock())

    def resolve(*args):
        resolving.set()
        assert release_dns.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("robot.test", 29999))]

    def stop_reply(size):
        stop_entered.set()
        assert release_stop.wait(1)
        return b"0,{},ResetRobot();"

    def read():
        try:
            control._command("GetPose(0,0)")
        except ConnectionAbortedError as error:
            failures.append(error)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    connection.recv.side_effect = stop_reply
    reader = threading.Thread(target=read)
    stopper = threading.Thread(target=lambda: control._command("ResetRobot()", stopping=True))
    try:
        reader.start()
        assert resolving.wait(1)
        stopper.start()
        assert stop_entered.wait(1)
        with pytest.raises(ConnectionAbortedError, match="cancelled for stop"):
            control._command("GetAngle()")
        release_stop.set()
        stopper.join(1)
        release_dns.set()
        reader.join(1)
    finally:
        release_stop.set()
        release_dns.set()
        reader.join(1)
        if stopper.ident is not None:
            stopper.join(1)
    assert len(failures) == 1
    socket.socket.assert_not_called()
    connection.sendall.assert_called_once_with(b"ResetRobot()")


def test_stop_cleanup_error_does_not_latch_query_gate(monkeypatch):
    connection = fake_connection(monkeypatch, [b"0,{},ResetRobot();", b"0,{},GetAngle();"])
    old = Mock()
    old.shutdown.side_effect = OSError("Already disconnected")
    old.close.side_effect = OSError("Close reported an error")
    control = JointControl("robot.test", Mock())
    control._dashboard["query"] = {"cancelled": threading.Event(), "connection": old}
    assert control._command("ResetRobot()", stopping=True) == ""
    assert control._dashboard["stops"] == 0
    assert control._command("GetAngle()") == ""
    assert connection.sendall.call_args_list == [call(b"ResetRobot()"), call(b"GetAngle()")]


def test_stop_interrupts_real_loopback_tcp_query(monkeypatch):
    # Only this local server is reachable. No robot connection is made.
    listener = REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(1)
    address = listener.getsockname()
    query_received, query_closed = threading.Event(), threading.Event()
    failures, commands = [], []
    monkeypatch.setattr(socket, "socket", REAL_SOCKET)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: REAL_GETADDRINFO(
            address[0],
            address[1],
            0,
            socket.SOCK_STREAM,
        ),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: REAL_CREATE_CONNECTION(
            address,
            timeout=kwargs["timeout"],
        ),
    )

    def server():
        try:
            with listener:
                with listener.accept()[0] as query:
                    query.settimeout(1)
                    commands.append(query.recv(4096))
                    query_received.set()
                    assert query.recv(4096) == b""
                query_closed.set()
                with listener.accept()[0] as stop:
                    stop.settimeout(1)
                    commands.append(stop.recv(4096))
                    stop.sendall(b"0,{},ResetRobot();")
        except Exception as error:
            failures.append(error)

    parent = JointControl("robot.test", Mock())
    child = JointControl("robot.test", Mock(), parent=parent, command_timeout=0.5)
    monkeypatch.setattr(child, "_wait_stable", Mock())

    def read():
        try:
            parent._command("GetPose(0,0)")
        except OSError:
            return
        failures.append(AssertionError("Query was not interrupted"))

    server_thread, query_thread = threading.Thread(target=server), threading.Thread(target=read)
    try:
        server_thread.start()
        query_thread.start()
        assert query_received.wait(1)
        assert child.stop()["ok"]
    finally:
        query_thread.join(2)
        server_thread.join(2)
        listener.close()
    assert not query_thread.is_alive() and not server_thread.is_alive()
    assert not failures
    assert query_closed.is_set()
    assert commands == [b"GetPose(0,0)", b"ResetRobot()"]


def test_dashboard_disconnect_releases_connection_for_stop(monkeypatch):
    connection = fake_connection(monkeypatch, [b"", b"0,{},ResetRobot();"])
    control = JointControl("robot.test", Mock())
    with pytest.raises(ConnectionError, match="disconnected"):
        control._command("GetPose(0,0)")
    assert control._command("ResetRobot()", stopping=True) == ""
    assert connection.__exit__.call_count == 2
    assert connection.sendall.call_args_list == [call(b"GetPose(0,0)"), call(b"ResetRobot()")]


def test_stop_dashboard_connection_does_not_wait_for_motion_ack(monkeypatch):
    motion_received, release_motion = threading.Event(), threading.Event()
    failures = []
    connections = []

    class Connection:
        def __init__(self, port):
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            self.command = data.decode("ascii")

        def recv(self, size):
            if self.port == 30003:
                motion_received.set()
                assert release_motion.wait(3)
            return f"0,{{}},{self.command};".encode("ascii")

    def connect(address, timeout):
        connections.append(address[1])
        return Connection(address[1])

    monkeypatch.setattr(socket, "create_connection", connect)
    control = JointControl("robot.test", lambda: (Feedback(START, 5, 0, False), time.monotonic()))
    pending = {"sent": False, "send_modes": {5}, "expected": START}

    def move():
        try:
            control._command(MOVE, 30003, pending=pending)
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=move)
    try:
        worker.start()
        assert motion_received.wait(1)
        assert control._command("ResetRobot()", stopping=True) == ""
        assert worker.is_alive()
        assert connections == [30003, 29999]
    finally:
        release_motion.set()
        worker.join(1)
    assert not failures
    assert not worker.is_alive()


@pytest.mark.parametrize("reply", [b"0,{},Other();", b"-1,{},EnableRobot();", b"invalid;"])
def test_transport_rejects_bad_ack_or_wrong_command_echo(monkeypatch, reply):
    connection = fake_connection(monkeypatch, [reply])
    control = JointControl("robot.test", Mock())
    with pytest.raises(RuntimeError):
        control._command("EnableRobot()")
    connection.sendall.assert_called_once_with(b"EnableRobot()")


@pytest.mark.parametrize(
    "parts,failure",
    [([b"0,{", b""], ConnectionError), ([TimeoutError("ACK lost")], TimeoutError)],
)
def test_transport_never_retries_lost_motion_ack(monkeypatch, parts, failure):
    connection = fake_connection(monkeypatch, parts)
    control = JointControl("robot.test", Mock())
    pending = {"sent": False, "send_modes": {5}, "expected": START}
    control.feedback_snapshot = lambda: (Feedback(START, 5, 0, False), time.monotonic())
    with pytest.raises(failure):
        control._command(MOVE, 30003, pending=pending)
    assert pending["sent"]
    connection.sendall.assert_called_once_with(MOVE.encode("ascii"))


@pytest.mark.parametrize("disabling", [False, True])
def test_cancelled_delayed_mutation_never_sends(monkeypatch, disabling):
    connection = fake_connection(monkeypatch, [])
    control = JointControl("robot.test", Mock())
    control.cancel(disabling=disabling)
    pending = {"sent": False}
    with pytest.raises(RuntimeError, match="stopped before"):
        control._command(MOVE, 30003, pending=pending)
    assert not pending["sent"]
    connection.sendall.assert_not_called()


@pytest.mark.parametrize("changed_mode,changed_joints", [(7, START), (5, TARGET)])
def test_send_rechecks_idle_state_and_start_pose_under_cancellation_lock(
    monkeypatch, changed_mode, changed_joints
):
    connection = fake_connection(monkeypatch, [])
    control = JointControl(
        "robot.test",
        lambda: (Feedback(changed_joints, changed_mode, 0, False), time.monotonic()),
    )
    pending = {"sent": False, "send_modes": {5}, "expected": START}
    with pytest.raises(RuntimeError):
        control._command(MOVE, 30003, pending=pending)
    assert not pending["sent"]
    connection.sendall.assert_not_called()


def test_stop_transport_failure_is_not_retried(rig, monkeypatch):
    control, _, _, _ = rig
    command = Mock(side_effect=TimeoutError("Reset ACK lost"))
    monkeypatch.setattr(control, "_command", command)
    assert not control.stop()["ok"]
    assert not control.stop()["stationary_verified"]
    assert command.call_args_list == [call("ResetRobot()", stopping=True)]


def test_linear_move_cannot_start_outside_manufacturer_j4_range(cartesian_rig):
    control, sample, _, commands, _ = cartesian_rig
    sample[0] = Feedback((*START[:3], -361.0), 5, 0, False)
    with pytest.raises(ValueError, match="J4"):
        control.move_linear(LINEAR_TARGET)
    assert not commands
    assert not control.stopped


def test_linear_move_rechecks_start_after_preflight(cartesian_rig, monkeypatch):
    control, _, _, commands, _ = cartesian_rig
    monkeypatch.setattr(control, "_preflight", lambda modes: (5, (*START[:3], -361.0)))
    with pytest.raises(ValueError, match="J4"):
        control.move_linear(LINEAR_TARGET)
    assert not any(text.startswith(("InverseSolution", "MovL")) for text, _ in commands)
    assert ("ResetRobot()", 29999) in commands


def test_preflight_returns_latest_verified_passive_baseline(rig, monkeypatch):
    control, _, now, commands = rig
    dashboard = (*START[:3], START[3] - 0.06)
    latest = (*START[:3], START[3] + 0.02)
    monkeypatch.setattr(control, "_dashboard_state", lambda: (5, dashboard))

    def feedback():
        joints = latest if now[0] >= 10.2 else START
        return Feedback(joints, 5, 0, False), now[0]

    control.feedback_snapshot = feedback
    assert control._preflight({5}) == (5, latest)
    assert now[0] >= 10.4
    assert not commands
    assert not control.stopped


@pytest.mark.parametrize("initial_mode", [4, 5])
def test_enable_accepts_a_new_settled_position_only_while_disabled(rig, monkeypatch, initial_mode):
    control, sample, now, commands = rig
    sample[0] = Feedback(START, initial_mode, 0, False)
    settled = (*START[:3], START[3] + 0.2)

    def feedback():
        joints = START if now[0] < 10.1 else settled
        sample[0] = Feedback(joints, sample[0].mode, 0, False)
        return sample[0], now[0]

    control.feedback_snapshot = feedback
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "EnableRobot()":
            assert now[0] >= 10.5
            assert kwargs["pending"]["expected"] == settled
        return original_command(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    if initial_mode == 4:
        enabled = control.enable()
        assert enabled.mode == 5
        assert enabled.joints_deg == settled
        assert not control.stopped
        assert [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))] == [
            "EnableRobot()",
        ]
        assert [text for text, _ in commands].count("RobotMode()") == 3
    else:
        with pytest.raises(TimeoutError, match="stationary state"):
            control.enable()
        assert control.stopped
        assert ("EnableRobot()", 29999) not in commands
        assert control.stop()["stationary_verified"]


@pytest.mark.parametrize(
    "fault",
    [
        "dashboard_mode",
        "passive_mode",
        "alarm",
        "stale",
        "dashboard_angles",
        "passive_angles",
        "both_angles",
        "source_difference",
        "cancelled",
    ],
)
def test_disabled_preflight_rechecks_settled_state_before_enable(rig, monkeypatch, fault):
    control, sample, now, commands = rig
    sample[0] = Feedback(START, 4, 0, False)
    original_command = control._command
    mode_reads = 0
    changed = (*START[:3], START[3] + 0.11)

    def command(text, *args, **kwargs):
        nonlocal mode_reads
        result = original_command(text, *args, **kwargs)
        if text == "RobotMode()":
            mode_reads += 1
            if mode_reads == 2:
                if fault == "dashboard_mode":
                    return "5"
                if fault == "passive_mode":
                    sample[0] = Feedback(START, 5, 0, False)
        if mode_reads == 2:
            if text == "GetErrorID()" and fault == "alarm":
                return "[[1],[],[],[],[]]"
            if text == "GetAngle()":
                if fault == "source_difference":
                    sample[0] = Feedback((*START[:3], START[3] - 0.09), 4, 0, False)
                    return ",".join(map(str, (*START[:3], START[3] + 0.09, 0, 0)))
                if fault == "stale":
                    control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
                if fault in {"passive_angles", "both_angles"}:
                    sample[0] = Feedback(changed, 4, 0, False)
                if fault in {"dashboard_angles", "both_angles"}:
                    return ",".join(map(str, (*changed, 0, 0)))
                if fault == "cancelled":
                    control.cancel()
        return result

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(PreparationRejectedError if fault == "both_angles" else RuntimeError):
        control.enable()
    assert mode_reads == 2
    assert ("EnableRobot()", 29999) not in commands
    if fault == "both_angles":
        assert not control.stopped
        assert ("ResetRobot()", 29999) not in commands
        assert control.enable().mode == 5
    else:
        assert control.stopped


def test_disabled_preflight_still_rejects_initial_source_mismatch(rig, monkeypatch):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, 4, 0, False)
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "GetAngle()":
            return ",".join(map(str, (*START[:3], START[3] + 0.2, 0, 0)))
        return original_command(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError, match="Dashboard and passive feedback disagree"):
        control.enable()
    assert [text for text, _ in commands].count("RobotMode()") == 1
    assert ("EnableRobot()", 29999) not in commands
    assert control.stopped


def test_move_rejects_excessive_source_difference_with_readings(rig, monkeypatch):
    control, sample, _, commands = rig
    passive = sample[0].joints_deg
    dashboard = (*passive[:3], passive[3] - 0.11)
    monkeypatch.setattr(control, "_dashboard_state", lambda: (5, dashboard))
    with pytest.raises(RuntimeError, match="Dashboard and passive feedback disagree") as error:
        control.move(TARGET)
    delta = tuple(actual - reference for actual, reference in zip(passive, dashboard))
    diagnostic = str(error.value)
    assert "dashboard_mode=5, passive_mode=5" in diagnostic
    assert f"dashboard={dashboard!r}" in diagnostic
    assert f"passive={passive!r}" in diagnostic
    assert f"passive_minus_dashboard={delta!r}" in diagnostic
    assert "tolerance_deg=0.1" in diagnostic
    assert control._pending is None
    assert control.stopped
    assert commands == [("ResetRobot()", 29999)]
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("failure_type", [TimeoutError, ConnectionError])
@pytest.mark.parametrize(
    "method,query",
    [
        ("enable", "RobotMode("),
        ("move", "GetAngle("),
        ("move_linear", "GetPose("),
        ("move_linear", "InverseSolution("),
    ],
)
def test_read_only_preparation_query_failure_can_retry_same_control(
    cartesian_rig, monkeypatch, method, query, failure_type
):
    control, sample, _, commands, _ = cartesian_rig
    if method == "enable":
        sample[0] = Feedback(START, 4, 0, False)
    original = control._command
    failed = False

    def command(text, *args, **kwargs):
        nonlocal failed
        if text.startswith(query) and not failed:
            failed = True
            raise failure_type("Read-only connection failed")
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    args = () if method == "enable" else (TARGET if method == "move" else LINEAR_TARGET,)
    with pytest.raises(PreparationRejectedError, match="Retry the action"):
        getattr(control, method)(*args)
    assert not any(
        text.startswith(("Enable", "JointMovJ", "MovL", "Reset")) for text, _ in commands
    )
    assert control._pending is None
    assert not control.stopped and not control.busy and not control._stop_uncertain
    assert getattr(control, method)(*args).mode == 5
    assert not control.stopped and not control.busy


def test_query_failure_after_prior_successful_move_can_retry(cartesian_rig, monkeypatch):
    control, _, _, commands, _ = cartesian_rig
    control.move(TARGET)
    pending = control._pending
    commands.clear()
    original = control._command

    def command(text, *args, **kwargs):
        if text == "GetAngle()":
            raise ConnectionError("Read unavailable")
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(PreparationRejectedError):
        control.move(START)
    assert control._pending is pending
    assert not control.stopped and not control._stop_uncertain
    assert ("ResetRobot()", 29999) not in commands
    monkeypatch.setattr(control, "_command", original)
    assert control.move(START).joints_deg == START


@pytest.mark.parametrize("change", ["alarm", "moving", "disabled", "stale", "joints"])
def test_preparation_query_failure_with_changed_feedback_still_latches(rig, monkeypatch, change):
    control, sample, now, commands = rig
    original = control._command

    def command(text, *args, **kwargs):
        if text == "GetAngle()":
            if change == "stale":
                control.feedback_snapshot = lambda: (sample[0], now[0] - 0.3)
            else:
                sample[0] = Feedback(
                    (START[0] + 0.2, *START[1:]) if change == "joints" else START,
                    7 if change == "moving" else 4 if change == "disabled" else 5,
                    0,
                    change == "alarm",
                )
            raise TimeoutError("Query lost")
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError) as caught:
        control.move(TARGET)
    assert not isinstance(caught.value, PreparationRejectedError)
    assert control.stopped and not control.busy
    assert ("ResetRobot()", 29999) in commands
    assert not any(text.startswith("JointMovJ") for text, _ in commands)


def test_post_send_query_timeout_still_latches_control(rig, monkeypatch):
    control, _, _, commands = rig
    original = control._command

    def command(text, *args, **kwargs):
        if text == "GetAngle()" and control._pending is not None:
            raise TimeoutError("Arrival query lost")
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(TimeoutError, match="Arrival query"):
        control.move(TARGET)
    assert control.stopped and not control.busy
    assert (MOVE, 30003) in commands
    assert ("ResetRobot()", 29999) in commands


@pytest.mark.parametrize("dashboard_mode", [4, 7, 9])
def test_dashboard_state_change_cannot_be_hidden_by_later_query_timeout(
    rig, monkeypatch, dashboard_mode
):
    control, _, _, commands = rig
    original = control._command

    def command(text, *args, **kwargs):
        if text == "RobotMode()":
            return str(dashboard_mode)
        if text == "GetErrorID()":
            raise TimeoutError("Alarm query unavailable")
        return original(text, *args, **kwargs)

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises(RuntimeError, match="Dashboard and passive feedback disagree") as caught:
        control.move(TARGET)
    assert not isinstance(caught.value, PreparationRejectedError)
    assert control.stopped and not control.busy
    assert commands == [("ResetRobot()", 29999)]


@pytest.mark.parametrize("j4", [-359, 359])
def test_linear_move_accepts_measured_start_outside_target_margin(cartesian_rig, j4):
    control, sample, _, commands, _ = cartesian_rig
    sample[0] = Feedback((*START[:3], j4), 5, 0, False)
    assert control.move_linear(LINEAR_TARGET).joints_deg == TARGET
    assert (LINEAR_MOVE, 30003) in commands
    assert not control.stopped


class _WireConnection:
    """Answer one fake controller socket and record each command."""

    def __init__(self, wire):
        self._wire = wire
        self._reply = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, timeout):
        pass

    def setblocking(self, flag):
        pass

    def connect_ex(self, address):
        return 0

    def shutdown(self, how):
        pass

    def close(self):
        pass

    def sendall(self, data):
        text = data.decode("ascii")
        self._wire.sent.append(text)
        self._wire.on_send(text)
        self._reply = f"0,{{{self._wire.answer(text)}}},{text};".encode("ascii")

    def recv(self, size):
        reply, self._reply = self._reply, b""
        return reply


class _Wire:
    """Fake controller behind the real JointControl transport, with a fake clock."""

    def __init__(self):
        self.now = 10.0
        self.sample = Feedback(START, 5, 0, False, 1)
        self.sent = []
        self.on_connect = lambda port: None
        self.on_send = lambda text: None

    def feedback(self):
        return self.sample, self.now

    def sleep(self, duration):
        self.now += duration

    def answer(self, text):
        if text == "RobotMode()":
            return str(self.sample.mode)
        if text == "GetErrorID()":
            return "[[],[],[],[],[]]"
        if text == "GetAngle()":
            return ",".join(map(str, (*self.sample.joints_deg, 0, 0)))
        if text == "GetPose(0,0)":
            return ",".join(map(str, POSE))
        if text.startswith("SpeedFactor("):
            speed = int(text[len("SpeedFactor(") : -1])
            self.sample = replace(self.sample, speed_scaling_raw=speed)
            return ""
        if text.startswith(("MoveJog(", "JointMovJ(")):
            return ""
        if text == "ResetRobot()":
            self.sample = replace(self.sample, mode=5)
            return ""
        if text == "DisableRobot()":
            self.sample = replace(self.sample, mode=4)
            return ""
        raise AssertionError(f"Unexpected command: {text}")


@pytest.fixture
def wire(monkeypatch):
    robot = _Wire()
    monkeypatch.setattr("mg400.joint_control.time.monotonic", lambda: robot.now)
    monkeypatch.setattr("mg400.joint_control.time.sleep", robot.sleep)

    def create_connection(address, timeout=None, **kwargs):
        # Only mutations and stop commands open connections this way.
        robot.on_connect(address[1])
        return _WireConnection(robot)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: _WireConnection(robot))
    return robot


# A dashboard mutation, the reused jog connection, and a motion command on its own connection.
GUARDED_MUTATIONS = [
    ("SpeedFactor(50)", 29999, {5}),
    ("MoveJog(J4+)", 30003, {5, 11}),
    (MOVE, 30003, {5, 7}),
]


@pytest.mark.parametrize("text,port,modes", GUARDED_MUTATIONS)
@pytest.mark.parametrize("child_guarded", [True, False])
def test_child_mutation_checks_own_guard_then_parent_guard_at_enqueue_and_send(
    wire, text, port, modes, child_guarded
):
    calls = []

    def guard(name):
        def check():
            pending = child._pending
            calls.append(
                (
                    name,
                    pending is not None,
                    pending is not None and pending["sent"],
                    child._send_lock.locked(),
                )
            )

        return check

    parent = JointControl("robot.test", wire.feedback, send_guard=guard("parent"))
    child = parent.begin_operation(send_guard=guard("child") if child_guarded else None)
    wire.on_send = lambda sent: calls.append(("send", sent))
    try:
        child._guarded_command(text, modes=modes, send_modes={5}, expected=START, port=port)
    finally:
        child.finish_operation()
    names = ["child", "parent"] if child_guarded else ["parent"]
    # Enqueue: under the send lock, before the command is registered.
    # Send time: under the send lock, while the registered command is still unsent.
    assert calls == [
        *[(name, False, False, True) for name in names],
        *[(name, True, False, True) for name in names],
        ("send", text),
    ]
    assert wire.sent == [text]
    assert not parent.stopped


@pytest.mark.parametrize("state", ["ready", "stopped", "stale"])
def test_send_time_guards_run_before_stop_and_feedback_checks(wire, state):
    events = []
    refusal = RuntimeError("The browser control lease expired")

    def parent_guard():
        events.append("parent")
        if state != "ready":
            raise refusal

    def feedback():
        events.append("fresh")
        return wire.sample, wire.now - (1.0 if state == "stale" else 0.0)

    parent = JointControl("robot.test", feedback, send_guard=parent_guard)
    child = JointControl(
        "robot.test",
        feedback,
        parent=parent,
        command_timeout=0.5,
        send_guard=lambda: events.append("child"),
    )
    if state == "stopped":
        child.cancel()
    wire.on_send = events.append
    pending = {"sent": False, "send_modes": {5}, "expected": START}
    if state == "ready":
        assert child._command("SpeedFactor(50)", pending=pending) == ""
        assert events == ["child", "parent", "fresh", "SpeedFactor(50)"]
        assert pending["sent"]
        return
    with pytest.raises(RuntimeError) as raised:
        child._command("SpeedFactor(50)", pending=pending)
    assert raised.value is refusal
    assert events == ["child", "parent"]
    assert not pending["sent"]
    assert not wire.sent


def test_send_guards_cover_every_ancestor_and_skip_controls_without_a_guard(wire):
    events = []
    root = JointControl("robot.test", wire.feedback, send_guard=lambda: events.append("root"))
    middle = JointControl("robot.test", wire.feedback, parent=root, command_timeout=0.5)
    leaf = JointControl(
        "robot.test",
        wire.feedback,
        parent=middle,
        command_timeout=0.5,
        send_guard=lambda: events.append("leaf"),
    )
    wire.on_send = events.append
    pending = {"sent": False, "send_modes": {5}, "expected": START}
    assert leaf._command("SpeedFactor(10)", pending=pending) == ""
    assert events == ["leaf", "root", "SpeedFactor(10)"]


@pytest.mark.parametrize("text,port,modes", GUARDED_MUTATIONS)
@pytest.mark.parametrize("when", ["enqueue", "send"])
def test_parent_guard_refusal_leaves_child_command_unsent_and_parent_recoverable(
    wire, text, port, modes, when
):
    refused = threading.Event()
    checks = []

    def parent_guard():
        checks.append(refused.is_set())
        if refused.is_set():
            raise RuntimeError("The browser control lease expired")

    if when == "enqueue":
        refused.set()
    else:
        # The lease expires after the enqueue check, while the command connects.
        wire.on_connect = lambda port: refused.set()
    parent = JointControl("robot.test", wire.feedback, send_guard=parent_guard)
    child = parent.begin_operation()
    try:
        with pytest.raises(RuntimeError, match="lease expired"):
            child._guarded_command(text, modes=modes, send_modes={5}, expected=START, port=port)
        if when == "enqueue":
            assert checks == [True]
            assert child._pending is None
        else:
            assert checks == [False, True]
            assert child._pending["sent"] is False
        assert not wire.sent
        assert not child._stop_uncertain and not parent._stop_uncertain
        assert not parent.stopped
        assert child.stop_after_command(jog=text.startswith("MoveJog("))["ok"]
        assert not child._stop_uncertain and not parent._stop_uncertain
    finally:
        child.finish_operation()
    assert not parent.stopped
    assert parent.feedback_retention_reason() is None
    # The panel then stops and disables the parent. Neither may report unknown ordering.
    assert parent.stop()["ok"]
    assert parent.feedback_retention_reason() is None
    assert parent.disable().mode == 4
    assert parent.feedback_retention_reason() is None
    assert not parent._stop_uncertain
    assert text not in wire.sent
    assert wire.sent[:3] == ["ResetRobot()", "ResetRobot()", "DisableRobot()"]


@pytest.mark.parametrize("when", ["enqueue", "send"])
def test_parent_preparation_rejection_leaves_real_keyboard_session_unfaulted(wire, when):
    cancelled = threading.Event()

    def parent_guard():
        if cancelled.is_set():
            raise PreparationRejectedError("Keyboard preparation cancelled.")

    if when == "enqueue":
        # The arming request is withdrawn during the read-only preflight.
        wire.on_send = lambda text: cancelled.set() if text == "GetPose(0,0)" else None
    else:
        # It is withdrawn after the enqueue check, while SpeedFactor connects.
        wire.on_connect = lambda port: cancelled.set()
    parent = JointControl("robot.test", wire.feedback, send_guard=parent_guard)
    session = KeyboardJogSession(parent, speed_percent=50)
    with pytest.raises(PreparationRejectedError, match="Keyboard preparation cancelled"):
        session.prepare()
    assert session.fault is None
    assert not parent.stopped
    assert not session.ready and not parent.busy
    assert wire.sent == ["RobotMode()", "GetErrorID()", "GetAngle()", "GetPose(0,0)"]
    if when == "enqueue":
        assert parent._pending is None
    else:
        assert parent._pending["sent"] is False
    assert not parent._stop_uncertain
    assert parent.feedback_retention_reason() is None
    # The parent stays usable for the next arming request.
    cancelled.clear()
    wire.on_send = wire.on_connect = lambda value: None
    retry = KeyboardJogSession(parent, speed_percent=50)
    try:
        assert retry.prepare() == POSE
        assert retry.ready
    finally:
        retry.close()
    assert wire.sent[-1] == "SpeedFactor(50)"
    assert not parent.stopped


def test_child_guard_refusal_without_parent_guard_sends_nothing(wire):
    # The parent has no send guard. The child guard keeps its errors.
    parent = JointControl("robot.test", wire.feedback)
    refusal = RuntimeError("Keyboard hold cancelled")
    refusing = Mock(side_effect=refusal)
    child = parent.begin_operation(send_guard=refusing)
    try:
        with pytest.raises(RuntimeError) as raised:
            child._guarded_command(
                "MoveJog(J4-)", modes={5, 11}, send_modes={5}, expected=START, port=30003
            )
        assert raised.value is refusal
        refusing.assert_called_once_with()
        assert child._pending is None
    finally:
        child.finish_operation()
    assert not wire.sent
    assert not parent.stopped
    assert parent.feedback_retention_reason() is None


@pytest.mark.parametrize("remains", [True, False])
def test_clear_alarms_sends_clear_error_only_and_reads_the_remaining_ids(rig, monkeypatch, remains):
    control, sample, _, _ = rig
    sample[0] = Feedback(START, 9, 0, True)
    # The panel clears on the stopped instance whose disable the alarm refused.
    control.cancel(disabling=True)
    original = control._command
    calls = []

    def command(text, port=29999, *, pending=None, stopping=False):
        calls.append((text, stopping))
        if text == "ClearError()":
            sample[0] = Feedback(START, 9, 0, True) if remains else Feedback(START, 5, 0, False)
            return ""
        if text == "GetErrorID()":
            return "[[72],[],[],[],[],[]]" if remains else "[[],[],[],[],[],[]]"
        return original(text, port, pending=pending, stopping=stopping)

    monkeypatch.setattr(control, "_command", command)
    expected = ((72,) if remains else (), (), (), (), (), ())
    assert control.clear_alarms() == expected
    assert calls == [("ClearError()", True), ("GetErrorID()", False)]
