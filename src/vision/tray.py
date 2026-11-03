"""Tray geometry and explicit slot checks from supplied frames; no acquisition."""

import itertools
import json
import math
import re
import tomllib
from pathlib import Path

import cv2
import numpy as np

from .calibration import detect_markers, image_size, robot_xy, validate_model

MAX_DOCUMENT = 64 * 1024
_MAX_COORDINATE = 1_000_000.0
_MIN_SCALE = 1e-12
_MIN_CELL_PIXELS = 2.0
_MIN_CONTRAST = 40.0


def _finite_array(value, label, *, shape=None):
    try:
        raw = np.asarray(value, dtype=object)
        if any(
            isinstance(item, (bool, np.bool_))
            or not isinstance(item, (int, float, np.integer, np.floating))
            for item in raw.flat
        ):
            raise ValueError
        points = np.asarray(value)
        if (shape is not None and points.shape != shape) or points.dtype.kind not in "ifu":
            raise ValueError
        points = points.astype(np.float64)
        if not np.isfinite(points).all() or np.any(np.abs(points) > 1_000_000):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Invalid finite {label}.") from None
    return points


def _limit(value):
    if type(value) not in (int, float) or not 0 < value <= 2 or not math.isfinite(value):
        raise ValueError("Tray tolerance must be finite, positive, and at most 2 mm.")
    return float(value)


def _segment(value, label):
    points = _finite_array(value, label, shape=(2, 2))
    if np.linalg.norm(points[1] - points[0]) < 1e-6:
        raise ValueError(f"Degenerate {label}.")
    return points


def validate_tray(value):
    """Validate bounded, unlabeled groove geometry without reading a device."""
    keys = {
        "schema",
        "name",
        "dictionary",
        "origin_marker_id",
        "plate_size_mm",
        "marker_size_mm",
        "groove_width_mm",
        "tolerance_mm",
        "markers",
        "slots",
    }
    if not isinstance(value, dict) or set(value) != keys or type(value["schema"]) is not int:
        raise ValueError("Invalid tray configuration fields.")
    if value["schema"] != 1 or value["dictionary"] != "DICT_4X4_50":
        raise ValueError("Tray schema 1 requires DICT_4X4_50.")
    if not isinstance(value["name"], str) or not re.fullmatch(r"[\w .-]{1,80}", value["name"]):
        raise ValueError("Invalid tray name.")
    plate = _finite_array(value["plate_size_mm"], "plate size", shape=(2,))
    if np.any(plate <= 0):
        raise ValueError("Plate dimensions must be positive.")
    for field in ("marker_size_mm", "groove_width_mm"):
        number = value[field]
        if (
            type(number) not in (int, float)
            or not 0 < number <= min(1000, *plate)
            or not math.isfinite(number)
        ):
            raise ValueError(f"Invalid tray {field}.")
    _limit(value["tolerance_mm"])
    markers = value["markers"]
    if not isinstance(markers, list) or len(markers) != 4:
        raise ValueError("A tray requires 4 corner markers.")
    identities, centers = [], []
    for marker in markers:
        if not isinstance(marker, dict) or set(marker) != {"id", "center_mm"}:
            raise ValueError("Invalid tray marker fields.")
        ident = marker["id"]
        if type(ident) is not int or not 0 <= ident < 50 or ident == 4 or ident in identities:
            raise ValueError("Corner IDs must be unique DICT_4X4_50 IDs, excluding arm ID 4.")
        center = _finite_array(marker["center_mm"], "marker center", shape=(2,))
        if np.any(center < 0) or np.any(center > plate):
            raise ValueError("Marker center lies outside the tray.")
        identities.append(ident)
        centers.append(center)
    if type(value["origin_marker_id"]) is not int or value["origin_marker_id"] not in identities:
        raise ValueError("The origin corner must name a tray marker.")
    for triple in itertools.combinations(centers, 3):
        points = np.asarray(triple)
        singular = np.linalg.svd(points - points.mean(axis=0), compute_uv=False)
        if singular[0] < 1e-6 or singular[1] <= singular[0] * 1e-8:
            raise ValueError("Any 3 tray markers must be noncollinear.")
    slots = value["slots"]
    if not isinstance(slots, list) or not 1 <= len(slots) <= 128:
        raise ValueError("Tray slots must be a bounded nonempty list.")
    seen = set()
    for slot in slots:
        if not isinstance(slot, dict) or set(slot) != {"id", "centerline_mm", "reference_mm"}:
            raise ValueError("Invalid tray slot fields.")
        ident = slot["id"]
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident)
            or ident in seen
        ):
            raise ValueError("Slot IDs must be unique bounded names.")
        seen.add(ident)
        segment = _segment(slot["centerline_mm"], "groove centerline")
        if np.any(segment < 0) or np.any(segment > plate):
            raise ValueError("Groove centerline lies outside the tray.")
        reference = _finite_array(slot["reference_mm"], "nominal reference center", shape=(2,))
        if not np.allclose(reference, segment.mean(axis=0), rtol=0, atol=1e-9):
            raise ValueError("The nominal reference must be the groove midpoint.")
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("Tray configuration exceeds 64 KiB.")
    return json.loads(encoded)


