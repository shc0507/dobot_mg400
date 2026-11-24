"""Verify a fresh Console snapshot before it becomes model context."""

import copy
import json
import math
import re
import threading
from collections import OrderedDict

from labkit.errors import ServiceError

from .contract import DIALECT, digest
from .protocol import PREFIX, QUALITY

MAX_CONTEXT_BYTES = 262144
MODES = {"live": "live", "mock": "MOCK"}
UNITS = {
    "position_rotations": "rotation",
    "pv_c": "degC",
    "joints_deg": "degree",
    "pose_mm_deg": "mm,mm,mm,degree",
}
STATE_FIELDS = {
    "type",
    "boot_id",
    "contract_id",
    "seq",
    "event_seq",
    "generated_at",
    "mode",
    "devices",
    "samples",
    "operations",
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _bounded(value):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ServiceError(502, "The Console returned invalid JSON data.") from None
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ServiceError(502, "The Console context exceeds the supported size.")
    return value


def _discovery(value, source):
    if not isinstance(value, dict):
        raise ServiceError(502, "The Console discovery response is invalid.")
    identity = value.get("contract_id")
    if (
        type(value.get("discovery_version")) is not int
        or value["discovery_version"] != 1
        or value.get("read_only") is not True
        or value.get("mode") != MODES[source]
        or not isinstance(value.get("boot_id"), str)
        or not value["boot_id"]
        or not isinstance(identity, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity)
    ):
        raise ServiceError(502, "The Console read interface does not match this source.")
    expected = PREFIX + "/contracts/" + identity[7:] + ".json"
    endpoints = value.get("endpoints")
    if (
        value.get("contract_url") != expected
        or not isinstance(endpoints, dict)
        or endpoints.get("state") != PREFIX + "/v1/state"
    ):
        raise ServiceError(502, "The Console advertises an unsupported read path.")
    return identity, expected


def _compatible(bundle):
    if not isinstance(bundle, dict):
        return False
    semantics, schemas = bundle.get("semantics"), bundle.get("schemas")
    if not isinstance(semantics, dict) or not isinstance(schemas, dict):
        return False
    state = schemas.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("properties"), dict):
        return False
    required, units = state.get("required", []), semantics.get("units")
    type_schema = state["properties"].get("type")
    version = bundle.get("version")
    return (
        bundle.get("$schema") == DIALECT
        and bundle.get("contract") == "agent-connect"
        and isinstance(version, str)
        and bool(re.fullmatch(r"1\.[0-9]+\.[0-9]+", version))
        and isinstance(type_schema, dict)
        and type_schema.get("const") == "state"
        and isinstance(required, list)
        and all(isinstance(key, str) and key in STATE_FIELDS for key in required)
        and not bundle.get("required_capabilities")
        and isinstance(units, dict)
        and all(units.get(key) == unit for key, unit in UNITS.items())
    )


def _frame(value, discovery, source):
    if not isinstance(value, dict):
        raise ServiceError(502, "The Console state response is invalid.")
    if (
        value.get("type") != "state"
        or value.get("mode") != MODES[source]
        or value.get("boot_id") != discovery["boot_id"]
        or value.get("contract_id") != discovery["contract_id"]
        or any(type(value.get(key)) is not int or value[key] < 0 for key in ("seq", "event_seq"))
        or not _number(value.get("generated_at"))
        or value["generated_at"] < 0
        or any(not isinstance(value.get(key), dict) for key in ("devices", "samples", "operations"))
    ):
        raise ServiceError(502, "The state frame does not match the verified data agreement.")
    for row in value["devices"].values():
        if (
            not isinstance(row, dict)
            or row.get("quality") not in QUALITY
            or not isinstance(row.get("values"), dict)
        ):
            raise ServiceError(502, "A device reading has an unsupported format or quality.")
        for key in ("age_s", "frame_age_s", "sampled_at", "observed_at", "state_at"):
            number = row.get(key)
            if number is not None and (not _number(number) or number < 0):
                raise ServiceError(502, "A device reading has an invalid time or age.")
        values = row["values"]
        for key in ("pv_c", "sp_c", "temperature_c"):
            if values.get(key) is not None and not _number(values[key]):
                raise ServiceError(502, "A temperature reading is not a finite number.")
        axes = values.get("axes", [])
        if not isinstance(axes, list):
            raise ServiceError(502, "The stage axes have an unsupported format.")
        for axis in axes:
            if (
                not isinstance(axis, dict)
                or axis.get("unit", "rotation") != "rotation"
                or (axis.get("quality") is not None and axis["quality"] not in QUALITY)
                or (
                    axis.get("position_rotations") is not None
                    and not _number(axis["position_rotations"])
                )
            ):
                raise ServiceError(502, "A stage reading has an invalid value, unit or quality.")


class ChatContextReader:
    """Read known endpoints and preserve quality, gaps and partial-data metadata."""

    def __init__(self, readers):
        self.readers = dict(readers)
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def _read(self, source, path):
        try:
            return _bounded(self.readers[source](path))
        except ServiceError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise ServiceError(
                503, "The Console data is unavailable. Check the connection."
            ) from None

    def read(self, source):
        if not isinstance(source, str) or source not in MODES or source not in self.readers:
            raise ServiceError(400, "Choose live or mock.")
        discovery = self._read(source, PREFIX + "/discovery")
        identity, path = _discovery(discovery, source)
        key = source, identity
        with self.lock:
            bundle = copy.deepcopy(self.cache.get(key))
        if bundle is None:
            bundle = self._read(source, path)
            try:
                verified = digest(bundle) == identity
            except (ValueError, TypeError, RecursionError):
                verified = False
            if not verified:
                raise ServiceError(502, "The data agreement failed its integrity check.")
            if not _compatible(bundle):
                raise ServiceError(502, "The Console data agreement requires an updated agent.")
            with self.lock:
                self.cache[key] = copy.deepcopy(bundle)
                while len(self.cache) > 8:
                    self.cache.popitem(last=False)
        state = self._read(source, PREFIX + "/v1/state")
        latest = self._read(source, PREFIX + "/discovery")
        _discovery(latest, source)
        if any(latest.get(key) != discovery.get(key) for key in ("mode", "boot_id", "contract_id")):
            raise ServiceError(
                409, "The Console changed while reading. Send the instruction again."
            )
        _frame(state, discovery, source)
        return _bounded(
            {
                "discovery": copy.deepcopy(latest),
                "contract_id": identity,
                "units": copy.deepcopy(bundle["semantics"]["units"]),
                "state": copy.deepcopy(state),
            }
        )
