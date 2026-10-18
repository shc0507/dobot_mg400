"""Four-axis MG400 actuator tracking with soft linkage constraints; offline only."""

import math
import time
from contextlib import nullcontext

from .joint_limits import validate_joint_segment, validate_joints, validate_measured_joints
from .kinematics import JOINT_NAMES, joint_angles
from .mujoco_replay import Keyframe, flange_state, load_model, trajectory_samples, wait_for_viewer

# Acceptance values for this uncalibrated simulation controller, not hardware limits.
ARRIVAL_ERROR_DEG = 0.25
ARRIVAL_SPEED_DEG_S = 0.5
LINKAGE_ERROR_DEG = 0.1
MAX_TRACKING_ERROR_DEG = 5.0
SETTLE_TIMEOUT_S = 2.0
SETTLE_HOLD_S = 0.1


def validate_trajectory(frames: list[Keyframe]) -> None:
    for start, end in zip(frames, frames[1:]):
        validate_joint_segment(start.joints_deg, end.joints_deg)


class DynamicModel:
    def __init__(self):
        self.mujoco, self.model, self.data = load_model("dynamics.xml")
        self._qpos = [int(self.model.joint("mg400_" + name).qposadr[0]) for name in JOINT_NAMES]
        self._actuators = [self.model.actuator(f"mg400_q{i}_position").id for i in range(1, 5)]
        self.reset((0, 0, 0, 0))

    def reset(self, joints_deg) -> None:
        """Initialize a new simulated state; normal motion never writes qpos/qvel."""
        target = validate_joints(joints_deg)
        self.mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[self._qpos] = joint_angles(target)
        self.set_target(target)
        self.mujoco.mj_forward(self.model, self.data)

    @property
    def joints_deg(self) -> tuple[float, float, float, float]:
        q = self.data.qpos[self._qpos]
        return tuple(math.degrees(v) for v in (q[0], q[1], q[1] + q[3], q[7]))

    def set_target(self, joints_deg) -> None:
        target = validate_joints(joints_deg)
        self.data.ctrl[self._actuators] = tuple(map(math.radians, target))
        self.target_joints_deg = target

    def step(self) -> None:
        self.mujoco.mj_step(self.model, self.data)
        # Derived site/actuator fields from mj_step otherwise precede integrated qpos.
        self.mujoco.mj_forward(self.model, self.data)
        if not all(math.isfinite(v) for v in (*self.data.qpos, *self.data.qvel)) or any(
            self.data.warning.number
        ):
            raise RuntimeError("MuJoCo dynamics produced a numerical warning or invalid state")
        validate_measured_joints(self.joints_deg)

    def state(self) -> dict:
        actual = self.joints_deg
        expected_linkage = joint_angles(actual)
        return {
            "joints_deg": actual,
            "target_joints_deg": self.target_joints_deg,
            "tracking_error_deg": max(
                abs(a - b) for a, b in zip(actual, self.target_joints_deg, strict=True)
            ),
            "max_joint_speed_deg_s": max(
                abs(math.degrees(v)) for v in self.data.actuator_velocity[self._actuators]
            ),
            "linkage_error_deg": max(
                abs(math.degrees(a - b))
                for a, b in zip(self.data.qpos[self._qpos], expected_linkage, strict=True)
            ),
            **flange_state(self.data),
        }


def run_dynamics(model: DynamicModel, frames: list[Keyframe], rate: float, viewer=None) -> dict:
    """Run a parsed trajectory after reset(first_pose); display rate affects only rendering."""
    validate_trajectory(frames)
    dt = float(model.model.opt.timestep)
    start_time = float(model.data.time)
    wall_start = time.monotonic()
    next_display = 0.0
    steps = 0
    peak_tracking = peak_linkage = 0.0

    def advance(target):
        nonlocal next_display, steps, peak_tracking, peak_linkage
        if viewer is not None and not viewer.is_running():
            return False
        with viewer.lock() if viewer is not None else nullcontext():
            model.set_target(target)
            model.step()
            state = model.state()
        steps += 1
        peak_tracking = max(peak_tracking, state["tracking_error_deg"])
        peak_linkage = max(peak_linkage, state["linkage_error_deg"])
        if state["tracking_error_deg"] > MAX_TRACKING_ERROR_DEG:
            raise RuntimeError(
                "Dynamics tracking error exceeds 5 degrees; slow down the trajectory"
            )
        if state["linkage_error_deg"] > LINKAGE_ERROR_DEG:
            raise RuntimeError("Dynamics linkage error exceeds 0.1 degrees")
        elapsed = float(model.data.time) - start_time
        if viewer is not None and elapsed >= next_display:
            if not wait_for_viewer(viewer, wall_start + elapsed):
                return False
            viewer.sync()
            next_display = elapsed + 1 / rate
        return True

    completed = True
    samples = iter(trajectory_samples(frames, 1 / dt))
    next(samples)  # The caller initialized the first pose before opening the viewer.
    for frame in samples:
        if not advance(frame.joints_deg):
            completed = False
            break

    if completed:
        stable_steps = 0
        for _ in range(math.ceil(SETTLE_TIMEOUT_S / dt)):
            if not advance(frames[-1].joints_deg):
                completed = False
                break
            state = model.state()
            arrived = (
                state["tracking_error_deg"] <= ARRIVAL_ERROR_DEG
                and state["max_joint_speed_deg_s"] <= ARRIVAL_SPEED_DEG_S
            )
            stable_steps = stable_steps + 1 if arrived else 0
            if stable_steps * dt >= SETTLE_HOLD_S:
                break
        else:
            raise TimeoutError(
                "Dynamics did not settle at the final target within 2 simulated seconds"
            )

    if viewer is not None and completed:
        if wait_for_viewer(viewer, wall_start + float(model.data.time) - start_time):
            viewer.sync()
        else:
            completed = False
    return {
        "mode": "offline_joint_dynamics",
        "completed": completed,
        "physics_steps": steps,
        "simulation_time_s": float(model.data.time) - start_time,
        "requested_trajectory_time_s": frames[-1].time_s,
        "peak_tracking_error_deg": peak_tracking,
        "peak_linkage_error_deg": peak_linkage,
        **model.state(),
    }
