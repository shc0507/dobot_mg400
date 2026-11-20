"""Immutable mock contracts, separate from the instrument-service contract.

Fixture bundles use the JCS subset with ASCII keys, strings, booleans, null,
arrays and safe integers. Reject other numbers instead of claiming general JCS
support. Runtime readings never enter a bundle or its digest.
"""

import copy
import hashlib
import json

DIALECT = "https://json-schema.org/draft/2020-12/schema"
FIXTURES = ("baseline", "compatible", "incompatible", "unsupported-dialect", "broken-digest")
PREFIX = "/lab/api/agent-fixtures"


def canonical_bytes(value):
    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key.isascii():
                    raise ValueError("Fixture contract keys must be ASCII strings.")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif type(item) is int:
            if abs(item) > 2**53 - 1:
                raise ValueError("Fixture integers must be exactly representable.")
        elif item is not None and not isinstance(item, (str, bool)):
            raise ValueError("Fixture contracts do not accept floating-point numbers.")

    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(bundle):
    return "sha256:" + hashlib.sha256(canonical_bytes(bundle)).hexdigest()


def fixture(name="baseline"):
    if name not in FIXTURES:
        raise ValueError("Unknown contract fixture.")
    incompatible = name == "incompatible"
    bundle = {
        "$schema": DIALECT,
        "contract": "agent-connect-mock",
        "version": "2.0.0" if incompatible else "1.1.0" if name == "compatible" else "1.0.0",
        "mode": "MOCK",
        "phase": 0,
        "state_semantics": {
            "stage.position": {
                "unit": "mm" if incompatible else "rotation",
                "frame": "mock-axis-reference",
                "kind": "absolute",
            },
            "temperature.value": {"unit": "degC", "sample_time": "source capture, Unix seconds"},
            "quality": ["good", "stale", "unknown"],
        },
        "schemas": {
            "state": {
                "$schema": DIALECT,
                "type": "object",
                "required": [
                    "mode",
                    "boot_id",
                    "contract_id",
                    "seq",
                    "sampled_at",
                    "age_s",
                    "quality",
                    "stage",
                    "temperature",
                    "samples",
                ],
                "properties": {
                    "mode": {"const": "MOCK"},
                    "boot_id": {"type": "string"},
                    "contract_id": {"type": "string"},
                    "seq": {"type": "integer"},
                    "sampled_at": {"type": "number", "description": "Source time in Unix seconds."},
                    "age_s": {"type": "number", "minimum": 0},
                    "quality": {"enum": ["good", "stale", "unknown"]},
                    "stage": {
                        "type": "object",
                        "required": [
                            "position",
                            "unit",
                            "instance",
                            "reference_revision",
                            "quality",
                        ],
                        "properties": {
                            "position": {"type": ["number", "null"]},
                            "unit": {"const": "mm" if incompatible else "rotation"},
                            "instance": {"type": "string"},
                            "reference_revision": {"type": "integer", "minimum": 1},
                            "quality": {"enum": ["good", "stale", "unknown"]},
                        },
                    },
                    "temperature": {
                        "type": "object",
                        "required": ["value", "unit", "quality"],
                        "properties": {
                            "value": {"type": ["number", "null"]},
                            "unit": {"const": "degC"},
                            "quality": {"enum": ["good", "stale", "unknown"]},
                        },
                    },
                    "samples": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["chip_id", "holder", "basis"],
                            "properties": {
                                "chip_id": {"type": "string"},
                                "holder": {"type": ["string", "null"]},
                                "basis": {"type": "string"},
                                "pending_action": {"type": ["string", "null"]},
                                "last_run": {"type": ["string", "null"]},
                                "last_step": {"type": ["string", "null"]},
                            },
                        },
                    },
                },
                "additionalProperties": True,
            }
        },
        "actions": [],
        "limitations": ["Phase 0 has no agent commands, approvals, leases or WebSocket stream."],
        "changes": [],
    }
    if name == "compatible":
        bundle["schemas"]["state"]["properties"]["source_label"] = {"type": "string"}
        bundle["changes"] = ["Optional state.source_label added. Existing units stay the same."]
    elif incompatible:
        bundle["changes"] = [
            "stage.position unit changed from rotation to mm. No automatic mapping."
        ]
    elif name == "unsupported-dialect":
        bundle["$schema"] = "urn:unsupported-schema-dialect"
    elif name == "broken-digest":
        bundle["changes"] = ["Test a bundle changed during transport."]
    return bundle


def compatibility(bundle, generation):
    """Check common state meaning before a script consumes numeric readings."""
    problems = []
    if bundle.get("$schema") != DIALECT:
        problems.append("Unsupported schema dialect.")
    if not str(bundle.get("version", "")).startswith("1."):
        problems.append("Unsupported major contract version.")
    semantics = bundle.get("state_semantics", {})
    if semantics.get("stage.position", {}).get("unit") != "rotation":
        problems.append("stage.position must use rotation. Unit conversion is not inferred.")
    for key, expected in (("frame", "mock-axis-reference"), ("kind", "absolute")):
        if semantics.get("stage.position", {}).get(key) != expected:
            problems.append(f"Unsupported stage.position {key}.")
    if semantics.get("temperature.value", {}).get("unit") != "degC":
        problems.append("temperature.value must use degC.")
    if problems:
        return {"status": "client update required", "problems": problems}
    optional = "source_label" in bundle.get("schemas", {}).get("state", {}).get("properties", {})
    return {
        "status": "supported subset" if generation == 2 and not optional else "compatible",
        "problems": [],
        "optional_source_label": optional,
    }


def differences(old, new):
    """Return exact changed paths, including schema and meaning changes."""
    rows = []

    def visit(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                child = f"{path}.{key}" if path else key
                if key not in a or key not in b:
                    rows.append({"path": child, "before": a.get(key), "after": b.get(key)})
                else:
                    visit(a[key], b[key], child)
        elif a != b:
            rows.append({"path": path, "before": copy.deepcopy(a), "after": copy.deepcopy(b)})

    visit(old, new, "")
    return rows
