"""Offline dual-axis firmware model with an explicit virtual clock.

``command`` returns immediate reply lines. ``advance`` returns asynchronous
completion lines. Nothing opens a device or starts a thread. A host must still
establish a focus reference and enforce its own automated-motion interlocks.
"""

import math
import re
from dataclasses import dataclass

MIN_POSITION = -(2**31)
MAX_POSITION = 2**31 - 1


@dataclass
class Motor:
    name: str
    speed: int
    enabled: bool = False
    moving: bool = False
    position: int = 0
    remaining: int = 0
    direction: int = 1
    last_step_us: int = 0


class MicroscopeSimulator:
    """Model the serial grammar without a serial port, real time, or hardware.

    The optional switch represents a test fixture. Defaults match the installed
    board: no switch and no verified direction or travel budget. ``advance``
    runs the nonblocking firmware loop at 100 microsecond intervals.
    """

    def __init__(
        self,
        *,
        home_switch: bool = False,
        home_direction: int = 0,
        home_max_steps: int = 0,
        home_speed: int = 100,
        home_timeout_ms: int = 30000,
    ):
        if type(home_switch) is not bool:
            raise ValueError("home_switch must be boolean")
        integers = (home_direction, home_max_steps, home_speed, home_timeout_ms)
        if any(type(value) is not int for value in integers):
            raise ValueError("home settings must be integers")
        if (
            home_direction not in (-1, 0, 1)
            or not 0 <= home_max_steps <= MAX_POSITION
            or not 1 <= home_speed <= 10000
            or not 20 <= home_timeout_ms <= 60000
        ):
            raise ValueError("invalid home settings")
        self.home_switch = home_switch
        self.home_direction = home_direction
        self.home_max_steps = home_max_steps
        self.home_speed = home_speed
        self.home_timeout_ms = home_timeout_ms
        self.reset()

    def reset(self) -> list[str]:
        """Model power-up: disable both motors and lose every focus reference."""
        self.objective = Motor("OBJ", 400)
        self.focus = Motor("FOCUS", 800)
        self.microstep = 1
        self._clear_reference()
        self.homing = False
        self._home_saved_speed = 800
        self._home_started_us = 0
        self._switch_since_us = None
        self._switch_active = False
        self._now_us = 0
        self._line = bytearray()
        self._discard = False
        return ["DUAL_A4988_READY", *self.status()]

    def _clear_reference(self):
        self.zeroed = self.limited = False
        self.minimum = self.maximum = 0

    def status(self) -> list[str]:
        lines = [f"MICRO={self.microstep}"]
        for motor in (self.objective, self.focus):
            lines.append(
                f"{motor.name}: enabled={int(motor.enabled)} moving={int(motor.moving)} "
                f"speed={motor.speed} remaining={motor.remaining} pos={motor.position}"
            )
        lines.append(
            f"FOCUS_REF: zeroed={int(self.zeroed)} limited={int(self.limited)} "
            f"min={self.minimum} max={self.maximum} homing={int(self.homing)}"
        )
        return lines

    def _stop(self, motor):
        if motor is self.focus:
            if self.homing:
                motor.speed = self._home_saved_speed
            self.homing = False
            self._switch_since_us = None
        motor.moving = False
        motor.remaining = 0

    @staticmethod
    def _integer(text):
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            return None
        value = int(text)
        return value if MIN_POSITION <= value <= MAX_POSITION else None

    def command(self, line: str) -> list[str]:
        """Send exactly one command; use ``feed`` for fragmented serial bytes."""
        if not isinstance(line, str) or "\n" in line or "\r" in line:
            raise ValueError("command must contain one line without a terminator")
        return self.feed(line.encode("utf-8") + b"\n")

    def feed(self, data: bytes) -> list[str]:
        """Consume serial bytes. Reject an entire invalid or overlong line."""
        replies = []
        for byte in data:
            if byte == 13:
                continue
            if byte == 10:
                if not self._discard and self._line:
                    replies.extend(self._process(self._line.decode("ascii")))
                self._line.clear()
                self._discard = False
            elif self._discard:
                continue
            elif (byte < 32 and byte != 9) or byte > 126:
                self._line.clear()
                self._discard = True
                replies.append("ERR LINE_CHARACTER")
            elif len(self._line) < 95:
                self._line.append(byte)
            else:
                self._line.clear()
                self._discard = True
                replies.append("ERR LINE_TOO_LONG")
        return replies

    def _process(self, line):
        words = line.upper().split()
        if not words:
            return []
        name, *args = words
        if name in ("STATUS", "VERSION", "HELP"):
            if args:
                return ["ERR EXTRA_ARGUMENTS"]
            if name == "STATUS":
                return self.status()
            if name == "VERSION":
                return ["VERSION microscope-axes 1.0.0"]
            return [
                "Commands:",
                "  OBJ ENABLE",
                "  OBJ DISABLE",
                "  OBJ MOVE <signed_steps>",
                "  OBJ SPEED <steps_per_second>",
                "  FOCUS ENABLE",
                "  FOCUS DISABLE",
                "  FOCUS MOVE <signed_steps>",
                "  FOCUS SPEED <steps_per_second>",
                "  MICRO <1|2|4|8|16>",
                "  STOP OBJ",
                "  STOP FOCUS",
                "  STOP ALL",
                "  STATUS",
                "  HELP",
                "  FOCUS ZERO",
                "  FOCUS LIMIT <min> <max>",
                "  FOCUS HOME",
                "  VERSION",
            ]
        if name == "MICRO":
            if not args:
                return ["ERR MICRO VALUE"]
            if len(args) != 1:
                return ["ERR EXTRA_ARGUMENTS"]
            value = self._integer(args[0])
            if value not in (1, 2, 4, 8, 16):
                return ["ERR MICRO VALID=1,2,4,8,16"]
            if self.objective.moving or self.focus.moving:
                return ["ERR MICRO WHILE_MOVING"]
            if self.microstep != value:
                self._clear_reference()
            self.microstep = value
            return [f"OK MICRO {value}"]
        if name == "STOP":
            if not args:
                return ["ERR STOP TARGET"]
            if len(args) != 1:
                return ["ERR EXTRA_ARGUMENTS"]
            if args[0] not in ("OBJ", "FOCUS", "ALL"):
                return ["ERR STOP TARGET"]
            if args[0] in ("OBJ", "ALL"):
                self._stop(self.objective)
            if args[0] in ("FOCUS", "ALL"):
                self._stop(self.focus)
            return [f"OK STOP {args[0]}"]
        if name not in ("OBJ", "FOCUS"):
            return ["ERR UNKNOWN_COMMAND"]
        if not args:
            return ["ERR MISSING_ACTION"]
        motor = self.objective if name == "OBJ" else self.focus
        action, *values = args
        if action in ("ENABLE", "DISABLE"):
            if values:
                return ["ERR EXTRA_ARGUMENTS"]
            motor.enabled = action == "ENABLE"
            if not motor.enabled:
                self._stop(motor)
                if motor is self.focus:
                    self._clear_reference()
            return [f"OK {name} {action}D"]
        if action in ("MOVE", "SPEED"):
            error = "ERR MOVE STEPS" if action == "MOVE" else "ERR SPEED VALUE"
            if not values:
                return [error]
            if len(values) != 1:
                return ["ERR EXTRA_ARGUMENTS"]
            value = self._integer(values[0])
            if action == "SPEED":
                if motor is self.focus and self.homing:
                    return ["ERR FOCUS BUSY"]
                if value is None or not 1 <= value <= 10000:
                    return ["ERR SPEED RANGE 1-10000"]
                motor.speed = value
                return [f"OK {name} SPEED {value}"]
            if value is None:
                return [error]
            if not motor.enabled:
                return [f"ERR {name} DISABLED"]
            if motor.moving:
                return [f"ERR {name} BUSY"]
            target = motor.position + value
            if not MIN_POSITION <= target <= MAX_POSITION:
                return ["ERR MOVE POSITION_RANGE"]
            if motor is self.focus and self.limited and not self.minimum <= target <= self.maximum:
                return ["ERR FOCUS LIMIT"]
            if value:
                motor.direction = 1 if value > 0 else -1
                motor.remaining = abs(value)
                motor.last_step_us = self._now_us
                motor.moving = True
            return [f"OK {name} MOVE {value}"]
        if name == "FOCUS" and action in ("ZERO", "LIMIT", "HOME"):
            if action == "LIMIT":
                if len(values) < 2:
                    return ["ERR FOCUS LIMIT RANGE"]
                minimum, maximum = map(self._integer, values[:2])
                if minimum is None or maximum is None or minimum > maximum:
                    return ["ERR FOCUS LIMIT RANGE"]
                values = values[2:]
            if values:
                return ["ERR EXTRA_ARGUMENTS"]
            if not motor.enabled:
                return ["ERR FOCUS DISABLED"]
            if motor.moving:
                return ["ERR FOCUS BUSY"]
            if action == "ZERO":
                self._clear_reference()
                motor.position = 0
                self.zeroed = True
                return ["OK FOCUS ZERO"]
            if action == "HOME":
                return self._home()
            if not self.zeroed:
                return ["ERR FOCUS NOT_ZEROED"]
            if not minimum <= motor.position <= maximum:
                return ["ERR FOCUS LIMIT POSITION"]
            self.minimum, self.maximum = minimum, maximum
            self.limited = True
            return [f"OK FOCUS LIMIT {minimum} {maximum}"]
        return ["ERR UNKNOWN_ACTION"]

    def _home(self):
        if not self.home_switch:
            return ["ERR FOCUS HOME NO_SWITCH"]
        if not self.home_direction or not self.home_max_steps:
            return ["ERR FOCUS HOME NOT_CONFIGURED"]
        if self._switch_active:
            return ["ERR FOCUS HOME SWITCH_ACTIVE"]
        target = self.focus.position + self.home_direction * self.home_max_steps
        if not MIN_POSITION <= target <= MAX_POSITION:
            return ["ERR MOVE POSITION_RANGE"]
        self._clear_reference()
        self._home_saved_speed = self.focus.speed
        self.focus.speed = self.home_speed
        self.focus.direction = self.home_direction
        self.focus.remaining = self.home_max_steps
        self.focus.last_step_us = self._now_us
        self.focus.moving = self.homing = True
        self._switch_since_us = None
        self._home_started_us = self._now_us
        return ["OK FOCUS HOME"]

    def set_home_switch(self, active: bool):
        """Set the fake switch input without advancing virtual time."""
        if type(active) is not bool:
            raise ValueError("active must be boolean")
        self._switch_active = active

    def advance(self, seconds: float) -> list[str]:
        """Advance at most 60 seconds of virtual time, without sleeping."""
        if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError("seconds must be finite and within 0..60")
        replies = []
        stop = self._now_us + round(seconds * 1_000_000)
        while self._now_us < stop:
            self._now_us = min(stop, self._now_us + 100)
            for motor in (self.objective, self.focus):
                if motor is self.focus and self.homing:
                    if self._now_us // 1000 - self._home_started_us // 1000 >= self.home_timeout_ms:
                        self._stop(motor)
                        replies.append("ERR FOCUS HOME TIMEOUT")
                        continue
                    if self._switch_active:
                        if self._switch_since_us is None:
                            self._switch_since_us = self._now_us
                        elif self._now_us // 1000 - self._switch_since_us // 1000 >= 20:
                            self._stop(motor)
                            motor.position = 0
                            self.zeroed = True
                            replies.append("DONE FOCUS HOME")
                        continue
                    self._switch_since_us = None
                if not motor.enabled or not motor.moving or not motor.remaining:
                    continue
                if self._now_us - motor.last_step_us < 1_000_000 // motor.speed:
                    continue
                motor.last_step_us = self._now_us
                motor.position += motor.direction
                motor.remaining -= 1
                if not motor.remaining:
                    if motor is self.focus and self.homing:
                        self._stop(motor)
                        replies.append("ERR FOCUS HOME TRAVEL")
                    else:
                        motor.moving = False
                        replies.append(f"DONE {motor.name} POS {motor.position}")
        return replies