def load_tray(path):
    """Read a bounded TOML tray model; chip identities are supplied by the run."""
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_DOCUMENT + 1)
    if len(data) > MAX_DOCUMENT:
        raise ValueError("Tray configuration exceeds 64 KiB.")
    return validate_tray(tomllib.loads(data.decode("utf-8")))


def locate_tray(image, calibration, tray, *, device, tolerance_mm=None):
    """Return localization geometry, without claiming verified station or chip data."""
    tray = validate_tray(tray)
    model = validate_model(calibration, device=device)
    tolerance = _limit(tray["tolerance_mm"] if tolerance_mm is None else tolerance_mm)
    pixels = np.asarray(image)
    if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError("Tray input must be an RGB image.")
    size = image_size((pixels.shape[1], pixels.shape[0]))
    if size != image_size(model["image_size"]):
        raise ValueError("Live image size differs from the stored camera calibration.")
    pixels = pixels.copy()
    expected = {marker["id"]: marker["center_mm"] for marker in tray["markers"]}
    measured = [marker for marker in detect_markers(pixels) if marker["id"] in expected]
    ids = [marker["id"] for marker in measured]
    if len(ids) != len(set(ids)):
        raise ValueError("The frame contains duplicate tray corner IDs.")
    if len(measured) < 3:
        raise ValueError("Tray localization requires at least 3 measured corner markers.")
    observed = [robot_xy(model, marker["center_px"], size) for marker in measured]
    fit = _fit_similarity([expected[ident] for ident in ids], observed, tolerance)
    markers = [
        {**marker, "center_xy_mm": xy, "residual_mm": residual}
        for marker, xy, residual in zip(measured, observed, fit["residuals_mm"])
    ]
    missing = set(expected) - set(ids)
    diagnostic = None
    if missing:
        ident = missing.pop()
        diagnostic = _sample_missing(
            pixels, model, fit, ident, expected[ident], tray["marker_size_mm"]
        )
    slots = {
        slot["id"]: {
            "centerline_xy_mm": _transform(slot["centerline_mm"], fit).tolist(),
            "reference_xy_mm": _transform([slot["reference_mm"]], fit)[0].tolist(),
            "width_mm": tray["groove_width_mm"] * fit["scale"],
        }
        for slot in tray["slots"]
    }
    return {
        "schema": 1,
        "tray_name": tray["name"],
        "camera_device": device,
        "image_size": list(size),
        "coordinate_system": "robot_xy_mm",
        "tolerance_mm": tolerance,
        "transform": fit,
        "markers": markers,
        "missing_marker": diagnostic,
        "slots": slots,
    }


class TrayCheckError(ValueError):
    """An incomplete or failed slot check, with the entire diagnostic report."""

    def __init__(self, report):
        super().__init__("Tray slot verification did not pass; inspect the report.")
        self.report = report


def _segment_residual(point, segment):
    vector = segment[1] - segment[0]
    fraction = np.clip(np.dot(point - segment[0], vector) / np.dot(vector, vector), 0, 1)
    return float(np.linalg.norm(point - (segment[0] + fraction * vector)))


