"""Test web gripper commands with temporary files and simulated feedback."""

import contextlib
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from helpers import block_network

from mg400_agent import web_gripper


@pytest.fixture
def rig(monkeypatch, tmp_path):
    block_network(monkeypatch, "create_connection", message="No network")
    now = [10.0]
    monkeypatch.setattr(web_gripper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(web_gripper.time, "time", lambda: 1000.0 + now[0])
    feedback_path = tmp_path / "feedback.json"
    command_path = tmp_path / "feedback.json.command.json"
    gripper = web_gripper.WebGripper(feedback_path)
    sample = {
        "session": str(uuid4()),
        "seq": 0,
        "init": 1,
        "state": 1,
        "position": 500,
        "control": {
            "protocol": 3,
            "command_id": None,
            "operation": None,
            "target": None,
            "ack": False,
            "locked": False,
            "error": None,
            "io_mode": None,
        },
    }

    def emit(**changes):
        sample.update(changes)
        sample["seq"] += 1
        sample["sent_at"] = 1000.0 + now[0]
        feedback_path.write_text(json.dumps(sample))

    requests = []

    def acknowledge(delay):
        now[0] += delay
        request = json.loads(command_path.read_text())
        requests.append(request)
        operation = request.get("operation", "position")
        sample["control"].update(
            command_id=request["id"],
            operation=operation,
            target=request.get("position"),
            ack=True,
        )
        emit(init=1, state=1, position=request.get("position", 1000))

    monkeypatch.setattr(web_gripper.time, "sleep", acknowledge)
    return SimpleNamespace(
        gripper=gripper,
        feedback_path=feedback_path,
        command_path=command_path,
        now=now,
        sample=sample,
        emit=emit,
        acknowledge=acknowledge,
        requests=requests,
    )


def test_construction_and_snapshot_do_not_read_or_start_collector(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("No reads")))
    gripper = web_gripper.WebGripper(tmp_path / "feedback.json")
    assert gripper.snapshot() == {
        "position": None,
        "available": False,
        "can_initialize": False,
        "busy": False,
        "locked": False,
        "message": "USB feedback is stale or unavailable. Check the collector.",
    }
    assert list(tmp_path.iterdir()) == []


def test_poll_exposes_measured_position_but_unchanged_file_becomes_stale(rig):
    rig.emit()
    rig.gripper.poll()
    assert rig.gripper.snapshot()["position"] == 500
    assert rig.gripper.snapshot()["available"]
    rig.now[0] += 0.8
    rig.gripper.poll()
    assert rig.gripper.snapshot()["position"] is None
    assert not rig.gripper.snapshot()["available"]
    assert not rig.command_path.exists()


@pytest.mark.parametrize("position", [-1, 1001, True, 500.0, "500"])
def test_invalid_target_cannot_publish(rig, position):
    rig.emit()
    with pytest.raises(ValueError, match="integer"):
        rig.gripper.move(position)
    assert not rig.command_path.exists()


@pytest.mark.parametrize("protocol", [2, 3])
@pytest.mark.parametrize("contact", [False, True])
def test_move_requires_current_ack_and_measured_result(rig, monkeypatch, protocol, contact):
    rig.sample["control"]["protocol"] = protocol
    rig.emit()

    def advance(delay):
        rig.acknowledge(delay)
        if contact:
            rig.emit(state=2, position=510)

    monkeypatch.setattr(web_gripper.time, "sleep", advance)
    assert rig.gripper.move(525) is True
    request = rig.requests[0]
    assert request == {
        "id": request["id"],
        "session": rig.sample["session"],
        "sent_at": 1010.0,
        "deadline_monotonic": 15.0,
        "position": 525,
    }
    assert len(rig.requests) == 1
    assert not rig.gripper.snapshot()["locked"]
    assert ("contact" in rig.gripper.snapshot()["message"]) is contact
    assert not rig.command_path.with_name(rig.command_path.name + ".tmp").exists()
    socket.create_connection.assert_not_called()


