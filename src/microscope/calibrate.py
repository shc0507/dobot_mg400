"""Supervised calibration commands with explicit operator reference handoffs.

Device construction and imports are inert. Real execution requires an interactive
operator and exclusive device admission; tests inject only fake device owners.
"""

import math
import os
import re
import select
import sys
import termios
import threading
import time
import tomllib
from dataclasses import dataclass

from .side_capture import SideCaptureCancelled

MODES = (1, 2, 4, 8, 16)
FOCUS_DEFAULTS = {
    "half_range": 5000,
    "coarse_step": 1000,
    "fine_step": 100,
    "ultrafine_step": 10,
    "local_half_range": 1500,
    "local_step": 100,
    "local_fine_radius": 200,
    "local_min_step": 10,
    "speed": 1200,
    "settle": 0.15,
    "frames": 2,
    "precision_settle": 0.35,
    "precision_frames": 3,
    "frame_interval": 0.0,
    "roi": 1.0,
    "backlash": 0,
    "lock_exposure": True,
    "camera_width": 2592,
    "camera_height": 1944,
}


def validated_focus_settings(overrides=None):
    """Validate the pinned GUI's focus settings before opening a device."""
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - set(FOCUS_DEFAULTS):
        raise ValueError("Unknown autofocus settings")
    values = {**FOCUS_DEFAULTS, **overrides}
    for name, default in FOCUS_DEFAULTS.items():
        value = values[name]
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, int):
            valid = type(value) is int and 0 <= value < 2**31
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        if not valid:
            raise ValueError(f"Invalid autofocus setting: {name}")
    positive = set(FOCUS_DEFAULTS) - {
        "settle",
        "precision_settle",
        "frame_interval",
        "backlash",
        "lock_exposure",
    }
    if any(values[name] <= 0 for name in positive):
        raise ValueError("Autofocus sizes, counts and speeds must be positive")
    if not values["ultrafine_step"] < values["fine_step"] < values["coarse_step"]:
        raise ValueError("Autofocus steps must satisfy ultra < fine < coarse")
    if values["coarse_step"] > 2 * values["half_range"]:
        raise ValueError("Autofocus coarse step exceeds its scan range")
    if values["local_step"] > 2 * values["local_half_range"]:
        raise ValueError("Local autofocus step exceeds its scan range")
    if values["local_fine_radius"] < values["local_step"]:
        raise ValueError("Local fine radius must include the maximum step")
    if not values["local_min_step"] <= values["local_step"]:
        raise ValueError("Local minimum step exceeds its maximum step")
    if values["speed"] > 10000 or values["roi"] > 1:
        raise ValueError("Autofocus speed or ROI is outside the source limits")
    if values["camera_width"] * values["camera_height"] > 16_000_000:
        raise ValueError("Camera dimensions exceed the bounded image size")
    return values


def load_settings(path):
    """Load a bounded TOML settings document without device or output access."""
    if path is None:
        return {}
    with open(path, "rb") as source:
        data = source.read(65537)
    if len(data) > 65536:
        raise ValueError("Settings document exceeds 64 KiB")
    value = tomllib.loads(data.decode("utf-8"))
    if set(value) - {"focus", "switch", "calibrator", "scan"}:
        raise ValueError("Unknown settings section")
    if any(not isinstance(section, dict) for section in value.values()):
        raise ValueError("Settings sections must be tables")
    return value


