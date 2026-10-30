"""Explicit measured geometry. Synthetic values are used only by demo mode."""

import math
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .protocol import MICROSTEP_MODES, MOTOR_IDS

DEFAULT_CONFIG = Path("/var/lib/mg400/stage.toml")
MAX_PULSES = 2**31 - 1
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Stage dimensions must be finite numbers.")
    return float(value)


def names(value):
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not NAME.fullmatch(name) for name in value
    ):
        raise ValueError("Stage names must contain 1..64 letters, digits, underscores or hyphens.")
    return value


@dataclass(frozen=True)
class Axis:
    steps_per_mm: float
    microstep: int
    min_mm: float
    max_mm: float
    positions: dict[str, float]

    def scale(self, microstep):
        if microstep not in MICROSTEP_MODES:
            raise ValueError("Stage microstep is unknown.")
        scale = self.steps_per_mm * microstep / self.microstep
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("Stage pulse scale is outside the supported range.")
        return scale

    def bounds(self, microstep):
        scale = self.scale(microstep)
        low, high = self.min_mm * scale, self.max_mm * scale
        if not all(math.isfinite(value) and abs(value) <= MAX_PULSES for value in (low, high)):
            raise ValueError("Stage soft limits exceed the firmware pulse range.")
        bounds = math.ceil(low), math.floor(high)
        if bounds[0] > bounds[1]:
            raise ValueError("Stage soft limits contain no representable pulse position.")
        return bounds

    def target(self, mm, microstep):
        mm = number(mm)
        if not self.min_mm <= mm <= self.max_mm:
            raise ValueError("Stage target is outside the configured soft limits.")
        low, high = self.bounds(microstep)
        target = round(mm * self.scale(microstep))
        if not low <= target <= high:
            raise ValueError("Rounded stage target is outside the configured soft limits.")
        return target


@dataclass(frozen=True)
class Geometry:
    motors: dict[str, Axis]
    poses: dict[str, tuple[tuple[str, float], ...]]


def parse_geometry(data):
    if not isinstance(data, dict) or set(data) - {"motors", "poses"}:
        raise ValueError("Stage configuration accepts only motors and poses tables.")
    motors = data.get("motors")
    if not isinstance(motors, dict) or set(motors) != set(MOTOR_IDS):
        raise ValueError("Configure bottom, top and insert stage motors.")
    axes = {}
    required = {"steps_per_mm", "microstep", "min_mm", "max_mm"}
    for motor, values in motors.items():
        if not isinstance(values, dict) or not required <= set(values) <= required | {"positions"}:
            raise ValueError(f"Invalid stage configuration fields for {motor}.")
        scale, low, high = (number(values[key]) for key in ("steps_per_mm", "min_mm", "max_mm"))
        microstep = values["microstep"]
        if (
            type(microstep) is not int
            or microstep not in MICROSTEP_MODES
            or scale <= 0
            or low > high
        ):
            raise ValueError(f"Invalid stage calibration or limits for {motor}.")
        positions = {name: number(mm) for name, mm in names(values.get("positions", {})).items()}
        if motor == "insert" and not {"load", "furnace"} <= set(positions):
            raise ValueError("Insert requires load and furnace named positions.")
        axis = Axis(scale, microstep, low, high, positions)
        for mode in MICROSTEP_MODES:
            axis.bounds(mode)
            for mm in positions.values():
                axis.target(mm, mode)
        axes[motor] = axis
    poses = {}
    for name, values in names(data.get("poses", {})).items():
        if not isinstance(values, dict) or set(values) != {"order", "positions"}:
            raise ValueError(f"Pose {name} requires order and positions.")
        order, positions = values["order"], values["positions"]
        if (
            not isinstance(order, list)
            or len(order) != 3
            or any(type(motor) is not str for motor in order)
            or set(order) != set(MOTOR_IDS)
            or not isinstance(positions, dict)
            or set(positions) != set(MOTOR_IDS)
        ):
            raise ValueError(f"Pose {name} must name every axis exactly once.")
        for motor in order:
            for mode in MICROSTEP_MODES:
                axes[motor].target(positions[motor], mode)
        poses[name] = tuple((motor, number(positions[motor])) for motor in order)
    return Geometry(axes, poses)


def load_geometry(path):
    path = Path(path).absolute()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            if part in (".", ".."):
                raise ValueError("Stage configuration must use a normalized path.")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError as exc:
        raise ValueError("Cannot read stage configuration without following symlinks.") from exc
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (1000, os.geteuid())
            or stat.S_IMODE(info.st_mode) not in (0o600, 0o640)
        ):
            raise ValueError(
                "Stage configuration must be an owned regular file with mode 0600 or 0640."
            )
        data = stream.read(1_048_577)
        if len(data) > 1_048_576:
            raise ValueError("Stage configuration exceeds 1 MiB.")
    return parse_geometry(tomllib.loads(data.decode("utf-8")))


def demo_geometry():
    """Never use these synthetic dimensions for a real transport."""
    return parse_geometry(
        {
            "motors": {
                motor: dict(
                    steps_per_mm=10,
                    microstep=1,
                    min_mm=-100,
                    max_mm=100,
                    positions={"load": 0, "furnace": 20} if motor == "insert" else {},
                )
                for motor in MOTOR_IDS
            },
            "poses": {
                "furnace": dict(order=list(MOTOR_IDS), positions=dict(bottom=0, top=0, insert=20))
            },
        }
    )