def test_initialization_requires_explicit_call_and_protocol_three(rig):
    rig.emit(init=0, position=65535, state=65535)
    rig.gripper.poll()
    assert rig.gripper.snapshot()["can_initialize"]
    assert not rig.gripper.snapshot()["available"]
    assert rig.gripper.snapshot()["position"] is None
    assert not rig.command_path.exists()
    with pytest.raises(RuntimeError, match="not initialized"):
        rig.gripper.move(525)
    assert rig.gripper.initialize() is True
    request = rig.requests[0]
    assert request["operation"] == "initialize"
    assert request["deadline_monotonic"] == 25.0
    assert "position" not in request
    assert rig.gripper.snapshot()["position"] == 1000
    assert not rig.gripper.snapshot()["can_initialize"]


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "session",
        "old_ack",
        "wrong_target",
        "locked",
        "dropped",
        "protocol",
        "uninitialized",
    ],
)
def test_unknown_move_result_locks_without_retry(rig, monkeypatch, fault):
    rig.emit()

    def advance(delay):
        if fault == "stale":
            rig.now[0] += 1
            return
        rig.acknowledge(1)
        if fault == "session":
            rig.emit(session=str(uuid4()))
        elif fault == "old_ack":
            rig.sample["control"]["command_id"] = str(uuid4())
            rig.emit()
        elif fault == "wrong_target":
            rig.sample["control"]["target"] = 526
            rig.emit()
        elif fault == "locked":
            rig.sample["control"].update(locked=True, error="Write reply lost.")
            rig.emit()
        elif fault == "dropped":
            rig.emit(state=3)
        elif fault == "protocol":
            rig.sample["control"]["protocol"] = 1
            rig.emit()
        elif fault == "uninitialized":
            rig.emit(init=0)

    monkeypatch.setattr(web_gripper.time, "sleep", advance)
    with pytest.raises(RuntimeError, match="Control is locked"):
        rig.gripper.move(525)
    published = rig.command_path.read_text()
    assert rig.gripper.snapshot()["locked"]
    assert not rig.gripper.snapshot()["available"]
    with pytest.raises(RuntimeError, match="Control is locked"):
        rig.gripper.move(550)
    assert rig.command_path.read_text() == published


@pytest.mark.parametrize("operation", ["move", "initialize"])
def test_result_at_deadline_is_not_success(rig, monkeypatch, operation):
    rig.emit(init=0 if operation == "initialize" else 1)
    monkeypatch.setattr(
        web_gripper.time,
        "sleep",
        lambda delay: rig.acknowledge(15 if operation == "initialize" else 5),
    )
    with pytest.raises(RuntimeError, match="not confirmed"):
        rig.gripper.move(525) if operation == "move" else rig.gripper.initialize()
    assert rig.gripper.snapshot()["locked"]


def test_snapshot_and_duplicate_request_during_move_are_thread_safe(rig, monkeypatch):
    rig.emit()
    entered, release = threading.Event(), threading.Event()
    result = []

    def wait(delay):
        entered.set()
        assert release.wait(2)
        rig.acknowledge(delay)

    monkeypatch.setattr(web_gripper.time, "sleep", wait)
    worker = threading.Thread(target=lambda: result.append(rig.gripper.move(525)))
    worker.start()
    try:
        assert entered.wait(1)
        rig.gripper.poll()
        assert rig.gripper.snapshot()["busy"]
        assert not rig.gripper.snapshot()["available"]
        with pytest.raises(RuntimeError, match="in progress"):
            rig.gripper.move(550)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert result == [True]


@pytest.mark.parametrize("fault", ["session", "locked", "wrong_operation", "in_progress"])
def test_initialization_requires_matching_completion(rig, monkeypatch, fault):
    rig.emit(init=0)

    def advance(delay):
        rig.acknowledge(1)
        if fault == "session":
            rig.emit(session=str(uuid4()))
        elif fault == "locked":
            rig.sample["control"].update(locked=True, error="Initialization result unknown.")
            rig.emit()
        elif fault == "wrong_operation":
            rig.sample["control"].update(operation="position", target=1000)
            rig.emit()
        else:
            rig.emit(init=2)

    monkeypatch.setattr(web_gripper.time, "sleep", advance)
    with pytest.raises(RuntimeError, match="Control is locked"):
        rig.gripper.initialize()
    assert rig.gripper.snapshot()["locked"]


