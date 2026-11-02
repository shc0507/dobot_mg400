"""Bounded, image-based autofocus for the Toupcam and focus A4988 motor.

The focus positions reported by this module are relative to the position where
the autofocus run started.  Zero is therefore the starting position, not an
absolute or homed motor coordinate.
"""

from __future__ import annotations

import argparse
import csv
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from toupcam_camera import MicroscopeCamera

from .paths import private_open, run_path
from .side_capture import SideCaptureCancelled


class AutofocusCancelled(RuntimeError):
    """Raised when the operator stops an autofocus run."""


@dataclass(frozen=True)
class FocusMeasurement:
    """One focus score at a commanded position relative to the start."""

    position: int
    score: float
    stage: str
    frame_id: int | None = None
    captured_at: float | None = None
    score_noise: float = 0.0


def focus_score(frame: np.ndarray, roi_fraction: float = 1.00) -> float:
    """Score sharpness over the selected image area (the full frame by default)."""
    if frame is None or frame.size == 0:
        raise ValueError("Cannot score an empty frame")
    if not 0 < roi_fraction <= 1:
        raise ValueError("roi_fraction must be greater than 0 and at most 1")

    height, width = frame.shape[:2]
    roi_width = max(32, int(width * roi_fraction))
    roi_height = max(32, int(height * roi_fraction))
    x1 = max(0, (width - roi_width) // 2)
    y1 = max(0, (height - roi_height) // 2)
    roi = frame[y1 : y1 + roi_height, x1 : x1 + roi_width]

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    # Limiting the working size makes scoring fast and keeps scores comparable.
    max_dimension = max(gray.shape)
    if max_dimension > 1200:
        scale = 1200.0 / max_dimension
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class AutoFocuser:
    """Scan a bounded relative Z range and return to the sharpest position."""

    def __init__(
        self,
        camera: MicroscopeCamera,
        motor,
        *,
        speed: int = 800,
        settle_seconds: float = 0.15,
        frames_per_position: int = 2,
        precision_settle_seconds: float = 0.35,
        precision_frames_per_position: int = 3,
        frame_interval: float = 0.0,
        roi_fraction: float = 1.00,
        backlash_steps: int = 200,
        progress=None,
        cancel_event: threading.Event | None = None,
    ):
        if speed <= 0:
            raise ValueError("speed must be positive")
        if settle_seconds < 0 or precision_settle_seconds < 0 or frame_interval < 0:
            raise ValueError("wait times cannot be negative")
        if frames_per_position < 1:
            raise ValueError("frames_per_position must be at least 1")
        if precision_frames_per_position < 1:
            raise ValueError("precision_frames_per_position must be at least 1")
        if backlash_steps < 0:
            raise ValueError("backlash_steps cannot be negative")

        self.camera = camera
        self.motor = motor
        self.speed = int(speed)
        self.settle_seconds = float(settle_seconds)
        self.frames_per_position = int(frames_per_position)
        self.precision_settle_seconds = float(precision_settle_seconds)
        self.precision_frames_per_position = int(precision_frames_per_position)
        self.frame_interval = float(frame_interval)
        self.roi_fraction = float(roi_fraction)
        self.backlash_steps = int(backlash_steps)
        self.progress = progress
        self.cancel_event = cancel_event or threading.Event()
        self.position = 0
        self.last_frame_id = None
        self.measurements: list[FocusMeasurement] = []
        self._stationary_since = 0.0

    def _wait(self, seconds: float) -> None:
        if self.cancel_event.wait(max(0.0, seconds)):
            self.motor.focus_stop()
            raise AutofocusCancelled("Autofocus was stopped")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            self.motor.focus_stop()
            raise AutofocusCancelled("Autofocus was stopped")

    def _focus_command(self, method, *args):
        try:
            return method(*args, cancel_event=self.cancel_event)
        except SideCaptureCancelled as error:
            raise AutofocusCancelled("Autofocus was stopped") from error

    def _move_to(self, target: int, *, precision: bool = False) -> None:
        self._check_cancelled()
        steps = int(target) - self.position
        if steps == 0:
            return
        self._focus_command(self.motor.focus_move, steps)
        # The controller awaits DONE on current firmware, or the computed
        # travel duration on legacy firmware. Keep the original settling delay.
        settle_seconds = self.precision_settle_seconds if precision else self.settle_seconds
        self._wait(settle_seconds)
        self.position = int(target)
        self._stationary_since = time.time()

    def _measure(self, stage: str, *, precision: bool = False) -> FocusMeasurement:
        scores = []
        frame_id = None
        captured_at = None
        sample_count = self.precision_frames_per_position if precision else self.frames_per_position
        for sample_index in range(sample_count):
            # Movement settling already precedes the first sample.  For later
            # samples, get_frame_packet(after_id=...) itself waits for a new
            # frame, so an additional delay is only needed when explicitly
            # requested by the operator.
            if sample_index > 0 and self.frame_interval > 0:
                self._wait(self.frame_interval)
            deadline = time.monotonic() + 2.0
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("No fresh, settled camera frame is available")
                packet = self.camera.get_frame_packet(
                    after_id=self.last_frame_id,
                    wait=True,
                    timeout=min(0.25, remaining),
                    raw=True,
                    copy=False,
                )
                if packet is None:
                    continue
                frame_id, captured_at, frame = packet
                self.last_frame_id = frame_id
                # A new ID relative to the last measurement can still belong
                # to a cached frame acquired during the intervening move.
                if captured_at >= self._stationary_since:
                    break
            scores.append(focus_score(frame, self.roi_fraction))

        median_score = float(np.median(scores))
        return self._record_measurement(
            position=self.position,
            score=median_score,
            stage=stage,
            frame_id=frame_id,
            captured_at=captured_at,
            score_noise=1.4826 * float(np.median(np.abs(np.asarray(scores) - median_score))),
        )

    def _record_measurement(
        self,
        *,
        position: int,
        score: float,
        stage: str,
        frame_id=None,
        captured_at=None,
        score_noise=0.0,
    ) -> FocusMeasurement:
        measurement = FocusMeasurement(
            position=position,
            score=score,
            stage=stage,
            frame_id=frame_id,
            captured_at=captured_at,
            score_noise=score_noise,
        )
        self.measurements.append(measurement)
        if self.progress is not None:
            self.progress(measurement)
        return measurement

    @staticmethod
    def _positions(start: int, stop: int, step: int) -> list[int]:
        positions = list(range(start, stop + 1, step))
        if not positions or positions[-1] != stop:
            positions.append(stop)
        return positions

    def _scan(
        self, start: int, stop: int, step: int, stage: str, *, precision: bool = False
    ) -> list[FocusMeasurement]:
        results = []
        for target in self._positions(start, stop, step):
            self._check_cancelled()
            self._move_to(target, precision=precision)
            results.append(self._measure(stage, precision=precision))
        return results

    def _continuous_scan(self, start: int, stop: int, stage: str) -> list[FocusMeasurement]:
        """Move without intermediate stops and score every new camera frame."""
        self._move_to(start)
        results = [self._measure(stage)]
        distance = stop - start
        if distance == 0:
            return results

        duration = abs(distance) / self.speed
        ticket = self._focus_command(self.motor.focus_move_async, distance)
        motion_started_at = ticket.started_wall
        motion_started_monotonic = ticket.started

        while True:
            self._check_cancelled()
            elapsed = time.monotonic() - motion_started_monotonic
            if elapsed >= duration:
                break
            packet = self.camera.get_frame_packet(
                after_id=self.last_frame_id,
                wait=True,
                timeout=min(0.5, max(0.01, duration - elapsed)),
                raw=True,
                copy=False,
            )
            if packet is None:
                continue
            frame_id, captured_at, frame = packet
            self.last_frame_id = frame_id
            fraction = np.clip((captured_at - motion_started_at) / duration, 0.0, 1.0)
            estimated_position = int(round(start + distance * fraction))
            results.append(
                self._record_measurement(
                    position=estimated_position,
                    score=focus_score(frame, self.roi_fraction),
                    stage=stage,
                    frame_id=frame_id,
                    captured_at=captured_at,
                )
            )

        self._focus_command(self.motor.wait_focus_done, ticket)
        self._wait(self.settle_seconds)
        self.position = stop
        self._stationary_since = time.time()
        results.append(self._measure(stage))
        return results

    @staticmethod
    def _coarse_candidate(samples: list[FocusMeasurement]) -> FocusMeasurement:
        """Choose a supported coarse peak rather than one isolated high frame."""
        if len(samples) < 3:
            return max(samples, key=lambda item: item.score)
        smoothed = [
            float(np.median([item.score for item in samples[max(0, index - 1) : index + 2]]))
            for index in range(len(samples))
        ]
        # The neighboring frame support matters more than a single maximum.
        index = max(
            range(len(samples)),
            key=lambda i: (smoothed[i], samples[i].score),
        )
        return samples[index]

    def _adaptive_fine_scan(
        self,
        *,
        start: int,
        stop: int,
        predicted_peak: int,
        max_step: int,
        min_step: int,
        lower_limit: int,
        stage: str | None = None,
        stage_prefix: str = "",
    ) -> list[FocusMeasurement]:
        """Scan from right to left once, reducing step size near the peak.

        The scan shrinks its step without reversing direction. Three settled
        samples must fall below the peak by more than measured frame noise
        before it ends early. A rising peak at the window edge can extend the
        scan within the caller's original travel limits.
        """
        results = []
        target = int(start)
        boundary = int(stop)
        step = int(max_step)
        best = None
        below_peak = 0
        while True:
            self._move_to(target, precision=True)
            label = stage or (stage_prefix + ("ultrafine" if step == min_step else "fine"))
            measurement = self._measure(label, precision=True)
            results.append(measurement)
            if best is None or measurement.score > best.score:
                best = measurement
                below_peak = 0
            else:
                drop = max(
                    abs(best.score) * 0.015,
                    3.0 * max(best.score_noise, measurement.score_noise),
                    1e-9,
                )
                if measurement.score < best.score - drop:
                    below_peak += 1
                else:
                    below_peak = 0

            # Do not stop on the first slightly lower/noisy value. Require
            # support on both sides and a spatially separated declining tail.
            if (
                step == min_step
                and below_peak >= 3
                and start > best.position
                and best.position - target >= 3 * min_step
            ):
                break

            # Broad, statistically flat peaks need no long minimum-step sweep.
            # Once the dense zone has been crossed, five agreeing samples near
            # the best measured score are sufficient to stop on a plateau.
            if step == min_step and len(results) >= 5 and target <= predicted_peak - max_step:
                tail = results[-5:]
                tolerance = max(
                    abs(best.score) * 0.015,
                    3.0 * max(item.score_noise for item in tail + [best]),
                    1e-9,
                )
                if all(abs(item.score - best.score) <= tolerance for item in tail):
                    break

            if target <= boundary:
                # Coarse timing/backlash may offset the predicted position.
                # Continue only if the latest samples show a real rising edge,
                # rather than starting another pass or chasing a flat plateau.
                rising = (
                    len(results) >= 3
                    and best.position == target
                    and measurement.score - results[-3].score
                    > max(
                        abs(measurement.score) * 0.015,
                        3.0 * max(measurement.score_noise, results[-3].score_noise),
                        1e-9,
                    )
                )
                if not rising or boundary <= lower_limit:
                    break
                boundary = max(lower_limit, boundary - max_step)
                step = min_step

            distance_to_prediction = target - predicted_peak
            if distance_to_prediction <= max_step:
                step = min_step
            elif distance_to_prediction <= 3 * max_step:
                step = min(step, max(min_step, max_step // 2))

            # An unexpected, noise-resolved turn also warrants small steps
            # when the time-estimated coarse peak was displaced.
            if len(results) >= 3:
                first, middle, last = results[-3:]
                turn_threshold = max(
                    abs(middle.score) * 0.015,
                    3.0 * max(first.score_noise, middle.score_noise, last.score_noise),
                    1e-9,
                )
                if (
                    middle.score - first.score > turn_threshold
                    and middle.score - last.score > turn_threshold
                ):
                    step = min_step

            # Land on the dense zone boundary before taking a minimum step;
            # a larger stride must never jump across the predicted peak.
            next_target = max(boundary, target - step)
            dense_edge = predicted_peak + max_step
            if target > dense_edge > next_target:
                next_target = dense_edge
            target = next_target
        return results

    @staticmethod
    def _fine_candidate(samples: list[FocusMeasurement]) -> FocusMeasurement:
        """Select the highest measured settled score, including on plateaus."""
        return max(samples, key=lambda item: item.score)

    def _land_and_verify(
        self,
        best: FocusMeasurement,
        *,
        lower_limit: int,
        upper_limit: int,
        correction_radius: int,
        min_step: int,
        stage_prefix: str = "",
    ) -> FocusMeasurement:
        """Return to the measured maximum; final score is diagnostic only.

        Keep the existing private-call signature for callers. A changed final
        score does not trigger a correction scan or a score-matching error.
        Camera acquisition and motion failures still propagate normally.
        """
        self._return_to_best(best.position, lower_limit)
        return self._measure(stage_prefix + "final", precision=True)

    def _return_to_best(self, target: int, lower_limit: int) -> None:
        """Approach the final position in the positive direction when possible."""
        approach = max(lower_limit, target - self.backlash_steps)
        if approach < target and self.position > approach:
            self._move_to(approach, precision=True)
        self._move_to(target, precision=True)

    def _disable_objective_for_focus(self) -> None:
        """Release the objective turret before collecting focus scores."""
        objective_stop = getattr(self.motor, "objective_stop", None)
        if callable(objective_stop):
            objective_stop()
        objective_disable = getattr(self.motor, "objective_disable", None)
        if callable(objective_disable):
            objective_disable()
            self._wait(self.settle_seconds)

    def run(
        self,
        *,
        half_range: int,
        coarse_step: int = 1000,
        fine_step: int = 100,
        ultrafine_step: int = 10,
        lock_exposure: bool = True,
    ) -> FocusMeasurement:
        """Run one continuous coarse pass and one adaptive fine pass."""
        if fine_step >= coarse_step:
            raise ValueError("fine_step must be smaller than the fine search radius")
        if ultrafine_step >= fine_step:
            raise ValueError("ultrafine_step must be smaller than fine_step")
        return self._run_scan(
            half_range=half_range,
            coarse_step=coarse_step,
            fine_step=fine_step,
            ultrafine_step=ultrafine_step,
            lock_exposure=lock_exposure,
        )

    def _run_scan(
        self,
        *,
        half_range: int,
        coarse_step: int,
        fine_step: int,
        ultrafine_step: int,
        lock_exposure: bool,
        stage_prefix: str = "",
    ) -> FocusMeasurement:
        """Shared full/local pipeline, including final landing verification."""
        if half_range <= 0:
            raise ValueError("half_range must be positive")
        if coarse_step <= 0 or fine_step <= 0 or ultrafine_step <= 0:
            raise ValueError("scan steps must be positive")
        if fine_step > coarse_step:
            raise ValueError("fine_step must not exceed the fine search radius")
        if ultrafine_step > fine_step:
            raise ValueError("ultrafine_step must not exceed fine_step")
        if coarse_step > 2 * half_range:
            raise ValueError("fine search radius is larger than the full scan range")
        if not self.camera.is_open:
            raise RuntimeError("Camera is not open")
        if not self.motor.connected:
            raise RuntimeError("Motor controller is not connected")

        self.position = 0
        self.last_frame_id = None
        self._stationary_since = time.time()
        self.measurements.clear()
        stabilization_before = self.camera.stabilization_enabled
        auto_exposure_before = None

        try:
            self.camera.set_stabilization(False)
            if lock_exposure:
                # Let automatic exposure settle before freezing it for the scan.
                time.sleep(0.7)
                try:
                    auto_exposure_before = bool(self.camera.sdk_camera.get_AutoExpoEnable())
                except Exception:
                    pass
                self.camera.set_auto_exposure(False)

            # Focus measurements must be taken with the objective turret
            # mechanically released.  Keep this guarantee inside AutoFocuser
            # so every caller, including the standalone GUI action and light
            # objective calibration, follows the same ordering.
            self._disable_objective_for_focus()

            self.motor.focus_speed(self.speed)
            self.motor.focus_enable()

            coarse = self._continuous_scan(-half_range, half_range, stage_prefix + "coarse")
            coarse_best = self._coarse_candidate(coarse)

            fine_start = max(-half_range, coarse_best.position - coarse_step)
            fine_stop = min(half_range, coarse_best.position + coarse_step)
            # Coarse motion ends on the right. Start on the nearer side and
            # refine while travelling left, without an intervening rescan.
            fine = self._adaptive_fine_scan(
                start=fine_stop,
                stop=fine_start,
                predicted_peak=coarse_best.position,
                max_step=fine_step,
                min_step=ultrafine_step,
                lower_limit=-half_range,
                stage_prefix=stage_prefix,
            )
            best = self._fine_candidate(fine)
            return self._land_and_verify(
                best,
                lower_limit=-half_range,
                upper_limit=half_range,
                correction_radius=fine_step,
                min_step=ultrafine_step,
                stage_prefix=stage_prefix,
            )
        except BaseException:
            self.motor.focus_stop()
            raise
        finally:
            self.camera.set_stabilization(stabilization_before)
            if lock_exposure and auto_exposure_before is not None:
                self.camera.set_auto_exposure(auto_exposure_before)

    def run_local(
        self,
        *,
        half_range: int = 1500,
        step: int = 100,
        fine_radius: int | None = None,
        min_step: int | None = None,
        lock_exposure: bool = True,
    ) -> FocusMeasurement:
        """Use the full autofocus algorithm inside a small local travel range.

        ``step`` is the maximum settled fine stride, not a fixed stop interval
        across the whole range. By default refine +/-2*step around the supported
        continuous-scan peak and reduce to min(10, step) near that peak.
        Existing callers, including Light calibration, use this shared path.
        """
        if half_range <= 0:
            raise ValueError("half_range must be positive")
        if step <= 0:
            raise ValueError("step must be positive")
        if step > 2 * half_range:
            raise ValueError("step is larger than the full local scan range")
        radius = 2 * step if fine_radius is None else fine_radius
        minimum = min(10, step) if min_step is None else min_step
        if radius < step:
            raise ValueError("local fine radius must be at least step")
        if minimum <= 0 or minimum > step:
            raise ValueError("local minimum step must be positive and at most step")
        # Very small local ranges still support step=1 without requiring an
        # impossible sub-pulse stride. All moves retain the local +/- limits.
        radius = min(int(radius), 2 * int(half_range))
        maximum = min(int(step), radius)
        return self._run_scan(
            half_range=int(half_range),
            coarse_step=radius,
            fine_step=maximum,
            ultrafine_step=min(int(minimum), maximum),
            lock_exposure=lock_exposure,
            stage_prefix="local_",
        )


def save_measurements(path: str | Path, measurements: list[FocusMeasurement]) -> Path:
    """Save the focus curve so scan parameters can be tuned from real data."""
    path = run_path(path)
    with private_open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            (
                "stage",
                "relative_position",
                "focus_score",
                "frame_id",
                "captured_at",
                "score_noise",
            )
        )
        for item in measurements:
            writer.writerow(
                (
                    item.stage,
                    item.position,
                    item.score,
                    item.frame_id,
                    item.captured_at,
                    item.score_noise,
                )
            )
    return path


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuous coarse and single-pass adaptive fine autofocus."
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("MG400_MICROSCOPE_SERIAL"),
        help="Arduino /dev/serial/by-id/ path",
    )
    parser.add_argument(
        "--half-range",
        required=True,
        type=_positive_int,
        help="safe travel allowed on each side of the starting point, in pulses",
    )
    parser.add_argument(
        "--fine-radius",
        "--coarse-step",
        dest="coarse_step",
        type=_positive_int,
        default=1000,
        help="radius searched around the continuous coarse-scan peak",
    )
    parser.add_argument(
        "--fine-step",
        type=_positive_int,
        default=100,
        help="maximum step used in the single adaptive fine pass",
    )
    parser.add_argument(
        "--ultrafine-step",
        type=_positive_int,
        default=10,
        help="minimum step near the peak in the same fine pass",
    )
    parser.add_argument("--speed", type=_positive_int, default=800)
    parser.add_argument("--settle", type=float, default=0.15)
    parser.add_argument("--frames", type=_positive_int, default=2)
    parser.add_argument("--precision-settle", type=float, default=0.35)
    parser.add_argument("--precision-frames", type=_positive_int, default=3)
    parser.add_argument("--roi", type=float, default=1.00)
    parser.add_argument("--backlash", type=int, default=200)
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--output", default="autofocus.csv")
    args = parser.parse_args()

    from .motors import BAUDRATE, MotorController

    controller = MotorController()
    camera = MicroscopeCamera(
        camera_index=args.camera_index,
        stabilization=False,
    )
    focuser = None

    def report(item: FocusMeasurement) -> None:
        print(f"{item.stage:6s}  position={item.position:7d}  score={item.score:12.3f}")

    try:
        controller.connect(args.port, BAUDRATE)
        camera.open()
        focuser = AutoFocuser(
            camera,
            controller,
            speed=args.speed,
            settle_seconds=args.settle,
            frames_per_position=args.frames,
            precision_settle_seconds=args.precision_settle,
            precision_frames_per_position=args.precision_frames,
            roi_fraction=args.roi,
            backlash_steps=args.backlash,
            progress=report,
        )
        best = focuser.run(
            half_range=args.half_range,
            coarse_step=args.coarse_step,
            fine_step=args.fine_step,
            ultrafine_step=args.ultrafine_step,
        )
        output = save_measurements(args.output, focuser.measurements)
        print(f"Best focus: relative position {best.position}, score {best.score:.3f}")
        print(f"Focus curve saved to: {output.resolve()}")
    except KeyboardInterrupt:
        print("Autofocus cancelled.")
    finally:
        if controller.connected:
            try:
                controller.focus_stop()
                controller.focus_disable()
                time.sleep(0.05)
            except Exception:
                pass
        controller.disconnect()
        camera.close()


if __name__ == "__main__":
    main()
