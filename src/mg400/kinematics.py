"""Flange position of the MG400 from J2 and J3, and the correction of the errors of the arm.

The controller gives the flange pose (User 0, Tool 0) from its joint angles as

    reach  = 109.5 + REAR_ARM sin J2 + FOREARM cos J3
    height = -53.0 + REAR_ARM cos J2 - FOREARM sin J3

with x = reach cos J1 and y = reach sin J1. The lengths are the values that this
controller uses: they give its reported pose from its reported joint angles within 0.01 mm.

The real arm differs from this model. An ArmError holds the differences. `true_pose` gives
the place that the flange really has at a reported pose, and `reported_pose` gives the pose
to command for a wanted real place. J1 and the tool rotation have no correction: an error of
the J1 zero only turns the frame of the arm.

The calibration gives no constant offset of the reach or of the height. Use the real places
for the difference between 2 poses, for example a look pose and a touch pose. Do not use
them as an absolute height.
"""

import math
from typing import NamedTuple

REAR_ARM_MM = 174.208
FOREARM_MM = 175.070
REACH_OFFSET_MM = 109.5
HEIGHT_OFFSET_MM = -53.0


class ArmError(NamedTuple):
    """Real arm minus controller model. A real joint angle is angle (1 + scale) + zero."""

    j2_zero_deg: float = 0.0
    j3_zero_deg: float = 0.0
    rear_arm_mm: float = 0.0
    forearm_mm: float = 0.0
    j2_scale: float = 0.0
    j3_scale: float = 0.0


# From `scripts/arm_calibrate.py fit`: views with J2 -16 to 15 degrees and J3 -16 to 24
# degrees. The views do not separate a short rear arm from a J2 angle scale, so the values
# are not verified for J2 above 45 degrees (poses near the table).
ERROR = ArmError(j3_zero_deg=0.88, rear_arm_mm=-2.50)


def plane_from_joints(j2_deg, j3_deg, error=None):
    """Reach and height of the flange. With an error, the real ones for reported angles."""
    error = error or ArmError()
    j2 = math.radians(j2_deg * (1.0 + error.j2_scale) + error.j2_zero_deg)
    j3 = math.radians(j3_deg * (1.0 + error.j3_scale) + error.j3_zero_deg)
    rear, fore = REAR_ARM_MM + error.rear_arm_mm, FOREARM_MM + error.forearm_mm
    reach = REACH_OFFSET_MM + rear * math.sin(j2) + fore * math.cos(j3)
    height = HEIGHT_OFFSET_MM + rear * math.cos(j2) - fore * math.sin(j3)
    return reach, height


def joints_from_plane(reach, height):
    """J2 and J3 in degrees that the controller reports at a reach and a height."""
    across, up = reach - REACH_OFFSET_MM, height - HEIGHT_OFFSET_MM
    # across^2 + up^2 = rear^2 + fore^2 + 2 rear fore sin(J2 - J3)
    sine = (across**2 + up**2 - REAR_ARM_MM**2 - FOREARM_MM**2) / (2 * REAR_ARM_MM * FOREARM_MM)
    if not -1.0 <= sine <= 1.0:
        raise ValueError(f"Reach {reach:g} mm at height {height:g} mm is outside the arm")
    bend = math.asin(sine)
    along, side = REAR_ARM_MM + FOREARM_MM * math.sin(bend), FOREARM_MM * math.cos(bend)
    j2 = math.atan2(along * across - side * up, side * across + along * up)
    return math.degrees(j2), math.degrees(j2 - bend)


def _plane_error(reach, height, error):
    real = plane_from_joints(*joints_from_plane(reach, height), error)
    return real[0] - reach, real[1] - height


def _moved(pose, reach, shift):
    x, y, z, *rest = pose
    factor = (reach + shift[0]) / reach
    return (x * factor, y * factor, z + shift[1], *rest)


def true_pose(pose, error=None):
    """Real flange place (x, y, z, ...) of a reported pose. Other values pass unchanged."""
    error = ERROR if error is None else error
    reach = math.hypot(pose[0], pose[1])
    return _moved(pose, reach, _plane_error(reach, pose[2], error))


def reported_pose(pose, error=None):
    """Pose that the controller must report so that the flange is really at `pose`."""
    error = ERROR if error is None else error
    wanted, height = math.hypot(pose[0], pose[1]), pose[2]
    reach, z = wanted, height
    for _ in range(20):
        shift = _plane_error(reach, z, error)
        miss = wanted - reach - shift[0], height - z - shift[1]
        reach, z = reach + miss[0], z + miss[1]
        if abs(miss[0]) < 1e-9 and abs(miss[1]) < 1e-9:
            break
    return _moved(pose, wanted, (reach - wanted, z - height))
