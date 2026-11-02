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
