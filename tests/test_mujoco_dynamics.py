import json
import math
import socket
from contextlib import nullcontext
from importlib.resources import files
from types import SimpleNamespace

import pytest

from mg400_agent.mujoco_dynamics import DynamicModel, run_dynamics
from mg400_agent.mujoco_replay import Keyframe, load_trajectory, main

mujoco = pytest.importorskip("mujoco")
np = pytest.importorskip("numpy")


@pytest.fixture
def robot():
    return DynamicModel()


def test_actuator_target_does_not_teleport_and_physics_advances(robot):
    original_qpos, original_qvel = robot.data.qpos.copy(), robot.data.qvel.copy()
    robot.set_target((10, 5, 10, -5))
    np.testing.assert_array_equal(robot.data.qpos, original_qpos)
    np.testing.assert_array_equal(robot.data.qvel, original_qvel)
    assert robot.data.time == 0
    robot.step()
    assert robot.data.time == pytest.approx(0.002)
    assert 0 < robot.joints_deg[0] < 10
    assert robot.state()["target_joints_deg"] == (10, 5, 10, -5)
    assert robot.state()["joints_deg"] != robot.state()["target_joints_deg"]


@pytest.mark.parametrize(
    "pose",
    [
        (0, 0, 0, 0),
        (90, 30, 70, -45),
        (-80, -15, -20, 55),
        (150, 80, 100, -140),
        (-150, 50, -5, 140),
        (120, -20, 35, 120),
    ],
)
def test_holds_pose_under_gravity_with_level_flange_and_coupled_joints(robot, pose):
    robot.reset(pose)
    for _ in range(1500):
        robot.step()
    state = robot.state()
    assert robot.model.nu == robot.model.neq == 4
    assert robot.model.opt.gravity[2] == pytest.approx(-9.81)
    assert state["tracking_error_deg"] < 0.25
    assert state["max_joint_speed_deg_s"] < 0.1
    assert state["linkage_error_deg"] < 0.001
    tilt = math.acos(min(1, float(robot.data.site("mg400_flange").xmat[8])))
    assert math.degrees(tilt) < 0.001
    assert np.isfinite(robot.data.qpos).all()
    assert not robot.data.warning.number.any()


def test_passive_linkage_perturbation_is_corrected_by_solver(robot):
    robot.data.joint("mg400_j4_1").qpos[0] += 0.02
    initial_error = robot.state()["linkage_error_deg"]
    assert initial_error > 1
    for _ in range(250):
        robot.step()
    assert 0 < robot.state()["linkage_error_deg"] < 0.001
    assert robot.data.time == pytest.approx(0.5)


def test_disabling_actuators_allows_gravity_sag(robot):
    robot.model.opt.disableflags |= mujoco.mjtDisableBit.mjDSBL_ACTUATION
    for _ in range(100):
        robot.step()
    assert abs(robot.joints_deg[1]) > 1
    assert abs(robot.joints_deg[2]) > 1


def test_actual_q3_is_sum_of_shoulder_and_relative_elbow(robot):
    robot.data.joint("mg400_j2_1").qpos[0] = math.radians(20)
    robot.data.joint("mg400_j3_1").qpos[0] = math.radians(30)
    robot.mujoco.mj_forward(robot.model, robot.data)
    assert robot.state()["joints_deg"][2] == pytest.approx(50)
    assert robot.state()["target_joints_deg"][2] == 0


def test_invalid_target_does_not_change_control_or_physical_state(robot):
    before = robot.data.qpos.copy(), robot.data.qvel.copy(), robot.data.ctrl.copy()
    with pytest.raises(ValueError, match="J3-J2"):
        robot.set_target((0, 0, 80, 0))
    for actual, expected in zip((robot.data.qpos, robot.data.qvel, robot.data.ctrl), before):
        np.testing.assert_array_equal(actual, expected)
    assert robot.target_joints_deg == (0, 0, 0, 0)


