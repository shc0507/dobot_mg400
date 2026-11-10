# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/calibration/stage.py
"""Fit and read calibrated X, Y and insert carriage travel."""

import math
from statistics import mean, median

from ..marker_registry import MARKER_GROUPS

# Tags on the child are measured relative to tags on its rigid parent.
# The registry supplies eligible IDs; a fit includes only pairs visible in
# both endpoint photos. Existing models continue using their saved tracks.
_ENDPOINT_PHOTOS = {
    "x": ("X stage Left Limit.jpg", "X stage Right Limit.jpg"),
    "y": ("Y stage Down Limit.jpg", "Y stage Up Limit.jpg"),
    "insert": ("Insert Min (outside).jpg", "Insert Max (inside).jpg"),
}
AXES = {
    axis: (
        *photos,
        MARKER_GROUPS[axis].marker_ids,
        MARKER_GROUPS[MARKER_GROUPS[axis].parent].marker_ids,
    )
    for axis, photos in _ENDPOINT_PHOTOS.items()
}


def centers(row):
    return {tag["id"]: tag["center_px"] for tag in row["detections"]}


def difference(point, origin):
    return [point[0] - origin[0], point[1] - origin[1]]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def fit_axis(axis, photos, span):
    low_name, high_name, child_ids, parent_ids = AXES[axis]
    if span is None or not math.isfinite(span) or span <= 0:
        raise ValueError("Measured travel must be a positive finite number in mm.")
    low, high = (centers(photos[name]) for name in (low_name, high_name))
    common = low.keys() & high.keys()
    tracks = []
    for child in child_ids:
        for parent in parent_ids:
            if child not in common or parent not in common:
                continue
            start = difference(low[child], low[parent])
            end = difference(high[child], high[parent])
            travel = difference(end, start)
            length = math.hypot(*travel)
            if length < 1.0:
                raise ValueError(f"Subpixel endpoint span on {axis} for IDs {child} and {parent}.")
            tracks.append(
                {
                    "moving_id": child,
                    "reference_id": parent,
                    "zero_relative_px": start,
                    "max_relative_px": end,
                    "travel_vector_px": travel,
                    "travel_length_px": length,
                    "mm_per_px_along_track": span / length,
                }
            )
    if not tracks:
        raise ValueError(f"No moving/reference ID pair decoded at both {axis.upper()} endpoints.")
    return {
        "unit": "mm",
        "min": 0.0,
        "max": span,
        "input_photos": {"min": low_name, "max": high_name},
        "span_source": "operator-measured endpoint-to-endpoint travel",
        "tracks": tracks,
    }


def estimate_axis(axis_model, points):
    if axis_model is None:
        return {"value_mm": None, "status": "unknown", "reason": "calibration_missing"}
    estimates = {}
    readings = []
    for track in axis_model["tracks"]:
        child, parent = track["moving_id"], track["reference_id"]
        if child not in points or parent not in points:
            continue
        relative = difference(points[child], points[parent])
        offset = difference(relative, track["zero_relative_px"])
        vector = track["travel_vector_px"]
        fraction = dot(offset, vector) / dot(vector, vector)
        value = fraction * axis_model["max"]
        estimates.setdefault(child, []).append(value)
        perpendicular = abs(offset[0] * vector[1] - offset[1] * vector[0]) / math.hypot(*vector)
        readings.append(
            {
                "moving_id": child,
                "reference_id": parent,
                "estimate_mm": value,
                "off_track_px": perpendicular,
            }
        )
    if not readings:
        return {"value_mm": None, "status": "unknown", "reason": "no_calibrated_pair_visible"}
    coordinate = median(mean(values) for values in estimates.values())
    all_values = [reading["estimate_mm"] for reading in readings]
    return {
        "value_mm": coordinate,
        "status": "provisional_interpolation"
        if 0 <= coordinate <= axis_model["max"]
        else "extrapolated",
        "moving_id_count": len(estimates),
        "track_spread_mm": max(all_values) - min(all_values) if len(all_values) > 1 else None,
        "tracks": readings,
    }
