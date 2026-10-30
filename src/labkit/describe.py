"""JSON Schema descriptions for contract version 1."""

import copy
import json
import math

CONTRACT_VERSION = 1


def parameter(kind, **keywords):
    """Build a JSON Schema parameter with the caller's bounds, units, or description."""
    if kind not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
        raise ValueError("Unknown JSON Schema type.")
    if "type" in keywords:
        raise ValueError("Pass the schema type only through kind.")
    value = {"type": kind, **keywords}
    json.dumps(value, allow_nan=False)
    return copy.deepcopy(value)


def command(params=None, *, any_client=False):
    """Describe command arguments; the service validates them before device access."""
    if not isinstance(any_client, bool):
        raise ValueError("any_client must be a boolean.")
    if params is None:
        params = {"type": "object", "properties": {}, "additionalProperties": False}
    if not isinstance(params, dict) or params.get("type") != "object":
        raise ValueError("Command parameters must use an object JSON Schema.")
    json.dumps(params, allow_nan=False)
    return {"params": copy.deepcopy(params), "any_client": any_client}


def describe(*, service, version, demo, zones, lease_seconds=3, on_lease_expiry, commands):
    """Build discovery metadata without opening a device or starting a service."""
    if not all(isinstance(value, str) and value for value in (service, version, on_lease_expiry)):
        raise ValueError("Service, version, and expiry behavior must be nonempty strings.")
    if not isinstance(demo, bool):
        raise ValueError("demo must be a boolean.")
    if not isinstance(zones, (list, tuple)) or not all(isinstance(zone, str) for zone in zones):
        raise ValueError("zones must be a list of names.")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, (int, float))
        or not math.isfinite(lease_seconds)
        or lease_seconds <= 0
    ):
        raise ValueError("Lease duration must be a finite positive number.")
    if not isinstance(commands, dict):
        raise ValueError("commands must be a mapping.")
    normalized = {}
    for name, spec in commands.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("Each command needs a name and description.")
        if "params" not in spec or spec["params"] is None:
            raise ValueError("Each command needs an object parameter schema.")
        normalized[name] = command(spec["params"], any_client=spec.get("any_client"))
    result = {
        "service": service,
        "version": version,
        "contract": CONTRACT_VERSION,
        "demo": demo,
        "zones": list(zones),
        "lease_seconds": lease_seconds,
        "on_lease_expiry": on_lease_expiry,
        "commands": normalized,
    }
    json.dumps(result, allow_nan=False)
    return result
