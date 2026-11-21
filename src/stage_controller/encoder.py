"""Thread-safe controller, independent of Flask and the browser.

One lock owns the serial stream. It covers the entire select/direction/speed/
rotations or hold transaction so separate motors or browser tabs cannot mix commands.
"""

import math
import re
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone

from serial.tools import list_ports

from .protocol import (
    ACTIVE_CALIBRATION,
    BAUD_RATE,
    COOLDOWN_SECONDS,
    DEFAULT_ROTATIONS,
    DEFAULT_SPEED,
    ENCODER_COUNTS_PER_ROTATION,
    HOLD_TIMEOUT_SECONDS,
    MAX_ROTATIONS,
    MAX_SPEED,
    MICROSTEP_MODES,
    MIN_ROTATIONS,
    MIN_SPEED,
    MOTOR_IDS,
    POSITION_LIMIT,
    REQUIRED_MICROSTEP,
    TELEMETRY_TIMEOUT_SECONDS,
    CommandRejected,
    ControllerError,
    DemoTransport,
    LinkError,
    MotorStatus,
    SerialTransport,
    absolute_position_error,
    has_calibrated_range,
    limit_defaults,
    motion_limit_error,
)


def _configured(state):
    return bool(state.lower_limit_set or state.upper_limit_set or state.calibration == "done")


