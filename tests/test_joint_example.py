import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def example(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "examples" / "move_joint_once.py"
    spec = importlib.util.spec_from_file_location("move_joint_once", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.socket, "create_connection", Mock(side_effect=AssertionError("Network prohibited"))
    )
    return module


@pytest.fixture(params=["joints", "linear"])
def motion(request):
    if request.param == "joints":
        return SimpleNamespace(
            name="move_joints",
            target=(10, -20, 30, -45),
            query="GetAngle()",
            command="JointMovJ(10.000000,-20.000000,30.000000,-45.000000,SpeedJ=10,AccJ=10)",
            start="10,-20,30,-40",
        )
    return SimpleNamespace(
        name="move_linear",
        target=(250, -25, 30, -45),
        query="GetPose(0,0)",
        command="MovL(250.000000,-25.000000,30.000000,-45.000000,User=0,Tool=0,SpeedL=1,AccL=1)",
        start="250,-25,30,-40",
    )


def successful_responses(motion):
    return [
        "5",
        "[[],[],[],[],[]]",
        motion.start,
        "5",
        "",
        motion.start,
        "",
        "",
        "",
        ",".join(map(str, motion.target)),
        "5",
    ]


def test_get_joints_accepts_actual_six_value_feedback(example, monkeypatch):
    command = Mock(return_value="10.824381,-4.704126,-1.64904,-22.60437,0,0")
    monkeypatch.setattr(example, "command", command)
    assert example.get_joints() == (10.824381, -4.704126, -1.64904, -22.60437)
    command.assert_called_once_with("GetAngle()")


@pytest.mark.parametrize(
    "reader, query", [("get_joints", "GetAngle()"), ("get_pose", "GetPose(0,0)")]
)
def test_four_value_joint_or_pose_feedback_remains_supported(example, monkeypatch, reader, query):
    command = Mock(return_value="10,-20,30,-40")
    monkeypatch.setattr(example, "command", command)
    assert getattr(example, reader)() == (10, -20, 30, -40)
    command.assert_called_once_with(query)


def test_pose_feedback_does_not_silently_truncate_six_values(example, monkeypatch):
    monkeypatch.setattr(example, "command", Mock(return_value="250,20,30,0,0,45"))
    with pytest.raises(ValueError):
        example.get_pose()


@pytest.mark.parametrize("reader", ["get_joints", "get_pose"])
@pytest.mark.parametrize("raw", ["", "1", "1,2,3", "1,2,3,4,5", "1,2,3,4,5,6,7", "1,bad,3,4"])
def test_position_feedback_rejects_malformed_data(example, monkeypatch, reader, raw):
    monkeypatch.setattr(example, "command", Mock(return_value=raw))
    with pytest.raises(ValueError):
        getattr(example, reader)()


@pytest.mark.parametrize(
    "reader, raw",
    [
        ("get_joints", "nan,2,3,4"),
        ("get_joints", "1,2,3,-inf"),
        ("get_joints", "1,2,nan,4,0,0"),
        ("get_joints", "1,2,3,4,inf,0"),
        ("get_joints", "1,2,3,4,0,-inf"),
        ("get_pose", "1,nan,3,4"),
        ("get_pose", "1,2,3,inf"),
    ],
)
def test_position_feedback_validates_finiteness_before_truncating(
    example, monkeypatch, reader, raw
):
    monkeypatch.setattr(example, "command", Mock(return_value=raw))
    with pytest.raises(ValueError):
        getattr(example, reader)()


@pytest.mark.parametrize("entrypoint", ["main", "move_joints"])
def test_six_value_joint_feedback_still_sends_only_four_joint_targets(
    example, monkeypatch, entrypoint
):
    start = "10.824381,-4.704126,-1.64904,-22.60437,0,0"
    actual = "10.824381,-4.704126,-1.64904,-17.60437,0,0"
    target = (10.824381, -4.704126, -1.64904, -17.60437)
    replies = ["5", "[[],[],[],[],[]]", start, "5", "", start, "", "", "", actual, "5"]
    if entrypoint == "main":
        replies.insert(0, start)
    command = Mock(side_effect=replies)
    monkeypatch.setattr(example, "command", command)
    if entrypoint == "main":
        example.main()
    else:
        assert example.move_joints(*target) == target
    motions = [item for item in command.call_args_list if item.args[0].startswith("JointMovJ(")]
    assert motions == [
        call("JointMovJ(10.824381,-4.704126,-1.649040,-17.604370,SpeedJ=10,AccJ=10)", port=30003)
    ]
    assert command.call_args_list[-3:] == [
        call("Sync()", port=30003, timeout=30.0),
        call("GetAngle()"),
        call("RobotMode()"),
    ]
    assert call("ResetRobot()") not in command.call_args_list


def test_default_changes_only_j4_once_and_verifies_arrival(example, monkeypatch):
    reader = Mock(return_value=(10, 20, 30, 40))
    mover = Mock(return_value=(10, 20, 30, 45))
    monkeypatch.setattr(example, "get_joints", reader)
    monkeypatch.setattr(example, "move_joints", mover)
    example.main()
    reader.assert_called_once_with()
    mover.assert_called_once_with(10, 20, 30, 45)


def test_public_move_uses_correct_absolute_units_query_and_verified_arrival(
    example, monkeypatch, motion
):
    command = Mock(side_effect=successful_responses(motion))
    monkeypatch.setattr(example, "command", command)
    assert getattr(example, motion.name)(*motion.target) == motion.target
    assert command.call_args_list == [
        call("RobotMode()"),
        call("GetErrorID()"),
        call(motion.query),
        call("RobotMode()"),
        call("SpeedFactor(10)"),
        call(motion.query),
        call("CP(0)"),
        call(motion.command, port=30003),
        call("Sync()", port=30003, timeout=30.0),
        call(motion.query),
        call("RobotMode()"),
    ]


def test_public_move_returns_measured_position_within_tolerance(example, monkeypatch, motion):
    responses = successful_responses(motion)
    measured = tuple(value + 0.05 for value in motion.target)
    responses[-2] = ",".join(map(str, measured))
    monkeypatch.setattr(example, "command", Mock(side_effect=responses))
    assert getattr(example, motion.name)(*motion.target) == measured


@pytest.mark.parametrize("mode", [1, 6, 7, 8, 9, 10, 11])
def test_nonidle_modes_never_prepare_or_move(example, monkeypatch, motion, mode):
    command = Mock(return_value=str(mode))
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(RuntimeError, match="must be idle"):
        getattr(example, motion.name)(*motion.target)
    command.assert_called_once_with("RobotMode()")


@pytest.mark.parametrize("errors", ["[[17],[],[],[],[]]", "[[],[23],[],[],[]]", "[]", "{}"])
def test_alarm_or_invalid_error_state_never_prepares_or_moves(example, monkeypatch, motion, errors):
    command = Mock(side_effect=["5", errors])
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(RuntimeError, match="alarms"):
        getattr(example, motion.name)(*motion.target)
    assert command.call_args_list == [call("RobotMode()"), call("GetErrorID()")]


@pytest.mark.parametrize(
    "delta",
    [
        (0, 0, 0),
        (0, 0, 0, 0, 0),
        (0, float("nan"), 0, 0),
        (0, 0, float("inf"), 0),
        (5.01, 0, 0, 0),
        (0, 0, 0, -5.01),
    ],
)
def test_invalid_delta_never_contacts_controller(example, monkeypatch, delta):
    command = Mock()
    monkeypatch.setattr(example, "command", command)
    monkeypatch.setattr(example, "DELTA_DEG", delta)
    with pytest.raises(ValueError):
        example.main()
    command.assert_not_called()


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_absolute_target_never_contacts_controller(
    example, monkeypatch, motion, index, invalid
):
    command = Mock()
    monkeypatch.setattr(example, "command", command)
    target = list(motion.target)
    target[index] = invalid
    with pytest.raises(ValueError):
        getattr(example, motion.name)(*target)
    command.assert_not_called()


@pytest.mark.parametrize("invalid", [True, "1.5", None])
def test_nonnumeric_absolute_target_never_contacts_controller(
    example, monkeypatch, motion, invalid
):
    command = Mock()
    monkeypatch.setattr(example, "command", command)
    target = [invalid, *motion.target[1:]]
    with pytest.raises(ValueError):
        getattr(example, motion.name)(*target)
    command.assert_not_called()


@pytest.mark.parametrize("target", [(1, 2, 3), (1, 2, 3, 4, 5)])
def test_wrong_target_count_never_contacts_controller(example, monkeypatch, motion, target):
    command = Mock()
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(TypeError):
        getattr(example, motion.name)(*target)
    command.assert_not_called()


@pytest.mark.parametrize("failure_index", [7, 8])
@pytest.mark.parametrize("failure", [TimeoutError("Timed out"), RuntimeError("Unexpected reply")])
def test_ambiguous_motion_or_sync_failure_requests_stop_without_retry(
    example, monkeypatch, motion, failure_index, failure
):
    responses = successful_responses(motion)[:failure_index] + [failure, ""]
    command = Mock(side_effect=responses)
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(type(failure)) as caught:
        getattr(example, motion.name)(*motion.target)
    assert caught.value is failure
    assert command.call_args_list[-1] == call("ResetRobot()")
    motions = [item for item in command.call_args_list if item.args[0] == motion.command]
    assert len(motions) == 1


@pytest.mark.parametrize("wrong_axis", [0, 1, 2, 3, None])
def test_wrong_arrival_or_nonidle_result_requests_stop(example, monkeypatch, motion, wrong_axis):
    actual = list(motion.target)
    mode = "9" if wrong_axis is None else "5"
    if wrong_axis is not None:
        actual[wrong_axis] += 0.11
    responses = successful_responses(motion)
    responses[-2:] = [",".join(map(str, actual)), mode]
    command = Mock(side_effect=responses + [""])
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(RuntimeError, match="Arrival not confirmed"):
        getattr(example, motion.name)(*motion.target)
    assert command.call_args_list[-1] == call("ResetRobot()")
    assert sum(item.args[0] == motion.command for item in command.call_args_list) == 1


def test_starting_pose_change_stops_before_motion(example, monkeypatch, motion):
    responses = successful_responses(motion)[:6]
    shifted = [float(value) for value in motion.start.split(",")]
    shifted[0] += 0.21
    responses[-1] = ",".join(map(str, shifted))
    command = Mock(side_effect=responses + [""])
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(RuntimeError, match="changed"):
        getattr(example, motion.name)(*motion.target)
    assert command.call_args_list[-1] == call("ResetRobot()")
    assert not any(item.args[0] == motion.command for item in command.call_args_list)


def test_disabled_robot_is_enabled_before_motion(example, monkeypatch, motion):
    responses = successful_responses(motion)
    responses[0] = "4"
    responses.insert(3, "")
    command = Mock(side_effect=responses)
    monkeypatch.setattr(example, "command", command)
    getattr(example, motion.name)(*motion.target)
    assert command.call_args_list[3] == call("EnableRobot()")
    assert sum(item.args[0] == motion.command for item in command.call_args_list) == 1


def test_stop_failure_is_reported_without_masking_original_error(
    example, monkeypatch, motion, capsys
):
    original = TimeoutError("Motion ACK missing")
    command = Mock(
        side_effect=successful_responses(motion)[:7] + [original, TimeoutError("Stop ACK missing")]
    )
    monkeypatch.setattr(example, "command", command)
    with pytest.raises(TimeoutError) as caught:
        getattr(example, motion.name)(*motion.target)
    assert caught.value is original
    assert "STOP NOT CONFIRMED" in capsys.readouterr().out


def socket_for(example, monkeypatch, parts):
    sock = Mock()
    sock.recv.side_effect = parts
    context = Mock()
    context.__enter__ = Mock(return_value=sock)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(example.socket, "create_connection", Mock(return_value=context))
    return sock


def test_command_sends_once_and_reassembles_fragmented_reply(example, monkeypatch):
    sock = socket_for(example, monkeypatch, [b"0,{10,20", b",30,40},GetAngle();"])
    assert example.command("GetAngle()") == "10,20,30,40"
    sock.sendall.assert_called_once_with(b"GetAngle()")


@pytest.mark.parametrize("reply", [b"0,{},OtherCommand();", b"-1,{},Sync();", b"invalid;"])
def test_command_rejects_wrong_echo_or_failed_ack(example, monkeypatch, reply):
    sock = socket_for(example, monkeypatch, [reply])
    with pytest.raises(RuntimeError):
        example.command("Sync()", port=30003)
    sock.sendall.assert_called_once_with(b"Sync()")


@pytest.mark.parametrize(
    "parts, failure",
    [([b"0,{", b""], ConnectionError), ([TimeoutError("Receive timed out")], TimeoutError)],
)
def test_command_never_retries_after_lost_ack(example, monkeypatch, parts, failure):
    sock = socket_for(example, monkeypatch, parts)
    with pytest.raises(failure):
        example.command("JointMovJ(1,2,3,4)", port=30003)
    sock.sendall.assert_called_once_with(b"JointMovJ(1,2,3,4)")
