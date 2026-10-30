"""Conservative project guards for MG400 controller J1-J4 angles, in degrees.

The angles use the controller QActual / GetAngle() convention without changes.
J3 is not J3-J2. This module never normalizes angles.

The manufacturer User Guide V1.7, Appendix A, lists these joint ranges:
J1 [-160, 160], J2 [-25, 85], J3 [-25, 105], J4 [-360, 360].
Source:
https://download.dobot.cc/DobotStudio-pro/20231116/Dobot%20MG400%20User%20Guide%20V1.7_20231116_en.pdf

The project retains additional conservative guards from HarvestX/MG400_ROS2:
J2 >= -19 when J3 <= 0 and J3-J2 [-60, 60].
These project guards are not manufacturer controller limits.
Target checks reduce each permitted range by 1.05 degrees at both ends.
validate_measured_joints applies no margin. These guards do not establish physical safety.
HarvestX source (Apache-2.0):
https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_common/include/mg400_common/kinematics.hpp
"""

from collections.abc import Sequence

from .config import finite_number

MARGIN_DEG = 1.05


def validate_joints(joints_deg: Sequence[float]) -> tuple[float, float, float, float]:
    """Check four finite controller angles against project guards with a 1.05-degree margin."""
    return _validate_joints(joints_deg, MARGIN_DEG)


def validate_measured_joints(joints_deg: Sequence[float]) -> tuple[float, float, float, float]:
    """Check measured controller angles against project guards without a margin."""
    return _validate_joints(joints_deg, 0.0)


def limit_distances(joints_deg: Sequence[float]):
    """Return (name, side, value, limit, distance) for each guard bound, without a margin.

    A negative distance means the value is outside that bound.
    """
    bounds = []
    for name, value, lower, upper in _limits(_four(joints_deg)):
        bounds.append((name, "lower", value, lower, value - lower))
        bounds.append((name, "upper", value, upper, upper - value))
    return tuple(bounds)


def _four(joints_deg: Sequence[float]) -> tuple[float, float, float, float]:
    if not isinstance(joints_deg, Sequence) or len(joints_deg) != 4:
        raise ValueError("Expected four joint angles in degrees")
    try:
        j1, j2, j3, j4 = (finite_number(value) for value in joints_deg)
    except OverflowError as error:
        raise ValueError("Expected a finite number") from error
    return j1, j2, j3, j4


def _limits(joints):
    j1, j2, j3, j4 = joints
    return (
        ("J1", j1, -160, 160),
        ("J2", j2, -25 if j3 > 0 else -19, 85),
        ("J3", j3, -25, 105),
        ("J4", j4, -360, 360),
        ("J3-J2", j3 - j2, -60, 60),
    )


def _validate_joints(
    joints_deg: Sequence[float], margin: float
) -> tuple[float, float, float, float]:
    joints = _four(joints_deg)
    for name, value, lower, upper in _limits(joints):
        lower += margin
        upper -= margin
        if not lower <= value <= upper:
            raise ValueError(
                f"{name}={value:g} degrees outside uncalibrated reference range "
                f"[{lower:g}, {upper:g}]"
            )
    return joints


def validate_joint_segment(start: Sequence[float], end: Sequence[float]) -> None:
    """Check a measured start, a target with margin, and the path between them.

    All constraints are linear within each side of J3=0, so the endpoints and
    the crossing suffice. Checking endpoints alone can miss the stricter J2
    lower bound on the J3<=0 side. The start and path use the raw ranges.
    A measured start outside the target range can move inward.
    """
    first = validate_measured_joints(start)
    last = validate_joints(end)
    if (first[2] < 0 < last[2]) or (last[2] < 0 < first[2]):
        fraction = -first[2] / (last[2] - first[2])
        crossing = [a + fraction * (b - a) for a, b in zip(first, last, strict=True)]
        crossing[2] = 0.0
        validate_measured_joints(crossing)
