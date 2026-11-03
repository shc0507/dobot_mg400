"""Supervised calibration commands with explicit operator reference handoffs.

Device construction and imports are inert. Real execution requires an interactive
operator and exclusive device admission; tests inject only fake device owners.
"""

import argparse
import fcntl
import math
import os
import re
import select
import signal
import stat
import struct
import sys
import termios
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass

import cv2
import numpy as np

from cameras.config import MJPEG_MODES, camera_devices
from cameras.uvc import USBCamera

from .autofocus import AutoFocuser
from .light_calibration import LightObjectiveCalibrator, save_light_objective_measurements
from .motors import MotorController
from .objective_calibration import ObjectiveCalibrator, save_objective_measurements
from .paths import calibration_path, private_open
from .side_capture import SideCaptureCancelled

MODES = (1, 2, 4, 8, 16)
NUDGE_SETTING = "MG400_MICROSCOPE_NUDGE_STEPS"
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


def nudge_steps(environ):
    """Require an explicit measured integer window; empty keeps MIC-05 behavior."""
    value = environ.get(NUDGE_SETTING, "")
    if value == "":
        return 0
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}", value):
        raise ValueError(f"{NUDGE_SETTING} must be an integer within 0..20")
    window = int(value)
    if not 0 <= window <= 20:
        raise ValueError(f"{NUDGE_SETTING} must be an integer within 0..20")
    return window


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

    def key(self, check):
        """Read one fresh key, restoring terminal mode before any command runs."""
        self.require_interactive()
        descriptor = self.source.fileno()
        saved = termios.tcgetattr(descriptor)
        changed = list(saved)
        changed[6] = list(saved[6])
        changed[3] &= ~(termios.ICANON | termios.ECHO)
        changed[6][termios.VMIN] = 0
        changed[6][termios.VTIME] = 0
        try:
            termios.tcflush(descriptor, termios.TCIFLUSH)
            termios.tcsetattr(descriptor, termios.TCSANOW, changed)
            deadline = self.clock() + self.timeout
            while True:
                check()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Setup input timed out")
                if select.select([descriptor], [], [], min(0.1, remaining))[0]:
                    data = os.read(descriptor, 1)
                    check()
                    if self.clock() >= deadline:
                        raise TimeoutError("Setup input timed out")
                    return None if not data or data == b"\x04" else data.decode("ascii", "replace")
        finally:
            termios.tcsetattr(descriptor, termios.TCSANOW, saved)

    @contextmanager
    def motion_controls(self, check):
        """Observe Stop/quit during an accepted move without queuing more motion."""
        self.require_interactive()
        descriptor = self.source.fileno()
        saved = termios.tcgetattr(descriptor)
        changed = list(saved)
        changed[6] = list(saved[6])
        changed[3] &= ~(termios.ICANON | termios.ECHO)
        changed[6][termios.VMIN] = 0
        changed[6][termios.VTIME] = 0

        def poll():
            check()
            if not select.select([descriptor], [], [], 0)[0]:
                return ""
            data = os.read(descriptor, 1)
            return None if not data or data == b"\x04" else data.decode("ascii", "replace")

        try:
            termios.tcsetattr(descriptor, termios.TCSANOW, changed)
            yield poll
        finally:
            termios.tcsetattr(descriptor, termios.TCSANOW, saved)


