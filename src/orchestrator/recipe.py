"""Validate the entire TOML recipe before acquiring any service."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .skills import typed

TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def positive(value, label):
    typed(value, float, label)
    if value <= 0:
        raise ValueError(f"{label} must be positive.")
    return float(value)


@dataclass(frozen=True)
class Step:
    skill: str
    params: dict
    timeout_s: float
    on_fail: str
    zone: str | None = None


@dataclass(frozen=True)
class Recipe:
    name: str
    version: int
    params: dict
    steps: tuple
    services: tuple


def validate(data, registry, overrides=None):
    if not isinstance(data, dict) or set(data) != {"name", "version", "params", "steps"}:
        raise ValueError("A recipe must define exactly name, version, params and steps.")
    if (
        not isinstance(data["name"], str)
        or not data["name"].strip()
        or data["version"] != 1
        or type(data["version"]) is not int
    ):
        raise ValueError("A recipe needs a nonempty name and version = 1.")
    schemas, overrides = data["params"], dict(overrides or {})
    if not isinstance(schemas, dict) or set(overrides) - set(schemas):
        raise ValueError("Unknown recipe parameter.")
    params = {}
    for name, spec in schemas.items():
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or not isinstance(spec, dict)
            or set(spec) - {"type", "default"}
        ):
            raise ValueError("Invalid recipe parameter definition.")
        kind = TYPES.get(spec.get("type")) if isinstance(spec.get("type"), str) else None
        if kind is None or (name not in overrides and "default" not in spec):
            raise ValueError(f"Missing parameter or invalid type: {name}")
        params[name] = typed(overrides.get(name, spec.get("default")), kind, name)
    if not isinstance(data["steps"], list) or not data["steps"]:
        raise ValueError("A recipe needs at least one step.")
    steps, services = [], set()
    for entry in data["steps"]:
        if (
            not isinstance(entry, dict)
            or set(entry) - {"skill", "params", "timeout_s", "on_fail", "zone"}
            or not {"skill", "params", "timeout_s"} <= set(entry)
        ):
            raise ValueError(
                "Each step needs skill, params and timeout_s, with optional on_fail and zone."
            )
        skill = registry[entry["skill"]]
        values = entry["params"]
        if not isinstance(values, dict):
            raise ValueError("Step params must be a table.")
        resolved = {}
        for key, value in values.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                name = value[2:-1]
                if name not in params:
                    raise ValueError(f"Unknown parameter reference: {name}")
                value = params[name]
            resolved[key] = value
        resolved = skill.validate(resolved)
        if entry["skill"] == "wait" and resolved["seconds"] < 0:
            raise ValueError("Wait duration must not be negative.")
        if "chip_id" in resolved and not resolved["chip_id"].strip():
            raise ValueError("chip_id must not be empty.")
        on_fail = entry.get("on_fail", "hold")
        if not isinstance(on_fail, str) or on_fail not in {"hold", "abort"}:
            raise ValueError("on_fail must be hold or abort.")
        zone = entry.get("zone")
        if "zone" in entry:
            from .zones import zone_name

            zone_name(zone)
        steps.append(
            Step(entry["skill"], resolved, positive(entry["timeout_s"], "timeout_s"), on_fail, zone)
        )
        services.update(skill.services)
    return Recipe(data["name"], 1, params, tuple(steps), tuple(sorted(services)))


def load_recipe(path, registry, overrides=None):
    with Path(path).open("rb") as source:
        return validate(tomllib.load(source), registry, overrides)
