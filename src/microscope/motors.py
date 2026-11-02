"""Headless dual-axis serial controller, adapted from MotorController.

Connecting is explicit. Ordinary transactions serialize, but stop bypasses a
long completion wait. Unknown outcomes latch a fault until explicit reconnect.
"""

import math
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

import serial

from .side_capture import SideCaptureCancelled

BAUDRATE = 115200


class CommandRejected(RuntimeError):
    """The firmware refused a command without accepting it."""


@dataclass(frozen=True)
class FocusMove:
    sequence: int
    generation: tuple[int, ...]
    steps: int
    started: float
    started_wall: float
    duration: float
    deadline: float


class MotorController:
    """Explicit serial lifecycle and acknowledgement/completion waits."""

    def __init__(self, *, serial_factory=None):
        self.ser = None
        self.reader_thread = None
        self.stop_reader = threading.Event()
        self.rx_queue = queue.Queue(maxsize=256)
        self.send_lock = threading.Lock()
        self.response_condition = threading.Condition()
        self.response_sequence = 0
        self.responses = deque(maxlen=128)
        self._transaction = threading.RLock()
        self._lifecycle = threading.RLock()
        self._stop_lock = threading.Lock()
        self._serial_factory = serial_factory or serial.Serial
        self._generation = 0
        self._stop_epoch = {"OBJ": 0, "FOCUS": 0}
        self._stopping = False
        self._fault = None
        self._pending_focus = None
        self._focus_speed = 800
        self.firmware_version = None
        self.focus_completion = False
        self._validated = False

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    @property
    def fault(self):
        return self._fault

    def connect(self, port=None, baudrate=BAUDRATE, *, reset_seconds=1.8):
        """Open a stable Linux device and probe VERSION. Tests inject the transport."""
        port = os.environ.get("MG400_MICROSCOPE_SERIAL", "") if port is None else str(port)
        if not re.fullmatch(r"/dev/serial/by-id/[A-Za-z0-9_.:+-]+", port):
            raise ValueError("Microscope serial port must be a /dev/serial/by-id/ path")
        if not math.isfinite(reset_seconds) or reset_seconds < 0:
            raise ValueError("reset_seconds must be finite and nonnegative")
        with self._lifecycle:
            self.disconnect()
            try:
                self.ser = self._serial_factory(
                    port=port, baudrate=baudrate, timeout=0.1, write_timeout=0.5, exclusive=True
                )
                time.sleep(reset_seconds)
                self.ser.reset_input_buffer()
                with self.response_condition:
                    self._generation += 1
                    self._fault = None
                    self._stopping = False
                    self._pending_focus = None
                    self.responses.clear()
                    self._focus_speed = 800
                    self.focus_completion = False
                    self._validated = False
                    self.firmware_version = None
                self.stop_reader = threading.Event()
                self.reader_thread = threading.Thread(
                    target=self._reader_loop,
                    args=(self.ser, self.stop_reader),
                    name="microscope-serial",
                    daemon=True,
                )
                self.reader_thread.start()
                try:
                    reply = self.send_and_wait("VERSION", "VERSION ", timeout=2.0, _handshake=True)
                except CommandRejected as error:
                    if str(error) != "VERSION: ERR UNKNOWN_COMMAND":
                        raise
                    # Only the known legacy grammar permits a computed wait.
                else:
                    match = re.fullmatch(r"VERSION microscope-axes (1\.\d+\.\d+)", reply)
                    if match is None:
                        raise RuntimeError("Unsupported microscope firmware version")
                    self.firmware_version = match[1]
                    self.focus_completion = True
                with self.response_condition:
                    self._validated = True
            except BaseException:
                self.disconnect()
                raise
        return self

    def disconnect(self):
        with self._lifecycle:
            with self.response_condition:
                self._generation += 1
                self._pending_focus = None
                self._validated = False
                self.stop_reader.set()
                port, self.ser = self.ser, None
                self.response_condition.notify_all()
            if port is not None:
                port.close()
            thread = self.reader_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
                if thread.is_alive():
                    raise RuntimeError("Previous microscope reader is still stopping")
            self.reader_thread = None

    def _fail(self, message):
        with self.response_condition:
            self._fault = message
            self.response_condition.notify_all()

    def _reader_loop(self, port, stop):
        # Keep transport identity and partial lines private to this session.
        pending = bytearray()
        try:
            while not stop.is_set():
                raw = port.readline(513)
                if not raw:
                    continue
                pending.extend(raw)
                while b"\n" in pending:
                    boundary = pending.index(10)
                    if boundary > 512:
                        raise RuntimeError("Microscope reply is too long")
                    text = pending[:boundary].decode("ascii", errors="strict").strip()
                    del pending[: boundary + 1]
                    if not text:
                        continue
                    with self.response_condition:
                        if port is not self.ser or stop.is_set():
                            return
                        self.response_sequence += 1
                        self.responses.append((self.response_sequence, text))
                        self.response_condition.notify_all()
                    try:
                        self.rx_queue.put_nowait(text)
                    except queue.Full:
                        try:
                            self.rx_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self.rx_queue.put_nowait(text)
                        except queue.Full:
                            pass
                if len(pending) > 512:
                    raise RuntimeError("Microscope reply is too long")
        except Exception:
            with self.response_condition:
                if port is self.ser and not stop.is_set():
                    self._fault = "Microscope serial reader failed"
                    self.response_condition.notify_all()

    @staticmethod
    def _command(command):
        if not isinstance(command, str) or not command or len(command) > 95:
            raise ValueError("Command must contain 1..95 ASCII characters")
        if any(ord(c) < 32 or ord(c) > 126 for c in command):
            raise ValueError("Command must be one printable ASCII line")
        command = command.strip().upper()
        if not command:
            raise ValueError("Command must not be empty")
        return command

    def _write(self, command):
        with self.send_lock:
            if not self.connected:
                raise RuntimeError("Arduino is not connected")
            payload = (command + "\n").encode("ascii")
            try:
                if self.ser.write(payload) != len(payload):
                    raise RuntimeError("Short microscope serial write")
                self.ser.flush()
            except Exception:
                self._fail("Microscope serial write outcome is unknown")
                raise

    def _allowed(self, *, pending=False, handshake=False):
        if not self.connected:
            raise RuntimeError("Arduino is not connected")
        if self._fault:
            raise RuntimeError(self._fault)
        if not self._validated and not handshake:
            raise RuntimeError("Microscope firmware handshake is not complete")
        if self._stopping:
            raise RuntimeError("Microscope stop is pending")
        if self._pending_focus is not None and not pending:
            raise RuntimeError("A focus move is still pending")

    def _generation_token(self, command):
        words = command.split()
        axis = words[1] if words[0] == "STOP" else words[0]
        if axis in self._stop_epoch:
            return (self._generation, self._stop_epoch[axis])
        return (self._generation, self._stop_epoch["OBJ"], self._stop_epoch["FOCUS"])

    def _wait_reply(
        self,
        sequence,
        generation,
        prefix,
        command,
        deadline,
        cancel_event=None,
        *,
        allow_fault=False,
        with_sequence=False,
    ):
        with self.response_condition:
            while True:
                if generation != self._generation_token(command):
                    raise SideCaptureCancelled("Microscope command was stopped")
                if cancel_event is not None and cancel_event.is_set():
                    raise SideCaptureCancelled("Microscope command was stopped")
                if not self.connected:
                    raise RuntimeError("Arduino disconnected during command")
                if self._fault and not allow_fault:
                    raise RuntimeError(self._fault)
                if self.responses and self.responses[0][0] > sequence + 1:
                    self._fault = "Microscope response history was lost"
                    raise RuntimeError(self._fault)
                for number, line in self.responses:
                    if number <= sequence:
                        continue
                    if line.startswith(("ERR", "ERROR")):
                        raise CommandRejected(f"{command}: {line}")
                    if line.startswith(prefix):
                        return (number, line) if with_sequence else line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fault = "Microscope command completion is unknown"
                    raise RuntimeError(self._fault)
                self.response_condition.wait(min(0.05, remaining))

    def send_and_wait(
        self,
        command,
        response_prefix,
        *,
        timeout=5.0,
        cancel_event=None,
        _handshake=False,
    ):
        command = self._command(command)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if _handshake and command != "VERSION":
            raise ValueError("Only VERSION is permitted during the handshake")
        if command.startswith("STOP "):
            return self._stop(command, timeout)
        generation = None
        try:
            with self._transaction:
                with self.response_condition:
                    self._allowed(handshake=_handshake)
                    if cancel_event is not None and cancel_event.is_set():
                        raise SideCaptureCancelled("Microscope command was stopped")
                    sequence, generation = self.response_sequence, self._generation_token(command)
                    self._write(command)
                deadline = time.monotonic() + timeout
                if " MOVE " in command and response_prefix.startswith("DONE "):
                    sequence, ack = self._wait_reply(
                        sequence,
                        generation,
                        "OK " + command,
                        command,
                        deadline,
                        cancel_event,
                        with_sequence=True,
                    )
                    if ack != "OK " + command:
                        with self.response_condition:
                            if generation == self._generation_token(command):
                                self._fail("Malformed move acknowledgement")
                        raise RuntimeError("Malformed move acknowledgement")
                return self._wait_reply(
                    sequence,
                    generation,
                    response_prefix,
                    command,
                    deadline,
                    cancel_event,
                )
        except SideCaptureCancelled:
            if generation is not None and cancel_event is not None and cancel_event.is_set():
                self._stop("STOP ALL", expected_session=generation[0])
            raise

    def send(self, command, *, cancel_event=None):
        """Send an acknowledged command; low-level callers use send_and_wait."""
        command = self._command(command)
        prefix = "OK " + command.split()[0] + " "
        return self.send_and_wait(command, prefix, cancel_event=cancel_event)

    def _stop(self, command, timeout=5.0, *, expected_session=None):
        if command not in ("STOP OBJ", "STOP FOCUS", "STOP ALL"):
            raise ValueError("Unknown stop target")
        with self._stop_lock:
            generation = None
            try:
                with self.response_condition:
                    if expected_session is not None and expected_session != self._generation:
                        return None
                    for axis in ("OBJ", "FOCUS"):
                        if command in (f"STOP {axis}", "STOP ALL"):
                            self._stop_epoch[axis] += 1
                    if command in ("STOP FOCUS", "STOP ALL"):
                        self._pending_focus = None
                    self._stopping = True
                    self.response_condition.notify_all()
                    sequence, generation = self.response_sequence, self._generation_token(command)
                    self._write(command)
                reply = self._wait_reply(
                    sequence,
                    generation,
                    "OK " + command,
                    command,
                    time.monotonic() + timeout,
                    allow_fault=True,
                )
                with self.response_condition:
                    if generation != self._generation_token(command):
                        raise SideCaptureCancelled("Stop acknowledgement is from an old session")
                    if reply != "OK " + command:
                        raise RuntimeError("Malformed microscope stop acknowledgement")
                return reply
            except BaseException:
                with self.response_condition:
                    if generation is not None and generation[0] == self._generation:
                        self._fault = "Microscope stop is unconfirmed"
                raise
            finally:
                with self.response_condition:
                    if generation is not None and generation[0] == self._generation:
                        self._stopping = False
                    self.response_condition.notify_all()

    @staticmethod
    def _steps(value):
        if type(value) is not int or not -(2**31) <= value < 2**31:
            raise ValueError("steps must be a signed 32-bit integer")
        return value

    def _ack(self, command, expected):
        with self._transaction:
            with self.response_condition:
                generation = self._generation_token(command)
            reply = self.send_and_wait(command, expected)
            with self.response_condition:
                if generation != self._generation_token(command):
                    raise SideCaptureCancelled("Acknowledgement is from an old session")
                if reply != expected:
                    self._fail("Malformed microscope acknowledgement")
                    raise RuntimeError("Malformed microscope acknowledgement")
            return reply

    def objective_enable(self):
        return self._ack("OBJ ENABLE", "OK OBJ ENABLED")

    def objective_disable(self):
        return self._ack("OBJ DISABLE", "OK OBJ DISABLED")

    def objective_move(self, steps):
        steps = self._steps(steps)
        return self._ack(f"OBJ MOVE {steps}", f"OK OBJ MOVE {steps}")

    def objective_speed(self, steps_per_second):
        speed = self._steps(steps_per_second)
        return self._ack(f"OBJ SPEED {speed}", f"OK OBJ SPEED {speed}")

    def objective_stop(self):
        return self._stop("STOP OBJ")

    def focus_enable(self):
        return self._ack("FOCUS ENABLE", "OK FOCUS ENABLED")

    def focus_disable(self):
        return self._ack("FOCUS DISABLE", "OK FOCUS DISABLED")

    def focus_move_async(self, steps, *, cancel_event=None):
        """Start one focus move and return its completion ticket."""
        steps = self._steps(steps)
        with self._transaction:
            with self.response_condition:
                self._allowed()
                if cancel_event is not None and cancel_event.is_set():
                    raise SideCaptureCancelled("Microscope command was stopped")
                started = time.monotonic()
                started_wall = time.time()
                duration = abs(steps) / self._focus_speed
                ticket = FocusMove(
                    self.response_sequence,
                    self._generation_token("FOCUS MOVE"),
                    steps,
                    started,
                    started_wall,
                    duration,
                    started + max(5.0, duration + 3.0),
                )
                self._pending_focus = ticket
                self._write(f"FOCUS MOVE {steps}")
            try:
                ack_sequence, reply = self._wait_reply(
                    ticket.sequence,
                    ticket.generation,
                    f"OK FOCUS MOVE {steps}",
                    "FOCUS MOVE",
                    ticket.deadline,
                    cancel_event,
                    with_sequence=True,
                )
                with self.response_condition:
                    if ticket.generation != self._generation_token("FOCUS MOVE"):
                        raise SideCaptureCancelled("Focus acknowledgement is from an old session")
                    if reply != f"OK FOCUS MOVE {steps}":
                        self._fail("Malformed focus acknowledgement")
                        raise RuntimeError("Malformed focus acknowledgement")
                    ticket = replace(ticket, sequence=ack_sequence)
                    self._pending_focus = ticket
            except BaseException:
                with self.response_condition:
                    if self._pending_focus is ticket:
                        self._pending_focus = None
                if cancel_event is not None and cancel_event.is_set():
                    self._stop("STOP FOCUS", expected_session=ticket.generation[0])
                raise
            return ticket

    def wait_focus_done(self, ticket, *, cancel_event=None):
        """Wait for DONE on current firmware, or the legacy computed duration."""
        try:
            with self._transaction:
                with self.response_condition:
                    if (
                        ticket is not self._pending_focus
                        or ticket.generation != self._generation_token("FOCUS MOVE")
                    ):
                        raise SideCaptureCancelled("Focus completion ticket is no longer current")
                if self.focus_completion and ticket.steps:
                    result = self._wait_reply(
                        ticket.sequence,
                        ticket.generation,
                        "DONE FOCUS POS ",
                        "FOCUS MOVE",
                        ticket.deadline,
                        cancel_event,
                    )
                    with self.response_condition:
                        if ticket.generation != self._generation_token("FOCUS MOVE"):
                            raise SideCaptureCancelled("Focus completion is from an old session")
                        if not re.fullmatch(r"DONE FOCUS POS -?\d+", result) or not (
                            -(2**31) <= int(result.rsplit(" ", 1)[-1]) < 2**31
                        ):
                            self._fail("Malformed focus completion")
                            raise RuntimeError("Malformed focus completion")
                else:
                    with self.response_condition:
                        while time.monotonic() < ticket.started + ticket.duration:
                            if not self.connected:
                                raise RuntimeError("Arduino disconnected during focus move")
                            if self._fault:
                                raise RuntimeError(self._fault)
                            if ticket.generation != self._generation_token("FOCUS MOVE") or (
                                cancel_event is not None and cancel_event.is_set()
                            ):
                                raise SideCaptureCancelled("Microscope command was stopped")
                            self.response_condition.wait(
                                min(0.05, ticket.started + ticket.duration - time.monotonic())
                            )
                    result = None
                with self.response_condition:
                    if (
                        ticket is not self._pending_focus
                        or ticket.generation != self._generation_token("FOCUS MOVE")
                    ):
                        raise SideCaptureCancelled("Focus completion ticket is no longer current")
                    self._pending_focus = None
                return result
        except SideCaptureCancelled:
            if cancel_event is not None and cancel_event.is_set():
                self._stop("STOP FOCUS", expected_session=ticket.generation[0])
            raise

    def focus_move(self, steps, *, cancel_event=None):
        return self.wait_focus_done(
            self.focus_move_async(steps, cancel_event=cancel_event), cancel_event=cancel_event
        )

    def focus_speed(self, steps_per_second):
        speed = self._steps(steps_per_second)
        if not 1 <= speed <= 10000:
            raise ValueError("Focus speed must be within 1..10000")
        with self._transaction:
            with self.response_condition:
                generation = self._generation_token("FOCUS SPEED")
            reply = self.send_and_wait(f"FOCUS SPEED {speed}", f"OK FOCUS SPEED {speed}")
            with self.response_condition:
                if generation != self._generation_token("FOCUS SPEED"):
                    raise SideCaptureCancelled("Focus speed acknowledgement is from an old session")
                if reply != f"OK FOCUS SPEED {speed}":
                    self._fail("Malformed focus speed acknowledgement")
                    raise RuntimeError("Malformed focus speed acknowledgement")
                self._focus_speed = speed
            return reply

    def focus_stop(self):
        return self._stop("STOP FOCUS")

    def set_microstep(self, microstep):
        if type(microstep) is not int or microstep not in (1, 2, 4, 8, 16):
            raise ValueError("Microstep must be 1, 2, 4, 8, or 16")
        return self._ack(f"MICRO {microstep}", f"OK MICRO {microstep}")

    def stop_all(self):
        return self._stop("STOP ALL")

    def status(self):
        return self.send_and_wait("STATUS", "MICRO=")