class SupervisedSession:
    """One nonrenewable operator session around already admitted fake or real owners."""

    def __init__(self, motor, camera, side, reference, operator, *, cancel_event=None):
        self._raw_motor = motor
        self._owners = (camera, side)
        self.reference, self.operator = reference, operator
        self.cancel_event = cancel_event or threading.Event()
        self._session = motor.session
        self._reference = None
        self._reference_revision = 0
        self._failure = None
        self._operator_stopped = False
        self._cause_lock = threading.Lock()
        self._abort_lock = threading.Lock()
        self._quit = threading.Event()
        self._watcher = None
        self._frame_progress = {}
        self._nudge_window = 0
        self._turret_units = None
        self.motor = GuardedMotor(self)
        self.camera = GuardedCamera(self, camera)
        self.side = GuardedCamera(self, side) if side is not None else None

    @property
    def failure(self):
        return self._failure

    @property
    def operator_stopped(self):
        return self._operator_stopped

    def abort(self, reason, *, operator=False):
        with self._cause_lock:
            if self._failure is None:
                self._operator_stopped = operator or isinstance(reason, KeyboardInterrupt)
                self._failure = str(reason) or type(reason).__name__
            self.cancel_event.set()
            self._reference = None
            self._reference_revision += 1
        # Stop must not wait behind a native frame operation or operator input.
        if self._abort_lock.acquire(blocking=False):
            try:
                if self._raw_motor.connected:
                    self._raw_motor.safe_stop_disable()
            finally:
                self._abort_lock.release()

    def _set_reference(self, reference):
        with self._cause_lock:
            self._reference = reference
            self._reference_revision += 1

    def check(self):
        reason = self._failure
        with self._cause_lock:
            reference, revision = self._reference, self._reference_revision
        if self.cancel_event.is_set():
            raise SideCaptureCancelled(reason or "Supervised operation was stopped")
        if not self._raw_motor.connected or self._raw_motor.session != self._session:
            reason = "Microscope disconnected or changed firmware session"
        elif self._raw_motor.fault:
            reason = "Microscope firmware reset or reported an unknown command outcome"
        elif reference is not None:
            epoch = self._raw_motor.reference_epoch
            with self._cause_lock:
                if revision == self._reference_revision and epoch != reference[1]:
                    reason = "The confirmed focus reference was invalidated"
        if reason is None and any(
            owner is not None and not owner.is_open for owner in self._owners
        ):
            reason = "An admitted camera owner disconnected"
        elif reason is None and any(
            owner is not None and getattr(owner, "last_error", None) for owner in self._owners
        ):
            reason = "An admitted camera stream failed"
        if reason is not None:
            self.abort(reason)
            raise SideCaptureCancelled(reason)
        if self.cancel_event.is_set():
            raise SideCaptureCancelled(self._failure or "Supervised operation was stopped")

    def start_watch(self):
        def watch():
            while not self._quit.wait(0.05):
                try:
                    self.check()
                    self._check_frame_progress()
                except BaseException as error:
                    try:
                        self.abort(self._failure or str(error))
                    except Exception:
                        pass
                    return

        self._watcher = threading.Thread(
            target=watch, name="microscope-supervised-watch", daemon=True
        )
        self._watcher.start()

    def _check_frame_progress(self):
        # Camera clocks differ (wall time for Toupcam, monotonic for UVC).
        # Observe advancing frame IDs with our own monotonic clock instead.
        now = time.monotonic()
        for owner in self._owners:
            if owner is None:
                continue
            packet = owner.get_frame_packet(wait=False, copy=False)
            identity = validate_packet(packet)[0]
            previous, changed = self._frame_progress.get(id(owner), (None, now))
            if previous is not None and identity < previous:
                raise RuntimeError("An admitted camera restarted its frame sequence")
            if identity != previous:
                self._frame_progress[id(owner)] = (identity, now)
            elif now - changed >= 2.0:
                raise TimeoutError("An admitted camera frame stream became stale")

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
            self._set_reference(None)
            try:
                self.operator.confirm(reason, self.check)
            except BaseException as error:
                # check() already latches a transport/reset fault. Otherwise a
                # refusal, terminal timeout or interrupt is an operator stop.
                self.abort(error, operator=True)
                raise
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
            self._set_reference((confirmed.session, confirmed.reference_epoch))
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

    def before_turret_move(self):
        if not self._nudge_window:
            self.move_to_clearance()
        return self._guard(clearance=not self._nudge_window)

    def reserve_turret_move(self, steps, status):
        """Count each admitted command once, independently of firmware coordinates."""
        if not self._nudge_window:
            return
        if type(steps) is not int or self._turret_units is None:
            self.abort("The calibration turret origin is not confirmed")
            raise ValueError(self.failure)
        target = self._turret_units + steps * (16 // status.microstep)
        if abs(target) > self._nudge_window * 16:
            self.abort("Turret move exceeds the measured at-focus window")
            raise ValueError(self.failure)
        self._turret_units = target

    def prepare_at_focus(self, window, focus_start, focus, progress):
        if type(window) is not int or not 1 <= window <= 20:
            raise ValueError("At-focus calibration needs a measured window within 1..20")
        if (
            self.reference is None
            or type(focus_start) is not int
            or not self.reference.minimum <= focus_start <= self.reference.maximum
            or self._reference is not None
            or self._nudge_window
        ):
            raise ValueError("At-focus calibration needs a fresh session and a safe --focus-start")
        try:
            self._raw_motor.stop_all()
            self._status()
            self._raw_motor.set_microstep(4, cancel_event=self.cancel_event)
            self.confirm_reference(
                "Initial at-focus calibration: check the objective is in its working position. "
                f"After confirmation focus will move to {focus_start} MICRO 4 pulses "
                "and run local autofocus."
            )
            self._nudge_window, self._turret_units = window, 0
            self.move_focus_to(focus_start)
            focuser = AutoFocuser(
                self.camera,
                self.motor,
                speed=focus["speed"],
                settle_seconds=focus["settle"],
                frames_per_position=focus["frames"],
                precision_settle_seconds=focus["precision_settle"],
                precision_frames_per_position=focus["precision_frames"],
                frame_interval=focus["frame_interval"],
                roi_fraction=focus["roi"],
                backlash_steps=focus["backlash"],
                progress=progress,
                cancel_event=self.cancel_event,
            )
            focuser.run_local(
                half_range=focus["local_half_range"],
                step=focus["local_step"],
                fine_radius=focus["local_fine_radius"],
                min_step=focus["local_min_step"],
                lock_exposure=focus["lock_exposure"],
            )
            self._guard()
        except BaseException as error:
            self.abort(error)
            raise

    def reference_event(self, reason, operation, *, microstep=None):
        restore = None
        if self._nudge_window:
            earlier = self._guard()
            mode = earlier.microstep if microstep is None else microstep
            lower, upper, _ = self.reference.scaled(mode)
            numerator = earlier.focus.position * mode
            # Integer nearest-pulse conversion, with half pulses away from zero.
            restore = (2 * abs(numerator) + earlier.microstep) // (2 * earlier.microstep)
            restore *= -1 if numerator < 0 else 1
            if not lower <= restore <= upper:
                self.abort("Focus return is outside the inward-rounded measured limits")
                raise ValueError(self.failure)
            reason += f". After CLEAR, focus will return to {restore} pulses at MICRO {mode}."
        self.move_to_clearance()
        status = self._guard(clearance=True)
        self.reference.scaled(status.microstep if microstep is None else microstep)
        self._set_reference(None)
        try:
            reply = operation()
            self.confirm_reference(reason)
            if restore is not None:
                current = self._guard()
                if current.focus.position != restore:
                    self.motor.focus_move(restore - current.focus.position)
                self._guard()
            return reply
        except BaseException as error:
            self.abort(error)
            raise


def validate_packet(packet, *, after_id=0):
    """Validate publication metadata and bounded pixels without assuming its clock."""
    if not isinstance(packet, (tuple, list)) or len(packet) != 3:
        raise RuntimeError("The admitted camera returned an invalid or missing frame")
    identity, captured, frame = packet
    if (
        type(identity) is not int
        or identity <= after_id
        or type(captured) not in (int, float)
        or not math.isfinite(captured)
        or not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[2] != 3
        or frame.dtype != np.uint8
        or not 0 < frame.shape[0] * frame.shape[1] <= 16_000_000
    ):
        raise RuntimeError("The admitted camera returned an invalid or stale frame")
    return packet


class GuardedCamera:
    """Preserve the source camera API while making stream loss terminal."""

    def __init__(self, session, owner):
        self._session, self._owner = session, owner
        self._last_frame_id = 0

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
        return self.get_frame_packet(**kwargs)[2]

    def get_frame_packet(self, **kwargs):
        after = kwargs.pop("after_id", None)
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError("Frame cursor must be a nonnegative integer")
        kwargs["after_id"] = max(self._last_frame_id, after or 0)
        packet = self._call("get_frame_packet", **kwargs)
        try:
            identity, _, _ = validate_packet(packet, after_id=kwargs["after_id"])
            self._last_frame_id = identity
            return packet
        except BaseException as error:
            self._session.abort(error)
            raise

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
        self._session.before_turret_move()
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
        status = self._session.before_turret_move()
        self._session.reserve_turret_move(steps, status)
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
        if self._session._nudge_window:
            self._session._guard()
            return self._call("focus_stop")
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
        status = self._session.before_turret_move()
        self._session.reserve_turret_move(value, status)
        try:
            reply = self._session._raw_motor.send_and_wait(
                command, response_prefix, timeout=timeout, cancel_event=self._session.cancel_event
            )
            self._session.check()
            return reply
        except BaseException as error:
            self._session.abort(error)
            raise


_V4L2_CAPABILITY = struct.Struct("=16s32s32sIII3I")
_VIDIOC_QUERYCAP = 0x80685600
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_STREAMING = 0x04000000
_V4L2_CAP_DEVICE_CAPS = 0x80000000


class NativeUVCCapture:
    """One directly owned UVC MMAP stream; construction performs no device I/O.

    Forced OpenCV V4L2 uses REQBUFS/MMAP, STREAMON and DQBUF, without read-I/O
    fallback. A retained UVC queue excludes other capture owners, including
    go2rtc. It does not exclude control-only handles. Never reconfigure or
    reopen after admission: OpenCV format/buffer resets release queue ownership.
    """

    def __init__(self, device, mode, *, video_factory=None, system=None, ioctl=None, cv=None):
        if camera_devices(device) != [device]:
            raise ValueError("The native side camera needs one exact stable by-path device")
        if (
            not isinstance(mode, tuple)
            or len(mode) != 3
            or any(type(value) is not int for value in mode)
            or mode not in MJPEG_MODES
        ):
            raise ValueError("The native side camera needs one supported MJPEG mode")
        self.device, self.mode = device, mode
        self._system = os if system is None else system
        self._ioctl = fcntl.ioctl if ioctl is None else ioctl
        self._cv = cv2 if cv is None else cv
        self._video_factory = self._cv.VideoCapture if video_factory is None else video_factory
        self._descriptor = None
        self._capture = None
        self._identity = None
        self._capability = None
        self._attempted = False
        self._failed = None

    @staticmethod
    def _device_identity(info):
        if not stat.S_ISCHR(info.st_mode):
            raise RuntimeError("The side-camera path must identify a character device")
        return info.st_dev, info.st_ino, info.st_rdev

    def _query_capability(self):
        data = bytearray(_V4L2_CAPABILITY.size)
        self._ioctl(self._descriptor, _VIDIOC_QUERYCAP, data, True)
        driver, _card, bus, _version, capabilities, device_caps, *_ = _V4L2_CAPABILITY.unpack(data)
        driver, bus = driver.split(b"\0", 1)[0], bus.split(b"\0", 1)[0]
        effective = device_caps if capabilities & _V4L2_CAP_DEVICE_CAPS else capabilities
        required = _V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_STREAMING
        if driver != b"uvcvideo" or not bus or effective & required != required:
            raise RuntimeError("The side camera must be a uvcvideo capture/streaming node")
        return driver, bus, effective

    def _check_identity(self):
        if (
            self._device_identity(self._system.fstat(self._descriptor)) != self._identity
            or self._device_identity(self._system.stat(self.device)) != self._identity
            or self._query_capability() != self._capability
        ):
            raise RuntimeError("The owned side-camera identity changed")

    def open(self):
        if self._attempted:
            raise RuntimeError("The native side capture cannot be reopened")
        self._attempted = True
        try:
            self._descriptor = self._system.open(
                self.device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC
            )
            self._identity = self._device_identity(self._system.fstat(self._descriptor))
            self._capability = self._query_capability()
            self._check_identity()
            self._capture = self._video_factory(
                f"/proc/self/fd/{self._descriptor}", self._cv.CAP_V4L2
            )
            capture = self._capture
            if not capture.isOpened() or capture.getBackendName() != "V4L2":
                raise RuntimeError("The side camera did not acquire the required V4L2 backend")
            width, height, fps = self.mode
            fourcc = self._cv.VideoWriter_fourcc(*"MJPG")
            for key, value in (
                (self._cv.CAP_PROP_FOURCC, fourcc),
                (self._cv.CAP_PROP_FRAME_WIDTH, width),
                (self._cv.CAP_PROP_FRAME_HEIGHT, height),
                (self._cv.CAP_PROP_FPS, fps),
                (self._cv.CAP_PROP_BUFFERSIZE, 1),
            ):
                if not capture.set(key, value):
                    raise RuntimeError("The side camera refused its configured capture mode")
            for key, expected in (
                (self._cv.CAP_PROP_FRAME_WIDTH, width),
                (self._cv.CAP_PROP_FRAME_HEIGHT, height),
                (self._cv.CAP_PROP_FOURCC, fourcc),
                (self._cv.CAP_PROP_FPS, fps),
            ):
                value = capture.get(key)
                if not math.isfinite(value) or not math.isclose(
                    value, expected, rel_tol=0, abs_tol=0.01
                ):
                    raise RuntimeError("The side camera did not retain its configured mode")
            self._check_identity()
            return self
        except BaseException:
            self.release()
            raise

    def read(self):
        if self._capture is None or self._failed is not None:
            raise RuntimeError("The owned side capture is unavailable") from self._failed
        try:
            self._check_identity()
            success, frame = self._capture.read()
            self._check_identity()
            width, height, _ = self.mode
            if (
                not success
                or not isinstance(frame, np.ndarray)
                or frame.dtype != np.uint8
                or frame.shape != (height, width, 3)
            ):
                raise RuntimeError("The owned side camera did not deliver its configured frame")
            return True, frame
        except BaseException as error:
            self._failed = error
            raise

    def release(self):
        # The side owner must drain read() first. Keep references if native
        # release fails, so failed cleanup cannot silently surrender ownership.
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        if self._descriptor is not None:
            self._system.close(self._descriptor)
            self._descriptor = None


class NativeSideCamera(USBCamera):
    """Reuse frame publication while retaining native ownership until reads drain."""

    def __init__(self, device, mode, stream="camera1", *, capture_factory=None, close_timeout=3.0):
        if not math.isfinite(close_timeout) or close_timeout <= 0:
            raise ValueError("The reader drain budget must be positive")
        self.device, self.mode = device, mode
        self._native_factory = NativeUVCCapture if capture_factory is None else capture_factory
        self._native_owner = None
        self._used = False
        self._close_timeout = close_timeout
        super().__init__(
            stream,
            resolution=mode[:2],
            fps=mode[2],
            capture_factory=self._open_capture,
        )

    @property
    def description(self):
        return f"Direct UVC side camera ({self.device})"

    def _open_capture(self, _stream):
        owner = self._native_factory(self.device, self.mode)
        self._native_owner = owner
        return owner.open()

    def open(self):
        with self._lifecycle:
            if self._used:
                if self.is_open and not self._stop_event.is_set() and self.last_error is None:
                    return self
                raise RuntimeError("The native side-camera session cannot be reopened")
            self._used = True
            self._stop_event = threading.Event()
            self._error = None
            self._latest_frame = None
            self._captured_at = None
            gate = threading.Event()
            stop = self._stop_event

            def reader(capture):
                gate.wait()
                if not stop.is_set():
                    self._acquisition_loop(capture, stop)

            try:
                capture = self._open_capture(self.stream)
                self._capture = capture
                self._thread = threading.Thread(
                    target=reader,
                    args=(capture,),
                    name="microscope-native-side-camera",
                    daemon=True,
                )
                self._thread.start()
                # A start failure may still leave an OS thread about to start.
                # Admit native reads only after start returns successfully.
                gate.set()
            except BaseException:
                stop.set()
                gate.set()
                self.close()
                raise
            return self

    def close(self):
        with self._lifecycle:
            self._stop_event.set()
            with self._frame_condition:
                self._latest_frame = None
                self._error = RuntimeError("The native side-camera reader is stopping")
                self._frame_condition.notify_all()
            if self._thread is not None:
                if self._thread is threading.current_thread():
                    raise RuntimeError("The native side-camera reader cannot close itself")
                # An interrupted start can leave a thread that cannot yet join.
                # Its cancelled gate prevents a late bootstrap from reading.
                if self._thread.ident is not None:
                    self._thread.join(timeout=self._close_timeout)
                    if self._thread.is_alive():
                        raise RuntimeError(
                            "Native side-camera read is still draining; ownership retained"
                        )
                self._thread = None
            owner = self._native_owner
            if owner is not None:
                owner.release()
            with self._frame_condition:
                self._capture = None
                self._native_owner = None
                self._frame_condition.notify_all()


@contextmanager
def supervised_session(
    *,
    need_side=False,
    resolution=(2592, 1944),
    environ=None,
    operator=None,
    factories=None,
    motion=True,
):
    """Own all devices before granting a lazy, explicitly confirmed reference.

    Factories are a Python test seam, never CLI options. The real defaults open
    native devices only after configuration and terminal validation.
    """
    from cameras.config import camera_configuration
    from toupcam_camera import MicroscopeCamera
    from toupcam_camera.capture import selection

    settings = dict(os.environ if environ is None else environ)
    serial = settings.get("MG400_MICROSCOPE_SERIAL", "")
    if not isinstance(serial, str) or not re.fullmatch(
        r"/dev/serial/by-id/[A-Za-z0-9_.:+-]+", serial
    ):
        raise ValueError("Set the stable MG400_MICROSCOPE_SERIAL device path")
    reference = ReferenceSettings.from_env(settings) if motion else None
    if (
        not isinstance(resolution, (tuple, list))
        or len(resolution) != 2
        or any(type(n) is not int or n <= 0 for n in resolution)
        or resolution[0] * resolution[1] > 16_000_000
    ):
        raise ValueError("Invalid microscope resolution")
    selection(settings=settings)  # Pure selection validation, no SDK load.
    side_args = None
    if need_side:
        config = camera_configuration(settings)
        stream = settings.get("MG400_MICROSCOPE_SIDE_STREAM", "")
        if stream not in config.streams:
            raise ValueError("Select the configured physical side camera stream")
        index = config.streams.index(stream)
        side_args = (config.devices[index], config.modes[index], stream)
    operator = TerminalOperator() if operator is None else operator
    operator.require_interactive()
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Supervised commands must run in the terminal's main thread")
    factories = {} if factories is None else factories
    if set(factories) - {"motor", "camera", "side"}:
        raise ValueError("Unknown supervised device factory")
    motor_factory = factories.get("motor", MotorController)
    camera_factory = factories.get("camera", MicroscopeCamera)
    # The native side adapter is resolved only when this command needs it.
    side_factory = factories.get("side")
    if need_side and side_factory is None:
        side_factory = NativeSideCamera
    cancel = threading.Event()
    saved_signals = {}
    motor = camera = side = session = None
    successful = False

    def interrupted(_number, _frame):
        cancel.set()
        raise KeyboardInterrupt("Supervised operation interrupted")

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            saved_signals[number] = signal.signal(number, interrupted)
        camera = camera_factory(
            resolution=tuple(resolution), stabilization=False, settings=settings
        )
        camera.open()
        packet = camera.get_frame_packet(wait=True, timeout=2.0, copy=False)
        validate_packet(packet)
        if not camera.is_open or camera.last_error is not None:
            raise RuntimeError("Main camera did not establish exclusive live capture")
        if need_side:
            side = side_factory(*side_args)
            side.open()
            packet = side.get_frame_packet(wait=True, timeout=2.0, copy=False)
            validate_packet(packet)
            if not side.is_open or side.last_error is not None:
                raise RuntimeError("Side camera did not establish exclusive live capture")
        motor = motor_factory()
        motor.connect(serial)
        if motion and not motor.focus_completion:
            raise RuntimeError("Supervised motion requires MIC-01 firmware status and completion")
        session = SupervisedSession(motor, camera, side, reference, operator, cancel_event=cancel)
        session.start_watch()
        session.check()
        yield session
        session.check()
        if session._reference is not None:
            session.move_to_clearance()
        successful = True
    except BaseException as error:
        if session is not None:
            session.abort(error)
        raise
    finally:
        try:
            # Never release capture or serial ownership while safety cleanup is live.
            if session is not None:
                session.abort(
                    "Supervised operation completed"
                    if successful
                    else "Supervised operation stopped"
                )
                session.finish_watch()
            elif motor is not None and motor.connected:
                motor.safe_stop_disable()
            if side is not None:
                side.close()
            if camera is not None:
                camera.close()
            if motor is not None:
                motor.disconnect()
        finally:
            for number, previous in saved_signals.items():
                signal.signal(number, previous)


@contextmanager
def setup_session(*, environ=None, operator=None, motor_factory=None):
    """Own only the serial transport for a supervised, unreferenced setup console."""
    settings = os.environ if environ is None else environ
    serial = settings.get("MG400_MICROSCOPE_SERIAL", "")
    if not isinstance(serial, str) or not re.fullmatch(
        r"/dev/serial/by-id/[A-Za-z0-9_.:+-]+", serial
    ):
        raise ValueError("Set the stable MG400_MICROSCOPE_SERIAL device path")
    operator = TerminalOperator() if operator is None else operator
    operator.require_interactive()
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Supervised setup must run in the terminal's main thread")
    motor_factory = MotorController if motor_factory is None else motor_factory
    saved_signals = {}
    cancel = threading.Event()
    motor = session = None

    def interrupted(_number, _frame):
        cancel.set()
        raise KeyboardInterrupt("Supervised setup interrupted")

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            saved_signals[number] = signal.signal(number, interrupted)
        motor = motor_factory()
        motor.connect(serial)
        if not motor.focus_completion:
            raise RuntimeError("Supervised setup requires MIC-01 firmware status and completion")
        session = SupervisedSession(motor, None, None, None, operator, cancel_event=cancel)
        session.start_watch()
        session.check()
        yield session
        session.check()
    except BaseException as error:
        if session is not None:
            session.abort(error)
        raise
    finally:
        try:
            if session is not None:
                session.abort("Supervised setup ended")
                session.finish_watch()
            elif motor is not None and motor.connected:
                motor.safe_stop_disable()
            if motor is not None:
                motor.disconnect()
        finally:
            for number, previous in saved_signals.items():
                signal.signal(number, previous)


def run_setup(session, *, progress=print):
    """One fresh bounded action at a time, with no limits or settings writes."""
    focus_keys = {"a": 1, "z": -1, "s": 10, "x": -10, "d": 100, "c": -100, "f": 1000, "v": -1000}
    turret_keys = {"j": -4, "l": 4, "u": -40, "o": 40}
    motor, cancel = session._raw_motor, session.cancel_event
    focus_count = turret_count = 0
    known = True

    def idle():
        status = session._status()
        if status.microstep != 4 or status.focus.moving or status.objective.moving:
            raise RuntimeError("Setup mode or stationary motor state was lost")
        return status

    def show():
        progress(
            f"Last confirmed focus count: {focus_count} MICRO 4 pulses; "
            f"turret displacement: {turret_count / 4:g} full steps from mark."
            + ("" if known else " Current position is unconfirmed.")
        )

    try:
        session.check()
        motor.safe_stop_disable()
        session.check()
        motor.set_microstep(4, cancel_event=cancel)
        motor.focus_speed(200, cancel_event=cancel)
        motor.objective_speed(40, cancel_event=cancel)
        idle()
        progress(
            "Setup only: observe every move. Focus a/z ±1, s/x ±10, d/c ±100, f/v ±1000 "
            "pulses; turret j/l -/+1, u/o -/+10 full steps. "
            "m marks turret origin; 0 requests CLEAR then focus zero; q quits. "
            "Other keys stop both motors. No limits or settings are written."
        )
        show()
        while True:
            before = idle()
            key = session.operator.key(session.check)
            session.check()
            idle()
            if key is None or key == "q":
                return
            if key == "m":
                turret_count = 0
            elif key == "0":
                motor.stop_all()
                session.operator.confirm(
                    "Set the counted focus origin at observed clearance", session.check
                )
                session.check()
                motor.focus_enable(cancel_event=cancel)
                motor.focus_zero(cancel_event=cancel)
                after = idle()
                if not after.zeroed or after.focus.position != 0 or after.limited:
                    raise RuntimeError("Setup focus zero was not confirmed")
                focus_count = 0
            elif key in focus_keys or key in turret_keys:
                steps = (focus_keys if key in focus_keys else turret_keys)[key]
                axis = "focus" if key in focus_keys else "objective"
                target = getattr(before, axis).position + steps
                if not -(2**31) <= target < 2**31:
                    raise ValueError("Setup move exceeds the firmware coordinate range")
                known = False
                quit_requested = stopped = False
                with session.operator.motion_controls(session.check) as poll:
                    getattr(motor, f"{axis}_enable")(cancel_event=cancel)
                    deadline = time.monotonic() + abs(steps) / (200 if axis == "focus" else 40) + 2
                    ticket = None
                    if axis == "focus":
                        ticket = motor.focus_move_async(steps, cancel_event=cancel)
                    else:
                        motor.objective_move(steps, cancel_event=cancel)
                    next_status = 0.0
                    while True:
                        control = poll()
                        quit_requested = control is None or control == "q"
                        if quit_requested or (
                            control and control not in focus_keys | turret_keys | {"m": 0, "0": 0}
                        ):
                            motor.stop_all()
                            stopped = True
                            break
                        now = time.monotonic()
                        if now >= next_status:
                            status = session._status()
                            next_status = now + 0.5
                            # Bound diagnostic STATUS rows while preserving the
                            # pending focus ticket's completion response history.
                            if status.microstep != 4:
                                raise RuntimeError("Setup microstep mode changed during motion")
                            if not status.focus.moving and not status.objective.moving:
                                if ticket is not None:
                                    motor.wait_focus_done(ticket, cancel_event=cancel)
                                break
                        if now >= deadline:
                            raise TimeoutError("Setup move did not finish within its bounded wait")
                        cancel.wait(0.01)
                after = idle()
                if not stopped and getattr(after, axis).position != target:
                    raise RuntimeError("Setup move completion position is not confirmed")
                travelled = getattr(after, axis).position - getattr(before, axis).position
                if axis == "focus":
                    focus_count += travelled
                else:
                    turret_count += travelled
                known = True
                if quit_requested:
                    return
            else:
                motor.stop_all()
                idle()
            show()
    except BaseException as error:
        session.abort(error, operator=isinstance(error, (KeyboardInterrupt, TimeoutError)))
        raise
    finally:
        if motor.connected:
            motor.safe_stop_disable()
        show()


# Defaults are the pinned GUI values, including its overrides of class defaults.
OBJECTIVE_OPTIONS = dict(
    reference_threshold=0.2,
    speed=200,
    move_settle_seconds=0.15,
    precision_settle_seconds=0.80,
    enable_settle_seconds=0.20,
    release_settle_seconds=0.35,
    roi_fraction=1.0,
    min_confidence=0.15,
    min_overlap=0.50,
    max_valid_displacement=250.0,
    peak_threshold=8.0,
    stable_threshold=10.0,
    neighbor_consistency_tolerance_px=2.0,
    atlas_merge_distance_px=10.0,
    post_peak_quiet_distance_px=150.0,
)
OBJECTIVE_SCAN = dict(
    half_range=30,
    coarse_step=5,
    stagger_offset=2,
    refine_radius=5,
    fine_step=1,
    verification_repeats=3,
    lock_exposure=True,
)
LIGHT_OPTIONS = dict(
    reference_magnification=10.0,
    current_magnification=20.0,
    speed=200,
    move_settle_seconds=0.50,
    minimum_score=0.25,
    minimum_uniqueness=0.03,
    minimum_overlap=0.50,
    center_tolerance_px=5.0,
    region_fraction=0.80,
    neighbor_support_fraction=0.60,
    release_threshold_px=10.0,
    neighbor_consistency_px=2.0,
    release_min_confidence=0.10,
    release_max_iterations=8,
    verification_repeats=3,
)
LIGHT_SCAN = dict(
    half_range_full_steps=10,
    coarse_microstep=2,
    fine_microstep=16,
    fine_step_pulses=1,
    lock_exposure=True,
)


def _numeric_settings(defaults, overrides):
    if not isinstance(overrides, dict) or set(overrides) - set(defaults):
        raise ValueError("Unknown calibration setting")
    values = {**defaults, **overrides}
    for name, default in defaults.items():
        value = values[name]
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, int):
            valid = type(value) is int and 0 <= value < 2**31
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value < 2**31
        if not valid:
            raise ValueError(f"Invalid calibration setting: {name}")
    return values


