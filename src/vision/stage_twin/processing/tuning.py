# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/processing/tuning.py
"""Per-session detector tuning driven only by freshly submitted measurements."""

import math
from dataclasses import dataclass
from time import monotonic

from ..extractors.aruco_presets import monitor_preset_catalog


@dataclass(frozen=True)
class _Score:
    axes: tuple = ()
    tracks: int = 0


class TuningPolicy:
    """Choose one preset per input. The source owns capture and scheduling.

    A host serializes next_preset, conversion and observe for each session.
    Failed conversions do not supply evidence. A new camera or profile starts
    a new policy. No historical coordinates are used in scoring.
    """

    def __init__(self, calibrated_axes, *, automatic=True, max_frames=4, clock=monotonic):
        calibrated_axes = frozenset(calibrated_axes)
        self.axes = tuple(name for name in ("x", "y", "insert") if name in calibrated_axes)
        self._presets = tuple(item["id"] for item in monitor_preset_catalog())
        self._clock = clock
        self.active_preset = 1
        self._seen_losses = set()
        self._order = ()
        self._tested = 0
        self._best = _Score()
        self._best_preset = 1
        self._deadline = 0.0
        self._manual = False
        self._latest = None
        self._outcome = "idle"
        self.automatic = True
        self.configure(automatic=automatic, max_frames=max_frames)

    @property
    def searching(self):
        return bool(self._order)

    def configure(self, *, automatic, max_frames):
        if type(automatic) is not bool:
            raise ValueError("Automatic tuning must be true or false.")
        if type(max_frames) is not int or not 1 <= max_frames <= len(self._presets):
            raise ValueError(f"Tuning frame limit must be 1 to {len(self._presets)}.")
        if self.automatic != automatic:
            self._seen_losses.clear()
            if not automatic and self.searching and not self._manual:
                self.cancel()
        self.automatic = automatic
        self.max_frames = max_frames

    def _start(self, manual, reference):
        split = self._presets.index(self.active_preset) + 1
        order = self._presets[split:] + self._presets[:split]
        self._order = order[: self.max_frames]
        self._tested = 0
        self._best = reference or _Score()
        self._best_preset = self.active_preset
        self._deadline = self._clock() + 30
        self._manual = manual
        self._outcome = "searching"

    def start_manual(self, *, reference_age_seconds=2.0):
        if not math.isfinite(reference_age_seconds) or reference_age_seconds < 0:
            raise ValueError("Reference age must be finite and nonnegative.")
        if not self.axes or (self.searching and self._manual):
            return False
        reference = None
        if self._latest is not None:
            previous_time, preset, score = self._latest
            if (
                preset == self.active_preset
                and self._clock() - previous_time <= reference_age_seconds
            ):
                reference = score
                self._seen_losses.add(tuple(name for name in self.axes if name not in score.axes))
        self._start(True, reference)
        return True

    def _finish(self, outcome):
        if self._best.axes:
            self.active_preset = self._best_preset
        self._order = ()
        self._outcome = outcome
        self._manual = False

    def cancel(self):
        if self.searching:
            self._order = ()
            self._manual = False
            self._outcome = "cancelled"

    def _expire(self):
        if self.searching and self._clock() >= self._deadline:
            self._finish("expired")
            return True
        return False

    def next_preset(self):
        self._expire()
        return self._order[self._tested] if self.searching else self.active_preset

    def observe(self, report):
        axes = report["axes"]
        visible = []
        missing = []
        tracks = 0
        for name in self.axes:
            reading = axes.get(name, {})
            value = reading.get("value_mm")
            if type(value) in (int, float) and math.isfinite(value) and not reading.get("held"):
                visible.append(name)
                tracks += len(reading.get("tracks", ()))
            elif reading.get("reason") == "no_calibrated_pair_visible":
                missing.append(name)
        score = _Score(tuple(visible), tracks)
        effective = 1 if report["detector_method"] == "Standard" else report["detector_preset"]
        self._latest = (self._clock(), effective, score)
        if self._expire():
            return
        if self.searching:
            self._tested += 1
            same_axes = score.axes == self._best.axes
            if (
                len(score.axes) > len(self._best.axes)
                or (same_axes and score.tracks > self._best.tracks)
                or (same_axes and visible and effective == 1 and score.tracks == self._best.tracks)
            ):
                self._best, self._best_preset = score, effective
            if score.axes == self.axes:
                self._finish("complete")
            elif self._tested == len(self._order):
                self._finish("limit")
        elif score.axes == self.axes:
            self._seen_losses.clear()
        elif missing and self.automatic and tuple(missing) not in self._seen_losses:
            self._seen_losses.add(tuple(missing))
            self._start(False, score)

    def snapshot(self):
        """Small diagnostic summary; all containers are fresh caller-owned values."""
        self._expire()
        return {
            "active_preset": self.active_preset,
            "next_preset": self._order[self._tested] if self.searching else self.active_preset,
            "searching": self.searching,
            "manual": self._manual,
            "automatic": self.automatic,
            "max_frames": len(self._order) if self.searching else self.max_frames,
            "tested": self._tested,
            "outcome": self._outcome,
            "best_axes": list(self._best.axes),
            "calibrated_axes": list(self.axes),
        }
