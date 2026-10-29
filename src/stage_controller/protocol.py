"""Arduino line protocol and a hardware-free demonstration transport.

Only this module knows the sketch's wire format. Commands are acknowledged one
at a time; callers must serialize whole select/configure/move transactions.
"""

import errno
import re
import time
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


class Transport(Protocol):
    microstep: int | None

    def command(self, command: str) -> None: ...
    def status(self) -> dict[str, MotorStatus]: ...
    def close(self) -> None: ...


STATUS_LINE = re.compile(
    r"^\s*\*?\s*(bottom|top|insert) (cw|ccw) (idle|moving|continuous) "
    r"pos=(-?\d+) remaining=(-?\d+) speed=(\d+(?:\.\d+)?) "
    r"accel=(\d+(?:\.\d+)?) spr=(\d+)\s*$"
)


def parse_status(line: str) -> MotorStatus | None:
    match = STATUS_LINE.fullmatch(line)
    if match is None:
        return None
    motor, direction, mode, pos, remaining, speed, accel, spr = match.groups()
    return MotorStatus(
        motor, direction, mode, int(pos), int(remaining), float(speed), float(accel), int(spr)
    )


class SerialTransport:
    def __init__(self, port: str):
        self.selected = "bottom"
        self.microstep = None
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
        self.motors = {motor: MotorStatus(motor) for motor in MOTOR_IDS}
        self.moves = {}
        self.holds = {}
        self.microstep = 1

    def _update(self):
        now = self.clock()
        for motor, (started, start, deadline) in list(self.holds.items()):
            state = self.motors[motor]
            elapsed = min(now, deadline) - started
            sign = 1 if state.direction == "cw" else -1
            state.position = start + sign * int(elapsed * state.speed)
            if now >= deadline:
                state.mode = "idle"
                del self.holds[motor]
        for motor, (started, duration, start, delta) in list(self.moves.items()):
            fraction = min(1.0, (self.clock() - started) / duration)
            state = self.motors[motor]
            state.position = start + int(delta * fraction)
            state.remaining = start + delta - state.position
            if fraction >= 1:
                state.mode = "idle"
                del self.moves[motor]

    def command(self, command: str) -> None:
        self._update()
        verb, _, argument = command.partition(" ")
        if verb == "microstep":
            if argument not in {str(value) for value in MICROSTEP_MODES}:
                raise CommandRejected("Choose microstep 1, 2, 4, 8, or 16.")
            if any(state.mode != "idle" for state in self.motors.values()):
                raise CommandRejected("All motors must be idle before changing microstep mode.")
            self.microstep = int(argument)
            return
        if command == "stop all":
            self.moves.clear()
            self.holds.clear()
            for state in self.motors.values():
                state.mode = "idle"
                state.remaining = 0
            return
        if verb == "motor":
            self.selected = argument
            return
        state = self.motors[self.selected]
        if verb == "stop":
            self.moves.pop(self.selected, None)
            self.holds.pop(self.selected, None)
            state.mode = "idle"
            state.remaining = 0
            return
        if verb == "keepalive":
            if self.selected not in self.holds:
                raise CommandRejected("Hold has ended. Release and press again.")
            started, start, _ = self.holds[self.selected]
            self.holds[self.selected] = (started, start, self.clock() + HOLD_TIMEOUT_SECONDS)
            return
        if state.mode != "idle":
            raise CommandRejected("Motor is already moving.")
        if verb == "dir":
            state.direction = argument
        elif verb == "speed":
            state.speed = int(argument)
        elif verb == "hold":
            state.mode = "continuous"
            state.remaining = 0
            self.holds[state.id] = (
                self.clock(),
                state.position,
                self.clock() + HOLD_TIMEOUT_SECONDS,
            )
        elif verb == "steps":
            delta = int(argument) * (1 if state.direction == "cw" else -1)
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