def validated_calibration_settings(kind, settings=None):
    settings = {} if settings is None else settings
    if kind not in {"objective", "light", "reference"}:
        raise ValueError("Unknown calibration command")
    if not isinstance(settings, dict) or set(settings) - {"focus", "calibrator", "scan"}:
        raise ValueError("Unknown calibration settings section")
    focus = validated_focus_settings(settings.get("focus"))
    if kind == "reference":
        if set(settings) - {"focus"}:
            raise ValueError("Reference capture accepts only focus camera dimensions")
        return focus, {}, {}
    light = kind == "light"
    options = _numeric_settings(
        LIGHT_OPTIONS if light else OBJECTIVE_OPTIONS, settings.get("calibrator", {})
    )
    scan = _numeric_settings(LIGHT_SCAN if light else OBJECTIVE_SCAN, settings.get("scan", {}))
    if options["speed"] > 10000:
        raise ValueError("Calibration speed exceeds the firmware limit")
    algorithm = LightObjectiveCalibrator if light else ObjectiveCalibrator
    # Constructors only validate calculation settings and initialize memory.
    algorithm(None, None, reference_frame=np.zeros((1, 1, 3), np.uint8), **options)
    if any(
        not value for key, value in scan.items() if key not in {"lock_exposure", "refine_radius"}
    ):
        raise ValueError("Calibration scan steps and counts must be positive")
    if light:
        if (
            scan["coarse_microstep"] not in MODES
            or scan["fine_microstep"] not in MODES
            or scan["fine_microstep"] < scan["coarse_microstep"]
        ):
            raise ValueError("Light scan microsteps must satisfy coarse <= fine")
        if options["current_magnification"] < options["reference_magnification"]:
            raise ValueError("Current magnification must cover the saved reference scale")
    return focus, options, scan