def test_entire_trajectory_is_validated_before_model_or_viewer(tmp_path, monkeypatch, capsys):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            [
                {"time_s": 0, "joints_deg": [0, -23, 5, 0]},
                {"time_s": 1, "joints_deg": [0, -17, -5, 0]},
            ]
        )
    )

    def forbidden(*args):
        pytest.fail("Invalid path must be rejected before initializing a model or opening a viewer")

    monkeypatch.setattr("mg400_agent.mujoco_dynamics.DynamicModel", forbidden)
    monkeypatch.setattr("mg400_agent.mujoco_replay.launch_macos_viewer", forbidden)
    assert main(["--dynamics", "--viewer", "--trajectory", str(path)]) == 1
    assert "J2" in capsys.readouterr().err


def test_dynamic_demo_uses_physics_without_network_and_reports_measured_error(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline dynamics must not connect to hardware")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert main(["--dynamics"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline_joint_dynamics"
    assert result["completed"] is True
    assert result["physics_steps"] > 4000
    assert 8 < result["simulation_time_s"] < 10
    assert 0.01 < result["tracking_error_deg"] < 0.25
    assert result["peak_tracking_error_deg"] < 1
    assert result["peak_linkage_error_deg"] < 0.1


def test_display_rate_does_not_change_dynamics():
    frames = load_trajectory(
        str(files("mg400_agent").joinpath("assets/mg400/demo_trajectory.json"))
    )
    slow = run_dynamics(DynamicModel(), frames, 10)
    fast = run_dynamics(DynamicModel(), frames, 60)
    assert slow == fast


def test_too_fast_trajectory_fails_instead_of_claiming_completion(robot):
    frames = [Keyframe(0, (0, 0, 0, 0)), Keyframe(0.002, (90, 30, 70, 0))]
    with pytest.raises(RuntimeError, match="tracking error"):
        run_dynamics(robot, frames, 30)


def test_nonintegral_duration_rounds_physics_up_by_less_than_one_step(robot):
    frames = [Keyframe(0, (0, 0, 0, 0)), Keyframe(0.0705, (0, 0, 0, 0))]
    result = run_dynamics(robot, frames, 30)
    assert result["completed"]
    assert result["requested_trajectory_time_s"] == 0.0705
    assert result["simulation_time_s"] == pytest.approx(result["physics_steps"] * 0.002)


def test_stalled_actuators_reach_settling_timeout(robot):
    robot.model.opt.disableflags |= mujoco.mjtDisableBit.mjDSBL_ACTUATION
    robot.model.opt.gravity[:] = 0
    frames = [Keyframe(0, (0, 0, 0, 0)), Keyframe(1, (1, 0, 0, 0))]
    with pytest.raises(TimeoutError, match="settle"):
        run_dynamics(robot, frames, 30)


def test_bad_physics_warning_is_not_reported_as_success(robot, monkeypatch):
    def broken_step(model, data):
        data.warning[mujoco.mjtWarning.mjWARN_BADQACC].number += 1

    monkeypatch.setattr(robot.mujoco, "mj_step", broken_step)
    with pytest.raises(RuntimeError, match="numerical warning"):
        robot.step()


def test_measured_range_violation_aborts_even_when_target_is_valid(robot):
    robot.data.joint("mg400_j1").qpos[0] = math.radians(170)
    with pytest.raises(ValueError, match="J1.*reference range"):
        robot.step()
    assert robot.target_joints_deg == (0, 0, 0, 0)


def test_closing_viewer_stops_physics_and_does_not_report_completion(robot, monkeypatch):
    open_window = True

    def close_window():
        nonlocal open_window
        open_window = False

    viewer = SimpleNamespace(is_running=lambda: open_window, lock=nullcontext, sync=close_window)
    monkeypatch.setattr("mg400_agent.mujoco_dynamics.wait_for_viewer", lambda *args: True)
    result = run_dynamics(robot, [Keyframe(0, (0, 0, 0, 0)), Keyframe(1, (5, 5, 5, 5))], 30, viewer)
    assert result["completed"] is False
    assert result["physics_steps"] == 1
    assert result["simulation_time_s"] == pytest.approx(0.002)
