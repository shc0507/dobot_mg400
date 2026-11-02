"""Fast objective-turret calibration from a saved, centered reference view.

The current camera frame and the saved reference have the same pixel size.
When the current objective has higher magnification, its complete frame is
resized by the known magnification ratio and searched as one continuous view
inside the saved reference.  No manually chosen image block is used.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from autofocus import AutoFocuser, AutofocusCancelled, FocusMeasurement
from camera import MicroscopeCamera
from objective_calibration import measure_image_displacement


class LightObjectiveCalibrationCancelled(RuntimeError):
    """Raised when the operator stops light objective calibration."""


@dataclass(frozen=True)
class PartialViewMatch:
    """Location of the complete current view inside the saved reference."""

    score: float
    second_score: float
    uniqueness: float
    center_dx: float
    center_dy: float
    center_error: float
    overlap: float
    valid: bool


@dataclass(frozen=True)
class LightObjectiveMeasurement:
    """One objective position and its full-frame partial-view match."""

    stage: str
    position_units: int
    microstep: int
    frame_id: int
    score: float
    second_score: float
    uniqueness: float
    center_dx: float
    center_dy: float
    center_error: float
    overlap: float
    valid: bool


@dataclass(frozen=True)
class LightObjectiveReleaseMeasurement:
    """One enabled-to-disabled release measurement in camera pixels."""

    stage: str
    iteration: int
    position_units: int
    microstep: int
    enabled_frame_id: int
    disabled_frame_id: int
    dx: float
    dy: float
    displacement: float
    confidence: float
    overlap: float
    valid: bool
    endpoint_error: float = float("nan")


@dataclass(frozen=True)
class LightObjectiveDiagnostic:
    stage: str
    message: str


@dataclass(frozen=True)
class LightObjectiveRegionUpdate:
    """Robust coarse-match region selected after rejecting isolated peaks."""

    lower_position_units: int
    upper_position_units: int
    peak_position_units: int
    region_cutoff: float
    neighbor_support_fraction: float
    rejected_isolated_positions: tuple[int, ...]


@dataclass(frozen=True)
class LightObjectiveReturnTrace:
    """Target-frame similarity samples collected during visual return."""

    stage: str
    target_position_units: int
    samples: tuple[tuple[int, float], ...]
    rejected_isolated_positions: tuple[int, ...]
    accepted_position_units: int | None


@dataclass(frozen=True)
class LightObjectiveResult:
    position_units: int
    final_score: float
    final_center_dx: float
    final_center_dy: float
    final_center_error: float
    stable: bool
    coarse_region_units: tuple[int, int]
    coarse_microstep: int
    fine_microstep: int
    autofocus_result: FocusMeasurement
    median_release_displacement: float
    neighbor_endpoint_separation: float
    verification_count: int


def _working_gray(frame: np.ndarray, max_dimension: int) -> tuple[np.ndarray, float]:
    if frame is None or frame.size == 0:
        raise ValueError("Cannot match an empty image")
    if frame.ndim == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    scale = min(1.0, float(max_dimension) / max(gray.shape))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA,
        )
    gray = gray.astype(np.float32)
    mean, standard_deviation = cv2.meanStdDev(gray)
    deviation = max(1.0, float(standard_deviation[0, 0]))
    gray = (gray - float(mean[0, 0])) / deviation
    return gray, scale


def _second_spatial_peak(
    score_map: np.ndarray,
    best_location: tuple[int, int],
    exclusion_radius: int,
) -> float:
    """Return the best score outside the winning peak's neighborhood."""
    if score_map.size <= 1:
        return -1.0
    masked = score_map.copy()
    x, y = best_location
    radius = max(2, int(exclusion_radius))
    x1 = max(0, x - radius)
    x2 = min(masked.shape[1], x + radius + 1)
    y1 = max(0, y - radius)
    y2 = min(masked.shape[0], y + radius + 1)
    masked[y1:y2, x1:x2] = -1.0
    return float(np.max(masked))


