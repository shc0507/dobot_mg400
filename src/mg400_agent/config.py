"""Explicit units and a small, simulation-only scene configuration."""

import math
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path


def finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a finite number")
    if not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return float(value)


@dataclass(frozen=True)
class Pose:
    x_mm: float
    y_mm: float
    z_mm: float
    r_deg: float

    @classmethod
    def from_values(cls, values: list) -> "Pose":
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError("Pose must be [x_mm, y_mm, z_mm, r_deg]")
        return cls(*(finite_number(value) for value in values))

    def values(self) -> tuple[float, float, float, float]:
        return self.x_mm, self.y_mm, self.z_mm, self.r_deg

    def at_height(self, height: float) -> "Pose":
        return replace(self, z_mm=height)


@dataclass(frozen=True)
class Workspace:
    minimum: Pose
    maximum: Pose

    def validate(self, pose: Pose) -> None:
        for value, lower, upper in zip(
            pose.values(), self.minimum.values(), self.maximum.values(), strict=True
        ):
            if not lower <= finite_number(value) <= upper:
                raise ValueError(f"Pose outside configured simulation workspace: {pose}")


@dataclass(frozen=True)
class Config:
    home: Pose
    travel_z_mm: float
    action_timeout_s: float
    workspace: Workspace
    objects: dict[str, Pose]
    locations: dict[str, Pose]

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with Path(path).open("rb") as file:
            data = tomllib.load(file)
        try:
            workspace = Workspace(
                Pose.from_values(data["workspace"]["minimum"]),
                Pose.from_values(data["workspace"]["maximum"]),
            )
            config = cls(
                home=Pose.from_values(data["home"]),
                travel_z_mm=finite_number(data["travel_z_mm"]),
                action_timeout_s=finite_number(data["action_timeout_s"]),
                workspace=workspace,
                objects={key: Pose.from_values(value) for key, value in data["objects"].items()},
                locations={
                    key: Pose.from_values(value) for key, value in data["locations"].items()
                },
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"Invalid scene configuration: {error}") from error
        if config.action_timeout_s <= 0 or not config.objects or not config.locations:
            raise ValueError("Timeout must be positive; objects and locations must be nonempty")
        for pose in [config.home, *config.objects.values(), *config.locations.values()]:
            workspace.validate(pose)
            workspace.validate(pose.at_height(config.travel_z_mm))
            if pose.z_mm > config.travel_z_mm:
                raise ValueError("travel_z_mm must be at or above every configured pose")
        return config
