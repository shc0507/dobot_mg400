import math

import pytest

from mg400 import kinematics
from mg400.kinematics import ArmError

# (J1 to J4 in degrees, flange pose) as this controller reported them.
READOUTS = (
    ((-101.3661, -21.7226, 21.3844, 76.1517), (-41.0, -203.96, 45.0, -25.2)),
    ((-87.3315, -13.8505, -15.7963, 62.1249), (11.0, -236.0, 163.8, -25.2)),
    ((-106.2933, -11.7202, 32.582, 77.0773), (-62.18, -212.73, 23.3, -29.2)),
    ((-87.0912, 1.8393, 33.4959, 63.2785), (13.25, -260.75, 24.5, -23.8)),
    ((-106.9654, 10.0896, 33.0643, 81.752), (-83.67, -274.26, 23.0, -25.2)),
    ((-101.7594, 15.7322, 31.579, 76.5417), (-62.34, -299.46, 23.0, -25.2)),
)
# J2, J3 and the reported flange Z at 2 low poses near the table.
LOW_READOUTS = (((42.7604, 53.1086), -65.1139), ((80.1792, 66.1914), -183.458))
ERROR = ArmError(0.3, -0.5, 0.8, -0.4, 0.004, -0.003)


@pytest.mark.parametrize(("joints", "pose"), READOUTS)
def test_the_model_gives_the_pose_that_the_controller_reports(joints, pose):
    reach, height = kinematics.plane_from_joints(joints[1], joints[2])

    assert reach == pytest.approx(math.hypot(pose[0], pose[1]), abs=0.02)
    assert height == pytest.approx(pose[2], abs=0.02)
    assert kinematics.joints_from_plane(reach, height) == pytest.approx(joints[1:3], abs=1e-9)


@pytest.mark.parametrize(("joints", "height"), LOW_READOUTS)
def test_the_model_gives_the_height_near_the_table(joints, height):
    assert kinematics.plane_from_joints(*joints)[1] == pytest.approx(height, abs=0.02)


def test_the_stored_error_is_applied_by_default(monkeypatch):
    pose = (-39.0, -236.0, 163.8, -25.2)

    assert kinematics.true_pose(pose) == pytest.approx(kinematics.true_pose(pose, kinematics.ERROR))
    monkeypatch.setattr(kinematics, "ERROR", ERROR)
    assert kinematics.true_pose(pose) == pytest.approx(kinematics.true_pose(pose, ERROR))
    assert kinematics.reported_pose(pose) == pytest.approx(kinematics.reported_pose(pose, ERROR))


def test_no_error_leaves_a_pose_unchanged():
    pose = (250.0, 80.0, -40.0, 12.0)

    assert kinematics.true_pose(pose, ArmError()) == pytest.approx(pose)
    assert kinematics.reported_pose(pose, ArmError()) == pytest.approx(pose)


def test_a_joint_zero_error_moves_the_flange_about_the_joint():
    # J2 = 0 and J3 = 0: the rear arm is upright and the forearm is level.
    reach, height = kinematics.plane_from_joints(0.0, 0.0)
    step = math.radians(0.1)

    x, y, z, r = kinematics.true_pose((reach, 0.0, height, 5.0), ArmError(j2_zero_deg=0.1))
    assert (x - reach, z - height) == pytest.approx((174.208 * step, 0.0), abs=1e-3)
    assert (y, r) == (0.0, 5.0)

    x, _, z, _ = kinematics.true_pose((reach, 0.0, height, 5.0), ArmError(j3_zero_deg=0.1))
    assert (x - reach, z - height) == pytest.approx((0.0, -175.070 * step), abs=1e-3)


def test_a_link_length_error_moves_the_flange_along_the_link():
    reach, height = kinematics.plane_from_joints(0.0, 0.0)

    x, _, z, _ = kinematics.true_pose((0.0, reach, height, 0.0), ArmError(rear_arm_mm=1.0))[:4]
    assert (x, z - height) == pytest.approx((0.0, 1.0))
    _, y, z, _ = kinematics.true_pose((0.0, reach, height, 0.0), ArmError(forearm_mm=1.0))
    assert (y - reach, z - height) == pytest.approx((1.0, 0.0))


def test_the_error_keeps_the_direction_from_the_base():
    pose = (-120.0, 260.0, -60.0, 30.0)

    real = kinematics.true_pose(pose, ERROR)

    assert math.atan2(real[1], real[0]) == pytest.approx(math.atan2(pose[1], pose[0]))
    assert math.hypot(real[0] - pose[0], real[1] - pose[1]) > 0.5


@pytest.mark.parametrize(
    "pose",
    [(-39.0, -236.0, 163.8, -25.2), (300.0, 0.0, -70.0, 0.0), (-150.0, 290.0, -85.0, 190.0)],
)
def test_the_reported_pose_of_a_real_place_maps_back_to_it(pose):
    command = kinematics.reported_pose(pose, ERROR)

    assert kinematics.true_pose(command, ERROR) == pytest.approx(pose, abs=1e-6)
    assert command[3] == pose[3]


def test_a_place_outside_the_arm_is_refused():
    with pytest.raises(ValueError, match="outside the arm"):
        kinematics.joints_from_plane(700.0, 0.0)


def test_the_stored_error_moves_no_pose_by_more_than_some_millimetres():
    for reach in (210.0, 280.0, 350.0):
        for z in (160.0, 20.0, -70.0, -180.0):
            try:
                kinematics.joints_from_plane(reach, z)
            except ValueError:
                continue
            real = kinematics.true_pose((reach, 0.0, z, 0.0))
            assert math.dist(real[:3], (reach, 0.0, z)) < 6.0
