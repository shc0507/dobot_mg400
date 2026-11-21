"""Saved named stage coordinates, separate from Position Finder calibration."""

import copy
import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

FORMAT = "sample-transfer.calibration-profile"
MAX_PROFILE_BYTES = 128 * 1024
MAX_POSITIONS = 64
LEGACY_ENDPOINTS = {"fully_out": "Fully out", "fully_in": "Fully in"}
TRANSFER_ENDPOINTS = ("fully_out", "fully_in")
STAGE_POSITIONS = ("out_stage", "microscope")
LEGACY_TRANSFER_NAMES = {"fully_out": "Legacy Extract", "fully_in": "Legacy Insert"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value, label, limit=256):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must contain 1 to {limit} printable characters.")
    return value.strip()


def _identifier(value):
    if not isinstance(value, str):
        raise ValueError("Choose a saved transfer profile.")
    try:
        identifier = str(UUID(value))
    except ValueError as error:
        raise ValueError("Choose a saved transfer profile.") from error
    if identifier != value:
        raise ValueError("Choose a saved transfer profile.")
    return identifier


def _fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label} has missing or unsupported fields.")


def _timestamp(value):
    _text(value, "Capture time", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Use an ISO timestamp with a time zone.") from error
    if parsed.tzinfo is None:
        raise ValueError("Use an ISO timestamp with a time zone.")
    return value


def _coordinate(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError("Positions must be finite numeric coordinates.")
    return float(value)


def _source(value):
    _fields(value, ("camera", "device_id", "profile_id", "name"), "Position Finder source")
    if value["camera"] not in ("camera1", "camera2", "camera3", "camera4"):
        raise ValueError("Choose a configured optical camera.")
    return {
        "camera": value["camera"],
        "device_id": _text(value["device_id"], "Camera identity", 1024),
        "profile_id": _identifier(value["profile_id"]),
        "name": _text(value["name"], "Position Finder profile name", 256),
    }


def new_profile(name, source=None):
    return validate_profile(
        {
            "format": FORMAT,
            "schema_version": 5,
            "unit": "rotations",
            "id": str(uuid4()),
            "name": name,
            "created_at": _now(),
            "updated_at": _now(),
            "source": source,
            "positions": {},
            "stage_positions": {},
        }
    )


def position_name(value):
    return _text(value, "Position name", 120)


def validate_profile(value):
    """Validate imports and normalize legacy endpoints without rewriting their files."""
    if not isinstance(value, dict) or (
        value.get("format") != FORMAT
        or type(value.get("schema_version")) is not int
        or value["schema_version"] not in (1, 2, 3, 4, 5)
    ):
        raise ValueError("Unsupported Sample Transfer profile format or version.")
    legacy = value["schema_version"] == 1
    rotations = value["schema_version"] in (3, 4, 5)
    field = "endpoints" if legacy else "positions"
    _fields(
        value,
        ("format", "schema_version", "id", "name", "created_at", "updated_at", "source", field)
        + (("unit",) if rotations else ())
        + (("transfer_endpoints",) if value["schema_version"] == 4 else ())
        + (("stage_positions",) if value["schema_version"] == 5 else ()),
        "Transfer profile",
    )
    result = copy.deepcopy(value)
    result["id"] = _identifier(result["id"])
    result["name"] = _text(result["name"], "Profile name", 120)
    for key in ("created_at", "updated_at"):
        result[key] = _timestamp(result[key])
    records = result.pop(field)
    endpoints = result.pop("transfer_endpoints", {})
    stage_positions = result.get("stage_positions", {})
    if not isinstance(endpoints, dict) or set(endpoints) - set(TRANSFER_ENDPOINTS):
        raise ValueError("Choose Fully out or Fully in for transfer endpoints.")
    if not isinstance(stage_positions, dict) or set(stage_positions) - set(STAGE_POSITIONS):
        raise ValueError("Choose Out stage or Microscope for stage positions.")
    # Schema 4 can contain 64 named records and 2 additional endpoint captures.
    limit = MAX_POSITIONS + (2 if value["schema_version"] == 5 else 0)
    if not isinstance(records, dict) or len(records) > limit:
        raise ValueError(f"A profile can contain at most {limit} named positions.")
    if legacy:
        if set(records) - set(LEGACY_ENDPOINTS):
            raise ValueError("Unsupported legacy transfer endpoint.")
        records = {LEGACY_ENDPOINTS[name]: capture for name, capture in records.items()}
    elif any(position_name(name) != name for name in records):
        raise ValueError("Position names cannot start or end with spaces.")
    if rotations and result["unit"] != "rotations":
        raise ValueError("Encoder profiles must use rotations.")
    if result["source"] is not None:
        result["source"] = (
            encoder_source(result["source"]) if rotations else _source(result["source"])
        )
    elif records or endpoints or stage_positions:
        raise ValueError("Recorded positions need a position source.")
    for capture in (*records.values(), *endpoints.values()):
        coordinate_key = "position_rotations" if rotations else "position_mm"
        _fields(
            capture,
            (coordinate_key, "captured_at") + (() if rotations else ("image_sha256",)),
            "Position record",
        )
        positions = capture[coordinate_key]
        _fields(positions, ("x", "y", "insert"), "Position coordinates")
        # Legacy records can have unknown X/Y. Preserve those values on later saves.
        capture[coordinate_key] = {
            axis: _coordinate(positions[axis], optional=not rotations and axis != "insert")
            for axis in ("x", "y", "insert")
        }
        _timestamp(capture["captured_at"])
        if not rotations and (
            not isinstance(capture["image_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["image_sha256"])
        ):
            raise ValueError("The position image fingerprint is invalid.")
    if legacy and set(records) == set(LEGACY_ENDPOINTS.values()):
        if (
            records["Fully out"]["position_mm"]["insert"]
            >= records["Fully in"]["position_mm"]["insert"]
        ):
            raise ValueError("Fully in must have a larger insert coordinate than fully out.")
    for capture in stage_positions.values():
        _fields(capture, ("position_rotations", "captured_at"), "Stage position record")
        positions = capture["position_rotations"]
        _fields(positions, ("x", "y"), "Stage position coordinates")
        capture["position_rotations"] = {axis: _coordinate(positions[axis]) for axis in ("x", "y")}
        _timestamp(capture["captured_at"])
    for target, capture in endpoints.items():
        base = LEGACY_TRANSFER_NAMES[target]
        name, suffix = base, 2
        while name in records:
            name = f"{base} ({suffix})"
            suffix += 1
        records[name] = capture
    result.update(schema_version=5 if rotations else 2, positions=records)
    if rotations:
        result["stage_positions"] = stage_positions
    return result


def is_complete(profile):
    records = profile.get("positions", {}) if isinstance(profile, dict) else {}
    captures = list(records.values())
    stage_captures = profile.get("stage_positions", {}) if compatible(profile) else {}
    key = "position_rotations" if compatible(profile) else "position_mm"
    return bool(captures or stage_captures) and all(
        all(capture[key][axis] is not None for axis in ("x", "y", "insert")) for capture in captures
    )


def compatible(profile):
    return (
        isinstance(profile, dict)
        and profile.get("schema_version") in (3, 4, 5)
        and profile.get("unit") == "rotations"
    )


def encoder_source(value):
    _fields(value, ("kind", "device_id", "name"), "Encoder source")
    if value["kind"] != "stage_encoder":
        raise ValueError("Use the stage encoder coordinate source.")
    return {
        "kind": "stage_encoder",
        "device_id": _text(value["device_id"], "Stage identity", 1024),
        "name": _text(value["name"], "Stage name"),
    }


def _record_capture(profile, source, report, axes=("x", "y", "insert")):
    """Validate a fresh encoder reading before assigning it to the profile."""
    candidate = validate_profile(profile)
    if not compatible(candidate):
        raise ValueError("This profile uses legacy millimetres. Create a new rotation profile.")
    source = encoder_source(source)
    if candidate["source"] is not None and candidate["source"] != source:
        raise ValueError(
            "Use the same stage encoder source for all recorded positions. "
            "Start a new profile to change it."
        )
    if report.get("source") != source:
        raise ValueError("The measurement does not match the stage encoder source.")
    positions = {}
    for axis in axes:
        reading = report.get("axes", {}).get(axis, {})
        if reading.get("ready") is not True:
            raise ValueError(
                "Calibrate the required axes and wait for fresh, idle encoder feedback."
            )
        positions[axis] = _coordinate(reading.get("value_rotations"))
    candidate["source"] = source
    candidate["updated_at"] = _now()
    capture = {
        "position_rotations": positions,
        "captured_at": report.get("received_at"),
    }
    return candidate, capture


def record_position(profile, name, source, report):
    """Record the fresh, calibrated encoder position of every axis."""
    name = position_name(name)
    candidate, capture = _record_capture(profile, source, report)
    if name not in candidate["positions"] and len(candidate["positions"]) >= MAX_POSITIONS:
        raise ValueError("A profile can contain at most 64 named positions.")
    candidate["positions"][name] = capture
    return validate_profile(candidate)


def record_endpoint(profile, target, source, report):
    """Record an X/Y stage target without reading or storing Insert."""
    if target not in STAGE_POSITIONS:
        raise ValueError("Choose Out stage or Microscope.")
    candidate, capture = _record_capture(profile, source, report, axes=("x", "y"))
    candidate["stage_positions"][target] = capture
    return validate_profile(candidate)


def delete_endpoint(profile, target):
    candidate = validate_profile(profile)
    if target not in STAGE_POSITIONS:
        raise ValueError("Choose Out stage or Microscope.")
    if target not in candidate.get("stage_positions", {}):
        raise ValueError("Choose a recorded stage position.")
    del candidate["stage_positions"][target]
    candidate["updated_at"] = _now()
    return candidate


def delete_position(profile, name):
    candidate = validate_profile(profile)
    name = position_name(name)
    if name not in candidate["positions"]:
        raise ValueError("Choose a recorded position.")
    del candidate["positions"][name]
    candidate["updated_at"] = _now()
    return candidate


class TransferProfileStore:
    """Atomic profile files and a persistent selected profile; IDs never become paths."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _write(self, path, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(data) > MAX_PROFILE_BYTES:
            raise ValueError("The transfer profile exceeds 128 KiB.")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o640)
            os.replace(temporary, path)
            # Sync the directory too, so the rename survives a power loss.
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, identifier):
        with self.lock:
            path = self.root / "profiles" / (_identifier(identifier) + ".json")
            with path.open("rb") as stream:
                return self._parse(stream.read(MAX_PROFILE_BYTES + 1))

    @staticmethod
    def _parse(payload):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_PROFILE_BYTES:
            raise ValueError("Upload a transfer profile JSON file of at most 128 KiB.")
        try:
            value = json.loads(payload.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ValueError("The transfer profile must contain valid UTF-8 JSON.") from error
        return validate_profile(value)

    def list(self):
        with self.lock:
            profiles = []
            for path in (self.root / "profiles").glob("*.json"):
                try:
                    profile = self.load(path.stem)
                except (OSError, ValueError):
                    continue
                profiles.append(
                    {key: profile[key] for key in ("id", "name", "source", "updated_at")}
                    | {
                        "complete": is_complete(profile),
                        "position_count": len(profile["positions"]),
                        "stage_position_count": len(profile.get("stage_positions", {})),
                        "compatible": compatible(profile),
                        "unit": "rotations" if compatible(profile) else "mm",
                    }
                )
            return sorted(profiles, key=lambda item: item["updated_at"], reverse=True)

    def save(self, profile):
        with self.lock:
            candidate = validate_profile(profile)
            candidate.update(id=str(uuid4()), updated_at=_now())
            self._write(self.root / "profiles" / (candidate["id"] + ".json"), candidate)
            return candidate

    def upload(self, payload):
        return self.save(self._parse(payload))

    def selection(self):
        with self.lock:
            try:
                with (self.root / "selection.json").open("rb") as stream:
                    raw = stream.read(1025)
                if len(raw) > 1024:
                    raise ValueError("The saved transfer selection is invalid.")
                selection = json.loads(raw)
            except FileNotFoundError:
                return None
            _fields(selection, ("profile_id",), "Transfer selection")
            return _identifier(selection["profile_id"]) if selection["profile_id"] else None

    def selected(self):
        with self.lock:
            identifier = self.selection()
            return self.load(identifier) if identifier else None

    def select(self, identifier):
        with self.lock:
            profile = self.load(identifier)
            self._write(self.root / "selection.json", {"profile_id": profile["id"]})
            return profile

    def delete(self, identifier):
        with self.lock:
            self.load(identifier)
            selected = self.selection() == identifier
            if selected:
                self._write(self.root / "selection.json", {"profile_id": None})
            try:
                (self.root / "profiles" / (identifier + ".json")).unlink()
            except OSError:
                if selected:
                    self._write(self.root / "selection.json", {"profile_id": identifier})
                raise

    def export(self, identifier):
        return self.load(identifier)
