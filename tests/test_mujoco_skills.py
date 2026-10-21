import socket
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from mg400_agent.mujoco_dynamics import DynamicModel
from mg400_agent.mujoco_skills import HOME_JOINTS_DEG, MuJoCoJointExecutor

pytest.importorskip("mujoco")
np = pytest.importorskip("numpy")


@pytest.fixture
def executor(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline joint tools must not connect to hardware or the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    return MuJoCoJointExecutor()


def snapshot(model):
    return (
        model.data.qpos.copy(),
        model.data.qvel.copy(),
        model.data.ctrl.copy(),
        model.data.time,
    )


def assert_unchanged(model, before):
    for actual, expected in zip(snapshot(model), before, strict=True):
        np.testing.assert_array_equal(actual, expected)


def test_initial_state_and_gripper_reference_are_explicit(executor):
    state = executor.execute("get_robot_state", {})["state"]
    assert state["joints_deg"] == state["target_joints_deg"] == HOME_JOINTS_DEG
    assert state["home_joints_deg"] == HOME_JOINTS_DEG
    assert state["simulation_time_s"] == 0
    assert state["state_valid"] and not state["stopped"]
    assert state["gripper"]["fixed_visual_only"]
    assert not state["gripper"]["actuation_enabled"]
    assert not state["gripper"]["collision_enabled"]
    assert not state["gripper"]["payload_inertia_included"]
    assert state["tool_offset_mm"] == [0, 0, 0]
    flange = executor.model.data.site("mg400_flange").xpos
    np.testing.assert_allclose(state["flange_in_base_mm"], flange * 1000)


def test_sequential_motion_and_home_use_physics_without_reset(executor, monkeypatch):
    def forbidden(*args):
        pytest.fail("Joint tools must never reset the simulation")

    monkeypatch.setattr(executor.model, "reset", forbidden)
    first = executor.execute("move_joints", {"joints_deg": [20, 10, 15, -5]})
    assert first["ok"]
    assert first["state"]["target_joints_deg"] == (20, 10, 15, -5)
    assert first["state"]["joints_deg"] == pytest.approx((20, 10, 15, -5), abs=0.25)
    assert first["motion"]["physics_steps"] > 1
    assert first["motion"]["requested_trajectory_time_s"] == pytest.approx(20 / 15)
    first_time = executor.model.data.time
    actual = executor.model.joints_deg
    second = executor.execute("move_joint", {"joint": "J2", "angle_deg": 5, "relative": True})
    assert second["ok"]
    assert second["state"]["target_joints_deg"] == (20, actual[1] + 5, 15, -5)
    assert executor.model.data.time > first_time
    second_time = executor.model.data.time
    home = executor.execute("home", {})
    assert home["ok"]
    assert executor.model.data.time > second_time
    assert home["state"]["target_joints_deg"] == HOME_JOINTS_DEG
    assert home["state"]["joints_deg"] == pytest.approx(HOME_JOINTS_DEG, abs=0.25)
    assert home["state"]["max_joint_speed_deg_s"] <= 0.5


def test_injected_model_is_not_reset_and_absolute_move_preserves_other_targets(monkeypatch):
    model = DynamicModel()
    model.reset((10, 10, 15, -5))
    for _ in range(500):
        model.step()
    before = snapshot(model)
    executor = MuJoCoJointExecutor(model)
    assert_unchanged(model, before)
    assert executor.state()["joints_deg"] == model.joints_deg
    assert executor.state()["home_joints_deg"] == HOME_JOINTS_DEG

    from mg400_agent.mujoco_dynamics import run_dynamics

    starts = []

    def record_start(robot, frames, **kwargs):
        starts.append(frames[0].joints_deg)
        return run_dynamics(robot, frames, **kwargs)

    monkeypatch.setattr("mg400_agent.mujoco_skills.run_dynamics", record_start)
    actual = model.joints_deg
    result = executor.execute("move_joint", {"joint": "J1", "angle_deg": 5, "relative": False})
    assert result["ok"]
    assert starts == [actual]
    assert result["state"]["target_joints_deg"] == (5, 10, 15, -5)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("arbitrary_code", {}),
        ("get_robot_state", {"extra": True}),
        ("home", {"speed": 1}),
        ("stop", {"extra": False}),
        ("move_joint", {"joint": "J5", "angle_deg": 5, "relative": False}),
        ("move_joint", {"joint": 1, "angle_deg": 5, "relative": False}),
        ("move_joint", {"joint": "J1", "angle_deg": "5", "relative": False}),
        ("move_joint", {"joint": "J1", "angle_deg": True, "relative": False}),
        ("move_joint", {"joint": "J1", "angle_deg": float("nan"), "relative": False}),
        ("move_joint", {"joint": "J1", "angle_deg": float("inf"), "relative": False}),
        ("move_joint", {"joint": "J1", "angle_deg": 5, "relative": 1}),
        ("move_joint", {"joint": "J1", "angle_deg": 5}),
        ("move_joints", {"joints_deg": [0, 0, 0]}),
        ("move_joints", {"joints_deg": [0, 0, 0, 0, 0]}),
        ("move_joints", {"joints_deg": "0000"}),
        ("move_joints", {"joints_deg": [0, False, 0, 0]}),
        ("move_joints", {"joints_deg": [0, "0", 0, 0]}),
        ("move_joints", {"joints_deg": [0, 0, float("-inf"), 0]}),
        ("move_joints", {"joints_deg": [0, 0, 80, 0]}),
        ("move_joint", {"joint": "J1", "angle_deg": 160, "relative": False}),
        ("home", []),
    ],
)
def test_invalid_tools_allow_corrected_request_without_mutating_physics(executor, name, arguments):
    before = snapshot(executor.model)
    result = executor.execute(name, arguments)
    assert not result["ok"]
    assert result["recoverable"]
    assert_unchanged(executor.model, before)
    state = executor.execute("get_robot_state", {})["state"]
    assert not state["stopped"] and state["state_valid"] and state["fault"] is None
    assert executor.execute("home", {})["ok"]


