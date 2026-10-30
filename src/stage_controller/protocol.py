"""Arduino line protocol and a hardware-free demonstration transport.

Only this module knows the sketch's wire format. Commands are acknowledged one
at a time; callers must serialize whole select/configure/move transactions.
"""

import errno
import math
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Protocol

import serial

MOTOR_IDS = ("bottom", "top", "insert")
BAUD_RATE = 115200
DEFAULT_SPEED = 50
DEFAULT_STEPS = 50
MAX_SPEED = 1000
MAX_STEPS = 100_000  # Deliberate UI bound, below the firmware's signed long limit.
COOLDOWN_SECONDS = 1.0
HOLD_TIMEOUT_SECONDS = 1.0  # Matches the Arduino hold watchdog.
MICROSTEP_MODES = {
    1: "Full step",
    2: "Half step (1/2)",
    4: "Quarter step (1/4)",
    8: "Eighth step (1/8)",
    16: "Sixteenth step (1/16)",
}


class ControllerError(Exception):
    """An actionable error to display in the control panel."""


class CommandRejected(ControllerError):
    """The board replied with an error; the connection is still synchronized."""


class LinkError(ControllerError):
    """The board could not be reached or its replies are no longer trustworthy."""


@dataclass
class MotorStatus:
    id: str
    direction: str = "cw"
    mode: str = "idle"
    position: int = 0
    remaining: int = 0
    speed: float = DEFAULT_SPEED
    acceleration: float = 50
    steps_per_revolution: int = 200
    homed: bool | None = None
    limits: str | None = None
    soft: bool | None = None
    awake: bool | None = None


class Transport(Protocol):
    microstep: int | None

    def command(self, command: str) -> None: ...
    def status(self) -> dict[str, MotorStatus]: ...
    def close(self) -> None: ...


STATUS_LINE = re.compile(
    r"^\s*\*?\s*(bottom|top|insert) (cw|ccw) (idle|moving|continuous) "
    r"pos=(-?\d+) remaining=(-?\d+) speed=(\d+(?:\.\d+)?) "
    r"accel=(\d+(?:\.\d+)?) spr=(\d+)"
    r"(?: homed=([01]) limits=([01]{2}) soft=([01]) awake=([01]))?\s*$"
)


def parse_status(line: str) -> MotorStatus | None:
    match = STATUS_LINE.fullmatch(line)
    if match is None:
        return None
    motor, direction, mode, pos, remaining, speed, accel, spr, homed, limits, soft, awake = (
        match.groups()
    )
    if (
        not all(math.isfinite(float(value)) for value in (speed, accel))
        or not all(-(2**31) <= int(value) < 2**31 for value in (pos, remaining))
        or not 1 <= float(speed) <= 1000
        or not 1 <= float(accel) <= 10000
        or not 1 <= int(spr) <= 65535
    ):
        raise LinkError("Invalid numeric Arduino status.")
    return MotorStatus(
        motor,
        direction,
        mode,
        int(pos),
        int(remaining),
        float(speed),
        float(accel),
        int(spr),
        None if homed is None else homed == "1",
        limits,
        None if soft is None else soft == "1",
        None if awake is None else awake == "1",
    )


