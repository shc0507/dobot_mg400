import pytest

from mg400_agent.joint_limits import (
    limit_distances,
    validate_joint_segment,
    validate_joints,
    validate_measured_joints,
)


@pytest.mark.parametrize(
    "joints",
    [
        (0, 0, 0, 0),
        (-158.95, 0, 0, 358.95),
        (158.95, 0, 0, -358.95),
        (0, -23.95, 1, 0),
        (0, -17.95, 0, 0),
        (0, 83.95, 60, 0),
        (0, 0, -23.95, 0),
        (0, 60, 103.95, 0),
        (0, 0, 58.95, 0),
        (0, 58.95, 0, 0),
    ],
)
def test_accepts_inclusive_reference_boundaries(joints):
    result = validate_joints(joints)
    assert result == joints
    assert isinstance(result, tuple)
    assert all(isinstance(value, float) for value in result)


@pytest.mark.parametrize(
    "joints, joint",
    [
        ((-158.950001, 0, 0, 0), "J1"),
        ((158.950001, 0, 0, 0), "J1"),
        ((0, -23.950001, 1, 0), "J2"),
        ((0, -17.950001, 0, 0), "J2"),
        ((0, -17.950001, -1, 0), "J2"),
        ((0, 83.950001, 60, 0), "J2"),
        ((0, 0, -23.950001, 0), "J3"),
        ((0, 60, 103.950001, 0), "J3"),
        ((0, 0, 0, -358.950001), "J4"),
        ((0, 0, 0, 358.950001), "J4"),
        ((0, 0, 58.950001, 0), "J3-J2"),
        ((0, 58.950001, 0, 0), "J3-J2"),
    ],
)
def test_rejects_values_outside_inward_margin(joints, joint):
    with pytest.raises(ValueError, match=f"{joint}=.*uncalibrated reference range"):
        validate_joints(joints)


@pytest.mark.parametrize("axis", range(4))
@pytest.mark.parametrize("turn", [-720, -360, 360, 720])
def test_never_normalizes_extra_turn_commands(axis, turn):
    joints = [0, 0, 0, 0]
    joints[axis] = turn
    with pytest.raises(ValueError, match="uncalibrated reference range"):
        validate_joints(joints)


@pytest.mark.parametrize(
    "validate", [validate_joints, validate_measured_joints], ids=["target", "measured"]
)
@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -float("inf"), "0", None, 10**400]
)
def test_rejects_nonfinite_or_nonnumeric_angles(validate, value):
    with pytest.raises(ValueError, match="finite number"):
        validate([0, value, 0, 0])


@pytest.mark.parametrize("joints", [None, 0, [], [0, 0, 0], [0] * 5])
def test_requires_four_angles(joints):
    with pytest.raises(ValueError, match="four joint angles"):
        validate_joints(joints)


@pytest.mark.parametrize("reverse", [False, True])
def test_valid_endpoints_can_cross_invalid_conditional_range(reverse):
    start, end = (0, -23, 5, 0), (0, -17, -5, 0)
    if reverse:
        start, end = end, start
    validate_joints(start)
    validate_joints(end)
    with pytest.raises(ValueError, match="J2=-20 degrees outside uncalibrated reference range"):
        validate_joint_segment(start, end)


@pytest.mark.parametrize(
    "start, end",
    [
        ((0, -23, 5, 0), (0, -18, 1, 0)),
        ((0, -17, -5, 0), (0, 20, -10, 0)),
        ((0, -18, 5, 0), (0, -16, -5, 0)),
        ((0, -17.95, 5, 0), (0, -17.95, -5, 0)),
        ((0, -17.95, 0, 0), (0, -23, 5, 0)),
        ((0, 0, 0, 0), (0, 0, 0, 0)),
        ((0, 14, 9, -200), (0, 14, 9, -201)),
        ((0, 14, 9, 200), (0, 14, 9, 201)),
    ],
)
def test_accepts_complete_valid_segments_in_either_direction(start, end):
    assert validate_joint_segment(start, end) is None
    assert validate_joint_segment(end, start) is None


