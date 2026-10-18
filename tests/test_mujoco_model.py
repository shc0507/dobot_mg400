"""Check MuJoCo against the pinned CAD chain, independently of runtime FK.

Reference: HarvestX/MG400_ROS2 commit 1a8c7344be44c52030f7216c0f387873ed91fd11,
mg400_description/urdf/mg400.xacro and mg400_common/include/mg400_common/kinematics.hpp.
"""

import math

import pytest

from mg400_agent.mujoco_replay import KinematicModel

mujoco = pytest.importorskip("mujoco")

# All poses obey the reference's 1.05 degree margin, J3-J2 constraint, and
# stricter J2 lower bound when J3 <= 0.
POSES = [
    (0, 0, 0, 0),
    (90, 30, 70, -45),
    (-80, -15, -20, 55),
    (150, 80, 100, -140),
    (-150, 50, -5, 140),
    (120, -20, 35, 120),
]


def cad_flange_position(joints_deg):
    """Analytic reduction of the exact URDF origin translations, in metres."""
    j1, j2, j3, _ = map(math.radians, joints_deg)
    radial = (
        0.0435
        - 0.0010512570171516 * math.cos(j2)
        + 0.175001164344716 * math.sin(j2)
        + 0.175 * math.cos(j3)
        + 0.00325 * math.sin(j3)
        + 0.066
    )
    lateral = -0.035775 + 0.0357748756218898 - 0.017 + 0.017
    height = (
        0.109
        + 0.119
        + 0.0010512570171516 * math.sin(j2)
        + 0.175001164344716 * math.cos(j2)
        - 0.175 * math.sin(j3)
        + 0.00325 * math.cos(j3)
        + 0.031
        - 0.084
    )
    return (
        -0.005 + radial * math.cos(j1) - lateral * math.sin(j1),
        radial * math.sin(j1) + lateral * math.cos(j1),
        height,
    )


@pytest.fixture
def robot():
    return KinematicModel()


def test_zero_pose_has_explicit_base_origin_and_zero_tool_frames_in_metres(robot):
    robot.set_joints((0, 0, 0, 0))
    assert robot.data.site("mg400_base").xpos == pytest.approx((0, 0, 0), rel=0, abs=1e-12)
    assert robot.data.site("mg400_controller_origin").xpos == pytest.approx(
        (-0.005, 0, 0.224), rel=0, abs=1e-12
    )
    assert robot.data.site("mg400_flange").xpos == pytest.approx(
        (0.2784487429828484, -0.0000001243781102, 0.353251164344716), rel=0, abs=1e-12
    )
    assert robot.data.site("mg400_tcp").xpos == pytest.approx(
        robot.data.site("mg400_flange").xpos, rel=0, abs=1e-12
    )


@pytest.mark.parametrize("joints_deg", POSES)
def test_flange_matches_exact_cad_chain_and_stays_level(robot, joints_deg):
    robot.set_joints(joints_deg)
    assert robot.data.site("mg400_flange").xpos == pytest.approx(
        cad_flange_position(joints_deg), rel=0, abs=1e-12
    )
    yaw = math.radians(joints_deg[0] + joints_deg[3])
    cosine, sine = math.cos(yaw), math.sin(yaw)
    assert robot.data.site("mg400_flange").xmat == pytest.approx(
        (cosine, -sine, 0, sine, cosine, 0, 0, 0, 1), rel=0, abs=1e-12
    )
    assert robot.data.site("mg400_tcp").xpos == pytest.approx(
        robot.data.site("mg400_flange").xpos, rel=0, abs=1e-12
    )
    state = robot.state()
    expected = cad_flange_position(joints_deg)
    assert state["flange_in_base_mm"] == pytest.approx(
        [1000 * value for value in expected], rel=0, abs=1e-9
    )
    assert state["flange_in_urdf_origin_mm"] == pytest.approx(
        [1000 * (expected[0] + 0.005), 1000 * expected[1], 1000 * (expected[2] - 0.224)],
        rel=0,
        abs=1e-9,
    )
    assert state["flange_yaw_deg"] == pytest.approx(
        (joints_deg[0] + joints_deg[3] + 180) % 360 - 180, rel=0, abs=1e-12
    )


def test_model_has_eight_named_joints_with_four_axis_coupling(robot):
    robot.set_joints((90, 30, 70, -45))
    expected_degrees = {
        "mg400_j1": 90,
        "mg400_j2_1": 30,
        "mg400_j2_2": 30,
        "mg400_j3_1": 40,
        "mg400_j3_2": -30,
        "mg400_j4_1": -70,
        "mg400_j4_2": 70,
        "mg400_j5": -45,
    }
    assert robot.model.nq == robot.model.nv == robot.model.njnt == 8
    for name, degrees in expected_degrees.items():
        assert robot.data.joint(name).qpos[0] == pytest.approx(math.radians(degrees))


def test_repeated_pose_updates_do_not_accumulate_drift_or_advance_dynamics(robot):
    for joints_deg in POSES + list(reversed(POSES)):
        robot.set_joints(joints_deg)
        assert robot.data.site("mg400_flange").xpos == pytest.approx(
            cad_flange_position(joints_deg), rel=0, abs=1e-12
        )
    assert robot.data.time == 0


def test_cad_origin_pose_is_not_misrepresented_as_controller_fk(robot):
    robot.set_joints((0, 0, 0, 0))
    flange_from_origin = (
        robot.data.site("mg400_flange").xpos - robot.data.site("mg400_controller_origin").xpos
    )
    # The reference's ideal LINK1...LINK4 FK predicts (0.2845, 0, 0.122).
    # Preserve the CAD discrepancy explicitly until real controller calibration.
    mismatch = flange_from_origin - (0.2845, 0, 0.122)
    assert mismatch == pytest.approx(
        (-0.0010512570171516, -0.0000001243781102, 0.007251164344716), rel=0, abs=1e-12
    )
