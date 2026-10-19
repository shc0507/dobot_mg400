import socket
import threading
import time
from unittest.mock import Mock, call

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl

START = (0.7, 0.04, 0.52, 23.08)
TARGET = (*START[:3], START[3] + 2)
MOVE = "JointMovJ(0.700000,0.040000,0.520000,25.080000,SpeedJ=5,AccJ=5)"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection", Mock(side_effect=AssertionError("Network prohibited"))
    )


@pytest.fixture
def rig(monkeypatch):
    now = [10.0]
    sample = [Feedback(START, 5, 0, False)]
    monkeypatch.setattr("mg400_agent.joint_control.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "mg400_agent.joint_control.time.sleep",
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


def test_construction_and_read_only_state_never_open_connections(rig):
    control, _, _, commands = rig
    assert not control.busy
    assert not control.stopped
    assert control.read_state().joints_deg == START
    assert not commands
    socket.create_connection.assert_not_called()


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


@pytest.mark.parametrize("target", [(0, 0, 0), (0, 0, 0, float("nan")), (0, 0, 0, True)])
def test_malformed_target_does_not_read_or_send(rig, target):
    control, _, _, commands = rig
    with pytest.raises(ValueError):
        control.move(target)
    assert not commands
    assert not control.stopped


def test_move_does_not_implicitly_enable_or_stop_disabled_robot(rig):
    control, sample, _, commands = rig
    sample[0] = Feedback(START, 4, 0, False)
    with pytest.raises(RuntimeError, match="enabled and idle"):
        control.move(TARGET)
    assert not commands
    assert not control.stopped


@pytest.mark.parametrize("target", [(*START[:3], START[3] + 90), (0, 0, 0, 0)])
def test_moves_over_five_degrees_keep_validation_and_low_speed(rig, target):
    control, _, _, commands = rig
    assert control.move(target).joints_deg == target
    moves = [(text, port) for text, port in commands if text.startswith("JointMovJ(")]
    values = ",".join(f"{value:.6f}" for value in target)
    assert moves == [(f"JointMovJ({values},SpeedJ=5,AccJ=5)", 30003)]
    assert not control.stopped


@pytest.mark.parametrize("target", [(160, 0, 0, 0), (0, 85, 50, 0), (0, 0, 60, 0)])
def test_out_of_range_targets_are_rejected_without_motion(rig, target):
    control, _, _, commands = rig
    with pytest.raises(ValueError, match="reference range"):
        control.move(target)
    assert not commands
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


def test_complete_joint_segment_is_validated_before_motion(rig):
    control, sample, _, commands = rig
    sample[0] = Feedback((0, -20, 1, 0), 5, 0, False)
    # Both endpoints satisfy their branch; crossing J3=0 violates the stricter J2 minimum.
    with pytest.raises(ValueError, match="J2"):
        control.move((0, -17, -1, 0))
    assert not commands
    assert not control.stopped


def test_busy_operation_rejects_another_without_commands(rig):
    control, _, _, commands = rig
    control._operation_lock.acquire()
    try:
        for action in (control.enable, lambda: control.move(TARGET)):
            with pytest.raises(RuntimeError, match="in progress"):
                action()
        assert control.busy
        assert not commands
    finally:
        control._operation_lock.release()


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
    assert now[0] >= 10.4
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
    for action in (control.enable, lambda: control.move(TARGET)):
        with pytest.raises(RuntimeError, match="stopped"):
            action()
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == ["DisableRobot()", "EnableRobot()", MOVE]


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


@pytest.mark.parametrize("failure", ["ack", "enabled", "dashboard", "feedback"])
def test_unconfirmed_disable_stays_cancelled_and_uses_failure_stop_once(rig, monkeypatch, failure):
    control, sample, now, commands = rig
    original_command = control._command

    def command(text, *args, **kwargs):
        if text == "DisableRobot()" and failure == "ack":
            commands.append((text, 29999))
            raise TimeoutError("Disable acknowledgement was lost")
        result = original_command(text, *args, **kwargs)
        if text == "DisableRobot()":
            if failure == "enabled":
                sample[0] = Feedback(START, 5, 0, False)
            elif failure == "feedback":
                control.feedback_snapshot = lambda: (sample[0], now[0] - 1)
        if text == "RobotMode()" and failure == "dashboard":
            return "5"
        return result

    monkeypatch.setattr(control, "_command", command)
    with pytest.raises((RuntimeError, TimeoutError)) as first:
        control.disable()
    with pytest.raises(type(first.value)) as second:
        control.disable()
    assert first.value is second.value
    assert control.stopped
    mutations = [text for text, _ in commands if not text.startswith(("Get", "RobotMode"))]
    assert mutations == ["DisableRobot()", "ResetRobot()"]


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


def test_cancel_preserves_unresolved_ack_even_if_ack_arrives_before_stop_worker(rig):
    control, _, _, commands = rig
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    control._pending = pending
    control.cancel()
    pending["ok"] = True
    pending["done"].set()
    assert not control.stop()["ok"]
    assert commands == [("ResetRobot()", 29999)]


@pytest.mark.parametrize("kind", ["stale", "future", "alarm", "mode"])
def test_read_rejects_stale_or_faulted_feedback_without_commands(rig, kind):
    control, sample, now, commands = rig
    age = 0.26 if kind == "stale" else -0.1 if kind == "future" else 0
    sample[0] = Feedback(START, 9 if kind == "mode" else 5, 0, kind == "alarm")
    control.feedback_snapshot = lambda: (sample[0], now[0] - age)
    with pytest.raises(RuntimeError):
        control.read_state()
    assert not commands


def test_disagreeing_dashboard_prevents_movement(rig, monkeypatch):
    control, _, _, commands = rig
    monkeypatch.setattr(control, "_dashboard_state", lambda: (5, (*START[:3], START[3] + 0.2)))
    with pytest.raises(RuntimeError, match="disagree"):
        control.move(TARGET)
    assert commands == [("ResetRobot()", 29999)]


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
    with pytest.raises(TimeoutError):
        control._wait_stable(START, modes={5}, final_modes={5}, timeout=0.5)


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
    finally:
        release.set()


def test_late_mutation_ack_cannot_clear_unknown_stop_ordering(rig, monkeypatch):
    control, _, _, _ = rig
    pending = {"done": threading.Event(), "sent": True, "ok": False}
    control._pending = pending

    def command(*args, **kwargs):
        pending["ok"] = True
        pending["done"].set()
        return ""

    monkeypatch.setattr(control, "_command", command)
    stable = Mock()
    monkeypatch.setattr(control, "_wait_stable", stable)
    result = control.stop()
    assert not result["ok"]
    assert not result["stationary_verified"]
    assert "unknown" in result["error"]
    assert control.stop() is result
    stable.assert_not_called()


def socket_for(monkeypatch, parts):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = parts
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=connection))
    return connection


