"""Explicit, typed skills. Functions receive a revocable step context."""

import inspect
import math
from dataclasses import dataclass
from typing import get_type_hints


def typed(value, kind, label):
    valid = type(value) is kind
    if kind is float:
        valid = type(value) in {int, float}
        try:
            valid = valid and math.isfinite(value)
        except OverflowError:
            valid = False
    if not valid:
        raise ValueError(f"{label} must have type {kind.__name__}.")
    return value


@dataclass(frozen=True)
class Skill:
    function: object
    services: tuple
    signature: inspect.Signature
    types: dict

    def validate(self, params):
        try:
            bound = self.signature.bind(None, **params)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        bound.apply_defaults()
        return {
            key: typed(value, self.types[key], key)
            for key, value in list(bound.arguments.items())[1:]
        }


class Registry:
    def __init__(self):
        self._skills = {}

    def register(self, name, function, *, services=()):
        if not name or name in self._skills:
            raise ValueError("Skill names must be nonempty and unique.")
        signature = inspect.signature(function)
        parameters = list(signature.parameters.values())
        types = get_type_hints(function)
        if not parameters or parameters[0].name != "ctx":
            raise ValueError("A skill's first parameter must be ctx.")
        for parameter in parameters[1:]:
            if parameter.kind not in {
                parameter.POSITIONAL_OR_KEYWORD,
                parameter.KEYWORD_ONLY,
            } or types.get(parameter.name) not in {str, int, float, bool}:
                raise ValueError("Skill parameters need explicit scalar types.")
        self._skills[name] = Skill(function, tuple(services), signature, types)
        return function

    def __getitem__(self, name):
        try:
            return self._skills[name]
        except (KeyError, TypeError):
            raise ValueError(f"Unknown skill: {name!r}") from None


def default_registry():
    registry = Registry()

    def wait(ctx, seconds: float):
        if seconds < 0:
            raise ValueError("Wait duration must not be negative.")
        ctx.sleep(seconds)

    def fail(ctx, message: str):
        raise RuntimeError(message)

    def enable(ctx):
        return ctx.clients["robot"].run("enable")

    def move(ctx, x_mm: float, y_mm: float, z_mm: float, r_deg: float):
        return ctx.clients["robot"].run(
            "execute", targets={"x": x_mm, "y": y_mm, "z": z_mm, "r": r_deg}
        )

    def stop(ctx):
        return ctx.clients["robot"].stop()

    def sample(ctx, chip_id: str, note: str):
        ctx.store.sample(chip_id, {"note": note})

    registry.register("wait", wait)
    registry.register("fail", fail)
    registry.register("robot.enable", enable, services=("robot",))
    registry.register("robot.move", move, services=("robot",))
    registry.register("robot.stop", stop, services=("robot",))
    registry.register("sample.record", sample)
    return registry