class SerialTransport:
    def __init__(self, port: str):
        self.selected = "bottom"
        self.microstep = None
        self.home_errors = deque(maxlen=32)
        try:
            # The lock stops a second program, or a second panel, from sharing the port.
            self.serial = serial.Serial(
                port=port, baudrate=BAUD_RATE, timeout=0.1, write_timeout=0.5, exclusive=True
            )
            # Most Arduino boards reset when the port is opened. Drain the boot
            # banner before beginning request/response traffic.
            time.sleep(2)
            self.serial.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            if hasattr(self, "serial"):
                self.close()
            if exc.errno in (errno.EAGAIN, errno.EBUSY):
                raise LinkError(f"{port} is in use by another program.") from exc
            raise LinkError(f"Cannot open {port}: {exc}") from exc

    def _write(self, command: str) -> None:
        try:
            payload = (command + "\n").encode("ascii")
            if self.serial.write(payload) != len(payload):
                raise LinkError("Incomplete serial write. Reconnect before trying again.")
        except (serial.SerialException, OSError) as exc:
            raise LinkError(f"Serial write failed: {exc}") from exc

    def _lines(self):
        deadline = time.monotonic() + 1.5
        pending = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = self.serial.read_until(b"\n", 512)
            except (serial.SerialException, OSError) as exc:
                raise LinkError(f"Serial read failed: {exc}") from exc
            pending.extend(chunk)
            if len(pending) > 2048:
                raise LinkError("Oversized Arduino reply. Check the uploaded sketch.")
            if pending.endswith(b"\n"):
                line = pending.decode("ascii", errors="replace").strip()
                pending.clear()
                if line.startswith("ERROR:"):
                    failure = re.fullmatch(r"ERROR: home failed: (bottom|top|insert) (.+)", line)
                    if failure:
                        self.home_errors.append(failure.groups())
                        continue
                    raise CommandRejected(line)
                yield line
        raise LinkError("Arduino did not reply in time. Check the port and uploaded sketch.")

    def command(self, command: str) -> None:
        self._write(command)
        verb, _, argument = command.partition(" ")
        if command == "stop all":
            expected = "OK: All motors stopped."
        elif verb == "microstep":
            expected = f"OK: microstep {argument}"
        elif verb == "motor":
            expected = f"OK: {argument} motor"
        else:
            expected = f"OK: {self.selected} {verb}"
        for line in self._lines():
            if line == expected:
                if verb == "motor":
                    self.selected = argument
                return
            if line.startswith("OK:"):
                raise LinkError(f"Unexpected Arduino acknowledgement: {line}")

    def status(self) -> dict[str, MotorStatus]:
        self._write("status")
        statuses = {}
        microstep = None
        for line in self._lines():
            if line.startswith("microstep"):
                match = re.fullmatch(r"microstep=(1|2|4|8|16)", line)
                if match is None or microstep is not None:
                    raise LinkError("Invalid or duplicate microstep status from Arduino.")
                microstep = int(match[1])
                continue
            parsed = parse_status(line)
            if parsed:
                if parsed.id in statuses:
                    raise LinkError("Duplicate motor in Arduino status reply.")
                statuses[parsed.id] = parsed
                if line.startswith("*"):
                    self.selected = parsed.id
                if len(statuses) == len(MOTOR_IDS):
                    capabilities = {state.homed is not None for state in statuses.values()}
                    if len(capabilities) != 1 or (True in capabilities and microstep is None):
                        raise LinkError("Inconsistent Arduino homing status.")
                    # An older sketch has no header: retain movement support but
                    # report resolution as unknown instead of assuming full step.
                    self.microstep = microstep
                    return statuses
        raise LinkError("Incomplete Arduino status.")  # Defensive: _lines raises on timeout.

    def close(self) -> None:
        try:
            self.serial.close()
        except (serial.SerialException, OSError):
            pass


