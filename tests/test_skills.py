from dataclasses import replace
from pathlib import Path

import pytest

from mg400_agent.backends import SimArm, SimGripper
from mg400_agent.config import Config, Pose
from mg400_agent.skills import SkillExecutor

SCENE = Path(__file__).resolve().parents[1] / "config" / "demo.toml"


@pytest.fixture
def config():
    return Config.load(SCENE)


@pytest.fixture
def executor(config):
    return SkillExecutor(config, SimArm(config.home), SimGripper())


def test_pick_and_place_updates_grip_scene_and_actual_pose(executor, config):
    assert executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert executor.holding == "red_block"
    assert executor.gripper.holding
    assert executor.objects["red_block"] is None
    assert executor.execute("place", {"location_id": "box_a"})["ok"]
    assert executor.holding is None
    assert not executor.gripper.holding
    assert executor.objects["red_block"] == config.locations["box_a"]
    assert executor.arm.get_pose() == config.locations["box_a"].at_height(config.travel_z_mm)
    assert executor.occupied_locations == {"box_a"}
    # The first move after picking is a lift, before any lateral travel.
    assert executor.arm.history[3] == config.objects["red_block"].at_height(config.travel_z_mm)
    assert executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert not executor.occupied_locations


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("arbitrary_code", {}),
        ("pick", {"object_id": "missing"}),
        ("pick", {"object_id": "red_block", "speed": 100}),
        ("pick", {"object_id": 123}),
        ("pick", {}),
        ("get_scene", {"ignored": "value"}),
        ("place", {"location_id": "box_a"}),
    ],
)
def test_rejected_actions_do_not_move_and_latch_stop(executor, name, arguments):
    result = executor.execute(name, arguments)
    assert not result["ok"]
    assert executor.arm.history == []
    assert executor.arm.stopped and executor.stopped
    assert not executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert executor.execute("get_robot_state", {})["ok"]


def test_duplicate_pick_does_not_release_existing_object(executor):
    executor.execute("pick", {"object_id": "red_block"})
    history = list(executor.arm.history)
    assert not executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert executor.gripper.holding
    assert executor.arm.history == history


def test_failed_grasp_does_not_lift_or_report_holding(config):
    class MissedGripper(SimGripper):
        def grasp(self, timeout_s):
            return False

    arm = SimArm(config.home)
    executor = SkillExecutor(config, arm, MissedGripper())
    result = executor.execute("pick", {"object_id": "red_block"})
    assert not result["ok"]
    assert "not confirmed" in result["error"]
    assert len(arm.history) == 3
    assert executor.holding is None
    assert not executor.state()["state_valid"]
    assert arm.stopped


def test_motion_timeout_stops_without_closing_gripper(config):
    class TimedOutArm(SimArm):
        def move_linear(self, target, timeout_s):
            raise TimeoutError("No arrival feedback")

    arm, gripper = TimedOutArm(config.home), SimGripper()
    executor = SkillExecutor(config, arm, gripper)
    assert not executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert arm.stopped
    assert not gripper.holding
    assert not executor.state()["state_valid"]


def test_release_failure_does_not_claim_successful_place(config):
    class FailedRelease(SimGripper):
        def release(self, timeout_s):
            if self.holding:
                raise TimeoutError("Release not confirmed")

    executor = SkillExecutor(config, SimArm(config.home), FailedRelease())
    assert executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert not executor.execute("place", {"location_id": "box_a"})["ok"]
    assert executor.holding == "red_block"
    assert executor.objects["red_block"] is None
    assert not executor.state()["state_valid"]


def test_out_of_bounds_route_rejected_before_any_movement(executor):
    executor.objects["red_block"] = Pose(999, 0, 20, 0)
    assert not executor.execute("pick", {"object_id": "red_block"})["ok"]
    assert not executor.arm.history


def test_initially_occupied_destination_is_not_overwritten(config):
    config = replace(config, objects={**config.objects, "other": config.locations["box_a"]})
    executor = SkillExecutor(config, SimArm(config.home), SimGripper())
    assert executor.execute("pick", {"object_id": "red_block"})["ok"]
    history = list(executor.arm.history)
    assert not executor.execute("place", {"location_id": "box_a"})["ok"]
    assert executor.arm.history == history
    assert executor.gripper.holding


def test_stop_keeps_gripper_closed(executor):
    executor.execute("pick", {"object_id": "red_block"})
    assert executor.execute("stop", {})["ok"]
    assert executor.gripper.holding
    assert not executor.execute("place", {"location_id": "box_a"})["ok"]


@pytest.mark.parametrize(
    "values",
    [
        [float("nan"), 0, 0, 0],
        [0, float("inf"), 0, 0],
        [True, 0, 0, 0],
        [0, 0, 0],
    ],
)
def test_pose_rejects_nonfinite_or_malformed_values(values):
    with pytest.raises(ValueError):
        Pose.from_values(values)


def test_config_rejects_travel_height_outside_workspace(tmp_path):
    path = tmp_path / "scene.toml"
    path.write_text(SCENE.read_text().replace("travel_z_mm = 100", "travel_z_mm = 999"))
    with pytest.raises(ValueError, match="outside"):
        Config.load(path)