def cross_check_slots(
    location, *, taught_stations, chips, same_location_slots=(), tolerance_mm=None
):
    """Check each groove; unknown evidence cannot pass or imply an empty slot.

    Station values are robot XY pairs. A chip observation is explicitly
    {"state": "present", "center_xy_mm": [x, y]}, {"state": "empty"}, or
    {"state": "unknown"}. Empty/unknown mappings do not assert emptiness.
    """
    if not isinstance(location, dict) or location.get("coordinate_system") != "robot_xy_mm":
        raise ValueError("Slot checks require robot XY tray localization.")
    slots = location.get("slots")
    if not isinstance(slots, dict) or not 1 <= len(slots) <= 128:
        raise ValueError("Slot checks require bounded groove geometry.")
    tolerance = _limit(location.get("tolerance_mm") if tolerance_mm is None else tolerance_mm)
    if not isinstance(taught_stations, dict) or not isinstance(chips, dict):
        raise ValueError("Station and chip evidence must be explicit slot mappings.")
    if set(taught_stations) - set(slots) or set(chips) - set(slots):
        raise ValueError("Evidence names an unknown slot.")
    if not isinstance(same_location_slots, (list, tuple, set, frozenset)):
        raise ValueError("Same-location targets must be an explicit collection of slot IDs.")
    if any(not isinstance(ident, str) or ident not in slots for ident in same_location_slots):
        raise ValueError("A same-location target names an unknown slot.")
    same_location = set(same_location_slots)
    results = {}
    for ident, geometry in slots.items():
        if not isinstance(geometry, dict):
            raise ValueError("Invalid groove geometry.")
        segment = _segment(geometry.get("centerline_xy_mm"), "robot groove centerline")
        failures, missing = [], []
        station, chip = None, None
        station_error, chip_error, separation = None, None, None
        if ident not in taught_stations:
            missing.append("taught_station")
        else:
            station = _finite_array(taught_stations[ident], "taught station XY", shape=(2,))
            station_error = _segment_residual(station, segment)
            if station_error > tolerance:
                failures.append("station_outside_groove")
        observation = chips.get(ident, {"state": "unknown"})
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("state"), str)
            or observation["state"] not in {"present", "empty", "unknown"}
        ):
            raise ValueError("Chip evidence must state present, empty, or unknown.")
        state = observation["state"]
        if set(observation) != ({"state", "center_xy_mm"} if state == "present" else {"state"}):
            raise ValueError("Invalid chip observation fields.")
        if state == "unknown":
            missing.append("chip_observation")
        elif state == "present":
            chip = _finite_array(observation["center_xy_mm"], "observed chip XY", shape=(2,))
            chip_error = _segment_residual(chip, segment)
            if chip_error > tolerance:
                failures.append("chip_outside_groove")
        if station is not None and chip is not None:
            separation = float(np.linalg.norm(station - chip))
            if ident in same_location and separation > tolerance:
                failures.append("same_location_xy_mismatch")
        elif ident in same_location:
            missing.append("same_location_position")
        status = "failed" if failures else "incomplete" if missing else "ok"
        results[ident] = {
            "status": status,
            "station_residual_mm": station_error,
            "chip_state": state,
            "chip_residual_mm": chip_error,
            "station_chip_distance_mm": separation,
            "same_location": ident in same_location,
            "failures": failures,
            "missing": missing,
        }
    report = {
        "ok": all(item["status"] == "ok" for item in results.values()),
        "tolerance_mm": tolerance,
        "slots": results,
    }
    if not report["ok"]:
        raise TrayCheckError(report)
    return report


def _finite_number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite real number.")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite real number.") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite real number.")
    return number


