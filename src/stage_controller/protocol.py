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
DEFAULT_SPEED = 0.25
DEFAULT_ROTATIONS = 0.25
MIN_SPEED = 0.001
MIN_ROTATIONS = 0.001
MAX_SPEED = 5.0  # Device reports a lower limit for the selected microstep mode.
MAX_ROTATIONS = 10_000
COOLDOWN_SECONDS = 1.0
HOLD_TIMEOUT_SECONDS = 1.0  # Matches the Arduino hold watchdog.
REQUIRED_MICROSTEP = 8
MICROSTEP_MODES = {REQUIRED_MICROSTEP: "Eighth step (1/8)"}
TELEMETRY_TIMEOUT_SECONDS = 1.0
ENCODER_COUNTS_PER_ROTATION = 4096
ENCODER_COUNT_LIMIT = 1_000_000_000
POSITION_LIMIT = ENCODER_COUNT_LIMIT / ENCODER_COUNTS_PER_ROTATION
ACTIVE_CALIBRATION = ("lower", "upper", "center")
CALIBRATION_PHASE = r"none|lower|upper|center|done|cancelled|failed"


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
    position: float = 0.0
    remaining: float = 0.0
    speed: float = DEFAULT_SPEED
    acceleration: float = 0.25
    steps_per_revolution: int = 200
    encoder: str = "unknown"
    fault: str = "none"
    tolerance: float = 0.0
    max_speed: float = 0.0
    measured_speed: float = 0.0
    encoder_raw: int | None = None
    encoder_channel: int | None = None
    magnet_status: int | None = None
    encoder_age_ms: int | None = None
    telemetry_age_ms: int | None = None
    lower_limit: float | None = None
    upper_limit: float | None = None
    lower_limit_set: bool | None = None
    upper_limit_set: bool | None = None
    lower_limit_direction: str | None = None
    switch_mask: int | None = None
    switch_raw: int | None = None
    limit_telemetry_age_ms: int | None = None
    positive_direction: str = "cw"
    calibration: str = "none"
    warning: str = "none"
    direction_warning_count: int = 0

    @property
    def recalibration_recommended(self) -> bool:
        return self.direction_warning_count > 5


class Transport(Protocol):
    microstep: int | None
    closed_loop: bool
    telemetry_streaming: bool
    travel_limits: bool
    home_switches: bool
    lower_limit_direction: str | None
    lower_home_zero: bool
    calibration_only_homing: bool
    positive_direction: str
    auto_calibration: bool
    absolute_positioning: bool
    hold_direction_warnings: bool

    def command(self, command: str) -> None: ...
    def status(self) -> dict[str, MotorStatus]: ...
    def poll_status(self) -> dict[str, MotorStatus]: ...
    def close(self) -> None: ...


NUMBER = r"-?\d+(?:\.\d+)?"
STATUS_LINE = re.compile(
    r"^\s*\*?\s*(bottom|top|insert) (cw|ccw) (idle|moving|continuous) "
    rf"pos=({NUMBER}) remaining=({NUMBER}) speed=({NUMBER}) "
    rf"accel=({NUMBER}) spr=(\d+)"
    rf"(?: encoder=(ok|offline|magnet|untrusted) "
    rf"fault=(none|encoder|stall|direction|tracking|timeout) "
    rf"tolerance=({NUMBER}) max_speed=({NUMBER}) measured_speed=({NUMBER}))?"
    r"(?: lower=(-?\d+) upper=(-?\d+) bounds=(\d+))?"
    r"(?: switch=(\d+) switch_raw=(\d+))?"
    rf"(?: calibration=({CALIBRATION_PHASE}))?"
    r"(?: warning=(none|direction_pause) warnings=(\d+))?\s*$"
)


def limit_defaults(lower_limit_direction, positive_direction="cw"):
    """Named bounds follow firmware's direction convention, not their numeric sign."""
    return (
        (POSITION_LIMIT, -POSITION_LIMIT)
        if lower_limit_direction == positive_direction
        else (-POSITION_LIMIT, POSITION_LIMIT)
    )


def _validate_limit_bounds(lower, upper, bounds, lower_limit_direction, positive_direction="cw"):
    if (
        not -ENCODER_COUNT_LIMIT <= lower <= ENCODER_COUNT_LIMIT
        or not -ENCODER_COUNT_LIMIT <= upper <= ENCODER_COUNT_LIMIT
        or not 0 <= bounds <= 3
    ):
        raise LinkError("Invalid travel limits from Arduino.")
    # A LIM frame can precede the first capability header. Validate its numeric
    # shape immediately; status() validates its convention before exposing it.
    if lower_limit_direction is None or positive_direction is None:
        return
    sign = 1 if lower_limit_direction == positive_direction else -1
    if (
        sign * lower < sign * upper
        or (not bounds & 1 and lower != sign * ENCODER_COUNT_LIMIT)
        or (not bounds & 2 and upper != -sign * ENCODER_COUNT_LIMIT)
    ):
        raise LinkError("Invalid travel-limit ordering or defaults from Arduino.")


def parse_status(
    line: str,
    *,
    lower_limit_direction="ccw",
    positive_direction="cw",
    require_calibration=None,
    require_warnings=None,
) -> MotorStatus | None:
    # The previous Console firmware appends pulse-reference status. Read it for
    # connection/Stop only; closed_loop stays false and measured turns stay absent.
    if " encoder=" not in line:
        line = re.sub(r" homed=[01] limits=[01]{2} soft=[01] awake=[01]\s*$", "", line)
    match = STATUS_LINE.fullmatch(line)
    if match is None:
        return None
    motor, direction, mode, pos, remaining, speed, accel, spr, *extra = match.groups()
    feedback, limits, switches, calibration = extra[:5], extra[5:8], extra[8:10], extra[10]
    warning, warning_count = extra[11:]
    if require_calibration is not None and require_calibration != (calibration is not None):
        raise LinkError("Arduino motor status does not match its calibration capability.")
    if require_warnings is not None and require_warnings != (warning is not None):
        raise LinkError("Arduino motor status does not match its direction-warning capability.")
    if warning_count is not None and not 0 <= int(warning_count) <= 65535:
        raise LinkError("Invalid direction-warning count from Arduino.")
    values = [float(value) for value in (pos, remaining, speed, accel)]
    if not all(math.isfinite(value) for value in values) or min(values[2:]) <= 0:
        raise LinkError("Invalid numeric motor status from Arduino.")
    if not 1 <= int(spr) <= 65535:
        raise LinkError("Invalid steps per revolution from Arduino.")
    result = MotorStatus(motor, direction, mode, *values, int(spr))
    result.positive_direction = positive_direction
    result.calibration = calibration or "none"
    result.warning = warning or "none"
    result.direction_warning_count = int(warning_count) if warning_count is not None else 0
    if feedback[0] is not None:
        encoder, fault, tolerance, max_speed, measured_speed = feedback
        numbers = [float(value) for value in (tolerance, max_speed, measured_speed)]
        if (
            not all(math.isfinite(value) for value in numbers)
            or not 0 < numbers[0] < 1
            or not MIN_SPEED <= numbers[1] <= MAX_SPEED
        ):
            raise LinkError("Invalid encoder feedback status from Arduino.")
        result.encoder, result.fault = encoder, fault
        result.tolerance, result.max_speed, result.measured_speed = numbers
    if limits[0] is not None:
        lower, upper, bounds = map(int, limits)
        if feedback[0] is None:
            raise LinkError("Invalid travel limits from Arduino.")
        _validate_limit_bounds(lower, upper, bounds, lower_limit_direction, positive_direction)
        result.lower_limit = lower / ENCODER_COUNTS_PER_ROTATION
        result.upper_limit = upper / ENCODER_COUNTS_PER_ROTATION
        result.lower_limit_set = bool(bounds & 1)
        result.upper_limit_set = bool(bounds & 2)
        result.lower_limit_direction = lower_limit_direction
    if switches[0] is not None:
        mask, raw = map(int, switches)
        if limits[0] is None or not 0 <= mask <= 3 or not 0 <= raw <= 1023:
            raise LinkError("Invalid home-switch status from Arduino.")
        result.switch_mask, result.switch_raw = mask, raw
    return result