def read_reference(value):
    """Read one bounded private PNG below the calibration root without symlinks."""
    path = calibration_path(value)
    if path.suffix.lower() != ".png":
        raise ValueError("Objective references must be PNG images")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        source = os.open(
            path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
        )
    finally:
        os.close(descriptor)
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o037
            or not 0 < info.st_size <= 64 * 1024**2
        ):
            raise ValueError("Reference must be a bounded owned private regular file")
        data = stream.read(64 * 1024**2 + 1)
    if (
        len(data) > 64 * 1024**2
        or len(data) < 33
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[8:16] != b"\x00\x00\x00\rIHDR"
    ):
        raise ValueError("Invalid PNG reference header")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not width or not height or width * height > 16_000_000:
        raise ValueError("Reference dimensions exceed the bounded image size")
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None or frame.shape != (height, width, 3):
        raise ValueError("Reference PNG could not be decoded")
    return frame


def run_calibration(
    kind,
    session,
    *,
    settings=None,
    reference_frame=None,
    output=None,
    progress=print,
    window=0,
    focus_start=None,
):
    """Run the source worker once with its original algorithm and microstep policy."""
    focus, options, scan = validated_calibration_settings(kind, settings)
    if type(window) is not int or not 0 <= window <= 20:
        raise ValueError("Measured turret window must be within 0..20 full steps")
    if window and kind in ("objective", "light") and type(focus_start) is not int:
        raise ValueError("At-focus calibration requires --focus-start in MICRO 4 coordinates")
    default = {
        "reference": "objective_reference.png",
        "objective": "objective_calibration.csv",
        "light": "light_objective_calibration.csv",
    }[kind]
    destination = calibration_path(default if output is None else output)
    session.check()
    if kind == "reference":
        if destination.suffix.lower() != ".png":
            raise ValueError("Objective references must be PNG images")
        frame = session.camera.get_frame(wait=True, timeout=2.0, raw=True, copy=True)
        session.check()
        if frame is None:
            raise RuntimeError("No camera frame is available")
        ok, encoded = cv2.imencode(".png", frame)
        if not ok:
            raise RuntimeError("Reference image could not be encoded")
        with private_open(destination, "wb") as stream:
            stream.write(encoded.tobytes())
        session.check()
        return destination
    if reference_frame is None:
        raise ValueError("A decoded reference image is required")
    if window:
        session.prepare_at_focus(window, focus_start, focus, progress)
    algorithm = LightObjectiveCalibrator if kind == "light" else ObjectiveCalibrator
    writer = save_light_objective_measurements if kind == "light" else save_objective_measurements
    parameters = dict(
        options,
        reference_frame=reference_frame,
        progress=progress,
        before_move=session.before_turret_move if window else session.move_to_clearance,
        cancel_event=session.cancel_event,
    )
    parameters["focus_progress" if kind == "light" else "position_progress"] = progress
    calibrator = algorithm(session.camera, session.motor, **parameters)
    if kind == "light":
        scan.update(
            focus_half_range=focus["local_half_range"],
            focus_step=focus["local_step"],
            focus_fine_radius=focus["local_fine_radius"],
            focus_min_step=focus["local_min_step"],
            focus_speed=focus["speed"],
            focus_settle_seconds=focus["precision_settle"],
            focus_frames=focus["precision_frames"],
            focus_frame_interval=focus["frame_interval"],
            focus_roi_fraction=focus["roi"],
            focus_backlash_steps=focus["backlash"],
        )
    try:
        result = calibrator.run(**scan)
        session.check()  # Source cleanup must not swallow a refused reference handoff.
    except BaseException as error:
        session.abort(error)
        raise
    finally:
        writer(destination, calibrator.measurements)
    session.check()
    progress(str(result))
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("objective", "light", "reference", "setup"))
    parser.add_argument("--focus-start", type=int, help="Working focus in measured MICRO 4 pulses")
    parser.add_argument("--settings", help="TOML focus, calibrator and scan settings")
    parser.add_argument(
        "--reference", default="objective_reference.png", help="Private calibration PNG"
    )
    parser.add_argument("--output", help="File beneath MG400_MICROSCOPE_CALIBRATION_DIR")
    args = parser.parse_args(argv)
    try:
        if args.kind == "setup":
            if (
                args.settings
                or args.output
                or args.focus_start is not None
                or args.reference != "objective_reference.png"
            ):
                raise ValueError(
                    "Setup accepts no settings, reference, output or focus-start option"
                )
            with setup_session() as session:
                run_setup(session)
            return 0
        window = nudge_steps(os.environ)
        if window and args.kind in ("objective", "light"):
            if args.focus_start is None:
                raise ValueError("At-focus calibration requires --focus-start")
            measured = ReferenceSettings.from_env(os.environ)
            if not measured.minimum <= args.focus_start <= measured.maximum:
                raise ValueError("--focus-start is outside the measured focus limits")
        settings = load_settings(args.settings)
        focus, _, _ = validated_calibration_settings(args.kind, settings)
        if args.output is not None:
            calibration_path(args.output)
        reference = None if args.kind == "reference" else read_reference(args.reference)
        with supervised_session(
            resolution=(focus["camera_width"], focus["camera_height"]),
            motion=args.kind != "reference",
        ) as session:
            destination = run_calibration(
                args.kind,
                session,
                settings=settings,
                reference_frame=reference,
                output=args.output,
                window=window,
                focus_start=args.focus_start,
            )
        print(destination)
    except KeyboardInterrupt:
        print("Microscope calibration stopped", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Microscope calibration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