def match_known_magnification_view(
    reference_frame: np.ndarray,
    current_frame: np.ndarray,
    *,
    reference_magnification: float,
    current_magnification: float,
    minimum_score: float = 0.25,
    minimum_uniqueness: float = 0.03,
    minimum_overlap: float = 0.50,
    max_dimension: int = 384,
) -> PartialViewMatch:
    """Match the whole current frame to a centered reference at known scale.

    ``current_magnification / reference_magnification`` is fixed by the two
    objectives; this function searches only image x/y.  Ratios below one are
    unsupported because the current view then contains scene outside a single
    saved reference frame.
    """
    if reference_magnification <= 0 or current_magnification <= 0:
        raise ValueError("Objective magnifications must be positive")
    ratio = float(current_magnification / reference_magnification)
    if ratio < 1.0 - 1e-6:
        raise ValueError(
            "The current magnification is lower than the saved-reference "
            "magnification; one reference image cannot cover the wider view"
        )
    if max_dimension < 64:
        raise ValueError("max_dimension must be at least 64")

    reference, work_scale = _working_gray(reference_frame, max_dimension)
    current, current_scale = _working_gray(current_frame, max_dimension)
    if reference.shape != current.shape or not np.isclose(
        work_scale, current_scale, rtol=0.02
    ):
        current = cv2.resize(
            current,
            (reference.shape[1], reference.shape[0]),
            interpolation=cv2.INTER_AREA,
        )

    if ratio <= 1.02:
        # Equal magnification: full-frame phase correlation directly finds
        # translation while naturally using every image pixel.
        dx, dy, _distance, confidence, overlap = measure_image_displacement(
            reference,
            current,
            roi_fraction=1.0,
        )
        center_dx = float(dx / work_scale)
        center_dy = float(dy / work_scale)
        score = float(confidence)
        second_score = 0.0
        uniqueness = score
        valid = score >= minimum_score and overlap >= minimum_overlap
    else:
        # Higher current magnification: the complete current frame represents
        # one smaller field of view inside the reference.  Shrink the complete
        # current image by the known ratio and slide it over all reference x/y.
        template_width = max(32, int(round(current.shape[1] / ratio)))
        template_height = max(32, int(round(current.shape[0] / ratio)))
        if (
            template_width > reference.shape[1]
            or template_height > reference.shape[0]
        ):
            raise ValueError("Known magnification ratio produced an invalid view")
        template = cv2.resize(
            current,
            (template_width, template_height),
            interpolation=cv2.INTER_AREA,
        )
        score_map = cv2.matchTemplate(
            reference,
            template,
            cv2.TM_CCOEFF_NORMED,
        )
        _minimum, score, _minimum_location, best_location = cv2.minMaxLoc(
            score_map
        )
        second_score = _second_spatial_peak(
            score_map,
            best_location,
            exclusion_radius=max(4, min(template.shape) // 6),
        )
        uniqueness = float(score - second_score)
        match_center_x = best_location[0] + 0.5 * template_width
        match_center_y = best_location[1] + 0.5 * template_height
        center_dx = float(
            (match_center_x - 0.5 * reference.shape[1]) / work_scale
        )
        center_dy = float(
            (match_center_y - 0.5 * reference.shape[0]) / work_scale
        )
        overlap = 1.0
        valid = (
            score >= minimum_score
            and uniqueness >= minimum_uniqueness
            and overlap >= minimum_overlap
        )

    return PartialViewMatch(
        score=float(score),
        second_score=float(second_score),
        uniqueness=float(uniqueness),
        center_dx=center_dx,
        center_dy=center_dy,
        center_error=float(np.hypot(center_dx, center_dy)),
        overlap=float(overlap),
        valid=bool(valid),
    )


class LightObjectiveCalibrator:
    """Center a known objective view with a short scan and autofocus."""

    MICROSTEP_MODES = (1, 2, 4, 8, 16)

    def __init__(
        self,
        camera: MicroscopeCamera,
        motor,
        *,
        reference_frame: np.ndarray,
        reference_magnification: float = 10.0,
        current_magnification: float = 10.0,
        speed: int = 200,
        move_settle_seconds: float = 0.50,
        minimum_score: float = 0.25,
        minimum_uniqueness: float = 0.03,
        minimum_overlap: float = 0.50,
        center_tolerance_px: float = 5.0,
        region_fraction: float = 0.80,
        neighbor_support_fraction: float = 0.60,
        release_threshold_px: float = 10.0,
        neighbor_consistency_px: float = 2.0,
        release_min_confidence: float = 0.10,
        release_max_iterations: int = 8,
        verification_repeats: int = 3,
        progress=None,
        focus_progress=None,
        before_move=None,
        cancel_event: threading.Event | None = None,
    ):
        if reference_frame is None or reference_frame.size == 0:
            raise ValueError("A non-empty calibrated reference image is required")
        if reference_magnification <= 0 or current_magnification <= 0:
            raise ValueError("Objective magnifications must be positive")
        if speed <= 0 or move_settle_seconds < 0:
            raise ValueError("Motor speed must be positive and settle nonnegative")
        if not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score must be between 0 and 1")
        if not 0 <= minimum_uniqueness <= 1:
            raise ValueError("minimum_uniqueness must be between 0 and 1")
        if not 0 < minimum_overlap <= 1:
            raise ValueError("minimum_overlap must be greater than 0 and at most 1")
        if center_tolerance_px <= 0:
            raise ValueError("center_tolerance_px must be positive")
        if not 0 < region_fraction <= 1:
            raise ValueError("region_fraction must be greater than 0 and at most 1")
        if not 0 < neighbor_support_fraction <= 1:
            raise ValueError(
                "neighbor_support_fraction must be greater than 0 and at most 1"
            )
        if release_threshold_px <= 0 or neighbor_consistency_px <= 0:
            raise ValueError("release and neighbor thresholds must be positive")
        if not 0 <= release_min_confidence <= 1:
            raise ValueError("release_min_confidence must be between 0 and 1")
        if release_max_iterations < 1 or verification_repeats < 1:
            raise ValueError("release iterations and verification repeats must be positive")

        self.camera = camera
        self.motor = motor
        self.reference_frame = reference_frame.copy()
        self.reference_magnification = float(reference_magnification)
        self.current_magnification = float(current_magnification)
        self.speed = int(speed)
        self.move_settle_seconds = float(move_settle_seconds)
        self.minimum_score = float(minimum_score)
        self.minimum_uniqueness = float(minimum_uniqueness)
        self.minimum_overlap = float(minimum_overlap)
        self.center_tolerance_px = float(center_tolerance_px)
        self.region_fraction = float(region_fraction)
        self.neighbor_support_fraction = float(neighbor_support_fraction)
        self.release_threshold_px = float(release_threshold_px)
        self.neighbor_consistency_px = float(neighbor_consistency_px)
        self.release_min_confidence = float(release_min_confidence)
        self.release_max_iterations = int(release_max_iterations)
        self.verification_repeats = int(verification_repeats)
        self.progress = progress
        self.focus_progress = focus_progress
        self.before_move = before_move
        self.cancel_event = cancel_event or threading.Event()
        self.current_microstep: int | None = None
        self.position_units = 0
        self.last_frame_id = None
        self.measurements: list[
            LightObjectiveMeasurement | LightObjectiveReleaseMeasurement
        ] = []
        self.coarse_frames: dict[int, np.ndarray] = {}

    @staticmethod
    def _units_per_pulse(microstep: int) -> int:
        return 16 // int(microstep)

    def _publish_diagnostic(self, stage: str, message: str) -> None:
        if self.progress is not None:
            self.progress(LightObjectiveDiagnostic(stage, message))

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            try:
                self.motor.objective_stop()
                self.motor.focus_stop()
            finally:
                raise LightObjectiveCalibrationCancelled(
                    "Light objective calibration was stopped"
                )

    def _wait(self, seconds: float) -> None:
        if self.cancel_event.wait(max(0.0, seconds)):
            self._check_cancelled()

    def _set_microstep(self, microstep: int) -> None:
        microstep = int(microstep)
        if microstep not in self.MICROSTEP_MODES:
            raise ValueError("microstep must be 1, 2, 4, 8, or 16")
        if microstep != self.current_microstep:
            self.motor.set_microstep(microstep)
            self.current_microstep = microstep
            self._wait(0.10)

    def _move_pulses(self, pulses: int) -> None:
        pulses = int(pulses)
        if pulses == 0:
            return
        self._check_cancelled()
        predicted_units = (
            self.position_units
            + pulses * self._units_per_pulse(self.current_microstep)
        )
        self._publish_diagnostic(
            "move",
            f"Before OBJ MOVE {pulses:+d} at MICRO "
            f"{self.current_microstep}: commanded position "
            f"{self.position_units}/16 -> {predicted_units}/16 step.",
        )
        if self.before_move is not None:
            self.before_move()
            self._check_cancelled()
        self.motor.objective_move(pulses)
        self._wait(abs(pulses) / self.speed + self.move_settle_seconds)
        self.position_units += pulses * self._units_per_pulse(
            self.current_microstep
        )

    def _move_to_units(self, target_units: int) -> None:
        unit_size = self._units_per_pulse(self.current_microstep)
        difference = int(target_units) - self.position_units
        if difference % unit_size:
            raise RuntimeError(
                f"Position {target_units}/16 step cannot be reached at "
                f"MICRO {self.current_microstep}"
            )
        self._move_pulses(difference // unit_size)

    def _new_frame(self) -> tuple[int, np.ndarray]:
        self._check_cancelled()
        packet = self.camera.get_frame_packet(
            after_id=self.last_frame_id,
            wait=True,
            timeout=2.5,
            raw=True,
            copy=True,
        )
        if packet is None:
            raise RuntimeError("No new camera frame is available")
        frame_id, _captured_at, frame = packet
        self.last_frame_id = frame_id
        return int(frame_id), frame

    def _measure(
        self,
        stage: str,
        *,
        remember_in: dict[int, np.ndarray] | None = None,
    ) -> LightObjectiveMeasurement:
        frame_id, frame = self._new_frame()
        if remember_in is not None:
            # Keep only a compact normalized representation in memory.  At
            # 384px maximum dimension, dozens of frames remain inexpensive
            # and no temporary PNG files are written.
            remember_in[int(self.position_units)] = _working_gray(
                frame, 384
            )[0]
        match = match_known_magnification_view(
            self.reference_frame,
            frame,
            reference_magnification=self.reference_magnification,
            current_magnification=self.current_magnification,
            minimum_score=self.minimum_score,
            minimum_uniqueness=self.minimum_uniqueness,
            minimum_overlap=self.minimum_overlap,
        )
        item = LightObjectiveMeasurement(
            stage=stage,
            position_units=int(self.position_units),
            microstep=int(self.current_microstep),
            frame_id=frame_id,
            score=match.score,
            second_score=match.second_score,
            uniqueness=match.uniqueness,
            center_dx=match.center_dx,
            center_dy=match.center_dy,
            center_error=match.center_error,
            overlap=match.overlap,
            valid=match.valid,
        )
        self.measurements.append(item)
        if self.progress is not None:
            self.progress(item)
        return item

    def _scan_units(
        self,
        start: int,
        stop: int,
        step: int,
        stage: str,
        remember_in: dict[int, np.ndarray] | None = None,
    ) -> list[LightObjectiveMeasurement]:
        results = []
        positions = list(range(int(start), int(stop) + 1, int(step)))
        if not positions or positions[-1] != int(stop):
            positions.append(int(stop))
        for target in positions:
            self._check_cancelled()
            self._move_to_units(target)
            results.append(self._measure(stage, remember_in=remember_in))
        return results

    @staticmethod
    def _same_view_score(first: np.ndarray, second: np.ndarray) -> float:
        if first.shape != second.shape:
            second = cv2.resize(
                second,
                (first.shape[1], first.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
        denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
        if denominator <= 1e-12:
            return 0.0
        return float(np.clip(
            np.dot(first.reshape(-1), second.reshape(-1)) / denominator,
            -1.0,
            1.0,
        ))

    def _return_to_saved_view(
        self,
        *,
        saved_frames: dict[int, np.ndarray],
        target_position_units: int,
        lower_position_units: int,
        upper_position_units: int,
        stage: str,
    ) -> None:
        """Return by finding a supported peak in target-frame similarity."""
        if target_position_units not in saved_frames:
            raise RuntimeError(
                f"The target frame at {target_position_units}/16 step was not saved"
            )
        if not saved_frames:
            raise RuntimeError("No saved scan frames are available for visual return")
        target_frame = saved_frames[target_position_units]
        unit_size = self._units_per_pulse(self.current_microstep)
        span_pulses = int(np.ceil(
            abs(upper_position_units - lower_position_units) / unit_size
        ))
        # Permit a modest backlash margin below the original scan, without
        # allowing a missed peak to drive through the range repeatedly.
        max_reverse_pulses = max(8, span_pulses + max(4, span_pulses // 4))
        samples: list[tuple[int, float]] = []
        isolated_positions: set[int] = set()

        def publish_trace(accepted_position_units: int | None = None) -> None:
            if self.progress is not None:
                self.progress(LightObjectiveReturnTrace(
                    stage=stage,
                    target_position_units=int(target_position_units),
                    samples=tuple(samples),
                    rejected_isolated_positions=tuple(sorted(
                        isolated_positions
                    )),
                    accepted_position_units=accepted_position_units,
                ))

        for reverse_index in range(max_reverse_pulses + 1):
            self._check_cancelled()
            _frame_id, frame = self._new_frame()
            current_frame = _working_gray(frame, 384)[0]
            target_score = self._same_view_score(target_frame, current_frame)
            samples.append((int(self.position_units), float(target_score)))
            self._publish_diagnostic(
                stage,
                f"reverse sample {reverse_index}: commanded position="
                f"{self.position_units}/16 step, target frame="
                f"{target_position_units}/16, similarity={target_score:.3f}.",
            )

            # A peak is classified only after one neighboring sample exists on
            # each side.  This removes isolated single-position spikes.
            if len(samples) >= 3:
                before = samples[-3]
                candidate = samples[-2]
                after = samples[-1]
                candidate_position, candidate_score = candidate
                is_local_peak = (
                    candidate_score >= before[1]
                    and candidate_score >= after[1]
                )
                support_limit = max(
                    self.minimum_score,
                    candidate_score * self.neighbor_support_fraction,
                )
                has_neighbor_support = (
                    before[1] >= support_limit
                    or after[1] >= support_limit
                )
                filtered_score = float(np.median([
                    before[1], candidate_score, after[1]
                ]))

                if is_local_peak and not has_neighbor_support:
                    isolated_positions.add(candidate_position)
                    self._publish_diagnostic(
                        stage,
                        f"rejected isolated target-similarity peak at "
                        f"{candidate_position}/16 step: center="
                        f"{candidate_score:.3f}, neighbors="
                        f"{before[1]:.3f}/{after[1]:.3f}, required neighbor "
                        f"support={support_limit:.3f}.",
                    )

                if (
                    candidate_position <= target_position_units
                    and is_local_peak
                    and has_neighbor_support
                    and filtered_score >= self.minimum_score
                ):
                    publish_trace(candidate_position)
                    confirmed_position = int(self.position_units)
                    # The visual match is authoritative.  The confirming frame
                    # is one pulse beyond the supported peak and remains in its
                    # neighborhood; rebase away the backlash-affected count.
                    self.position_units = int(target_position_units)
                    self._publish_diagnostic(
                        stage,
                        f"accepted supported target-similarity peak at "
                        f"{candidate_position}/16 step (raw "
                        f"{candidate_score:.3f}, three-point median "
                        f"{filtered_score:.3f}); confirmation stopped at "
                        f"{confirmed_position}/16 step and visual position "
                        f"was rebased to {target_position_units}/16.",
                    )
                    return

            publish_trace()
            if reverse_index < max_reverse_pulses:
                self._move_pulses(-1)

        supported_scores = []
        for index in range(1, len(samples) - 1):
            before, candidate, after = samples[index - 1:index + 2]
            support_limit = max(
                self.minimum_score,
                candidate[1] * self.neighbor_support_fraction,
            )
            if (
                candidate[0] <= target_position_units
                and (before[1] >= support_limit or after[1] >= support_limit)
            ):
                supported_scores.append(float(np.median([
                    before[1], candidate[1], after[1]
                ])))
        best_supported = max(supported_scores, default=float("nan"))
        raise RuntimeError(
            f"Visual return could not find a neighbor-supported similarity "
            f"peak for the saved target frame at {target_position_units}/16 "
            f"step; best supported three-point score was "
            f"{best_supported:.3f}"
        )

    def _matching_region(
        self,
        coarse: list[LightObjectiveMeasurement],
        coarse_step_units: int,
    ) -> tuple[int, int, LightObjectiveMeasurement]:
        ordered = sorted(coarse, key=lambda item: item.position_units)
        usable = [item for item in ordered if item.valid]
        if not usable:
            raise RuntimeError(
                "No trustworthy partial-reference match was found in the "
                "light-calibration scan"
            )

        by_position = {item.position_units: item for item in ordered}
        supported: list[LightObjectiveMeasurement] = []
        isolated_positions: list[int] = []
        for item in usable:
            support_limit = max(
                self.minimum_score,
                item.score * self.neighbor_support_fraction,
            )
            neighbors = [
                by_position.get(item.position_units - coarse_step_units),
                by_position.get(item.position_units + coarse_step_units),
            ]
            has_support = any(
                neighbor is not None
                and neighbor.valid
                and neighbor.score >= support_limit
                for neighbor in neighbors
            )
            if has_support:
                supported.append(item)
            else:
                isolated_positions.append(item.position_units)

        if not supported:
            raise RuntimeError(
                "All valid coarse matches were isolated single-position "
                "peaks; no continuous matching region was found"
            )

        peak = max(supported, key=lambda item: item.score)
        region_cutoff = max(
            self.minimum_score,
            peak.score * self.region_fraction,
        )
        selected = sorted(
            (item for item in supported if item.score >= region_cutoff),
            key=lambda item: item.position_units,
        )
        groups: list[list[LightObjectiveMeasurement]] = []
        for item in selected:
            if (
                not groups
                or item.position_units - groups[-1][-1].position_units
                > coarse_step_units
            ):
                groups.append([item])
            else:
                groups[-1].append(item)
        peak_group = next(
            group for group in groups if peak in group
        )
        lower = peak_group[0].position_units
        upper = peak_group[-1].position_units
        if lower == upper:
            all_lower = min(item.position_units for item in coarse)
            all_upper = max(item.position_units for item in coarse)
            lower = max(all_lower, lower - coarse_step_units)
            upper = min(all_upper, upper + coarse_step_units)
        self._publish_diagnostic(
            "coarse region",
            f"Peak match={peak.score:.3f} at {peak.position_units}/16 "
            f"step; retained continuous region [{lower}, {upper}]/16 "
            f"step using cutoff {region_cutoff:.3f}. Rejected "
            f"{len(isolated_positions)} isolated high-score point(s).",
        )
        if self.progress is not None:
            self.progress(LightObjectiveRegionUpdate(
                lower_position_units=int(lower),
                upper_position_units=int(upper),
                peak_position_units=int(peak.position_units),
                region_cutoff=float(region_cutoff),
                neighbor_support_fraction=self.neighbor_support_fraction,
                rejected_isolated_positions=tuple(isolated_positions),
            ))
        return lower, upper, peak

    def _coarse_motor_axis(self) -> np.ndarray:
        """Estimate image motion per 1/16-step unit from the forward scan."""
        positions = sorted(self.coarse_frames)
        vectors = []
        for first_position, second_position in zip(positions, positions[1:]):
            delta_units = second_position - first_position
            if delta_units <= 0:
                continue
            try:
                dx, dy, distance, confidence, overlap = (
                    measure_image_displacement(
                        self.coarse_frames[first_position],
                        self.coarse_frames[second_position],
                        roi_fraction=1.0,
                    )
                )
            except (cv2.error, ValueError):
                continue
            if (
                confidence >= self.release_min_confidence
                and overlap >= self.minimum_overlap
                and distance >= 0.25
            ):
                vectors.append((dx / delta_units, dy / delta_units))
        if not vectors:
            raise RuntimeError(
                "The coarse light scan did not provide a trustworthy visual "
                "motor direction"
            )
        axis = np.median(np.asarray(vectors, dtype=np.float64), axis=0)
        magnitude = float(np.linalg.norm(axis))
        if magnitude <= 1e-4:
            raise RuntimeError("The estimated visual motor direction is zero")
        self._publish_diagnostic(
            "release axis",
            f"Forward coarse scan estimated image motion per 1/16-step "
            f"unit as dx={axis[0]:+.4f}px, dy={axis[1]:+.4f}px from "
            f"{len(vectors)} adjacent pair(s).",
        )
        return axis

    def _record_release(
        self,
        stage: str,
        iteration: int,
        enabled_frame_id: int,
        enabled_frame: np.ndarray,
        disabled_frame_id: int,
        disabled_frame: np.ndarray,
        *,
        endpoint_target: np.ndarray | None = None,
    ) -> tuple[LightObjectiveReleaseMeasurement, np.ndarray]:
        enabled_work, enabled_scale = _working_gray(enabled_frame, 384)
        disabled_work, disabled_scale = _working_gray(disabled_frame, 384)
        if not np.isclose(enabled_scale, disabled_scale):
            raise RuntimeError("Release frames were captured at different scales")
        dx_work, dy_work, distance_work, confidence, overlap = (
            measure_image_displacement(
                enabled_work,
                disabled_work,
                roi_fraction=1.0,
            )
        )
        scale = max(1e-9, float(enabled_scale))
        endpoint_error = float("nan")
        if endpoint_target is not None:
            _target_dx, _target_dy, target_distance, target_confidence, (
                target_overlap
            ) = measure_image_displacement(
                endpoint_target,
                disabled_work,
                roi_fraction=1.0,
            )
            if (
                target_confidence >= self.release_min_confidence
                and target_overlap >= self.minimum_overlap
            ):
                endpoint_error = float(target_distance / scale)
        item = LightObjectiveReleaseMeasurement(
            stage=stage,
            iteration=int(iteration),
            position_units=int(self.position_units),
            microstep=int(self.current_microstep),
            enabled_frame_id=int(enabled_frame_id),
            disabled_frame_id=int(disabled_frame_id),
            dx=float(dx_work / scale),
            dy=float(dy_work / scale),
            displacement=float(distance_work / scale),
            confidence=float(confidence),
            overlap=float(overlap),
            valid=bool(
                confidence >= self.release_min_confidence
                and overlap >= self.minimum_overlap
            ),
            endpoint_error=endpoint_error,
        )
        self.measurements.append(item)
        if self.progress is not None:
            self.progress(item)
        return item, disabled_work

    def _capture_release(
        self,
        stage: str,
        iteration: int,
        *,
        endpoint_target: np.ndarray | None = None,
    ) -> tuple[
        LightObjectiveReleaseMeasurement,
        np.ndarray,
        np.ndarray,
    ]:
        self.motor.objective_enable()
        self._wait(self.move_settle_seconds)
        enabled_frame_id, enabled_frame = self._new_frame()
        self.motor.objective_disable()
        self._wait(self.move_settle_seconds)
        disabled_frame_id, disabled_frame = self._new_frame()
        item, disabled_work = self._record_release(
            stage,
            iteration,
            enabled_frame_id,
            enabled_frame,
            disabled_frame_id,
            disabled_frame,
            endpoint_target=endpoint_target,
        )
        return item, disabled_frame, disabled_work

    def _alignment_command(
        self,
        dx_work: float,
        dy_work: float,
        motor_axis: np.ndarray,
    ) -> int:
        denominator = float(np.dot(motor_axis, motor_axis))
        requested_units = float(
            np.dot(np.asarray([dx_work, dy_work]), motor_axis) / denominator
        )
        units_per_pulse = self._units_per_pulse(self.current_microstep)
        requested_pulses = requested_units / units_per_pulse
        if abs(requested_pulses) < 0.25:
            return 0
        pulses = int(round(requested_pulses))
        if pulses == 0:
            pulses = 1 if requested_pulses > 0 else -1
        return int(np.clip(pulses, -8, 8))

    def _align_enabled_to_working_frame(
        self,
        target_work: np.ndarray,
        motor_axis: np.ndarray,
        stage: str,
        *,
        tolerance_px: float,
        max_attempts: int = 16,
    ) -> tuple[int, np.ndarray]:
        """Move the enabled turret until its image reaches a disabled target."""
        self.motor.objective_enable()
        self._wait(self.move_settle_seconds)
        no_progress = 0
        previous_distance = float("inf")
        for attempt in range(1, max_attempts + 1):
            frame_id, frame = self._new_frame()
            current_work, scale = _working_gray(frame, 384)
            dx, dy, distance, confidence, overlap = measure_image_displacement(
                current_work,
                target_work,
                roi_fraction=1.0,
            )
            distance_px = float(distance / max(1e-9, scale))
            valid = (
                confidence >= self.release_min_confidence
                and overlap >= self.minimum_overlap
            )
            self._publish_diagnostic(
                stage,
                f"alignment attempt {attempt}: residual={distance_px:.3f}px "
                f"(dx={dx / max(1e-9, scale):+.3f}, "
                f"dy={dy / max(1e-9, scale):+.3f}), confidence="
                f"{confidence:.3f}, overlap={overlap:.3f}.",
            )
            if not valid:
                raise RuntimeError(
                    f"{stage} could not localize the disabled target image"
                )
            if distance_px <= tolerance_px:
                return frame_id, frame

            pulses = self._alignment_command(dx, dy, motor_axis)
            if pulses == 0:
                raise RuntimeError(
                    f"{stage} has {distance_px:.3f}px residual but no "
                    "correctable motion along the objective axis"
                )
            if distance_px >= previous_distance - 0.5:
                no_progress += 1
            else:
                no_progress = 0
            if no_progress >= 3:
                raise RuntimeError(
                    f"{stage} did not reduce its visual residual for three "
                    "consecutive attempts"
                )
            previous_distance = distance_px
            self._move_pulses(pulses)
        raise RuntimeError(
            f"{stage} did not reach the disabled target within "
            f"{max_attempts} visual corrections"
        )

    def _release_centering(
        self,
        motor_axis: np.ndarray,
    ) -> tuple[np.ndarray, LightObjectiveReleaseMeasurement]:
        """Iteratively move the enabled image onto each relaxed landing image."""
        last_item = None
        last_disabled_work = None
        for iteration in range(1, self.release_max_iterations + 1):
            item, _disabled_frame, disabled_work = self._capture_release(
                "release_centering",
                iteration,
            )
            if not item.valid:
                raise RuntimeError(
                    "The enabled-to-disabled displacement could not be "
                    "measured reliably during light calibration"
                )
            last_item = item
            last_disabled_work = disabled_work
            self._publish_diagnostic(
                "release centering",
                f"Iteration {iteration}: enabled-to-disabled shift="
                f"{item.displacement:.3f}px (limit "
                f"{self.release_threshold_px:.3f}px).",
            )
            if item.displacement <= self.release_threshold_px:
                return disabled_work, item
            self._align_enabled_to_working_frame(
                disabled_work,
                motor_axis,
                "release_centering_move",
                tolerance_px=self.neighbor_consistency_px,
            )
        raise RuntimeError(
            f"Release centering did not reach {self.release_threshold_px:g}px "
            f"within {self.release_max_iterations} cycles; last shift was "
            f"{last_item.displacement:.3f}px"
        )

    def _stable_point_check(
        self,
        stable_target: np.ndarray,
        motor_axis: np.ndarray,
        probe_pulses: int,
    ) -> tuple[bool, float, float, int]:
        """Check left/right release agreement and repeated center release."""
        probe_disabled_frames = []
        probes = []
        for direction, name in ((-1, "left"), (1, "right")):
            self._align_enabled_to_working_frame(
                stable_target,
                motor_axis,
                f"stable_{name}_prepare",
                tolerance_px=self.neighbor_consistency_px,
            )
            self._move_pulses(direction * int(probe_pulses))
            item, _frame, disabled_work = self._capture_release(
                f"stable_{name}_probe",
                1,
                endpoint_target=stable_target,
            )
            probes.append(item)
            probe_disabled_frames.append(disabled_work)

        endpoint_separation = float("inf")
        endpoint_confidence = 0.0
        endpoint_overlap = 0.0
        if len(probe_disabled_frames) == 2:
            _dx, _dy, endpoint_work, endpoint_confidence, endpoint_overlap = (
                measure_image_displacement(
                    probe_disabled_frames[0],
                    probe_disabled_frames[1],
                    roi_fraction=1.0,
                )
            )
            # All working frames use the same resize factor.  Recover it from
            # the reference image size for a full-camera-pixel report.
            _reference_work, reference_scale = _working_gray(
                self.reference_frame, 384
            )
            endpoint_separation = float(
                endpoint_work / max(1e-9, reference_scale)
            )
        endpoints_agree = (
            all(item.valid for item in probes)
            and endpoint_confidence >= self.release_min_confidence
            and endpoint_overlap >= self.minimum_overlap
            and endpoint_separation <= self.neighbor_consistency_px
        )

        verification_target = (
            probe_disabled_frames[-1]
            if endpoints_agree else stable_target
        )
        verification = []
        for repeat in range(1, self.verification_repeats + 1):
            self._align_enabled_to_working_frame(
                verification_target,
                motor_axis,
                f"stable_verify_prepare_{repeat}",
                tolerance_px=self.neighbor_consistency_px,
            )
            item, _frame, _disabled_work = self._capture_release(
                "stable_verify",
                repeat,
                endpoint_target=verification_target,
            )
            verification.append(item)

        distances = [
            item.displacement for item in verification if item.valid
        ]
        median_release = (
            float(np.median(distances)) if distances else float("nan")
        )
        verification_passed = (
            len(distances) == self.verification_repeats
            and median_release <= self.release_threshold_px
        )
        stable = endpoints_agree and verification_passed
        self._publish_diagnostic(
            "stable check",
            f"Left/right disabled endpoint separation="
            f"{endpoint_separation:.3f}px (limit "
            f"{self.neighbor_consistency_px:.3f}px): "
            f"{'PASS' if endpoints_agree else 'FAIL'}; repeated center "
            f"release median={median_release:.3f}px (limit "
            f"{self.release_threshold_px:.3f}px): "
            f"{'PASS' if verification_passed else 'FAIL'}.",
        )
        return stable, median_release, endpoint_separation, len(distances)

    def run(
        self,
        *,
        half_range_full_steps: int = 10,
        coarse_microstep: int = 2,
        fine_microstep: int = 16,
        fine_step_pulses: int = 1,
        focus_half_range: int = 1500,
        focus_step: int = 100,
        focus_fine_radius: int | None = None,
        focus_min_step: int | None = None,
        focus_speed: int = 1200,
        focus_settle_seconds: float = 0.35,
        focus_frames: int = 3,
        focus_frame_interval: float = 0.08,
        focus_roi_fraction: float = 0.60,
        focus_backlash_steps: int = 0,
        lock_exposure: bool = True,
    ) -> LightObjectiveResult:
        if half_range_full_steps <= 0:
            raise ValueError("half_range_full_steps must be positive")
        if coarse_microstep not in self.MICROSTEP_MODES:
            raise ValueError("coarse_microstep must be 1, 2, 4, 8, or 16")
        if fine_microstep not in self.MICROSTEP_MODES:
            raise ValueError("fine_microstep must be 1, 2, 4, 8, or 16")
        if fine_microstep < coarse_microstep:
            raise ValueError("fine_microstep must be at least coarse_microstep")
        if fine_step_pulses < 1:
            raise ValueError("fine_step_pulses must be at least 1")
        if focus_half_range <= 0 or not 0 < focus_step <= 2 * focus_half_range:
            raise ValueError("Local autofocus range/maximum step is invalid")
        if focus_fine_radius is not None and focus_fine_radius < focus_step:
            raise ValueError("Local autofocus fine radius must be at least its maximum step")
        if focus_min_step is not None and not 0 < focus_min_step <= focus_step:
            raise ValueError("Local autofocus minimum step must be positive and at most its maximum step")
        if not self.camera.is_open:
            raise RuntimeError("Camera is not open")
        if not self.motor.connected:
            raise RuntimeError("Motor controller is not connected")

        stabilization_before = self.camera.stabilization_enabled
        auto_exposure_before = None
        self.position_units = 0
        self.last_frame_id = None
        self.measurements.clear()
        self.coarse_frames.clear()
        autofocus_result = None

        try:
            self.camera.set_stabilization(False)
            if lock_exposure:
                time.sleep(0.7)
                try:
                    auto_exposure_before = bool(
                        self.camera.sdk_camera.get_AutoExpoEnable()
                    )
                except Exception:
                    pass
                self.camera.set_auto_exposure(False)

            self.motor.objective_speed(self.speed)
            self.motor.objective_enable()
            self._set_microstep(coarse_microstep)
            coarse_step_units = self._units_per_pulse(coarse_microstep)
            half_range_units = int(half_range_full_steps) * 16
            coarse = self._scan_units(
                -half_range_units,
                half_range_units,
                coarse_step_units,
                "coarse_match",
                remember_in=self.coarse_frames,
            )
            region_lower, region_upper, coarse_peak = self._matching_region(
                coarse,
                coarse_step_units,
            )
            motor_axis = self._coarse_motor_axis()

            self._return_to_saved_view(
                saved_frames=self.coarse_frames,
                target_position_units=coarse_peak.position_units,
                lower_position_units=-half_range_units,
                upper_position_units=half_range_units,
                stage="coarse_visual_return",
            )
            self._publish_diagnostic(
                "autofocus",
                f"At the visually recovered coarse peak "
                f"{coarse_peak.position_units}/16 step. Autofocus will stop "
                "and disable the objective turret before running LOCAL "
                f"autofocus: continuous ±{focus_half_range} focus steps, then "
                f"one adaptive fine pass with maximum step {focus_step}; "
                "the landing score is verified.",
            )
            focuser = AutoFocuser(
                self.camera,
                self.motor,
                speed=focus_speed,
                settle_seconds=focus_settle_seconds,
                frames_per_position=focus_frames,
                precision_settle_seconds=focus_settle_seconds,
                precision_frames_per_position=focus_frames,
                frame_interval=focus_frame_interval,
                roi_fraction=focus_roi_fraction,
                backlash_steps=focus_backlash_steps,
                progress=self.focus_progress,
                cancel_event=self.cancel_event,
            )
            try:
                autofocus_result = focuser.run_local(
                    half_range=focus_half_range,
                    step=focus_step,
                    fine_radius=focus_fine_radius,
                    min_step=focus_min_step,
                    lock_exposure=False,
                )
            except AutofocusCancelled as exc:
                raise LightObjectiveCalibrationCancelled(str(exc)) from exc
            finally:
                try:
                    self.motor.focus_stop()
                    self.motor.focus_disable()
                except Exception:
                    pass

            self._set_microstep(fine_microstep)
            stable_target, _centered_release = self._release_centering(
                motor_axis
            )
            mechanical_stable, median_release, endpoint_separation, (
                verification_count
            ) = self._stable_point_check(
                stable_target,
                motor_axis,
                probe_pulses=int(fine_step_pulses),
            )

            # The stable-point routine finishes with the turret disabled.
            # Preserve the original light-calibration centering requirement as
            # a final reference-view check.
            final_disabled = self._measure("center_verify_disabled")
            stable = (
                mechanical_stable
                and final_disabled.valid
                and final_disabled.center_error <= self.center_tolerance_px
            )
            self._publish_diagnostic(
                "final",
                f"Mechanical stable check="
                f"{'PASS' if mechanical_stable else 'FAIL'}; disabled view "
                f"center error={final_disabled.center_error:.3f}px "
                f"(limit {self.center_tolerance_px:.3f}px), match="
                f"{final_disabled.score:.3f}; "
                f"{'PASS' if stable else 'REVIEW NEEDED'}.",
            )
            return LightObjectiveResult(
                position_units=int(self.position_units),
                final_score=float(final_disabled.score),
                final_center_dx=float(final_disabled.center_dx),
                final_center_dy=float(final_disabled.center_dy),
                final_center_error=float(final_disabled.center_error),
                stable=bool(stable),
                coarse_region_units=(int(region_lower), int(region_upper)),
                coarse_microstep=int(coarse_microstep),
                fine_microstep=int(fine_microstep),
                autofocus_result=autofocus_result,
                median_release_displacement=float(median_release),
                neighbor_endpoint_separation=float(endpoint_separation),
                verification_count=int(verification_count),
            )
        finally:
            try:
                self.motor.objective_stop()
            except Exception:
                pass
            try:
                self.motor.objective_disable()
            except Exception:
                pass
            try:
                self.motor.focus_stop()
                self.motor.focus_disable()
            except Exception:
                pass
            self.camera.set_stabilization(stabilization_before)
            if lock_exposure and auto_exposure_before is not None:
                self.camera.set_auto_exposure(auto_exposure_before)


def save_light_objective_measurements(
    path: str | Path,
    measurements: list[
        LightObjectiveMeasurement | LightObjectiveReleaseMeasurement
    ],
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow((
            "kind",
            "stage",
            "position_1_16_step_units",
            "microstep",
            "frame_id",
            "score",
            "second_score",
            "uniqueness",
            "center_dx_px",
            "center_dy_px",
            "center_error_px",
            "overlap",
            "valid",
            "enabled_frame_id",
            "disabled_frame_id",
            "release_dx_px",
            "release_dy_px",
            "release_displacement_px",
            "release_confidence",
            "endpoint_error_px",
        ))
        for item in measurements:
            if isinstance(item, LightObjectiveReleaseMeasurement):
                writer.writerow((
                    "release",
                    item.stage,
                    item.position_units,
                    item.microstep,
                    "", "", "", "", "", "", "",
                    item.overlap,
                    item.valid,
                    item.enabled_frame_id,
                    item.disabled_frame_id,
                    item.dx,
                    item.dy,
                    item.displacement,
                    item.confidence,
                    item.endpoint_error,
                ))
            else:
                writer.writerow((
                    "reference_match",
                    item.stage,
                    item.position_units,
                    item.microstep,
                    item.frame_id,
                    item.score,
                    item.second_score,
                    item.uniqueness,
                    item.center_dx,
                    item.center_dy,
                    item.center_error,
                    item.overlap,
                    item.valid,
                    "", "", "", "", "", "", "",
                ))
    return path