def motion_limit_error(state: MotorStatus, direction: str, rotations=None) -> str | None:
    """Reject outward travel; permit strictly inward recovery from outside a bound."""
    lower_direction = state.lower_limit_direction or "ccw"
    side, bit = ("lower", 1) if direction == lower_direction else ("upper", 2)
    if state.switch_mask is not None:
        if state.switch_mask & bit:
            return (
                f"The {side} home switch is pressed. Move in the opposite direction "
                "until it releases. Clearing a travel limit does not release the switch."
            )
    if state.lower_limit is None or state.upper_limit is None:
        return None
    position = round(state.position * ENCODER_COUNTS_PER_ROTATION)
    sign = 1 if direction == state.positive_direction else -1
    if rotations is None:
        if (sign > 0 and position >= ENCODER_COUNT_LIMIT) or (
            sign < 0 and position <= -ENCODER_COUNT_LIMIT
        ):
            return "Encoder position range reached. Move inward or reset position."
        bound = round(getattr(state, f"{side}_limit") * ENCODER_COUNTS_PER_ROTATION)
        if getattr(state, f"{side}_limit_set") and sign * position >= sign * bound:
            return (
                f"{side.title()} travel limit reached. "
                "Move in the opposite direction or clear the limit."
            )
        return None
    delta = math.floor(rotations * ENCODER_COUNTS_PER_ROTATION + 0.5)
    target = position + sign * delta
    if not -ENCODER_COUNT_LIMIT <= target <= ENCODER_COUNT_LIMIT:
        return "The requested move exceeds the encoder position range."
    lower_sign = 1 if lower_direction == state.positive_direction else -1
    for side, outward_sign in (("lower", lower_sign), ("upper", -lower_sign)):
        bound = round(getattr(state, f"{side}_limit") * ENCODER_COUNTS_PER_ROTATION)
        if (
            getattr(state, f"{side}_limit_set")
            and outward_sign * target > outward_sign * bound
            and outward_sign * target >= outward_sign * position
        ):
            return f"The requested move goes beyond the {side} travel limit."
    return None


def has_calibrated_range(state: MotorStatus) -> bool:
    return bool(
        state.calibration == "done"
        and state.lower_limit_set
        and state.upper_limit_set
        and state.lower_limit is not None
        and state.upper_limit is not None
        and math.isfinite(state.lower_limit)
        and math.isfinite(state.upper_limit)
        and 0 <= state.lower_limit < state.upper_limit <= POSITION_LIMIT
        and state.positive_direction == "ccw"
        and state.lower_limit_direction == "cw"
    )


def absolute_position_error(state: MotorStatus, position: float) -> str | None:
    if not has_calibrated_range(state):
        return "Complete Auto calibrate successfully before moving to an absolute position."
    if not state.lower_limit <= position <= state.upper_limit:
        return (
            f"Position must be within the calibrated range "
            f"{state.lower_limit:g} to {state.upper_limit:g} rotations."
        )
    target = math.floor(position * ENCODER_COUNTS_PER_ROTATION + 0.5)
    current = round(state.position * ENCODER_COUNTS_PER_ROTATION)
    delta = (target - current) / ENCODER_COUNTS_PER_ROTATION
    if abs(delta) <= state.tolerance:
        return None
    direction = (
        state.positive_direction
        if delta > 0
        else ("ccw" if state.positive_direction == "cw" else "cw")
    )
    return motion_limit_error(state, direction, abs(delta))


TELEMETRY_LINE = re.compile(
    rf"^TEL (\d+) (\d+) (bottom|top|insert) (cw|ccw) (idle|moving|continuous) "
    rf"counts=(-?\d+) remaining=(-?\d+) speed=({NUMBER}) "
    rf"encoder=(ok|offline|magnet|untrusted) "
    rf"fault=(none|encoder|stall|direction|tracking|timeout) "
    rf"raw=(\d+) channel=(\d+) magnet=(\d+) age=(\d+)$"
)


def parse_telemetry(line: str) -> dict:
    match = TELEMETRY_LINE.fullmatch(line)
    if match is None:
        raise LinkError("Invalid Arduino telemetry frame.")
    (
        sequence,
        uptime,
        motor,
        direction,
        mode,
        counts,
        remaining,
        speed,
        encoder,
        fault,
        raw,
        channel,
        magnet,
        age,
    ) = match.groups()
    sequence, uptime, counts, remaining, raw, channel, magnet, age = map(
        int, (sequence, uptime, counts, remaining, raw, channel, magnet, age)
    )
    speed = float(speed)
    if (
        not all(0 <= value <= 0xFFFFFFFF for value in (sequence, uptime, age))
        or not -1_000_000_000 <= counts <= 1_000_000_000
        or not -2_000_000_000 <= remaining <= 2_000_000_000
        or not math.isfinite(speed)
        or not 0 <= raw < 4096
        or not 0 <= channel < 8
        or not 0 <= magnet <= 255
    ):
        raise LinkError("Out-of-range Arduino telemetry frame.")
    return {
        "sequence": sequence,
        "uptime": uptime,
        "id": motor,
        "direction": direction,
        "mode": mode,
        "position": counts / 4096,
        "remaining": remaining / 4096,
        "measured_speed": speed,
        "encoder": encoder,
        "fault": fault,
        "encoder_raw": raw,
        "encoder_channel": channel,
        "magnet_status": magnet,
        "encoder_age_ms": age,
    }


LIMIT_TELEMETRY_LINE = re.compile(
    r"^LIM (\d+) (\d+) (bottom|top|insert) lower=(-?\d+) upper=(-?\d+) "
    r"bounds=(\d+) switch=(\d+) raw=(\d+)(?: counts=(-?\d+))?"
    rf"(?: calibration=({CALIBRATION_PHASE}))?"
    r"(?: warning=(none|direction_pause) warnings=(\d+))?$"
)


