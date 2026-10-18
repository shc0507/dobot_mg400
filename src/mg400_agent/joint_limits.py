"""Uncalibrated MG400 reference ranges for offline simulation, in degrees.

Source: HarvestX/MG400_ROS2, commit 1a8c7344be44c52030f7216c0f387873ed91fd11,
mg400_common/include/mg400_common/kinematics.hpp (Apache-2.0).
Unlike its IK constraint check, angles are never normalized here.
These ranges do not establish safe limits for a physical robot.
"""

from collections.abc import Sequence

from .config import finite_number

MARGIN_DEG = 1.05


def validate_joints(joints_deg: Sequence[float]) -> tuple[float, float, float, float]:
    """Return four finite angles within the inward-margined reference ranges."""
    return _validate_joints(joints_deg, MARGIN_DEG)


def validate_measured_joints(joints_deg: Sequence[float]) -> tuple[float, float, float, float]:
    """Check measured simulation angles against reference ranges without margin."""
    return _validate_joints(joints_deg, 0.0)


def _validate_joints(
    joints_deg: Sequence[float], margin: float
) -> tuple[float, float, float, float]:
    if not isinstance(joints_deg, Sequence) or len(joints_deg) != 4:
        raise ValueError("Expected four joint angles in degrees")
    try:
        j1, j2, j3, j4 = (finite_number(value) for value in joints_deg)
    except OverflowError as error:
        raise ValueError("Expected a finite number") from error
    limits = (
        ("J1", j1, -160, 160),
        ("J2", j2, -25 if j3 > 0 else -19, 85),
        ("J3", j3, -25, 105),
        ("J4", j4, -160, 160),
        ("J3-J2", j3 - j2, -60, 60),
    )
    for name, value, lower, upper in limits:
        lower += margin
        upper -= margin
        if not lower <= value <= upper:
            raise ValueError(
                f"{name}={value:g} degrees outside uncalibrated reference range "
                f"[{lower:g}, {upper:g}]"
            )
    return j1, j2, j3, j4


def validate_joint_segment(start: Sequence[float], end: Sequence[float]) -> None:
    """Check a complete linear joint segment, including the J3=0 branch boundary.

    All constraints are linear within each side of J3=0, so the endpoints and
    the crossing suffice. Checking endpoints alone can miss the stricter J2
    lower bound on the J3<=0 side.
    """
    first = validate_joints(start)
    last = validate_joints(end)
    if (first[2] < 0 < last[2]) or (last[2] < 0 < first[2]):
        fraction = -first[2] / (last[2] - first[2])
        crossing = [a + fraction * (b - a) for a, b in zip(first, last, strict=True)]
        crossing[2] = 0.0
        validate_joints(crossing)