@dataclass(frozen=True)
class ReferenceSettings:
    """Measured MICRO 4 coordinates; manual zero is the clearance origin."""

    minimum: int
    maximum: int
    clearance: int
    home_switch: bool

    def __post_init__(self):
        coordinates = (self.minimum, self.maximum, self.clearance)
        if any(type(value) is not int or not -(2**31) <= value < 2**31 for value in coordinates):
            raise ValueError("Measured focus coordinates must be signed 32-bit integers")
        if not self.minimum <= 0 <= self.maximum or self.minimum == self.maximum:
            raise ValueError("Measured focus limits must include zero and nonzero travel")
        if not self.minimum <= self.clearance <= self.maximum or type(self.home_switch) is not bool:
            raise ValueError("Measured clearance and home-switch configuration are required")
        if not self.home_switch and self.clearance != 0:
            raise ValueError("Without a home switch, the clearance position must be zero")

    @classmethod
    def from_env(cls, environ):
        limits = environ.get("MG400_MICROSCOPE_FOCUS_LIMITS", "")
        clearance = environ.get("MG400_MICROSCOPE_SWITCH_FOCUS", "")
        home = environ.get("MG400_MICROSCOPE_HOME_SWITCH", "")
        if not re.fullmatch(r"-?\d+,-?\d+", limits) or not re.fullmatch(r"-?\d+", clearance):
            raise ValueError("Measured H-06 focus limits and clearance are required")
        if home not in ("0", "1"):
            raise ValueError("The measured H-06 home-switch configuration is required")
        minimum, maximum = map(int, limits.split(","))
        return cls(minimum, maximum, int(clearance), home == "1")

    def scaled(self, microstep):
        if type(microstep) is not int or microstep not in MODES:
            raise ValueError("Microstep mode must be 1, 2, 4, 8 or 16")
        lower = -((-self.minimum * microstep) // 4)
        upper = self.maximum * microstep // 4
        if lower >= upper or not -(2**31) <= lower <= upper < 2**31:
            raise ValueError("Inward-rounded focus limits have no representable safe travel")
        clearance = self.coordinate(self.clearance, microstep)
        if not lower <= clearance <= upper:
            raise ValueError("Scaled clearance is outside the inward-rounded focus limits")
        return lower, upper, clearance

    @staticmethod
    def coordinate(base_position, microstep):
        if type(base_position) is not int or type(microstep) is not int or microstep not in MODES:
            raise ValueError("Focus positions need integer MICRO 4 coordinates and a valid mode")
        numerator = base_position * microstep
        if numerator % 4:
            raise ValueError("The focus coordinate is not exactly representable in this microstep")
        value = numerator // 4
        if not -(2**31) <= value < 2**31:
            raise ValueError("Scaled focus coordinate exceeds the firmware range")
        return value


class TerminalOperator:
    """One bounded explicit confirmation; a refusal never reprompts."""

    def __init__(self, source=None, output=None, *, timeout=60.0, clock=time.monotonic):
        self.source = sys.stdin if source is None else source
        self.output = sys.stdout if output is None else output
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 300
        ):
            raise ValueError("Operator confirmation timeout must be within 1..300 seconds")
        self.timeout, self.clock = timeout, clock

    def require_interactive(self):
        if not self.source.isatty() or not self.output.isatty():
            raise RuntimeError(
                "Supervised calibration requires a person at an interactive terminal"
            )

    def confirm(self, reason, check):
        self.require_interactive()
        descriptor = self.source.fileno()
        # Each reference event requires new input, never a pasted earlier answer.
        termios.tcflush(descriptor, termios.TCIFLUSH)
        self.output.write(
            f"{reason}\nVisually confirm focus is at clearance and objectives are clear of the "
            "window. Type CLEAR to establish the reference, or anything else to stop: "
        )
        self.output.flush()
        deadline = self.clock() + self.timeout
        answer = bytearray()
        while True:
            check()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Operator confirmation timed out")
            if not select.select([descriptor], [], [], min(0.1, remaining))[0]:
                continue
            block = os.read(descriptor, 128 - len(answer))
            if not block:
                raise RuntimeError("Operator confirmation was refused by end of input")
            answer.extend(block)
            if len(answer) >= 128:
                raise RuntimeError("Operator confirmation was refused")
            if b"\n" in answer:
                check()
                if self.clock() >= deadline:
                    raise TimeoutError("Operator confirmation timed out")
                if bytes(answer) != b"CLEAR\n":
                    raise RuntimeError("Operator confirmation was refused")
                return