def _fit_similarity(source_xy, target_xy, tolerance_mm):
    """Fit every measured corner; never drop a fourth observation or reflect."""
    tolerance_mm = _finite_number(tolerance_mm, "Marker residual tolerance")
    if not 0 < tolerance_mm <= 2:
        raise ValueError("Marker residual tolerance must be positive and at most 2 mm.")
    source = _finite_array(source_xy, "Tray corners")
    if source.shape not in {(3, 2), (4, 2)}:
        raise ValueError("Similarity fitting requires exactly 3 or 4 measured corners.")
    target = _finite_array(target_xy, "Robot corners", shape=source.shape)
    if any(len(np.unique(points, axis=0)) != len(points) for points in (source, target)):
        raise ValueError("Measured marker coordinates must be distinct.")
    means = source.mean(axis=0), target.mean(axis=0)
    centered = source - means[0], target - means[1]
    for points, original in zip(centered, (source, target)):
        singular = np.linalg.svd(points, compute_uv=False)
        resolution = 128 * np.finfo(float).eps * max(1.0, float(np.abs(original).max()))
        if singular[0] <= resolution or singular[1] <= singular[0] * 1e-8:
            raise ValueError("Marker geometry is collapsed or ill-conditioned.")
    left, values, right = np.linalg.svd(centered[0].T @ centered[1])
    rotation = right.T @ left.T
    if np.linalg.det(rotation) <= 0:
        raise ValueError("Corner identities imply a reflected tray pose.")
    scale = float(values.sum() / np.sum(centered[0] ** 2))
    if not math.isfinite(scale) or scale <= _MIN_SCALE:
        raise ValueError("Tray similarity scale is numerically collapsed.")
    translation = means[1] - scale * (rotation @ means[0])
    predicted = scale * (source @ rotation.T) + translation
    residuals = np.linalg.norm(predicted - target, axis=1)
    if not np.isfinite(residuals).all() or np.any(residuals > tolerance_mm):
        raise ValueError("Measured tray corner residual exceeds its allowed tolerance.")
    return {
        "scale": scale,
        "rotation": rotation.tolist(),
        "translation": translation.tolist(),
        "residuals_mm": residuals.tolist(),
    }


def _transform(points, fit):
    """Apply a finite proper similarity to points with trailing XY dimension."""
    source = _finite_array(points, "Tray coordinates")
    if source.ndim < 1 or source.shape[-1] != 2 or source.size == 0:
        raise ValueError("Tray coordinates require a nonempty trailing XY dimension.")
    if not isinstance(fit, dict):
        raise ValueError("A tray similarity is required.")
    scale = _finite_number(fit.get("scale"), "Tray similarity scale")
    if scale <= _MIN_SCALE:
        raise ValueError("Tray similarity scale must be finite and positive.")
    rotation = _finite_array(fit.get("rotation"), "Tray rotation", shape=(2, 2))
    translation = _finite_array(fit.get("translation"), "Tray translation", shape=(2,))
    if not np.allclose(rotation.T @ rotation, np.eye(2), atol=1e-9, rtol=0) or not math.isclose(
        float(np.linalg.det(rotation)), 1.0, abs_tol=1e-9, rel_tol=0
    ):
        raise ValueError("Tray rotation must be proper and orthonormal.")
    result = scale * (source @ rotation.T) + translation
    return _finite_array(result, "Transformed tray coordinates")


