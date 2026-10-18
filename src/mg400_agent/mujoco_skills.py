"""Sequential, bounded joint tools for the offline MuJoCo agent."""

import logging

from .config import finite_number
from .joint_limits import validate_joint_segment, validate_joints
from .mujoco_dynamics import DynamicModel, run_dynamics
from .mujoco_replay import Keyframe

HOME_JOINTS_DEG = (0.0, 0.0, 0.0, 0.0)
MAX_TARGET_RATE_DEG_S = 15.0
MIN_MOTION_TIME_S = 0.5
JOINTS = ("J1", "J2", "J3", "J4")
logger = logging.getLogger(__name__)


class MuJoCoJointExecutor:
    """Run tools on one persistent simulation; idle and stopped states do not step."""

    def __init__(self, model: DynamicModel | None = None, viewer=None):
        self.model = model if model is not None else DynamicModel()
        self.viewer = viewer
        self.stopped = False
        self.fault: str | None = None

    def state(self) -> dict:
        return {
            "mode": "offline_joint_dynamics",
            **self.model.state(),
            "simulation_time_s": float(self.model.data.time),
            "home_joints_deg": HOME_JOINTS_DEG,
            "stopped": self.stopped,
            "fault": self.fault,
            "state_valid": self.fault is None,
            "gripper": {
                "model": "PGEA-(2-10)(15-10) O-S visual CAD",
                "fixed_visual_only": True,
                "actuation_enabled": False,
                "collision_enabled": False,
                "payload_inertia_included": False,
                "position_reference": "Reported positions are the flange, not the gripper tip.",
            },
        }

    def stop(self) -> dict:
        # Freeze by refusing further steps; retain qpos, qvel, controls and simulation time.
        self.stopped = True
        return {"ok": True, "stopped": True}

    def _move(self, target) -> dict:
        target = validate_joints(target)
        start = self.model.joints_deg
        validate_joint_segment(start, target)
        duration = max(
            MIN_MOTION_TIME_S,
            max(abs(a - b) for a, b in zip(start, target, strict=True)) / MAX_TARGET_RATE_DEG_S,
        )
        motion = run_dynamics(
            self.model,
            [Keyframe(0, start), Keyframe(duration, target)],
            rate=30,
            viewer=self.viewer,
        )
        if not motion["completed"]:
            raise RuntimeError("Motion cancelled because the MuJoCo viewer closed")
        return motion

    def execute(self, name: str, arguments: dict) -> dict:
        expected = {
            "get_robot_state": set(),
            "move_joint": {"joint", "angle_deg", "relative"},
            "move_joints": {"joints_deg"},
            "home": set(),
            "stop": set(),
        }
        try:
            if not isinstance(name, str) or name not in expected:
                raise ValueError("Unknown tool")
            if not isinstance(arguments, dict) or set(arguments) != expected[name]:
                raise ValueError(f"Invalid arguments for {name}")
            if name == "get_robot_state":
                return {"ok": True, "state": self.state()}
            if name == "stop":
                return self.stop()
            if self.stopped:
                raise RuntimeError("Session stopped; no further motion is allowed")
            if name == "move_joint":
                joint = arguments["joint"]
                if not isinstance(joint, str) or joint not in JOINTS:
                    raise ValueError("joint must be J1, J2, J3 or J4")
                if type(arguments["relative"]) is not bool:
                    raise ValueError("relative must be a boolean")
                angle = finite_number(arguments["angle_deg"])
                index = JOINTS.index(joint)
                target = list(self.model.target_joints_deg)
                target[index] = angle + (
                    self.model.joints_deg[index] if arguments["relative"] else 0
                )
            elif name == "move_joints":
                target = arguments["joints_deg"]
                if not isinstance(target, list) or len(target) != 4:
                    raise ValueError("joints_deg must contain exactly four numbers")
            else:
                target = HOME_JOINTS_DEG
            motion = self._move(target)
            logger.info("Completed %s: measured joints_deg=%s", name, self.model.joints_deg)
            return {"ok": True, "motion": motion, "state": self.state()}
        except Exception as error:
            self.fault = str(error)
            return {"ok": False, "error": self.fault, "stop": self.stop()}

    def tool_schemas(self) -> list[dict]:
        specs = [
            (
                "get_robot_state",
                "Read actual and commanded joint angles in degrees, fixed zero home, and "
                "flange position. The attached PGEA gripper is fixed visual geometry only: "
                "no opening, closing, grasping, contacts or added payload inertia. "
                "Reported positions refer to the flange, not a calibrated gripper TCP.",
                {},
            ),
            (
                "move_joint",
                "Move one controller joint in the offline simulation. With relative=false, "
                "angle_deg is an absolute angle; with relative=true, add the signed angle "
                "to that joint's measured current position. Other joints retain their last "
                "commanded targets. The whole linear segment is checked against joint and "
                "coupled limits before motion. Target rate is at most 15 deg/s. Returns "
                "after settling; never claim exact actual angles from target angles.",
                {
                    "joint": {"type": "string", "enum": list(JOINTS)},
                    "angle_deg": {"type": "number"},
                    "relative": {"type": "boolean"},
                },
            ),
            (
                "move_joints",
                "Move all four controller joints to absolute angles in [J1,J2,J3,J4] "
                "order, in degrees. Checks the entire linear segment, including coupled "
                "limits, then moves at a target rate of at most 15 deg/s and settles. "
                "This simulation does not perform collision checking.",
                {
                    "joints_deg": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    }
                },
            ),
            (
                "home",
                "Move through physics to the initial zero target [0,0,0,0] degrees, "
                "checking the whole segment and using at most 15 deg/s target rate. "
                "Does not reset or teleport the simulation.",
                {},
            ),
            (
                "stop",
                "Freeze this simulation session and forbid further motion. State remains "
                "readable. This is not a physical robot emergency stop.",
                {},
            ),
        ]
        return [
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
            for name, description, properties in specs
        ]
