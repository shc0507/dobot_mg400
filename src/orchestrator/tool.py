"""Pure conversions between controller flange and measured fingertip poses."""

import math
from dataclasses import dataclass


def finite(value, label):
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{label} must be a finite number.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number.")
    return result


@dataclass(frozen=True)
class Pose:
    x_mm: float
    y_mm: float
    z_mm: float
    r_deg: float

    def __post_init__(self):
        for name in ("x_mm", "y_mm", "z_mm", "r_deg"):
            object.__setattr__(self, name, finite(getattr(self, name), name))


@dataclass(frozen=True)
class ToolOffset:
    dx_mm: float
    dy_mm: float
    dz_mm: float

    def __post_init__(self):
        for name in ("dx_mm", "dy_mm", "dz_mm"):
            object.__setattr__(self, name, finite(getattr(self, name), name))

    @classmethod
    def from_config(cls, data):
        if not isinstance(data, dict) or set(data) != {"offset_mm"}:
            raise ValueError("The tool file must contain only offset_mm = [dx, dy, dz].")
        offset = data["offset_mm"]
        if not isinstance(offset, list) or len(offset) != 3:
            raise ValueError("offset_mm must contain exactly 3 measured numbers.")
        return cls(*offset)


def _rotated(pose, offset):
    if not isinstance(pose, Pose) or not isinstance(offset, ToolOffset):
        raise ValueError("A Pose and a ToolOffset are required.")
    # Reduce only the trigonometric argument. Preserve the caller's R value.
    angle = math.radians(pose.r_deg % 360)
    cosine, sine = math.cos(angle), math.sin(angle)
    return (
        finite(offset.dx_mm * cosine - offset.dy_mm * sine, "rotated dx"),
        finite(offset.dx_mm * sine + offset.dy_mm * cosine, "rotated dy"),
    )


def flange_to_fingertip(pose, offset):
    dx, dy = _rotated(pose, offset)
    return Pose(pose.x_mm + dx, pose.y_mm + dy, pose.z_mm - offset.dz_mm, pose.r_deg)


def fingertip_to_flange(pose, offset):
    dx, dy = _rotated(pose, offset)
    return Pose(pose.x_mm - dx, pose.y_mm - dy, pose.z_mm + offset.dz_mm, pose.r_deg)
