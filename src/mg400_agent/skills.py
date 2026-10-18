"""Named skills with sequential execution and explicit failure state."""

import logging
from dataclasses import asdict

from .backends import Arm, Gripper
from .config import Config, Pose

logger = logging.getLogger(__name__)


class SkillExecutor:
    def __init__(self, config: Config, arm: Arm, gripper: Gripper):
        self.config = config
        self.arm = arm
        self.gripper = gripper
        self.objects: dict[str, Pose | None] = dict(config.objects)
        self.holding: str | None = None
        self.stopped = False
        self.fault: str | None = None

    @property
    def occupied_locations(self) -> set[str]:
        return {
            name for name, pose in self.config.locations.items() if pose in self.objects.values()
        }

    def state(self) -> dict:
        return {
            "pose": asdict(self.arm.get_pose()),
            "holding": self.holding,
            "stopped": self.stopped,
            "fault": self.fault,
            "state_valid": self.fault is None,
            "objects": {
                name: asdict(pose) if pose is not None else None
                for name, pose in self.objects.items()
            },
            "locations": {name: asdict(pose) for name, pose in self.config.locations.items()},
            "occupied_locations": sorted(self.occupied_locations),
        }

    def stop(self) -> dict:
        # Do not open the gripper during a stop: it might be holding a load.
        self.stopped = True
        try:
            self.arm.stop()
        except Exception as error:
            self.fault = f"Stop could not be confirmed: {error}"
            return {"ok": False, "error": self.fault, "stopped": True}
        return {"ok": True, "stopped": True}

    def _route(self, target: Pose) -> list[Pose]:
        current = self.arm.get_pose()
        height = self.config.travel_z_mm
        route = [current.at_height(height), target.at_height(height), target]
        for pose in [current, *route]:
            self.config.workspace.validate(pose)
            if pose.z_mm > height:
                raise ValueError("Pose above configured travel height")
        # This bounds check is not IK, collision checking, or a real motion planner.
        return route

    def _move(self, route: list[Pose]) -> None:
        for pose in route:
            self.arm.move_linear(pose, self.config.action_timeout_s)

    def _pick(self, object_id: str) -> None:
        if self.holding is not None:
            raise ValueError("Gripper already holds an object")
        if object_id not in self.objects:
            raise ValueError(f"Unknown object: {object_id}")
        target = self.objects[object_id]
        if target is None:
            raise ValueError("Object position is unknown")
        route = self._route(target)
        self.gripper.release(self.config.action_timeout_s)
        self._move(route)
        if not self.gripper.grasp(self.config.action_timeout_s):
            raise RuntimeError("Grasp was not confirmed by gripper feedback")
        self.holding = object_id
        self.objects[object_id] = None
        self._move([target.at_height(self.config.travel_z_mm)])

    def _place(self, location_id: str) -> None:
        if self.holding is None:
            raise ValueError("No confirmed object in gripper")
        if location_id not in self.config.locations:
            raise ValueError(f"Unknown location: {location_id}")
        if location_id in self.occupied_locations:
            raise ValueError("Destination is already occupied")
        target = self.config.locations[location_id]
        route = self._route(target)
        self._move(route)
        self.gripper.release(self.config.action_timeout_s)
        self.objects[self.holding] = target
        self.holding = None
        self._move([target.at_height(self.config.travel_z_mm)])

    def execute(self, name: str, arguments: dict) -> dict:
        expected = {
            "get_scene": set(),
            "get_robot_state": set(),
            "pick": {"object_id"},
            "place": {"location_id"},
            "stop": set(),
        }
        try:
            if name not in expected or not isinstance(arguments, dict):
                raise ValueError("Unknown tool or invalid arguments")
            if set(arguments) != expected[name] or any(
                not isinstance(value, str) for value in arguments.values()
            ):
                raise ValueError(f"Invalid arguments for {name}")
            if name == "stop":
                return self.stop()
            if name in {"get_scene", "get_robot_state"}:
                return {"ok": True, "state": self.state()}
            if self.stopped:
                raise RuntimeError("Session stopped; no further motion is allowed")
            if name == "pick":
                self._pick(arguments["object_id"])
            else:
                self._place(arguments["location_id"])
            logger.info("Completed %s %s", name, arguments)
            return {"ok": True, "state": self.state()}
        except Exception as error:
            self.fault = str(error)
            stop_result = self.stop()
            logger.warning("Skill %s failed: %s", name, error)
            return {"ok": False, "error": str(error), "stop": stop_result}

    def tool_schemas(self) -> list[dict]:
        tools = []
        specs = [
            ("get_scene", "Read known object poses and named destinations.", {}),
            ("get_robot_state", "Read current robot and task state.", {}),
            (
                "pick",
                "Pick one known object; return only after confirmed grasp and lift.",
                {"object_id": {"type": "string", "enum": list(self.objects)}},
            ),
            (
                "place",
                "Place the held object at an unoccupied named destination.",
                {"location_id": {"type": "string", "enum": list(self.config.locations)}},
            ),
            ("stop", "Stop this session. This tool is not a hardware emergency stop.", {}),
        ]
        for name, description, properties in specs:
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties),
                        "additionalProperties": False,
                    },
                }
            )
        return tools