class SupervisedSession:
    """One nonrenewable operator session around already admitted fake or real owners."""

    def __init__(self, motor, camera, side, reference, operator, *, cancel_event=None):
        self._raw_motor = motor
        self._owners = (camera, side)
        self.reference, self.operator = reference, operator
        self.cancel_event = cancel_event or threading.Event()
        self._session = motor.session
        self._reference = None
        self._failure = None
        self._abort_lock = threading.Lock()
        self._quit = threading.Event()
        self._watcher = None
        self.motor = GuardedMotor(self)
        self.camera = GuardedCamera(self, camera)
        self.side = GuardedCamera(self, side) if side is not None else None

    def abort(self, reason):
        self._failure = self._failure or str(reason)
        self.cancel_event.set()
        self._reference = None
        # Stop must not wait behind a native frame operation or operator input.
        if self._abort_lock.acquire(blocking=False):
            try:
                if self._raw_motor.connected:
                    self._raw_motor.safe_stop_disable()
            finally:
                self._abort_lock.release()

    def check(self):
        reason = self._failure
        if self.cancel_event.is_set():
            raise SideCaptureCancelled(reason or "Supervised operation was stopped")
        if not self._raw_motor.connected or self._raw_motor.session != self._session:
            reason = "Microscope disconnected or changed firmware session"
        elif self._raw_motor.fault:
            reason = "Microscope firmware reset or reported an unknown command outcome"
        elif self._reference is not None and self._raw_motor.reference_epoch != self._reference[1]:
            reason = "The confirmed focus reference was invalidated"
        elif any(owner is not None and not owner.is_open for owner in self._owners):
            reason = "An admitted camera owner disconnected"
        elif any(
            owner is not None and getattr(owner, "last_error", None) for owner in self._owners
        ):
            reason = "An admitted camera stream failed"
        if reason is not None:
            self.abort(reason)
            raise SideCaptureCancelled(reason)

    def start_watch(self):
        def watch():
            while not self._quit.wait(0.05):
                try:
                    self.check()
                except BaseException:
                    try:
                        self.abort(self._failure or "Supervised session was stopped")
                    except Exception:
                        pass
                    return

        self._watcher = threading.Thread(
            target=watch, name="microscope-supervised-watch", daemon=True
        )
        self._watcher.start()

    def finish_watch(self):
        self._quit.set()
        if self._watcher is not None and self._watcher is not threading.current_thread():
            self._watcher.join(6)
            if self._watcher.is_alive():
                raise RuntimeError("Supervised safety cleanup has not drained")

    def _status(self):
        self.check()
        status = self._raw_motor.read_status(cancel_event=self.cancel_event)
        self.check()
        if status.session != self._session or status.homing:
            self.abort("Focus status is from a stale session or unfinished home")
            raise SideCaptureCancelled(self._failure)
        return status

    def confirm_reference(self, reason):
        self.check()
        if self.reference is None:
            raise RuntimeError("This image-only command has no motion capability")
        try:
            self._raw_motor.stop_all()
            status = self._status()
            limits = self.reference.scaled(status.microstep)
            if status.objective.moving or status.focus.moving:
                raise RuntimeError("Both motors must be stopped before operator confirmation")
            self._reference = None
            self.operator.confirm(reason, self.check)
            self.check()
            self._raw_motor.focus_enable(cancel_event=self.cancel_event)
            if self.reference.home_switch:
                self._raw_motor.focus_home(cancel_event=self.cancel_event)
            else:
                self._raw_motor.focus_zero(cancel_event=self.cancel_event)
            self._raw_motor.focus_limits(*limits[:2], cancel_event=self.cancel_event)
            confirmed = self._status()
            if (
                confirmed.microstep != status.microstep
                or not confirmed.zeroed
                or not confirmed.limited
                or confirmed.homing
                or confirmed.focus.moving
                or confirmed.objective.moving
                or not confirmed.focus.enabled
                or confirmed.focus.position != 0
                or (confirmed.minimum, confirmed.maximum) != limits[:2]
            ):
                raise RuntimeError("Firmware did not confirm the operator's reference and limits")
            self._reference = (confirmed.session, confirmed.reference_epoch)
        except BaseException as error:
            self.abort(error)
            raise

    def _guard(self, *, clearance=False, idle=True):
        self.check()
        if self._reference is None:
            self.confirm_reference("Initial supervised focus reference")
        status = self._status()
        lower, upper, safe = self.reference.scaled(status.microstep)
        if (
            self._reference != (status.session, status.reference_epoch)
            or not status.zeroed
            or not status.limited
            or not status.focus.enabled
            or (status.minimum, status.maximum) != (lower, upper)
            or not lower <= status.focus.position <= upper
            or (idle and (status.focus.moving or status.objective.moving))
            or (clearance and status.focus.position != safe)
        ):
            self.abort("Focus reference, measured limits or clearance is not confirmed")
            raise SideCaptureCancelled(self._failure)
        return status

    def move_focus_to(self, base_position):
        status = self._guard()
        target = self.reference.coordinate(base_position, status.microstep)
        if not status.minimum <= target <= status.maximum:
            raise ValueError("Requested focus start is outside the measured limits")
        if target != status.focus.position:
            self.motor.focus_move(target - status.focus.position)
        self._guard()

    def move_to_clearance(self):
        if self.reference is None:
            raise RuntimeError("This image-only command has no motion capability")
        self.move_focus_to(self.reference.clearance)
        self._guard(clearance=True)

    def reference_event(self, reason, operation, *, microstep=None):
        self.move_to_clearance()
        status = self._guard(clearance=True)
        self.reference.scaled(status.microstep if microstep is None else microstep)
        self._reference = None
        try:
            reply = operation()
            self.confirm_reference(reason)
            return reply
        except BaseException as error:
            self.abort(error)
            raise


