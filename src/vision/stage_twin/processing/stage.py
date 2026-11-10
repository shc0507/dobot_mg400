# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/processing/stage.py
"""Convert an overhead camera frame with a numerical calibration profile."""

from functools import cached_property
from time import perf_counter

from ..calibration.profiles import (
    analyze_frame,
    evaluate_profile,
)
from ..calibration.stage import AXES, centers, estimate_axis
from ..extractors.aruco_presets import detector_for_preset, monitor_preset_catalog
from ..marker_registry import ARUCO_DICTIONARY, MARKER_GROUPS


class StageConverter:
    """A stateless per-frame measurement using an immutable calibration snapshot."""

    def __init__(self, profile):
        profile = evaluate_profile(profile)["profile"]
        self.name = profile["name"]
        self.profile = profile
        dimensions = profile["camera"]["dimensions"]
        self.dimensions = tuple(dimensions) if dimensions else None
        self.model = {
            "axes": profile["axes"],
            "camera_size_px": dimensions,
            "status": "provisional_endpoint_fit" if profile["axes"] else "calibration_pending",
        }

    @classmethod
    def from_profile(cls, profile):
        """Use only valid fitted axes; an empty profile reports unknown positions."""
        return cls(profile)

    @cached_property
    def _insert_search(self):
        if "insert" not in self.model["axes"]:
            return None
        # Delay the vision imports until a measurement is requested. Reference
        # IDs come from the same fitted tracks that estimate_axis will use.
        from ..extractors.insert_recovery import build_insert_search

        return build_insert_search(self.profile)

    @property
    def metadata(self) -> dict:
        axes = self.model["axes"]
        return {
            "session": self.name,
            "profile_id": self.profile["id"] if self.profile else None,
            "dimensions": list(self.dimensions) if self.dimensions else None,
            "axes": {
                name: {
                    "calibrated": name in axes,
                    "min": axes[name]["min"] if name in axes else None,
                    "max": axes[name]["max"] if name in axes else None,
                }
                for name in AXES
            },
            "marker_groups": {
                name: list(group.marker_ids) for name, group in MARKER_GROUPS.items()
            },
            "calibration_status": self.model["status"],
            "unit": "mm",
            "dictionary": ARUCO_DICTIONARY,
            "detection_presets": monitor_preset_catalog(),
        }

    def _frame_detector(self, preset):
        targets = set()
        if preset != 1:
            for axis in self.model["axes"].values():
                for track in axis["tracks"]:
                    targets.update((track["moving_id"], track["reference_id"]))
        detector = detector_for_preset(preset, targets, monitor=True)
        if self._insert_search is None:
            return detector

        from ..extractors.aruco import detect_markers
        from ..extractors.insert_recovery import recover_insert

        detect = detector or detect_markers

        def detect_with_insert(gray, color):
            observed, recovery = recover_insert(gray, detect(gray, color), self._insert_search)
            return {**observed, "insert_recovery": recovery}

        return detect_with_insert

    def measure(self, data: bytes, *, preset: int = 1) -> dict:
        started = perf_counter()
        observation = analyze_frame(data, self.dimensions, detector=self._frame_detector(preset))
        points = centers(observation)
        return {
            "dimensions": observation["image_size_px"],
            "detections": observation["detections"],
            "ambiguous_ids": observation["ambiguous_ids"],
            "axes": {name: estimate_axis(self.model["axes"].get(name), points) for name in AXES},
            "image_sha256": observation["image_sha256"],
            "captured_at": observation["captured_at"],
            "insert_recovery": observation.get(
                "insert_recovery",
                {
                    "status": "calibration_missing",
                    "reference_ids": [],
                    "expected_reference_ids": [],
                },
            ),
            "detector_preset": preset,
            "detector_method": observation.get("method", "Standard"),
            "processing_ms": round((perf_counter() - started) * 1000, 2),
        }
