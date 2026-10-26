"""Test gripper commands without Isaac Sim or hardware connections."""

import asyncio
import builtins
import json
import runpy
import socket
import sys
import threading
import weakref
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
    return (
        dict(
            session="a51c687e-95f5-4fa5-a73d-6088dba073f1",
            seq=1,
            init=1,
            state=1,
            position=500,
            control=dict(
                protocol=2,
                command_id=None,
                target=None,
                ack=False,
                locked=False,
                error=None,
                io_mode=None,
            ),
        )
        | changes
    )


def test_file_transport_never_falls_back_to_io_and_requires_fresh_capability(module, tmp_path):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.operate = Mock()
    assert panel.measured_position() is None
    assert panel.submit("open") is None
    assert panel.submit("close") is None
    assert panel.submit_position(500) is None
    assert panel.report["command_published"] is False
    panel.operate.assert_not_called()
    socket.create_connection.assert_not_called()


def test_legacy_collector_measurement_is_viewable_but_cannot_publish(module, tmp_path):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    sample = position_feedback(sent_at=module.time.time())
    sample["control"]["protocol"] = 1
    assert panel.feedback.receive(sample)
    assert panel.measured_position() == 500
    assert panel.position_sample() is None
    assert panel.submit_position(525) is None
    assert "Update and restart the USB collector" in panel.report["result"]
    assert panel.report["command_published"] is False
    assert not panel.failed
    assert not list(tmp_path.glob("*.command.json*"))
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("position", [-1, 1001, True, 50.0])
def test_position_validation_prevents_file_writes(module, tmp_path, position):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    with pytest.raises(ValueError, match="integer"):
        panel.submit_position(position)
    assert not list(tmp_path.glob("*.command.json"))


@pytest.mark.parametrize("arrival", [True, False])
@pytest.mark.parametrize("initial_state", [1, 3])
def test_position_requires_current_ack_and_measured_arrival(
    module, monkeypatch, tmp_path, arrival, initial_state
):
    async def exercise():
        feedback_path = tmp_path / "feedback.json"
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(feedback_path))
        panel.report["contact_observed"] = True
        sample = position_feedback(state=initial_state)
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
                protocol=2,
                command_id=request["id"],
                target=525,
                ack=True,
                locked=False,
                error=None,
                io_mode=0,
            )

        monkeypatch.setattr(asyncio, "sleep", advance)
        first = panel.submit_position(525)
        assert panel.submit_position(550) is None
        await first
        assert panel.report["ack"]
        assert panel.report["arrival_observed"] is arrival
        assert panel.report["contact_observed"] is not arrival
        assert not panel.failed
        assert panel.measured_position() == sample["position"]
        assert len(observed) == 1
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("arrival_at", [4.9, 5.0, None])
@pytest.mark.parametrize("wall_jump", [-10000, 10000])
def test_position_budget_includes_file_io_acknowledgement_and_arrival(
    module, monkeypatch, tmp_path, arrival_at, wall_jump
):
    now, wall = [10.0], [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "time", lambda: wall[0])
    command = tmp_path / "feedback.json.command.json"
    write_text, replace = Path.write_text, Path.replace

    def delayed_write(path, content, *args, **kwargs):
        if path == command.with_name(command.name + ".tmp"):
            now[0] += 1
        return write_text(path, content, *args, **kwargs)

    def delayed_replace(path, destination):
        if Path(destination) == command:
            now[0] += 1
        return replace(path, destination)

    monkeypatch.setattr(Path, "write_text", delayed_write)
    monkeypatch.setattr(Path, "replace", delayed_replace)

    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        sample = position_feedback()
        panel.feedback.get = lambda: sample.copy()
        original_sleep = asyncio.sleep
        polls = []

        async def advance(delay):
            await original_sleep(0)
            request = json.loads(command.read_text())
            assert request["deadline_monotonic"] == 15.0
            assert request["sent_at"] == 1000.0
            polls.append(request)
            if len(polls) == 1:
                assert now[0] == 12.0
                now[0] = 12.5
                wall[0] += wall_jump
                sample.update(seq=2, state=0)
                sample["control"].update(command_id=request["id"], target=525, ack=True)
            elif len(polls) == 2:
                assert panel.report["ack"]
                assert not panel.report["arrival_observed"]
                now[0] = 10.0 + (5.0 if arrival_at is None else arrival_at)
                if arrival_at is not None:
                    sample.update(seq=3, state=1, position=525)
            else:
                now[0] += 2

        monkeypatch.setattr(asyncio, "sleep", advance)
        await panel.submit_position(525)
        arrived = arrival_at == 4.9
        assert panel.failed is not arrived
        assert panel.report["arrival_observed"] is arrived
        assert panel.report["ack"]
        assert now[0] == (14.9 if arrived else 15.0)
        assert len(polls) == 2 and polls[0] == polls[1]
        if not arrived:
            assert "target arrival is not confirmed" in panel.report["result"]
            assert panel.submit_position(550) is None
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


