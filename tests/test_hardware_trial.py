import socket
import threading
from unittest.mock import Mock

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.hardware_trial import JointTrial

START = (0.7, 0.04, 0.52, 23.08)
OUT = (*START[:3], START[3] + 2)


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection", Mock(side_effect=AssertionError("Network prohibited"))
    )


@pytest.fixture
def trial(monkeypatch):
    result = JointTrial("robot.test", "source.test")
    result.start = START
    result.initial_mode = 4
    result.phase = "ready"
    result._latest = (1, 10.0, Feedback(START, 4, 0, False))
    monkeypatch.setattr("mg400_agent.hardware_trial.time.monotonic", lambda: 10.0)
    return result


def test_construction_and_invalid_tools_never_contact_robot():
    trial = JointTrial("robot.test")
    for name, arguments in [
        ("move_joints", {"joints_deg": [0, 0, 0, 0]}),
        ("rotate_j4_out", {"angle_deg": 5}),
        ("rotate_j4_out", None),
        ([], {}),
    ]:
        assert trial.execute(name, arguments)["ok"] is False
    assert trial.stop() == {
        "ok": True,
        "session_stopped": True,
        "physical_stop_requested": False,
    }
    socket.create_connection.assert_not_called()


def test_read_state_and_invalid_phase_have_no_writes(trial, monkeypatch):
    command = Mock(side_effect=AssertionError("No commands expected"))
    monkeypatch.setattr(trial, "_command", command)
    assert trial.execute("get_robot_state", {})["state"]["joints_deg"] == START
    assert trial.execute("return_to_start", {})["ok"] is False
    command.assert_not_called()


@pytest.mark.parametrize("initial_mode", [4, 5])
def test_exact_two_step_sequence_preserves_other_joints_and_restores_enable_state(
    trial,
    monkeypatch,
    initial_mode,
):
    trial.initial_mode = initial_mode
    current = [START, initial_mode]
    commands = []

    def guarded(text, *, port=29999, allowed_modes):
        commands.append((text, port))
        trial._owned = trial._uncertain = True
        if text == "EnableRobot()":
            current[1] = 5
        elif text == "DisableRobot()":
            current[1] = 4
        elif text.startswith("JointMovJ("):
            current[0] = (
                OUT if len([c for c, _ in commands if c.startswith("JointMovJ")]) == 1 else START
            )

    monkeypatch.setattr(trial, "_guarded_command", guarded)
    monkeypatch.setattr(trial, "_preflight", lambda expected: current[1])
    monkeypatch.setattr(trial, "_wait_stable", Mock())
    monkeypatch.setattr(trial, "_dashboard_state", lambda: (current[1], current[0]))
    monkeypatch.setattr(
        trial, "_fresh", lambda *args, **kwargs: (1, 10, Feedback(current[0], current[1], 0, False))
    )
    assert trial.execute("rotate_j4_out", {})["state"]["phase"] == "out"
    assert trial.execute("rotate_j4_out", {})["ok"] is False
    result = trial.execute("return_to_start", {})
    assert result["state"]["phase"] == "complete"
    assert result["state"]["verified_joints_deg"] == {
        "rotate_j4_out": OUT,
        "return_to_start": START,
    }
    assert trial.execute("return_to_start", {})["ok"] is False
    expected = [
        ("JointMovJ(0.700000,0.040000,0.520000,25.080000,SpeedJ=5,AccJ=5)", 30003),
        ("JointMovJ(0.700000,0.040000,0.520000,23.080000,SpeedJ=5,AccJ=5)", 30003),
    ]
    if initial_mode == 4:
        expected = [("EnableRobot()", 29999), *expected, ("DisableRobot()", 29999)]
    assert commands == expected
    assert not trial._owned


def test_verified_return_is_retained_when_disabled_settling_fails(trial, monkeypatch):
    trial.phase = "out"
    trial._owned = True
    trial.verified_joints_deg["rotate_j4_out"] = OUT
    returned_actual = (*START[:3], START[3] + 0.016)
    move = Mock(return_value=returned_actual)
    monkeypatch.setattr(trial, "_move", move)

    def disable(text, *, allowed_modes):
        assert text == "DisableRobot()"
        assert allowed_modes == {4, 5}
        assert trial.phase == "restoring_disabled"

    monkeypatch.setattr(trial, "_guarded_command", disable)
    monkeypatch.setattr(
        trial, "_wait_stable", Mock(side_effect=TimeoutError("Disabled pose outside tolerance"))
    )
    monkeypatch.setattr(trial, "stop", Mock(return_value={"ok": True, "stationary_verified": True}))
    state = Mock(side_effect=AssertionError("Failure diagnostics must not read new state"))
    monkeypatch.setattr(trial, "state", state)
    result = trial.execute("return_to_start", {})
    assert not result["ok"]
    assert result["phase"] == "restoring_disabled"
    assert result["last_verified_checkpoint"] == {
        "action": "return_to_start",
        "joints_deg": returned_actual,
        "robot_mode": 5,
    }
    assert result["verified_joints_deg"] == {
        "rotate_j4_out": OUT,
        "return_to_start": returned_actual,
    }
    move.assert_called_once_with(START, OUT)
    state.assert_not_called()


