"""Test gripper commands without Isaac Sim or hardware connections."""

import asyncio
import json
import runpy
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    with monkeypatch.context() as guard:
        guard.setattr(asyncio, "ensure_future", Mock(side_effect=AssertionError("No startup task")))
        namespace = runpy.run_path(
            str(ROOT / "examples/isaac_sim_gripper_mg400.py"), run_name="test_gripper"
        )
    monkeypatch.setitem(
        namespace["GripperPanel"].write_report.__globals__, "REPORT", tmp_path / "status.json"
    )
    return SimpleNamespace(**namespace)


def test_import_and_construction_send_no_commands(module):
    panel = module.GripperPanel()
    assert panel.action is None
    assert "MG400_GRIPPER_START_TASK" not in vars(module)
    socket.create_connection.assert_not_called()


def connection(monkeypatch, replies):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.recv.side_effect = replies
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=result))
    return result


def test_command_checks_fragmented_response(module, monkeypatch):
    wire = connection(monkeypatch, [b"0,{", b"},DOExecute(1,1);"])
    assert module.GripperPanel().command("DOExecute(1,1)") == ""
    wire.sendall.assert_called_once_with(b"DOExecute(1,1)")
    socket.create_connection.assert_called_once_with(("192.168.1.6", 29999), timeout=2)


def test_command_accepts_multiline_alarm_json(module, monkeypatch):
    wire = connection(monkeypatch, [b"0,{[\n[],\n[],\n", b"[],\n[],\n[]\n]},GetErrorID();"])
    payload = module.GripperPanel().command("GetErrorID()")
    assert json.loads(payload) == [[], [], [], [], []]
    wire.sendall.assert_called_once_with(b"GetErrorID()")


@pytest.mark.parametrize("reply", [b"0,{[\n[]\n]},RobotMode();", b"-1,{[\n[]\n]},GetErrorID();"])
def test_multiline_response_still_requires_success_and_exact_echo(module, monkeypatch, reply):
    wire = connection(monkeypatch, [reply])
    with pytest.raises((RuntimeError, ValueError)):
        module.GripperPanel().command("GetErrorID()")
    wire.sendall.assert_called_once_with(b"GetErrorID()")


@pytest.mark.parametrize(
    "reply", [b"-1,{},DOExecute(1,1);", b"0,{},DOExecute(1,0);", b"0,{},DOExecute(1,1);extra", b""]
)
def test_bad_response_does_not_retry_write(module, monkeypatch, reply):
    wire = connection(monkeypatch, [reply])
    with pytest.raises((RuntimeError, ValueError, ConnectionError)):
        module.GripperPanel().command("DOExecute(1,1)")
    wire.sendall.assert_called_once()
    socket.create_connection.assert_called_once()


def test_command_rejects_arm_and_other_output_commands(module):
    panel = module.GripperPanel()
    for text in ("EnableRobot()", "ResetRobot()", "DOExecute(3,1)", "DOExecute(2,1)"):
        with pytest.raises(ValueError, match="not allowed"):
            panel.command(text)
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("mode,alarms", [("7", "[[]]"), ("4", "[[42]]"), ("4", "[]")])
def test_preflight_blocks_output_when_robot_is_not_ready(module, mode, alarms):
    panel = module.GripperPanel()
    panel.command = Mock(side_effect=[mode, alarms])
    with pytest.raises(RuntimeError, match="not ready"):
        panel.operate("close")
    assert [call.args[0] for call in panel.command.call_args_list] == [
        "RobotMode()",
        "GetErrorID()",
    ]


@pytest.mark.parametrize("mode", ["4", "5"])
def test_refresh_reads_raw_inputs_without_output(module, mode):
    panel = module.GripperPanel()
    panel.command = Mock(side_effect=[mode, "[[],[],[]]", "1", "0"])
    panel.refresh()
    assert panel.report["di"] == [1, 0]
    assert panel.report["mode"] == int(mode)
    assert [call.args[0] for call in panel.command.call_args_list] == [
        "RobotMode()",
        "GetErrorID()",
        "DI(1)",
        "DI(2)",
    ]


@pytest.mark.parametrize("action,value", [("open", 0), ("close", 1)])
@pytest.mark.parametrize("transition", [True, False])
def test_output_mapping_and_arrival_requires_transition(
    module, monkeypatch, action, value, transition
):
    panel = module.GripperPanel()
    panel.command = Mock(return_value="")
    frames = iter([(1, 0), (0, 0), (1, 0)] if transition else [(1, 0)])
    panel.state = Mock(side_effect=lambda: next(frames, (1, 0)))
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    panel.operate(action)
    assert [call.args[0] for call in panel.command.call_args_list] == [
        "DOExecute(2,0)",
        f"DOExecute(1,{value})",
    ]
    assert panel.report["ack"]
    assert panel.report["arrival_observed"] is transition


def test_duplicate_click_does_not_queue_another_operation(module):
    async def exercise():
        panel = module.GripperPanel()
        entered, release = threading.Event(), threading.Event()

        def operate(request):
            entered.set()
            assert release.wait(2)

        panel.operate = Mock(side_effect=operate)
        first = panel.submit("open")
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert panel.submit("close") is None
        finally:
            release.set()
            await first
        panel.operate.assert_called_once_with("open")

    asyncio.run(exercise())


def test_write_failure_locks_panel_without_second_write(module):
    async def exercise():
        panel = module.GripperPanel()
        panel.state = Mock(return_value=(1, 0))
        panel.command = Mock(side_effect=TimeoutError("Response lost"))
        await panel.submit("close")
        assert panel.failed
        assert not panel.report["ack"]
        assert panel.report["request"] == "close"
        assert panel.report["time"]
        assert panel.submit("open") is None
        panel.command.assert_called_once_with("DOExecute(2,0)")

    asyncio.run(exercise())
