#!/usr/bin/env python3
"""Load MG400 + fixed PGEA visuals with Isaac Sim 5.1's standalone Python.

Run with Isaac Sim's python.sh (Windows: python.bat), not this project's venv.
This is coupled kinematic playback: every step sets all eight joint states.
It does not simulate the parallel linkage or gripper actuation/contact.
Isaac Sim 6.x deprecates the World/SingleArticulation compatibility APIs used here.
"""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mg400_agent.joint_limits import validate_joint_segment, validate_joints  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usd", type=Path, default=ROOT / "isaacsim/mg400_with_gripper.usd")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--steps", type=int, help="Exit after this many simulation steps")
    parser.add_argument(
        "--joints", type=float, nargs=4, default=(20, 15, 35, 25), metavar=("J1", "J2", "J3", "J4")
    )
    parser.add_argument("--demo", action="store_true", help="Cycle from zero to --joints and back")
    args = parser.parse_args()
    if args.steps is not None and args.steps <= 0:
        parser.error("--steps must be positive")
    if not args.usd.is_file():
        parser.error(f"USD not found: {args.usd}; run scripts/build_isaac_model.py first")
    try:
        target = validate_joints(args.joints)
        if args.demo:
            validate_joint_segment((0, 0, 0, 0), target)
    except ValueError as error:
        parser.error(str(error))
    limit = args.steps if args.steps is not None else (240 if args.headless else None)

    # Omniverse/Isaac imports must follow SimulationApp construction.
    from isaacsim import SimulationApp

    app = SimulationApp({"headless": args.headless})
    try:
        import numpy as np
        from isaacsim.core.api import World
        from isaacsim.core.prims import SingleArticulation
        from isaacsim.core.utils.stage import add_reference_to_stage, get_current_stage
        from isaacsim.core.utils.types import ArticulationAction
        from isaacsim.core.utils.viewports import set_camera_view
        from pxr import UsdLux

        world = World(stage_units_in_meters=1.0, physics_dt=1 / 60, rendering_dt=1 / 60)
        world.get_physics_context().set_gravity(0.0)
        add_reference_to_stage(str(args.usd.resolve()), "/World/MG400")
        UsdLux.DomeLight.Define(get_current_stage(), "/World/light").CreateIntensityAttr(1500)
        if not args.headless:
            set_camera_view(eye=np.array([0.85, -0.85, 0.65]), target=np.array([0.17, 0, 0.2]))
        robot = world.scene.add(SingleArticulation(prim_path="/World/MG400", name="mg400"))
        world.reset()
        names = ["mg400_" + name for name in JOINT_NAMES]
        if set(robot.dof_names) != set(names):
            raise RuntimeError(f"Expected eight MG400 joints; found {robot.dof_names}")
        indices = np.array([robot.get_dof_index(name) for name in names], dtype=np.int32)
        zeros = np.zeros(len(names))
        steps = 0
        peak_error = 0.0
        while app.is_running() and (limit is None or steps < limit):
            if not world.is_playing():
                app.update()
                continue
            fraction = (1 - math.cos(2 * math.pi * steps / 240)) / 2 if args.demo else 1.0
            pose = tuple(value * fraction for value in target)
            positions = np.array(joint_angles(pose))
            robot.set_joint_positions(positions, joint_indices=indices)
            robot.set_joint_velocities(zeros, joint_indices=indices)
            robot.apply_action(ArticulationAction(joint_positions=positions, joint_indices=indices))
            world.step(render=not args.headless)
            actual = robot.get_joint_positions(joint_indices=indices)
            if not np.isfinite(actual).all():
                raise RuntimeError("Isaac Sim returned a non-finite joint state")
            error = float(np.rad2deg(np.max(np.abs(actual - positions))))
            peak_error = max(peak_error, error)
            if error > 0.1:
                raise RuntimeError(f"Kinematic pose changed by {error:.6f} degrees during a step")
            steps += 1
        print(
            json.dumps(
                {
                    "usd": str(args.usd.resolve()),
                    "mode": "kinematic_playback",
                    "steps": steps,
                    "joint_names": names,
                    "target_joints_deg": target,
                    "max_joint_error_deg": peak_error,
                },
                indent=2,
            )
        )
    finally:
        app.close()


if __name__ == "__main__":
    main()