def test_uncertain_motion_stops_once_never_retries_or_returns(trial, monkeypatch):
    trial.initial_mode = 5
    monkeypatch.setattr(trial, "_preflight", lambda expected: 5)
    monkeypatch.setattr(trial, "_fresh", lambda *args, **kwargs: trial._latest)
    monkeypatch.setattr(trial, "_wait_stable", Mock())
    command = Mock(side_effect=[TimeoutError("ACK lost"), ""])
    monkeypatch.setattr(trial, "_command", command)
    result = trial.execute("rotate_j4_out", {})
    assert result["ok"] is False
    assert result["stop"]["stationary_verified"] is False
    assert "acknowledgement was unresolved or failed" in result["stop"]["error"]
    assert [call.args[0] for call in command.call_args_list] == [
        "JointMovJ(0.700000,0.040000,0.520000,25.080000,SpeedJ=5,AccJ=5)",
        "ResetRobot()",
    ]
    assert trial.execute("rotate_j4_out", {})["ok"] is False
    assert trial.execute("return_to_start", {})["ok"] is False
    trial.stop()
    assert command.call_count == 2


@pytest.mark.parametrize("kind", ["stale", "mode", "alarm", "other_joint", "j4_limit"])
def test_feedback_guard_rejects_unverified_state(trial, kind):
    joints, mode, error, received = START, 4, False, 10.0
    if kind == "stale":
        received = 9.74
    elif kind == "mode":
        mode = 9
    elif kind == "alarm":
        error = True
    elif kind == "other_joint":
        joints = (START[0] + 0.21, *START[1:])
    else:
        joints = (*START[:3], START[3] + 2.21)
    trial._latest = (1, received, Feedback(joints, mode, 0, error))
    with pytest.raises(RuntimeError):
        trial._fresh({4, 5})


@pytest.mark.parametrize("failure", [RuntimeError("stale feedback"), RuntimeError("mode 9")])
def test_stop_ack_is_not_stationary_confirmation(trial, monkeypatch, failure):
    trial._owned = trial._uncertain = True
    command = Mock(return_value="")
    monkeypatch.setattr(trial, "_command", command)
    monkeypatch.setattr(trial, "_wait_stable", Mock(side_effect=failure))
    result = trial.stop()
    assert not result["ok"]
    assert not result["stationary_verified"]
    assert result["physical_stop_requested"]
    assert trial.stop() is result
    assert command.call_count == 1


def test_idle_read_only_agent_failure_does_not_reset_robot(trial, monkeypatch):
    command = Mock()
    monkeypatch.setattr(trial, "_command", command)
    trial.stop()
    command.assert_not_called()


def test_owned_session_with_unexpected_motion_requests_stop(trial, monkeypatch):
    trial._owned = True
    trial._latest = (2, 10, Feedback(OUT, 7, 0, False))
    command = Mock(return_value="")
    monkeypatch.setattr(trial, "_command", command)
    monkeypatch.setattr(trial, "_wait_stable", Mock())
    assert trial.stop()["physical_stop_requested"]
    command.assert_called_once_with("ResetRobot()", mutation=True, stopping=True)


def test_stop_transport_failure_is_not_retried_or_reported_confirmed(trial, monkeypatch):
    trial._owned = trial._uncertain = True
    command = Mock(side_effect=TimeoutError("Stop ACK missing"))
    monkeypatch.setattr(trial, "_command", command)
    assert not trial.stop()["ok"]
    assert not trial.stop()["stationary_verified"]
    assert command.call_count == 1


def test_late_mutation_ack_after_stop_does_not_establish_command_order(trial, monkeypatch):
    trial._owned = trial._uncertain = True
    worker = Mock()
    completed = threading.Event()
    outcome = []
    trial._pending_mutation = worker, completed, outcome

    def reset(*args, **kwargs):
        outcome.append((True, ""))
        completed.set()
        return ""

    command = Mock(side_effect=reset)
    stable = Mock()
    monkeypatch.setattr(trial, "_command", command)
    monkeypatch.setattr(trial, "_wait_stable", stable)
    result = trial.stop()
    assert not result["ok"]
    assert not result["stationary_verified"]
    assert "physical emergency stop" in result["operator_action"]
    worker.join.assert_called_once_with(timeout=4.25)
    stable.assert_not_called()
    assert trial.stop() is result
    command.assert_called_once_with("ResetRobot()", mutation=True, stopping=True)