def parse_limit_telemetry(
    line: str,
    *,
    lower_limit_direction="ccw",
    positive_direction="cw",
    require_position=None,
    require_calibration=None,
    require_warnings=None,
) -> dict:
    match = LIMIT_TELEMETRY_LINE.fullmatch(line)
    if match is None:
        raise LinkError("Invalid Arduino limit telemetry frame.")
    (
        sequence,
        uptime,
        motor,
        lower,
        upper,
        bounds,
        mask,
        raw,
        counts,
        calibration,
        warning,
        warning_count,
    ) = match.groups()
    sequence, uptime, lower, upper, bounds, mask, raw = map(
        int, (sequence, uptime, lower, upper, bounds, mask, raw)
    )
    if (
        not all(0 <= value <= 0xFFFFFFFF for value in (sequence, uptime))
        or not 0 <= mask <= 3
        or not 0 <= raw <= 1023
    ):
        raise LinkError("Out-of-range Arduino limit telemetry frame.")
    _validate_limit_bounds(lower, upper, bounds, lower_limit_direction, positive_direction)
    if counts is not None and not -ENCODER_COUNT_LIMIT <= int(counts) <= ENCODER_COUNT_LIMIT:
        raise LinkError("Out-of-range position in Arduino limit telemetry.")
    if require_position is not None and require_position != (counts is not None):
        raise LinkError("Arduino limit telemetry does not match its homing capability.")
    if require_calibration is not None and require_calibration != (calibration is not None):
        raise LinkError("Arduino limit telemetry does not match its calibration capability.")
    if require_warnings is not None and require_warnings != (warning is not None):
        raise LinkError("Arduino limit telemetry does not match its direction-warning capability.")
    if warning_count is not None and not 0 <= int(warning_count) <= 65535:
        raise LinkError("Invalid direction-warning count in Arduino limit telemetry.")
    result = {
        "sequence": sequence,
        "uptime": uptime,
        "id": motor,
        "lower_limit": lower / ENCODER_COUNTS_PER_ROTATION,
        "upper_limit": upper / ENCODER_COUNTS_PER_ROTATION,
        "lower_limit_set": bool(bounds & 1),
        "upper_limit_set": bool(bounds & 2),
        "switch_mask": mask,
        "switch_raw": raw,
    }
    if counts is not None:
        result["position"] = int(counts) / ENCODER_COUNTS_PER_ROTATION
    if calibration is not None:
        result["calibration"] = calibration
    if warning is not None:
        result["warning"] = warning
        result["direction_warning_count"] = int(warning_count)
    return result