def test_invalid_middle_of_segment_rejected_before_any_motion(executor):
    executor.model.reset((0, -23, 5, 0))
    before = snapshot(executor.model)
    # Both endpoints pass, but crossing J3=0 violates the conditional J2 bound.
    result = executor.execute("move_joints", {"joints_deg": [0, -17, -5, 0]})
    assert not result["ok"]
    assert result["recoverable"]
    assert "J2" in result["error"]
    assert_unchanged(executor.model, before)
    assert not executor.stopped and executor.fault is None


def test_value_error_after_physics_step_still_stops(executor, monkeypatch):
    def fail(model, *args, **kwargs):
        model.step()
        raise ValueError("Physics feedback failed")

    monkeypatch.setattr("mg400_agent.mujoco_skills.run_dynamics", fail)
    result = executor.execute("home", {})
    assert not result["ok"] and not result.get("recoverable")
    assert executor.stopped and executor.fault
    assert executor.model.data.time > 0


def test_measured_pose_outside_raw_limits_remains_a_fault(executor):
    executor.model.data.joint("mg400_j1").qpos[0] = np.deg2rad(161)
    before = snapshot(executor.model)
    result = executor.execute("home", {})
    assert not result["ok"] and not result.get("recoverable")
    assert "J1" in result["error"]
    assert executor.stopped and executor.fault
    assert_unchanged(executor.model, before)


def test_stop_freezes_existing_state_and_allows_reads(executor):
    assert executor.execute("move_joint", {"joint": "J1", "angle_deg": 5, "relative": False})["ok"]
    before = snapshot(executor.model)
    assert executor.stop() == executor.execute("stop", {}) == {"ok": True, "stopped": True}
    state = executor.execute("get_robot_state", {})["state"]
    assert state["state_valid"] and state["stopped"]
    assert_unchanged(executor.model, before)
    assert not executor.execute("home", {})["ok"]
    assert_unchanged(executor.model, before)


def test_viewer_cancellation_stops_at_partial_motion(executor, monkeypatch):
    open_window = True

    def close_window():
        nonlocal open_window
        open_window = False

    executor.viewer = SimpleNamespace(
        is_running=lambda: open_window, lock=nullcontext, sync=close_window
    )
    monkeypatch.setattr("mg400_agent.mujoco_dynamics.wait_for_viewer", lambda *args: True)
    result = executor.execute("move_joint", {"joint": "J1", "angle_deg": 20, "relative": False})
    assert not result["ok"]
    assert "viewer closed" in result["error"]
    assert executor.stopped
    assert executor.model.data.time == pytest.approx(0.002)
    assert 0 < executor.model.joints_deg[0] < 20
    before = snapshot(executor.model)
    assert not executor.execute("home", {})["ok"]
    assert_unchanged(executor.model, before)


def test_dynamics_fault_stops_and_does_not_report_completion(executor):
    executor.model.model.opt.gravity[:] = 0
    executor.model.model.opt.disableflags |= executor.model.mujoco.mjtDisableBit.mjDSBL_ACTUATION
    result = executor.execute("move_joint", {"joint": "J1", "angle_deg": 1, "relative": False})
    assert not result["ok"]
    assert "settle" in result["error"]
    assert executor.stopped
    assert not executor.state()["state_valid"]


def test_tools_have_strict_arguments_and_joint_order(executor):
    tools = {tool["name"]: tool for tool in executor.tool_schemas()}
    assert set(tools) == {"get_robot_state", "move_joint", "move_joints", "home", "stop"}
    for tool in tools.values():
        assert tool["strict"]
        parameters = tool["parameters"]
        assert not parameters["additionalProperties"]
        assert set(parameters["required"]) == set(parameters["properties"])
    assert tools["move_joint"]["parameters"]["properties"]["joint"]["enum"] == [
        "J1",
        "J2",
        "J3",
        "J4",
    ]
    joints = tools["move_joints"]["parameters"]["properties"]["joints_deg"]
    assert joints["minItems"] == joints["maxItems"] == 4