class MotorController:
    def __init__(self, *, demo=False, transport_factory=None, clock=time.monotonic):
        self.demo = demo
        self.clock = clock
        self._factory = transport_factory or (
            (lambda _port: DemoTransport(clock)) if demo else SerialTransport
        )
        self._lock = threading.RLock()
        self._transport = None
        self._port = None
        self._error = None
        self._motors = {motor: MotorStatus(motor) for motor in MOTOR_IDS}
        self._microstep = None
        self._closed_loop = False
        self._telemetry_streaming = False
        self._travel_limits = False
        self._home_switches = False
        self._lower_home_zero = False
        self._calibration_only_homing = False
        self._auto_calibration = False
        self._absolute_positioning = False
        self._hold_direction_warnings = False
        self._lower_limit_direction = None
        self._positive_direction = "cw"
        self._observed_at = self.clock()
        self._active = set()
        self._holds = {}
        self._closed_holds = {}
        self._stopped_at = float("-inf")
        self._cooldowns = dict.fromkeys(MOTOR_IDS, 0.0)
        self._events = deque(maxlen=30)
        self._event_id = 0
        self._shutdown = threading.Event()
        self._thread = None

    def ports(self):
        if self.demo:
            return [{"device": "DEMO", "description": "Simulated Arduino · no hardware"}]
        return [
            {"device": port.device, "description": port.description}
            for port in sorted(list_ports.comports())
        ]

    def _event(self, message, level="info"):
        self._event_id += 1
        self._events.appendleft(
            {
                "id": self._event_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "level": level,
            }
        )

    def _require_connection(self):
        if self._transport is None:
            raise ControllerError("Connect to an Arduino first.")

    def _read_status(self, *, fresh=True):
        statuses = self._transport.status() if fresh else self._transport.poll_status()
        self._microstep = self._transport.microstep
        self._positive_direction = self._transport.positive_direction
        self._closed_loop = self._transport.closed_loop
        self._telemetry_streaming = self._transport.telemetry_streaming
        self._travel_limits = self._transport.travel_limits and self._closed_loop
        self._lower_limit_direction = (
            self._transport.lower_limit_direction if self._travel_limits else None
        )
        self._home_switches = (
            self._transport.home_switches and self._travel_limits and self._telemetry_streaming
        )
        self._lower_home_zero = (
            self._transport.lower_home_zero
            and self._home_switches
            and self._lower_limit_direction == "cw"
        )
        self._calibration_only_homing = (
            self._transport.calibration_only_homing
            and self._home_switches
            and self._lower_limit_direction == "cw"
        )
        self._auto_calibration = (
            self._transport.auto_calibration
            and (self._lower_home_zero or self._calibration_only_homing)
            and self._positive_direction == "ccw"
        )
        self._absolute_positioning = (
            self._transport.absolute_positioning
            and self._auto_calibration
            and self._calibration_only_homing
        )
        self._hold_direction_warnings = (
            self._transport.hold_direction_warnings
            and self._home_switches
            and (self._lower_home_zero or self._calibration_only_homing)
        )
        now = self.clock()
        self._observed_at = now
        for motor, state in statuses.items():
            previous = self._motors[motor]
            if self._hold_direction_warnings:
                increase = state.direction_warning_count - previous.direction_warning_count
                if increase > 0:
                    self._event(
                        f"{motor.title()} recorded {increase} held-direction "
                        f"warning{'s' if increase != 1 else ''}; "
                        f"total {state.direction_warning_count}.",
                        "warning",
                    )
                if state.warning != previous.warning:
                    if state.warning == "direction_pause":
                        self._event(
                            f"{motor.title()} hold paused while encoder feedback settles.",
                            "warning",
                        )
                    elif state.mode == "continuous" and state.fault == "none":
                        self._event(
                            f"{motor.title()} held motion resumed after the direction pause."
                        )
                    else:
                        self._event(f"{motor.title()} direction pause ended; motor stopped.")
                if state.recalibration_recommended and not previous.recalibration_recommended:
                    self._event(
                        f"{motor.title()} has more than five direction warnings; "
                        "Auto calibrate is recommended.",
                        "warning",
                    )
                if previous.direction_warning_count > 0 and state.direction_warning_count == 0:
                    self._event(f"{motor.title()} direction-warning history cleared.")
            if state.calibration != previous.calibration:
                labels = {
                    "lower": "seeking the lower switch",
                    "upper": "seeking the upper switch",
                    "center": "moving to the midpoint",
                    "done": "complete at the midpoint",
                    "cancelled": "cancelled",
                    "failed": "failed",
                    "none": "idle",
                }
                self._event(
                    f"{motor.title()} calibration {labels[state.calibration]}.",
                    "error" if state.calibration == "failed" else "info",
                )
            if self._home_switches:
                for side, bit in (("lower", 1), ("upper", 2)):
                    if (
                        previous.switch_mask is not None
                        and state.switch_mask is not None
                        and bool(previous.switch_mask & bit) != bool(state.switch_mask & bit)
                    ):
                        action = "triggered" if state.switch_mask & bit else "released"
                        self._event(f"{motor.title()} {side} home switch {action}.")
                    bound = getattr(state, f"{side}_limit")
                    enabled = getattr(state, f"{side}_limit_set")
                    if getattr(previous, f"{side}_limit") is not None and (bound, enabled) != (
                        getattr(previous, f"{side}_limit"),
                        getattr(previous, f"{side}_limit_set"),
                    ):
                        detail = f"updated to {bound:g} rotations" if enabled else "cleared"
                        self._event(f"{motor.title()} {side} travel limit {detail}.")
            if state.fault != "none" and state.fault != self._motors[motor].fault:
                self._event(
                    f"{motor.title()} stopped with {state.fault} fault; inspect and clear.", "error"
                )
            busy = (
                state.mode != "idle"
                or state.calibration in ACTIVE_CALIBRATION
                or state.warning == "direction_pause"
            )
            if not busy:
                self._forget_hold(motor)
            if busy:
                self._active.add(motor)
            elif motor in self._active:
                self._active.remove(motor)
                self._cooldowns[motor] = now + COOLDOWN_SECONDS
                self._event(f"{motor.title()} stopped · 1 second cooldown")
        self._motors = statuses

    def _lose_connection(self, exc):
        # Try to cancel movement even when the response stream is lost.
        # A broken cable cannot deliver this command: never claim it stopped.
        try:
            self._transport.command("stop all")
        except ControllerError:
            pass
        finally:
            for motor in list(self._holds):
                self._forget_hold(motor)
            self._transport.close()
            self._transport = None
            self._port = None
        self._error = f"{exc} Connection lost; motor stop could not be confirmed."
        self._event(self._error, "error")

    def connect(self, port):
        with self._lock:
            if self._transport is not None:
                raise ControllerError("Disconnect the current board before choosing another port.")
            if port not in {item["device"] for item in self.ports()}:
                raise ControllerError("That port is no longer available. Refresh the port list.")
            transport = self._factory(port)
            try:
                statuses = transport.status()
            except ControllerError:
                transport.close()
                raise
            self._transport = transport
            self._port = port
            # Opening the port resets the Arduino, which clears limits and calibration.
            reset = [
                motor.title()
                for motor, state in self._motors.items()
                if _configured(state) and motor in statuses and not _configured(statuses[motor])
            ]
            self._motors = statuses
            self._microstep = transport.microstep
            self._positive_direction = transport.positive_direction
            self._closed_loop = transport.closed_loop
            self._telemetry_streaming = transport.telemetry_streaming
            self._travel_limits = transport.travel_limits and self._closed_loop
            self._lower_limit_direction = (
                transport.lower_limit_direction if self._travel_limits else None
            )
            self._home_switches = (
                transport.home_switches and self._travel_limits and self._telemetry_streaming
            )
            self._lower_home_zero = (
                transport.lower_home_zero
                and self._home_switches
                and self._lower_limit_direction == "cw"
            )
            self._calibration_only_homing = (
                transport.calibration_only_homing
                and self._home_switches
                and self._lower_limit_direction == "cw"
            )
            self._auto_calibration = (
                transport.auto_calibration
                and (self._lower_home_zero or self._calibration_only_homing)
                and self._positive_direction == "ccw"
            )
            self._absolute_positioning = (
                transport.absolute_positioning
                and self._auto_calibration
                and self._calibration_only_homing
            )
            self._hold_direction_warnings = (
                transport.hold_direction_warnings
                and self._home_switches
                and (self._lower_home_zero or self._calibration_only_homing)
            )
            self._observed_at = self.clock()
            self._active = {
                motor
                for motor, state in statuses.items()
                if state.mode != "idle"
                or state.calibration in ACTIVE_CALIBRATION
                or state.warning == "direction_pause"
            }
            self._error = None
            # Preserve cooldowns across reconnects; reconnecting is not a bypass.
            self._event(f"Connected to {port} · {BAUD_RATE:,} baud")
            if reset:
                self._event(
                    f"Arduino reset cleared the travel limits and calibration of "
                    f"{', '.join(reset)}. Set the limits or Auto calibrate again.",
                    "warning",
                )
            return self.snapshot()

    def disconnect(self):
        with self._lock:
            if self._transport is not None:
                try:
                    self._transport.command("stop all")
                    self._read_status()
                except ControllerError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
                self._transport.close()
                self._transport = None
                self._port = None
                self._event("All motors stopped · disconnected")
            return self.snapshot()

    def set_microstep(self, divisor):
        if type(divisor) is not int or divisor not in MICROSTEP_MODES:
            raise ValueError("All stages use fixed 1/8 stepping (microstep 8).")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if self._microstep is None:
                    raise ControllerError(
                        "Upload the updated stage-controller-v3 sketch to change stepping mode."
                    )
                if any(
                    state.mode != "idle"
                    or state.calibration in ACTIVE_CALIBRATION
                    or state.warning == "direction_pause"
                    for state in self._motors.values()
                ):
                    raise ControllerError("Stop all motors before changing stepping mode.")
                if divisor != self._microstep:
                    self._transport.command(f"microstep {divisor}")
                    self._read_status()
                    if self._microstep != divisor:
                        raise LinkError("Arduino did not confirm the requested stepping mode.")
                    self._event(f"All motors · {MICROSTEP_MODES[divisor]}")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    @staticmethod
    def _validate_number(value, minimum, maximum, name):
        if (
            type(value) not in (int, float)
            or not minimum <= value <= maximum
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be a number from {minimum:g} to {maximum:g}.")

    def _require_motion_ready(self, motor, speed):
        if not self._closed_loop:
            raise ControllerError(
                "Upload stage-controller-v3 with closed-loop encoder support before moving."
            )
        if self._microstep != REQUIRED_MICROSTEP:
            raise ControllerError(
                "All stages require 1/8 stepping. Upload the fixed-mode "
                "stage-controller-v3 sketch before moving."
            )
        state = self._motors[motor]
        if self._telemetry_streaming and (
            state.telemetry_age_ms is None
            or state.telemetry_age_ms > TELEMETRY_TIMEOUT_SECONDS * 1000
        ):
            raise ControllerError("Waiting for fresh position telemetry from the Arduino.")
        if self._home_switches and (
            state.limit_telemetry_age_ms is None
            or state.limit_telemetry_age_ms > TELEMETRY_TIMEOUT_SECONDS * 1000
        ):
            raise ControllerError("Waiting for fresh limit/switch telemetry from the Arduino.")
        if state.calibration in ACTIVE_CALIBRATION:
            raise ControllerError(
                f"{motor.title()} calibration is still running. Use Stop all to cancel."
            )
        if state.mode != "idle" or state.warning == "direction_pause":
            raise ControllerError(f"{motor.title()} is still moving.")
        if state.encoder != "ok" or state.fault != "none":
            raise ControllerError(
                f"{motor.title()} encoder: {state.encoder}; fault: {state.fault}. "
                "Check the encoder and clear its fault before moving."
            )
        if speed > state.max_speed:
            raise ValueError(
                f"{motor.title()} speed must be at most {state.max_speed:g} rotations/s "
                "at the selected stepping mode."
            )
        if self.clock() < self._cooldowns[motor]:
            raise ControllerError(f"Wait for {motor}'s 1 second cooldown to finish.")

    def move(self, motor, direction, speed, rotations):
        if motor not in MOTOR_IDS or direction not in ("cw", "ccw"):
            raise ValueError("Choose a valid motor and direction (cw or ccw).")
        self._validate_number(speed, MIN_SPEED, MAX_SPEED, "Speed (rotations/s)")
        self._validate_number(rotations, MIN_ROTATIONS, MAX_ROTATIONS, "Rotations")
        if rotations / speed > 86400:
            raise ValueError("A single move must take at most 24 hours at the requested speed.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                self._require_motion_ready(motor, speed)
                if self._travel_limits and (
                    error := motion_limit_error(self._motors[motor], direction, rotations)
                ):
                    raise ControllerError(error)
                for command in (
                    f"motor {motor}",
                    f"dir {direction}",
                    f"speed {speed:.9g}",
                    f"rev {rotations:.9g}",
                ):
                    self._transport.command(command)
                # Mark active even if a small move finishes before the next poll.
                self._active.add(motor)
                self._motors[motor].mode = "moving"
                sign = 1 if direction == self._motors[motor].positive_direction else -1
                self._motors[motor].remaining = rotations * sign
                self._motors[motor].speed = speed
                self._motors[motor].direction = direction
                self._event(
                    f"{motor.title()} · {direction.upper()} · {rotations:g} rotations "
                    f"at {speed:g} rotations/s"
                )
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def move_to_position(self, motor, position, speed):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        self._validate_number(position, 0, POSITION_LIMIT, "Position (rotations)")
        self._validate_number(speed, MIN_SPEED, MAX_SPEED, "Speed (rotations/s)")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if not self._absolute_positioning:
                    raise ControllerError(
                        "Upload stage-controller-v3 with calibrated absolute positioning support."
                    )
                self._require_motion_ready(motor, speed)
                before = self._motors[motor]
                if error := absolute_position_error(before, position):
                    raise ControllerError(error)
                target = math.floor(position * ENCODER_COUNTS_PER_ROTATION + 0.5)
                current = round(before.position * ENCODER_COUNTS_PER_ROTATION)
                distance = abs(target - current) / ENCODER_COUNTS_PER_ROTATION
                if distance / speed > 86400:
                    raise ValueError(
                        "A single move must take at most 24 hours at the requested speed."
                    )
                for command in (f"motor {motor}", f"speed {speed:.9g}", f"goto {position:.9g}"):
                    self._transport.command(command)
                if distance > before.tolerance:
                    self._active.add(motor)
                # Consume the acknowledged transport state without another
                # status request; an already-reached target remains idle.
                self._read_status(fresh=False)
                self._event(
                    f"{motor.title()} target position: {position:g} rotations "
                    f"at {speed:g} rotations/s."
                )
            except CommandRejected:
                try:
                    self._read_status()
                except LinkError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
                raise
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def calibrate(self, motor, speed):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        self._validate_number(speed, MIN_SPEED, MAX_SPEED, "Speed (rotations/s)")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if not self._auto_calibration:
                    raise ControllerError(
                        "Upload stage-controller-v3 with automatic switch calibration support."
                    )
                self._require_motion_ready(motor, speed)
                if self._motors[motor].switch_mask == 3:
                    raise ControllerError("Resolve the conflicting home-switch signals first.")
                state = self._motors[motor]
                # Limits set with Set limit (calibration not done) do not survive a calibration.
                manual_limits = state.calibration != "done" and (
                    state.lower_limit_set or state.upper_limit_set
                )
                for command in (f"motor {motor}", f"speed {speed:.9g}", "calibrate"):
                    self._transport.command(command)
                if manual_limits:
                    self._event(
                        f"{motor.title()} auto calibration cleared the travel limits; "
                        "it moves to both home switches.",
                        "warning",
                    )
                self._active.add(motor)
                state.calibration, state.mode, state.remaining = "lower", "continuous", 0.0
                state.speed = speed
                self._event(
                    f"{motor.title()} auto calibration started: "
                    "lower switch, upper switch, midpoint."
                )
            except CommandRejected:
                try:
                    self._read_status()
                except LinkError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
                raise
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    @staticmethod
    def _validate_hold(motor, hold_id):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        if not isinstance(hold_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", hold_id):
            raise ValueError("Invalid hold identifier.")

    def _forget_hold(self, motor):
        hold = self._holds.pop(motor, None)
        if hold is not None:
            self._closed_holds[(motor, hold[0])] = self.clock()

    def _stop_hold(self, motor):
        self._transport.command(f"motor {motor}")
        self._transport.command("stop")
        self._forget_hold(motor)

    def hold(self, motor, direction, speed, hold_id, sampled_at):
        self._validate_hold(motor, hold_id)
        if direction not in ("cw", "ccw"):
            raise ValueError("Choose a direction (cw or ccw).")
        self._validate_number(speed, MIN_SPEED, MAX_SPEED, "Speed (rotations/s)")
        with self._lock:
            now = self.clock()
            # Expire delayed start requests. Closed identifiers outlive this window,
            # so a release arriving before its start can never turn into a late move.
            # A stop also cancels a hold whose start was sampled before the stop.
            if (
                type(sampled_at) not in (int, float)
                or not now - 2.0 <= sampled_at <= now
                or sampled_at < self._stopped_at
            ):
                raise ControllerError("Hold request expired. Release and press again.")
            self._closed_holds = {
                key: ended for key, ended in self._closed_holds.items() if now - ended < 10
            }
            if (motor, hold_id) in self._closed_holds:
                raise ControllerError("Hold has ended. Release and press again.")
            self._require_connection()
            try:
                self._read_status()
                if motor in self._holds and self._holds[motor][0] == hold_id:
                    return self.snapshot()
                self._require_motion_ready(motor, speed)
                if self._travel_limits and (
                    error := motion_limit_error(self._motors[motor], direction)
                ):
                    raise ControllerError(error)
                for command in (f"motor {motor}", f"dir {direction}", f"speed {speed:.9g}"):
                    self._transport.command(command)
                try:
                    self._transport.command("hold")
                except CommandRejected as exc:
                    raise ControllerError(
                        "Hold rejected. Check encoder faults and the stage-controller-v3 sketch."
                    ) from exc
                self._holds[motor] = (hold_id, self.clock() + HOLD_TIMEOUT_SECONDS)
                self._active.add(motor)
                self._motors[motor].mode = "continuous"
                self._motors[motor].remaining = 0
                self._motors[motor].speed = speed
                self._motors[motor].direction = direction
                self._event(f"{motor.title()} · hold {direction.upper()} · {speed:g} rotations/s")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def keep_hold_alive(self, motor, hold_id):
        self._validate_hold(motor, hold_id)
        with self._lock:
            self._require_connection()
            hold = self._holds.get(motor)
            if hold is None or hold[0] != hold_id:
                raise ControllerError("Hold has ended. Release and press again.")
            try:
                if self.clock() >= hold[1]:
                    self._stop_hold(motor)
                    self._read_status()
                    raise ControllerError("Hold timed out. Release and press again.")
                self._transport.command(f"motor {motor}")
                try:
                    self._transport.command("keepalive")
                except CommandRejected:
                    self._stop_hold(motor)
                    self._read_status()
                    raise ControllerError("Hold has ended. Release and press again.") from None
                self._holds[motor] = (hold_id, self.clock() + HOLD_TIMEOUT_SECONDS)
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def release_hold(self, motor, hold_id):
        self._validate_hold(motor, hold_id)
        with self._lock:
            # Record cancellation even when the corresponding start is still in flight.
            self._closed_holds[(motor, hold_id)] = self.clock()
            hold = self._holds.get(motor)
            if hold is not None and hold[0] == hold_id and self._transport is not None:
                try:
                    self._stop_hold(motor)
                    self._read_status()
                except ControllerError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
            return self.snapshot()

    def stop_all(self):
        with self._lock:
            self._stopped_at = self.clock()
            self._require_connection()
            try:
                self._transport.command("stop all")
                self._read_status()
                self._event("Stop all acknowledged by Arduino")
            except ControllerError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def clear_fault(self, motor):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                state = self._motors[motor]
                if not self._closed_loop:
                    raise ControllerError("Upload stage-controller-v3 to use encoder feedback.")
                if (
                    state.mode != "idle"
                    or state.calibration in ACTIVE_CALIBRATION
                    or state.warning == "direction_pause"
                ):
                    raise ControllerError("Stop the motor before clearing its fault.")
                if state.encoder in ("offline", "magnet"):
                    raise ControllerError("Restore the encoder connection and magnet first.")
                if state.encoder == "untrusted" and (
                    state.lower_limit_set or state.upper_limit_set
                ):
                    raise ControllerError(
                        "Position tracking was lost. Clear both travel limits before clearing "
                        "the encoder fault, then set the limits again."
                    )
                self._transport.command(f"motor {motor}")
                self._transport.command("clear")
                self._read_status()
                state = self._motors[motor]
                if state.fault != "none" or state.encoder != "ok" or state.mode != "idle":
                    raise ControllerError("Encoder is not ready; fault clear was not confirmed.")
                self._event(f"{motor.title()} encoder fault cleared; motor remains stopped.")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def _configure_travel(self, motor, action):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if not self._travel_limits:
                    raise ControllerError(
                        "Upload stage-controller-v3 with position reset and travel-limit "
                        "support to use this control."
                    )
                before = replace(self._motors[motor])
                if (
                    before.mode != "idle"
                    or before.calibration in ACTIVE_CALIBRATION
                    or before.warning == "direction_pause"
                ):
                    raise ControllerError("Stop the motor before changing its position or limits.")
                if action in ("reset", "setlimit"):
                    if before.encoder != "ok":
                        raise ControllerError("A healthy, trusted encoder is required.")
                    if self._telemetry_streaming and (
                        before.telemetry_age_ms is None
                        or before.telemetry_age_ms > TELEMETRY_TIMEOUT_SECONDS * 1000
                    ):
                        raise ControllerError(
                            "Waiting for fresh position telemetry from the Arduino."
                        )
                if action == "setlimit" and before.position == 0:
                    raise ControllerError("Move away from zero before setting a travel limit.")
                self._transport.command(f"motor {motor}")
                self._transport.command(action)
                self._read_status()
                after = self._motors[motor]
                lower_default, upper_default = limit_defaults(
                    after.lower_limit_direction, after.positive_direction
                )
                tolerance = max(before.tolerance, after.tolerance, 1 / 4096)
                confirmed = (
                    self._travel_limits
                    and after.mode == "idle"
                    and (before.fault == "none" or after.fault == before.fault)
                )
                if action == "reset":
                    confirmed = confirmed and (
                        abs(after.position) <= tolerance
                        and after.lower_limit_set is False
                        and after.upper_limit_set is False
                        and after.lower_limit == lower_default
                        and after.upper_limit == upper_default
                    )
                    message = "position reset to zero; both travel limits cleared"
                elif action == "setlimit":
                    # Firmware captures a fresh encoder sample; the shaft may
                    # have moved since the pre-command status, even across zero.
                    captured = []
                    if confirmed:
                        lower_sign = (
                            1 if after.lower_limit_direction == after.positive_direction else -1
                        )
                        for side, opposite, sign in (
                            ("lower", "upper", lower_sign),
                            ("upper", "lower", -lower_sign),
                        ):
                            bound = getattr(after, f"{side}_limit")
                            if (
                                getattr(after, f"{side}_limit_set") is True
                                and sign * bound > 0
                                and abs(bound - after.position) <= tolerance
                                and getattr(after, f"{opposite}_limit")
                                == getattr(before, f"{opposite}_limit")
                                and getattr(after, f"{opposite}_limit_set")
                                == getattr(before, f"{opposite}_limit_set")
                            ):
                                captured.append(side)
                    confirmed = confirmed and bool(captured)
                    message = f"{captured[0]} travel limit set" if captured else "travel limit set"
                else:
                    side = "upper" if action == "clearupper" else "lower"
                    opposite = "lower" if side == "upper" else "upper"
                    confirmed = confirmed and (
                        getattr(after, f"{side}_limit_set") is False
                        and getattr(after, f"{side}_limit")
                        == (upper_default if side == "upper" else lower_default)
                        and getattr(after, f"{opposite}_limit")
                        == getattr(before, f"{opposite}_limit")
                        and getattr(after, f"{opposite}_limit_set")
                        == getattr(before, f"{opposite}_limit_set")
                    )
                    message = f"{side} travel limit cleared"
                if not confirmed:
                    raise ControllerError("Arduino did not confirm the position/limit change.")
                self._event(f"{motor.title()} {message}; motor remains stopped.")
            except CommandRejected:
                # A fresh hardware read may explain a rejection caused by a new
                # encoder fault. Never report the requested limits as confirmed.
                try:
                    self._read_status()
                except LinkError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
                raise
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def reset_position(self, motor):
        return self._configure_travel(motor, "reset")

    def set_limit(self, motor):
        return self._configure_travel(motor, "setlimit")

    def clear_upper_limit(self, motor):
        return self._configure_travel(motor, "clearupper")

    def clear_lower_limit(self, motor):
        return self._configure_travel(motor, "clearlower")

    def poll(self):
        with self._lock:
            if self._transport is not None:
                try:
                    for motor, (_, deadline) in list(self._holds.items()):
                        if self.clock() >= deadline:
                            self._stop_hold(motor)
                    self._read_status(fresh=False)
                except (LinkError, CommandRejected) as exc:
                    self._lose_connection(exc)

    def snapshot(self):
        with self._lock:
            now = self.clock()
            connected = self._transport is not None
            motors = []
            for motor, status in self._motors.items():
                cooldown = max(0.0, self._cooldowns[motor] - now)
                age = status.telemetry_age_ms
                if age is not None:
                    age += max(0, int((now - self._observed_at) * 1000))
                fresh = not self._telemetry_streaming or (
                    age is not None and age <= TELEMETRY_TIMEOUT_SECONDS * 1000
                )
                limit_age = status.limit_telemetry_age_ms
                if limit_age is not None:
                    limit_age += max(0, int((now - self._observed_at) * 1000))
                if self._home_switches:
                    fresh = fresh and (
                        limit_age is not None and limit_age <= TELEMETRY_TIMEOUT_SECONDS * 1000
                    )
                available = (
                    connected
                    and fresh
                    and self._closed_loop
                    and self._microstep == REQUIRED_MICROSTEP
                    and status.mode == "idle"
                    and status.calibration not in ACTIVE_CALIBRATION
                    and status.warning != "direction_pause"
                    and cooldown == 0
                    and status.encoder == "ok"
                    and status.fault == "none"
                )
                motors.append(
                    {
                        **asdict(status),
                        "recalibration_recommended": status.recalibration_recommended,
                        "telemetry_age_ms": age,
                        "limit_telemetry_age_ms": limit_age,
                        "cooldown": round(cooldown, 3),
                        "available": available,
                        "positioning_ready": (
                            available
                            and self._absolute_positioning
                            and has_calibrated_range(status)
                        ),
                    }
                )
            return {
                "sampled_at": now,
                "connected": connected,
                "port": self._port,
                "demo": self.demo,
                "error": self._error,
                "baud_rate": BAUD_RATE,
                "microstep": self._microstep if connected else None,
                "closed_loop": connected and self._closed_loop,
                "telemetry_streaming": connected and self._telemetry_streaming,
                "travel_limits": connected and self._travel_limits,
                "home_switches": connected and self._home_switches,
                "lower_home_zero": connected and self._lower_home_zero,
                "calibration_only_homing": connected and self._calibration_only_homing,
                "auto_calibration": connected and self._auto_calibration,
                "absolute_positioning": connected and self._absolute_positioning,
                "hold_direction_warnings": connected and self._hold_direction_warnings,
                "lower_limit_direction": self._lower_limit_direction if connected else None,
                "positive_direction": self._positive_direction if connected else None,
                "required_microstep": REQUIRED_MICROSTEP,
                "microstep_modes": [
                    {"value": value, "label": label} for value, label in MICROSTEP_MODES.items()
                ],
                "motors": motors,
                "events": list(self._events),
                "defaults": {"speed": DEFAULT_SPEED, "rotations": DEFAULT_ROTATIONS},
                "limits": {
                    "speed": MAX_SPEED,
                    "rotations": MAX_ROTATIONS,
                    "min_speed": MIN_SPEED,
                    "min_rotations": MIN_ROTATIONS,
                },
            }

    def start(self):
        if self._thread is None:

            def run():
                while not self._shutdown.wait(0.05):
                    self.poll()

            self._thread = threading.Thread(target=run, name="arduino-status", daemon=True)
            self._thread.start()

    def close(self):
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.disconnect()
        except ControllerError:
            pass
