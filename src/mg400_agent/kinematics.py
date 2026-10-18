"""Map four controller angles to the eight model joints."""

import math

JOINT_NAMES = ("j1", "j2_1", "j2_2", "j3_1", "j3_2", "j4_1", "j4_2", "j5")


def joint_angles(joints_deg: tuple[float, float, float, float]) -> tuple[float, ...]:
    if len(joints_deg) != 4 or not all(math.isfinite(q) for q in joints_deg):
        raise ValueError("Expected four finite joint angles in degrees")
    q1, q2, q3, q4 = map(math.radians, joints_deg)
    return q1, q2, q2, q3 - q2, -q2, -q3, q3, q4
