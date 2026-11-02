"""Serial behavior through an in-memory MIC-01 firmware transport only."""

import queue
import threading
import time
from concurrent.futures import Future

import pytest
import serial

from microscope.autofocus import AutofocusCancelled, AutoFocuser
from microscope.motors import MotorController
from microscope.side_capture import SideCaptureCancelled
from microscope.sim import MicroscopeSimulator

PORT = "/dev/serial/by-id/offline-microscope"


class FakePort:
    def __init__(self, *, legacy=False, version="VERSION microscope-axes 1.0.0"):
        self.sim = MicroscopeSimulator()
        self.legacy, self.version = legacy, version
        self.is_open = True
        self.queue = queue.Queue()
        self.commands = []
        self.lock = threading.RLock()
        self.hook = None
        self.read_error = None

    def emit(self, *lines):
        for line in lines:
            self.queue.put((line + "\r\n").encode("ascii"))

    def write(self, payload):
        command = payload.decode("ascii").strip()
        with self.lock:
            self.commands.append(command)
            custom = self.hook(command) if self.hook is not None else None
            if custom is not None:
                self.emit(*custom)
            elif command == "VERSION":
                self.emit("ERR UNKNOWN_COMMAND" if self.legacy else self.version)
            else:
                self.emit(*self.sim.command(command))
        return len(payload)

    def complete(self, seconds=1):
        with self.lock:
            self.emit(*self.sim.advance(seconds))

    def readline(self, size=513):
        if self.read_error:
            raise self.read_error
        try:
            return self.queue.get(timeout=0.01)
        except queue.Empty:
            return b""

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False
        self.queue.put(b"")


def asynchronous(function):
    future = Future()

    def run():
        try:
            future.set_result(function())
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return future