def test_position_waits_through_feedback_gap_without_refreshing_or_resending(
    module, monkeypatch, tmp_path
):
    from mg400_agent.gripper_feedback import FileFeedback

    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "time", lambda: 1000.0 + now[0])

    async def exercise():
        feedback_path = tmp_path / "feedback.json"
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(feedback_path))
        feedback_path.write_text(json.dumps(position_feedback(sent_at=1000.0)))
        source = FileFeedback(feedback_path, panel.feedback)
        source.poll()
        original_sleep = asyncio.sleep
        requests = []

        async def advance(delay):
            await original_sleep(0)
            requests.append(command_path.read_text())
            assert panel.busy and not panel.failed
            assert panel.submit_position(550) is None
            if len(requests) == 1:
                now[0] = 0.8
                source.poll()
                assert panel.feedback.get() is None
                assert panel.measured_position() is None
            else:
                now[0] = 0.9
                request = json.loads(requests[0])
                feedback_path.write_text(
                    json.dumps(
                        position_feedback(
                            seq=2,
                            sent_at=1000.9,
                            position=525,
                            control=dict(
                                protocol=2,
                                command_id=request["id"],
                                target=525,
                                ack=True,
                                locked=False,
                                error=None,
                                io_mode=0,
                            ),
                        )
                    )
                )
                source.poll()

        monkeypatch.setattr(asyncio, "sleep", advance)
        await panel.submit_position(525)
        assert len(requests) == 2 and requests[0] == requests[1] == command_path.read_text()
        assert not panel.failed
        assert panel.report["ack"] and panel.report["arrival_observed"]
        assert not panel.report["contact_observed"]
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("initialized", [0, 2])
def test_position_stops_waiting_on_explicit_initialization_diagnostic(
    module, monkeypatch, tmp_path, initialized
):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.feedback.receive(position_feedback(sent_at=module.time.time()))
        original_sleep = asyncio.sleep
        polls = []

        async def advance(delay):
            await original_sleep(0)
            polls.append(delay)
            assert not panel.feedback.receive(
                position_feedback(
                    seq=2,
                    sent_at=module.time.time(),
                    init=initialized,
                )
            )

        monkeypatch.setattr(asyncio, "sleep", advance)
        await panel.submit_position(525)
        assert panel.failed
        assert panel.feedback.diagnostic() in panel.report["result"]
        assert len(polls) == 1
        assert not panel.report["ack"]
        assert panel.submit_position(550) is None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "fault", ["stale", "session", "old_ack", "wrong_target", "locked", "dropped", "protocol"]
)
def test_position_failure_locks_panel_without_retry(module, monkeypatch, tmp_path, fault):
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
                sample[0].update(seq=2, position=525, state=3 if fault == "dropped" else 1)
                sample[0]["control"] = dict(
                    protocol=1 if fault == "protocol" else 2,
                    command_id=str(uuid4()) if fault == "old_ack" else request["id"],
                    target=526 if fault == "wrong_target" else 525,
                    ack=True,
                    locked=fault == "locked",
                    error="Write reply lost",
                    io_mode=0,
                )

        monkeypatch.setattr(asyncio, "sleep", advance)
        await panel.submit_position(525)
        original = command_path.read_text()
        assert panel.failed and not panel.report["arrival_observed"]
        assert not panel.report["contact_observed"]
        assert panel.report["command_published"] is True
        assert panel.report["ack"] is (fault == "dropped")
        assert panel.submit("open") is None
        assert command_path.read_text() == original
        assert now[0] == (1 if fault in ("session", "locked", "protocol") else 5)
        if fault == "protocol":
            assert "Update and restart the USB collector" in panel.report["result"]
        if fault == "locked":
            assert "Write reply lost" in panel.report["result"]
        if fault == "stale":
            assert "target arrival is not confirmed" in panel.report["result"]
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["session", "missing", "uninitialized", "write", "protocol"])
def test_position_rejection_before_publication_allows_explicit_retry(
    module, monkeypatch, tmp_path, change
):
    async def exercise():
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        sample = [position_feedback()]
        panel.feedback.get = lambda: sample[0]
        action = panel.submit_position(525)
        if change == "session":
            sample[0] = position_feedback(session=str(uuid4()))
        elif change == "protocol":
            sample[0]["control"]["protocol"] = 1
        elif change == "write":
            write_text = Path.write_text
            failed_once = False

            def fail_command_write(path, text, *args, **kwargs):
                nonlocal failed_once
                if path == command_path.with_name(command_path.name + ".tmp") and not failed_once:
                    failed_once = True
                    raise OSError("Temporary disk failure")
                return write_text(path, text, *args, **kwargs)

            monkeypatch.setattr(Path, "write_text", fail_command_write)
        else:
            sample[0] = None
            if change == "uninitialized":
                panel.feedback.diagnostic = lambda: "The gripper is not initialized."
        assert await action is False
        assert not panel.failed
        assert not panel.report["ack"]
        assert panel.report["command_published"] is False
        assert panel.report["result"].startswith("No command sent.")
        assert not command_path.exists()

        sample[0] = position_feedback(session=str(uuid4()))
        panel.feedback.diagnostic = lambda: None
        original_sleep = asyncio.sleep

        async def arrive(delay):
            await original_sleep(0)
            request = json.loads(command_path.read_text())
            sample[0] = position_feedback(
                session=request["session"],
                seq=2,
                position=525,
                control=dict(
                    protocol=2,
                    command_id=request["id"],
                    target=525,
                    ack=True,
                    locked=False,
                    error=None,
                    io_mode=0,
                ),
            )

        monkeypatch.setattr(asyncio, "sleep", arrive)
        await panel.submit_position(525)
        assert not panel.failed
        assert panel.report["ack"]
        assert panel.report["command_published"] is True
        assert panel.report["arrival_observed"]
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("published", [False, True])
def test_uncertain_command_publication_locks_panel_without_retry(
    module,
    monkeypatch,
    tmp_path,
    published,
):
    async def exercise():
        command_path = tmp_path / "feedback.json.command.json"
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.feedback.get = lambda: position_feedback()
        replace = Path.replace

        def uncertain_replace(path, destination):
            if Path(destination) == command_path:
                if published:
                    replace(path, destination)
                raise OSError("Publication outcome unavailable")
            return replace(path, destination)

        monkeypatch.setattr(Path, "replace", uncertain_replace)
        await panel.submit_position(525)
        assert panel.failed
        assert panel.report["command_published"] is None
        assert not panel.report["ack"]
        assert "Control is locked" in panel.report["result"]
        assert command_path.exists() is published
        assert panel.submit_position(525) is None
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
            self.begin_callbacks = {}

        def set_value(self, value):
            self.value = value

        def get_value_as_int(self):
            return self.value

        def add_begin_edit_fn(self, callback):
            token = len(self.begin_callbacks) + 1
            self.begin_callbacks[token] = callback
            return token

        def remove_begin_edit_fn(self, token):
            del self.begin_callbacks[token]

        def begin_edit(self):
            for callback in self.begin_callbacks.values():
                callback(self)

    class Widget:
        def __init__(self, text=None, *args, **kwargs):
            self.text = text
            self.enabled = kwargs.get("enabled", True)
            self.visible = kwargs.get("visible", True)
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
        **dict(
            busy=False,
            keyboard_enabled=False,
            keyboard_arming=False,
            closed=False,
            sync_enabled=True,
            targets_editable=True,
            begin_target_edit=Mock(),
            control=SimpleNamespace(stopped=False, read_state=Mock()),
        )
        | changes
    )