class GuardedCamera:
    """Preserve the source camera API while making stream loss terminal."""

    def __init__(self, session, owner):
        self._session, self._owner = session, owner

    @property
    def is_open(self):
        return self._owner.is_open

    @property
    def stabilization_enabled(self):
        return self._owner.stabilization_enabled

    @property
    def sdk_camera(self):
        return self._owner.sdk_camera

    def _call(self, method, *args, **kwargs):
        self._session.check()
        try:
            value = getattr(self._owner, method)(*args, **kwargs)
            self._session.check()
            if method in {"get_frame", "get_frame_packet"} and value is None:
                raise RuntimeError("The admitted camera did not provide a fresh frame")
            return value
        except BaseException as error:
            self._session.abort(error)
            raise

    def get_frame(self, **kwargs):
        return self._call("get_frame", **kwargs)

    def get_frame_packet(self, **kwargs):
        return self._call("get_frame_packet", **kwargs)

    def set_stabilization(self, enabled):
        return self._call("set_stabilization", enabled)

    def set_auto_exposure(self, enabled):
        return self._call("set_auto_exposure", enabled)


class GuardedMotor:
    """The sole motion interface supplied to the unchanged source algorithms."""

    def __init__(self, session):
        self._session = session

    @property
    def connected(self):
        return self._session._raw_motor.connected and not self._session.cancel_event.is_set()

    def _call(self, method, *args):
        self._session.check()
        try:
            value = getattr(self._session._raw_motor, method)(
                *args, cancel_event=self._session.cancel_event
            )
            self._session.check()
            return value
        except BaseException as error:
            self._session.abort(error)
            raise

    def focus_enable(self):
        self._session._guard()
        return self._call("focus_enable")

    def objective_enable(self):
        self._session.move_to_clearance()
        return self._call("objective_enable")

    def focus_speed(self, speed):
        self._session._guard()
        return self._call("focus_speed", speed)

    def objective_speed(self, speed):
        self._session._guard()
        return self._call("objective_speed", speed)

    def focus_move_async(self, steps, *, cancel_event=None):
        status = self._session._guard()
        if (
            type(steps) is not int
            or not status.minimum <= status.focus.position + steps <= status.maximum
        ):
            self._session.abort("Focus move exceeds measured limits")
            raise ValueError("Focus move exceeds measured limits")
        return self._call("focus_move_async", steps)

    def wait_focus_done(self, ticket, *, cancel_event=None):
        value = self._call("wait_focus_done", ticket)
        self._session._guard()
        return value

    def focus_move(self, steps, *, cancel_event=None):
        return self.wait_focus_done(self.focus_move_async(steps, cancel_event=cancel_event))

    def objective_move(self, steps):
        self._session.move_to_clearance()
        self._session._guard(clearance=True)
        return self._call("objective_move", steps)

    def set_microstep(self, microstep):
        status = self._session._guard()
        self._session.reference.scaled(microstep)
        if status.microstep == microstep:
            return self._call("set_microstep", microstep)
        return self._session.reference_event(
            f"MICRO {microstep} changed the shared focus reference",
            lambda: self._call("set_microstep", microstep),
            microstep=microstep,
        )

    def focus_disable(self):
        if self._session.cancel_event.is_set():
            return self._session._raw_motor.safe_stop_disable()
        return self._session.reference_event(
            "FOCUS DISABLE cleared the focus reference", lambda: self._call("focus_disable")
        )

    def objective_disable(self):
        return self._session._raw_motor.objective_disable()

    def objective_stop(self):
        return self._session._raw_motor.objective_stop()

    def focus_stop(self):
        return self._session._raw_motor.focus_stop()

    def send_and_wait(self, command, response_prefix, *, timeout=5.0, cancel_event=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 65:
            raise ValueError("Command timeout must be within 0..65 seconds")
        safety = {
            "STOP OBJ": self.objective_stop,
            "STOP FOCUS": self.focus_stop,
            "STOP ALL": self._session._raw_motor.stop_all,
            "OBJ DISABLE": self.objective_disable,
            "FOCUS DISABLE": self.focus_disable,
            "OBJ ENABLE": self.objective_enable,
            "FOCUS ENABLE": self.focus_enable,
        }
        if command in safety:
            return safety[command]()
        match = re.fullmatch(r"(MICRO|OBJ SPEED|FOCUS SPEED|OBJ MOVE|FOCUS MOVE) (-?\d+)", command)
        if match is None:
            raise ValueError("Raw commands are not available to supervised algorithms")
        action, value = match[1], int(match[2])
        if action == "MICRO":
            return self.set_microstep(value)
        if action == "OBJ SPEED":
            return self.objective_speed(value)
        if action == "FOCUS SPEED":
            return self.focus_speed(value)
        if action == "FOCUS MOVE":
            return self.focus_move(value)
        self._session.move_to_clearance()
        self._session._guard(clearance=True)
        try:
            reply = self._session._raw_motor.send_and_wait(
                command, response_prefix, timeout=timeout, cancel_event=self._session.cancel_event
            )
            self._session.check()
            return reply
        except BaseException as error:
            self._session.abort(error)
            raise