def _sample_missing(image, model, fit, marker_id, center_mm, size_mm=24):
    """Sample one predicted label; its result never becomes a measured corner.

    Square edges are assumed parallel to tray axes modulo 90 degrees. Dictionary
    rows run down the printed face, hence their local tray Y decreases. Arbitrary
    label rotation, nonplanarity, and occlusion can yield a diagnostic mismatch.
    No quiet-zone requirement is imposed on this separate cell check.
    """
    if type(marker_id) is not int or not 0 <= marker_id < 50 or marker_id == 4:
        raise ValueError("A configured tray corner marker ID is required.")
    size_mm = _finite_number(size_mm, "Marker black-square size")
    if not 0 < size_mm <= 1000:
        raise ValueError("Marker black-square size must be finite and positive.")
    center = _finite_array(center_mm, "Marker center", shape=(2,))
    geometry = validate_model(model)
    pixels = np.asarray(image)
    if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError("Marker sampling requires an RGB uint8 image.")
    height, width = pixels.shape[:2]
    if [width, height] != geometry["image_size"]:
        raise ValueError("Live image size differs from the stored camera calibration.")
    report = {
        "missing_id": marker_id,
        "status": "unavailable",
        "reason": None,
        "predicted_center_px": None,
        "data_bit_errors": None,
        "border_errors": None,
        "contrast": None,
        "rotation_deg": None,
        "used_for_pose": False,
    }
    matrix = np.asarray(geometry["camera_matrix"], np.float64)
    distortion = np.asarray(geometry["distortion"], np.float64)
    rotation = np.asarray(geometry["rotation"], np.float64)
    translation = np.asarray(geometry["translation"], np.float64)
    orientation = cv2.Rodrigues(rotation)[0]

    def project(local):
        try:
            xy = _transform(local, fit).reshape(-1, 2)
        except (ValueError, OverflowError):
            return None
        xyz = np.column_stack((xy, np.full(len(xy), geometry["tray_height_mm"])))
        depth = (xyz @ orientation.T + translation)[:, 2]
        if not np.isfinite(depth).all() or np.any(depth <= 1e-9):
            return None
        projected = cv2.projectPoints(xyz, rotation, translation, matrix, distortion)[0].reshape(
            -1, 2
        )
        return projected if np.isfinite(projected).all() else None

    projected_center = project(center)
    if projected_center is None:
        report["reason"] = "invalid_camera_depth_or_projection"
        return report
    if np.max(np.abs(projected_center)) <= _MAX_COORDINATE:
        report["predicted_center_px"] = projected_center[0].tolist()
    cell = float(size_mm) / 6
    rows, columns = np.indices((7, 7))
    boundaries = center + np.stack(((columns - 3) * cell, (3 - rows) * cell), axis=-1)
    projected = project(boundaries)
    if projected is None:
        report["reason"] = "invalid_camera_depth_or_projection"
        return report
    if np.any(projected < 0) or np.any(projected > [width - 1, height - 1]):
        report["reason"] = "out_of_frame"
        return report
    grid = projected.reshape(7, 7, 2)
    corners = np.stack((grid[:-1, :-1], grid[:-1, 1:], grid[1:, 1:], grid[1:, :-1]), axis=-2)
    edges = np.roll(corners, -1, axis=-2) - corners
    lengths = np.linalg.norm(edges, axis=-1)
    following = np.roll(corners, -1, axis=-2)
    area = 0.5 * np.abs(
        np.sum(corners[..., 0] * following[..., 1] - corners[..., 1] * following[..., 0], axis=-1)
    )
    if np.any(lengths <= 0) or np.any(area / lengths.max(axis=-1) < _MIN_CELL_PIXELS):
        report["reason"] = "insufficient_resolution"
        return report
    rows, columns = np.indices((6, 6))
    centers = center + np.stack(((columns - 2.5) * cell, (2.5 - rows) * cell), axis=-1)
    # Nine interior positions per cell avoid deciding bits at cell boundaries.
    offsets = np.array([(x, y) for y in (-0.2, 0, 0.2) for x in (-0.2, 0, 0.2)]) * cell
    projected = project(centers[..., None, :] + offsets)
    if projected is None:
        report["reason"] = "invalid_camera_depth_or_projection"
        return report
    if np.any(projected < 0) or np.any(projected >= [width - 1, height - 1]):
        report["reason"] = "out_of_frame"
        return report
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    left, top = np.floor(projected).astype(int).T
    dx, dy = (projected - np.floor(projected)).T
    samples = (
        gray[top, left] * (1 - dx) * (1 - dy)
        + gray[top, left + 1] * dx * (1 - dy)
        + gray[top + 1, left] * (1 - dx) * dy
        + gray[top + 1, left + 1] * dx * dy
    )
    values = np.median(samples.reshape(6, 6, 9), axis=-1)
    contrast = float(np.percentile(values, 90) - np.percentile(values, 10))
    report["contrast"] = contrast
    if contrast < _MIN_CONTRAST:
        report["reason"] = "low_contrast"
        return report
    _, observed = cv2.threshold(
        np.rint(values).astype(np.uint8), 0, 1, cv2.THRESH_BINARY | cv2.THRESH_OTSU
    )
    expected = (
        cv2.aruco.generateImageMarker(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), marker_id, 6
        )
        // 255
    )
    border = np.ones((6, 6), dtype=bool)
    border[1:5, 1:5] = False
    border_errors = int(np.count_nonzero(observed[border]))
    candidates = [
        (int(np.count_nonzero(observed[1:5, 1:5] != np.rot90(expected, turns)[1:5, 1:5])), turns)
        for turns in range(4)
    ]
    data_errors, turns = min(candidates)
    report.update(
        status="match" if data_errors == border_errors == 0 else "mismatch",
        reason=None if data_errors == border_errors == 0 else "cell_bits_do_not_match",
        data_bit_errors=data_errors,
        border_errors=border_errors,
        rotation_deg=90 * turns,
    )
    return report
