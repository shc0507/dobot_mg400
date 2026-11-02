"""Switch objectives, then calibrate the disabled side-camera pose.

Positive moves follow red -> yellow -> green -> blue in the supplied
scan. Independent ArUco measurements close the loop around each reference.
No hardware runs at import time.
"""

import csv
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, fields
from pathlib import Path

from .aruco_locator import COLORS, Locator
from .paths import private_open, run_path
from .side_capture import SideCaptureCancelled

OBJECTIVE_ORDER = (0, 1, 2, 3)
MOTION_MICROSTEP = 4


def full_steps_to_pulses(full_steps):
    """Motor distances remain full-step equivalents; MICRO 4 needs 4x pulses."""
    return int(round(full_steps * MOTION_MICROSTEP))


@dataclass(frozen=True)
class SwitchSettings:
    speed: int = 200
    switch_steps: int = 185
    enable_settle_seconds: float = 0.2
    release_settle_seconds: float = 0.4
    verify_frames: int = 3
    frame_retries: int = 3
    tolerance_px: float = 5.0
    move_settle_seconds: float = 0.25
    max_correction_moves: int = 24
    max_correction_steps: float = 40.0
    max_release_attempts: int = 4
    progress_min_px: float = 0.5
    stagnant_move_limit: int = 8
    stagnant_release_limit: int = 3

    def __post_init__(self):
        for field in fields(self):
            if not math.isfinite(getattr(self, field.name)):
                raise ValueError(f"{field.name} must be finite")
        for name in (
            "speed",
            "switch_steps",
            "verify_frames",
            "frame_retries",
            "max_correction_moves",
            "max_release_attempts",
            "stagnant_move_limit",
            "stagnant_release_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.speed * MOTION_MICROSTEP > 10000:
            raise ValueError(
                "Objective base speed must be at most 2500 full steps/s (10000 pulses/s at MICRO 4)"
            )
        if not 1 <= self.switch_steps < 360:
            raise ValueError("Switch steps must be between 1 and 359")
        if (
            min(self.enable_settle_seconds, self.release_settle_seconds, self.move_settle_seconds)
            < 0
        ):
            raise ValueError("Settling times must be nonnegative")
        if self.verify_frames < 2:
            raise ValueError("Use at least two distinct verification frames")
        if min(self.tolerance_px, self.max_correction_steps, self.progress_min_px) <= 0:
            raise ValueError(
                "Tolerance, initial correction travel and progress threshold must be positive"
            )
        if self.stagnant_move_limit < 4:
            raise ValueError("Allow at least four correction commands before declaring no progress")


class CalibrationTrend:
    """Track real pixel error, with a noise threshold and a short recent window."""

    def __init__(self, error, minimum_progress):
        self.minimum_progress = minimum_progress
        self.best_error = self.checkpoint_error = abs(error)
        self.history = deque([abs(error)], maxlen=4)
        self.stagnant = 0

    def observe(self, error):
        error = abs(error)
        self.history.append(error)
        self.best_error = min(self.best_error, error)
        improved = self.checkpoint_error - self.best_error >= self.minimum_progress
        if improved:
            self.checkpoint_error = self.best_error
            self.stagnant = 0
        else:
            self.stagnant += 1
        return improved

    @property
    def advancing(self):
        return (
            len(self.history) >= 2 and self.history[0] - self.history[-1] >= self.minimum_progress
        )


class CorrectionBudget:
    """Renew initial pulse budgets only while measured pixel error improves."""

    def __init__(self, moves, full_steps):
        self.move_limit = moves
        self.step_limit = full_steps
        self.move_increment = max(4, moves // 2)
        self.step_increment = max(4.0, full_steps / 2)

    def reserve(self, moves, travel, distance, *, advancing):
        needs_moves = moves >= self.move_limit
        needs_steps = travel + distance > self.step_limit + 1e-9
        if not needs_moves and not needs_steps:
            return None
        if not advancing:
            raise RuntimeError(
                "Correction budget exhausted without recent pixel improvement: "
                f"commands={moves}/{self.move_limit}, travel={travel:g}/"
                f"{self.step_limit:g} full steps, next={distance:g} full steps"
            )
        old = (self.move_limit, self.step_limit)
        while moves >= self.move_limit:
            self.move_limit += self.move_increment
        while travel + distance > self.step_limit + 1e-9:
            self.step_limit += self.step_increment
        return (
            f"Pixel error is decreasing; budget extended: commands "
            f"{old[0]} -> {self.move_limit}, full steps {old[1]:g} -> {self.step_limit:g}"
        )


class CrossingProgress:
    """Count unshrinking zero crossings rather than every change of direction."""

    def __init__(self, minimum_progress):
        self.minimum_progress = minimum_progress
        self.checkpoint_width = None
        self.stagnant = 0
        self.moves_since_progress = 0
        self.last_improved = False

    @property
    def advancing(self):
        return self.last_improved and self.moves_since_progress <= 3

    def observe(self, before, after):
        self.moves_since_progress += 1
        if before * after >= 0:
            return None
        width = abs(before) + abs(after)
        self.last_improved = (
            self.checkpoint_width is not None
            and self.checkpoint_width - width >= self.minimum_progress
        )
        if self.checkpoint_width is None or self.last_improved:
            self.checkpoint_width = width
            self.stagnant = 0
            if self.last_improved:
                self.moves_since_progress = 0
        else:
            self.stagnant += 1
        return width


@dataclass(frozen=True)
class SwitchProgress:
    stage: str
    message: str
    marker_id: int | None = None
    dx_px: float | None = None
    microstep: int | None = None
    source_id: int | None = None
    target_id: int | None = None


@dataclass(frozen=True)
class SwitchResult:
    source_id: int
    target_id: int
    dx_px: float
    dy_px: float
    microstep: int
    log_path: Path
    max_abs_dx_px: float
    tolerance_px: float
    calibration_only: bool = False


def within_tolerance(measurements, tolerance_px):
    """Every independent disabled frame must pass; a good median is insufficient."""
    return bool(measurements) and all(abs(m["dx_px"]) <= tolerance_px for m in measurements)


def adjacent_objective(marker_id, direction):
    if direction not in (-1, 1):
        raise ValueError("Switch direction must be -1 or +1")
    return OBJECTIVE_ORDER[(OBJECTIVE_ORDER.index(marker_id) + direction) % 4]


def select_objective(markers, expected_id=None):
    """A locked ID always wins, even when a different marker is more central.

    Nearest-reference selection is only an explicit auto-identification option
    before a source has been chosen. Missing locked IDs never fall back to it.
    """
    if expected_id is not None:
        if expected_id not in OBJECTIVE_ORDER:
            raise ValueError(f"Unknown objective ID {expected_id}")
        matches = [m for m in markers if m["id"] == expected_id]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"Locked objective ID {expected_id} is not visible")
        raise ValueError(f"Duplicate detections for locked objective ID {expected_id}")
    if not markers:
        raise LookupError("No objective is visible for auto identification")
    return min(markers, key=lambda m: abs(m["dx_px"]))


class ArucoObjectiveSwitcher:
    def __init__(
        self,
        camera,
        motor,
        *,
        settings=None,
        locator=None,
        cancel_event=None,
        progress=None,
        before_move=None,
    ):
        self.camera, self.motor = camera, motor
        self.settings = settings or SwitchSettings()
        self.locator = locator if locator is not None else Locator()
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.progress, self.before_move = progress, before_move
        self.microstep = None
        self.frame_id = None
        self.frame_not_before = 0.0
        self.writer = None
        self.commanded_steps = 0
        self.correction_moves = 0
        self.correction_travel = 0.0
        self.budget = CorrectionBudget(
            self.settings.max_correction_moves, self.settings.max_correction_steps
        )
        self.source_id = self.target_id = None

    def _check(self):
        if self.cancel_event.is_set():
            raise SideCaptureCancelled("Side-camera objective switch was stopped")

    def _wait(self, seconds):
        if self.cancel_event.wait(seconds):
            self._check()

    def _settle(self, seconds):
        self.frame_not_before = time.monotonic() + seconds
        self._wait(seconds)

    def _report(self, stage, message, marker=None):
        if self.writer and stage in {
            "budget_extend",
            "budget_stop",
            "no_progress",
            "crossing_check",
            "release_progress",
            "target_locked",
        }:
            self.writer.writerow(
                (
                    time.monotonic(),
                    stage,
                    self.frame_id,
                    "diagnostic",
                    self.target_id,
                    marker["id"] if marker else "",
                    marker["dx_px"] if marker else "",
                    marker["dy_px"] if marker else "",
                    self.microstep,
                    self.commanded_steps,
                    "",
                    self.settings.tolerance_px,
                    message,
                )
            )
        if self.progress:
            self.progress(
                SwitchProgress(
                    stage,
                    message,
                    marker["id"] if marker else None,
                    marker["dx_px"] if marker else None,
                    self.microstep,
                    self.source_id,
                    self.target_id,
                )
            )

    def _command(self, command, prefix, timeout=5.0):
        self._check()
        return self.motor.send_and_wait(
            command, prefix, timeout=timeout, cancel_event=self.cancel_event
        )

    def _measure(self, stage, expected_id=None):
        # Accept the next distinct frame after settling, including an already
        # received eligible frame. Do not discard another frame for every
        # verification: no motion has occurred between those measurements.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self._check()
            packet = self.camera.get_frame_packet(after_id=self.frame_id, timeout=0.2, copy=True)
            if packet is None:
                continue
            self.frame_id, received_at, image = packet
            if received_at < self.frame_not_before:
                continue
            # Detect the locked ID, including retries for that ID. Other IDs
            # may be logged, but cannot replace the requested objective.
            result = self.locator.locate(image, target_id=expected_id, only_target=False)
            for marker in result["markers"] or [{}]:
                if self.writer:
                    self.writer.writerow(
                        (
                            time.monotonic(),
                            stage,
                            self.frame_id,
                            result["status"],
                            expected_id,
                            marker.get("id", ""),
                            marker.get("dx_px", ""),
                            marker.get("dy_px", ""),
                            self.microstep,
                            self.commanded_steps,
                            marker.get("method", ""),
                            self.settings.tolerance_px,
                            "",
                        )
                    )
            if result["status"] == "image_size_mismatch":
                raise RuntimeError("Side-camera resolution differs from the reference images")
            return result
        raise RuntimeError("No fresh side-camera frame arrived")

    def _visible_objective(self, stage, expected_id=None):
        last_status = "not_detected"
        for _ in range(self.settings.frame_retries):
            result = self._measure(stage, expected_id)
            last_status = result["status"]
            if result["status"] == "ok" and result["markers"]:
                try:
                    marker = select_objective(result["markers"], expected_id)
                except LookupError as error:
                    last_status = str(error)
                else:
                    self._report(
                        stage,
                        f"{COLORS[marker['id']]} ID {marker['id']}; dx={marker['dx_px']:+.3f}px",
                        marker,
                    )
                    return marker
            self._report(stage, f"{last_status}; retrying a fresh image without moving")
        raise RuntimeError(f"Objective identity could not be confirmed: {last_status}")

    def _correct(self, pulses, *, advancing=False):
        s = self.settings
        distance = abs(pulses) / MOTION_MICROSTEP
        try:
            extension = self.budget.reserve(
                self.correction_moves, self.correction_travel, distance, advancing=advancing
            )
        except RuntimeError as error:
            self._report("budget_stop", str(error))
            raise
        if extension:
            self._report("budget_extend", extension)
        if self.before_move:
            self.before_move()
        self._check()
        self._report(
            "correct",
            f"Move {pulses:+d} pulse(s), MICRO 4 "
            f"({pulses / MOTION_MICROSTEP:+g} full steps); "
            f"target |dx| <= {s.tolerance_px:g}px",
        )
        self.correction_moves += 1
        self.correction_travel += distance
        self.commanded_steps += pulses / MOTION_MICROSTEP
        self._command(
            f"OBJ MOVE {pulses}",
            "DONE OBJ POS ",
            timeout=max(5.0, abs(pulses) / (s.speed * MOTION_MICROSTEP) + 3.0),
        )
        self._settle(s.move_settle_seconds)

    def _disabled_measurements(self):
        measurements = []
        for _ in range(self.settings.verify_frames):
            marker = self._visible_objective("disabled_verify", self.target_id)
            measurements.append(marker)
            if abs(marker["dx_px"]) > self.settings.tolerance_px:
                break  # Already failed: start correcting without two extra photos.
        offsets = ", ".join(f"{m['dx_px']:+.3f}" for m in measurements)
        self._report(
            "tolerance_check",
            f"Disabled dx=[{offsets}]px; "
            f"limit +/-{self.settings.tolerance_px:g}px; "
            f"PASS={within_tolerance(measurements, self.settings.tolerance_px)}",
            measurements[-1],
        )
        return measurements

    def _calibrate(self, released):
        s = self.settings
        release_limit = s.max_release_attempts
        attempts = 0
        release_trend = CalibrationTrend(max(abs(m["dx_px"]) for m in released), s.progress_min_px)
        while True:
            if within_tolerance(released, s.tolerance_px):
                return released
            if attempts >= release_limit:
                if not release_trend.advancing:
                    message = (
                        "Release-check budget exhausted without recent improvement: "
                        f"checks={attempts}, best disabled |dx|="
                        f"{release_trend.best_error:.3f}px"
                    )
                    self._report("budget_stop", message, released[-1])
                    raise RuntimeError(message)
                old_limit = release_limit
                release_limit += s.max_release_attempts
                self._report(
                    "budget_extend",
                    f"Disabled error is decreasing; release checks {old_limit} -> {release_limit}",
                    released[-1],
                )
            self._command("OBJ ENABLE", "OK OBJ ENABLED")
            self._settle(s.enable_settle_seconds)
            marker = self._visible_objective("enabled_position", self.target_id)
            gain = 4.0  # Initial estimate only; never a pixel position source.
            pulse_cap = full_steps_to_pulses(4)
            trend = CalibrationTrend(marker["dx_px"], s.progress_min_px)
            crossings = CrossingProgress(s.progress_min_px)
            # If enabling alone brings the image to zero but release does not,
            # nudge toward the measured disabled error instead of repeating an
            # unchanged enable/disable cycle. The next photo checks the result.
            if abs(marker["dx_px"]) <= s.tolerance_px:
                error = max(released, key=lambda m: abs(m["dx_px"]))["dx_px"]
                self._correct(
                    full_steps_to_pulses(1 if error > 0 else -1), advancing=release_trend.advancing
                )
                marker = self._visible_objective("corrected_position", self.target_id)
                trend.observe(marker["dx_px"])
            while abs(marker["dx_px"]) > s.tolerance_px:
                old = marker
                dx = old["dx_px"]
                # Positive motor motion moves marker x left in the supplied scan.
                direction = 1 if dx > 0 else -1
                full_steps = min(4.0, 0.7 * abs(dx) / gain)
                cap = full_steps_to_pulses(4) if abs(dx) > 20 else pulse_cap
                pulses = max(1, min(cap, full_steps_to_pulses(full_steps)))
                if trend.stagnant >= 3:
                    pulses = min(
                        full_steps_to_pulses(4), max(2 ** min(trend.stagnant - 2, 4), pulses)
                    )
                    self._report(
                        "no_progress",
                        f"No new best error for {trend.stagnant} moves; "
                        f"trying {pulses} pulses at MICRO 4",
                        marker,
                    )
                advancing = (
                    trend.advancing
                    or crossings.advancing
                    or (len(trend.history) < 2 and release_trend.advancing)
                )
                self._correct(direction * pulses, advancing=advancing)
                marker = self._visible_objective("corrected_position", self.target_id)
                delta = marker["dx_px"] - dx
                if abs(delta) > 0.25 and delta * direction < 0:
                    if max(abs(dx), abs(marker["dx_px"])) <= 50:
                        gain = max(0.5, min(50.0, abs(delta) / (pulses / MOTION_MICROSTEP)))
                trend.observe(marker["dx_px"])
                crossing_width = crossings.observe(dx, marker["dx_px"])
                if crossing_width is not None:
                    pulse_cap = max(1, pulse_cap // 2)
                    if crossings.advancing:
                        trend.stagnant = 0
                    self._report(
                        "crossing_check",
                        f"Crossed zero: bracket width="
                        f"{crossing_width:.3f}px; nonshrinking crossings="
                        f"{crossings.stagnant}/3; next near-target cap={pulse_cap} pulses",
                        marker,
                    )
                if abs(marker["dx_px"]) <= s.tolerance_px:
                    break
                if trend.stagnant >= s.stagnant_move_limit or crossings.stagnant >= 3:
                    message = (
                        "Calibration stopped without effective pixel improvement: "
                        f"no-progress moves={trend.stagnant}/{s.stagnant_move_limit}, "
                        f"nonshrinking crossings={crossings.stagnant}/3, "
                        f"best |dx|={trend.best_error:.3f}px, "
                        f"current |dx|={abs(marker['dx_px']):.3f}px"
                    )
                    self._report("budget_stop", message, marker)
                    raise RuntimeError(message)
            self._command("OBJ DISABLE", "OK OBJ DISABLED")
            self._settle(s.release_settle_seconds)
            released = self._disabled_measurements()
            attempts += 1
            release_error = max(abs(m["dx_px"]) for m in released)
            release_trend.observe(release_error)
            self._report(
                "release_progress",
                f"Release check {attempts}/{release_limit}: "
                f"|dx|={release_error:.3f}px; best={release_trend.best_error:.3f}px; "
                f"checks without improvement={release_trend.stagnant}/"
                f"{s.stagnant_release_limit}",
                released[-1],
            )
            if within_tolerance(released, s.tolerance_px):
                return released
            if release_trend.stagnant >= s.stagnant_release_limit:
                message = (
                    f"Disabled position did not improve by {s.progress_min_px:g}px "
                    f"over {release_trend.stagnant} release checks; "
                    f"current |dx|={release_error:.3f}px, limit={s.tolerance_px:g}px"
                )
                self._report("budget_stop", message, released[-1])
                raise RuntimeError(message)

    def run(self, direction, log_path, *, source_id=None):
        if direction not in (-1, 1):
            raise ValueError("Switch direction must be -1 or +1")
        return self._execute(direction, log_path, source_id=source_id)

    def calibrate_current(self, log_path, *, marker_id=None):
        """Recognize and recover the current objective; no +/-185-step move."""
        return self._execute(0, log_path, source_id=marker_id)

    def _execute(self, direction, log_path, *, source_id=None):
        if source_id is not None and source_id not in OBJECTIVE_ORDER:
            raise ValueError(f"Unknown source objective ID {source_id}")
        path = run_path(log_path)
        try:
            with private_open(path, "x", newline="", encoding="utf-8", buffering=1) as output:
                self.writer = csv.writer(output)
                self.writer.writerow(
                    (
                        "monotonic",
                        "stage",
                        "frame_id",
                        "status",
                        "expected_id",
                        "detected_id",
                        "dx_px",
                        "dy_px",
                        "microstep",
                        "commanded_full_steps",
                        "method",
                        "tolerance_px",
                        "diagnostic",
                    )
                )
                result = self._run(direction, path, source_id=source_id)
        finally:
            self.writer = None
            # Cancellation cannot suppress STOP/DISABLE. Attempt both even if
            # the first fails; do not report success if release is unconfirmed.
            errors = []
            for command, prefix in (
                ("STOP OBJ", "OK STOP OBJ"),
                ("OBJ DISABLE", "OK OBJ DISABLED"),
            ):
                try:
                    self.motor.send_and_wait(command, prefix, timeout=3.0)
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError("Could not confirm objective disabled: " + "; ".join(errors))
        return result

    def _run(self, direction, path, *, source_id=None):
        s = self.settings
        self.source_id = source_id
        if source_id is not None:
            self.target_id = adjacent_objective(source_id, direction) if direction else source_id
            self._report(
                "target_locked", f"Source ID {source_id}; locked target ID {self.target_id}"
            )
        self._command("STOP OBJ", "OK STOP OBJ")
        self._command("OBJ DISABLE", "OK OBJ DISABLED")
        self._settle(s.release_settle_seconds)
        first = self._visible_objective("identify", self.source_id)
        self.source_id = first["id"]
        for _ in range(s.verify_frames - 1):
            self._visible_objective("identify_verify", self.source_id)
        self.target_id = (
            adjacent_objective(self.source_id, direction) if direction else self.source_id
        )
        description = (
            f"{COLORS[self.source_id]} ID {self.source_id} -> "
            f"{COLORS[self.target_id]} ID {self.target_id}"
            if direction
            else f"Calibrate current {COLORS[self.source_id]} ID {self.source_id}"
        )
        self._report("identified", description, first)
        # Keep the former full-step-equivalent speed: 200 -> 800 pulses/s.
        # Configure this once, and leave it unchanged throughout calibration.
        pulse_speed = s.speed * MOTION_MICROSTEP
        self._command(f"OBJ SPEED {pulse_speed}", "OK OBJ SPEED")
        self._command("MICRO 4", "OK MICRO 4")
        self.microstep = MOTION_MICROSTEP
        if direction:
            self._command("OBJ ENABLE", "OK OBJ ENABLED")
            self._settle(s.enable_settle_seconds)
            if self.before_move:
                self.before_move()
            self._check()
            steps = direction * s.switch_steps
            pulses = full_steps_to_pulses(steps)
            self._report(
                "move",
                f"{pulses:+d} pulses at MICRO 4 ({steps:+d} full steps), {pulse_speed} pulses/s",
            )
            self.commanded_steps = steps
            self._command(
                f"OBJ MOVE {pulses}",
                "DONE OBJ POS ",
                timeout=max(5.0, abs(pulses) / pulse_speed + 3.0),
            )
        self._command("OBJ DISABLE", "OK OBJ DISABLED")
        self._settle(s.release_settle_seconds)
        # Correct only after the destination ID is confirmed. Missing markers
        # stop movement; no motor-count address substitutes for a visual pose.
        verified = self._calibrate(self._disabled_measurements())
        final = verified[-1]
        return SwitchResult(
            self.source_id,
            self.target_id,
            final["dx_px"],
            final["dy_px"],
            self.microstep,
            path,
            max(abs(m["dx_px"]) for m in verified),
            s.tolerance_px,
            calibration_only=direction == 0,
        )
