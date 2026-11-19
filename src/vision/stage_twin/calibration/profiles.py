# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/calibration/profiles.py
"""Portable numerical calibration profiles; camera rasters stay in memory.

Profiles preserve marker observations, camera mode and hardware travel. Derived
axis fits are rebuilt on load, so incomplete drafts are safe to save and resume.
"""

import math
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

from ..marker_registry import ARUCO_DICTIONARY, MARKER_GROUPS
from .stage import AXES, fit_axis

PROFILE_FORMAT = "furnace-twin.calibration-profile"
DEFAULT_TRAVEL_MM = {"x": 45.0, "y": 40.0, "insert": 150.0}
MAX_FRAME_PIXELS = 24_000_000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
ENDPOINTS = {
    "x_low": {"axis": "x", "limit": "min", "label": "X low limit (left)"},
    "x_high": {"axis": "x", "limit": "max", "label": "X high limit (right)"},
    "y_low": {"axis": "y", "limit": "min", "label": "Y low limit (down)"},
    "y_high": {"axis": "y", "limit": "max", "label": "Y high limit (up)"},
    "insert_out": {"axis": "insert", "limit": "min", "label": "Insert fully out"},
    "insert_in": {"axis": "insert", "limit": "max", "label": "Insert fully in"},
}
AXIS_ENDPOINTS = {
    axis: tuple(key for key, value in ENDPOINTS.items() if value["axis"] == axis) for axis in AXES
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_name():
    return "Calibration " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H-%M-%S UTC")


def _mapping(value, label, allowed=None):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    if allowed is not None and set(value) - set(allowed):
        raise ValueError(
            f"{label} contains unsupported fields: {', '.join(sorted(set(value) - set(allowed)))}."
        )
    return value


def _text(value, label, maximum=256):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(
            f"{label} must be text of at most {maximum} characters without control characters."
        )
    return value.strip()


def _timestamp(value, label):
    value = _text(value, label, 40)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp with a timezone.") from exc
    return value


def validate_dimensions(value, *, allow_none=False):
    if value is None and allow_none:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(n) is not int or n < 1 or n > 16384 for n in value)
        or value[0] * value[1] > MAX_FRAME_PIXELS
    ):
        raise ValueError(
            "Camera dimensions must be positive integers within the 24 megapixel limit."
        )
    return list(value)


def _number(value, label, low, high):
    if type(value) not in (float, int):
        raise ValueError(f"{label} must be a finite number between {low} and {high}.")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} is too large.") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a finite number between {low} and {high}.")
    return value


def _marker_id(value, label):
    if type(value) is not int or not 0 <= value < 50:
        raise ValueError(f"{label} must be an integer ArUco ID from 0 to 49.")
    return value