@pytest.mark.parametrize(
    "joints",
    [
        (-160, 0, 0, 360),
        (160, 0, 0, -360),
        (0, -25, 1, 0),
        (0, -19, 0, 0),
        (0, 85, 60, 0),
        (0, 0, -25, 0),
        (0, 60, 105, 0),
        (0, 0, 60, 0),
        (0, 60, 0, 0),
    ],
)
def test_measured_angles_may_reach_raw_reference_boundaries(joints):
    assert validate_measured_joints(joints) == joints
    with pytest.raises(ValueError, match="uncalibrated reference range"):
        validate_joints(joints)


@pytest.mark.parametrize(
    "joints, joint",
    [
        ((-160.000001, 0, 0, 0), "J1"),
        ((160.000001, 0, 0, 0), "J1"),
        ((0, -25.000001, 1, 0), "J2"),
        ((0, -19.000001, 0, 0), "J2"),
        ((0, -19.000001, -1, 0), "J2"),
        ((0, 85.000001, 60, 0), "J2"),
        ((0, 0, -25.000001, 0), "J3"),
        ((0, 60, 105.000001, 0), "J3"),
        ((0, 0, 0, -360.000001), "J4"),
        ((0, 0, 0, 360.000001), "J4"),
        ((0, 0, 60.000001, 0), "J3-J2"),
        ((0, 60.000001, 0, 0), "J3-J2"),
    ],
)
def test_measured_angles_reject_raw_range_violations(joints, joint):
    with pytest.raises(ValueError, match=f"{joint}=.*uncalibrated reference range"):
        validate_measured_joints(joints)


@pytest.mark.parametrize("j4", [-358.95, -227.81, -200.71, 200.71, 227.81, 358.95])
def test_j4_uses_manufacturer_range_without_normalizing_angles(j4):
    joints = (0, 14, 9, j4)
    assert validate_joints(joints) == joints
    assert validate_measured_joints(joints) == joints


@pytest.mark.parametrize(
    "start, target",
    [
        ((0, 14, 9, 359), (0, 14, 9, 358)),
        ((0, 14, 9, -360), (0, 14, 9, -358)),
        ((159, 14, 9, 0), (158, 14, 9, 0)),
        ((-160, 14, 9, 0), (-158, 14, 9, 0)),
        ((0, -19, 0, 0), (0, -17, 0, 0)),
        ((0, 0, 59.5, 0), (0, 0, 58, 0)),
    ],
)
def test_measured_start_may_move_out_of_target_margin(start, target):
    validate_measured_joints(start)
    with pytest.raises(ValueError):
        validate_joints(start)
    assert validate_joint_segment(start, target) is None
    with pytest.raises(ValueError):
        validate_joint_segment(target, start)


@pytest.mark.parametrize(
    "start",
    [
        (0, 14, 9, 360.01),
        (-160.01, 14, 9, 0),
        (0, -19.01, 0, 0),
        (0, 0, 60.01, 0),
    ],
)
def test_segment_still_rejects_a_start_outside_raw_ranges(start):
    with pytest.raises(ValueError):
        validate_joint_segment(start, (0, 0, 0, 0))


def test_segment_crossing_uses_raw_conditional_range():
    # The crossing is J2=-19, J3=0. Both commanded endpoints retain their margins.
    assert validate_joint_segment((0, -21, 5, 0), (0, -17, -5, 0)) is None


def test_limit_distances_report_each_raw_bound():
    distances = {
        (name, side): (limit, distance)
        for name, side, _, limit, distance in limit_distances((157, 10, -22, 0))
    }
    assert distances[("J1", "upper")] == (160, 3)
    assert distances[("J1", "lower")] == (-160, 317)
    # J3 <= 0 uses the stricter J2 lower bound.
    assert distances[("J2", "lower")] == (-19, 29)
    assert distances[("J3", "lower")] == (-25, 3)
    assert distances[("J3-J2", "lower")] == (-60, 28)
    assert distances[("J3-J2", "upper")] == (60, 92)
    assert len(distances) == 10
    with pytest.raises(ValueError):
        limit_distances((0, 0, float("nan"), 0))