def test_transport_reassembles_reply_without_binding_host_source_ip(monkeypatch):
    connection = socket_for(monkeypatch, [b"0,{1,2,", b"3,4,0,0},GetAngle();"])
    control = JointControl("robot.test", Mock())
    assert control._command("GetAngle()") == "1,2,3,4,0,0"
    socket.create_connection.assert_called_once_with(("robot.test", 29999), timeout=2.0)
    connection.sendall.assert_called_once_with(b"GetAngle()")


@pytest.mark.parametrize("reply", [b"0,{},Other();", b"-1,{},EnableRobot();", b"invalid;"])
def test_transport_rejects_bad_ack_or_wrong_command_echo(monkeypatch, reply):
    connection = socket_for(monkeypatch, [reply])
    control = JointControl("robot.test", Mock())
    with pytest.raises(RuntimeError):
        control._command("EnableRobot()")
    connection.sendall.assert_called_once_with(b"EnableRobot()")


@pytest.mark.parametrize(
    "parts,failure",
    [([b"0,{", b""], ConnectionError), ([TimeoutError("ACK lost")], TimeoutError)],
)
def test_transport_never_retries_lost_motion_ack(monkeypatch, parts, failure):
    connection = socket_for(monkeypatch, parts)
    control = JointControl("robot.test", Mock())
    pending = {"sent": False, "send_modes": {5}, "expected": START}
    control.feedback_snapshot = lambda: (Feedback(START, 5, 0, False), time.monotonic())
    with pytest.raises(failure):
        control._command(MOVE, 30003, pending=pending)
    assert pending["sent"]
    connection.sendall.assert_called_once_with(MOVE.encode("ascii"))


@pytest.mark.parametrize("disabling", [False, True])
def test_cancelled_delayed_mutation_never_sends(monkeypatch, disabling):
    connection = socket_for(monkeypatch, [])
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
    connection = socket_for(monkeypatch, [])
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