def test_context_exit_surfaces_failed_stop_even_with_original_error(trial, monkeypatch):
    trial._owned = True
    monkeypatch.setattr(trial, "stop", Mock(return_value={"ok": False, "error": "stale"}))
    close = Mock()
    monkeypatch.setattr(trial, "_close_feedback", close)
    original = RuntimeError("Final state request failed")
    with pytest.raises(RuntimeError, match="physical emergency stop") as raised:
        trial.__exit__(RuntimeError, original, None)
    assert raised.value.__cause__ is original
    close.assert_called_once_with()


def test_completion_requires_fresh_distinct_samples_and_stable_dwell(trial, monkeypatch):
    now = [10.0]
    sequence = [0]

    def tick(duration):
        now[0] += duration

    def fresh(*args, **kwargs):
        sequence[0] += 1
        # First 0.2 s is near target but still moving beyond the settling threshold.
        joint4 = OUT[3] + (0.08 if now[0] < 10.2 else 0.0)
        return sequence[0], now[0], Feedback((*OUT[:3], joint4), 5, 0, False)

    monkeypatch.setattr("mg400_agent.hardware_trial.time.monotonic", lambda: now[0])
    monkeypatch.setattr("mg400_agent.hardware_trial.time.sleep", tick)
    monkeypatch.setattr(trial, "_fresh", fresh)
    assert trial._wait_stable(OUT, final_modes={5}, allowed_modes={5, 7}).joints_deg == OUT
    assert now[0] >= 10.6


def test_repeated_same_sample_cannot_confirm_completion(trial, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("mg400_agent.hardware_trial.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "mg400_agent.hardware_trial.time.sleep",
        lambda duration: now.__setitem__(0, now[0] + duration),
    )
    monkeypatch.setattr(trial, "_fresh", lambda *a, **kw: (1, 10, Feedback(OUT, 5, 0, False)))
    with pytest.raises(TimeoutError):
        trial._wait_stable(OUT, final_modes={5}, allowed_modes={5, 7}, timeout=0.5)


def test_pending_ack_does_not_block_feedback_fault(trial, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def command(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        return ""

    def fresh(*args, **kwargs):
        if entered.is_set():
            raise RuntimeError("Feedback alarm while ACK pending")
        return trial._latest

    monkeypatch.setattr(trial, "_command", command)
    monkeypatch.setattr(trial, "_fresh", fresh)
    try:
        with pytest.raises(RuntimeError, match="ACK pending"):
            trial._guarded_command("JointMovJ(...)", port=30003, allowed_modes={5, 7})
        assert not release.is_set()
    finally:
        release.set()


def test_cancelled_delayed_mutation_is_not_sent(trial, monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=connection))
    trial._cancelled = True
    with pytest.raises(RuntimeError, match="cancelled before"):
        trial._command("JointMovJ(...)", mutation=True)
    connection.sendall.assert_not_called()


def test_transport_reassembles_response_and_never_retries(trial, monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = [b"0,{1,2,", b"3,4,0,0},GetAngle();"]
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=connection))
    assert trial._command("GetAngle()") == "1,2,3,4,0,0"
    connection.sendall.assert_called_once_with(b"GetAngle()")
    connection.recv.side_effect = TimeoutError("lost acknowledgement")
    with pytest.raises(TimeoutError):
        trial._command("JointMovJ(...)", port=30003, mutation=True)
    assert connection.sendall.call_count == 2


def test_preflight_rejects_changed_start_before_enabling(trial, monkeypatch):
    monkeypatch.setattr(trial, "_dashboard_state", lambda: (4, (*START[:3], START[3] + 0.15)))
    command = Mock()
    monkeypatch.setattr(trial, "_command", command)
    assert trial.execute("rotate_j4_out", {})["ok"] is False
    command.assert_not_called()


def test_external_enable_after_capture_is_rejected(trial, monkeypatch):
    trial._latest = (1, 10, Feedback(START, 5, 0, False))
    monkeypatch.setattr(trial, "_dashboard_state", lambda: (5, START))
    command = Mock()
    monkeypatch.setattr(trial, "_command", command)
    assert trial.execute("rotate_j4_out", {})["ok"] is False
    command.assert_not_called()


def test_context_entry_reads_only_and_captures_actual_start(monkeypatch):
    class Passive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("mg400_agent.hardware_trial.FeedbackConnection", Passive)
    monkeypatch.setattr(JointTrial, "_read_feedback", lambda self: None)
    monkeypatch.setattr(JointTrial, "_wait_stable", Mock())
    trial = JointTrial("robot.test")
    from time import monotonic

    trial._latest = (1, monotonic(), Feedback(START, 4, 0, False))
    command = Mock(side_effect=["4", "[[],[],[],[],[]]", ",".join(map(str, (*START, 0, 0)))])
    monkeypatch.setattr(trial, "_command", command)
    with trial:
        assert trial.start == START
        assert trial.initial_mode == 4
        assert trial.phase == "ready"
    assert [call.args[0] for call in command.call_args_list] == [
        "RobotMode()",
        "GetErrorID()",
        "GetAngle()",
    ]
