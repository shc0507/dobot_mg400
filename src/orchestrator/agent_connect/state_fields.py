"""Bounded public device fields, with explicit omissions and source units."""

import json
import math
import re

MAX_BYTES = 16384
MAX_NODES = 512
MAX_ITEMS = 128
MAX_DEPTH = 8
MAX_TEXT = 512
MAX_OMISSIONS = 32
_OMIT = object()
_PRIVATE = re.compile(
    r"token|secret|password|credential|authorization|cookie|private_?key|api_?key|access_key|session_key",
    re.I,
)
_EXCLUDED = {
    "image",
    "images",
    "image_bytes",
    "image_data",
    "image_b64",
    "video_data",
    "frame",
    "jpeg",
    "jpg",
    "png",
    "pixels",
    "raw",
    "ir",
    "matrix",
    "matrices",
    "temperatures_c",
    "thermal_matrix",
    "video",
    "frames_data",
    "segments",
    "events",
    "history",
    "output",
    "stdout",
    "stderr",
    "traceback",
    "environment",
    "env",
    "headers",
}

# These legacy names have units verified in their device snapshot implementations.
_LEGACY_UNITS = {
    "robot": {"joints": "degree", "speed_factor": "percent"},
    "furnace": {"pv": "degC", "sv": "degC", "stored_sv": "degC", "ramp": "degC/min"},
}
_SUFFIX_UNITS = {
    "_c_per_min": "degC/min",
    "_celsius": "degC",
    "_c": "degC",
    "_mm": "mm",
    "_deg": "degree",
    "_rotations": "rotation",
    "_pulses": "pulse",
    "_steps": "step",
    "_pct": "percent",
    "_percent": "percent",
    "_kg": "kg",
    "_ms": "ms",
    "_s": "s",
    "_seconds": "s",
    "_minutes": "min",
    "_px": "pixel",
}


def field_unit(device, key, *, index=None):
    if device == "robot" and key == "actual":
        return "mm" if index in (0, 1, 2) else "degree" if index == 3 else None
    if key in _LEGACY_UNITS.get(device, {}):
        return _LEGACY_UNITS[device][key]
    if key in {"ts_unix", "sample_time"}:
        return "unix_s"
    if key == "sampled_at":
        return "monotonic_s" if device == "stage" else None
    return next((unit for suffix, unit in _SUFFIX_UNITS.items() if key.endswith(suffix)), None)


def cached_fields(state, device):
    """Copy finite JSON fields. Never fetch data or assign unreported units."""
    remaining, nodes, omitted = MAX_BYTES, MAX_NODES, []
    omitted_count = 0
    units = {}

    def omit(path, reason):
        nonlocal omitted_count
        omitted_count += 1
        if len(omitted) < MAX_OMISSIONS:
            omitted.append({"path": path[:256], "reason": reason})
        return _OMIT

    def visit(value, path, depth, key, index=None):
        nonlocal remaining, nodes
        nodes -= 1
        if nodes < 0 or remaining < 16:
            return omit(path, "size_limit")
        if depth > MAX_DEPTH:
            return omit(path, "depth_limit")
        if len(path) > 256:
            return omit(path, "path_limit")
        if _PRIVATE.search(key) or key.lower() in _EXCLUDED:
            return omit(path, "private_or_bulk_data")
        if key.lower().endswith(("_url", "_path")) or key.lower() in {"url", "path"}:
            return omit(path, "location")
        if isinstance(value, dict):
            result = {}
            for index, (child, item) in enumerate(value.items()):
                if index >= MAX_ITEMS or nodes < 0 or remaining < 16:
                    omit(path + "/*", "size_limit")
                    break
                if not isinstance(child, str) or len(child) > 128:
                    omit(path + "/*", "invalid_key")
                    continue
                pointer = path + "/" + child.replace("~", "~0").replace("/", "~1")
                remaining -= len(json.dumps(child).encode()) + 4
                copied = visit(item, pointer, depth + 1, child)
                if copied is not _OMIT:
                    result[child] = copied
            return result
        if isinstance(value, (list, tuple)):
            # Pixel and thermal matrices are bulk data, even with an unknown field name.
            if any(isinstance(item, (list, tuple)) for item in value[:MAX_ITEMS]):
                return omit(path, "matrix")
            if len(value) > MAX_ITEMS:
                omit(path + "/*", "item_limit")
            result = []
            for index, item in enumerate(value[:MAX_ITEMS]):
                if nodes < 0 or remaining < 16:
                    omit(path + "/*", "size_limit")
                    break
                copied = visit(item, path + "/" + str(index), depth + 1, key, index)
                result.append(None if copied is _OMIT else copied)
            return result
        if type(value) in (int, float):
            if type(value) is float and not math.isfinite(value):
                return omit(path, "nonfinite")
        elif isinstance(value, str):
            if len(value) > MAX_TEXT:
                omit(path, "text_limit")
                value = value[:MAX_TEXT]
            if value.startswith(("data:", "Bearer ", "-----BEGIN ")):
                return omit(path, "private_or_bulk_data")
        elif value is not None and type(value) is not bool:
            return omit(path, "unsupported_type")
        if value is None or type(value) in (int, float):
            unit = field_unit(device, key, index=index)
            if unit is not None:
                units[path] = unit
                remaining -= len(json.dumps({path: unit}).encode())
        remaining -= len(json.dumps(value).encode()) + 4
        if remaining < 0:
            units.pop(path, None)
            return omit(path, "size_limit")
        return value

    values = visit(state, "", 0, "")
    return {
        "values": {} if values is _OMIT else values,
        "complete": omitted_count == 0,
        "omitted": omitted,
        "omitted_count": omitted_count,
        "units": units,
    }
