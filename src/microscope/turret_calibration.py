"""Turret profiles that store each seated objective's marker coordinates.

Capture and use must share the camera position and camera settings.
Recorded focus position is metadata. The service checks live focus clearance before movement.
Offsets use pixels at an image width of 3840, independent of capture resolution.
"""

import base64
import binascii
import copy
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .aruco_locator import COLORS, detect_objectives, marker_center
from .paths import calibration_path

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 256 * 1024
REFERENCE_WIDTH = 3840
DICTIONARY = "DICT_4X4_50"
_LEGACY_REFERENCE = {"dictionary": DICTIONARY, "id": 35, "side_mm": 30.0}
_PROFILE_KEYS = {
    "schema_version",
    "profile_id",
    "name",
    "camera",
    "focus_position",
    "dictionary",
    "objective_side_mm",
    "captures",
}
_CAPTURE_KEYS = {"shape", "objective_corners", "preview_jpeg"}


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"Invalid {label} fields")


def _number(value, label):
    try:
        finite = math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _profile_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("Invalid profile ID")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{label} must contain 1 to 128 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must not contain control characters")
    return value


def _shape(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Image shape must contain height and width")
    if any(type(size) is not int or not 16 <= size <= 16384 for size in value):
        raise ValueError("Image dimensions must be integers from 16 to 16384")
    return value


def _corners(value, shape):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("Marker corners must contain 4 points")
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("Each marker corner must contain 2 coordinates")
        for coordinate in point:
            _number(coordinate, "Marker coordinate")
    points = np.asarray(value, dtype=np.float64)
    height, width = shape
    if (points < -0.5).any() or (points >= [width - 0.5, height - 0.5]).any():
        raise ValueError("Marker corners must be inside the image")
    edges = np.roll(points, -1, axis=0) - points
    turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if (turns <= 0).any() or (np.linalg.norm(edges, axis=1) < 2).any():
        raise ValueError("Marker corners must form a clockwise convex quadrilateral")
    if cv2.contourArea(points.astype(np.float32)) < 4:
        raise ValueError("Marker area is too small")
    return points


def _preview(value):
    if not isinstance(value, str) or len(value) > 4 * ((MAX_PREVIEW_BYTES + 2) // 3):
        raise ValueError("Preview JPEG is too large")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Preview JPEG is not valid base64") from exc
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_PREVIEW_BYTES:
        raise ValueError("Preview must be a bounded JPEG")
    # Read JPEG dimensions before decoding, so uploads cannot allocate a large image.
    offset = 2
    dimensions = None
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8:
                break
            dimensions = (
                int.from_bytes(data[offset + 3 : offset + 5], "big"),
                int.from_bytes(data[offset + 5 : offset + 7], "big"),
            )
            break
        offset += length
    if dimensions is None or any(not 1 <= size <= 640 for size in dimensions):
        raise ValueError("Preview dimensions must not exceed 640 pixels")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != dimensions:
        raise ValueError("Preview JPEG cannot be decoded")


def validate_profile(profile):
    """Validate measurements and convert version 1 to version 2 without changing files."""
    if not isinstance(profile, dict):
        raise ValueError("Invalid profile fields")
    version = profile.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unsupported turret profile version")
    keys = _PROFILE_KEYS - {"dictionary"} | {"reference"} if version == 1 else _PROFILE_KEYS
    _fields(profile, keys, "profile")
    _profile_id(profile["profile_id"])
    _text(profile["name"], "Profile name")
    _text(profile["camera"], "Camera")
    if profile["focus_position"] is not None:
        _number(profile["focus_position"], "Focus position")
    if version == 1:
        _fields(profile["reference"], set(_LEGACY_REFERENCE), "reference")
        if type(profile["reference"]["id"]) is not int or profile["reference"] != _LEGACY_REFERENCE:
            raise ValueError("Reference must be ID 35, DICT_4X4_50, with a 30 mm black square")
    elif profile["dictionary"] != DICTIONARY:
        raise ValueError("Marker dictionary must be DICT_4X4_50")
    if _number(profile["objective_side_mm"], "Objective marker size") != 8:
        raise ValueError("Objective marker size must be 8 mm")
    captures = profile["captures"]
    if not isinstance(captures, dict) or not set(captures) <= {str(i) for i in COLORS}:
        raise ValueError("Capture IDs must be 0, 1, 2, or 3")
    first_shape = None
    for record in captures.values():
        keys = _CAPTURE_KEYS | {"reference_corners"} if version == 1 else _CAPTURE_KEYS
        _fields(record, keys, "capture")
        shape = _shape(record["shape"])
        if first_shape and first_shape[0] * shape[1] != first_shape[1] * shape[0]:
            raise ValueError("Capture image aspect ratios must agree")
        first_shape = shape
        if version == 1:
            _corners(record["reference_corners"], shape)
        _corners(record["objective_corners"], shape)
        _preview(record["preview_jpeg"])
    normalized = copy.deepcopy(profile)
    if version == 1:
        normalized["schema_version"] = 2
        normalized["dictionary"] = normalized.pop("reference")["dictionary"]
        for record in normalized["captures"].values():
            del record["reference_corners"]
    return normalized


def new_profile(name, camera, focus_position=None):
    return validate_profile(
        {
            "schema_version": 2,
            "profile_id": uuid.uuid4().hex,
            "name": name,
            "camera": camera,
            "focus_position": focus_position,
            "dictionary": DICTIONARY,
            "objective_side_mm": 8.0,
            "captures": {},
        }
    )


def is_complete(profile):
    return set(profile["captures"]) == {str(i) for i in COLORS}


def _image(image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        raise ValueError("Capture must be an unsigned 8-bit image")
    if image.ndim not in (2, 3) or (image.ndim == 3 and image.shape[2] != 3):
        raise ValueError("Capture must be a grayscale or BGR image")
    _shape(list(image.shape[:2]))


def capture(image, objective_id):
    """Measure the objective in one supplied frame. The caller checks frame freshness."""
    _image(image)
    if type(objective_id) is not int or objective_id not in COLORS:
        raise ValueError("Objective ID must be 0, 1, 2, or 3")
    detection = detect_objectives(image, target_id=objective_id)
    if detection["status"] != "ok":
        raise ValueError(f"Cannot capture objective {objective_id}: {detection['status']}")
    objective = next(marker for marker in detection["markers"] if marker["id"] == objective_id)
    h, w = image.shape[:2]
    scale = min(1.0, 640 / max(h, w))
    preview = cv2.resize(image, (round(w * scale), round(h * scale)))
    ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok or len(encoded) > MAX_PREVIEW_BYTES:
        raise ValueError("Cannot encode a bounded capture preview")
    record = {
        "shape": [h, w],
        "objective_corners": objective["corners"],
        "preview_jpeg": base64.b64encode(encoded).decode("ascii"),
    }
    _corners(record["objective_corners"], record["shape"])
    return record


def _image_center(corners, width):
    # Pixel centers map as (x + 0.5) * scale - 0.5 between image sizes.
    return (marker_center(corners) + 0.5) * REFERENCE_WIDTH / width - 0.5


def stable_capture(images, objective_id):
    """Capture at least 3 fresh frames whose image centers agree within 5 normalized pixels."""
    images = list(images)
    if len(images) < 3:
        raise ValueError("Capture needs at least 3 fresh frames")
    records = [capture(image, objective_id) for image in images]
    height, width = records[0]["shape"]
    if any(record["shape"][0] * width != record["shape"][1] * height for record in records):
        raise ValueError("Capture image aspect ratios must agree")
    centers = np.array(
        [_image_center(record["objective_corners"], record["shape"][1]) for record in records]
    )
    if np.max(np.linalg.norm(centers[:, None] - centers[None, :], axis=2)) > 5:
        raise ValueError("Objective marker moved during capture")
    return records[-1]


class ProfileLocator:
    def __init__(self, profile, near_px=50.0):
        self.profile = validate_profile(profile)
        if not is_complete(self.profile):
            raise ValueError("Turret profile is incomplete")
        self.near_px = _number(near_px, "Near threshold")
        self.references = {
            int(ident): {
                "id": int(ident),
                "shape": record["shape"],
                "corners": record["objective_corners"],
                "center": marker_center(record["objective_corners"]).tolist(),
            }
            for ident, record in self.profile["captures"].items()
        }

    def locate(self, image, target_id=None, *, only_target=True):
        _image(image)
        detection = detect_objectives(image, target_id=target_id)
        result = dict(
            detection,
            markers=[],
            profile_id=self.profile["profile_id"],
            offset_width_px=REFERENCE_WIDTH,
        )
        if detection["status"] != "ok":
            return result
        for marker in detection["markers"]:
            if target_id is not None and only_target and marker["id"] != target_id:
                continue
            record = self.profile["captures"][str(marker["id"])]
            height, width = detection["shape"]
            ref_height, ref_width = record["shape"]
            if height * ref_width != width * ref_height:
                return dict(
                    result,
                    status="image_size_mismatch",
                    reference_shape=record["shape"],
                    markers=[],
                )
            delta = _image_center(marker["corners"], width) - _image_center(
                record["objective_corners"], ref_width
            )
            dx, dy = (float(value) for value in delta)
            result["markers"].append(
                dict(
                    marker,
                    dx_px=dx,
                    dy_px=dy,
                    range="near" if abs(dx) <= self.near_px else "far",
                    direction="left" if dx < 0 else "right" if dx > 0 else "zero",
                )
            )
        return result


def _decode(data):
    if not isinstance(data, bytes) or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Profile upload must not exceed 2 MiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Profile upload is not valid JSON") from exc


class TurretProfileStore:
    """Store validated JSON files with atomic replacement and a persistent selection."""

    def __init__(self, root=None):
        self._root = Path(root) if root is not None else None
        self._lock = threading.RLock()

    @property
    def root(self):
        return self._root if self._root is not None else calibration_path("turret_profiles")

    def _path(self, profile_id):
        return self.root / f"{_profile_id(profile_id)}.json"

    def _atomic(self, path, value):
        data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Profile JSON must not exceed 2 MiB")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor, temporary = tempfile.mkstemp(prefix=".turret-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, profile):
        profile = validate_profile(profile)
        with self._lock:
            selected = self.selection_status()["profile_id"]
            if selected == profile["profile_id"] and not is_complete(profile):
                raise ValueError("The selected profile must remain complete")
            self._atomic(self._path(profile["profile_id"]), profile)
        return profile

    def load(self, profile_id):
        path = self._path(profile_id)
        with self._lock:
            with path.open("rb") as source:
                profile = validate_profile(_decode(source.read(MAX_UPLOAD_BYTES + 1)))
        if profile["profile_id"] != profile_id:
            raise ValueError("Stored profile ID does not match its filename")
        return profile

    def list(self):
        with self._lock:
            summaries = []
            for path in sorted(self.root.glob("*.json")):
                if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                    continue
                profile = self.load(path.stem)
                summaries.append(
                    {
                        "profile_id": profile["profile_id"],
                        "name": profile["name"],
                        "camera": profile["camera"],
                        "focus_position": profile["focus_position"],
                        "complete": is_complete(profile),
                        "objective_ids": sorted(int(key) for key in profile["captures"]),
                        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                    }
                )
            return summaries

    def selection_status(self):
        with self._lock:
            path = self.root / "selection.json"
            try:
                with path.open("rb") as source:
                    value = _decode(source.read(MAX_UPLOAD_BYTES + 1))
            except FileNotFoundError:
                return {"ever_selected": False, "profile_id": None}
            _fields(value, {"ever_selected", "profile_id"}, "selection")
            if value["ever_selected"] is not True:
                raise ValueError("Invalid stored turret selection")
            if value["profile_id"] is not None:
                _profile_id(value["profile_id"])
            return value

    def select(self, profile_id):
        with self._lock:
            profile = self.load(profile_id) if profile_id is not None else None
            if profile is not None and not is_complete(profile):
                raise ValueError("Turret profile is incomplete")
            self._atomic(
                self.root / "selection.json", {"ever_selected": True, "profile_id": profile_id}
            )
            return profile

    def selected(self):
        with self._lock:
            ident = self.selection_status()["profile_id"]
            if ident is None:
                return None
            try:
                profile = self.load(ident)
            except FileNotFoundError as exc:
                raise ValueError("The selected turret profile is missing") from exc
            if not is_complete(profile):
                raise ValueError("The selected turret profile is incomplete")
            return profile

    def delete(self, profile_id):
        with self._lock:
            path = self._path(profile_id)
            if not path.exists():
                raise FileNotFoundError(path)
            was_selected = self.selection_status()["profile_id"] == profile_id
            if was_selected:
                self.select(None)
            try:
                path.unlink()
            except OSError:
                if was_selected:
                    self.select(profile_id)
                raise

    def upload(self, data):
        profile = validate_profile(_decode(data))
        profile["profile_id"] = uuid.uuid4().hex
        return self.save(profile)

    def export(self, profile_id):
        return json.dumps(self.load(profile_id), allow_nan=False, indent=2).encode("utf-8")
