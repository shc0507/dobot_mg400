"""Test gripper commands without Isaac Sim or hardware connections."""

import asyncio
import builtins
import json
import runpy
import socket
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    with monkeypatch.context() as guard:
        guard.setattr(asyncio, "ensure_future", Mock(side_effect=AssertionError("No startup task")))
        namespace = runpy.run_path(
            str(ROOT / "apps/isaacsim/gripper_control.py"), run_name="test_gripper"
        )
    monkeypatch.setitem(
        namespace["GripperPanel"].write_report.__globals__, "REPORT", tmp_path / "status.json"
    )
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "192.168.1.6"\n')
    monkeypatch.setitem(namespace["GripperPanel"].__init__.__globals__, "ROBOT_CONFIG", config)
    return SimpleNamespace(**namespace, config_path=config)


def test_import_and_construction_send_no_commands(module):
    panel = module.GripperPanel()
    assert panel.action is None
    assert "MG400_GRIPPER_START_TASK" not in vars(module)
    socket.create_connection.assert_not_called()


def test_collector_request_atomically_replaces_token_without_sending_commands(
    module, monkeypatch, tmp_path
):
    feedback = tmp_path / "feedback.json"
    request = tmp_path / "gripper_collector.start"
    temporary = tmp_path / "gripper_collector.tmp"
    panel = module.GripperPanel(str(feedback))
    assert not request.exists()
    request.write_text("previous request\n")
    previous, tokens = [request.read_text()], []
    replace = Path.replace

    def observe_replace(path, destination):
        assert path == temporary
        assert destination == request
        assert request.read_text() == previous[0]
        content = path.read_text()
        token = UUID(content.strip())
        assert token.version == 4
        assert content == str(token) + "\n"
        tokens.append(str(token))
        result = replace(path, destination)
        previous[0] = content
        return result

    monkeypatch.setattr(Path, "replace", observe_replace)
    for _ in range(2):
        assert panel.request_collector_start() is None
        assert panel.report["collector_start_error"] is None
        assert not temporary.exists()
    assert tokens[0] != tokens[1]
    assert not feedback.exists()
    assert not list(tmp_path.glob("*.command.json"))
    socket.create_connection.assert_not_called()


def test_io_panel_does_not_write_collector_request(module, monkeypatch):
    panel = module.GripperPanel()
    write = Mock(side_effect=AssertionError("I/O mode must not write a collector request"))
    monkeypatch.setattr(Path, "write_text", write)
    assert panel.request_collector_start() is None
    write.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("operation", ("write_text", "replace"))
def test_collector_request_failure_preserves_old_request_and_removes_temporary_file(
    module, monkeypatch, tmp_path, operation
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    request = tmp_path / "gripper_collector.start"
    temporary = tmp_path / "gripper_collector.tmp"
    request.write_text("previous request\n")
    original = getattr(Path, operation)

    def fail_request(path, *args, **kwargs):
        if path == temporary:
            raise PermissionError("Shared directory is read-only")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail_request)
    assert panel.request_collector_start() is None
    assert panel.report["collector_start_error"] == (
        "Could not request USB feedback: Shared directory is read-only"
    )
    assert request.read_text() == "previous request\n"
    assert not temporary.exists()
    assert not panel.failed
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


def test_command_uses_host_captured_when_panel_was_created(module, monkeypatch):
    module.config_path.write_text('[robot]\nhost = "robot-first.test"\n')
    panel = module.GripperPanel()
    module.config_path.write_text('[robot]\nhost = "robot-second.test"\n')
    connection(monkeypatch, [b"0,{5},RobotMode();"])
    assert panel.command("RobotMode()") == "5"
    assert panel.host == "robot-first.test"
    socket.create_connection.assert_called_once_with(("robot-first.test", 29999), timeout=2)


@pytest.mark.parametrize("invalid", ("missing", "invalid"))
def test_invalid_configuration_prevents_gripper_connections(module, invalid):
    if invalid == "missing":
        module.config_path.unlink()
    else:
        module.config_path.write_text('[robot]\nhost = ""\n')
    with pytest.raises((FileNotFoundError, ValueError)):
        module.GripperPanel()
    socket.create_connection.assert_not_called()


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


def position_feedback(**changes):
    return dict(
        session="a51c687e-95f5-4fa5-a73d-6088dba073f1", seq=1, init=1, state=1, position=500,
        control=dict(protocol=1, command_id=None, target=None, ack=False,
                     locked=False, error=None, io_mode=None),
        **changes,
    )