def until(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        assert time.monotonic() < deadline, "offline condition was not published"
        time.sleep(0.001)


@pytest.fixture
def connect():
    controllers = []

    def create(port=None):
        port = port or FakePort()
        controller = MotorController(serial_factory=lambda **kwargs: port)
        controllers.append(controller)
        controller.connect(PORT, reset_seconds=0)
        return controller, port

    yield create
    for controller in controllers:
        controller.disconnect()


def test_connect_negotiates_current_and_legacy_grammar(connect):
    current, port = connect()
    legacy, old = connect(FakePort(legacy=True))
    assert current.focus_completion and current.firmware_version == "1.0.0"
    assert not legacy.focus_completion and legacy.firmware_version is None
    assert port.commands == old.commands == ["VERSION"]


@pytest.mark.parametrize(
    "version", ["VERSION other 1.0.0", "VERSION microscope-axes 2.0.0", "ERR BAD"]
)
def test_unknown_version_is_not_legacy(version):
    port = FakePort(version=version)
    controller = MotorController(serial_factory=lambda **kwargs: port)
    with pytest.raises(RuntimeError):
        controller.connect(PORT, reset_seconds=0)
    assert not controller.connected and not port.is_open
    assert port.commands == ["VERSION"]


def test_serial_path_and_exclusive_open():
    opened = []
    port = FakePort()
    controller = MotorController(serial_factory=lambda **kwargs: opened.append(kwargs) or port)
    with pytest.raises(ValueError, match="by-id"):
        controller.connect("COM3", reset_seconds=0)
    assert not opened
    controller.connect(PORT, reset_seconds=0)
    try:
        assert opened[0]["exclusive"] is True
    finally:
        controller.disconnect()


def test_no_ordinary_command_during_unvalidated_handshake():
    port = FakePort()
    port.hook = lambda command: [] if command == "VERSION" else None
    controller = MotorController(serial_factory=lambda **kwargs: port)
    future = asynchronous(lambda: controller.connect(PORT, reset_seconds=0))
    try:
        until(lambda: "VERSION" in port.commands)
        blocked = asynchronous(controller.objective_enable)
        port.emit("VERSION microscope-axes 9.0.0")
        with pytest.raises(RuntimeError):
            future.result(1)
        with pytest.raises((RuntimeError, SideCaptureCancelled)):
            blocked.result(1)
        assert port.commands == ["VERSION"]
    finally:
        controller.disconnect()


def test_modern_focus_waits_for_real_done_not_computed_duration(connect):
    controller, port = connect()
    controller.focus_enable()
    controller.focus_speed(10000)
    future = asynchronous(lambda: controller.focus_move(1))
    until(lambda: "FOCUS MOVE 1" in port.commands)
    time.sleep(0.02)
    assert not future.done()
    port.complete(0.01)
    assert future.result(1) == "DONE FOCUS POS 1"


def test_async_focus_preserves_sampling_and_correlates_ack(connect):
    controller, port = connect()
    controller.focus_enable()

    def stale_before_ack(command):
        if command == "FOCUS MOVE 2":
            port.emit("DONE FOCUS POS 999")
        return None

    port.hook = stale_before_ack
    ticket = controller.focus_move_async(2)
    future = asynchronous(lambda: controller.wait_focus_done(ticket))
    time.sleep(0.02)
    assert not future.done()
    port.complete(0.01)
    assert future.result(1) == "DONE FOCUS POS 2"


def test_focus_cancellation_stops_without_waiting_full_move(connect):
    controller, port = connect()
    controller.focus_enable()
    cancel = threading.Event()
    future = asynchronous(lambda: controller.focus_move(10000, cancel_event=cancel))
    until(lambda: "FOCUS MOVE 10000" in port.commands)
    cancel.set()
    with pytest.raises(SideCaptureCancelled):
        future.result(0.5)
    assert "STOP FOCUS" in port.commands
    assert not port.sim.focus.moving


def test_stop_revokes_old_focus_ticket_and_new_done_is_required(connect):
    controller, port = connect()
    controller.focus_enable()
    old = controller.focus_move_async(20)
    controller.focus_stop()
    new = controller.focus_move_async(3)
    with pytest.raises(SideCaptureCancelled):
        controller.wait_focus_done(old)
    assert controller._pending_focus is new
    port.complete(0.01)
    assert controller.wait_focus_done(new) == "DONE FOCUS POS 3"


def test_objective_stop_preserves_modern_focus_ticket(connect):
    controller, port = connect()
    controller.focus_enable()
    ticket = controller.focus_move_async(2)
    controller.objective_stop()
    assert controller._pending_focus is ticket and port.sim.focus.moving
    port.complete(0.01)
    assert controller.wait_focus_done(ticket) == "DONE FOCUS POS 2"


def test_focus_stop_preserves_objective_completion_wait(connect):
    controller, port = connect()
    controller.objective_enable()
    future = asynchronous(lambda: controller.send_and_wait("OBJ MOVE 2", "DONE OBJ POS "))
    until(lambda: "OBJ MOVE 2" in port.commands)
    controller.focus_stop()
    assert not future.done() and port.sim.objective.moving
    port.complete(0.01)
    assert future.result(1) == "DONE OBJ POS 2"


def test_legacy_computed_wait_survives_unrelated_pending_stop(connect):
    controller, port = connect(FakePort(legacy=True))
    controller.focus_enable()
    controller.focus_speed(100)
    ticket = controller.focus_move_async(3)
    port.hook = lambda command: [] if command == "STOP OBJ" else None
    stop = asynchronous(controller.objective_stop)
    until(lambda: "STOP OBJ" in port.commands)
    started = time.monotonic()
    assert controller.wait_focus_done(ticket) is None
    assert time.monotonic() >= ticket.started + 0.03
    assert time.monotonic() - started < 0.2
    port.emit("OK STOP OBJ")
    stop.result(1)


@pytest.mark.parametrize(
    "operation,command,reply",
    [
        (lambda c: c.set_microstep(1), "MICRO 1", "OK MICRO 16"),
        (lambda c: c.objective_move(1), "OBJ MOVE 1", "OK OBJ MOVE 10"),
        (lambda c: c.objective_speed(1), "OBJ SPEED 1", "OK OBJ SPEED 10"),
        (lambda c: c.focus_speed(1), "FOCUS SPEED 1", "OK FOCUS SPEED 10"),
        (lambda c: c.focus_move_async(1), "FOCUS MOVE 1", "OK FOCUS MOVE 10"),
        (lambda c: c.stop_all(), "STOP ALL", "OK STOP ALLjunk"),
    ],
)
def test_wrong_value_acknowledgements_fault_instead_of_succeeding(
    connect, operation, command, reply
):
    controller, port = connect()
    port.hook = lambda value: [reply] if value == command else None
    with pytest.raises(RuntimeError):
        operation(controller)
    assert controller.fault
    with pytest.raises(RuntimeError):
        controller.objective_enable()


def test_reader_error_wakes_pending_completion(connect):
    controller, port = connect()
    controller.focus_enable()
    ticket = controller.focus_move_async(50)
    future = asynchronous(lambda: controller.wait_focus_done(ticket))
    port.read_error = serial.SerialException("offline unplug")
    with pytest.raises(RuntimeError, match="reader failed"):
        future.result(0.5)
    assert controller.fault


def test_partial_reply_does_not_confirm_until_newline(connect):
    controller, port = connect()
    port.hook = lambda command: [] if command == "OBJ ENABLE" else None
    future = asynchronous(controller.objective_enable)
    until(lambda: "OBJ ENABLE" in port.commands)
    port.queue.put(b"OK OBJ ENABLED")
    time.sleep(0.02)
    assert not future.done()
    port.queue.put(b"\r\n")
    assert future.result(1) == "OK OBJ ENABLED"


def test_old_reader_cannot_enter_new_connection():
    old, new = FakePort(), FakePort()
    release, entered = threading.Event(), threading.Event()
    original = old.readline

    def blocked(size=513):
        entered.set()
        release.wait(2)
        return original(size)

    controller = MotorController(serial_factory=lambda **kwargs: old)
    controller.connect(PORT, reset_seconds=0)
    old.readline = blocked
    until(entered.is_set)
    controller._serial_factory = lambda **kwargs: new
    try:
        with pytest.raises(RuntimeError, match="still stopping"):
            controller.connect(PORT, reset_seconds=0)
        assert new.commands == [] and not controller.connected
        release.set()
        until(lambda: not controller.reader_thread.is_alive())
        controller.connect(PORT, reset_seconds=0)
        controller.objective_enable()
        assert new.commands == ["VERSION", "OBJ ENABLE"]
    finally:
        release.set()
        controller.disconnect()


def test_autofocus_translates_cancellation_during_blocked_motor_completion(connect):
    controller, port = connect()
    controller.focus_enable()
    cancel = threading.Event()
    focuser = AutoFocuser(object(), controller, cancel_event=cancel)
    future = asynchronous(lambda: focuser._move_to(10000))
    until(lambda: "FOCUS MOVE 10000" in port.commands)
    cancel.set()
    with pytest.raises(AutofocusCancelled):
        future.result(0.5)
    assert focuser.position == 0 and not port.sim.focus.moving


@pytest.mark.parametrize(
    "operation,axis",
    [
        (lambda motor: motor.objective_enable(), "OBJ"),
        (lambda motor: motor.send_and_wait("OBJ ENABLE", "OK OBJ ENABLED"), "OBJ"),
        (lambda motor: motor.focus_move_async(1), "FOCUS"),
        (lambda motor: motor.focus_speed(100), "FOCUS"),
        (lambda motor: motor.set_microstep(1), "ALL"),
    ],
)
@pytest.mark.parametrize("revoke", ["reconnect", "stop"])
def test_queued_commands_cannot_resume_after_authority_revocation(
    connect, monkeypatch, operation, axis, revoke
):
    controller, old_port = connect()
    controller.focus_enable()
    captured = threading.Event()
    admission = controller._admission_token
    owner = threading.current_thread()

    def snapshot(command, **kwargs):
        token = admission(command, **kwargs)
        if threading.current_thread() is not owner:
            captured.set()
        return token

    monkeypatch.setattr(controller, "_admission_token", snapshot)
    with controller._transaction:
        future = asynchronous(lambda: operation(controller))
        assert captured.wait(1)
        if revoke == "reconnect":
            new_port = FakePort()
            controller._serial_factory = lambda **_: new_port
            controller.connect(PORT, reset_seconds=0)
        else:
            controller._stop("STOP " + axis)
        before = list(controller.ser.commands)
    with pytest.raises(SideCaptureCancelled, match="Queued"):
        future.result(1)
    assert controller.ser.commands == before
    if revoke == "reconnect":
        assert new_port.commands == ["VERSION"]


def test_queued_command_survives_unrelated_axis_stop(connect, monkeypatch):
    controller, port = connect()
    captured = threading.Event()
    admission = controller._admission_token

    def snapshot(command, **kwargs):
        token = admission(command, **kwargs)
        captured.set()
        return token

    monkeypatch.setattr(controller, "_admission_token", snapshot)
    with controller._transaction:
        future = asynchronous(controller.objective_enable)
        assert captured.wait(1)
        controller.focus_stop()
    assert future.result(1) == "OK OBJ ENABLED"
    assert port.commands[-2:] == ["STOP FOCUS", "OBJ ENABLE"]


def test_command_admission_is_closed_during_version_validation(monkeypatch):
    from microscope import motors

    port = FakePort()
    controller = MotorController(serial_factory=lambda **_: port)
    validating, release = threading.Event(), threading.Event()
    fullmatch = motors.re.fullmatch

    def validate(pattern, value):
        if pattern.startswith("VERSION microscope-axes"):
            validating.set()
            assert release.wait(1)
        return fullmatch(pattern, value)

    monkeypatch.setattr(motors.re, "fullmatch", validate)
    future = asynchronous(lambda: controller.connect(PORT, reset_seconds=0))
    try:
        assert validating.wait(1)
        with pytest.raises(RuntimeError, match="handshake"):
            controller.objective_enable()
        assert port.commands == ["VERSION"]
        release.set()
        assert future.result(1) is controller
        assert controller.objective_enable() == "OK OBJ ENABLED"
    finally:
        release.set()
        controller.disconnect()


def reference_controller(connect, *, home=False):
    port = FakePort()
    if home:
        port.sim = MicroscopeSimulator(home_switch=True, home_direction=-1, home_max_steps=100)
    controller, port = connect(port)
    controller.set_microstep(4)
    controller.focus_enable()
    return controller, port


def test_full_status_is_fresh_complete_and_immutable(connect):
    from dataclasses import FrozenInstanceError

    controller, port = reference_controller(connect)
    controller.focus_zero()
    controller.focus_limits(-20, 30)
    first = controller.read_status()
    assert first.session == controller.session
    assert first.reference_epoch == controller.reference_epoch
    assert first.microstep == 4 and first.focus.enabled and not first.objective.enabled
    assert first.zeroed and first.limited and (first.minimum, first.maximum) == (-20, 30)
    assert first.focus.position == 0 and not first.homing
    with pytest.raises(FrozenInstanceError):
        first.zeroed = False
    with pytest.raises(FrozenInstanceError):
        first.focus.position = 2
    ticket = controller.focus_move_async(4)
    port.complete(0.02)
    assert controller.wait_focus_done(ticket) == "DONE FOCUS POS 4"
    second = controller.read_status()
    assert second.focus.position == 4 and second.sequence > first.sequence
    assert second.observed_at >= first.observed_at
    assert controller.status() == "MICRO=4"


def test_status_bypasses_long_move_wait_without_consuming_done(connect):
    controller, port = reference_controller(connect)
    ticket = controller.focus_move_async(10)
    completion = asynchronous(lambda: controller.wait_focus_done(ticket))
    sample = controller.read_status(timeout=0.2)
    assert sample.focus.moving and sample.focus.remaining == 10 and not completion.done()
    port.complete(0.1)
    assert completion.result(1) == "DONE FOCUS POS 10"
    assert not controller.read_status().focus.moving


def test_status_accepts_unsigned_remaining_for_minimum_signed_move(connect):
    controller, port = reference_controller(connect)
    ticket = controller.focus_move_async(-(2**31))
    assert controller.read_status().focus.remaining == 2**31
    controller.focus_stop()
    with pytest.raises(SideCaptureCancelled):
        controller.wait_focus_done(ticket)


@pytest.mark.parametrize(
    "row,replacement",
    [
        (0, "MICRO=3"),
        (1, "OBJ: enabled=2 moving=0 speed=400 remaining=0 pos=0"),
        (1, "OBJ: enabled=0 moving=0 speed=0 remaining=0 pos=0"),
        (1, "OBJ: enabled=1 moving=1 speed=400 remaining=2147483649 pos=0"),
        (2, "FOCUS: enabled=1 moving=0 speed=800 remaining=0 pos=2147483648"),
        (3, "FOCUS_REF: zeroed=0 limited=1 min=-10 max=10 homing=0"),
        (3, "FOCUS_REF: zeroed=1 limited=1 min=10 max=-10 homing=0"),
        (3, "FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=1"),
    ],
)
def test_malformed_status_faults_without_using_cached_fields(connect, row, replacement):
    controller, port = reference_controller(connect)
    old = controller.read_status()
    lines = port.sim.status()
    lines[row] = replacement
    port.hook = lambda command: lines if command == "STATUS" else None
    with pytest.raises(RuntimeError):
        controller.read_status()
    assert controller.fault and controller.reference_epoch > old.reference_epoch
    before = list(port.commands)
    with pytest.raises(RuntimeError):
        controller.read_status()
    assert port.commands == before


@pytest.mark.parametrize("rows", [(0, 2, 1, 3), (0, 0, 1, 2, 3), (0, 1, 2)])
def test_status_rejects_reordered_duplicate_or_missing_rows(connect, rows):
    controller, port = reference_controller(connect)
    lines = port.sim.status()
    port.hook = lambda command: [lines[i] for i in rows] if command == "STATUS" else None
    with pytest.raises(RuntimeError):
        controller.read_status(timeout=0.03)
    assert controller.fault


def test_status_drain_discards_group_revoked_before_first_byte(connect):
    controller, port = reference_controller(connect)
    original = port.sim.status()
    count = 0

    def status_reply(command):
        nonlocal count
        if command == "STATUS":
            count += 1
            if count == 1:
                return []
        return None

    port.hook = status_reply
    stale = asynchronous(lambda: controller.read_status(timeout=0.5))
    until(lambda: count == 1)
    controller.focus_zero()
    current = asynchronous(lambda: controller.read_status(timeout=0.5))
    time.sleep(0.02)
    assert count == 1 and not current.done()
    port.emit(*original)
    with pytest.raises(SideCaptureCancelled):
        stale.result(1)
    sample = current.result(1)
    assert sample.zeroed and sample.reference_epoch == controller.reference_epoch
    assert count == 2 and controller.fault is None


def test_revoked_status_missing_tail_faults_instead_of_reusing_late_group(connect):
    controller, port = reference_controller(connect)
    port.hook = lambda command: [] if command == "STATUS" else None
    future = asynchronous(lambda: controller.read_status(timeout=0.05))
    until(lambda: "STATUS" in port.commands)
    controller.focus_zero()
    with pytest.raises(RuntimeError, match="incomplete"):
        future.result(1)
    port.emit(*port.sim.status())
    with pytest.raises(RuntimeError):
        controller.read_status()


@pytest.mark.parametrize("during", ["status", "focus"])
def test_ready_revokes_pending_reference_and_requires_explicit_reconnect(connect, during):
    controller, port = reference_controller(connect)
    controller.focus_zero()
    controller.focus_limits(-50, 50)
    before = controller.read_status()
    if during == "status":
        port.hook = lambda command: [] if command == "STATUS" else None
        future = asynchronous(controller.read_status)
        until(lambda: port.commands.count("STATUS") == 2)
    else:
        ticket = controller.focus_move_async(5)
        future = asynchronous(lambda: controller.wait_focus_done(ticket))
    port.emit(*port.sim.reset())
    with pytest.raises(SideCaptureCancelled):
        future.result(1)
    until(lambda: controller.session != before.session)
    assert controller.reference_epoch > before.reference_epoch
    assert controller.fault and controller._pending_focus is None
    commands = list(port.commands)
    with pytest.raises(RuntimeError, match="reset"):
        controller.focus_enable()
    with pytest.raises(RuntimeError, match="reset"):
        controller.read_status()
    assert port.commands == commands
    new = FakePort()
    controller._serial_factory = lambda **_: new
    controller.connect(PORT, reset_seconds=0)
    assert not controller.read_status().zeroed and controller.fault is None


def test_legacy_status_api_remains_available_without_reference_authority(connect):
    controller, port = connect(FakePort(legacy=True))
    port.hook = lambda command: port.sim.status()[:3] if command == "STATUS" else None
    assert controller.status() == "MICRO=1"
    before = list(port.commands)
    with pytest.raises(RuntimeError, match="reference status"):
        controller.read_status()
    assert port.commands == before and controller.fault is None


def test_reference_epoch_changes_for_reference_writes_faults_and_disconnect(connect):
    controller, port = reference_controller(connect)
    epoch = controller.reference_epoch
    controller.set_microstep(4)
    assert controller.reference_epoch == epoch
    for action in (
        controller.focus_zero,
        lambda: controller.focus_limits(-10, 10),
        controller.focus_disable,
    ):
        action()
        assert controller.reference_epoch > epoch
        epoch = controller.reference_epoch
    controller.set_microstep(2)
    assert controller.reference_epoch > epoch
    epoch = controller.reference_epoch
    port.read_error = serial.SerialException("offline unplug")
    until(lambda: controller.fault is not None)
    assert controller.reference_epoch > epoch
    epoch = controller.reference_epoch
    controller.disconnect()
    assert controller.reference_epoch > epoch


def test_home_waits_for_own_done_then_confirms_fresh_reference(connect):
    controller, port = reference_controller(connect, home=True)

    def stale_done(command):
        if command == "FOCUS HOME":
            port.emit("DONE FOCUS HOME")
        return None

    port.hook = stale_done
    future = asynchronous(lambda: controller.focus_home(timeout=1))
    until(lambda: "FOCUS HOME" in port.commands)
    time.sleep(0.02)
    assert not future.done()
    port.sim.set_home_switch(True)
    port.complete(0.03)
    status = future.result(1)
    assert status.zeroed and not status.limited and not status.homing
    assert status.focus.position == 0 and not status.focus.moving
    assert port.commands[-1] == "STATUS" and "FOCUS ZERO" not in port.commands


def test_home_rejection_never_falls_back_to_zero(connect):
    controller, port = reference_controller(connect)
    with pytest.raises(RuntimeError, match="NO_SWITCH"):
        controller.focus_home(timeout=0.2)
    assert "FOCUS ZERO" not in port.commands and not port.sim.zeroed


@pytest.mark.parametrize("cancelled", [False, True])
def test_home_timeout_and_cancel_cannot_establish_zero(connect, cancelled):
    controller, port = reference_controller(connect, home=True)
    event = threading.Event()
    future = asynchronous(lambda: controller.focus_home(timeout=0.06, cancel_event=event))
    until(lambda: "FOCUS HOME" in port.commands)
    if cancelled:
        event.set()
    with pytest.raises(SideCaptureCancelled if cancelled else RuntimeError):
        future.result(1)
    assert not port.sim.zeroed and "FOCUS ZERO" not in port.commands
    if cancelled:
        assert "STOP FOCUS" in port.commands and not port.sim.homing
    else:
        assert controller.fault
        controller.safe_stop_disable(timeout=0.1)
        assert controller.fault and not port.sim.homing


@pytest.mark.parametrize(
    "operation",
    [
        lambda c, e: c.objective_enable(cancel_event=e),
        lambda c, e: c.objective_disable(cancel_event=e),
        lambda c, e: c.objective_move(1, cancel_event=e),
        lambda c, e: c.objective_speed(100, cancel_event=e),
        lambda c, e: c.focus_enable(cancel_event=e),
        lambda c, e: c.focus_disable(cancel_event=e),
        lambda c, e: c.focus_speed(100, cancel_event=e),
        lambda c, e: c.set_microstep(4, cancel_event=e),
        lambda c, e: c.focus_zero(cancel_event=e),
        lambda c, e: c.focus_limits(-5, 5, cancel_event=e),
        lambda c, e: c.focus_home(cancel_event=e),
        lambda c, e: c.send_and_wait("STOP OBJ", "OK STOP OBJ", cancel_event=e),
    ],
)
def test_cancelled_mutations_do_not_emit_commands_or_cleanup_for_unissued_work(connect, operation):
    controller, port = connect()
    event = threading.Event()
    event.set()
    with pytest.raises(SideCaptureCancelled):
        operation(controller, event)
    assert port.commands == ["VERSION"] and controller.fault is None


def test_cancelled_queued_enable_does_not_reenergize_or_stop_new_work(connect, monkeypatch):
    controller, port = connect()
    captured, cancel = threading.Event(), threading.Event()
    admission = controller._admission_token

    def snapshot(command, **kwargs):
        value = admission(command, **kwargs)
        captured.set()
        return value

    monkeypatch.setattr(controller, "_admission_token", snapshot)
    with controller._transaction:
        future = asynchronous(lambda: controller.focus_enable(cancel_event=cancel))
        assert captured.wait(1)
        cancel.set()
    with pytest.raises(SideCaptureCancelled):
        future.result(1)
    assert port.commands == ["VERSION"]


def test_safe_cleanup_confirms_all_outputs_and_keeps_existing_fault(connect):
    controller, port = reference_controller(connect)
    controller.objective_enable()
    controller.focus_zero()
    controller.focus_limits(-20, 20)
    before = controller.reference_epoch
    controller._fail("Earlier command outcome is unknown")
    assert controller.safe_stop_disable(timeout=0.1) == (
        "OK STOP ALL",
        "OK OBJ DISABLED",
        "OK FOCUS DISABLED",
    )
    assert port.commands[-3:] == ["STOP ALL", "OBJ DISABLE", "FOCUS DISABLE"]
    assert not port.sim.objective.enabled and not port.sim.focus.enabled and not port.sim.zeroed
    assert controller.reference_epoch > before
    assert controller.fault == "Earlier command outcome is unknown"
    with pytest.raises(RuntimeError, match="Earlier"):
        controller.objective_enable()


@pytest.mark.parametrize("failed", ["STOP ALL", "OBJ DISABLE", "FOCUS DISABLE"])
def test_safety_cleanup_attempts_both_disables_despite_wrong_ack(connect, failed):
    controller, port = reference_controller(connect)
    controller.objective_enable()

    def wrong(command):
        if command == failed:
            replies = port.sim.command(command)
            return [replies[0] + "junk"]
        return None

    port.hook = wrong
    with pytest.raises(RuntimeError):
        controller.safe_stop_disable(timeout=0.1)
    assert port.commands[-3:] == ["STOP ALL", "OBJ DISABLE", "FOCUS DISABLE"]
    assert not port.sim.objective.enabled and not port.sim.focus.enabled
    assert controller.fault and not controller._cleaning


def test_cleanup_admission_stays_closed_between_stop_and_both_disables(connect):
    controller, port = reference_controller(connect)
    controller.objective_enable()

    def delay(command):
        if command in ("STOP ALL", "OBJ DISABLE"):
            port.sim.command(command)
            return []
        return None

    port.hook = delay
    future = asynchronous(lambda: controller.safe_stop_disable(timeout=0.5))
    until(lambda: "STOP ALL" in port.commands)
    with pytest.raises(RuntimeError, match="stop is pending"):
        controller.objective_enable()
    port.emit("OK STOP ALL")
    until(lambda: "OBJ DISABLE" in port.commands)
    assert not controller._stopping and controller._cleaning
    with pytest.raises(RuntimeError, match="stop is pending"):
        controller.focus_enable()
    port.emit("OK OBJ DISABLED")
    assert future.result(1)[-1] == "OK FOCUS DISABLED"
    assert not controller._cleaning and not controller.fault


def test_overlapping_cleanup_requests_keep_a_continuous_admission_gate(connect):
    controller, port = connect()
    port.hook = lambda command: [] if command == "STOP ALL" else None
    first = asynchronous(lambda: controller.safe_stop_disable(timeout=0.5))
    until(lambda: port.commands.count("STOP ALL") == 1)
    second = asynchronous(lambda: controller.safe_stop_disable(timeout=0.5))
    until(lambda: controller._cleaning == 2)
    port.emit("OK STOP ALL")
    first.result(1)
    until(lambda: port.commands.count("STOP ALL") == 2)
    assert controller._cleaning == 1
    with pytest.raises(RuntimeError, match="stop is pending"):
        controller.objective_enable()
    port.emit("OK STOP ALL")
    second.result(1)
    assert not controller._cleaning and controller.fault is None


def test_old_cleanup_cannot_disable_reconnected_session(connect):
    controller, old = connect()
    old.hook = lambda command: [] if command == "STOP ALL" else None
    future = asynchronous(lambda: controller.safe_stop_disable(timeout=0.5))
    until(lambda: "STOP ALL" in old.commands)
    new = FakePort()
    controller._serial_factory = lambda **_: new
    controller.connect(PORT, reset_seconds=0)
    with pytest.raises(RuntimeError, match="old session"):
        future.result(1)
    assert new.commands == ["VERSION"]
    assert not controller.fault and not controller._cleaning
    controller.objective_enable()
    assert new.sim.objective.enabled


def test_ready_revokes_already_queued_mutations(connect, monkeypatch):
    controller, port = connect()
    captured = threading.Event()
    admission = controller._admission_token

    def snapshot(command, **kwargs):
        token = admission(command, **kwargs)
        captured.set()
        return token

    monkeypatch.setattr(controller, "_admission_token", snapshot)
    with controller._transaction:
        future = asynchronous(controller.objective_enable)
        assert captured.wait(1)
        old_session = controller.session
        port.emit(*port.sim.reset())
        until(lambda: controller.session != old_session)
    with pytest.raises(SideCaptureCancelled):
        future.result(1)
    assert port.commands == ["VERSION"]


def test_serial_thread_start_failure_closes_transport_without_joining_unstarted_thread(monkeypatch):
    port = FakePort()
    controller = MotorController(serial_factory=lambda **_: port)

    def fail(_thread):
        raise RuntimeError("offline thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="offline thread start failure"):
        controller.connect(PORT, reset_seconds=0)
    assert not port.is_open and not controller.connected and controller.reader_thread is None
    controller.disconnect()
