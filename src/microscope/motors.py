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
class AxisStatus:
    enabled: bool
    moving: bool
    speed: int
    remaining: int
    position: int


@dataclass(frozen=True)
class MotorStatus:
    session: int
    reference_epoch: int
    observed_at: float
    sequence: int
    microstep: int
    objective: AxisStatus
    focus: AxisStatus
    zeroed: bool
    limited: bool
    minimum: int
    maximum: int
    homing: bool


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
        self._stop_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._serial_factory = serial_factory or serial.Serial
        self._generation = 0
        self._reference_epoch = 0
        self._known_microstep = None
        self._cleaning = 0
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

    @property
    def session(self):
        with self.response_condition:
            return self._generation

    @property
    def reference_epoch(self):
        with self.response_condition:
            return self._reference_epoch

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
                    self._reference_epoch += 1
                    self._known_microstep = None
                    self._cleaning = 0
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
                self._reference_epoch += 1
                self._known_microstep = None
                self._pending_focus = None
                self._validated = False
                self.stop_reader.set()
                port, self.ser = self.ser, None
                self.response_condition.notify_all()
            if port is not None:
                port.close()
            thread = self.reader_thread
            if (
                thread is not None
                and thread is not threading.current_thread()
                and thread.ident is not None
            ):
                thread.join(timeout=1.0)
                if thread.is_alive():
                    raise RuntimeError("Previous microscope reader is still stopping")
            self.reader_thread = None

    def _fail(self, message):
        with self.response_condition:
            self._fault = self._fault or message
            self._reference_epoch += 1
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
                        if text == "DUAL_A4988_READY":
                            self._generation += 1
                            self._reference_epoch += 1
                            self._known_microstep = None
                            self._pending_focus = None
                            self._validated = False
                            self._fault = "Microscope firmware reset; reconnect required"
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
                    self._fail("Microscope serial reader failed")

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
            if command in ("FOCUS ZERO", "FOCUS HOME", "FOCUS DISABLE") or command.startswith(
                "FOCUS LIMIT "
            ):
                self._reference_epoch += 1
            elif re.fullmatch(r"MICRO (1|2|4|8|16)", command):
                microstep = int(command.split()[1])
                if microstep != self._known_microstep:
                    self._reference_epoch += 1
                    self._known_microstep = microstep
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
        if self._stopping or self._cleaning:
            raise RuntimeError("Microscope stop is pending")
        if self._pending_focus is not None and not pending:
            raise RuntimeError("A focus move is still pending")

    def _generation_token(self, command):
        words = command.split()
        axis = words[1] if words[0] == "STOP" else words[0]
        if axis in self._stop_epoch:
            return (self._generation, self._stop_epoch[axis])
        return (self._generation, self._stop_epoch["OBJ"], self._stop_epoch["FOCUS"])

    def _admission_token(self, command, *, handshake=False):
        # Snapshot authority before a command can queue behind another transaction.
        # A stop or reconnect must revoke queued work as well as an active wait.
        with self.response_condition:
            self._allowed(pending=True, handshake=handshake)
            return self._generation_token(command)

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
                    self._fail("Microscope response history was lost")
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
                    self._fail("Microscope command completion is unknown")
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
        _expected_generation=None,
    ):
        command = self._command(command)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if _handshake and command != "VERSION":
            raise ValueError("Only VERSION is permitted during the handshake")
        if command.startswith("STOP "):
            return self._stop(command, timeout, cancel_event=cancel_event)
        if command == "STATUS":
            lines, _ = self._status_lines(timeout, cancel_event, require_reference=False)
            for line in lines:
                if line.startswith(response_prefix):
                    return line
            raise RuntimeError("The status response did not contain the requested field")
        generation = (
            self._admission_token(command, handshake=_handshake)
            if _expected_generation is None
            else _expected_generation
        )
        dispatched = False
        try:
            with self._transaction:
                with self.response_condition:
                    if generation != self._generation_token(command):
                        raise SideCaptureCancelled("Queued microscope command was stopped")
                    self._allowed(handshake=_handshake)
                    if cancel_event is not None and cancel_event.is_set():
                        raise SideCaptureCancelled("Microscope command was stopped")
                    sequence, generation = self.response_sequence, self._generation_token(command)
                    self._write(command)
                    dispatched = True
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
            if dispatched and cancel_event is not None and cancel_event.is_set():
                self._stop("STOP ALL", expected_session=generation[0])
            raise

    def send(self, command, *, cancel_event=None):
        """Send an acknowledged command; low-level callers use send_and_wait."""
        command = self._command(command)
        prefix = "OK " + command.split()[0] + " "
        return self.send_and_wait(command, prefix, cancel_event=cancel_event)

    def _stop(self, command, timeout=5.0, *, expected_session=None, cancel_event=None):
        if command not in ("STOP OBJ", "STOP FOCUS", "STOP ALL"):
            raise ValueError("Unknown stop target")
        with self.response_condition:
            entry = self._generation_token(command)
        with self._stop_lock:
            generation = None
            try:
                with self.response_condition:
                    if expected_session is not None and expected_session != self._generation:
                        return None
                    if cancel_event is not None and (
                        cancel_event.is_set() or entry != self._generation_token(command)
                    ):
                        raise SideCaptureCancelled("Queued microscope stop was cancelled")
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
                        self._fail("Microscope stop is unconfirmed")
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

    def _ack(self, command, expected, *, cancel_event=None):
        generation = self._admission_token(command)
        with self._transaction:
            reply = self.send_and_wait(
                command, expected, cancel_event=cancel_event, _expected_generation=generation
            )
            with self.response_condition:
                if generation != self._generation_token(command):
                    raise SideCaptureCancelled("Acknowledgement is from an old session")
                if reply != expected:
                    self._fail("Malformed microscope acknowledgement")
                    raise RuntimeError("Malformed microscope acknowledgement")
            return reply

    def objective_enable(self, *, cancel_event=None):
        return self._ack("OBJ ENABLE", "OK OBJ ENABLED", cancel_event=cancel_event)

    def objective_disable(self, *, cancel_event=None):
        return self._ack("OBJ DISABLE", "OK OBJ DISABLED", cancel_event=cancel_event)

    def objective_move(self, steps, *, cancel_event=None):
        steps = self._steps(steps)
        return self._ack(f"OBJ MOVE {steps}", f"OK OBJ MOVE {steps}", cancel_event=cancel_event)

    def objective_speed(self, steps_per_second, *, cancel_event=None):
        speed = self._steps(steps_per_second)
        return self._ack(f"OBJ SPEED {speed}", f"OK OBJ SPEED {speed}", cancel_event=cancel_event)

    def objective_stop(self, *, cancel_event=None):
        return self._stop("STOP OBJ", cancel_event=cancel_event)

    def focus_enable(self, *, cancel_event=None):
        return self._ack("FOCUS ENABLE", "OK FOCUS ENABLED", cancel_event=cancel_event)

    def focus_disable(self, *, cancel_event=None):
        return self._ack("FOCUS DISABLE", "OK FOCUS DISABLED", cancel_event=cancel_event)

    def focus_move_async(self, steps, *, cancel_event=None):
        """Start one focus move and return its completion ticket."""
        steps = self._steps(steps)
        generation = self._admission_token("FOCUS MOVE")
        with self._transaction:
            with self.response_condition:
                if generation != self._generation_token("FOCUS MOVE"):
                    raise SideCaptureCancelled("Queued focus command was stopped")
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

    def focus_speed(self, steps_per_second, *, cancel_event=None):
        speed = self._steps(steps_per_second)
        if not 1 <= speed <= 10000:
            raise ValueError("Focus speed must be within 1..10000")
        generation = self._admission_token("FOCUS SPEED")
        with self._transaction:
            reply = self.send_and_wait(
                f"FOCUS SPEED {speed}",
                f"OK FOCUS SPEED {speed}",
                cancel_event=cancel_event,
                _expected_generation=generation,
            )
            with self.response_condition:
                if generation != self._generation_token("FOCUS SPEED"):
                    raise SideCaptureCancelled("Focus speed acknowledgement is from an old session")
                if reply != f"OK FOCUS SPEED {speed}":
                    self._fail("Malformed focus speed acknowledgement")
                    raise RuntimeError("Malformed focus speed acknowledgement")
                self._focus_speed = speed
            return reply

    def focus_stop(self, *, cancel_event=None):
        return self._stop("STOP FOCUS", cancel_event=cancel_event)

    def set_microstep(self, microstep, *, cancel_event=None):
        if type(microstep) is not int or microstep not in (1, 2, 4, 8, 16):
            raise ValueError("Microstep must be 1, 2, 4, 8, or 16")
        return self._ack(f"MICRO {microstep}", f"OK MICRO {microstep}", cancel_event=cancel_event)

    def stop_all(self, *, cancel_event=None):
        return self._stop("STOP ALL", cancel_event=cancel_event)

    def status(self):
        return self.send_and_wait("STATUS", "MICRO=")

    @staticmethod
    def _timeout(value, maximum=65.0):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"timeout must be finite and within 0..{maximum}")
        return value

    @staticmethod
    def _parse_axis(line, name):
        match = re.fullmatch(
            rf"{name}: enabled=([01]) moving=([01]) speed=(\d+) remaining=(\d+) pos=(-?\d+)",
            line,
        )
        if match is None:
            raise ValueError("Malformed microscope axis status")
        enabled, moving, speed, remaining, position = map(int, match.groups())
        if (
            not 1 <= speed <= 10000
            or not 0 <= remaining <= 2**31
            or not -(2**31) <= position < 2**31
            or (moving and (not enabled or not remaining))
            or (not moving and remaining != 0)
        ):
            raise ValueError("Microscope axis status is outside its bounds")
        return AxisStatus(bool(enabled), bool(moving), speed, remaining, position)

    def _parse_status(self, lines, sequence):
        microstep = re.fullmatch(r"MICRO=(1|2|4|8|16)", lines[0])
        reference = re.fullmatch(
            r"FOCUS_REF: zeroed=([01]) limited=([01]) min=(-?\d+) max=(-?\d+) homing=([01])",
            lines[3],
        )
        if microstep is None or reference is None:
            raise ValueError("Malformed microscope reference status")
        zeroed, limited, minimum, maximum, homing = map(int, reference.groups())
        objective = self._parse_axis(lines[1], "OBJ")
        focus = self._parse_axis(lines[2], "FOCUS")
        if (
            not -(2**31) <= minimum < 2**31
            or not -(2**31) <= maximum < 2**31
            or minimum > maximum
            or (limited and (not zeroed or not minimum <= focus.position <= maximum))
            or (homing and (zeroed or limited or not focus.moving))
        ):
            raise ValueError("Microscope reference status is inconsistent")
        divisor = int(microstep[1])
        if self._known_microstep is not None and divisor != self._known_microstep:
            self._reference_epoch += 1
        self._known_microstep = divisor
        return MotorStatus(
            self._generation,
            self._reference_epoch,
            time.monotonic(),
            sequence,
            divisor,
            objective,
            focus,
            bool(zeroed),
            bool(limited),
            minimum,
            maximum,
            bool(homing),
        )

    def _status_lines(self, timeout, cancel_event, *, require_reference):
        timeout = self._timeout(timeout)
        deadline = time.monotonic() + timeout
        with self.response_condition:
            generation = self._admission_token("STATUS")
            reference_epoch = self._reference_epoch
            modern = self.focus_completion
            if require_reference and not modern:
                raise RuntimeError("Firmware reference status is unavailable")
        if not self._status_lock.acquire(timeout=timeout):
            raise RuntimeError("Another microscope status read is still pending")
        try:
            with self.response_condition:
                if (
                    generation != self._generation_token("STATUS")
                    or reference_epoch != self._reference_epoch
                    or (cancel_event is not None and cancel_event.is_set())
                ):
                    raise SideCaptureCancelled("Queued microscope status was revoked")
                self._allowed(pending=True)
                if time.monotonic() >= deadline:
                    raise RuntimeError("Microscope status deadline expired before dispatch")
                sequence = self.response_sequence
                self._write("STATUS")
                expected = ("MICRO=", "OBJ: ", "FOCUS: ", "FOCUS_REF: ")
                expected = expected if modern else expected[:3]
                lines = []
                revoked = False
                while len(lines) < len(expected):
                    if generation[0] != self._generation:
                        raise SideCaptureCancelled("Microscope status belongs to an old session")
                    if not self.connected:
                        raise RuntimeError("Arduino disconnected during status")
                    if self._fault:
                        raise RuntimeError(self._fault)
                    # Drain the full group after a same-session revocation. An
                    # abandoned late group must never satisfy the next reader.
                    revoked |= (
                        generation != self._generation_token("STATUS")
                        or reference_epoch != self._reference_epoch
                        or (cancel_event is not None and cancel_event.is_set())
                    )
                    if self.responses and self.responses[0][0] > sequence + 1:
                        self._fail("Microscope status response history was lost")
                        raise RuntimeError(self._fault)
                    for number, line in self.responses:
                        if number <= sequence:
                            continue
                        sequence = number
                        if line.startswith(("ERR", "ERROR")):
                            self._fail("Microscope status could not be confirmed")
                            raise RuntimeError(self._fault)
                        if line.startswith(expected[len(lines)]):
                            lines.append(line)
                            if len(lines) == len(expected):
                                break
                        elif line.startswith(("MICRO=", "OBJ:", "FOCUS:", "FOCUS_REF:")):
                            self._fail("Microscope status rows are incomplete or out of order")
                            raise RuntimeError(self._fault)
                    if len(lines) == len(expected):
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._fail("Microscope status is incomplete")
                        raise RuntimeError(self._fault)
                    self.response_condition.wait(min(0.05, remaining))
                if revoked:
                    raise SideCaptureCancelled("Microscope status was revoked while reading")
                try:
                    status = self._parse_status(lines, sequence) if modern else None
                except ValueError as error:
                    self._fail(str(error))
                    raise RuntimeError(str(error)) from error
                return lines, status
        finally:
            self._status_lock.release()

    def read_status(self, *, timeout=1.0, cancel_event=None):
        """Read a complete modern status without blocking behind a long move."""
        return self._status_lines(timeout, cancel_event, require_reference=True)[1]

    def focus_zero(self, *, cancel_event=None):
        return self._ack("FOCUS ZERO", "OK FOCUS ZERO", cancel_event=cancel_event)

    def focus_limits(self, minimum, maximum, *, cancel_event=None):
        minimum, maximum = self._steps(minimum), self._steps(maximum)
        if minimum > maximum:
            raise ValueError("Focus minimum must not exceed maximum")
        command = f"FOCUS LIMIT {minimum} {maximum}"
        return self._ack(command, "OK " + command, cancel_event=cancel_event)

    def focus_home(self, *, timeout=65.0, cancel_event=None):
        """Await this HOME's acknowledgement, completion, and fresh reference."""
        timeout = self._timeout(timeout)
        command = "FOCUS HOME"
        generation = self._admission_token(command)
        deadline = time.monotonic() + timeout
        dispatched = False
        try:
            with self._transaction:
                with self.response_condition:
                    if generation != self._generation_token(command):
                        raise SideCaptureCancelled("Queued focus home was stopped")
                    self._allowed()
                    if cancel_event is not None and cancel_event.is_set():
                        raise SideCaptureCancelled("Focus home was cancelled")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Focus home deadline expired before dispatch")
                    sequence = self.response_sequence
                    self._write(command)
                    dispatched = True
                sequence, reply = self._wait_reply(
                    sequence,
                    generation,
                    "OK FOCUS HOME",
                    command,
                    deadline,
                    cancel_event,
                    with_sequence=True,
                )
                if reply != "OK FOCUS HOME":
                    with self.response_condition:
                        if generation[0] == self._generation:
                            self._fail("Malformed focus home acknowledgement")
                    raise RuntimeError("Malformed focus home acknowledgement")
                reply = self._wait_reply(
                    sequence, generation, "DONE FOCUS HOME", command, deadline, cancel_event
                )
                with self.response_condition:
                    if generation != self._generation_token(command):
                        raise SideCaptureCancelled("Focus home completion was revoked")
                    if reply != "DONE FOCUS HOME":
                        self._fail("Malformed focus home completion")
                        raise RuntimeError("Malformed focus home completion")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._fail("Focus home reference is unconfirmed")
                        raise RuntimeError(self._fault)
                status = self.read_status(timeout=min(1.0, remaining), cancel_event=cancel_event)
                with self.response_condition:
                    if (
                        generation != self._generation_token(command)
                        or status.session != self._generation
                        or status.reference_epoch != self._reference_epoch
                    ):
                        raise SideCaptureCancelled("Focus home status was revoked")
                    if (
                        not status.zeroed
                        or status.limited
                        or status.homing
                        or status.focus.moving
                        or status.focus.position
                    ):
                        self._fail("Focus home did not establish an idle zero")
                        raise RuntimeError(self._fault)
                return status
        except SideCaptureCancelled:
            if dispatched and cancel_event is not None and cancel_event.is_set():
                self._stop("STOP FOCUS", expected_session=generation[0])
            raise

    def _safety_disable(self, command, expected, session, timeout):
        with self.response_condition:
            if session != self._generation:
                raise SideCaptureCancelled("Safety cleanup belongs to an old session")
            sequence = self.response_sequence
            generation = self._generation_token(command)
            self._write(command)
        reply = self._wait_reply(
            sequence, generation, expected, command, time.monotonic() + timeout, allow_fault=True
        )
        with self.response_condition:
            if session != self._generation:
                raise SideCaptureCancelled("Safety acknowledgement belongs to an old session")
            if reply != expected:
                self._fail("Malformed microscope disable acknowledgement")
                raise RuntimeError("Malformed microscope disable acknowledgement")
        return reply

    def safe_stop_disable(self, *, timeout=5.0):
        """Attempt only STOP ALL and both disables, even with a latched fault."""
        timeout = self._timeout(timeout, maximum=5.0)
        with self.response_condition:
            session = self._generation
            self._cleaning += 1
        replies, failures = [], []
        try:
            with self._stop_lock:
                for command, expected in (
                    ("STOP ALL", "OK STOP ALL"),
                    ("OBJ DISABLE", "OK OBJ DISABLED"),
                    ("FOCUS DISABLE", "OK FOCUS DISABLED"),
                ):
                    try:
                        if command == "STOP ALL":
                            reply = self._stop(command, timeout, expected_session=session)
                            if reply is None:
                                raise SideCaptureCancelled(
                                    "Safety cleanup belongs to an old session"
                                )
                        else:
                            reply = self._safety_disable(command, expected, session, timeout)
                        replies.append(reply)
                    except Exception as error:
                        failures.append(f"{command}: {error}")
                if failures:
                    with self.response_condition:
                        if session == self._generation:
                            self._fail("Microscope stop or disable is unconfirmed")
                    raise RuntimeError("; ".join(failures))
                return tuple(replies)
        finally:
            with self.response_condition:
                if session == self._generation:
                    self._cleaning -= 1
                self.response_condition.notify_all()