def test_file_transport_never_falls_back_to_io_and_requires_fresh_capability(module, tmp_path):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.operate = Mock()
    assert panel.measured_position() is None
    assert panel.submit("open") is None
    assert panel.submit("close") is None
    assert panel.submit_position(500) is None
    panel.operate.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("position", [-1, 1001, True, 50.0])
def test_position_validation_prevents_file_writes(module, tmp_path, position):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    with pytest.raises(ValueError, match="integer"):
        panel.submit_position(position)
    assert not list(tmp_path.glob("*.command.json"))


@pytest.mark.parametrize("arrival", [True, False])
def test_position_requires_current_ack_and_measured_arrival(
    module, monkeypatch, tmp_path, arrival
):
    async def exercise():
        feedback_path = tmp_path / "feedback.json"
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(feedback_path))
        sample = position_feedback()
        panel.feedback.get = lambda: sample.copy()
        original_sleep = asyncio.sleep
        observed = []

        async def advance(delay):
            await original_sleep(0)
            request = json.loads(command_path.read_text())
            observed.append(request)
            assert request["session"] == sample["session"]
            assert request["position"] == 525
            sample["seq"] += 1
            sample["position"] = 525 if arrival else 515
            sample["state"] = 1 if arrival else 2
            sample["control"] = dict(
                protocol=1, command_id=request["id"], target=525, ack=True,
                locked=False, error=None, io_mode=0,
            )

        monkeypatch.setattr(asyncio, "sleep", advance)
        first = panel.submit_position(525)
        assert panel.submit_position(550) is None
        await first
        assert panel.report["ack"]
        assert panel.report["arrival_observed"] is arrival
        assert not panel.failed
        assert panel.measured_position() == sample["position"]
        assert len(observed) == 1
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["stale", "session", "old_ack", "wrong_target", "locked"])
def test_position_failure_locks_panel_without_retry(
    module, monkeypatch, tmp_path, fault
):
    async def exercise():
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        sample = [position_feedback()]
        panel.feedback.get = lambda: None if sample[0] is None else sample[0].copy()
        original_sleep = asyncio.sleep
        now = [0.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

        async def advance(delay):
            await original_sleep(0)
            request = json.loads(command_path.read_text())
            now[0] += 1
            if fault == "stale":
                sample[0] = None
            elif fault == "session":
                sample[0]["session"] = str(uuid4())
            else:
                sample[0].update(seq=2, position=525)
                sample[0]["control"] = dict(
                    protocol=1,
                    command_id=str(uuid4()) if fault == "old_ack" else request["id"],
                    target=526 if fault == "wrong_target" else 525,
                    ack=True, locked=fault == "locked", error="Write reply lost", io_mode=0,
                )

        monkeypatch.setattr(asyncio, "sleep", advance)
        await panel.submit_position(525)
        original = command_path.read_text()
        assert panel.failed and not panel.report["arrival_observed"]
        assert not panel.report["ack"]
        assert panel.submit("open") is None
        assert command_path.read_text() == original
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


def test_file_open_close_use_position_transport(module, tmp_path):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.submit_position = Mock()
    panel.submit("open")
    panel.submit("close")
    assert [call.args for call in panel.submit_position.call_args_list] == [(1000,), (0,)]


def install_embedded_ui(monkeypatch, next_update):
    widgets = []

    class IntModel:
        def __init__(self, value=0):
            self.value = value

        def set_value(self, value):
            self.value = value

        def get_value_as_int(self):
            return self.value

    class Widget:
        def __init__(self, text=None, *args, **kwargs):
            self.text = text
            self.enabled = kwargs.get("enabled", True)
            self.clicked_fn = kwargs.get("clicked_fn")
            self.model = kwargs.get("model", IntModel())
            widgets.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    omni = ModuleType("omni")
    omni.ui = ModuleType("omni.ui")
    omni.kit = ModuleType("omni.kit")
    omni.kit.app = ModuleType("omni.kit.app")
    omni.kit.app.get_app = lambda: SimpleNamespace(next_update_async=next_update)
    omni.ui.Window = Mock(side_effect=AssertionError("Embedded controls must not create a window"))
    for name in ("VStack", "HStack", "Label", "IntSlider", "IntField", "Button"):
        setattr(omni.ui, name, type(name, (Widget,), {}))
    omni.ui.SimpleIntModel = IntModel
    for item in (omni, omni.ui, omni.kit, omni.kit.app):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    return widgets, omni.ui.Window


def gripper_owner(**changes):
    return SimpleNamespace(
        **dict(busy=False, keyboard_enabled=False, keyboard_arming=False, closed=False) | changes
    )


def test_run_requests_collector_once_while_stale_feedback_keeps_ui_running(
    module, monkeypatch, tmp_path
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    parent_window = SimpleNamespace(visible=True, destroy=Mock())
    panel.mirror = AsyncMock()
    panel.request_collector_start = Mock(wraps=panel.request_collector_start)
    frames, clock = [0], [0.0]
    monkeypatch.setitem(panel.run.__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def next_update():
        frames[0] += 1
        clock[0] += 0.6
        if frames[0] == 2:
            assert panel.status.text == (
                "USB feedback is stale or unavailable. Check the collector on Olares."
            )
            assert all(not widget.enabled for widget in panel.buttons + panel.position_widgets)
        if frames[0] == 3:
            parent_window.visible = False

    _, new_window = install_embedded_ui(monkeypatch, next_update)
    panel.build_ui()
    asyncio.run(panel.run(window=parent_window))
    assert frames[0] == 3
    panel.request_collector_start.assert_called_once_with()
    assert (tmp_path / "gripper_collector.start").exists()
    assert not list(tmp_path.glob("*.command.json"))
    assert not panel.failed
    new_window.assert_not_called()
    parent_window.destroy.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("initialized,message", [
    (0, "The gripper is not initialized."),
    (2, "Gripper initialization is in progress."),
])
def test_uninitialized_feedback_disables_controls_and_expires_to_stale(
    module, monkeypatch, tmp_path, initialized, message
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    parent_window = SimpleNamespace(visible=True, destroy=Mock())
    panel.mirror = AsyncMock()
    frames, clock = [0], [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    payload = position_feedback(sent_at=module.time.time())
    payload.update(init=initialized, state=65535, position=1070)
    assert not panel.feedback.receive(payload)
    assert panel.measured_position() is None
    assert panel.position_sample() is None
    assert panel.submit_position(500) is None
    assert panel.report["result"] == message

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            assert panel.status.text == message
            assert not panel.target_initialized
            assert all(not widget.enabled for widget in panel.buttons + panel.position_widgets)
            clock[0] = 0.751
        elif frames[0] == 3:
            assert panel.status.text.startswith("USB feedback is stale or unavailable.")
            assert not panel.manual_enabled
            parent_window.visible = False

    install_embedded_ui(monkeypatch, next_update)
    panel.build_ui()
    asyncio.run(panel.run(window=parent_window))
    assert not panel.failed
    assert not list(tmp_path.glob("*.command.json"))
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("initialized,message", [
    (0, "The gripper is not initialized."),
    (2, "Gripper initialization is in progress."),
])
def test_mirror_reports_initialization_without_applying_invalid_position(
    module, monkeypatch, tmp_path, initialized, message
):
    path = tmp_path / "feedback.json"
    panel = module.GripperPanel(str(path))
    payload = position_feedback(sent_at=module.time.time())
    payload.update(init=initialized, state=65535, position=1070)
    path.write_text(json.dumps(payload))
    frames, clock = [0], [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            assert panel.report["mirror"]["status"] == message + " Holding last position."
            clock[0] = 0.751
        elif frames[0] == 3:
            assert panel.report["mirror"]["status"] == (
                "Feedback stale or invalid. Holding last position."
            )
            panel.closed = True

    install_embedded_ui(monkeypatch, next_update)
    for name in ("carb", "carb.eventdispatcher", "omni.timeline", "omni.usd",
                 "mg400_agent.gripper_mirror"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    jaw = Mock(side_effect=AssertionError("Invalid feedback must not update the model"))
    sys.modules["mg400_agent.gripper_mirror"].JawMirror = jaw
    asyncio.run(panel.mirror())
    assert frames[0] == 3
    assert panel.feedback.get() is None
    assert "position" not in panel.report["mirror"]
    jaw.assert_not_called()
    socket.create_connection.assert_not_called()


def test_fresh_feedback_restores_controls_after_collector_request_failure(
    module, monkeypatch, tmp_path
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    parent_window = SimpleNamespace(visible=True, destroy=Mock())
    panel.owner = gripper_owner()
    panel.mirror = AsyncMock()
    sample = [None]
    panel.feedback.get = lambda: sample[0]
    frames, clock = [0], [0.0]
    monkeypatch.setitem(panel.run.__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    replace = Path.replace

    def fail_request(path, destination):
        if path.name == "gripper_collector.tmp":
            raise PermissionError("Shared directory is read-only")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_request)

    async def next_update():
        frames[0] += 1
        clock[0] += 0.6
        if frames[0] == 2:
            assert panel.status.text == (
                "Could not request USB feedback: Shared directory is read-only"
            )
            assert not panel.failed
            assert not panel.manual_enabled
            sample[0] = position_feedback()
        if frames[0] == 3:
            assert panel.report["read_error"] is None
            assert panel.status.text.startswith("Ready.")
            assert panel.manual_enabled
            assert all(widget.enabled for widget in panel.buttons + panel.position_widgets)
            parent_window.visible = False

    _, new_window = install_embedded_ui(monkeypatch, next_update)
    panel.build_ui()
    asyncio.run(panel.run(window=parent_window))
    assert frames[0] == 3
    assert not panel.failed
    assert not (tmp_path / "gripper_collector.tmp").exists()
    assert not list(tmp_path.glob("*.command.json"))
    new_window.assert_not_called()
    parent_window.destroy.assert_not_called()
    socket.create_connection.assert_not_called()


def test_embedded_ui_uses_parent_window_and_preserves_edited_target(module, monkeypatch, tmp_path):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.owner = gripper_owner()
        parent_window = SimpleNamespace(visible=True, destroy=Mock())
        sample = position_feedback()
        panel.feedback.get = lambda: sample.copy()
        frames = 0
        mirror_finished = asyncio.Event()

        async def mirror():
            while not panel.closed:
                await asyncio.sleep(0)
            mirror_finished.set()

        async def next_update():
            nonlocal frames
            frames += 1
            await asyncio.sleep(0)
            if frames == 2:
                assert panel.target.model.get_value_as_int() == 500
                panel.target.model.set_value(650)
                sample["position"] = 700
            elif frames == 3:
                assert panel.target.model.get_value_as_int() == 650
                parent_window.visible = False

        widgets, new_window = install_embedded_ui(monkeypatch, next_update)
        panel.mirror = mirror
        panel.build_ui()
        widget_count = len(widgets)
        assert panel.action is None
        assert not list(tmp_path.glob("*.command.json"))
        await asyncio.wait_for(panel.run(window=parent_window), 1)
        assert len(widgets) == widget_count
        assert panel.closed
        assert mirror_finished.is_set()
        assert panel.mirror_task.done()
        new_window.assert_not_called()
        parent_window.destroy.assert_not_called()
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("owner_state", ["busy", "keyboard_enabled", "keyboard_arming", "closed"])
def test_manual_callbacks_reject_owner_motion_and_keyboard_control(
    module, monkeypatch, tmp_path, owner_state
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner(**{owner_state: True})
    panel.feedback.get = lambda: position_feedback()
    widgets, _ = install_embedded_ui(monkeypatch, AsyncMock())
    panel.build_ui()
    panel.submit = Mock()
    panel.submit_position = Mock()
    panel.target.model.set_value(650)
    callbacks = [widget.clicked_fn for widget in widgets if widget.clicked_fn is not None]
    assert len(callbacks) == 3
    for callback in callbacks:
        callback()
    panel.submit.assert_not_called()
    panel.submit_position.assert_not_called()
    setattr(panel.owner, owner_state, False)
    for callback in callbacks:
        callback()
    assert [call.args for call in panel.submit.call_args_list] == [("open",), ("close",)]
    panel.submit_position.assert_called_once_with(650)


def test_keyboard_position_submission_remains_available_while_owner_is_busy(module, tmp_path):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.owner = gripper_owner(busy=True, keyboard_enabled=True)
        sample = position_feedback()
        panel.feedback.get = lambda: sample.copy()
        panel.perform_position = AsyncMock()
        action = panel.submit_position(525)
        assert action is not None
        await action
        panel.perform_position.assert_awaited_once_with(525, sample)
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("close_request", ["window", "closing"])
def test_embedded_close_waits_for_current_ack_and_keeps_mirror_alive(
    module, monkeypatch, tmp_path, close_request
):
    async def exercise():
        feedback_path = tmp_path / "feedback.json"
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(feedback_path))
        panel.owner = gripper_owner()
        sample = position_feedback()
        panel.feedback.get = lambda: sample.copy()
        parent_window = SimpleNamespace(visible=True, destroy=Mock())
        closing_seen = asyncio.Event()
        mirror_finished = asyncio.Event()

        async def mirror():
            while not panel.closed:
                await asyncio.sleep(0)
            mirror_finished.set()

        async def next_update():
            await asyncio.sleep(0)
            if command_path.exists():
                if close_request == "window":
                    parent_window.visible = False
                else:
                    panel.closing = True
                closing_seen.set()

        _, new_window = install_embedded_ui(monkeypatch, next_update)
        panel.mirror = mirror
        panel.build_ui()
        panel.task = asyncio.create_task(panel.run(window=parent_window))
        action = panel.submit_position(525)
        assert action is not None
        await asyncio.wait_for(closing_seen.wait(), 1)
        await asyncio.sleep(0)
        assert panel.closing
        assert not panel.closed
        assert not panel.task.done()
        assert not mirror_finished.is_set()
        assert panel.submit_position(550) is None
        request = json.loads(command_path.read_text())
        sample.update(seq=2, position=525)
        sample["control"] = dict(
            protocol=1, command_id=request["id"], target=525, ack=True,
            locked=False, error=None, io_mode=0,
        )
        await asyncio.wait_for(panel.task, 1)
        assert action.done()
        assert panel.report["arrival_observed"]
        assert not panel.failed
        assert panel.closed
        assert panel.mirror_task.done()
        assert mirror_finished.is_set()
        assert json.loads(command_path.read_text())["id"] == request["id"]
        parent_window.destroy.assert_not_called()
        new_window.assert_not_called()
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["position", "io"])
def test_close_before_action_starts_prevents_queued_command(module, tmp_path, transport):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json") if transport == "position"
                                    else None)
        panel.feedback.get = lambda: position_feedback()
        panel.operate = Mock()
        action = panel.submit_position(525) if transport == "position" else panel.submit("open")
        assert action is not None
        panel.closing = True
        await action
        assert not list(tmp_path.glob("*.command.json"))
        panel.operate.assert_not_called()
        assert not panel.failed

    asyncio.run(exercise())


def test_close_during_io_preflight_prevents_first_output(module):
    panel = module.GripperPanel()

    def state():
        panel.closing = True
        return 1, 0

    panel.state = state
    panel.command = Mock()
    with pytest.raises(RuntimeError, match="cannot send another command"):
        panel.operate("open")
    panel.command.assert_not_called()
    assert not panel.report["ack"]


def test_embedded_close_releases_mirror_when_action_raises(module, monkeypatch, tmp_path):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        parent_window = SimpleNamespace(visible=False, destroy=Mock())
        _, new_window = install_embedded_ui(monkeypatch, AsyncMock())
        panel.build_ui()
        panel.mirror = AsyncMock()
        panel.action = asyncio.get_running_loop().create_future()
        panel.action.set_exception(RuntimeError("Report write failed"))
        with pytest.raises(RuntimeError, match="Report write failed"):
            await panel.run(window=parent_window)
        assert panel.closed
        assert panel.mirror_task.done()
        panel.mirror.assert_awaited_once_with()
        parent_window.destroy.assert_not_called()
        new_window.assert_not_called()

    asyncio.run(exercise())


def test_standalone_reload_cannot_replace_owned_gripper(module, monkeypatch):
    old = SimpleNamespace(owner=gripper_owner(), closed=False, busy=False, task=None)
    monkeypatch.setattr(builtins, module.SINGLETON, old, raising=False)
    with pytest.raises(RuntimeError, match="Use the MG400 Control panel"):
        asyncio.run(module.replace_panel())
    assert not old.closed
    assert getattr(builtins, module.SINGLETON) is old
    socket.create_connection.assert_not_called()


def test_reload_drains_legacy_panel_without_closing_attribute(module, monkeypatch):
    async def exercise():
        old = SimpleNamespace(closed=False, busy=False, task=None)

        async def old_run():
            while not old.closed:
                await asyncio.sleep(0)

        old.task = asyncio.create_task(old_run())
        monkeypatch.setattr(builtins, module.SINGLETON, old, raising=False)
        monkeypatch.setattr(module.GripperPanel, "run", AsyncMock())
        new = await asyncio.wait_for(module.replace_panel(), 1)
        assert old.closed
        assert old.task.done()
        assert getattr(builtins, module.SINGLETON) is new
        assert new is not old
        await new.task
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())