class DemoTransport:
    """Simulate finite and held moves without opening a serial port."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.selected = "bottom"
        self.motors = {
            motor: MotorStatus(motor, homed=False, limits="00", soft=False, awake=False)
            for motor in MOTOR_IDS
        }
        self.moves = {}
        self.holds = {}
        self.microstep = 1
        self.homes = {}
        self.home_errors = deque(maxlen=32)
        self.home_failures = {}
        self.soft_ranges = {}
        self.hard_ranges = {motor: (-100_000, 100_000) for motor in MOTOR_IDS}
        self.awake_ms = dict.fromkeys(MOTOR_IDS, 0)
        self.awake_until = dict.fromkeys(MOTOR_IDS, 0.0)
        self.idle_until = dict.fromkeys(MOTOR_IDS, 0.0)

    def _stop(self, motor):
        state = self.motors[motor]
        if motor in self.homes:
            del self.homes[motor]
            self.home_errors.append((motor, "cancelled"))
        self.moves.pop(motor, None)
        self.holds.pop(motor, None)
        if state.mode != "idle":
            self.idle_until[motor] = self.clock() + 10
            self.awake_until[motor] = self.clock() + self.awake_ms[motor] / 1000
        state.mode = "idle"
        state.remaining = 0

    def _bounds(self, motor):
        low, high = self.hard_ranges[motor]
        if motor in self.soft_ranges:
            soft_low, soft_high = self.soft_ranges[motor]
            low, high = max(low, soft_low), min(high, soft_high)
        return low, high

    def _update(self):
        now = self.clock()
        for motor, deadline in list(self.homes.items()):
            if now >= deadline:
                del self.homes[motor]
                state = self.motors[motor]
                self._stop(motor)
                failure = self.home_failures.get(motor)
                if failure:
                    self.home_errors.append((motor, failure))
                else:
                    state.position = 0
                    state.homed = True
                    state.direction = "ccw"
                    self.hard_ranges[motor] = (0, 100_000)
        for motor, (started, start, deadline) in list(self.holds.items()):
            state = self.motors[motor]
            elapsed = min(now, deadline) - started
            sign = 1 if state.direction == "cw" else -1
            target = start + sign * int(elapsed * state.speed)
            low, high = self._bounds(motor)
            state.position = min(target, high) if sign > 0 else max(target, low)
            if (
                now >= deadline
                or (sign > 0 and state.position >= high)
                or (sign < 0 and state.position <= low)
            ):
                self._stop(motor)
        for motor, (started, duration, start, delta) in list(self.moves.items()):
            fraction = min(1.0, (self.clock() - started) / duration)
            state = self.motors[motor]
            target = start + int(delta * fraction)
            low, high = self._bounds(motor)
            state.position = min(target, high) if delta > 0 else max(target, low)
            state.remaining = start + delta - state.position
            if fraction >= 1 or state.position != target:
                self._stop(motor)
        for motor, state in self.motors.items():
            low, high = self.hard_ranges[motor]
            state.limits = f"{int(state.position <= low)}{int(state.position >= high)}"
            state.awake = state.mode != "idle" or now < max(
                self.awake_until[motor], self.idle_until[motor]
            )

    def command(self, command: str) -> None:
        self._update()
        verb, _, argument = command.partition(" ")
        if verb == "microstep":
            if argument not in {str(value) for value in MICROSTEP_MODES}:
                raise CommandRejected("Choose microstep 1, 2, 4, 8, or 16.")
            if any(state.mode != "idle" for state in self.motors.values()):
                raise CommandRejected("All motors must be idle before changing microstep mode.")
            if self.microstep != int(argument):
                self.soft_ranges.clear()
                for state in self.motors.values():
                    state.homed = state.soft = False
            self.microstep = int(argument)
            return
        if command == "stop all":
            for motor in MOTOR_IDS:
                self._stop(motor)
            return
        if verb == "motor":
            self.selected = argument
            return
        state = self.motors[self.selected]
        if verb == "stop":
            self._stop(self.selected)
            return
        if verb == "awake":
            milliseconds = int(argument)
            if not 0 <= milliseconds <= 60000:
                raise CommandRejected("Invalid awake interval.")
            self.awake_ms[self.selected] = milliseconds
            self.awake_until[self.selected] = self.clock() + milliseconds / 1000
            return
        if verb == "keepalive":
            if self.selected not in self.holds:
                raise CommandRejected("Hold has ended. Release and press again.")
            started, start, _ = self.holds[self.selected]
            self.holds[self.selected] = (started, start, self.clock() + HOLD_TIMEOUT_SECONDS)
            return
        if state.mode != "idle":
            raise CommandRejected("Motor is already moving.")
        if verb == "home":
            state.homed = state.soft = False
            self.soft_ranges.pop(state.id, None)
            state.mode = "moving"
            self.homes[state.id] = self.clock() + 0.5
        elif verb == "soft":
            low, high = map(int, argument.split())
            if not state.homed or low > high:
                raise CommandRejected("Soft limits require a homed motor and an ordered range.")
            self.soft_ranges[state.id] = (low, high)
            state.soft = True
        elif verb == "dir":
            state.direction = argument
        elif verb == "speed":
            state.speed = int(argument)
        elif verb == "hold":
            low, high = self._bounds(state.id)
            if (state.direction == "cw" and state.position >= high) or (
                state.direction == "ccw" and state.position <= low
            ):
                raise CommandRejected("Motion would enter an active limit.")
            state.mode = "continuous"
            state.remaining = 0
            self.holds[state.id] = (
                self.clock(),
                state.position,
                self.clock() + HOLD_TIMEOUT_SECONDS,
            )
        elif verb == "steps":
            delta = int(argument) * (1 if state.direction == "cw" else -1)
            low, high = self._bounds(state.id)
            if (delta > 0 and state.position >= high) or (delta < 0 and state.position <= low):
                raise CommandRejected("Motion would enter an active limit.")
            soft = self.soft_ranges.get(state.id)
            if soft and not soft[0] <= state.position + delta <= soft[1]:
                raise CommandRejected("Target is outside soft limits.")
            state.mode = "moving"
            state.remaining = delta
            self.moves[state.id] = (
                self.clock(),
                max(0.25, abs(delta) / state.speed),
                state.position,
                delta,
            )

    def status(self) -> dict[str, MotorStatus]:
        self._update()
        return {motor: replace(state) for motor, state in self.motors.items()}

    def close(self) -> None:
        self.moves.clear()
        self.holds.clear()
        self.homes.clear()