def _point(value, dimensions, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain two pixel coordinates.")
    return [_number(number, label, 0, size - 1) for number, size in zip(value, dimensions)]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _marker(value, dimensions):
    value = _mapping(value, "Marker", ("id", "center_px", "corners_px"))
    marker_id = _marker_id(value.get("id"), "Marker ID")
    corners = value.get("corners_px")
    if not isinstance(corners, list) or len(corners) != 4:
        raise ValueError(f"ID {marker_id} needs four original-pixel corners.")
    corners = [_point(point, dimensions, f"ID {marker_id} corner") for point in corners]
    edges = [[corners[(i + 1) % 4][j] - corners[i][j] for j in (0, 1)] for i in range(4)]
    turns = [_cross(edges[i], edges[(i + 1) % 4]) for i in range(4)]
    if not (all(turn > 0 for turn in turns) or all(turn < 0 for turn in turns)):
        raise ValueError(f"ID {marker_id} corners must form a convex quadrilateral.")
    area = abs(sum(_cross(corners[i], corners[(i + 1) % 4]) for i in range(4))) / 2
    if area < 1:
        raise ValueError(f"ID {marker_id} marker area is too small.")
    center = _point(value.get("center_px"), dimensions, f"ID {marker_id} center")
    a, b, c, d = corners
    ac = [c[j] - a[j] for j in (0, 1)]
    bd = [d[j] - b[j] for j in (0, 1)]
    denominator = _cross(ac, bd)
    if abs(denominator) < 1e-9:
        raise ValueError(f"ID {marker_id} has degenerate corners.")
    t = _cross([b[j] - a[j] for j in (0, 1)], bd) / denominator
    intersection = [a[j] + t * ac[j] for j in (0, 1)]
    if math.dist(center, intersection) > 1.5:
        raise ValueError(f"ID {marker_id} center does not match its corners.")
    return {"id": marker_id, "center_px": center, "corners_px": corners}


def _observation(value):
    value = _mapping(
        value,
        "Endpoint observation",
        ("image_size_px", "detections", "ambiguous_ids", "image_sha256", "captured_at"),
    )
    dimensions = validate_dimensions(value.get("image_size_px"))
    detections = value.get("detections")
    if not isinstance(detections, list) or len(detections) > 50:
        raise ValueError("Endpoint detections must contain at most 50 markers.")
    detections = [_marker(marker, dimensions) for marker in detections]
    ids = [marker["id"] for marker in detections]
    if len(ids) != len(set(ids)):
        raise ValueError("A marker ID appears more than once in an endpoint.")
    ambiguous = value.get("ambiguous_ids", [])
    if not isinstance(ambiguous, list) or len(ambiguous) > 50:
        raise ValueError("Ambiguous marker IDs must be a list of at most 50 IDs.")
    ambiguous = [_marker_id(marker, "Ambiguous ID") for marker in ambiguous]
    if len(ambiguous) != len(set(ambiguous)) or set(ids) & set(ambiguous):
        raise ValueError("Ambiguous IDs must be unique and cannot also be accepted detections.")
    digest = value.get("image_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in digest)
    ):
        raise ValueError("Endpoint image_sha256 must be a 64-character hexadecimal hash.")
    return {
        "image_size_px": dimensions,
        "detections": detections,
        "ambiguous_ids": ambiguous,
        "image_sha256": digest.lower(),
        "captured_at": _timestamp(value.get("captured_at"), "Endpoint captured_at"),
    }


def _groups(value):
    value = _mapping(value, "marker_groups", MARKER_GROUPS)
    if set(value) != set(MARKER_GROUPS):
        raise ValueError("marker_groups must define desk, y, x and insert.")
    result = {}
    all_ids = []
    for name, group in MARKER_GROUPS.items():
        ids = value[name]
        if not isinstance(ids, list) or not ids or len(ids) > 50:
            raise ValueError(f"marker_groups.{name} needs a nonempty list of marker IDs.")
        ids = [_marker_id(marker, f"marker_groups.{name}") for marker in ids]
        if not set(ids) <= set(group.marker_ids):
            raise ValueError(f"marker_groups.{name} conflicts with the installed marker registry.")
        all_ids.extend(ids)
        result[name] = ids
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("A marker ID must belong to only one component.")
    return result


def new_profile(travel_mm=None, name=""):
    now = utc_now()
    profile = {
        "format": PROFILE_FORMAT,
        "schema_version": 1,
        "id": str(uuid4()),
        "name": name or default_name(),
        "created_at": now,
        "updated_at": now,
        "dictionary": ARUCO_DICTIONARY,
        "camera": {"dimensions": None, "device_id": "", "label": ""},
        "travel_mm": dict(DEFAULT_TRAVEL_MM if travel_mm is None else travel_mm),
        "marker_groups": {name: list(group.marker_ids) for name, group in MARKER_GROUPS.items()},
        "endpoints": {},
    }
    return evaluate_profile(profile)["profile"]


def _status(status, message, **detail):
    return {"status": status, "message": message, "reason": message, **detail}


def _role_error(observation, axis, groups):
    ids = {marker["id"] for marker in observation["detections"]}
    moving = groups[axis]
    parent_name = MARKER_GROUPS[axis].parent
    references = groups[parent_name]
    missing = []
    if not ids.intersection(moving):
        missing.append(
            "moving "
            + MARKER_GROUPS[axis].label
            + " marker (need "
            + ", ".join(map(str, moving))
            + ")"
        )
    if not ids.intersection(references):
        missing.append(
            "reference "
            + MARKER_GROUPS[parent_name].label
            + " marker (need "
            + ", ".join(map(str, references))
            + ")"
        )
    if missing:
        visible = ", ".join(map(str, sorted(ids))) or "none"
        return "Missing " + " and ".join(missing) + f". Accepted IDs: {visible}."
    return None


def evaluate_profile(value):
    """Validate and refit a profile, allowing incomplete or failed endpoint pairs.

    Invalid structure raises ValueError. Valid observations with missing roles,
    mixed camera dimensions or unusable pairs remain saveable with error status.
    """
    value = _mapping(
        value,
        "Calibration profile",
        (
            "format",
            "schema_version",
            "id",
            "name",
            "created_at",
            "updated_at",
            "dictionary",
            "camera",
            "travel_mm",
            "marker_groups",
            "endpoints",
            "axes",
            "status",
            "endpoint_status",
            "axis_status",
        ),
    )
    if (
        value.get("format") != PROFILE_FORMAT
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("Choose a supported Furnace Twin calibration profile (schema version 1).")
    if value.get("dictionary") != ARUCO_DICTIONARY:
        raise ValueError(f"This project requires {ARUCO_DICTIONARY} markers.")
    try:
        identifier = str(UUID(_text(value.get("id"), "Profile id", 36)))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Profile id must be a UUID.") from exc
    name = _text(value.get("name", ""), "Profile name", 120) or default_name()
    camera = _mapping(value.get("camera"), "camera", ("dimensions", "device_id", "label"))
    camera = {
        "dimensions": validate_dimensions(camera.get("dimensions"), allow_none=True),
        "device_id": _text(camera.get("device_id", ""), "Camera device ID", 512),
        "label": _text(camera.get("label", ""), "Camera label", 256),
    }
    travel = _mapping(value.get("travel_mm"), "travel_mm", AXES)
    if set(travel) != set(AXES):
        raise ValueError("travel_mm must define x, y and insert.")
    travel = {axis: _number(travel[axis], f"{axis} travel in mm", 1e-6, 1_000_000) for axis in AXES}
    groups = _groups(value.get("marker_groups"))
    raw_endpoints = _mapping(value.get("endpoints"), "endpoints", ENDPOINTS)
    endpoints = {key: _observation(observation) for key, observation in raw_endpoints.items()}
    if camera["dimensions"] is None and endpoints:
        camera["dimensions"] = next(iter(endpoints.values()))["image_size_px"]
    profile = {
        "format": PROFILE_FORMAT,
        "schema_version": 1,
        "id": identifier,
        "name": name,
        "created_at": _timestamp(value.get("created_at"), "Profile created_at"),
        "updated_at": _timestamp(value.get("updated_at"), "Profile updated_at"),
        "dictionary": ARUCO_DICTIONARY,
        "camera": camera,
        "travel_mm": travel,
        "marker_groups": groups,
        "endpoints": endpoints,
    }
    endpoint_status, axis_status, axes = {}, {}, {}
    for key, definition in ENDPOINTS.items():
        observation = endpoints.get(key)
        if observation is None:
            endpoint_status[key] = _status("missing", "Not captured yet.")
            continue
        if observation["image_size_px"] != camera["dimensions"]:
            endpoint_status[key] = _status(
                "error",
                "Camera resolution differs from this profile. "
                "Use the same camera mode for every endpoint.",
            )
            continue
        error = _role_error(observation, definition["axis"], groups)
        endpoint_status[key] = (
            _status("error", error)
            if error
            else _status("captured", "Markers captured; waiting for the other endpoint.")
        )
    for axis, (low, high) in AXIS_ENDPOINTS.items():
        if any(endpoint_status[key]["status"] == "error" for key in (low, high)):
            axis_status[axis] = _status(
                "error", "Correct the endpoint error before this axis can be calibrated."
            )
            continue
        missing = [ENDPOINTS[key]["label"] for key in (low, high) if key not in endpoints]
        if missing:
            axis_status[axis] = _status("pending", "Capture " + " and ".join(missing) + ".")
            continue
        allowed_ids = set(groups[axis] + groups[MARKER_GROUPS[axis].parent])
        photos = {
            filename: {
                "detections": [
                    marker for marker in endpoints[key]["detections"] if marker["id"] in allowed_ids
                ]
            }
            for filename, key in zip(AXES[axis][:2], (low, high))
        }
        try:
            fitted = fit_axis(axis, photos, travel[axis])
        except ValueError as exc:
            message = str(exc)
            if "No moving/reference" in message:
                message = (
                    "No identical moving/reference marker pair is readable in both endpoints. "
                    "Recapture with the same pair visible."
                )
            elif "Subpixel endpoint" in message:
                message = (
                    "Endpoints have too little relative movement. "
                    "Move the axis to its opposite limit and snap again."
                )
            axis_status[axis] = _status("error", message)
            for key in (low, high):
                endpoint_status[key] = _status("error", message)
            continue
        fitted["span_source"] = "profile hardware travel snapshot"
        fitted["input_endpoints"] = {"min": low, "max": high}
        fitted.pop("input_photos", None)
        axes[axis] = fitted
        count = len(fitted["tracks"])
        message = f"Calibrated over {travel[axis]:g} mm using {count} marker pair" + (
            "s." if count != 1 else "."
        )
        axis_status[axis] = _status("calibrated", message, track_count=count)
        for key in (low, high):
            endpoint_status[key] = _status(
                "calibrated", "Capture accepted; axis calibration is ready.", track_count=count
            )
    profile.update(
        axes=axes,
        status="complete" if len(axes) == len(AXES) else "partial" if endpoints else "empty",
        endpoint_status=endpoint_status,
        axis_status=axis_status,
    )
    return {"profile": profile, "endpoint_status": endpoint_status, "axis_status": axis_status}


def analyze_frame(data, expected_dimensions=None, *, detector=None):
    """Decode and detect one bounded JPEG/PNG without retaining its pixels."""
    from PIL import Image, UnidentifiedImageError

    from ..extractors.aruco import decode_color, decode_gray, detect_markers

    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Send a JPEG or PNG camera frame of at most 16 MiB.")
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG")) as header:
            dimensions = validate_dimensions(header.size)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError("A readable JPEG or PNG camera frame is required.") from exc
    if expected_dimensions is not None and dimensions != list(expected_dimensions):
        raise ValueError(
            f"Frame is {dimensions[0]} x {dimensions[1]} px. This calibration requires "
            f"{expected_dimensions[0]} x {expected_dimensions[1]} px in the original camera view."
        )
    gray = decode_gray(data)
    if list(gray.shape[::-1]) != dimensions:
        raise ValueError("Decoded dimensions do not match the image header.")
    if detector is None:
        detector = detect_markers
    observation = detector(gray, decode_color(data))
    return {**observation, "image_sha256": sha256(data).hexdigest(), "captured_at": utc_now()}


def _numerical_observation(observation):
    return {
        "image_size_px": observation["image_size_px"],
        "detections": [
            {key: marker[key] for key in ("id", "center_px", "corners_px")}
            for marker in observation["detections"]
        ],
        "ambiguous_ids": observation.get("ambiguous_ids", []),
        "image_sha256": observation.get("image_sha256", observation.get("sha256")),
        "captured_at": observation.get("captured_at", utc_now()),
    }


def capture_endpoint(data, endpoint):
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown calibration endpoint.")
    observation = _observation(_numerical_observation(analyze_frame(data)))
    error = _role_error(
        observation,
        ENDPOINTS[endpoint]["axis"],
        {name: list(group.marker_ids) for name, group in MARKER_GROUPS.items()},
    )
    if error:
        raise ValueError(error)
    return observation