class SerialTransport:
    def __init__(self, port: str):
        self.selected = "bottom"
        self.microstep = None
        self.closed_loop = False
        self.telemetry_streaming = False
        self.travel_limits = False
        self.home_switches = False
        self.lower_home_zero = False
        self.calibration_only_homing = False
        self._counted_limits = False
        self.auto_calibration = False
        self.absolute_positioning = False
        self.hold_direction_warnings = False
        self.lower_limit_direction = None
        self._limit_convention = None
        self.positive_direction = "cw"
        self._coordinate_convention = None
        self._pending = bytearray()
        self._inbox = deque()
        self._states = {}
        self._telemetry = {}
        self._limit_telemetry = {}
        self._unversioned_limits = []
        self._sequence = None
        self._rx_order = 0
        self._stream_started = None
        self._limits_started = None
        self._status_at = None
        self._established = False
        try:
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

    def _feed(self, chunk):
        # One persistent assembler serves command replies and idle broadcasts.
        # A timeout or return from command() must never discard half a TEL line.
        self._pending.extend(chunk)
        while b"\n" in self._pending:
            end = self._pending.index(b"\n")
            if end > 2048:
                raise LinkError("Oversized Arduino reply. Check the uploaded sketch.")
            line = self._pending[:end].decode("ascii", errors="replace").strip()
            del self._pending[: end + 1]
            if line:
                self._inbox.append(line)
            if len(self._inbox) > 128:
                raise LinkError("Arduino reply backlog is too large. Reconnect.")
        if len(self._pending) > 2048:
            raise LinkError("Oversized Arduino reply. Check the uploaded sketch.")

    def _with_telemetry(self, state, *, dynamic=True):
        frame = self._telemetry.get(state.id)
        if frame is None:
            return replace(state, telemetry_age_ms=None)
        age = max(0, int((time.monotonic() - frame["received"]) * 1000))
        names = ("encoder_raw", "encoder_channel", "magnet_status", "encoder_age_ms")
        values = {name: frame[name] for name in names}
        if dynamic:
            names = (
                "direction",
                "mode",
                "position",
                "remaining",
                "measured_speed",
                "encoder",
                "fault",
            )
            values.update({name: frame[name] for name in names})
            if self._counted_limits:
                # Homing during calibration can rebase position and bounds.
                # Counted LIM carries their coherent coordinate snapshot.
                values.pop("position")
        values["encoder_age_ms"] += age
        return replace(state, **values, telemetry_age_ms=age)

    def _with_limits(self, state, *, dynamic=True):
        frame = self._limit_telemetry.get(state.id)
        if frame is None:
            return replace(state, limit_telemetry_age_ms=None)
        age = max(0, int((time.monotonic() - frame["received"]) * 1000))
        names = (
            "lower_limit",
            "upper_limit",
            "lower_limit_set",
            "upper_limit_set",
            "switch_mask",
            "switch_raw",
        )
        values = {name: frame[name] for name in names} if dynamic else {}
        if dynamic and self._counted_limits:
            values["position"] = frame["position"]
        if dynamic and self.auto_calibration:
            values["calibration"] = frame["calibration"]
        if dynamic and self.hold_direction_warnings:
            values["warning"] = frame["warning"]
            values["direction_warning_count"] = frame["direction_warning_count"]
        return replace(state, **values, limit_telemetry_age_ms=age)

    def _accept_telemetry(self, line, *, limits=False):
        frame = (
            parse_limit_telemetry(
                line,
                lower_limit_direction=self._limit_convention,
                positive_direction=self._coordinate_convention,
                require_position=self._counted_limits if self._established else None,
                require_calibration=self.auto_calibration if self._established else None,
                require_warnings=self.hold_direction_warnings if self._established else None,
            )
            if limits
            else parse_telemetry(line)
        )
        if limits and (self._limit_convention is None or self._coordinate_convention is None):
            self._unversioned_limits.append(frame)
            if len(self._unversioned_limits) > 128:
                raise LinkError("Arduino limit telemetry arrived without a capability reply.")
        sequence = frame["sequence"]
        if self._sequence is not None:
            distance = (sequence - self._sequence) & 0xFFFFFFFF
            if distance == 0 or distance >= 0x80000000:
                return  # Duplicate/older frames must not renew freshness.
        self._sequence = sequence
        frame.update(received=time.monotonic(), order=self._rx_order)
        cache = self._limit_telemetry if limits else self._telemetry
        merge = self._with_limits if limits else self._with_telemetry
        cache[frame["id"]] = frame
        if frame["id"] in self._states:
            self._states[frame["id"]] = merge(self._states[frame["id"]])

    def _next_reply(self):
        while self._inbox:
            line = self._inbox.popleft()
            self._rx_order += 1
            if line.startswith("TEL"):
                self._accept_telemetry(line)
                continue
            if line.startswith("LIM"):
                self._accept_telemetry(line, limits=True)
                continue
            if self._established and line.startswith("Stage closed-loop firmware ready."):
                raise LinkError(
                    "Arduino restarted; reconnect to establish its new position baseline."
                )
            if line.startswith("ERROR:"):
                raise CommandRejected(line)
            return line
        return None

    def _lines(self):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            line = self._next_reply()
            if line is not None:
                yield line
                continue
            try:
                self._feed(self.serial.read_until(b"\n", 512))
            except (serial.SerialException, OSError) as exc:
                raise LinkError(f"Serial read failed: {exc}") from exc
        raise LinkError("Arduino did not reply in time. Check the port and uploaded sketch.")

    def _drain_available(self):
        try:
            count = self.serial.in_waiting
            # Linux reports at most 4095 bytes for a tty; a full buffer means the
            # USB bridge drops bytes and the queued frames are stale.
            if count >= 4000:
                raise LinkError("Arduino telemetry backlog is too large to trust. Reconnect.")
            if count:
                self._feed(self.serial.read(count))
        except (serial.SerialException, OSError) as exc:
            raise LinkError(f"Serial read failed: {exc}") from exc
        while (line := self._next_reply()) is not None:
            if line.startswith(
                (
                    "OK:",
                    "motion=",
                    "microstep=",
                    "telemetry=",
                    "limits=",
                    "switches=",
                    "homing=",
                    "coordinates=",
                    "calibration=",
                    "positioning=",
                    "warnings=",
                )
            ):
                raise LinkError(f"Unexpected Arduino reply outside a command: {line}")

    def _command_accepted(self, verb, argument, command):
        # Do not let a cached pre-command idle frame prematurely finish a new move.
        # Firmware finishes an outstanding TEL line before executing the command;
        # the next TEL after its ACK therefore describes the resulting state.
        if command == "stop all":
            for state in self._states.values():
                state.mode, state.remaining, state.measured_speed = "idle", 0.0, 0.0
                state.warning = "none"
                if state.calibration in ACTIVE_CALIBRATION:
                    state.calibration = "cancelled"
            return
        state = self._states.get(self.selected)
        if state is None:
            return
        if verb == "dir":
            state.direction = argument
        elif verb == "speed":
            state.speed = float(argument)
        elif verb == "rev":
            state.mode = "moving"
            sign = 1 if state.direction == state.positive_direction else -1
            state.remaining = float(argument or 1) * sign
        elif verb == "goto":
            target = math.floor(float(argument) * ENCODER_COUNTS_PER_ROTATION + 0.5)
            current = round(state.position * ENCODER_COUNTS_PER_ROTATION)
            delta = (target - current) / ENCODER_COUNTS_PER_ROTATION
            state.mode = "idle" if abs(delta) <= state.tolerance else "moving"
            state.remaining = 0.0 if state.mode == "idle" else delta
            if state.mode == "idle":
                state.measured_speed = 0.0
            elif delta > 0:
                state.direction = state.positive_direction
            else:
                state.direction = "ccw" if state.positive_direction == "cw" else "cw"
        elif verb in ("hold", "start"):
            state.mode, state.remaining = "continuous", 0.0
        elif verb == "calibrate":
            state.mode, state.remaining, state.calibration = "continuous", 0.0, "lower"
        elif verb == "stop":
            state.mode, state.remaining, state.measured_speed = "idle", 0.0, 0.0
            state.warning = "none"
            if state.calibration in ACTIVE_CALIBRATION:
                state.calibration = "cancelled"

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
                self._command_accepted(verb, argument, command)
                return
            if line.startswith("OK:"):
                raise LinkError(f"Unexpected Arduino acknowledgement: {line}")

    def status(self) -> dict[str, MotorStatus]:
        self._write("status")
        statuses = {}
        microstep = None
        closed_loop = False
        streaming = False
        travel_limits = False
        home_switches = False
        lower_home_zero = False
        calibration_only_homing = False
        auto_calibration = False
        absolute_positioning = False
        hold_direction_warnings = False
        coordinates = False
        positive_direction = "cw"
        lower_limit_direction = None
        switch_direction = None
        row_order = {}
        for line in self._lines():
            if line.startswith("warnings"):
                if (
                    line != "warnings=held_direction_v1"
                    or hold_direction_warnings
                    or absolute_positioning
                    or auto_calibration
                    or coordinates
                    or lower_home_zero
                    or calibration_only_homing
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError(
                        "Invalid or duplicate direction-warning capability from Arduino."
                    )
                hold_direction_warnings = True
                continue
            if line.startswith("positioning"):
                if (
                    line != "positioning=calibrated_absolute_v1"
                    or absolute_positioning
                    or auto_calibration
                    or coordinates
                    or lower_home_zero
                    or calibration_only_homing
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate positioning capability from Arduino.")
                absolute_positioning = True
                continue
            if line.startswith("calibration"):
                if (
                    line != "calibration=switch_midpoint_v1"
                    or auto_calibration
                    or coordinates
                    or lower_home_zero
                    or calibration_only_homing
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate calibration capability from Arduino.")
                auto_calibration = True
                continue
            if line.startswith("coordinates"):
                if (
                    line != "coordinates=ccw_positive_v1"
                    or coordinates
                    or lower_home_zero
                    or calibration_only_homing
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate coordinate capability from Arduino.")
                coordinates = True
                positive_direction = "ccw"
                continue
            if line.startswith("homing"):
                if (
                    line not in ("homing=lower_zero_v1", "homing=calibration_only_v1")
                    or lower_home_zero
                    or calibration_only_homing
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate lower-home capability from Arduino.")
                lower_home_zero = line == "homing=lower_zero_v1"
                calibration_only_homing = line == "homing=calibration_only_v1"
                continue
            if line.startswith("switches"):
                if (
                    line not in ("switches=analog_pair_v1", "switches=analog_pair_v2")
                    or home_switches
                    or travel_limits
                    or streaming
                    or closed_loop
                    or microstep is not None
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate home-switch capability from Arduino.")
                home_switches = True
                switch_direction = "cw" if line.endswith("v2") else "ccw"
                continue
            if line.startswith("limits"):
                if (
                    line not in ("limits=soft_v1", "limits=directional_v2")
                    or travel_limits
                    or statuses
                ):
                    raise LinkError("Invalid or duplicate travel-limit capability from Arduino.")
                travel_limits = True
                lower_limit_direction = "cw" if line.endswith("v2") else "ccw"
                if (
                    self._limit_convention is not None
                    and self._limit_convention != lower_limit_direction
                ):
                    raise LinkError("Arduino travel-limit convention changed. Reconnect the board.")
                continue
            if line.startswith("telemetry"):
                if line != "telemetry=positions_v1" or streaming or statuses:
                    raise LinkError("Invalid or duplicate telemetry capability from Arduino.")
                streaming = True
                continue
            if line.startswith("motion"):
                if line != "motion=closed_loop_v1" or closed_loop or statuses:
                    raise LinkError("Invalid or duplicate motion capability from Arduino.")
                closed_loop = True
                continue
            if line.startswith("microstep"):
                match = re.fullmatch(r"microstep=(1|2|4|8|16)", line)
                if match is None or microstep is not None:
                    raise LinkError("Invalid or duplicate microstep status from Arduino.")
                microstep = int(match[1])
                continue
            parsed = parse_status(
                line,
                lower_limit_direction=lower_limit_direction,
                positive_direction=positive_direction,
                require_calibration=auto_calibration,
                require_warnings=hold_direction_warnings,
            )
            if parsed:
                if closed_loop != (parsed.encoder != "unknown"):
                    raise LinkError("Missing or inconsistent closed-loop encoder status.")
                if travel_limits != (parsed.lower_limit is not None):
                    raise LinkError("Missing or inconsistent travel-limit status.")
                if home_switches != (parsed.switch_mask is not None):
                    raise LinkError("Missing or inconsistent home-switch status.")
                if parsed.id in statuses:
                    raise LinkError("Duplicate motor in Arduino status reply.")
                statuses[parsed.id] = parsed
                row_order[parsed.id] = self._rx_order
                if line.startswith("*"):
                    self.selected = parsed.id
                if len(statuses) == len(MOTOR_IDS):
                    if closed_loop and microstep is None:
                        raise LinkError("Closed-loop firmware did not report microstep mode.")
                    if coordinates and not closed_loop:
                        raise LinkError("Coordinate capability requires closed-loop firmware.")
                    if (
                        self._coordinate_convention is not None
                        and self._coordinate_convention != positive_direction
                    ):
                        raise LinkError(
                            "Arduino coordinate convention changed. Reconnect the board."
                        )
                    self.microstep = microstep
                    self.closed_loop = closed_loop
                    if streaming and not closed_loop:
                        raise LinkError("Position telemetry requires closed-loop firmware.")
                    if travel_limits and not closed_loop:
                        raise LinkError("Travel limits require closed-loop firmware.")
                    if home_switches and not (travel_limits and streaming and closed_loop):
                        raise LinkError(
                            "Home switches require travel limits and position telemetry."
                        )
                    if home_switches and switch_direction != lower_limit_direction:
                        raise LinkError("Arduino switch and travel-limit conventions do not match.")
                    counted_limits = lower_home_zero or calibration_only_homing
                    if counted_limits and not (home_switches and lower_limit_direction == "cw"):
                        raise LinkError("Lower-home zeroing requires directional v2 home switches.")
                    if self._established and (
                        (lower_home_zero, calibration_only_homing)
                        != (self.lower_home_zero, self.calibration_only_homing)
                    ):
                        raise LinkError(
                            "Arduino lower-home capability changed. Reconnect the board."
                        )
                    if auto_calibration and not (counted_limits and positive_direction == "ccw"):
                        raise LinkError(
                            "Auto calibration requires CCW-positive lower-home firmware."
                        )
                    if self._established and auto_calibration != self.auto_calibration:
                        raise LinkError(
                            "Arduino calibration capability changed. Reconnect the board."
                        )
                    if absolute_positioning and not (
                        auto_calibration and calibration_only_homing and positive_direction == "ccw"
                    ):
                        raise LinkError(
                            "Absolute positioning requires CCW-positive calibration-only firmware."
                        )
                    if self._established and absolute_positioning != self.absolute_positioning:
                        raise LinkError(
                            "Arduino positioning capability changed. Reconnect the board."
                        )
                    if hold_direction_warnings and not (
                        counted_limits and streaming and closed_loop
                    ):
                        raise LinkError(
                            "Direction warnings require counted position/limit telemetry."
                        )
                    if (
                        self._established
                        and hold_direction_warnings != self.hold_direction_warnings
                    ):
                        raise LinkError(
                            "Arduino direction-warning capability changed. Reconnect the board."
                        )
                    if self._unversioned_limits:
                        if not home_switches:
                            raise LinkError("Arduino sent limit telemetry without switch support.")
                        for frame in self._unversioned_limits:
                            if hold_direction_warnings != ("warning" in frame):
                                raise LinkError(
                                    "Arduino limit telemetry does not match its "
                                    "direction-warning capability."
                                )
                            if auto_calibration != ("calibration" in frame):
                                raise LinkError(
                                    "Arduino limit telemetry does not match its "
                                    "calibration capability."
                                )
                            if counted_limits != ("position" in frame):
                                raise LinkError(
                                    "Arduino limit telemetry does not match its homing capability."
                                )
                            _validate_limit_bounds(
                                round(frame["lower_limit"] * ENCODER_COUNTS_PER_ROTATION),
                                round(frame["upper_limit"] * ENCODER_COUNTS_PER_ROTATION),
                                int(frame["lower_limit_set"])
                                | (int(frame["upper_limit_set"]) << 1),
                                lower_limit_direction,
                                positive_direction,
                            )
                        self._unversioned_limits.clear()
                    self.travel_limits = travel_limits
                    self.lower_home_zero = lower_home_zero
                    self.calibration_only_homing = calibration_only_homing
                    self._counted_limits = counted_limits
                    self.auto_calibration = auto_calibration
                    self.absolute_positioning = absolute_positioning
                    self.hold_direction_warnings = hold_direction_warnings
                    self.positive_direction = positive_direction
                    self._coordinate_convention = positive_direction
                    self.lower_limit_direction = lower_limit_direction
                    if lower_limit_direction is not None:
                        self._limit_convention = lower_limit_direction
                    if home_switches and not self.home_switches:
                        self._limits_started = time.monotonic()
                    self.home_switches = home_switches
                    if streaming and not self.telemetry_streaming:
                        self._stream_started = time.monotonic()
                    self.telemetry_streaming = streaming
                    self._established = True
                    self._status_at = time.monotonic()
                    self._states = {
                        motor: self._with_telemetry(
                            state,
                            dynamic=self._telemetry.get(motor, {}).get("order", -1)
                            > row_order[motor],
                        )
                        if streaming
                        else state
                        for motor, state in statuses.items()
                    }
                    if home_switches:
                        self._states = {
                            motor: self._with_limits(
                                state,
                                dynamic=self._limit_telemetry.get(motor, {}).get("order", -1)
                                > row_order[motor],
                            )
                            for motor, state in self._states.items()
                        }
                    return {motor: replace(state) for motor, state in self._states.items()}
            elif re.match(r"\*?\s*(bottom|top|insert)\b", line):
                raise LinkError("Invalid motor status from Arduino.")
        raise LinkError("Incomplete Arduino status.")  # Defensive: _lines raises on timeout.

    def poll_status(self) -> dict[str, MotorStatus]:
        if not self.telemetry_streaming:
            # Older firmware is still readable, with its original polling cadence.
            if self._status_at is None or time.monotonic() - self._status_at >= 0.2:
                return self.status()
            return {motor: replace(state) for motor, state in self._states.items()}
        self._drain_available()
        now = time.monotonic()
        for motor in MOTOR_IDS:
            received = self._telemetry.get(motor, {}).get("received", self._stream_started)
            if received is None or now - received > TELEMETRY_TIMEOUT_SECONDS:
                raise LinkError(
                    f"{motor.title()} position telemetry stopped. Reconnect the Arduino."
                )
            if self.home_switches:
                received = self._limit_telemetry.get(motor, {}).get(
                    "received", self._limits_started
                )
                if received is None or now - received > TELEMETRY_TIMEOUT_SECONDS:
                    raise LinkError(
                        f"{motor.title()} limit/switch telemetry stopped. Reconnect the Arduino."
                    )
        # Only ages are recomputed here; applying an old frame's motion fields
        # would overwrite the optimistic state of an acknowledged new command.
        states = {
            motor: self._with_telemetry(state, dynamic=False)
            for motor, state in self._states.items()
        }
        if self.home_switches:
            states = {
                motor: self._with_limits(state, dynamic=False) for motor, state in states.items()
            }
        return states

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
            motor: MotorStatus(
                motor,
                encoder="ok",
                tolerance=math.ceil(0.5 * 4096 / (200 * REQUIRED_MICROSTEP) + 2) / 4096,
                max_speed=800 / (200 * REQUIRED_MICROSTEP),
                lower_limit=-POSITION_LIMIT,
                upper_limit=POSITION_LIMIT,
                lower_limit_set=False,
                upper_limit_set=False,
                lower_limit_direction="cw",
                positive_direction="ccw",
                switch_mask=0,
                switch_raw=1023,
                limit_telemetry_age_ms=0,
            )
            for motor in MOTOR_IDS
        }
        self.moves = {}
        self._absolute_directions = {}
        self.holds = {}
        self._hold_pauses = {}
        self._direction_incidents = {motor: deque(maxlen=3) for motor in MOTOR_IDS}
        self.calibrations = {}
        # Demo contacts are fixed in physical CW-positive shaft coordinates.
        self._calibration_endpoints = dict.fromkeys(MOTOR_IDS, (0.0, -1.0))
        self.microstep = REQUIRED_MICROSTEP
        self.closed_loop = True
        self.telemetry_streaming = True
        self.travel_limits = True
        self.home_switches = True
        self.lower_home_zero = False
        self.calibration_only_homing = True
        self.auto_calibration = True
        self.absolute_positioning = True
        self.hold_direction_warnings = True
        self.lower_limit_direction = "cw"
        self.positive_direction = "ccw"
        self._encoder_origins = dict.fromkeys(MOTOR_IDS, 0.0)

    def _start_calibration_phase(self, motor, phase, started, speed):
        state = self.motors[motor]
        original_direction = self.calibrations.get(motor, {}).get(
            "original_direction", state.direction
        )
        physical_sign = 1 if state.positive_direction == "cw" else -1
        if phase == "center":
            target = math.floor((state.lower_limit + state.upper_limit) * 4096 / 2 + 0.5) / 4096
            direction = (
                state.positive_direction
                if target >= state.position
                else ("ccw" if state.positive_direction == "cw" else "cw")
            )
        else:
            physical = self._calibration_endpoints[motor][0 if phase == "lower" else 1]
            target = (physical - self._encoder_origins[motor]) * physical_sign
            direction = "cw" if phase == "lower" else "ccw"
        state.calibration, state.direction = phase, direction
        state.mode = "moving" if phase == "center" else "continuous"
        state.remaining = target - state.position if phase == "center" else 0.0
        state.measured_speed = 0.0
        self.calibrations[motor] = {
            "started": started,
            "start": state.position,
            "target": target,
            "speed": speed,
            "original_direction": original_direction,
        }

    def _end_calibration(self, motor, outcome, fault=None):
        state = self.motors[motor]
        run = self.calibrations.pop(motor, None)
        if run is not None:
            state.direction = run["original_direction"]
        state.calibration, state.mode = outcome, "idle"
        state.remaining = state.measured_speed = 0.0
        if fault is not None:
            state.fault = fault
        if outcome == "done":
            state.warning, state.direction_warning_count = "none", 0
            self._direction_incidents[motor].clear()

    def _end_hold(self, motor, fault=None):
        state = self.motors[motor]
        self.holds.pop(motor, None)
        self._hold_pauses.pop(motor, None)
        state.warning, state.mode = "none", "idle"
        state.remaining = state.measured_speed = 0.0
        if fault is not None:
            state.fault = fault

    def inject_hold_direction_warning(self, motor, excursion_counts):
        """Demo/test hook for a detected feedback incident; never exposed by the API.

        Input is the larger backward excursion from the fixed hold origin or
        retry anchor. This models the retry policy without editing the measured
        public position or saved bounds.
        """
        if motor not in MOTOR_IDS or type(excursion_counts) is not int or excursion_counts < 0:
            raise ValueError("Choose a valid motor and a nonnegative integer excursion.")
        self._update()
        if motor not in self.holds:
            raise CommandRejected("Direction-warning simulation requires an active held move.")
        state = self.motors[motor]
        soft = max(32, 2 * 4096 / (state.steps_per_revolution * self.microstep))
        hard = 2 * soft
        now = self.clock()
        if motor in self._hold_pauses:
            if excursion_counts >= hard:
                self._end_hold(motor, "direction")
            elif excursion_counts > soft:
                pause = self._hold_pauses[motor]
                pause.update(
                    stable_since=now,
                    samples=0,
                    last_sample=now,
                    anchor=round(state.position * 4096),
                )
            return
        if excursion_counts <= soft:
            return
        state.direction_warning_count = min(65535, state.direction_warning_count + 1)
        incidents = self._direction_incidents[motor]
        while incidents and now - incidents[0] > 10:
            incidents.popleft()
        incidents.append(now)
        if excursion_counts >= hard or len(incidents) >= 3:
            self._end_hold(motor, "direction")
            return
        self._hold_pauses[motor] = {
            "started": now,
            "stable_since": now,
            "samples": 0,
            "last_sample": now,
            "anchor": round(state.position * 4096),
        }
        state.warning, state.measured_speed = "direction_pause", 0.0

    def _update_calibrations(self, now):
        for motor in list(self.calibrations):
            # At most three phases can finish during a delayed read. Advancing
            # their actual completion times keeps calibration independent of polling.
            for _ in range(3):
                state, run = self.motors[motor], self.calibrations[motor]
                if state.encoder != "ok" or state.fault != "none":
                    self._end_calibration(
                        motor, "failed", state.fault if state.fault != "none" else "encoder"
                    )
                    break
                phase, speed = state.calibration, run["speed"]
                sign = 1 if state.direction == state.positive_direction else -1
                distance = max(0.0, sign * (run["target"] - run["start"]))
                elapsed = max(0.0, now - run["started"])
                if phase == "center":
                    duration = distance / speed
                    travel, velocity = min(distance, elapsed * speed), speed
                else:
                    ramp = speed / state.acceleration
                    ramp_distance = 0.5 * speed * ramp
                    duration = (
                        math.sqrt(2 * distance / state.acceleration)
                        if distance <= ramp_distance
                        else distance / speed + 0.5 * ramp
                    )
                    active_time = min(elapsed, 120.0)
                    ramp_time = min(active_time, ramp)
                    travel = min(
                        distance,
                        0.5 * state.acceleration * ramp_time * ramp_time
                        + speed * (active_time - ramp_time),
                    )
                    velocity = min(speed, state.acceleration * active_time)
                state.position = run["start"] + sign * travel
                state.measured_speed = sign * velocity
                state.remaining = run["target"] - state.position if phase == "center" else 0.0
                if travel > 0:
                    state.switch_mask, state.switch_raw = 0, 1023
                if abs(state.position) > POSITION_LIMIT:
                    state.position = max(-POSITION_LIMIT, min(POSITION_LIMIT, state.position))
                    self._end_calibration(motor, "failed", "tracking")
                    break
                if phase != "center" and elapsed >= 120 and duration > 120:
                    self._end_calibration(motor, "failed", "timeout")
                    break
                if elapsed < duration:
                    break
                finished = run["started"] + duration
                if phase == "lower":
                    physical_sign = 1 if state.positive_direction == "cw" else -1
                    self._encoder_origins[motor] += state.position * physical_sign
                    state.position = state.lower_limit = 0.0
                    state.lower_limit_set = True
                    state.switch_mask, state.switch_raw = 1, 250
                    self._start_calibration_phase(motor, "upper", finished, speed)
                elif phase == "upper":
                    state.upper_limit, state.upper_limit_set = state.position, True
                    state.switch_mask, state.switch_raw = 2, 0
                    if state.upper_limit <= state.lower_limit + state.tolerance:
                        self._end_calibration(motor, "failed", "tracking")
                        break
                    self._start_calibration_phase(motor, "center", finished, speed)
                else:
                    state.switch_mask, state.switch_raw = 0, 1023
                    self._end_calibration(motor, "done")
                    break

    def _update(self):
        now = self.clock()
        for motor, (started, start, deadline) in list(self.holds.items()):
            state = self.motors[motor]
            if self._contact_blocks(state):
                self._end_hold(motor)
                continue
            pause = self._hold_pauses.get(motor)
            if pause is not None:
                if state.encoder != "ok" or state.fault != "none":
                    self._end_hold(motor, state.fault if state.fault != "none" else "encoder")
                    continue
                if now >= deadline:
                    self._end_hold(motor)
                    continue
                if now > pause["last_sample"]:
                    count = round(state.position * 4096)
                    if abs(count - pause["anchor"]) > 4:
                        pause.update(anchor=count, stable_since=now, samples=1)
                    else:
                        pause["samples"] += 1
                    pause["last_sample"] = now
                if now - pause["started"] >= 0.5:
                    self._end_hold(motor, "direction")
                    continue
                if pause["samples"] >= 3 and now - pause["stable_since"] >= 0.1:
                    self._hold_pauses.pop(motor)
                    state.warning = "none"
                    self.holds[motor] = (now, state.position, deadline)
                state.measured_speed = 0.0
                continue
            elapsed = max(0.0, min(now, deadline) - started)
            sign = 1 if state.direction == state.positive_direction else -1
            # Keep the original start across heartbeats: held movement ramps
            # once, then cruises. Stops still take effect immediately.
            ramp_time = min(elapsed, state.speed / state.acceleration)
            distance = 0.5 * state.acceleration * ramp_time * ramp_time + state.speed * (
                elapsed - ramp_time
            )
            state.position = start + sign * distance
            state.measured_speed = sign * min(state.speed, state.acceleration * elapsed)
            reached_limit = False
            side = "lower" if state.direction == state.lower_limit_direction else "upper"
            bound = getattr(state, f"{side}_limit")
            if getattr(state, f"{side}_limit_set") and sign * state.position >= sign * bound:
                state.position = bound
                reached_limit = True
            if state.position >= POSITION_LIMIT or state.position <= -POSITION_LIMIT:
                state.position = max(-POSITION_LIMIT, min(POSITION_LIMIT, state.position))
                state.fault = "tracking"
                reached_limit = True
            if now >= deadline or reached_limit:
                self._end_hold(motor)
        for motor, (started, duration, start, delta) in list(self.moves.items()):
            state = self.motors[motor]
            if motor in self._absolute_directions and (
                state.encoder != "ok" or state.fault != "none"
            ):
                if state.fault == "none":
                    state.fault = "encoder"
                state.mode, state.remaining, state.measured_speed = "idle", 0.0, 0.0
                del self.moves[motor]
                self._restore_absolute_direction(motor)
                continue
            if self._contact_blocks(state):
                state.mode, state.remaining, state.measured_speed = "idle", 0.0, 0.0
                del self.moves[motor]
                self._restore_absolute_direction(motor)
                continue
            fraction = min(1.0, (self.clock() - started) / duration)
            state.position = start + delta * fraction
            state.remaining = start + delta - state.position
            state.measured_speed = delta / duration
            if fraction >= 1:
                state.mode = "idle"
                state.measured_speed = 0.0
                del self.moves[motor]
                self._restore_absolute_direction(motor)
        self._update_calibrations(now)

    @staticmethod
    def _contact_blocks(state):
        bit = 1 if state.direction == state.lower_limit_direction else 2
        return bool((state.switch_mask or 0) & bit)

    def _restore_absolute_direction(self, motor):
        direction = self._absolute_directions.pop(motor, None)
        if direction is not None:
            self.motors[motor].direction = direction

    def command(self, command: str) -> None:
        self._update()
        verb, _, argument = command.partition(" ")
        if verb == "microstep":
            if argument not in {str(value) for value in MICROSTEP_MODES}:
                raise CommandRejected("All stages use fixed 1/8 stepping (microstep 8).")
            if any(
                state.mode != "idle" or state.calibration in ACTIVE_CALIBRATION
                for state in self.motors.values()
            ):
                raise CommandRejected("All motors must be idle before changing microstep mode.")
            self.microstep = int(argument)
            for state in self.motors.values():
                state.max_speed = min(
                    MAX_SPEED, 800 / (state.steps_per_revolution * self.microstep)
                )
                state.tolerance = (
                    math.ceil(0.5 * 4096 / (state.steps_per_revolution * self.microstep) + 2) / 4096
                )
                state.speed = min(state.speed, state.max_speed)
            return
        if command == "stop all":
            for motor in list(self.calibrations):
                self._end_calibration(motor, "cancelled")
            self.moves.clear()
            for motor in list(self._absolute_directions):
                self._restore_absolute_direction(motor)
            for motor in list(self.holds):
                self._end_hold(motor)
            for state in self.motors.values():
                state.mode = "idle"
                state.remaining = 0
                state.measured_speed = 0.0
            return
        if verb == "motor":
            self.selected = argument
            return
        state = self.motors[self.selected]
        if verb == "stop":
            if state.calibration in ACTIVE_CALIBRATION:
                self._end_calibration(self.selected, "cancelled")
            self.moves.pop(self.selected, None)
            self._restore_absolute_direction(self.selected)
            if self.selected in self.holds:
                self._end_hold(self.selected)
            state.mode = "idle"
            state.remaining = 0
            state.measured_speed = 0.0
            return
        if verb == "keepalive":
            if self.selected not in self.holds:
                raise CommandRejected("Hold has ended. Release and press again.")
            started, start, _ = self.holds[self.selected]
            self.holds[self.selected] = (started, start, self.clock() + HOLD_TIMEOUT_SECONDS)
            return
        if state.mode != "idle" or state.calibration in ACTIVE_CALIBRATION:
            raise CommandRejected("Motor is already moving.")
        if verb == "calibrate":
            if not (
                self.auto_calibration
                and (self.lower_home_zero or self.calibration_only_homing)
                and self.home_switches
                and state.positive_direction == "ccw"
                and state.lower_limit_direction == "cw"
            ):
                raise CommandRejected("Upload firmware with automatic switch calibration support.")
            if state.encoder != "ok" or state.fault != "none":
                raise CommandRejected("A healthy, trusted encoder with no fault is required.")
            if state.switch_mask == 3:
                raise CommandRejected("Resolve the conflicting home-switch signals first.")
            state.lower_limit, state.upper_limit = limit_defaults(
                state.lower_limit_direction, state.positive_direction
            )
            state.lower_limit_set = state.upper_limit_set = False
            self._start_calibration_phase(state.id, "lower", self.clock(), min(state.speed, 0.25))
            return
        if verb in ("reset", "setlimit", "clearupper", "clearlower"):
            if not self.travel_limits:
                raise CommandRejected("Upload firmware with position reset and travel limits.")
            if verb in ("reset", "setlimit") and state.encoder != "ok":
                raise CommandRejected("A healthy, trusted encoder is required.")
            if verb == "reset":
                physical_sign = 1 if state.positive_direction == "cw" else -1
                self._encoder_origins[state.id] += state.position * physical_sign
                state.position = state.remaining = state.measured_speed = 0.0
                state.lower_limit, state.upper_limit = limit_defaults(
                    state.lower_limit_direction, state.positive_direction
                )
                state.lower_limit_set = state.upper_limit_set = False
            elif verb == "setlimit":
                count = round(state.position * ENCODER_COUNTS_PER_ROTATION)
                position = count / ENCODER_COUNTS_PER_ROTATION
                if not -ENCODER_COUNT_LIMIT <= count <= ENCODER_COUNT_LIMIT:
                    raise CommandRejected("Encoder position is outside the supported range.")
                if count == 0:
                    raise CommandRejected("Move away from zero before setting a travel limit.")
                direction = (
                    state.positive_direction
                    if count > 0
                    else ("ccw" if state.positive_direction == "cw" else "cw")
                )
                side = "lower" if direction == state.lower_limit_direction else "upper"
                opposite = "upper" if side == "lower" else "lower"
                sign = 1 if count > 0 else -1
                if getattr(state, f"{opposite}_limit_set") and sign * position < sign * getattr(
                    state, f"{opposite}_limit"
                ):
                    raise CommandRejected(
                        "The captured limit would cross the opposite travel limit."
                    )
                setattr(state, f"{side}_limit", position)
                setattr(state, f"{side}_limit_set", True)
            elif verb == "clearupper":
                state.upper_limit = limit_defaults(
                    state.lower_limit_direction, state.positive_direction
                )[1]
                state.upper_limit_set = False
            else:
                state.lower_limit = limit_defaults(
                    state.lower_limit_direction, state.positive_direction
                )[0]
                state.lower_limit_set = False
            state.calibration = "none"
            return
        if verb == "clear":
            if state.encoder in ("offline", "magnet"):
                raise CommandRejected("Restore the encoder connection and magnet first.")
            if state.encoder == "untrusted":
                if state.lower_limit_set or state.upper_limit_set:
                    raise CommandRejected(
                        "Position tracking was lost. Clear both travel limits before clearing "
                        "the encoder fault."
                    )
                physical_sign = 1 if state.positive_direction == "cw" else -1
                self._encoder_origins[state.id] += state.position * physical_sign
                state.position = 0.0
            state.encoder = "ok"
            state.fault = "none"
            return
        if verb in ("rev", "hold", "goto") and (state.encoder != "ok" or state.fault != "none"):
            raise CommandRejected("Encoder feedback is not ready. Clear the fault first.")
        if verb == "dir":
            state.direction = argument
        elif verb == "speed":
            value = float(argument)
            if not math.isfinite(value) or not MIN_SPEED <= value <= state.max_speed:
                raise CommandRejected("Speed exceeds this motor's rotation/s limit.")
            state.speed = value
        elif verb == "hold":
            if self.travel_limits and (error := motion_limit_error(state, state.direction)):
                raise CommandRejected(error)
            state.mode = "continuous"
            state.warning = "none"
            state.remaining = 0
            self.holds[state.id] = (
                self.clock(),
                state.position,
                self.clock() + HOLD_TIMEOUT_SECONDS,
            )
        elif verb == "goto":
            if not (
                self.absolute_positioning and self.auto_calibration and self.calibration_only_homing
            ):
                raise CommandRejected(
                    "Upload firmware with calibrated absolute positioning support."
                )
            value = float(argument)
            if not math.isfinite(value) or not 0 <= value <= POSITION_LIMIT:
                raise CommandRejected(
                    "Position must be a nonnegative value within the encoder range."
                )
            if error := absolute_position_error(state, value):
                raise CommandRejected(error)
            target = math.floor(value * ENCODER_COUNTS_PER_ROTATION + 0.5)
            current = round(state.position * ENCODER_COUNTS_PER_ROTATION)
            delta = (target - current) / ENCODER_COUNTS_PER_ROTATION
            if abs(delta) / state.speed > 86400:
                raise CommandRejected("A single move must take at most 24 hours.")
            if abs(delta) <= state.tolerance:
                state.remaining = state.measured_speed = 0.0
                return
            self._absolute_directions[state.id] = state.direction
            state.direction = (
                state.positive_direction
                if delta > 0
                else ("ccw" if state.positive_direction == "cw" else "cw")
            )
            state.mode, state.remaining = "moving", delta
            self.moves[state.id] = (
                self.clock(),
                max(0.25, abs(delta) / state.speed),
                state.position,
                target / ENCODER_COUNTS_PER_ROTATION - state.position,
            )
        elif verb == "rev":
            value = float(argument)
            if (
                not math.isfinite(value)
                or not MIN_ROTATIONS <= value <= MAX_ROTATIONS
                or value / state.speed > 86400
            ):
                raise CommandRejected("Invalid rotations or move duration exceeds 24 hours.")
            if self.travel_limits and (error := motion_limit_error(state, state.direction, value)):
                raise CommandRejected(error)
            delta = value * (1 if state.direction == state.positive_direction else -1)
            state.mode = "moving"
            state.remaining = delta
            self.moves[state.id] = (
                self.clock(),
                max(0.25, abs(delta) / state.speed),
                state.position,
                delta,
            )
        else:
            raise CommandRejected("Unknown command.")

    def status(self) -> dict[str, MotorStatus]:
        self._update()
        channels = {"bottom": 0, "top": 2, "insert": 1}
        return {
            motor: replace(
                state,
                encoder_raw=round(
                    (
                        state.position * (1 if state.positive_direction == "cw" else -1)
                        + self._encoder_origins[motor]
                    )
                    * 4096
                )
                % 4096,
                encoder_channel=channels[motor],
                magnet_status=32,
                encoder_age_ms=0,
                telemetry_age_ms=0,
                limit_telemetry_age_ms=0,
            )
            for motor, state in self.motors.items()
        }

    def poll_status(self) -> dict[str, MotorStatus]:
        return self.status()

    def close(self) -> None:
        for motor in list(self.calibrations):
            self._end_calibration(motor, "cancelled")
        self.moves.clear()
        for motor in list(self._absolute_directions):
            self._restore_absolute_direction(motor)
        for motor in list(self.holds):
            self._end_hold(motor)
