import math

import pytest

from mg400_agent.kinematics import joint_angles


@pytest.mark.parametrize(
    "source, expected_degrees",
    [
        ((0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 0, 0)),
        ((90, 30, 70, -45), (90, 30, 30, 40, -30, -70, 70, -45)),
        ((-90, -30, -70, 45), (-90, -30, -30, -40, 30, 70, -70, 45)),
    ],
)
def test_maps_robot_degrees_to_eight_coupled_model_joints(source, expected_degrees):
    expected = tuple(value * math.pi / 180 for value in expected_degrees)
    assert joint_angles(source) == pytest.approx(expected)


@pytest.mark.parametrize(
    "joints",
    [(), (1, 2, 3), (1, 2, 3, 4, 5), (float("nan"), 0, 0, 0), (0, 0, 0, float("inf"))],
)
def test_rejects_invalid_robot_joint_input(joints):
    with pytest.raises(ValueError, match="four finite joint angles"):
        joint_angles(joints)
