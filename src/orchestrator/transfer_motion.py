"""Encoder coordinate checks for sequential, firmware-controlled stage moves."""

import math

AXES = {"top": "x", "bottom": "y", "insert": "insert"}
MOVE_ORDER = ("bottom", "top", "insert")
SPEED = 0.25
JOG_ROTATIONS = 0.1
ACTIVE_CALIBRATIONS = {"lower", "upper", "center"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def motors(state):
    return {item["id"]: item for item in state.get("motors", [])}


def fresh(state, motor):
    return (
        state.get("connected") is True
        and motor.get("telemetry_fresh") is True
        and motor.get("encoder") == "ok"
        and motor.get("fault") in (None, "none")
        and finite(motor.get("position_rotations"))
    )


def idle(motor):
    return motor.get("mode") == "idle" and motor.get("calibration") not in ACTIVE_CALIBRATIONS


def coordinates(state):
    axes = motors(state)
    return {
        axis: axes[motor]["position_rotations"]
        if motor in axes and fresh(state, axes[motor])
        else None
        for motor, axis in AXES.items()
    }


def target_ready(state, motor, target):
    """Use board readiness and bounds; never estimate encoder distance from pulses."""
    axis = motors(state).get(motor, {})
    lower, upper = axis.get("lower_limit_rotations"), axis.get("upper_limit_rotations")
    return (
        finite(target)
        and fresh(state, axis)
        and axis.get("positioning_ready") is True
        and finite(lower)
        and finite(upper)
        and lower <= target <= upper
        and finite(axis.get("tolerance"))
        and axis["tolerance"] > 0
    )


def sequence(position, *, stage_only=False):
    order = MOVE_ORDER[:2] if stage_only else MOVE_ORDER
    if not isinstance(position, dict) or set(position) != {AXES[motor] for motor in order}:
        raise ValueError(
            "A stage position needs X and Y rotations."
            if stage_only
            else "A named position needs X, Y and Insert rotations."
        )
    if not all(finite(value) for value in position.values()):
        raise ValueError("A named position needs finite rotations.")
    return [(motor, position[AXES[motor]]) for motor in order]
