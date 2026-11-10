# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/calibration/autotune.py
"""Calibration-only detector settings for repeated endpoint snaps."""

from ..extractors.aruco_presets import detector_for_preset
from ..marker_registry import MARKER_GROUPS
from .profiles import (
    ENDPOINTS,
    _groups,
    _numerical_observation,
    _observation,
    _role_error,
    analyze_frame,
)


def analyze_attempt(data, endpoint, attempt=1, marker_groups=None):
    """Analyze one capture. Coverage includes desk, moving and parent markers.

    Missing groups produce a numerical observation and diagnostics so retries
    can be ranked without combining measurements from different frames.
    """
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown calibration endpoint.")
    if marker_groups is None:
        marker_groups = {name: list(group.marker_ids) for name, group in MARKER_GROUPS.items()}
    groups = _groups(marker_groups)
    axis = ENDPOINTS[endpoint]["axis"]
    parent = MARKER_GROUPS[axis].parent
    targets = set(groups["desk"]) | set(groups[axis]) | set(groups[parent])

    result = analyze_frame(data, detector=detector_for_preset(attempt, targets))
    observation = _observation(_numerical_observation(result))
    found = targets.intersection(marker["id"] for marker in observation["detections"])
    missing = sorted(targets - found)
    role_error = _role_error(observation, axis, groups)
    reason = role_error or (
        "Missing target IDs: " + ", ".join(map(str, missing)) if missing else None
    )
    return {
        "capture": observation,
        "tuning": {
            "targets": sorted(targets),
            "found": sorted(found),
            "missing": missing,
            "usable": role_error is None,
            "complete": not missing,
            "reason": reason,
            "method": result.get("method", "Standard"),
        },
    }
