"""Image-anchored calibration for the objective-turret detent.

Reported positions are cumulative image displacement in pixels from the
visually detected left edge of the saved-view region.  Motor pulses are used
only to move and to estimate the size of the next command; every captured
frame corrects the position through image-atlas registration.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from camera import MicroscopeCamera


class ObjectiveCalibrationCancelled(RuntimeError):
    """Raised when the operator stops objective calibration."""


class _AtlasStepRegistrationError(RuntimeError):
    """Raised when adjacent atlas frames cannot form a trustworthy segment."""


class _VisualTargetUnreachable(RuntimeError):
    """Raised when the motor repeatedly crosses a target without reaching it."""

    def __init__(
        self,
        target: float,
        last_position: float,
        crossings: int,
        frame_id: int,
        similarity: float,
        confidence: float,
    ):
        super().__init__(
            f"Target {target:.3f} was crossed {crossings} times; "
            f"last visual position={last_position:.3f}"
        )
        self.target = float(target)
        self.last_position = float(last_position)
        self.crossings = int(crossings)
        self.frame_id = int(frame_id)
        self.similarity = float(similarity)
        self.confidence = float(confidence)


@dataclass(frozen=True)
class ObjectiveMeasurement:
    """One ENABLE-to-DISABLE image-displacement measurement."""

    position: float
    displacement: float
    dx: float
    dy: float
    confidence: float
    overlap: float
    valid: bool
    stage: str
    enabled_frame_id: int | None = None
    disabled_frame_id: int | None = None
    disabled_visual_position: float | None = None
    reference_similarity: float | None = None
    status: str = "VALID"
    commanded_position: float | None = None
    localization_confidence: float | None = None
    microstep: int = 1
    signed_displacement: float = float("nan")
    scan_direction: int = 0
    atlas_attempt_id: int | None = None


@dataclass(frozen=True)
class ObjectiveAtlasAttemptUpdate:
    """Tell the GUI to retain or remove one temporary atlas attempt."""

    attempt_id: int
    microstep: int
    action: str
    reason: str = ""
    retain_through: float | None = None


@dataclass(frozen=True)
class ObjectiveDiagnosticUpdate:
    """Human-readable explanation of one calibration decision."""

    stage: str
    message: str


@dataclass(frozen=True)
class ObjectivePositionUpdate:
    """Pixel-atlas position reported immediately before a motor move."""

    position: float
    predicted_position: float
    target: float | None
    pulses: int
    microstep: int
    stage: str


@dataclass(frozen=True)
class ObjectiveCalibrationResult:
    """Final relative motor position and its repeated-release verification."""

    position: float
    median_displacement: float
    maximum_displacement: float
    minimum_confidence: float
    stable: bool
    peak_regions: tuple[tuple[float, float], ...]
    match_region: tuple[float, float]
    release_range: tuple[float, float]
    verification_count: int
    atlas_microstep: int = 1
    final_microstep: int = 16


@dataclass(frozen=True)
class _AtlasNode:
    position: float
    frame: np.ndarray
    frame_id: int
    reference_similarity: float


@dataclass(frozen=True)
class _AtlasSegment:
    left: _AtlasNode
    right: _AtlasNode
    dx: float
    dy: float
    confidence: float
    overlap: float


@dataclass(frozen=True)
class _MergedReleasePoint:
    """Nearby release samples represented by one robust visual point."""

    position: float
    displacement: float
    signed_displacement: float
    disabled_visual_position: float | None


def _center_gray(frame: np.ndarray, roi_fraction: float) -> tuple[np.ndarray, float, float]:
    if frame is None or frame.size == 0:
        raise ValueError("Cannot measure displacement from an empty frame")
    if not 0 < roi_fraction <= 1:
        raise ValueError("roi_fraction must be greater than 0 and at most 1")

    height, width = frame.shape[:2]
    roi_width = max(64, int(width * roi_fraction))
    roi_height = max(64, int(height * roi_fraction))
    x1 = max(0, (width - roi_width) // 2)
    y1 = max(0, (height - roi_height) // 2)
    roi = frame[y1:y1 + roi_height, x1:x1 + roi_width]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    gray = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)

    # Phase correlation is much faster at this size.  Shifts are converted
    # back to pixels in the original ROI before being returned.
    scale_x = scale_y = 1.0
    if max(gray.shape) > 1024:
        scale = 1024.0 / max(gray.shape)
        resized = cv2.resize(
            gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        scale_x = roi_width / resized.shape[1]
        scale_y = roi_height / resized.shape[0]
        gray = resized
    return gray, scale_x, scale_y


def _aligned_correlation(first: np.ndarray, second: np.ndarray,
                         dx: int, dy: int) -> float:
    """Return brightness-independent correlation over the translated overlap."""
    height, width = first.shape
    overlap_width = width - abs(dx)
    overlap_height = height - abs(dy)
    if overlap_width < 32 or overlap_height < 32:
        return 0.0

    first_x = max(0, -dx)
    second_x = max(0, dx)
    first_y = max(0, -dy)
    second_y = max(0, dy)
    a = first[
        first_y:first_y + overlap_height,
        first_x:first_x + overlap_width,
    ].reshape(-1)
    b = second[
        second_y:second_y + overlap_height,
        second_x:second_x + overlap_width,
    ].reshape(-1)
    a = a - float(a.mean())
    b = b - float(b.mean())
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 0.0
    return float(np.clip(np.dot(a, b) / denominator, 0.0, 1.0))


def measure_image_displacement(
    enabled_frame: np.ndarray,
    disabled_frame: np.ndarray,
    *,
    roi_fraction: float = 1.0,
) -> tuple[float, float, float, float, float]:
    """Return ``(dx, dy, distance, confidence, overlap)`` in ROI pixels."""
    first, scale_x, scale_y = _center_gray(enabled_frame, roi_fraction)
    second, second_scale_x, second_scale_y = _center_gray(
        disabled_frame, roi_fraction
    )
    if first.shape != second.shape:
        raise ValueError("Enabled and disabled frames have different sizes")
    if not np.isclose(scale_x, second_scale_x) or not np.isclose(
        scale_y, second_scale_y
    ):
        raise ValueError("Enabled and disabled frames have different scales")

    window = cv2.createHanningWindow((first.shape[1], first.shape[0]), cv2.CV_32F)
    (work_dx, work_dy), phase_response = cv2.phaseCorrelate(
        first, second, window
    )
    rounded_dx = int(round(work_dx))
    rounded_dy = int(round(work_dy))
    overlap = max(0.0, 1.0 - abs(work_dx) / first.shape[1]) * max(
        0.0, 1.0 - abs(work_dy) / first.shape[0]
    )
    image_correlation = _aligned_correlation(
        first, second, rounded_dx, rounded_dy
    )
    confidence = min(
        float(np.clip(phase_response, 0.0, 1.0)), image_correlation
    )
    dx = float(work_dx * scale_x)
    dy = float(work_dy * scale_y)
    distance = float(np.hypot(dx, dy))
    return dx, dy, distance, confidence, float(overlap)


def _as_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def reference_view_similarity(
    reference_frame: np.ndarray,
    candidate_frame: np.ndarray,
    *,
    roi_fraction: float = 0.80,
) -> float:
    """Compare color distribution and blurred structure on a 0-to-1 scale.

    Strong common blurring deliberately makes this comparison tolerant of the
    focus change caused by switching objectives.  A uniform black, yellow, or
    other field normally lacks both the reference color distribution and its
    large-scale spatial structure.
    """
    if reference_frame is None or candidate_frame is None:
        return 0.0
    reference = _as_bgr(reference_frame)
    candidate = _as_bgr(candidate_frame)
    if reference.size == 0 or candidate.size == 0:
        return 0.0

    def center_roi(frame):
        height, width = frame.shape[:2]
        roi_width = max(32, int(width * roi_fraction))
        roi_height = max(32, int(height * roi_fraction))
        x1 = max(0, (width - roi_width) // 2)
        y1 = max(0, (height - roi_height) // 2)
        return frame[y1:y1 + roi_height, x1:x1 + roi_width]

    reference = center_roi(reference)
    candidate = center_roi(candidate)
    work_scale = min(1.0, 512.0 / max(reference.shape[:2]))
    work_width = max(64, int(round(reference.shape[1] * work_scale)))
    work_height = max(64, int(round(reference.shape[0] * work_scale)))
    reference = cv2.resize(
        reference,
        (work_width, work_height),
        interpolation=cv2.INTER_AREA,
    )
    candidate = cv2.resize(
        candidate,
        (work_width, work_height),
        interpolation=cv2.INTER_AREA,
    )

    def color_histogram(frame):
        small = cv2.resize(frame, (192, 144), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        histogram = cv2.calcHist(
            [hsv], [0, 1], None, [24, 16], [0, 180, 0, 256]
        )
        cv2.normalize(histogram, histogram, alpha=1.0, norm_type=cv2.NORM_L1)
        return histogram

    color_similarity = float(np.clip(cv2.compareHist(
        color_histogram(reference),
        color_histogram(candidate),
        cv2.HISTCMP_INTERSECT,
    ), 0.0, 1.0))

    # Preserve large-scale sample structure while suppressing focus-sensitive
    # detail.  Phase correlation permits moderate lateral displacement.
    reference_blurred = cv2.GaussianBlur(reference, (0, 0), 5.0)
    candidate_blurred = cv2.GaussianBlur(candidate, (0, 0), 5.0)
    try:
        _dx, _dy, _distance, structure_similarity, _overlap = (
            measure_image_displacement(
                reference_blurred,
                candidate_blurred,
                roi_fraction=1.0,
            )
        )
    except (cv2.error, ValueError):
        structure_similarity = 0.0

    # A very small spatial color grid distinguishes a structured reference
    # from a uniform field even when both contain a similar average color.
    reference_grid = cv2.resize(
        reference, (8, 6), interpolation=cv2.INTER_AREA
    ).astype(np.float32)
    candidate_grid = cv2.resize(
        candidate, (8, 6), interpolation=cv2.INTER_AREA
    ).astype(np.float32)
    grid_difference = float(np.mean(np.abs(reference_grid - candidate_grid)))
    grid_similarity = float(np.exp(-grid_difference / 45.0))

    return float(np.clip(
        0.50 * color_similarity
        + 0.30 * structure_similarity
        + 0.20 * grid_similarity,
        0.0,
        1.0,
    ))


class _EdgeReacquireObjectiveCalibrator:
    """Previous edge-reacquisition implementation kept for comparison."""

    def __init__(
        self,
        camera: MicroscopeCamera,
        motor,
        *,
        reference_frame: np.ndarray,
        reference_threshold: float = 0.45,
        speed: int = 200,
        move_settle_seconds: float = 0.15,
        precision_settle_seconds: float = 0.80,
        enable_settle_seconds: float = 0.20,
        release_settle_seconds: float = 0.35,
        roi_fraction: float = 1.0,
        min_confidence: float = 0.15,
        min_overlap: float = 0.50,
        max_valid_displacement: float = 250.0,
        peak_threshold: float = 8.0,
        stable_threshold: float = 4.0,
        progress=None,
        cancel_event: threading.Event | None = None,
    ):
        if speed <= 0:
            raise ValueError("speed must be positive")
        if min(move_settle_seconds, precision_settle_seconds, enable_settle_seconds,
               release_settle_seconds) < 0:
            raise ValueError("wait times cannot be negative")
        if not 0 < roi_fraction <= 1:
            raise ValueError("roi_fraction must be greater than 0 and at most 1")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        if not 0 <= reference_threshold <= 1:
            raise ValueError("reference_threshold must be between 0 and 1")
        if not 0 < min_overlap <= 1:
            raise ValueError("min_overlap must be greater than 0 and at most 1")
        if min(max_valid_displacement, peak_threshold, stable_threshold) <= 0:
            raise ValueError("displacement thresholds must be positive")

        self.camera = camera
        self.motor = motor
        if reference_frame is None or reference_frame.size == 0:
            raise ValueError("A non-empty objective reference frame is required")
        self.reference_frame = reference_frame.copy()
        self.reference_threshold = float(reference_threshold)
        self.speed = int(speed)
        self.move_settle_seconds = float(move_settle_seconds)
        self.precision_settle_seconds = float(precision_settle_seconds)
        self.enable_settle_seconds = float(enable_settle_seconds)
        self.release_settle_seconds = float(release_settle_seconds)
        self.roi_fraction = float(roi_fraction)
        self.min_confidence = float(min_confidence)
        self.min_overlap = float(min_overlap)
        self.max_valid_displacement = float(max_valid_displacement)
        self.peak_threshold = float(peak_threshold)
        self.stable_threshold = float(stable_threshold)
        self.progress = progress
        self.cancel_event = cancel_event or threading.Event()
        self.position = 0
        self.last_relative_position: int | None = None
        self.last_frame_id = None
        self.measurements: list[ObjectiveMeasurement] = []
        self.edge_confirmation_positions = 2
        self.edge_exit_threshold = max(0.0, self.reference_threshold * 0.80)

    def _wait(self, seconds: float) -> None:
        if self.cancel_event.wait(max(0.0, seconds)):
            try:
                self.motor.objective_stop()
            finally:
                raise ObjectiveCalibrationCancelled(
                    "Objective calibration was stopped"
                )

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            try:
                self.motor.objective_stop()
            finally:
                raise ObjectiveCalibrationCancelled(
                    "Objective calibration was stopped"
                )

    def _move_to(self, target: int, *, settle_seconds: float | None = None) -> None:
        self._check_cancelled()
        distance = int(target) - self.position
        if distance == 0:
            return
        self.motor.objective_move(distance)
        if settle_seconds is None:
            settle_seconds = self.move_settle_seconds
        self._wait(abs(distance) / self.speed + settle_seconds)
        self.position = int(target)

    def _move_by(self, distance: int, *,
                 settle_seconds: float | None = None) -> None:
        self._move_to(
            self.position + int(distance), settle_seconds=settle_seconds
        )

    def _new_frame(self) -> tuple[int, np.ndarray]:
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
        return frame_id, frame

    def _measure_current_position(
        self,
        relative_position: int,
        stage: str,
    ) -> ObjectiveMeasurement:
        """Release once at a position found from a fresh visual anchor."""
        enabled_frame_id, enabled_frame = self._new_frame()

        self.motor.objective_disable()
        self._wait(self.release_settle_seconds)
        disabled_frame_id, disabled_frame = self._new_frame()
        enabled_similarity = reference_view_similarity(
            self.reference_frame, enabled_frame
        )
        disabled_similarity = reference_view_similarity(
            self.reference_frame, disabled_frame
        )
        reference_similarity = min(enabled_similarity, disabled_similarity)
        if reference_similarity < self.reference_threshold:
            dx = dy = displacement = float("nan")
            confidence = overlap = 0.0
            valid = False
            status = "SCENE_MISMATCH"
        else:
            dx, dy, displacement, confidence, overlap = (
                measure_image_displacement(
                    enabled_frame,
                    disabled_frame,
                    roi_fraction=self.roi_fraction,
                )
            )
            # A fast mechanical release can still be moving when the first
            # post-release frame arrives.  Retry once after the precision
            # settling time and keep whichever registration is clearer.
            if confidence < self.min_confidence:
                self._wait(self.precision_settle_seconds)
                retry_frame_id, retry_frame, retry_similarity = (
                    self._reference_sample()
                )
                if retry_similarity >= self.reference_threshold:
                    retry_values = measure_image_displacement(
                        enabled_frame,
                        retry_frame,
                        roi_fraction=self.roi_fraction,
                    )
                    if retry_values[3] > confidence:
                        dx, dy, displacement, confidence, overlap = retry_values
                        disabled_frame_id = retry_frame_id
                        disabled_frame = retry_frame
                        disabled_similarity = retry_similarity
                        reference_similarity = min(
                            enabled_similarity, disabled_similarity
                        )
            valid = (
                confidence >= self.min_confidence
                and overlap >= self.min_overlap
                and displacement <= self.max_valid_displacement
            )
            if valid:
                status = "VALID"
            elif confidence < self.min_confidence:
                status = "LOW_CONFIDENCE"
            else:
                status = "LARGE_MOVE"
        item = ObjectiveMeasurement(
            position=int(relative_position),
            displacement=displacement,
            dx=dx,
            dy=dy,
            confidence=confidence,
            overlap=overlap,
            valid=valid,
            stage=stage,
            enabled_frame_id=enabled_frame_id,
            disabled_frame_id=disabled_frame_id,
            reference_similarity=reference_similarity,
            status=status,
        )
        self.measurements.append(item)
        if self.progress is not None:
            self.progress(item)
        return item

    def _reference_sample(self) -> tuple[int, float]:
        """Capture one powered frame and return its ID and reference score."""
        frame_id, frame = self._new_frame()
        similarity = reference_view_similarity(self.reference_frame, frame)
        return frame_id, similarity

    def _publish_reference_sample(
        self,
        relative_position: int,
        stage: str,
        frame_id: int,
        similarity: float,
    ) -> ObjectiveMeasurement:
        matched = similarity >= self.reference_threshold
        item = ObjectiveMeasurement(
            position=int(relative_position),
            displacement=float("nan"),
            dx=float("nan"),
            dy=float("nan"),
            confidence=similarity,
            overlap=1.0,
            valid=matched,
            stage=stage,
            enabled_frame_id=frame_id,
            reference_similarity=similarity,
            status="REFERENCE_MATCH" if matched else "REFERENCE_MISS",
        )
        self.measurements.append(item)
        if self.progress is not None:
            self.progress(item)
        return item

    @staticmethod
    def _positions(start: int, stop: int, step: int) -> list[int]:
        positions = list(range(start, stop + 1, step))
        if not positions or positions[-1] != stop:
            positions.append(stop)
        return positions

    def _find_visual_anchor(
        self,
        direction: int,
        *,
        outside_margin: int,
        max_search_steps: int,
    ) -> None:
        """Find a reference-region edge from outside using image feedback.

        ``direction`` is +1 for the left edge and -1 for the right edge.  The
        returned physical location is the second consecutive matched point.
        No commanded absolute position is used as an anchor.
        """
        if direction not in (-1, 1):
            raise ValueError("anchor direction must be -1 or 1")

        self.motor.objective_enable()
        self._wait(self.enable_settle_seconds)

        # First move away from the matching region.  Continue in that same
        # direction until two consecutive images prove that we are outside.
        self._move_by(
            -direction * max(1, int(outside_margin)),
            settle_seconds=self.move_settle_seconds,
        )
        outside_count = 0
        for _ in range(max_search_steps):
            self._check_cancelled()
            _frame_id, similarity = self._reference_sample()
            if similarity < self.edge_exit_threshold:
                outside_count += 1
                if outside_count >= self.edge_confirmation_positions:
                    break
            else:
                outside_count = 0
            self._move_by(
                -direction, settle_seconds=self.move_settle_seconds
            )
        else:
            raise RuntimeError(
                "Could not find the outside of the saved-view region. "
                "Increase Edge search range or raise Reference threshold."
            )

        # Reverse once, discard however many pulses the reversal consumes,
        # and stop only when the camera sees the edge.  Variable backlash
        # therefore changes the pulse count, not the physical anchor.
        matched_count = 0
        for _ in range(max_search_steps):
            self._move_by(direction, settle_seconds=self.move_settle_seconds)
            _frame_id, similarity = self._reference_sample()
            if similarity >= self.reference_threshold:
                matched_count += 1
                if matched_count >= self.edge_confirmation_positions:
                    return
            else:
                matched_count = 0
        side = "left" if direction > 0 else "right"
        raise RuntimeError(
            f"Could not reacquire the {side} saved-view edge. Increase Edge "
            "search range, lower Reference threshold, or record a new image."
        )

    def _map_reference_region(
        self,
        *,
        outside_margin: int,
        max_search_steps: int,
    ) -> int:
        """Map the saved-view interval in one powered left-to-right pass."""
        self._find_visual_anchor(
            1,
            outside_margin=outside_margin,
            max_search_steps=max_search_steps,
        )
        anchor_frame_id, anchor_similarity = self._reference_sample()
        self._publish_reference_sample(
            0, "reference_map", anchor_frame_id, anchor_similarity
        )

        offset = 0
        last_matched_offset = 0
        outside_count = 0
        for _ in range(max_search_steps):
            self._move_by(1, settle_seconds=self.move_settle_seconds)
            offset += 1
            frame_id, similarity = self._reference_sample()
            self._publish_reference_sample(
                offset, "reference_map", frame_id, similarity
            )
            if similarity >= self.reference_threshold:
                last_matched_offset = offset
            if similarity < self.edge_exit_threshold:
                outside_count += 1
                if outside_count >= self.edge_confirmation_positions:
                    break
            else:
                outside_count = 0
        else:
            raise RuntimeError(
                "Could not find the right edge of the saved-view region. "
                "Increase Edge search range or raise Reference threshold."
            )

        # A right-edge search approaches from the right and confirms on its
        # second matched point, so use the corresponding interior point here.
        width = last_matched_offset - (
            self.edge_confirmation_positions - 1
        )
        if width <= 0:
            raise RuntimeError(
                "The saved-view region is too narrow for reliable image "
                "anchoring. Lower Reference threshold or record a clearer view."
            )
        return int(width)

    def _locate_relative_position(
        self,
        relative_position: int,
        match_width: int,
        *,
        outside_margin: int,
        max_search_steps: int,
    ) -> None:
        """Reach one candidate from the nearest usable visual edge."""
        relative_position = int(relative_position)
        if relative_position < 0:
            direction = -1
        elif relative_position > match_width:
            direction = 1
        elif relative_position <= match_width - relative_position:
            direction = 1
        else:
            direction = -1

        seek_margin = max(1, int(outside_margin))
        if self.last_relative_position is not None:
            if direction > 0:
                # Cross from the previous candidate to the left side first.
                seek_margin += max(0, self.last_relative_position)
            else:
                # Cross from the previous candidate to the right side first.
                seek_margin += max(
                    0, match_width - self.last_relative_position
                )

        self._find_visual_anchor(
            direction,
            outside_margin=seek_margin,
            max_search_steps=max_search_steps,
        )
        distance = (
            relative_position
            if direction > 0
            else match_width - relative_position
        )
        if distance < 0:
            raise RuntimeError("Candidate lies behind its visual anchor")
        if distance:
            self._move_by(
                direction * distance,
                settle_seconds=self.precision_settle_seconds,
            )
        else:
            self._wait(self.precision_settle_seconds)

    def _measure_relative_position(
        self,
        relative_position: int,
        match_width: int,
        stage: str,
        *,
        outside_margin: int,
        max_search_steps: int,
    ) -> ObjectiveMeasurement:
        self._locate_relative_position(
            relative_position,
            match_width,
            outside_margin=outside_margin,
            max_search_steps=max_search_steps,
        )
        item = self._measure_current_position(relative_position, stage)
        self.last_relative_position = int(relative_position)
        return item

    def _is_peak_signal(self, item: ObjectiveMeasurement) -> bool:
        # A low-overlap measurement can locate a violent-release region, but
        # its dx/dy is never used to choose a direction.
        if not np.isfinite(item.displacement):
            return False
        return item.status == "LARGE_MOVE" or (
            item.valid and item.displacement >= self.peak_threshold
        )

    @staticmethod
    def _group_positions(positions: list[int], max_gap: int
                         ) -> list[tuple[int, int]]:
        ordered = sorted(set(positions))
        if not ordered:
            return []
        groups = []
        start = previous = ordered[0]
        for position in ordered[1:]:
            if position - previous > max_gap:
                groups.append((start, previous))
                start = position
            previous = position
        groups.append((start, previous))
        return groups

    @staticmethod
    def _best_valid_by_position(
        measurements: list[ObjectiveMeasurement],
    ) -> dict[int, float]:
        by_position: dict[int, list[float]] = {}
        for item in measurements:
            if item.valid and np.isfinite(item.displacement):
                by_position.setdefault(item.position, []).append(
                    item.displacement
                )
        return {
            position: float(np.median(values))
            for position, values in by_position.items()
        }

    def _local_three_point_search(
        self,
        start: int,
        lower: int,
        upper: int,
        step: int,
        match_width: int,
        *,
        outside_margin: int,
        max_search_steps: int,
    ) -> int:
        candidate = int(np.clip(start, lower, upper))
        visited = set()
        best_seen = (float("inf"), candidate)

        while candidate not in visited:
            visited.add(candidate)
            positions = sorted(set((
                max(lower, candidate - step),
                candidate,
                min(upper, candidate + step),
            )))
            trio = [
                self._measure_relative_position(
                    position,
                    match_width,
                    "direction",
                    outside_margin=outside_margin,
                    max_search_steps=max_search_steps,
                )
                for position in positions
            ]
            usable = [item for item in trio if item.valid]
            if not usable:
                break
            local_best = min(usable, key=lambda item: item.displacement)
            center = next(
                (item for item in usable if item.position == candidate), None
            )
            # Sub-pixel differences are normally registration noise.  Keep
            # the center instead of drifting one pulse left because of list
            # ordering when the three positions are effectively equivalent.
            if (center is not None
                    and center.displacement <= local_best.displacement + 0.25):
                if center.displacement < best_seen[0]:
                    best_seen = (center.displacement, center.position)
                return candidate
            if local_best.displacement < best_seen[0]:
                best_seen = (local_best.displacement, local_best.position)
            if local_best.position == candidate:
                return candidate
            candidate = local_best.position
        return best_seen[1]

    def run(
        self,
        *,
        half_range: int = 30,
        coarse_step: int = 5,
        stagger_offset: int = 2,
        refine_radius: int = 5,
        fine_step: int = 1,
        verification_repeats: int = 3,
        lock_exposure: bool = True,
    ) -> ObjectiveCalibrationResult:
        """Map visual edges, then measure every release from a fresh edge."""
        if half_range <= 0:
            raise ValueError("half_range must be positive")
        if coarse_step <= 0 or refine_radius < 0 or fine_step <= 0:
            raise ValueError("scan steps and radii must be positive")
        if stagger_offset < 1:
            raise ValueError("edge confirmation count must be at least 1")
        if verification_repeats < 1:
            raise ValueError("verification_repeats must be at least 1")
        if not self.camera.is_open:
            raise RuntimeError("Camera is not open")
        if not self.motor.connected:
            raise RuntimeError("Motor controller is not connected")

        self.position = 0
        self.last_relative_position = None
        self.last_frame_id = None
        self.measurements.clear()
        self.edge_confirmation_positions = int(stagger_offset)
        stabilization_before = self.camera.stabilization_enabled
        auto_exposure_before = None
        peak_regions: list[tuple[int, int]] = []

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
            self._wait(self.enable_settle_seconds)

            # Build a fresh coordinate system from image edges.  Position 0
            # is the confirmed left edge; match_width is the confirmed right
            # edge.  Motor pulse totals never become calibration coordinates.
            outside_margin = max(2, int(coarse_step))
            max_search_steps = (
                2 * int(half_range)
                + 2 * int(refine_radius)
                + outside_margin
            )
            match_width = self._map_reference_region(
                outside_margin=outside_margin,
                max_search_steps=max_search_steps,
            )
            self.last_relative_position = match_width + (
                self.edge_confirmation_positions + 1
            )
            selected_match_region = (0, match_width)

            # Release scanning covers the complete matched interval plus the
            # requested number of pulses outside both of its boundaries.
            release_lower = -int(refine_radius)
            release_upper = match_width + int(refine_radius)
            release_positions = self._positions(
                release_lower, release_upper, fine_step
            )
            for relative_position in release_positions:
                # Each ENABLE-to-DISABLE test gets a new visual anchor.  Any
                # motion caused by the previous release is intentionally not
                # carried into the next candidate.
                self._measure_relative_position(
                    relative_position,
                    match_width,
                    "release_scan",
                    outside_margin=outside_margin,
                    max_search_steps=max_search_steps,
                )

            peak_positions = [
                item.position for item in self.measurements
                if self._is_peak_signal(item)
            ]
            peak_regions = self._group_positions(
                peak_positions, max(1, fine_step)
            )

            displacement_map = self._best_valid_by_position(self.measurements)
            if not displacement_map:
                raise RuntimeError(
                    "No trustworthy image-overlap measurements were found. "
                    "Lower Min confidence or increase Max valid shift."
                )
            coarse_best = min(displacement_map, key=displacement_map.get)
            search_start = coarse_best
            # When two release peaks have been mapped, begin at the middle of
            # the valley they enclose.  The three-point measurements then
            # compensate for an asymmetric mechanical detent.
            enclosing_pairs = []
            for left_region, right_region in zip(
                peak_regions, peak_regions[1:]
            ):
                valley_start = left_region[1] + fine_step
                valley_stop = right_region[0] - fine_step
                if valley_start > valley_stop:
                    continue
                valley_values = [
                    value for position, value in displacement_map.items()
                    if valley_start <= position <= valley_stop
                ]
                if valley_values:
                    enclosing_pairs.append((
                        min(valley_values),
                        valley_start,
                        valley_stop,
                    ))
            if enclosing_pairs:
                _score, valley_start, valley_stop = min(enclosing_pairs)
                search_start = int(round((valley_start + valley_stop) / 2))
            best_position = self._local_three_point_search(
                search_start,
                release_lower,
                release_upper,
                fine_step,
                match_width,
                outside_margin=outside_margin,
                max_search_steps=max_search_steps,
            )

            verification = [
                self._measure_relative_position(
                    best_position,
                    match_width,
                    "verify",
                    outside_margin=outside_margin,
                    max_search_steps=max_search_steps,
                )
                for _ in range(verification_repeats)
            ]

            distances = [item.displacement for item in verification]
            valid_verification = [item.valid for item in verification]
            result = ObjectiveCalibrationResult(
                position=best_position,
                median_displacement=float(np.median(distances)),
                maximum_displacement=float(max(distances)),
                minimum_confidence=float(
                    min(item.confidence for item in verification)
                ),
                stable=(
                    all(valid_verification)
                    and float(np.median(distances)) <= self.stable_threshold
                ),
                peak_regions=tuple(peak_regions),
                match_region=selected_match_region,
                release_range=(release_lower, release_upper),
                verification_count=verification_repeats,
            )
            return result
        finally:
            # The mechanical detent, rather than motor torque, must hold the
            # objective after every successful, cancelled, or failed run.
            try:
                if self.motor.connected:
                    self.motor.objective_stop()
            except Exception:
                pass
            try:
                if self.motor.connected:
                    self.motor.objective_disable()
            except Exception:
                pass
            self.camera.set_stabilization(stabilization_before)
            if lock_exposure and auto_exposure_before is not None:
                self.camera.set_auto_exposure(auto_exposure_before)


class ObjectiveCalibrator:
    """Calibrate the detent with an in-memory visual position atlas.

    Atlas and measurement positions are image displacement in pixels.  Motor
    pulse counts remain separate and are used only to estimate a command size;
    captured images always provide the authoritative position.
    """

    MICROSTEP_MODES = (1, 2, 4, 8, 16)

    def __init__(
        self,
        camera: MicroscopeCamera,
        motor,
        *,
        reference_frame: np.ndarray,
        reference_threshold: float = 0.45,
        speed: int = 200,
        move_settle_seconds: float = 0.15,
        precision_settle_seconds: float = 0.80,
        enable_settle_seconds: float = 0.20,
        release_settle_seconds: float = 0.35,
        roi_fraction: float = 1.0,
        min_confidence: float = 0.15,
        min_overlap: float = 0.50,
        max_valid_displacement: float = 250.0,
        peak_threshold: float = 8.0,
        stable_threshold: float = 4.0,
        neighbor_consistency_tolerance_px: float = 2.0,
        atlas_merge_distance_px: float = 1.0,
        post_peak_quiet_distance_px: float = 150.0,
        progress=None,
        position_progress=None,
        before_move=None,
        cancel_event: threading.Event | None = None,
    ):
        if speed <= 0:
            raise ValueError("speed must be positive")
        if min(move_settle_seconds, precision_settle_seconds,
               enable_settle_seconds, release_settle_seconds) < 0:
            raise ValueError("wait times cannot be negative")
        if not 0 < roi_fraction <= 1:
            raise ValueError("roi_fraction must be greater than 0 and at most 1")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        if not 0 <= reference_threshold <= 1:
            raise ValueError("reference_threshold must be between 0 and 1")
        if not 0 < min_overlap <= 1:
            raise ValueError("min_overlap must be greater than 0 and at most 1")
        if min(max_valid_displacement, peak_threshold, stable_threshold) <= 0:
            raise ValueError("displacement thresholds must be positive")
        if neighbor_consistency_tolerance_px <= 0:
            raise ValueError(
                "neighbor consistency tolerance must be positive"
            )
        if atlas_merge_distance_px < 0:
            raise ValueError("atlas merge distance cannot be negative")
        if post_peak_quiet_distance_px < 0:
            raise ValueError("post-peak quiet distance cannot be negative")
        if reference_frame is None or reference_frame.size == 0:
            raise ValueError("A non-empty objective reference frame is required")

        self.camera = camera
        self.motor = motor
        self.reference_frame = reference_frame.copy()
        self.reference_threshold = float(reference_threshold)
        self.edge_exit_threshold = max(0.0, reference_threshold * 0.80)
        self.speed = int(speed)
        self.move_settle_seconds = float(move_settle_seconds)
        self.precision_settle_seconds = float(precision_settle_seconds)
        self.enable_settle_seconds = float(enable_settle_seconds)
        self.release_settle_seconds = float(release_settle_seconds)
        self.roi_fraction = float(roi_fraction)
        self.min_confidence = float(min_confidence)
        self.min_overlap = float(min_overlap)
        self.max_valid_displacement = float(max_valid_displacement)
        self.peak_threshold = float(peak_threshold)
        self.stable_threshold = float(stable_threshold)
        self.neighbor_consistency_tolerance_px = float(
            neighbor_consistency_tolerance_px
        )
        self.atlas_merge_distance_px = max(
            0.20, float(atlas_merge_distance_px)
        )
        self.post_peak_quiet_distance_px = float(
            post_peak_quiet_distance_px
        )
        self.progress = progress
        self.position_progress = position_progress
        self.before_move = before_move
        self.cancel_event = cancel_event or threading.Event()

        self.last_frame_id = None
        # Unknown until the first explicit MICRO command.  The GUI selection
        # may not match the Arduino after a previous manual operation.
        self.current_microstep: int | None = None
        self.commanded_position_units = 0.0
        # Learned from the forward atlas.  This conversion only estimates how
        # large the next command should be; it never defines visual position.
        self.atlas_pixels_per_motor_unit = 1.0
        self.visual_position_estimate: float | None = None
        self.movement_stage = "initializing"
        self.movement_target: float | None = None
        self.measurements: list[ObjectiveMeasurement] = []
        self.atlas: list[_AtlasNode] = []
        self.atlas_segments: list[_AtlasSegment] = []
        self.atlas_attempt_counter = 0
        self.peak_frames: list[
            tuple[float, np.ndarray, np.ndarray]
        ] = []

    @staticmethod
    def _units_per_pulse(microstep: int) -> int:
        return 16 // int(microstep)

    def _estimated_pixels_per_pulse(self, microstep: int | None = None) -> float:
        """Return the atlas-based pixel travel expected for one motor pulse."""
        if microstep is None:
            if self.current_microstep is None:
                return 1.0
            microstep = self.current_microstep
        return max(
            0.05,
            float(self.atlas_pixels_per_motor_unit)
            * self._units_per_pulse(int(microstep)),
        )

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            try:
                self.motor.objective_stop()
            finally:
                raise ObjectiveCalibrationCancelled(
                    "Objective calibration was stopped"
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
            self._wait(self.enable_settle_seconds)

    def _move_pulses(
        self,
        pulses: int,
        *,
        settle_seconds: float | None = None,
    ) -> None:
        self._check_cancelled()
        if self.current_microstep is None:
            raise RuntimeError("Microstep mode has not been initialized")
        pulses = int(pulses)
        if pulses == 0:
            if settle_seconds:
                self._wait(settle_seconds)
            return
        delta_units = pulses * self._units_per_pulse(self.current_microstep)
        predicted_visual_delta = (
            pulses * self._estimated_pixels_per_pulse(self.current_microstep)
        )
        if (
            self.position_progress is not None
            and self.visual_position_estimate is not None
            and np.isfinite(self.visual_position_estimate)
        ):
            self.position_progress(ObjectivePositionUpdate(
                position=float(self.visual_position_estimate),
                predicted_position=float(
                    self.visual_position_estimate + predicted_visual_delta
                ),
                target=(
                    None if self.movement_target is None
                    else float(self.movement_target)
                ),
                pulses=pulses,
                microstep=self.current_microstep,
                stage=self.movement_stage,
            ))
        # The GUI may put objective calibration into operator-controlled
        # single-step mode.  Position/prediction is published first so the
        # displacement plot shows the pending command while this callback
        # waits.  Other motor workflows do not use this hook.
        if self.before_move is not None:
            self.before_move()
            self._check_cancelled()
        self.motor.objective_move(pulses)
        if settle_seconds is None:
            settle_seconds = self.move_settle_seconds
        self._wait(abs(pulses) / self.speed + settle_seconds)
        self.commanded_position_units += delta_units
        if self.visual_position_estimate is not None:
            # This remains a displayed prediction only until the next camera
            # frame replaces it with an atlas-localized pixel coordinate.
            self.visual_position_estimate += predicted_visual_delta

    def _set_visual_position(
        self,
        position: float | None,
        *,
        stage: str,
        target: float | None = None,
    ) -> None:
        self.visual_position_estimate = (
            None if position is None else float(position)
        )
        self.movement_stage = stage
        self.movement_target = target

    def _new_frame(self) -> tuple[int, np.ndarray]:
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
        return frame_id, frame

    def _reference_sample(self) -> tuple[int, np.ndarray, float]:
        frame_id, frame = self._new_frame()
        similarity = reference_view_similarity(
            self.reference_frame,
            frame,
            roi_fraction=self.roi_fraction,
        )
        return frame_id, frame, similarity

    def _prepare_atlas_frame(self, frame: np.ndarray) -> np.ndarray:
        gray, _scale_x, _scale_y = _center_gray(frame, self.roi_fraction)
        if max(gray.shape) > 512:
            scale = 512.0 / max(gray.shape)
            gray = cv2.resize(
                gray,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA,
            )
        # uint8 keeps a 512-pixel atlas frame near 0.25 MB instead of roughly
        # 1 MB.  The displacement routine converts it to float only while a
        # comparison is active.
        return np.clip(gray, 0, 255).astype(np.uint8)

    def _publish(self, item: ObjectiveMeasurement) -> ObjectiveMeasurement:
        self.measurements.append(item)
        if self.progress is not None:
            self.progress(item)
        return item

    def _publish_diagnostic(self, stage: str, message: str) -> None:
        if self.progress is not None:
            self.progress(ObjectiveDiagnosticUpdate(
                stage=str(stage),
                message=str(message),
            ))

    def _publish_reference(
        self,
        position: float,
        frame_id: int,
        similarity: float,
        stage: str,
        atlas_attempt_id: int | None = None,
    ) -> None:
        matched = similarity >= self.reference_threshold
        self._publish(ObjectiveMeasurement(
            position=float(position),
            displacement=float("nan"),
            dx=float("nan"),
            dy=float("nan"),
            confidence=similarity,
            overlap=1.0,
            valid=matched,
            stage=stage,
            enabled_frame_id=frame_id,
            reference_similarity=similarity,
            status="REFERENCE_MATCH" if matched else "REFERENCE_MISS",
            commanded_position=self.commanded_position_units,
            localization_confidence=similarity,
            microstep=self.current_microstep,
            atlas_attempt_id=atlas_attempt_id,
        ))

    def _new_atlas_attempt(self, microstep: int) -> int:
        self.atlas_attempt_counter += 1
        attempt_id = self.atlas_attempt_counter
        if self.progress is not None:
            self.progress(ObjectiveAtlasAttemptUpdate(
                attempt_id=attempt_id,
                microstep=int(microstep),
                action="begin",
            ))
        return attempt_id

    def _finish_atlas_attempt(
        self,
        attempt_id: int,
        microstep: int,
        *,
        success: bool,
        reason: str = "",
    ) -> None:
        action = "commit" if success else "discard"
        if not success:
            self.measurements[:] = [
                item for item in self.measurements
                if item.atlas_attempt_id != attempt_id
            ]
        if self.progress is not None:
            self.progress(ObjectiveAtlasAttemptUpdate(
                attempt_id=int(attempt_id),
                microstep=int(microstep),
                action=action,
                reason=reason,
            ))

    def _publish_atlas_step(
        self,
        *,
        attempt_id: int,
        stage: str,
        position: float,
        frame_id: int,
        similarity: float,
        displacement: float,
        confidence: float,
        overlap: float,
        valid: bool,
        status: str,
    ) -> None:
        signed = (
            float(displacement)
            if np.isfinite(displacement) else float("nan")
        )
        self._publish(ObjectiveMeasurement(
            position=float(position),
            displacement=float(displacement),
            dx=float("nan"),
            dy=float("nan"),
            confidence=float(confidence),
            overlap=float(overlap),
            valid=bool(valid),
            stage=stage,
            enabled_frame_id=frame_id,
            reference_similarity=float(similarity),
            status=status,
            commanded_position=self.commanded_position_units,
            localization_confidence=float(confidence),
            microstep=self.current_microstep,
            signed_displacement=signed,
            scan_direction=1,
            atlas_attempt_id=int(attempt_id),
        ))

    def _find_visual_edge(
        self,
        direction: int,
        *,
        outside_margin_full_steps: int,
        max_search_full_steps: int,
        confirmation_positions: int,
        already_outside: bool = False,
    ) -> tuple[int, np.ndarray, float]:
        """Find the left (+1) or right (-1) edge from outside."""
        if direction not in (-1, 1):
            raise ValueError("edge direction must be -1 or 1")
        pulses_per_full_step = self.current_microstep
        outside_pulses = max(
            confirmation_positions,
            int(outside_margin_full_steps) * pulses_per_full_step,
        )
        max_pulses = max(
            confirmation_positions + 1,
            int(max_search_full_steps) * pulses_per_full_step,
        )

        self.motor.objective_enable()
        self._wait(self.enable_settle_seconds)
        if not already_outside:
            self._move_pulses(
                -direction * outside_pulses,
                settle_seconds=self.move_settle_seconds,
            )

            outside_count = 0
            for _ in range(max_pulses):
                frame_id, frame, similarity = self._reference_sample()
                if similarity < self.edge_exit_threshold:
                    outside_count += 1
                    if outside_count >= confirmation_positions:
                        break
                else:
                    outside_count = 0
                self._move_pulses(
                    -direction, settle_seconds=self.move_settle_seconds
                )
            else:
                raise RuntimeError(
                    "Could not reach the outside of the saved-view region. "
                    "Increase Edge search range or raise Reference threshold."
                )

        matched_count = 0
        for _ in range(max_pulses):
            self._move_pulses(direction, settle_seconds=self.move_settle_seconds)
            frame_id, frame, similarity = self._reference_sample()
            if similarity >= self.reference_threshold:
                matched_count += 1
                if matched_count >= confirmation_positions:
                    return frame_id, frame, similarity
            else:
                matched_count = 0
        side = "left" if direction > 0 else "right"
        raise RuntimeError(
            f"Could not find the {side} saved-view edge. Increase Edge "
            "search range, lower Reference threshold, or record a new image."
        )

    def _prepared_adjacent_registration(
        self,
        first: np.ndarray,
        second: np.ndarray,
    ) -> tuple[float, float, float, float, float] | None:
        try:
            values = measure_image_displacement(
                first,
                second,
                roi_fraction=1.0,
            )
        except (cv2.error, ValueError):
            return None
        _dx, _dy, _displacement, confidence, overlap = values
        if confidence < self.min_confidence or overlap < self.min_overlap:
            return None
        return values

    def _adjacent_registration(
        self,
        first: np.ndarray,
        second: np.ndarray,
    ) -> tuple[float, float, float, float, float] | None:
        return self._prepared_adjacent_registration(
            self._prepare_atlas_frame(first),
            self._prepare_atlas_frame(second),
        )

    def _adjacent_registration_is_usable(
        self,
        first: np.ndarray,
        second: np.ndarray,
    ) -> bool:
        return self._adjacent_registration(first, second) is not None

    def _choose_atlas_microstep(
        self,
        *,
        outside_margin_full_steps: int,
        max_search_full_steps: int,
        confirmation_positions: int,
        starting_mode_index: int = 0,
    ) -> tuple[int, int]:
        """Use the coarsest subdivision whose adjacent images register."""
        for microstep in self.MICROSTEP_MODES[starting_mode_index:]:
            attempt_id = self._new_atlas_attempt(microstep)
            self._set_microstep(microstep)
            self._set_visual_position(None, stage="atlas_step_test")
            try:
                anchor_id, anchor_frame, anchor_similarity = (
                    self._find_visual_edge(
                        1,
                        outside_margin_full_steps=outside_margin_full_steps,
                        max_search_full_steps=max_search_full_steps,
                        confirmation_positions=confirmation_positions,
                    )
                )
                self._set_visual_position(0.0, stage="atlas_step_test")
                self._publish_atlas_step(
                    attempt_id=attempt_id,
                    stage="atlas_step_test",
                    position=0.0,
                    frame_id=anchor_id,
                    similarity=anchor_similarity,
                    displacement=0.0,
                    confidence=1.0,
                    overlap=1.0,
                    valid=True,
                    status="ATLAS_TEST_ANCHOR",
                )
                self._move_pulses(
                    1, settle_seconds=self.precision_settle_seconds
                )
                next_id, next_frame, next_similarity = (
                    self._reference_sample()
                )
                try:
                    (
                        _dx,
                        _dy,
                        distance,
                        confidence,
                        overlap,
                    ) = measure_image_displacement(
                        self._prepare_atlas_frame(anchor_frame),
                        self._prepare_atlas_frame(next_frame),
                        roi_fraction=1.0,
                    )
                except (cv2.error, ValueError):
                    distance = float("nan")
                    confidence = 0.0
                    overlap = 0.0

                if next_similarity < self.reference_threshold:
                    status = "ATLAS_TEST_NO_MATCH"
                elif confidence < self.min_confidence:
                    status = "ATLAS_TEST_LOW_CONFIDENCE"
                elif overlap < self.min_overlap:
                    status = "ATLAS_TEST_LOW_OVERLAP"
                else:
                    status = "ATLAS_TEST_VALID"
                valid = status == "ATLAS_TEST_VALID"
                plot_position = (
                    float(distance)
                    if np.isfinite(distance)
                    else self._estimated_pixels_per_pulse(microstep)
                )
                self._publish_atlas_step(
                    attempt_id=attempt_id,
                    stage="atlas_step_test",
                    position=plot_position,
                    frame_id=next_id,
                    similarity=next_similarity,
                    displacement=distance,
                    confidence=confidence,
                    overlap=overlap,
                    valid=valid,
                    status=status,
                )
            except RuntimeError as exc:
                self._finish_atlas_attempt(
                    attempt_id,
                    microstep,
                    success=False,
                    reason=str(exc),
                )
                continue

            if valid:
                return microstep, attempt_id
            # Let the GUI render the failed criterion marker before the
            # attempt is removed and the next MICRO test begins.
            self._wait(0.25)
            self._finish_atlas_attempt(
                attempt_id,
                microstep,
                success=False,
                reason=(
                    f"{status}: similarity={next_similarity:.3f} "
                    f"(need {self.reference_threshold:.3f}), "
                    f"confidence={confidence:.3f} "
                    f"(need {self.min_confidence:.3f}), "
                    f"overlap={overlap:.3f} "
                    f"(need {self.min_overlap:.3f})"
                ),
            )
        raise RuntimeError(
            "Adjacent images still cannot be registered at MICRO 16. "
            "Increase image texture, improve focus, or lower Min confidence."
        )

    def _recover_partial_atlas_tail(
        self,
        nodes: list[_AtlasNode],
        segments: list[_AtlasSegment],
        failed_microstep: int,
        attempt_id: int,
        failed_position_hint: float | None = None,
    ) -> tuple[list[_AtlasNode], list[_AtlasSegment], float]:
        """Return to a trustworthy atlas node, dropping unreachable tail nodes."""
        self.atlas = list(nodes)
        self.atlas_segments = list(segments)
        failed_pixel_step = self._estimated_pixels_per_pulse(failed_microstep)
        current_position_hint = float(
            nodes[-1].position
            if failed_position_hint is None
            else failed_position_hint
        )

        def localize_current_without_motion() -> float | None:
            nonlocal current_position_hint
            frame_id, frame, similarity = self._reference_sample()
            if similarity < self.reference_threshold:
                return None
            try:
                position, _confidence = self._localize_in_atlas(
                    frame,
                    previous_position=nodes[-1].position,
                )
            except RuntimeError:
                return None
            current_position_hint = float(position)
            self._set_visual_position(
                position,
                stage="atlas_tail_reacquired",
                target=nodes[-1].position,
            )
            return current_position_hint

        # Seeking cannot choose a motor direction until the current frame has
        # an atlas coordinate.  Try fresh frames first in case the failed
        # registration was transient.  If localization is still unavailable,
        # move opposite to the known left-to-right atlas-build direction while
        # keeping the last trustworthy node as the one fixed target.
        localized_position = None
        self._set_visual_position(
            current_position_hint,
            stage="atlas_tail_reacquire",
            target=nodes[-1].position,
        )
        for _ in range(3):
            localized_position = localize_current_without_motion()
            if localized_position is not None:
                break

        if localized_position is None:
            failed_mode_index = self.MICROSTEP_MODES.index(failed_microstep)
            recovery_modes = list(reversed(
                self.MICROSTEP_MODES[:failed_mode_index + 1]
            ))
            for recovery_mode in recovery_modes:
                self._set_microstep(recovery_mode)
                for reverse_pulses in (1, 2, 4):
                    self._publish_diagnostic(
                        "atlas return",
                        f"Current frame is not localizable; keeping return "
                        f"target fixed at {nodes[-1].position:.3f}px and "
                        f"commanding {-reverse_pulses} pulse(s) at MICRO "
                        f"{recovery_mode} to re-enter the atlas.",
                    )
                    self._move_pulses(
                        -reverse_pulses,
                        settle_seconds=self.precision_settle_seconds,
                    )
                    localized_position = localize_current_without_motion()
                    if localized_position is not None:
                        break
                if localized_position is not None:
                    break

        if localized_position is None:
            raise RuntimeError(
                "Could not re-enter the partial atlas after explicit reverse "
                f"moves toward the fixed {nodes[-1].position:.3f}px target."
            )
        current_position_hint = float(localized_position)

        for target_index in range(len(nodes) - 1, -1, -1):
            target = nodes[target_index].position
            self._set_microstep(failed_microstep)
            self._set_visual_position(
                current_position_hint,
                stage="atlas_tail_return",
                target=target,
            )

            try:
                # First capture and localize the actual position.  The failed
                # forward pulse may not have moved the turret at all, in which
                # case the last trustworthy node has already been reached and
                # no reverse command is necessary.  Adaptive seeking uses
                # larger command groups and stronger microstep modes only when
                # the camera shows that progress has stalled.
                (
                    _frame_id,
                    _frame,
                    _similarity,
                    position,
                    _confidence,
                ) = self._seek_inside_atlas(
                    target,
                    current_position_hint,
                    max_pulses=max(
                        32,
                        int(np.ceil(
                            abs(current_position_hint - target)
                            / max(0.05, failed_pixel_step)
                        )) + 16,
                    ),
                    max_command_pulses=16,
                    tolerance=max(
                        self.atlas_merge_distance_px,
                        0.45 * failed_pixel_step,
                    ),
                    stage="atlas_tail_fine_return",
                    max_target_crossings=3,
                    adaptive_recovery=True,
                )
            except _VisualTargetUnreachable as exc:
                current_position_hint = exc.last_position
                self._publish_diagnostic(
                    "atlas return",
                    f"Atlas node {target:.3f}px abandoned only after "
                    f"crossing it {exc.crossings} times; last localized "
                    f"position={exc.last_position:.3f}px. Trying the "
                    "preceding trustworthy node.",
                )
                continue
            except ObjectiveCalibrationCancelled:
                raise
            except RuntimeError as exc:
                if (
                    "could not be placed in the atlas" in str(exc)
                    or "left the mapped material region" in str(exc)
                    or "localization is ambiguous" in str(exc)
                ):
                    self._publish_diagnostic(
                        "atlas return",
                        f"Atlas localization was lost while returning to the "
                        f"fixed {target:.3f}px target ({exc}); stopping this "
                        "atlas attempt instead of changing targets without a "
                        "known visual position.",
                    )
                    raise RuntimeError(
                        "Atlas localization was lost during partial-atlas "
                        f"return to {target:.3f}px."
                    ) from exc
                # Adaptive recovery has already tried stronger motion before
                # reaching this branch.  Only now may the target move back to
                # the preceding trustworthy atlas node.
                self._publish_diagnostic(
                    "atlas return",
                    f"Atlas node {target:.3f}px could not be recovered after "
                    f"adaptive positioning ({exc}). Trying the preceding "
                    "trustworthy node.",
                )
                continue

            retained_nodes = list(nodes[:target_index + 1])
            retained_segments = [
                segment for segment in segments
                if segment.right.position <= target
            ]
            self.atlas = retained_nodes
            self.atlas_segments = retained_segments
            self.measurements[:] = [
                item for item in self.measurements
                if not (
                    item.atlas_attempt_id == attempt_id
                    and item.stage == "atlas_build"
                    and (not item.valid or item.position > target)
                )
            ]
            if self.progress is not None:
                self.progress(ObjectiveAtlasAttemptUpdate(
                    attempt_id=int(attempt_id),
                    microstep=int(failed_microstep),
                    action="trim",
                    reason=(
                        "Unreachable atlas tail removed; continuing from "
                        f"{target:.3f}px with a finer MICRO."
                    ),
                    retain_through=float(target),
                ))
            self._set_visual_position(
                position,
                stage="atlas_tail_returned",
                target=target,
            )
            return retained_nodes, retained_segments, float(position)

        raise RuntimeError(
            "Could not return to any trustworthy node in the partial atlas."
        )

    def _build_visual_atlas(
        self,
        microstep: int,
        *,
        attempt_id: int,
        outside_margin_full_steps: int,
        max_search_full_steps: int,
        confirmation_positions: int,
    ) -> float:
        """Build a left-to-right atlas whose coordinate is image travel in px."""
        active_microstep = int(microstep)
        self._set_microstep(active_microstep)
        self._set_visual_position(None, stage="atlas_edge_search")
        frame_id, frame, similarity = self._find_visual_edge(
            1,
            outside_margin_full_steps=outside_margin_full_steps,
            max_search_full_steps=max_search_full_steps,
            confirmation_positions=confirmation_positions,
        )
        self._set_visual_position(0.0, stage="atlas_build")
        nodes = [_AtlasNode(
            position=0.0,
            frame=self._prepare_atlas_frame(frame),
            frame_id=frame_id,
            reference_similarity=similarity,
        )]
        self._publish_atlas_step(
            attempt_id=attempt_id,
            stage="atlas_build",
            position=0.0,
            frame_id=frame_id,
            similarity=similarity,
            displacement=0.0,
            confidence=1.0,
            overlap=1.0,
            valid=True,
            status="ATLAS_BUILD_ANCHOR",
        )
        segments: list[_AtlasSegment] = []
        pending_reference_samples = [(0.0, frame_id, similarity)]
        pixel_scale_samples: list[float] = []
        forward_axis: tuple[float, float] | None = None
        pulses_since_node = 0
        motor_units_since_node = 0.0

        outside_count = 0
        pulse_index = 0

        def refine_from_last_reachable_node(
            reason: str,
            failed_position_hint: float,
        ) -> None:
            nonlocal active_microstep, nodes, segments
            nonlocal pending_reference_samples, outside_count
            nonlocal pulses_since_node, motor_units_since_node

            mode_index = self.MICROSTEP_MODES.index(active_microstep)
            if mode_index + 1 >= len(self.MICROSTEP_MODES):
                raise _AtlasStepRegistrationError(reason)
            if len(nodes) < 2 or not segments:
                next_microstep = self.MICROSTEP_MODES[mode_index + 1]
                restart_reason = (
                    f"{reason} Only the 0px anchor exists, so there is no "
                    "atlas segment available for visual return. Discarding "
                    f"this attempt and rebuilding from the left edge at "
                    f"MICRO {next_microstep}."
                )
                self._publish_diagnostic("atlas build", restart_reason)
                raise _AtlasStepRegistrationError(restart_reason)
            try:
                nodes, segments, _returned_position = (
                    self._recover_partial_atlas_tail(
                        nodes,
                        segments,
                        active_microstep,
                        attempt_id,
                        failed_position_hint=failed_position_hint,
                    )
                )
            except ObjectiveCalibrationCancelled:
                raise
            except RuntimeError as exc:
                # Recovery may temporarily select a stronger microstep mode.
                # Restore the mode that actually failed to build the atlas so
                # the outer rebuild loop advances to its next finer mode.
                self._set_microstep(active_microstep)
                next_microstep = self.MICROSTEP_MODES[mode_index + 1]
                restart_reason = (
                    f"Partial-atlas return failed at MICRO "
                    f"{active_microstep} ({exc}). Discarding this map, "
                    f"reacquiring the left edge, and rebuilding at MICRO "
                    f"{next_microstep}."
                )
                self._publish_diagnostic("atlas build", restart_reason)
                raise _AtlasStepRegistrationError(restart_reason) from exc
            retained_position = nodes[-1].position
            pending_reference_samples = [
                sample for sample in pending_reference_samples
                if sample[0] <= retained_position
            ]
            active_microstep = self.MICROSTEP_MODES[mode_index + 1]
            self._set_microstep(active_microstep)
            if self.progress is not None:
                self.progress(ObjectiveAtlasAttemptUpdate(
                    attempt_id=int(attempt_id),
                    microstep=int(active_microstep),
                    action="refine",
                    reason=(
                        f"Partial atlas retained through "
                        f"{retained_position:.3f}px; continuing at "
                        f"MICRO {active_microstep}."
                    ),
                    retain_through=float(retained_position),
                ))
            self._set_visual_position(
                retained_position,
                stage="atlas_build",
            )
            outside_count = 0
            pulses_since_node = 0
            motor_units_since_node = 0.0

        while pulse_index < max_search_full_steps * active_microstep:
            self._move_pulses(1, settle_seconds=self.move_settle_seconds)
            pulse_index += 1
            pulses_since_node += 1
            motor_units_since_node += self._units_per_pulse(
                active_microstep
            )
            frame_id, frame, similarity = self._reference_sample()
            predicted_position = (
                nodes[-1].position
                + pulses_since_node
                * self._estimated_pixels_per_pulse(active_microstep)
            )
            if similarity >= self.reference_threshold:
                prepared_frame = self._prepare_atlas_frame(frame)
                candidate = _AtlasNode(
                    position=predicted_position,
                    frame=prepared_frame,
                    frame_id=frame_id,
                    reference_similarity=similarity,
                )
                try:
                    registration = measure_image_displacement(
                        nodes[-1].frame,
                        candidate.frame,
                        roi_fraction=1.0,
                    )
                except (cv2.error, ValueError):
                    registration = None
                if registration is None:
                    self._publish_atlas_step(
                        attempt_id=attempt_id,
                        stage="atlas_build",
                        position=predicted_position,
                        frame_id=frame_id,
                        similarity=similarity,
                        displacement=float("nan"),
                        confidence=0.0,
                        overlap=0.0,
                        valid=False,
                        status="ATLAS_BUILD_REGISTRATION_ERROR",
                    )
                    self._wait(0.25)
                    refine_from_last_reachable_node(
                        "Adjacent atlas images were not trustworthy at "
                        f"pulse {pulse_index} using MICRO {active_microstep}.",
                        predicted_position,
                    )
                    continue
                dx, dy, distance, confidence, overlap = registration
                if (
                    confidence < self.min_confidence
                    or overlap < self.min_overlap
                ):
                    status = (
                        "ATLAS_BUILD_LOW_CONFIDENCE"
                        if confidence < self.min_confidence
                        else "ATLAS_BUILD_LOW_OVERLAP"
                    )
                    self._publish_atlas_step(
                        attempt_id=attempt_id,
                        stage="atlas_build",
                        position=predicted_position,
                        frame_id=frame_id,
                        similarity=similarity,
                        displacement=distance,
                        confidence=confidence,
                        overlap=overlap,
                        valid=False,
                        status=status,
                    )
                    self._wait(0.25)
                    refine_from_last_reachable_node(
                        "Adjacent atlas images were not trustworthy at "
                        f"pulse {pulse_index} using MICRO {active_microstep}: "
                        f"confidence={confidence:.3f}, overlap={overlap:.3f}.",
                        predicted_position,
                    )
                    continue
                if distance < self.atlas_merge_distance_px:
                    # A detent can hold the turret through several pulses.
                    # Treat visually coincident frames as one atlas node and
                    # wait until their cumulative separation is meaningful.
                    continue
                if forward_axis is None:
                    forward_axis = (dx / distance, dy / distance)
                signed_progress = (
                    dx * forward_axis[0] + dy * forward_axis[1]
                )
                if signed_progress <= 0.20:
                    # A detent snap can send the image backwards even though
                    # the commanded scan direction is forward.  It must not
                    # extend the monotonic forward atlas.
                    continue
                position = float(nodes[-1].position + distance)
                candidate = _AtlasNode(
                    position=position,
                    frame=prepared_frame,
                    frame_id=frame_id,
                    reference_similarity=similarity,
                )
                segments.append(_AtlasSegment(
                    left=nodes[-1],
                    right=candidate,
                    dx=dx,
                    dy=dy,
                    confidence=confidence,
                    overlap=overlap,
                ))
                nodes.append(candidate)
                self._publish_atlas_step(
                    attempt_id=attempt_id,
                    stage="atlas_build",
                    position=position,
                    frame_id=frame_id,
                    similarity=similarity,
                    displacement=distance,
                    confidence=confidence,
                    overlap=overlap,
                    valid=True,
                    status="ATLAS_BUILD_VALID",
                )
                pixel_scale_samples.append(
                    distance
                    / max(1.0, motor_units_since_node)
                )
                self.atlas_pixels_per_motor_unit = float(np.median(
                    pixel_scale_samples
                ))
                pulses_since_node = 0
                motor_units_since_node = 0.0
                pending_reference_samples.append((
                    position, frame_id, similarity
                ))
                self._set_visual_position(position, stage="atlas_build")
            else:
                pending_reference_samples.append((
                    predicted_position, frame_id, similarity
                ))
            if similarity < self.edge_exit_threshold:
                outside_count += 1
                if outside_count >= confirmation_positions:
                    break
            else:
                outside_count = 0
        else:
            raise RuntimeError(
                "Could not find the right edge while building the image map."
            )

        if len(nodes) < 3:
            raise RuntimeError(
                "Too few matched images were available to build a position map."
            )
        right_position = nodes[-1].position
        if len(nodes) < 2 or right_position <= 0:
            raise RuntimeError("The matched image region is too narrow")
        if len(segments) != len(nodes) - 1:
            raise RuntimeError(
                "The atlas did not form one continuous registered position map."
            )
        self.atlas = nodes
        self.atlas_segments = segments
        for position, sample_frame_id, sample_similarity in (
            pending_reference_samples
        ):
                self._publish_reference(
                    position,
                    sample_frame_id,
                    sample_similarity,
                    "atlas",
                    atlas_attempt_id=attempt_id,
                )
        return float(right_position)

    def _localize_in_atlas(
        self,
        frame: np.ndarray,
        *,
        previous_position: float | None = None,
    ) -> tuple[float, float]:
        """Locate a frame on the cumulative pixel-displacement atlas."""
        candidate = self._prepare_atlas_frame(frame)
        best = None
        typical_spacing = float(np.median([
            segment.right.position - segment.left.position
            for segment in self.atlas_segments
        ]))
        for segment in self.atlas_segments:
            vector_norm_sq = segment.dx ** 2 + segment.dy ** 2
            if vector_norm_sq < 0.04:
                continue
            try:
                dx, dy, _distance, confidence, overlap = (
                    measure_image_displacement(
                        segment.left.frame, candidate, roi_fraction=1.0
                    )
                )
            except (cv2.error, ValueError):
                continue
            if overlap < self.min_overlap or confidence < self.min_confidence:
                continue
            alpha = (dx * segment.dx + dy * segment.dy) / vector_norm_sq
            projected_dx = alpha * segment.dx
            projected_dy = alpha * segment.dy
            residual = float(np.hypot(
                dx - projected_dx, dy - projected_dy
            ))
            outside_penalty = max(0.0, -alpha, alpha - 1.0)
            visual_position = segment.left.position + alpha * (
                segment.right.position - segment.left.position
            )
            prior_penalty = 0.0
            if previous_position is not None:
                prior_penalty = min(
                    0.30,
                    abs(visual_position - previous_position)
                    / max(1.0, 8.0 * typical_spacing)
                    * 0.10,
                )
            score = (
                confidence
                - min(0.50, residual / 40.0)
                - 0.35 * outside_penalty
                - prior_penalty
            )
            if best is None or score > best[0]:
                best = (score, visual_position, confidence, residual, alpha)
        if best is None:
            raise RuntimeError("Current image could not be placed in the atlas")
        score, visual_position, confidence, residual, alpha = best
        if score < -0.10 or residual > 30.0 or not -2.0 <= alpha <= 3.0:
            raise RuntimeError(
                "Image-atlas localization is ambiguous: "
                f"score={score:.3f}, residual={residual:.3f}, "
                f"alpha={alpha:.3f}, confidence={confidence:.3f}"
            )
        return float(visual_position), float(max(0.0, min(1.0, confidence)))

    def _insert_localized_atlas_frame(
        self,
        position: float,
        frame: np.ndarray,
        frame_id: int,
        similarity: float,
    ) -> bool:
        """Insert a trustworthy reverse-scan frame at a measured pixel position."""
        position = float(position)
        if (
            len(self.atlas) < 2
            or position <= self.atlas[0].position
            or position >= self.atlas[-1].position
        ):
            return False
        insertion_index = 0
        while (
            insertion_index < len(self.atlas)
            and self.atlas[insertion_index].position < position
        ):
            insertion_index += 1

        left = (
            self.atlas[insertion_index - 1]
            if insertion_index > 0 else None
        )
        right = (
            self.atlas[insertion_index]
            if insertion_index < len(self.atlas) else None
        )
        # Nearly coincident frames do not add useful positional resolution and
        # would create zero-length atlas segments.
        if (
            left is not None
            and position - left.position < self.atlas_merge_distance_px
        ):
            return False
        if (
            right is not None
            and right.position - position < self.atlas_merge_distance_px
        ):
            return False

        candidate = _AtlasNode(
            position=position,
            frame=self._prepare_atlas_frame(frame),
            frame_id=frame_id,
            reference_similarity=similarity,
        )
        new_segments = []
        for first, second in ((left, candidate), (candidate, right)):
            if first is None or second is None:
                continue
            registration = self._prepared_adjacent_registration(
                first.frame, second.frame
            )
            if registration is None:
                return False
            dx, dy, _distance, confidence, overlap = registration
            if dx * dx + dy * dy < 0.04:
                return False
            new_segments.append(_AtlasSegment(
                left=first,
                right=second,
                dx=dx,
                dy=dy,
                confidence=confidence,
                overlap=overlap,
            ))

        if left is not None and right is not None:
            self.atlas_segments = [
                segment for segment in self.atlas_segments
                if not (segment.left is left and segment.right is right)
            ]
        self.atlas.insert(insertion_index, candidate)
        self.atlas_segments.extend(new_segments)
        self.atlas_segments.sort(key=lambda segment: segment.left.position)
        self._publish_reference(position, frame_id, similarity, "atlas")
        return True

    def _signed_image_displacement(
        self,
        position: float,
        dx: float,
        dy: float,
        displacement: float,
        disabled_position: float | None,
    ) -> float:
        """Return release distance signed along the one-dimensional atlas."""
        if disabled_position is not None and np.isfinite(disabled_position):
            return float(disabled_position - position)
        if not (
            np.isfinite(dx) and np.isfinite(dy) and np.isfinite(displacement)
        ):
            return float("nan")
        if not self.atlas_segments:
            return float("nan")
        segment = min(
            self.atlas_segments,
            key=lambda item: (
                0.0
                if item.left.position <= position <= item.right.position
                else min(
                    abs(position - item.left.position),
                    abs(position - item.right.position),
                )
            ),
        )
        dot = dx * segment.dx + dy * segment.dy
        if abs(dot) < 1e-9:
            return 0.0
        return float(np.copysign(displacement, dot))

    def _merge_release_points(
        self,
        measurements: list[ObjectiveMeasurement],
    ) -> list[_MergedReleasePoint]:
        """Merge nearby valid measurements without mixing scan directions."""
        usable = sorted(
            (
                item for item in measurements
                if item.valid
                and np.isfinite(item.displacement)
                and np.isfinite(item.signed_displacement)
            ),
            key=lambda item: item.position,
        )
        groups: list[list[ObjectiveMeasurement]] = []
        for item in usable:
            if not groups:
                groups.append([item])
                continue
            center = float(np.median([
                member.position for member in groups[-1]
            ]))
            if abs(item.position - center) <= self.atlas_merge_distance_px:
                groups[-1].append(item)
            else:
                groups.append([item])

        merged = []
        for group in groups:
            disabled_positions = [
                item.disabled_visual_position for item in group
                if item.disabled_visual_position is not None
                and np.isfinite(item.disabled_visual_position)
            ]
            merged.append(_MergedReleasePoint(
                position=float(np.median([
                    item.position for item in group
                ])),
                displacement=float(np.median([
                    item.displacement for item in group
                ])),
                signed_displacement=float(np.median([
                    item.signed_displacement for item in group
                ])),
                disabled_visual_position=(
                    float(np.median(disabled_positions))
                    if disabled_positions else None
                ),
            ))
        return merged

    def _record_failed_location(
        self,
        guessed_position: float,
        frame_id: int,
        similarity: float,
        stage: str,
        scan_direction: int = 0,
    ) -> None:
        self._publish(ObjectiveMeasurement(
            position=float(guessed_position),
            displacement=float("nan"),
            dx=float("nan"),
            dy=float("nan"),
            confidence=0.0,
            overlap=0.0,
            valid=False,
            stage=stage,
            enabled_frame_id=frame_id,
            reference_similarity=similarity,
            status=(
                "SCENE_MISMATCH"
                if similarity < self.reference_threshold
                else "LOCALIZATION_FAILED"
            ),
            commanded_position=self.commanded_position_units,
            localization_confidence=0.0,
            microstep=self.current_microstep,
            scan_direction=int(scan_direction),
        ))

    def _record_skipped_target(
        self,
        error: _VisualTargetUnreachable,
        stage: str,
    ) -> ObjectiveMeasurement:
        return self._publish(ObjectiveMeasurement(
            position=error.target,
            displacement=float("nan"),
            dx=float("nan"),
            dy=float("nan"),
            confidence=0.0,
            overlap=0.0,
            valid=False,
            stage=stage,
            enabled_frame_id=error.frame_id,
            reference_similarity=error.similarity,
            status="POSITION_SKIPPED",
            commanded_position=self.commanded_position_units,
            localization_confidence=error.confidence,
            microstep=self.current_microstep,
        ))

    def _measure_release(
        self,
        visual_position: float,
        localization_confidence: float,
        enabled_frame_id: int,
        enabled_frame: np.ndarray,
        enabled_similarity: float,
        stage: str,
        scan_direction: int = 0,
    ) -> ObjectiveMeasurement:
        self.motor.objective_disable()
        self._wait(self.release_settle_seconds)
        disabled_frame_id, disabled_frame, disabled_similarity = (
            self._reference_sample()
        )
        reference_similarity = min(enabled_similarity, disabled_similarity)
        if reference_similarity < self.reference_threshold:
            dx = dy = displacement = float("nan")
            confidence = overlap = 0.0
            valid = False
            status = "SCENE_MISMATCH"
        else:
            dx, dy, displacement, confidence, overlap = (
                measure_image_displacement(
                    enabled_frame,
                    disabled_frame,
                    roi_fraction=self.roi_fraction,
                )
            )
            # The first disabled frame can still contain motion blur from the
            # fast detent release.  Wait once more and keep the clearer result
            # instead of repeatedly measuring the same motor position.
            if confidence < self.min_confidence:
                self._wait(self.precision_settle_seconds)
                retry_frame_id, retry_frame, retry_similarity = (
                    self._reference_sample()
                )
                if retry_similarity >= self.reference_threshold:
                    retry_values = measure_image_displacement(
                        enabled_frame,
                        retry_frame,
                        roi_fraction=self.roi_fraction,
                    )
                    if retry_values[3] > confidence:
                        dx, dy, displacement, confidence, overlap = retry_values
                        disabled_frame_id = retry_frame_id
                        disabled_frame = retry_frame
                        disabled_similarity = retry_similarity
                        reference_similarity = min(
                            enabled_similarity, disabled_similarity
                        )
            if confidence < self.min_confidence:
                valid = False
                status = "LOW_CONFIDENCE"
            elif overlap < self.min_overlap:
                valid = False
                status = "LOW_OVERLAP"
            elif displacement > self.max_valid_displacement:
                # A large, well-registered release is useful evidence for a
                # detent peak.  Keep it valid and mark it separately in the
                # plot instead of discarding it.
                valid = True
                status = "LARGE_MOVE"
            else:
                valid = True
                status = "VALID"
        disabled_visual_position = None
        if reference_similarity >= self.reference_threshold:
            try:
                disabled_visual_position, _disabled_location_confidence = (
                    self._localize_in_atlas(
                        disabled_frame,
                        previous_position=visual_position,
                    )
                )
            except RuntimeError:
                pass
        signed_displacement = self._signed_image_displacement(
            visual_position,
            dx,
            dy,
            displacement,
            disabled_visual_position,
        )
        item = ObjectiveMeasurement(
            position=float(visual_position),
            displacement=displacement,
            dx=dx,
            dy=dy,
            confidence=confidence,
            overlap=overlap,
            valid=valid,
            stage=stage,
            enabled_frame_id=enabled_frame_id,
            disabled_frame_id=disabled_frame_id,
            disabled_visual_position=disabled_visual_position,
            reference_similarity=reference_similarity,
            status=status,
            commanded_position=self.commanded_position_units,
            localization_confidence=localization_confidence,
            microstep=self.current_microstep,
            signed_displacement=signed_displacement,
            scan_direction=int(scan_direction),
        )
        if np.isfinite(displacement) and displacement >= self.peak_threshold:
            self.peak_frames.append((
                float(visual_position),
                self._prepare_atlas_frame(enabled_frame),
                self._prepare_atlas_frame(disabled_frame),
            ))
        return self._publish(item)

    def _release_scan_pass(
        self,
        direction: int,
        microstep: int,
        match_width: float,
        *,
        outside_margin_full_steps: int,
        max_search_full_steps: int,
        confirmation_positions: int,
        stage: str,
        scan_bounds: tuple[float, float] | None = None,
    ) -> list[ObjectiveMeasurement]:
        """Scan once, inserting every release sample into visual coordinates."""
        self._set_microstep(microstep)
        self._set_visual_position(None, stage=f"{stage}_edge_search")
        self._find_visual_edge(
            direction,
            outside_margin_full_steps=outside_margin_full_steps,
            max_search_full_steps=max_search_full_steps,
            confirmation_positions=confirmation_positions,
            already_outside=True,
        )
        if scan_bounds is None:
            scan_lower, scan_upper = 0.0, float(match_width)
        else:
            scan_lower = float(np.clip(scan_bounds[0], 0.0, match_width))
            scan_upper = float(np.clip(scan_bounds[1], 0.0, match_width))
            if scan_lower >= scan_upper:
                raise RuntimeError(
                    "The requested release-rescan pixel window is empty"
                )
        active_microstep = microstep
        pixel_step = self._estimated_pixels_per_pulse(active_microstep)
        move_pulses = 1
        edge_position = 0.0 if direction > 0 else float(match_width)
        start_guess = edge_position
        guess = start_guess
        self._set_visual_position(start_guess, stage=stage)
        previous_visual = None
        if scan_bounds is not None:
            entrance = scan_lower if direction > 0 else scan_upper
            self._publish_diagnostic(
                "release rescan",
                f"MICRO {microstep}: skipping known quiet image regions; "
                f"moving from the {edge_position:.3f}px edge to the "
                f"[{scan_lower:.3f}, {scan_upper:.3f}]px rescan window.",
            )
            if abs(entrance - edge_position) > max(0.50, 0.5 * pixel_step):
                self._set_microstep(1)
                full_step_pixels = self._estimated_pixels_per_pulse(1)
                (
                    _approach_frame_id,
                    _approach_frame,
                    _approach_similarity,
                    approach_position,
                    _approach_confidence,
                ) = self._seek_inside_atlas(
                    entrance,
                    edge_position,
                    max_pulses=max(
                        32,
                        int(np.ceil(
                            abs(entrance - edge_position)
                            / max(0.05, full_step_pixels)
                        )) + 16,
                    ),
                    max_command_pulses=16,
                    tolerance=max(0.50, 0.75 * full_step_pixels),
                    stage=f"{stage}_window_approach",
                    adaptive_recovery=True,
                    accept_target_crossing=True,
                )
                previous_visual = approach_position
                guess = approach_position
            else:
                previous_visual = edge_position
                guess = edge_position
            self._set_microstep(microstep)
            active_microstep = microstep
            pixel_step = self._estimated_pixels_per_pulse(active_microstep)
            self._set_visual_position(
                guess,
                stage=stage,
                target=(scan_upper if direction > 0 else scan_lower),
            )
        last_measured_visual = None
        best_progress_visual = None
        stagnant_pulses = 0
        seen_match = False
        outside_after_match = 0
        results = []
        last_large_release_visual = None
        completed_peak_search = False
        maximum_samples = (
            int(np.ceil((scan_upper - scan_lower) / pixel_step))
            + max(16, outside_margin_full_steps * microstep)
            + 2 * confirmation_positions
        )

        reached_other_edge = False
        completed_scan_window = False
        for _ in range(maximum_samples):
            released_this_sample = False
            resumed_after_release = False
            self.motor.objective_enable()
            self._wait(self.enable_settle_seconds)
            frame_id, enabled_frame, similarity = self._reference_sample()
            if similarity >= self.reference_threshold:
                seen_match = True
                outside_after_match = 0
                try:
                    visual_position, location_confidence = (
                        self._localize_in_atlas(
                            enabled_frame,
                            previous_position=previous_visual,
                        )
                    )
                except RuntimeError:
                    self._record_failed_location(
                        guess,
                        frame_id,
                        similarity,
                        stage,
                        scan_direction=direction,
                    )
                else:
                    previous_visual = visual_position
                    self._set_visual_position(visual_position, stage=stage)
                    scan_exit = scan_upper if direction > 0 else scan_lower
                    if (
                        scan_bounds is not None
                        and direction * (visual_position - scan_exit)
                        > max(0.50, 0.35 * pixel_step)
                    ):
                        completed_scan_window = True
                        break
                    self._insert_localized_atlas_frame(
                        visual_position,
                        enabled_frame,
                        frame_id,
                        similarity,
                    )
                    if best_progress_visual is None:
                        best_progress_visual = visual_position
                    progress = direction * (
                        visual_position - best_progress_visual
                    )
                    if progress >= max(0.35 * pixel_step, 0.5):
                        best_progress_visual = visual_position
                        stagnant_pulses = 0
                    else:
                        stagnant_pulses += 1

                    spacing_ok = (
                        last_measured_visual is None
                        or direction * (
                            visual_position - last_measured_visual
                        ) >= max(0.35 * pixel_step, 0.5)
                    )
                    if spacing_ok:
                        measurement = self._measure_release(
                            visual_position,
                            location_confidence,
                            frame_id,
                            enabled_frame,
                            similarity,
                            stage,
                            scan_direction=direction,
                        )
                        results.append(measurement)
                        released_this_sample = True
                        last_measured_visual = visual_position

                        if (
                            scan_bounds is not None
                            and direction * (visual_position - scan_exit) >= 0
                        ):
                            completed_scan_window = True
                            break

                        if (
                            measurement.valid
                            and np.isfinite(measurement.signed_displacement)
                            and abs(measurement.signed_displacement)
                            >= self.peak_threshold
                        ):
                            last_large_release_visual = visual_position

                        # Once this pass contains the expected positive/negative
                        # release peaks, a long quiet tail cannot improve the
                        # between-peaks estimate.  Measure the tail in atlas
                        # pixels because motor pulses are unreliable while the
                        # detent is applying an external force.
                        if (
                            self.post_peak_quiet_distance_px > 0
                            and last_large_release_visual is not None
                            and direction * (
                                visual_position - last_large_release_visual
                            ) >= self.post_peak_quiet_distance_px
                        ):
                            current_regions = self._peak_regions(
                                results,
                                max_gap=max(1.0, 2.2 * pixel_step),
                            )
                            if self._choose_peak_pair(
                                current_regions, results
                            ) is not None:
                                completed_peak_search = True
                                break

                        disabled_position = (
                            measurement.disabled_visual_position
                        )
                        resume_distance = pixel_step * move_pulses
                        resume_target = (
                            visual_position + direction * resume_distance
                        )
                        release_jump = (
                            0.0 if disabled_position is None else
                            disabled_position - visual_position
                        )
                        large_release_jump = (
                            abs(release_jump)
                            > max(2.0, 1.5 * pixel_step)
                        )
                        if (
                            large_release_jump
                            and 0.0 <= resume_target <= match_width
                        ):
                            # A large release in either direction must not skip
                            # visual samples.  If release went backward, seek
                            # forward past this sample.  If it jumped forward
                            # beyond the next sample, the same absolute target
                            # makes the seeker issue reverse commands.
                            snap_direction = (
                                "forward"
                                if direction * release_jump > 0
                                else "backward"
                            )
                            self._publish_diagnostic(
                                "release scan return",
                                f"{stage}: {snap_direction} release jump from "
                                f"{visual_position:.3f}px to "
                                f"{disabled_position:.3f}px; returning to "
                                f"the next scan target {resume_target:.3f}px.",
                            )
                            pulse_distance = abs(
                                disabled_position - resume_target
                            )
                            pulse_budget = (
                                int(np.ceil(pulse_distance / pixel_step))
                                + 8
                            )
                            command_pulses = max(
                                2,
                                min(
                                    16,
                                    int(np.ceil(pulse_distance / pixel_step)),
                                ),
                            )
                            (
                                _resume_frame_id,
                                _resume_frame,
                                _resume_similarity,
                                resumed_position,
                                _resume_confidence,
                            ) = self._seek_inside_atlas(
                                resume_target,
                                disabled_position,
                                max_pulses=pulse_budget,
                                max_command_pulses=command_pulses,
                                tolerance=max(0.50, 0.75 * pixel_step),
                                stage=f"{stage}_resume",
                                adaptive_recovery=True,
                                accept_target_crossing=True,
                            )
                            # Adaptive recovery may have selected a stronger
                            # microstep mode.  Continue the scan using the
                            # controller's real mode and its pixel estimate.
                            active_microstep = self.current_microstep
                            pixel_step = self._estimated_pixels_per_pulse(
                                active_microstep
                            )
                            previous_visual = resumed_position
                            guess = resumed_position
                            resumed_after_release = True
                            resumed_progress = direction * (
                                resumed_position - best_progress_visual
                            )
                            if resumed_progress >= max(
                                0.35 * pixel_step, 0.5
                            ):
                                best_progress_visual = resumed_position
                                stagnant_pulses = 0

                    maximum_stagnant = max(12, 4 * active_microstep)
                    if stagnant_pulses >= maximum_stagnant:
                        mode_index = self.MICROSTEP_MODES.index(
                            active_microstep
                        )
                        if mode_index > 0:
                            # A finer subdivision can lack enough incremental
                            # torque to move the turret.  Fall back to the next
                            # stronger mode and cross the stuck section in a
                            # small pulse group.  Visual localization, rather
                            # than the skipped pulse positions, establishes the
                            # next trustworthy sample coordinate.
                            active_microstep = self.MICROSTEP_MODES[
                                mode_index - 1
                            ]
                            self._set_microstep(active_microstep)
                            pixel_step = self._estimated_pixels_per_pulse(
                                active_microstep
                            )
                            move_pulses = min(4, max(2, move_pulses * 2))
                            stagnant_pulses = 0
                            best_progress_visual = visual_position
                        elif move_pulses < 4:
                            # Full-step mode is already the strongest setting;
                            # increase the distance of one recovery command.
                            move_pulses = min(4, move_pulses * 2)
                            stagnant_pulses = 0
                            best_progress_visual = visual_position
                        else:
                            raise RuntimeError(
                                "The objective motor still produced no visual "
                                "progress after falling back to MICRO 1 with "
                                f"{move_pulses}-pulse recovery moves. Check "
                                "motor current, load, and missed steps."
                            )
            else:
                if seen_match:
                    outside_after_match += 1
                    if outside_after_match >= confirmation_positions:
                        reached_other_edge = True
                        break
            if released_this_sample:
                if resumed_after_release:
                    continue
                else:
                    self.motor.objective_enable()
                    self._wait(self.enable_settle_seconds)
            self._move_pulses(
                direction * move_pulses,
                settle_seconds=self.precision_settle_seconds,
            )
            guess += direction * pixel_step * move_pulses
        if (
            not reached_other_edge
            and not completed_peak_search
            and not completed_scan_window
        ):
            raise RuntimeError(
                "Release scan did not reach the opposite image boundary. "
                "The motor may be slipping or returning to the same position."
            )
        return results

    def _peak_regions(
        self,
        measurements: list[ObjectiveMeasurement],
        max_gap: float,
    ) -> list[tuple[float, float, int, float]]:
        """Return signed peak regions from one scan pass and direction."""
        peak_points = [
            point for point in self._merge_release_points(measurements)
            if abs(point.signed_displacement) >= self.peak_threshold
        ]
        if not peak_points:
            return []
        regions: list[tuple[float, float, int, float]] = []
        first = peak_points[0]
        start = previous = first.position
        sign = 1 if first.signed_displacement > 0 else -1
        peak_size = abs(first.signed_displacement)
        for point in peak_points[1:]:
            point_sign = 1 if point.signed_displacement > 0 else -1
            if point.position - previous > max_gap or point_sign != sign:
                regions.append((
                    float(start), float(previous), sign, float(peak_size)
                ))
                start = point.position
                sign = point_sign
                peak_size = abs(point.signed_displacement)
            else:
                peak_size = max(peak_size, abs(point.signed_displacement))
            previous = point.position
        regions.append((
            float(start), float(previous), sign, float(peak_size)
        ))
        return regions

    def _choose_peak_pair(
        self,
        peaks: list[tuple[float, float, int, float]],
        measurements: list[ObjectiveMeasurement],
    ) -> tuple[
        tuple[float, float, int, float],
        tuple[float, float, int, float],
        float,
    ] | None:
        """Choose opposite signed peaks surrounding a low-release valley."""
        curve = self._merge_release_points(measurements)
        candidates = []
        for left, right in zip(peaks, peaks[1:]):
            # On the left side of a stable detent, release must move toward
            # increasing atlas position; on the right it must move back.
            if not (left[2] > 0 and right[2] < 0):
                continue
            valley_start = left[1]
            valley_stop = right[0]
            valley = [
                point for point in curve
                if valley_start <= point.position <= valley_stop
            ]
            if valley_start > valley_stop or not valley:
                continue
            valley_best = min(
                valley, key=lambda point: abs(point.signed_displacement)
            )
            if abs(valley_best.signed_displacement) > self.peak_threshold:
                continue
            release_endpoints = [
                point.disabled_visual_position for point in valley
                if point.disabled_visual_position is not None
            ]
            if release_endpoints:
                stable_target = float(np.median(release_endpoints))
                endpoint_spread = float(np.median(np.abs(
                    np.asarray(release_endpoints) - stable_target
                )))
            else:
                stable_target = float(valley_best.position)
                endpoint_spread = self.peak_threshold
            stable_target = float(np.clip(
                stable_target, valley_start, valley_stop
            ))
            score = (
                abs(valley_best.signed_displacement) + endpoint_spread
            )
            candidates.append((score, left, right, stable_target))
        if not candidates:
            return None
        _score, left, right, stable_target = min(
            candidates, key=lambda item: item[0]
        )
        return left, right, stable_target

    def _seek_inside_atlas(
        self,
        target: float,
        previous_position: float | None,
        *,
        max_pulses: int,
        max_command_pulses: int = 1,
        tolerance: float = 0.85,
        stage: str = "fine_position",
        max_target_crossings: int | None = None,
        adaptive_recovery: bool = False,
        accept_target_crossing: bool = False,
    ) -> tuple[int, np.ndarray, float, float, float]:
        """Reach a visual coordinate without leaving the mapped material view."""
        max_command_pulses = max(1, int(max_command_pulses))
        tolerance = max(0.05, float(tolerance))
        self.movement_stage = stage
        self.movement_target = float(target)
        self.motor.objective_enable()
        self._wait(self.enable_settle_seconds)
        frame_id, frame, similarity = self._reference_sample()
        if similarity < self.reference_threshold:
            raise RuntimeError(
                "The image left the mapped material region during fine tuning"
            )
        position, confidence = self._localize_in_atlas(
            frame, previous_position=previous_position
        )
        self._set_visual_position(
            position,
            stage=stage,
            target=float(target),
        )
        previous_error = target - position
        target_crossings = 0
        # Atlas interpolation is sub-pixel, but camera noise can move the
        # estimate slightly.  Do not chatter across an already-close target.
        remaining_pulses = max(1, int(max_pulses))
        command_group_limit = max_command_pulses
        local_pixels_per_pulse = self._estimated_pixels_per_pulse(
            self.current_microstep
        )
        no_progress_attempts = 0
        while remaining_pulses > 0 or adaptive_recovery:
            if abs(position - target) <= tolerance:
                return frame_id, frame, similarity, position, confidence
            direction = 1 if target > position else -1
            pixel_step = (
                local_pixels_per_pulse
                if adaptive_recovery
                else self._estimated_pixels_per_pulse(self.current_microstep)
            )
            pulses_needed = max(
                1, int(np.ceil(abs(target - position) / pixel_step))
            )
            available_pulses = (
                remaining_pulses
                if remaining_pulses > 0
                else command_group_limit
            )
            pulses = min(
                available_pulses,
                command_group_limit,
                pulses_needed,
            )
            previous_position_before_move = position
            previous_absolute_error = abs(target - position)
            self._move_pulses(
                direction * pulses,
                settle_seconds=self.precision_settle_seconds,
            )
            remaining_pulses -= pulses
            frame_id, frame, similarity = self._reference_sample()
            if similarity < self.reference_threshold:
                raise RuntimeError(
                    "Fine tuning left the mapped material region"
                )
            position, confidence = self._localize_in_atlas(
                frame, previous_position=position
            )
            self._set_visual_position(
                position,
                stage=stage,
                target=float(target),
            )
            current_error = target - position
            if (
                accept_target_crossing
                and previous_error * current_error <= 0
            ):
                # A release-scan resume point is only a boundary separating
                # already sampled positions from new ones.  Landing beyond it
                # in the requested direction is sufficient and avoids
                # oscillating around an unnecessary exact coordinate.
                return frame_id, frame, similarity, position, confidence
            if adaptive_recovery:
                current_absolute_error = abs(current_error)
                progress_toward_target = (
                    previous_absolute_error - current_absolute_error
                )
                meaningful_progress = max(
                    0.20,
                    min(1.0, 0.10 * local_pixels_per_pulse),
                )
                if progress_toward_target >= meaningful_progress:
                    observed_pixels_per_pulse = (
                        abs(position - previous_position_before_move)
                        / max(1, pulses)
                    )
                    # Follow the locally observed motion without allowing one
                    # unusually large detent jump to dominate the next move.
                    observed_pixels_per_pulse = min(
                        observed_pixels_per_pulse,
                        4.0 * local_pixels_per_pulse,
                    )
                    local_pixels_per_pulse = max(
                        0.05,
                        0.5 * local_pixels_per_pulse
                        + 0.5 * observed_pixels_per_pulse,
                    )
                    needed_from_observed_progress = int(np.ceil(
                        current_absolute_error / local_pixels_per_pulse
                    ))
                    remaining_pulses = max(
                        remaining_pulses,
                        needed_from_observed_progress + 8,
                    )
                    no_progress_attempts = 0
                else:
                    no_progress_attempts += 1
                    if no_progress_attempts >= 3:
                        mode_index = self.MICROSTEP_MODES.index(
                            self.current_microstep
                        )
                        if mode_index > 0:
                            old_mode = self.current_microstep
                            stronger_mode = self.MICROSTEP_MODES[
                                mode_index - 1
                            ]
                            self._set_microstep(stronger_mode)
                            local_pixels_per_pulse *= (
                                self._units_per_pulse(stronger_mode)
                                / self._units_per_pulse(old_mode)
                            )
                        elif command_group_limit < 32:
                            command_group_limit = min(
                                32, max(2, command_group_limit * 2)
                            )
                        else:
                            raise RuntimeError(
                                "Visual recovery made no meaningful progress "
                                "after MICRO 1 and a 32-pulse command group. "
                                "Check motor current, load, and missed steps."
                            )
                        remaining_pulses = max(
                            remaining_pulses,
                            command_group_limit + 8,
                        )
                        no_progress_attempts = 0
            if previous_error * current_error < 0:
                target_crossings += 1
                if (
                    max_target_crossings is not None
                    and target_crossings >= max_target_crossings
                    and abs(current_error) > tolerance
                ):
                    raise _VisualTargetUnreachable(
                        target,
                        position,
                        target_crossings,
                        frame_id,
                        similarity,
                        confidence,
                    )
            previous_error = current_error
        if abs(position - target) <= tolerance:
            return frame_id, frame, similarity, position, confidence
        raise RuntimeError(
            f"Visual positioning during {stage} did not converge: "
            f"target={target:.3f}, last={position:.3f}, "
            f"MICRO={self.current_microstep}"
        )

    def _balance_scan(
        self,
        lower: float,
        upper: float,
        *,
        fine_step_units: int,
        map_width: float,
        starting_microstep: int,
        stable_hint: float | None,
    ) -> tuple[
        list[ObjectiveMeasurement],
        ObjectiveMeasurement,
        int,
        bool,
    ]:
        """Search between peaks, refining only when neighbor releases differ."""

        refinement_modes = []
        for mode in self.MICROSTEP_MODES:
            if mode < starting_microstep:
                continue
            refinement_modes.append(mode)
            if self._units_per_pulse(mode) <= fine_step_units:
                break
        if not refinement_modes:
            refinement_modes = [starting_microstep]

        # Use full steps for the initial transit.  Pulse count is estimated
        # from the pixel atlas, and the next camera frame corrects the result.
        full_step_pixels = self._estimated_pixels_per_pulse(1)
        coarse_pulse_budget = max(
            4,
            int(np.ceil(map_width / full_step_pixels)) + 4,
        )
        self._set_microstep(1)
        approach_target = float(np.clip(
            stable_hint if stable_hint is not None else lower,
            lower,
            upper,
        ))
        (
            _coarse_frame_id,
            _coarse_frame,
            _coarse_similarity,
            coarse_position,
            _coarse_confidence,
        ) = self._seek_inside_atlas(
            approach_target,
            None,
            max_pulses=coarse_pulse_budget,
            max_command_pulses=coarse_pulse_budget,
            tolerance=max(0.50, 0.5 * full_step_pixels),
            stage="balance_coarse_approach",
        )

        results: list[ObjectiveMeasurement] = []
        previous = coarse_position
        search_lower = float(lower)
        search_upper = float(upper)
        best: ObjectiveMeasurement | None = None
        final_microstep = starting_microstep
        neighbor_release_consistent = False

        def measure_target(target: float, stage: str, pixel_step: float):
            nonlocal previous
            try:
                frame_id, frame, similarity, position, confidence = (
                    self._seek_inside_atlas(
                        float(target),
                        previous,
                        max_pulses=max(
                            64,
                            int(np.ceil(
                                map_width / max(0.05, pixel_step)
                            )) + 32,
                        ),
                        max_command_pulses=16,
                        tolerance=max(0.50, 0.45 * pixel_step),
                        stage=stage,
                        max_target_crossings=3,
                    )
                )
            except _VisualTargetUnreachable as exc:
                results.append(self._record_skipped_target(exc, stage))
                previous = exc.last_position
                return None
            self._insert_localized_atlas_frame(
                position,
                frame,
                frame_id,
                similarity,
            )
            measurement = self._measure_release(
                position,
                confidence,
                frame_id,
                frame,
                similarity,
                stage,
            )
            results.append(measurement)
            if (
                measurement.disabled_visual_position is not None
                and abs(
                    measurement.disabled_visual_position - position
                ) > max(2.0, 1.5 * pixel_step)
            ):
                self._publish_diagnostic(
                    "balance return",
                    f"{stage}: release moved from {position:.3f}px to "
                    f"{measurement.disabled_visual_position:.3f}px. The next "
                    "absolute between-peaks target will be approached from "
                    "this measured disable position, including a reverse "
                    "move if the release jumped past it.",
                )
            previous = (
                measurement.disabled_visual_position
                if measurement.disabled_visual_position is not None
                else position
            )
            return measurement

        for refinement_index, mode in enumerate(refinement_modes):
            self._set_microstep(mode)
            pixel_step = self._estimated_pixels_per_pulse(mode)
            targets = list(np.arange(
                search_lower,
                search_upper + 0.25 * pixel_step,
                pixel_step,
            ))
            if not targets:
                targets = [search_lower]
            if targets[-1] < search_upper - 0.25 * pixel_step:
                targets.append(search_upper)
            if stable_hint is not None and search_lower <= stable_hint <= search_upper:
                targets.append(float(stable_hint))
                targets = sorted(set(targets))

            self._publish_diagnostic(
                "balance",
                f"MICRO {mode}: scanning {len(targets)} target(s) in "
                f"[{search_lower:.3f}, {search_upper:.3f}]px; estimated "
                f"one-pulse visual step={pixel_step:.3f}px.",
            )

            pass_measurements = []
            for target in targets:
                measurement = measure_target(
                    float(target), "balance_scan", pixel_step
                )
                if measurement is not None:
                    pass_measurements.append(measurement)

            usable = [item for item in pass_measurements if item.valid]
            if not usable:
                continue
            endpoint_positions = [
                item.disabled_visual_position for item in usable
                if item.disabled_visual_position is not None
                and np.isfinite(item.disabled_visual_position)
            ]
            if endpoint_positions:
                # A release landing point is a measured atlas coordinate.  It
                # may lie just outside the current refinement window, so only
                # constrain it to the complete between-peaks interval.  Clipping
                # it to search_lower/search_upper can move the target away from
                # the mechanical detent that the images actually identified.
                release_center = float(np.clip(
                    np.median(endpoint_positions),
                    lower,
                    upper,
                ))
            else:
                release_center = min(
                    usable,
                    key=lambda item: abs(item.signed_displacement),
                ).position

            nearest = min(
                usable, key=lambda item: abs(item.position - release_center)
            )
            if abs(nearest.position - release_center) > max(
                0.35, 0.25 * pixel_step
            ):
                center_measurement = measure_target(
                    release_center, "balance_scan", pixel_step
                )
                if center_measurement is not None and center_measurement.valid:
                    usable.append(center_measurement)
                    nearest = center_measurement
            best = min(
                usable,
                key=lambda item: (
                    abs(item.position - release_center),
                    abs(item.signed_displacement),
                ),
            )
            final_microstep = mode

            probe_targets = (
                best.position - pixel_step,
                best.position + pixel_step,
            )
            probes = []
            probes_within_atlas = all(
                0.0 <= target <= map_width for target in probe_targets
            )
            if probes_within_atlas:
                for target in probe_targets:
                    try:
                        measurement = measure_target(
                            float(target), "balance_probe", pixel_step
                        )
                    except ObjectiveCalibrationCancelled:
                        raise
                    except RuntimeError as exc:
                        self._publish_diagnostic(
                            "balance",
                            f"MICRO {mode}: probe at {target:.3f}px could "
                            f"not be measured ({exc}); this neighbor test "
                            "fails and a finer MICRO will be tried.",
                        )
                        measurement = None
                    if measurement is not None:
                        probes.append(measurement)

            valid_probes = sorted(
                (
                    item for item in probes
                    if item.valid
                    and item.disabled_visual_position is not None
                    and np.isfinite(item.signed_displacement)
                ),
                key=lambda item: item.position,
            )
            same_release_tolerance = self.neighbor_consistency_tolerance_px
            center_is_stable = (
                best.valid
                and np.isfinite(best.signed_displacement)
                and abs(best.signed_displacement) <= self.stable_threshold
                and best.displacement <= self.stable_threshold
            )
            self._publish_diagnostic(
                "balance",
                f"MICRO {mode}: candidate={best.position:.3f}px, "
                f"release center={release_center:.3f}px, signed shift="
                f"{best.signed_displacement:+.3f}px, total shift="
                f"{best.displacement:.3f}px, valid={best.valid}; center "
                f"stable={'YES' if center_is_stable else 'NO'} "
                f"(limit={self.stable_threshold:.3f}px).",
            )
            if not probes_within_atlas:
                self._publish_diagnostic(
                    "balance",
                    f"MICRO {mode}: neighbor test NOT RUN because a probe "
                    f"would leave the complete atlas [0.000, "
                    f"{map_width:.3f}]px; requested left="
                    f"{probe_targets[0]:.3f}px, right="
                    f"{probe_targets[1]:.3f}px.",
                )

            if len(valid_probes) == 2:
                left_probe, right_probe = valid_probes
                probe_center = float(np.median([
                    left_probe.disabled_visual_position,
                    right_probe.disabled_visual_position,
                ]))
                endpoints_agree = (
                    abs(
                        left_probe.disabled_visual_position
                        - right_probe.disabled_visual_position
                    ) <= same_release_tolerance
                )
                directions_point_inward = (
                    (
                        left_probe.signed_displacement > 0
                        or abs(left_probe.signed_displacement)
                        <= self.stable_threshold
                    )
                    and (
                        right_probe.signed_displacement < 0
                        or abs(right_probe.signed_displacement)
                        <= self.stable_threshold
                    )
                )
                endpoint_separation = abs(
                    left_probe.disabled_visual_position
                    - right_probe.disabled_visual_position
                )
                endpoint_center_error = abs(probe_center - release_center)
                self._publish_diagnostic(
                    "balance",
                    f"MICRO {mode}: left probe {left_probe.position:.3f}px "
                    f"-> {left_probe.disabled_visual_position:.3f}px "
                    f"(signed {left_probe.signed_displacement:+.3f}px); "
                    f"right probe {right_probe.position:.3f}px -> "
                    f"{right_probe.disabled_visual_position:.3f}px "
                    f"(signed {right_probe.signed_displacement:+.3f}px). "
                    f"Endpoint separation={endpoint_separation:.3f}px, "
                    f"endpoint-center error={endpoint_center_error:.3f}px, "
                    f"allowed={same_release_tolerance:.3f}px; endpoints "
                    f"agree={'YES' if endpoints_agree else 'NO'}, directions "
                    f"point inward={'YES' if directions_point_inward else 'NO'}.",
                )

                if endpoints_agree:
                    # Both neighboring positions released to one visual atlas
                    # coordinate.  That common landing coordinate is the
                    # mechanical stable point.  Go there directly and test it
                    # at the current MICRO instead of narrowing the step first.
                    stable_landing = float(np.clip(
                        probe_center,
                        lower,
                        upper,
                    ))
                    self._publish_diagnostic(
                        "balance",
                        f"MICRO {mode}: both probes release to the same "
                        f"point. Moving directly to their common landing "
                        f"position {stable_landing:.3f}px and checking it "
                        "before considering a finer MICRO.",
                    )
                    landing_measurement = measure_target(
                        stable_landing,
                        "balance_landing_check",
                        pixel_step,
                    )
                    if (
                        landing_measurement is not None
                        and landing_measurement.valid
                    ):
                        best = landing_measurement
                        release_center = stable_landing
                        center_is_stable = (
                            np.isfinite(best.signed_displacement)
                            and abs(best.signed_displacement)
                            <= self.stable_threshold
                            and best.displacement <= self.stable_threshold
                        )
                        self._publish_diagnostic(
                            "balance",
                            f"MICRO {mode}: common-landing check at "
                            f"{best.position:.3f}px produced signed shift "
                            f"{best.signed_displacement:+.3f}px and total "
                            f"shift {best.displacement:.3f}px; stable="
                            f"{'YES' if center_is_stable else 'NO'} "
                            f"(limit={self.stable_threshold:.3f}px).",
                        )
                    else:
                        center_is_stable = False
                        self._publish_diagnostic(
                            "balance",
                            f"MICRO {mode}: the common landing point could "
                            "not be checked reliably; a finer MICRO may be "
                            "tried.",
                        )
            else:
                endpoints_agree = False
                directions_point_inward = False
                if probes_within_atlas:
                    self._publish_diagnostic(
                        "balance",
                        f"MICRO {mode}: neighbor test FAILED because "
                        f"{len(valid_probes)} of 2 probes were valid; two "
                        "valid localized release measurements are required.",
                    )
            # Neighbor consistency describes only whether the two neighboring
            # probes release to the same visual position.  Stability of that
            # common landing point is a separate check, followed by repeated
            # final verification measurements.
            neighbor_release_consistent = endpoints_agree
            balance_passed = center_is_stable and endpoints_agree
            if balance_passed:
                self._publish_diagnostic(
                    "balance",
                    f"MICRO {mode}: PASS. Both probes share one release "
                    "endpoint and the direct check at that endpoint is stable; "
                    "refinement stops at the current MICRO.",
                )
                break

            failed_checks = []
            if not center_is_stable:
                failed_checks.append("candidate center is not stable")
            if not endpoints_agree:
                failed_checks.append("neighbor release endpoints do not agree")
            if not directions_point_inward and not endpoints_agree:
                failed_checks.append("neighbor directions are not inward")
            next_mode = (
                refinement_modes[refinement_index + 1]
                if refinement_index + 1 < len(refinement_modes)
                else None
            )
            action = (
                f"refining with MICRO {next_mode}"
                if next_mode is not None
                else "no finer configured mode remains"
            )
            self._publish_diagnostic(
                "balance",
                f"MICRO {mode}: FAIL ({'; '.join(failed_checks)}); {action}.",
            )

            # Refine only when the probes did not identify and confirm one
            # common stable landing point at the current MICRO.
            stable_hint = release_center
            search_lower = max(float(lower), release_center - pixel_step)
            search_upper = min(float(upper), release_center + pixel_step)

        if best is None:
            raise RuntimeError(
                "No valid between-peak measurements were available"
            )
        return (
            results,
            best,
            final_microstep,
            neighbor_release_consistent,
        )

    def run(
        self,
        *,
        half_range: int = 30,
        coarse_step: int = 5,
        stagger_offset: int = 2,
        refine_radius: int = 5,
        fine_step: int = 1,
        verification_repeats: int = 3,
        lock_exposure: bool = True,
    ) -> ObjectiveCalibrationResult:
        if half_range <= 0:
            raise ValueError("half_range must be positive")
        if coarse_step <= 0 or refine_radius < 0 or fine_step <= 0:
            raise ValueError("scan steps and radii must be positive")
        if stagger_offset < 1:
            raise ValueError("edge confirmation count must be at least 1")
        if verification_repeats < 1:
            raise ValueError("verification_repeats must be at least 1")
        if not self.camera.is_open:
            raise RuntimeError("Camera is not open")
        if not self.motor.connected:
            raise RuntimeError("Motor controller is not connected")

        self.last_frame_id = None
        self.commanded_position_units = 0.0
        self.atlas_pixels_per_motor_unit = 1.0
        self.visual_position_estimate = None
        self.movement_stage = "initializing"
        self.movement_target = None
        self.measurements.clear()
        self.atlas.clear()
        self.atlas_segments.clear()
        self.atlas_attempt_counter = 0
        self.peak_frames.clear()
        stabilization_before = self.camera.stabilization_enabled
        auto_exposure_before = None
        outside_margin = int(coarse_step)
        max_search = 2 * int(half_range) + 2 * int(refine_radius) + coarse_step

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
            self._wait(self.enable_settle_seconds)

            starting_mode_index = 0
            while True:
                atlas_microstep, atlas_attempt_id = (
                    self._choose_atlas_microstep(
                        outside_margin_full_steps=outside_margin,
                        max_search_full_steps=max_search,
                        confirmation_positions=stagger_offset,
                        starting_mode_index=starting_mode_index,
                    )
                )
                try:
                    match_width = self._build_visual_atlas(
                        atlas_microstep,
                        attempt_id=atlas_attempt_id,
                        outside_margin_full_steps=outside_margin,
                        max_search_full_steps=max_search,
                        confirmation_positions=stagger_offset,
                    )
                    # A partial atlas can continue with progressively finer
                    # modes; report the finest mode actually used at the end.
                    atlas_microstep = int(self.current_microstep)
                    self._finish_atlas_attempt(
                        atlas_attempt_id,
                        atlas_microstep,
                        success=True,
                    )
                    break
                except _AtlasStepRegistrationError as exc:
                    failed_mode = int(self.current_microstep)
                    self._finish_atlas_attempt(
                        atlas_attempt_id,
                        failed_mode,
                        success=False,
                        reason=str(exc),
                    )
                    mode_index = self.MICROSTEP_MODES.index(failed_mode)
                    if mode_index + 1 >= len(self.MICROSTEP_MODES):
                        raise RuntimeError(
                            "Adjacent atlas images are still unreliable at "
                            "MICRO 16. Increase image texture or lower Min "
                            "confidence/Min overlap."
                        ) from exc
                    # Discard this incomplete map and rebuild it from the left
                    # visual edge using half the physical step size.
                    self.atlas.clear()
                    self.atlas_segments.clear()
                    starting_mode_index = mode_index + 1
                except Exception as exc:
                    self._finish_atlas_attempt(
                        atlas_attempt_id,
                        atlas_microstep,
                        success=False,
                        reason=str(exc),
                    )
                    raise

            release_measurements: list[ObjectiveMeasurement] = []
            scan_modes = [
                atlas_microstep,
                *(
                    mode for mode in self.MICROSTEP_MODES
                    if mode > atlas_microstep
                ),
            ]
            direction = -1
            selected_pair = None
            peak_regions: list[tuple[float, float]] = []
            stable_hint: float | None = None
            used_modes = []
            rescan_bounds: tuple[float, float] | None = None
            known_large_positions: list[float] = []
            for pass_index, mode in enumerate(scan_modes):
                used_modes.append(mode)
                stage = (
                    "release_coarse" if pass_index == 0
                    else "release_rescan"
                )
                batch = self._release_scan_pass(
                    direction,
                    mode,
                    match_width,
                    outside_margin_full_steps=outside_margin,
                    max_search_full_steps=max_search,
                    confirmation_positions=stagger_offset,
                    stage=stage,
                    scan_bounds=rescan_bounds,
                )
                release_measurements.extend(batch)
                sampled_pixel_steps = [
                    self._estimated_pixels_per_pulse(item.microstep)
                    for item in batch
                    if item.microstep in self.MICROSTEP_MODES
                ]
                pixel_step = max(
                    sampled_pixel_steps,
                    default=self._estimated_pixels_per_pulse(mode),
                )
                signed_regions = self._peak_regions(
                    batch,
                    max_gap=max(1.0, 2.2 * pixel_step),
                )
                selected_pair = self._choose_peak_pair(
                    signed_regions,
                    batch,
                )
                if selected_pair is not None:
                    left_region, right_region, stable_hint = selected_pair
                    peak_regions = [
                        (left_region[0], left_region[1]),
                        (right_region[0], right_region[1]),
                    ]
                    break
                known_large_positions.extend(
                    item.position for item in batch
                    if item.valid
                    and np.isfinite(item.signed_displacement)
                    and abs(item.signed_displacement) >= self.peak_threshold
                )
                if known_large_positions:
                    rescan_margin = max(
                        4.0 * pixel_step,
                        self.post_peak_quiet_distance_px,
                    )
                    rescan_bounds = (
                        max(
                            0.0,
                            min(known_large_positions) - rescan_margin,
                        ),
                        min(
                            float(match_width),
                            max(known_large_positions) + rescan_margin,
                        ),
                    )
                    self._publish_diagnostic(
                        "release rescan",
                        f"No complete signed peak pair at MICRO {mode}; "
                        f"{len(known_large_positions)} large-displacement "
                        f"sample(s) are known. The next finer reverse pass "
                        f"will scan only [{rescan_bounds[0]:.3f}, "
                        f"{rescan_bounds[1]:.3f}]px (margin "
                        f"{rescan_margin:.3f}px).",
                    )
                else:
                    self._publish_diagnostic(
                        "release rescan",
                        f"No trustworthy large-displacement sample was "
                        f"found at MICRO {mode}; the next finer pass must "
                        "retain the full image range.",
                    )
                direction *= -1

            if selected_pair is None:
                raise RuntimeError(
                    "No trustworthy double release peak was found, even at "
                    "MICRO 16. Review the displacement plot and thresholds."
                )

            left_peak_region, right_peak_region, stable_hint = selected_pair
            left_peak = (left_peak_region[0], left_peak_region[1])
            right_peak = (right_peak_region[0], right_peak_region[1])
            # The complete image-defined gap belongs to the between-peaks
            # search.  A probe's common release endpoint must not be excluded
            # merely because it is close to a detected peak boundary.
            balance_lower = float(left_peak[1])
            balance_upper = float(right_peak[0])
            if balance_lower > balance_upper:
                raise RuntimeError("The two release peaks have no stable interval")

            # Enter the double-peak interval once.  All following positioning
            # uses atlas feedback and stays inside that interval.
            if direction < 0:
                self._find_visual_edge(
                    1,
                    outside_margin_full_steps=outside_margin,
                    max_search_full_steps=max_search,
                    confirmation_positions=stagger_offset,
                    already_outside=True,
                )
            else:
                self._find_visual_edge(
                    -1,
                    outside_margin_full_steps=outside_margin,
                    max_search_full_steps=max_search,
                    confirmation_positions=stagger_offset,
                    already_outside=True,
                )
            balance_starting_microstep = mode
            balance_hint = stable_hint
            while True:
                (
                    balance_measurements,
                    best,
                    final_microstep,
                    neighbor_release_consistent,
                ) = self._balance_scan(
                    balance_lower,
                    balance_upper,
                    fine_step_units=max(1, int(fine_step)),
                    map_width=match_width,
                    starting_microstep=balance_starting_microstep,
                    stable_hint=balance_hint,
                )

                verification = []
                verification_positioning_error = None
                self._set_microstep(final_microstep)
                previous_position = best.position
                final_pixel_step = self._estimated_pixels_per_pulse(
                    final_microstep
                )
                verification_tolerance = max(
                    0.50, 0.45 * final_pixel_step
                )
                self._publish_diagnostic(
                    "verification",
                    f"MICRO {final_microstep}: verifying candidate "
                    f"{best.position:.3f}px {verification_repeats} time(s) "
                    f"with positioning tolerance "
                    f"{verification_tolerance:.3f}px.",
                )
                for _ in range(verification_repeats):
                    try:
                        frame_id, frame, similarity, position, confidence = (
                            self._seek_inside_atlas(
                                best.position,
                                previous_position,
                                max_pulses=max(
                                    64,
                                    int(np.ceil(
                                        match_width
                                        / max(0.05, final_pixel_step)
                                    )) + 32,
                                ),
                                max_command_pulses=16,
                                tolerance=verification_tolerance,
                                stage="verify_position",
                                max_target_crossings=3,
                            )
                        )
                    except ObjectiveCalibrationCancelled:
                        raise
                    except _VisualTargetUnreachable as exc:
                        verification.append(
                            self._record_skipped_target(exc, "verify")
                        )
                        previous_position = exc.last_position
                        verification_positioning_error = str(exc)
                        break
                    except RuntimeError as exc:
                        verification_positioning_error = str(exc)
                        self._publish_diagnostic(
                            "verification",
                            f"MICRO {final_microstep}: could not position at "
                            f"{best.position:.3f}px within "
                            f"{verification_tolerance:.3f}px ({exc}).",
                        )
                        break
                    verification_measurement = self._measure_release(
                        position,
                        confidence,
                        frame_id,
                        frame,
                        similarity,
                        "verify",
                    )
                    verification.append(verification_measurement)
                    previous_position = (
                        verification_measurement.disabled_visual_position
                        if verification_measurement.disabled_visual_position
                        is not None
                        else position
                    )

                finite_distances = [
                    item.displacement for item in verification
                    if np.isfinite(item.displacement)
                ]
                if finite_distances:
                    median_displacement = float(np.median(finite_distances))
                    maximum_displacement = float(max(finite_distances))
                else:
                    median_displacement = maximum_displacement = float("nan")
                verification_passed = (
                    neighbor_release_consistent
                    and verification_positioning_error is None
                    and len(verification) == verification_repeats
                    and all(item.valid for item in verification)
                    and np.isfinite(median_displacement)
                    and median_displacement <= self.stable_threshold
                )
                self._publish_diagnostic(
                    "verification",
                    f"MICRO {final_microstep} verification "
                    f"{'PASS' if verification_passed else 'FAIL'}: neighbor "
                    f"consistency="
                    f"{'PASS' if neighbor_release_consistent else 'FAIL'}, "
                    f"valid measurements="
                    f"{sum(item.valid for item in verification)}/"
                    f"{verification_repeats}, median release shift="
                    f"{median_displacement:.3f}px (limit "
                    f"{self.stable_threshold:.3f}px).",
                )
                if verification_passed:
                    break

                final_mode_index = self.MICROSTEP_MODES.index(
                    final_microstep
                )
                if final_mode_index + 1 >= len(self.MICROSTEP_MODES):
                    break

                disabled_endpoints = [
                    item.disabled_visual_position for item in verification
                    if item.valid
                    and item.disabled_visual_position is not None
                    and np.isfinite(item.disabled_visual_position)
                ]
                if disabled_endpoints:
                    balance_hint = float(np.clip(
                        np.median(disabled_endpoints),
                        balance_lower,
                        balance_upper,
                    ))
                elif (
                    best.disabled_visual_position is not None
                    and np.isfinite(best.disabled_visual_position)
                ):
                    balance_hint = float(np.clip(
                        best.disabled_visual_position,
                        balance_lower,
                        balance_upper,
                    ))
                else:
                    balance_hint = float(best.position)
                balance_starting_microstep = self.MICROSTEP_MODES[
                    final_mode_index + 1
                ]
                self._publish_diagnostic(
                    "verification",
                    f"MICRO {final_microstep} was not repeatably stable; "
                    f"using disable landing position {balance_hint:.3f}px "
                    f"as the next hint and repeating center/probe/verification "
                    f"at MICRO {balance_starting_microstep}.",
                )

            result = ObjectiveCalibrationResult(
                position=float(best.position),
                median_displacement=median_displacement,
                maximum_displacement=maximum_displacement,
                minimum_confidence=float(
                    min(
                        (item.confidence for item in verification),
                        default=0.0,
                    )
                ),
                stable=(
                    verification_passed
                ),
                peak_regions=tuple(peak_regions),
                match_region=(0.0, float(match_width)),
                release_range=(
                    0.0,
                    float(match_width),
                ),
                verification_count=len(verification),
                atlas_microstep=atlas_microstep,
                final_microstep=final_microstep,
            )
            self._publish_diagnostic(
                "verification",
                f"Final decision: neighbor consistency="
                f"{'PASS' if neighbor_release_consistent else 'FAIL'}, "
                f"valid verification measurements="
                f"{sum(item.valid for item in verification)}/"
                f"{len(verification)}, median release shift="
                f"{median_displacement:.3f}px (limit "
                f"{self.stable_threshold:.3f}px); stable="
                f"{'YES' if result.stable else 'NO'}.",
            )
            return result
        finally:
            try:
                if self.motor.connected:
                    self.motor.objective_stop()
            except Exception:
                pass
            try:
                if self.motor.connected:
                    self.motor.objective_disable()
            except Exception:
                pass
            self.camera.set_stabilization(stabilization_before)
            if lock_exposure and auto_exposure_before is not None:
                self.camera.set_auto_exposure(auto_exposure_before)


def save_objective_measurements(
    path: str | Path,
    measurements: list[ObjectiveMeasurement],
) -> Path:
    """Save the reference map, release scan, refinement, and verification."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow((
            "stage", "visual_position_px", "commanded_position_1_16_step",
            "microstep", "scan_direction", "atlas_attempt_id",
            "localization_confidence",
            "signed_displacement_px",
            "displacement_px", "dx_px", "dy_px",
            "confidence", "overlap", "valid", "status",
            "reference_similarity", "enabled_frame_id", "disabled_frame_id",
            "disabled_visual_position_px",
        ))
        for item in measurements:
            writer.writerow((
                item.stage,
                item.position,
                item.commanded_position,
                item.microstep,
                item.scan_direction,
                item.atlas_attempt_id,
                item.localization_confidence,
                item.signed_displacement,
                item.displacement,
                item.dx,
                item.dy,
                item.confidence,
                item.overlap,
                int(item.valid),
                item.status,
                item.reference_similarity,
                item.enabled_frame_id,
                item.disabled_frame_id,
                item.disabled_visual_position,
            ))
    return path