@pytest.mark.parametrize("operation", ["move", "initialize"])
def test_cancellation_during_preparation_prevents_publication(rig, monkeypatch, operation):
    rig.emit(init=0 if operation == "initialize" else 1)
    prepared, release = threading.Event(), threading.Event()
    panel_lock = threading.RLock()
    cancelled = [False]
    errors = []
    original = Path.write_text

    @contextlib.contextmanager
    def guard():
        with panel_lock:
            if cancelled[0]:
                raise RuntimeError("The panel cancelled the command.")
            yield

    rig.gripper.publication_guard = guard

    def prepare(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name.endswith(".command.json.tmp"):
            prepared.set()
            assert release.wait(2)
        return result

    def operate():
        try:
            rig.gripper.move(525) if operation == "move" else rig.gripper.initialize()
        except RuntimeError as error:
            errors.append(str(error))

    monkeypatch.setattr(Path, "write_text", prepare)
    worker = threading.Thread(target=operate)
    worker.start()
    try:
        assert prepared.wait(1)
        with panel_lock:
            cancelled[0] = True
            assert rig.gripper.snapshot()["busy"]
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert errors == ["No command sent. The panel cancelled the command."]
    assert not rig.command_path.exists()
    assert not rig.command_path.with_name(rig.command_path.name + ".tmp").exists()
    assert not rig.gripper.snapshot()["locked"]


def test_publication_holds_guard_until_replace_finishes(rig, monkeypatch):
    rig.emit()
    guarded = [False]
    original = Path.replace

    @contextlib.contextmanager
    def guard():
        guarded[0] = True
        try:
            yield
        finally:
            guarded[0] = False

    def publish(path, destination):
        assert guarded[0]
        return original(path, destination)

    gripper = web_gripper.WebGripper(rig.feedback_path, publication_guard=guard)
    monkeypatch.setattr(Path, "replace", publish)
    assert gripper.move(525)
    assert not guarded[0]


def fail_temporary(monkeypatch, operation):
    """Make one file operation on the temporary command file raise OSError."""
    original = getattr(Path, operation)

    def fail(path, *args, **kwargs):
        if path.name.endswith(".command.json.tmp"):
            raise OSError("Disk failure.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail)


def test_publication_guard_refusal_raises_not_sent_without_lock(rig):
    rig.emit()
    refuse = [True]

    @contextlib.contextmanager
    def guard():
        if refuse[0]:
            raise RuntimeError("The panel cancelled the command.")
        yield

    rig.gripper.publication_guard = guard
    with pytest.raises(web_gripper.GripperNotSentError) as raised:
        rig.gripper.move(525)
    assert str(raised.value) == "No command sent. The panel cancelled the command."
    assert not isinstance(raised.value, web_gripper.GripperLockedError)
    assert not rig.gripper.failed
    assert not rig.gripper.snapshot()["locked"]
    assert rig.gripper.last_outcome is None
    assert not rig.command_path.exists()
    refuse[0] = False
    assert rig.gripper.move(525) is True
    assert rig.gripper.last_outcome == "arrived"


@pytest.mark.parametrize(
    "cause",
    [
        "absent",
        "stale",
        "uninitialized",
        "initializing",
        "collector_locked",
        "old_collector",
        "write_failure",
        "session_change",
        "closed",
        "init_old_protocol",
        "init_already",
        "init_in_progress",
    ],
)
def test_command_that_publishes_nothing_raises_not_sent_without_lock(rig, monkeypatch, cause):
    if cause != "absent":
        rig.emit()
    if cause == "stale":
        # The sample was received, and the unchanged file then ages.
        rig.gripper.poll()
        rig.now[0] += 0.8
    elif cause == "uninitialized":
        rig.emit(init=0)
    elif cause == "initializing":
        rig.emit(init=2)
    elif cause == "collector_locked":
        rig.sample["control"].update(locked=True, error="Write reply lost.")
        rig.emit()
    elif cause == "old_collector":
        rig.sample["control"]["protocol"] = 1
        rig.emit()
    elif cause == "init_old_protocol":
        rig.sample["control"]["protocol"] = 2
        rig.emit(init=0)
    elif cause == "init_in_progress":
        rig.emit(init=2)
    elif cause == "write_failure":
        fail_temporary(monkeypatch, "write_text")
    elif cause in {"session_change", "closed"}:
        original = Path.write_text

        def prepare(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name.endswith(".command.json.tmp"):
                if cause == "session_change":
                    rig.emit(session=str(uuid4()))
                else:
                    rig.gripper.close()
            return result

        monkeypatch.setattr(Path, "write_text", prepare)
    with pytest.raises(web_gripper.GripperNotSentError) as raised:
        if cause in {"init_old_protocol", "init_already", "init_in_progress"}:
            rig.gripper.initialize()
        else:
            rig.gripper.move(525)
    assert str(raised.value).startswith("No command sent. ")
    assert not isinstance(raised.value, web_gripper.GripperLockedError)
    assert not rig.gripper.failed
    assert rig.gripper.snapshot()["locked"] is (cause == "collector_locked")
    assert rig.gripper.last_outcome is None
    assert not rig.command_path.exists()
    assert not rig.requests


@pytest.mark.parametrize("cause", ["replace", "timeout", "close"])
def test_unknown_result_raises_locked_error_and_keeps_rejecting_commands(rig, monkeypatch, cause):
    rig.emit()
    if cause == "replace":
        fail_temporary(monkeypatch, "replace")
    elif cause == "timeout":
        monkeypatch.setattr(
            web_gripper.time, "sleep", lambda delay: rig.now.__setitem__(0, rig.now[0] + 5)
        )
    else:
        monkeypatch.setattr(web_gripper.time, "sleep", lambda delay: rig.gripper.close())
    with pytest.raises(web_gripper.GripperLockedError) as raised:
        rig.gripper.move(525)
    assert not rig.command_path.with_name(rig.command_path.name + ".tmp").exists()
    message = str(raised.value)
    assert message.startswith("Control is locked. Check the gripper. ")
    suffix = {
        "replace": "Disk failure.",
        "timeout": "not confirmed",
        "close": "closed before the result was confirmed",
    }[cause]
    assert suffix in message
    assert not isinstance(raised.value, web_gripper.GripperNotSentError)
    assert rig.gripper.failed
    assert rig.gripper.closed is (cause == "close")
    assert rig.gripper.snapshot()["locked"]
    assert rig.gripper.last_outcome is None
    published = rig.command_path.read_text() if rig.command_path.exists() else None
    assert (published is None) is (cause == "replace")
    # Fresh feedback does not clear the latch. The adapter publishes nothing more.
    rig.emit()
    with pytest.raises(web_gripper.GripperLockedError) as again:
        rig.gripper.move(550)
    assert str(again.value) == message
    assert (rig.command_path.read_text() if rig.command_path.exists() else None) == published
    # Initialization cannot bypass the latch.
    rig.emit(init=0)
    with pytest.raises(web_gripper.GripperLockedError):
        rig.gripper.initialize()
    assert (rig.command_path.read_text() if rig.command_path.exists() else None) == published
    assert rig.gripper.failed
    assert rig.gripper.last_outcome is None
    socket.create_connection.assert_not_called()


def test_last_outcome_reports_only_the_current_confirmed_result(rig, monkeypatch):
    rig.emit()
    assert rig.gripper.last_outcome is None
    assert rig.gripper.move(525) is True
    assert rig.gripper.last_outcome == "arrived"
    seen_while_waiting = []

    def contact(delay):
        seen_while_waiting.append(rig.gripper.last_outcome)
        rig.acknowledge(delay)
        rig.emit(state=2, position=510)

    monkeypatch.setattr(web_gripper.time, "sleep", contact)
    assert rig.gripper.move(550) is True
    # A new command clears the previous outcome before its own result arrives.
    assert seen_while_waiting[0] is None
    assert rig.gripper.last_outcome == "contact"

    def arrive(delay):
        seen_while_waiting.append(rig.gripper.last_outcome)
        rig.acknowledge(delay)

    seen_while_waiting.clear()
    monkeypatch.setattr(web_gripper.time, "sleep", arrive)
    assert rig.gripper.move(600) is True
    assert seen_while_waiting[0] is None
    assert rig.gripper.last_outcome == "arrived"

    @contextlib.contextmanager
    def refuse():
        raise RuntimeError("The panel cancelled the command.")
        yield

    rig.gripper.publication_guard = refuse
    with pytest.raises(web_gripper.GripperNotSentError):
        rig.gripper.move(625)
    assert rig.gripper.last_outcome is None


@pytest.mark.parametrize("position", [0, 25, 500, 1000])
def test_measure_reads_new_feedback_and_returns_any_measured_position(rig, position):
    rig.emit(position=position)
    assert rig.gripper.measure() == (position, None)
    rig.emit(position=1000 - position)
    assert rig.gripper.measure() == (1000 - position, None)
    assert rig.gripper.snapshot()["position"] == 1000 - position
    assert not rig.command_path.exists()
    assert not rig.gripper.failed


@pytest.mark.parametrize(
    "case,reason",
    [
        ("absent", "stale or unavailable"),
        ("stale", "stale or unavailable"),
        ("uninitialized", "not initialized"),
        ("initializing", "initialization is in progress"),
        ("collector_locked", "Write reply lost."),
        ("adapter_locked", "Control is locked. Check the gripper."),
        ("closed", "Gripper control is closed."),
    ],
)
def test_measure_returns_no_position_and_the_reason(rig, monkeypatch, case, reason):
    if case != "absent":
        rig.emit(position=500)
    if case == "stale":
        # The sample was received, and the unchanged file then ages.
        assert rig.gripper.measure() == (500, None)
        rig.now[0] += 0.8
    elif case == "uninitialized":
        rig.emit(init=0, position=500)
    elif case == "initializing":
        rig.emit(init=2, position=500)
    elif case == "collector_locked":
        rig.sample["control"].update(locked=True, error="Write reply lost.")
        rig.emit()
    elif case == "adapter_locked":
        fail_temporary(monkeypatch, "replace")
        with pytest.raises(web_gripper.GripperLockedError):
            rig.gripper.move(525)
        rig.emit()
    elif case == "closed":
        rig.gripper.close()
    position, explanation = rig.gripper.measure()
    assert position is None
    assert isinstance(explanation, str) and reason in explanation
    assert rig.gripper.failed is (case == "adapter_locked")
    assert rig.gripper.closed is (case == "closed")


def test_closed_and_failed_report_close_and_adapter_latch_only(rig, monkeypatch):
    assert rig.gripper.closed is False
    assert rig.gripper.failed is False
    rig.sample["control"].update(locked=True, error="Write reply lost.")
    rig.emit()
    rig.gripper.poll()
    # A collector lock blocks commands, but it is not an adapter failure.
    assert rig.gripper.snapshot()["locked"]
    assert rig.gripper.failed is False
    with pytest.raises(web_gripper.GripperNotSentError):
        rig.gripper.move(525)
    assert rig.gripper.failed is False
    rig.sample["control"].update(locked=False, error=None)
    rig.emit()
    fail_temporary(monkeypatch, "replace")
    with pytest.raises(web_gripper.GripperLockedError):
        rig.gripper.move(525)
    assert rig.gripper.failed is True
    assert rig.gripper.closed is False
    rig.gripper.close()
    assert rig.gripper.closed is True
    assert rig.gripper.failed is True


def test_initialization_failures_use_the_same_typed_errors(rig, monkeypatch):
    rig.emit()
    assert rig.gripper.move(525) is True
    assert rig.gripper.last_outcome == "arrived"
    rig.emit(init=0)
    refuse = [True]

    @contextlib.contextmanager
    def guard():
        if refuse[0]:
            raise RuntimeError("The panel cancelled the command.")
        yield

    rig.gripper.publication_guard = guard
    with pytest.raises(web_gripper.GripperNotSentError) as raised:
        rig.gripper.initialize()
    assert str(raised.value) == "No command sent. The panel cancelled the command."
    assert rig.gripper.last_outcome is None
    assert not rig.gripper.failed
    refuse[0] = False
    monkeypatch.setattr(
        web_gripper.time, "sleep", lambda delay: rig.now.__setitem__(0, rig.now[0] + 15)
    )
    with pytest.raises(web_gripper.GripperLockedError) as raised:
        rig.gripper.initialize()
    assert str(raised.value).startswith("Control is locked. Check the gripper. ")
    assert rig.gripper.failed
    assert rig.gripper.last_outcome is None