def install_mirror_kit(monkeypatch, next_update):
    install_embedded_ui(monkeypatch, next_update)
    for name in (
        "carb",
        "carb.eventdispatcher",
        "omni.timeline",
        "omni.usd",
        "mg400_agent.gripper_mirror",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["carb"].eventdispatcher = sys.modules["carb.eventdispatcher"]
    sys.modules["omni"].timeline = sys.modules["omni.timeline"]
    sys.modules["omni"].usd = sys.modules["omni.usd"]
    stage = object()
    timeline = SimpleNamespace(is_stopped=Mock(return_value=True))
    get_stage = Mock(return_value=stage)
    jaw = Mock(stage=stage)
    jaw.apply.return_value = {"q_m": 0.0025, "gap_m": 0.005}
    factory = Mock(return_value=jaw)
    sys.modules["omni.timeline"].get_timeline_interface = lambda: timeline
    sys.modules["omni.usd"].get_context = lambda: SimpleNamespace(get_stage=get_stage)
    sys.modules["mg400_agent.gripper_mirror"].JawMirror = factory
    monkeypatch.setattr(builtins, "_mg400_isaac_feedback_mirror", None, raising=False)
    return SimpleNamespace(
        stage=stage, timeline=timeline, get_stage=get_stage, jaw=jaw, factory=factory
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


@pytest.mark.parametrize("compact", [False, True])
def test_legacy_collector_disables_controls_until_protocol_upgrade(
    module, monkeypatch, tmp_path, compact
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner()
    window = SimpleNamespace(visible=True)
    panel.mirror = AsyncMock()
    sample = position_feedback()
    sample["control"]["protocol"] = 1
    panel.feedback.get = lambda: sample
    frames, clock = [0], [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def next_update():
        frames[0] += 1
        clock[0] += 0.6
        if frames[0] == 2:
            assert "Update and restart the USB collector" in panel.report["read_error"]
            assert panel.measured_position() == 500
            assert panel.target_initialized
            assert not panel.manual_enabled
            assert all(not widget.enabled for widget in panel.buttons + panel.position_widgets)
            sample["control"]["protocol"] = 2
        elif frames[0] == 3:
            assert panel.report["read_error"] is None
            assert panel.manual_enabled
            assert all(widget.enabled for widget in panel.buttons + panel.position_widgets)
            window.visible = False

    install_embedded_ui(monkeypatch, next_update)
    if compact:
        panel.build_compact_row()
    else:
        panel.build_ui()
    asyncio.run(panel.run(window=window))
    assert not panel.failed
    assert not list(tmp_path.glob("*.command.json*"))
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize(
    "initialized,message",
    [
        (0, "The gripper is not initialized."),
        (2, "Gripper initialization is in progress."),
    ],
)
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


@pytest.mark.parametrize(
    "initialized,message",
    [
        (0, "The gripper is not initialized."),
        (2, "Gripper initialization is in progress."),
    ],
)
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
    for name in (
        "carb",
        "carb.eventdispatcher",
        "omni.timeline",
        "omni.usd",
        "mg400_agent.gripper_mirror",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    jaw = Mock(side_effect=AssertionError("Invalid feedback must not update the model"))
    sys.modules["mg400_agent.gripper_mirror"].JawMirror = jaw
    asyncio.run(panel.mirror())
    assert frames[0] == 3
    assert panel.feedback.get() is None
    assert "position" not in panel.report["mirror"]
    jaw.assert_not_called()
    socket.create_connection.assert_not_called()


def test_mirror_displays_object_drop_and_keeps_measured_position(module, monkeypatch, tmp_path):
    path = tmp_path / "feedback.json"
    panel = module.GripperPanel(str(path))
    path.write_text(
        json.dumps(
            position_feedback(
                sent_at=module.time.time(),
                state=3,
                position=1000,
            )
        )
    )
    frames = [0]

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            panel.closed = True

    install_embedded_ui(monkeypatch, next_update)
    for name in (
        "carb",
        "carb.eventdispatcher",
        "omni.timeline",
        "omni.usd",
        "mg400_agent.gripper_mirror",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["omni"].timeline = sys.modules["omni.timeline"]
    sys.modules["omni"].usd = sys.modules["omni.usd"]
    sys.modules["omni.timeline"].get_timeline_interface = lambda: SimpleNamespace(
        is_stopped=lambda: True,
    )
    stage = object()
    sys.modules["omni.usd"].get_context = lambda: SimpleNamespace(get_stage=lambda: stage)
    jaw = Mock(stage=stage)
    jaw.apply.return_value = {"q_m": 0.005}
    sys.modules["mg400_agent.gripper_mirror"].JawMirror = Mock(return_value=jaw)
    monkeypatch.setattr(builtins, "_mg400_isaac_feedback_mirror", None, raising=False)
    asyncio.run(panel.mirror())
    assert frames[0] == 2
    assert panel.report["mirror"]["status"] == ("Object dropped. Following measured USB position.")
    assert panel.report["mirror"]["position"] == 1000
    assert panel.position_sample()["state"] == 3
    assert not panel.report["arrival_observed"]
    assert not panel.report["contact_observed"]
    jaw.apply.assert_called_with(1000)
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("fault", ["constructor", "apply", "import", "stage"])
def test_render_failure_keeps_polling_and_retries_with_latest_measurement(
    module, monkeypatch, tmp_path, fault
):
    path = tmp_path / "feedback.json"
    panel = module.GripperPanel(str(path))
    clock, frames = [0.0], [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    times = [0.0, 0.1, 0.5, 0.9, 1.05, 1.1]

    async def next_update():
        index = frames[0]
        frames[0] += 1
        if index:
            assert panel.feedback.get()["seq"] == index
        if 1 <= index <= 4:
            assert panel.report["mirror"]["error"] == "Broken scene"
            assert failed.call_count == 1
            assert not panel.failed
        if index == 5:
            assert "error" not in panel.report["mirror"]
            assert kit.jaw.apply.call_args_list[0].args == (540,)
            panel.closed = True
        clock[0] = times[index]
        path.write_text(
            json.dumps(
                position_feedback(
                    sent_at=module.time.time(),
                    seq=index + 1,
                    position=500 + index * 10,
                )
            )
        )

    kit = install_mirror_kit(monkeypatch, next_update)
    if fault == "constructor":
        kit.factory.side_effect = [RuntimeError("Broken scene"), kit.jaw]
        failed = kit.factory
    elif fault == "apply":
        broken = Mock(stage=kit.stage)
        broken.apply.side_effect = RuntimeError("Broken scene")
        kit.factory.side_effect = [broken, kit.jaw]
        failed = broken.apply
    elif fault == "stage":
        kit.get_stage.side_effect = [RuntimeError("Broken scene"), kit.stage, kit.stage]
        failed = kit.get_stage
    else:
        original_import = builtins.__import__
        failed = Mock()

        def import_module(name, *args, **kwargs):
            if name == "mg400_agent.gripper_mirror":
                failed()
                if failed.call_count == 1:
                    raise ImportError("Broken scene")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_module)
    asyncio.run(panel.mirror())
    assert frames[0] == len(times)
    assert not panel.failed
    assert "error" not in panel.report["mirror"]
    assert kit.jaw.apply.call_args_list[0].args == (540,)
    socket.create_connection.assert_not_called()


def test_render_retry_does_not_revive_unchanged_feedback(module, monkeypatch, tmp_path):
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps(position_feedback(sent_at=module.time.time())))
    panel = module.GripperPanel(str(path))
    clock, frames = [0.0], [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            assert panel.measured_position() == 500
            clock[0] = 0.751
        elif frames[0] == 3:
            assert panel.measured_position() is None
            clock[0] = 1.1
        elif frames[0] == 4:
            assert panel.feedback.get_status() is None
            assert "stale or invalid" in panel.report["mirror"]["status"]
            assert panel.report["mirror"]["error"] == "Broken scene"
            kit.factory.assert_called_once()
            panel.closed = True

    kit = install_mirror_kit(monkeypatch, next_update)
    kit.factory.side_effect = RuntimeError("Broken scene")
    asyncio.run(panel.mirror())
    assert frames[0] == 4
    kit.factory.assert_called_once()
    assert panel.feedback.get_status() is None
    kit.jaw.apply.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("gate", ["sync", "timeline"])
def test_render_recovery_respects_scene_gates_while_feedback_continues(
    module, monkeypatch, tmp_path, gate
):
    path = tmp_path / "feedback.json"
    panel = module.GripperPanel(str(path))
    panel.owner = gripper_owner()
    clock, frames = [0.0], [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            assert panel.report["mirror"]["error"] == "Broken scene"
            clock[0] = 1.1
            if gate == "sync":
                panel.owner.sync_enabled = False
            else:
                kit.timeline.is_stopped.return_value = False
        elif frames[0] == 3:
            assert panel.measured_position() == 520
            assert panel.report["mirror"]["error"] == "Broken scene"
            kit.factory.assert_called_once()
            panel.owner.sync_enabled = True
            kit.timeline.is_stopped.return_value = True
            clock[0] = 1.2
        elif frames[0] == 4:
            assert "error" not in panel.report["mirror"]
            panel.closed = True
        path.write_text(
            json.dumps(
                position_feedback(
                    seq=frames[0],
                    sent_at=module.time.time(),
                    position=500 + 10 * frames[0],
                )
            )
        )

    kit = install_mirror_kit(monkeypatch, next_update)
    kit.factory.side_effect = [RuntimeError("Broken scene"), kit.jaw]
    asyncio.run(panel.mirror())
    assert frames[0] == 4
    assert "error" not in panel.report["mirror"]
    assert kit.jaw.apply.call_args_list[0].args == (530,)
    socket.create_connection.assert_not_called()


def test_update_stream_failure_is_not_reported_as_render_failure(module, monkeypatch, tmp_path):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))

    async def next_update():
        raise RuntimeError("Update stream ended")

    kit = install_mirror_kit(monkeypatch, next_update)
    asyncio.run(panel.mirror())
    assert "Update stream ended" in panel.report["mirror"]["status"]
    assert "error" not in panel.report["mirror"]
    assert panel.feedback.get_status() is None
    kit.factory.assert_not_called()
    socket.create_connection.assert_not_called()


def test_webrtc_subscription_survives_render_failure_until_close(module, monkeypatch):
    panel = module.GripperPanel()
    clock, frames = [0.0], [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    observed = SimpleNamespace(callback=None, count=0, subscription=None, resets=0)

    class Subscription:
        def reset(self):
            observed.resets += 1

    def observe_event(*, event_name, on_event):
        assert event_name == "mg400.gripper.feedback"
        observed.count += 1
        observed.callback = on_event
        subscription = Subscription()
        observed.subscription = weakref.ref(subscription)
        return subscription

    async def next_update():
        frames[0] += 1
        if frames[0] > 1:
            assert panel.measured_position() == 500 + frames[0] - 1
            assert panel.report["mirror"]["error"] == "Broken scene"
            assert observed.subscription() is not None
            assert observed.count == 1
            assert observed.resets == 0
        clock[0] += 0.1
        observed.callback(
            SimpleNamespace(
                payload=position_feedback(
                    seq=frames[0],
                    sent_at=module.time.time(),
                    position=500 + frames[0],
                )
            )
        )
        if frames[0] == 3:
            panel.closed = True

    kit = install_mirror_kit(monkeypatch, next_update)
    kit.factory.side_effect = RuntimeError("Broken scene")
    manager = SimpleNamespace(is_extension_enabled=lambda name: True)
    sys.modules["omni.kit.app"].get_app = lambda: SimpleNamespace(
        next_update_async=next_update,
        get_extension_manager=lambda: manager,
    )
    sys.modules["carb.eventdispatcher"].get_eventdispatcher = lambda: SimpleNamespace(
        observe_event=observe_event,
    )
    asyncio.run(panel.mirror())
    assert frames[0] == 3 and observed.count == 1
    assert observed.resets == 1
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("result", [None, "position", "initialize"])
def test_render_warning_is_visible_without_changing_control_status(
    module, monkeypatch, tmp_path, compact, result
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner()
    panel.feedback.get = lambda: position_feedback()
    panel.mirror = AsyncMock()
    panel.report["mirror"].update(error="Broken scene", status="Scene update paused: Broken scene")
    message = "Control is locked. The device reply is missing."
    if result is not None:
        panel.failed = True
        panel.report.update(request=result, result=message)
    window = SimpleNamespace(visible=True)
    frames = [0]

    async def next_update():
        frames[0] += 1
        if frames[0] == 2:
            assert panel.render_warning.visible
            assert panel.render_warning.text == "Scene update paused: Broken scene"
            if compact:
                assert panel.actual_label.text == "500"
            else:
                assert "Actual: 500 / 1000" in panel.mirror_status.text
            if result is None:
                assert panel.manual_enabled
                assert all(widget.enabled for widget in panel.buttons + panel.position_widgets)
                assert panel.report["result"].startswith("Ready.")
            else:
                assert not panel.manual_enabled
                assert panel.report["result"] == message
                assert panel.initialization_status.text == message
                if not compact:
                    assert panel.status.text == message
            panel.report["mirror"].pop("error")
        elif frames[0] == 3:
            assert not panel.render_warning.visible
            if result is not None:
                assert panel.report["result"] == message
                assert panel.failed and not panel.manual_enabled
            window.visible = False

    install_embedded_ui(monkeypatch, next_update)
    if compact:
        panel.build_compact_row()
    else:
        panel.build_ui()
    asyncio.run(panel.run(window=window))
    assert not list(tmp_path.glob("*.command.json*"))
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("operation", ["position", "initialize"])
def test_render_failure_preserves_action_feedback_during_panel_close(
    module, monkeypatch, tmp_path, operation
):
    async def exercise():
        path = tmp_path / "feedback.json"
        path.write_text(json.dumps(position_feedback(sent_at=module.time.time())))
        panel = module.GripperPanel(str(path))
        panel.owner = gripper_owner()
        window = SimpleNamespace(visible=True)
        clock = [0.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        original_sleep = asyncio.sleep

        async def tick(*args):
            await original_sleep(0)

        async def until(predicate):
            for _ in range(100):
                if predicate():
                    return
                await original_sleep(0)
            raise AssertionError("The panel did not reach the expected state.")

        monkeypatch.setattr(asyncio, "sleep", tick)
        kit = install_mirror_kit(monkeypatch, tick)
        kit.factory.side_effect = RuntimeError("Broken scene")
        panel.build_compact_row()
        task = asyncio.create_task(panel.run(window=window))
        await until(lambda: "error" in panel.report["mirror"])
        if operation == "initialize":
            path.write_text(json.dumps(initialization_feedback(module, seq=2)))
            clock[0] = 0.1
            await until(lambda: panel.feedback.get_status()["init"] == 0)
            action = panel.submit_initialization()
        else:
            action = panel.submit_position(525)
        assert action is not None
        command = path.with_name(path.name + ".command.json")
        await until(command.exists)
        window.visible = False
        await until(lambda: panel.closing)
        assert not task.done() and not panel.mirror_task.done()
        assert not action.done()
        request = json.loads(command.read_text())
        path.write_text(
            json.dumps(
                position_feedback(
                    seq=3,
                    sent_at=module.time.time(),
                    position=525 if operation == "position" else 1000,
                    control=dict(
                        protocol=3,
                        operation=operation,
                        command_id=request["id"],
                        target=525 if operation == "position" else None,
                        ack=True,
                        locked=False,
                        error=None,
                        io_mode=0,
                    ),
                )
            )
        )
        clock[0] = 0.2
        await until(task.done)
        await task
        assert panel.closed and panel.mirror_task.done() and action.done()
        assert panel.report["ack"] and panel.report["arrival_observed"]
        assert not panel.failed
        assert panel.report["mirror"]["error"] == "Broken scene"
        assert json.loads(command.read_text()) == request
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


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
    assert len(callbacks) == 4
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
            protocol=2,
            command_id=request["id"],
            target=525,
            ack=True,
            locked=False,
            error=None,
            io_mode=0,
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
        panel = module.GripperPanel(
            str(tmp_path / "feedback.json") if transport == "position" else None
        )
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


def test_compact_gripper_row_shares_input_model_and_tracks_only_user_edits(
    module, monkeypatch, tmp_path
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner(sync_enabled=True)
    panel.feedback.get = lambda: position_feedback()
    panel.mirror = AsyncMock()
    window = SimpleNamespace(visible=True)
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 2:
            assert panel.target_initialized
            assert panel.target_baseline == 500
            assert panel.pending_target() is None
            assert panel.actual_label.text == "500"
            panel.target.model.set_value(650)
            assert panel.pending_target() == 650
            panel.owner.sync_enabled = False
        elif frames == 3:
            assert not panel.manual_enabled
            assert all(not widget.enabled for widget in panel.position_widgets)
            window.visible = False

    widgets, _ = install_embedded_ui(monkeypatch, next_update)
    panel.build_compact_row()
    assert panel.buttons == []
    assert [widget.text for widget in widgets if type(widget).__name__ == "Button"] == [
        "Initialize gripper"
    ]
    slider, field = panel.position_widgets
    assert slider.model is field.model
    asyncio.run(panel.run(window=window))
    assert not list(tmp_path.glob("*.command.json"))
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("cancel", ("closed", "sync_stopped", "control_stopped"))
def test_queued_position_command_cancels_when_owner_stops_before_publication(
    module, tmp_path, cancel
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner(sync_enabled=True, control=SimpleNamespace(stopped=False))
    panel.feedback.get = lambda: position_feedback()

    async def exercise():
        action = panel.submit_position(650)
        assert action is not None
        if cancel == "closed":
            panel.owner.closed = True
        elif cancel == "sync_stopped":
            panel.owner.sync_enabled = False
        else:
            panel.owner.control.stopped = True
        assert await action is False
        assert panel.report["command_published"] is False
        assert "cancelled" in panel.report["result"]
        assert not panel.failed
        assert not list(tmp_path.glob("*.command.json"))

    asyncio.run(exercise())


def test_compact_target_edit_can_disarm_idle_keyboard_without_enabling_manual_commands(
    module, monkeypatch, tmp_path
):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner(keyboard_enabled=True)
    panel.submit_position = Mock()

    def disarm(_model):
        panel.owner.keyboard_enabled = False

    panel.owner.begin_target_edit = Mock(side_effect=disarm)
    install_embedded_ui(monkeypatch, AsyncMock())
    panel.build_compact_row()
    panel.target_initialized = True
    assert panel.target_edit_enabled
    assert not panel.manual_enabled
    panel.target.model.begin_edit()
    assert not panel.owner.keyboard_enabled
    panel.owner.begin_target_edit.assert_called_once_with(panel.target.model)
    panel.target.model.set_value(650)
    panel.submit_position.assert_not_called()


@pytest.mark.parametrize(
    "state",
    (
        "busy",
        "keyboard_arming",
        "closed",
        "sync_stopped",
        "targets_locked",
        "gripper_failed",
        "control_stopped",
    ),
)
def test_compact_target_edit_retains_owner_and_gripper_gates(module, monkeypatch, tmp_path, state):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner(keyboard_enabled=True)
    install_embedded_ui(monkeypatch, AsyncMock())
    panel.build_compact_row()
    panel.target_initialized = True
    if state == "sync_stopped":
        panel.owner.sync_enabled = False
    elif state == "targets_locked":
        panel.owner.targets_editable = False
    elif state == "gripper_failed":
        panel.failed = True
    elif state == "control_stopped":
        panel.owner.control.stopped = True
    else:
        setattr(panel.owner, state, True)
    assert not panel.target_edit_enabled
    assert not panel.manual_enabled


def initialization_feedback(module, **changes):
    return (
        position_feedback(
            sent_at=module.time.time(),
            init=0,
            state=65535,
            position=1120,
            control=dict(
                protocol=3,
                operation=None,
                command_id=None,
                target=None,
                ack=False,
                locked=False,
                error=None,
                io_mode=None,
            ),
        )
        | changes
    )


def test_opted_in_panel_initializes_once_after_fresh_feedback_and_idle_arm(
    module, monkeypatch, tmp_path
):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.auto_initialize_pending = True
        panel.owner = gripper_owner(busy=True)
        panel.perform_initialization = AsyncMock(return_value=True)
        assert panel.maybe_initialize() is None
        sample = initialization_feedback(module)
        panel.feedback.receive(sample)
        assert panel.maybe_initialize() is None
        panel.owner.busy = False
        action = panel.maybe_initialize()
        assert action is not None
        assert panel.maybe_initialize() is None
        await action
        assert panel.maybe_initialize() is None
        panel.perform_initialization.assert_awaited_once_with(sample)
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("initialized", [1, 2])
def test_opted_in_panel_skips_initialized_or_initializing_gripper(module, tmp_path, initialized):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.auto_initialize_pending = True
    panel.feedback.receive(
        initialization_feedback(
            module,
            init=initialized,
            state=1,
            position=1000,
        )
    )
    panel.submit_initialization = Mock()
    panel.maybe_initialize()
    panel.feedback.receive(initialization_feedback(module, seq=2))
    panel.maybe_initialize()
    panel.submit_initialization.assert_not_called()


@pytest.mark.parametrize(
    "block",
    ["stale", "protocol", "locked", "busy", "keyboard", "stopped", "robot_feedback", "closing"],
)
def test_initialization_rejects_unavailable_state_without_writing(module, tmp_path, block):
    panel = module.GripperPanel(str(tmp_path / "feedback.json"))
    panel.owner = gripper_owner()
    sample = initialization_feedback(module)
    if block == "protocol":
        sample["control"].update(protocol=2)
    if block == "locked":
        sample["control"].update(locked=True)
    if block == "busy":
        panel.owner.busy = True
    if block == "keyboard":
        panel.owner.keyboard_enabled = True
    if block == "stopped":
        panel.owner.control.stopped = True
    if block == "robot_feedback":
        panel.owner.control.read_state.side_effect = RuntimeError("stale robot feedback")
    if block == "closing":
        panel.closing = True
    if block != "stale":
        panel.feedback.receive(sample)
    assert not panel.initialization_enabled
    assert panel.submit_initialization() is None
    assert not list(tmp_path.glob("*.command.json*"))


@pytest.mark.parametrize("outcome", ["complete", "timeout", "locked", "session"])
def test_initialization_waits_for_matching_ack_and_valid_measured_completion(
    module, monkeypatch, tmp_path, outcome
):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.owner = gripper_owner()
        now = [0.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
        panel.feedback.receive(initialization_feedback(module))
        requests = []

        async def advance(_):
            now[0] += 1
            request = json.loads((tmp_path / "feedback.json.command.json").read_text())
            requests.append(request)
            assert request["operation"] == "initialize"
            assert "position" not in request
            assert request["deadline_monotonic"] == 15
            assert panel.submit_initialization() is None
            if now[0] == 1:
                # Acknowledgement alone must not accept an uninitialized gripper.
                sample = initialization_feedback(module, seq=2)
                sample["control"].update(operation="initialize", command_id=request["id"], ack=True)
                panel.feedback.receive(sample)
            elif now[0] == 3 and outcome != "timeout":
                sample = initialization_feedback(module, seq=3, init=1, state=1, position=1000)
                sample["control"].update(
                    operation="initialize",
                    command_id=request["id"],
                    ack=True,
                    locked=outcome == "locked",
                )
                if outcome == "session":
                    sample["session"] = str(uuid4())
                panel.feedback.receive(sample)

        monkeypatch.setattr(module.asyncio, "sleep", advance)
        action = panel.submit_initialization()
        assert action is not None
        await action
        assert requests and all(request == requests[0] for request in requests)
        assert panel.failed is (outcome != "complete")
        assert panel.report["arrival_observed"] is (outcome == "complete")
        assert not panel.auto_initialize_pending
        assert panel.maybe_initialize() is None
        socket.create_connection.assert_not_called()

    asyncio.run(exercise())


def test_initialization_rechecks_owner_before_publication(module, tmp_path):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        panel.owner = gripper_owner()
        panel.feedback.receive(initialization_feedback(module))
        action = panel.submit_initialization()
        panel.owner.busy = True
        assert await action is False
        assert not panel.failed
        assert not list(tmp_path.glob("*.command.json*"))
        assert panel.maybe_initialize() is None

    asyncio.run(exercise())


@pytest.mark.parametrize("auto_initialize", [False, True])
def test_compact_initialization_requires_click_unless_explicitly_enabled(
    module, monkeypatch, tmp_path, auto_initialize
):
    async def exercise():
        panel = module.GripperPanel(str(tmp_path / "feedback.json"))
        if auto_initialize:
            panel.auto_initialize_pending = True
        panel.owner = gripper_owner()
        panel.feedback.receive(initialization_feedback(module))

        async def complete(_):
            panel.feedback.receive(
                initialization_feedback(
                    module,
                    seq=2,
                    init=1,
                    state=1,
                    position=1000,
                )
            )
            return True

        panel.perform_initialization = AsyncMock(side_effect=complete)
        panel.mirror = AsyncMock()
        window = SimpleNamespace(visible=True)
        frames = [0]

        async def next_update():
            frames[0] += 1
            if frames[0] == 2 and not auto_initialize:
                panel.perform_initialization.assert_not_awaited()
                assert panel.action is None
                assert not list(tmp_path.glob("*.command.json*"))
                assert panel.initialize_button.enabled
                assert panel.initialize_button.clicked_fn() is not None
            if frames[0] == 3:
                assert not panel.initialize_button.enabled
                window.visible = False
            await asyncio.sleep(0)

        widgets, _ = install_embedded_ui(monkeypatch, next_update)
        panel.build_compact_row()
        button = next(widget for widget in widgets if widget.text == "Initialize gripper")
        assert button is panel.initialize_button
        await panel.run(window=window)
        panel.perform_initialization.assert_awaited_once()
        assert not button.enabled

    asyncio.run(exercise())
