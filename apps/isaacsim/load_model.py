"""Load and verify MG400 in an existing Isaac Sim 6.1 GUI/streaming session.

In Script Editor: import runpy; runpy.run_path('/path/to/apps/isaacsim/load_model.py')
Also usable as a Kit --exec script. Does not create a second SimulationApp.
The current stage is backed up before opening the packaged scene. This verifies
coupled kinematic poses, not physical linkage closure or gripper actuation.
"""

import asyncio
import json
import math
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import omni.kit.app
import omni.timeline
import omni.usd
import warp as wp
from isaacsim.core.simulation_manager import SimulationManager
from omni.kit.viewport.utility import get_active_viewport
from pxr import Gf, UsdGeom, UsdPhysics

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mg400_agent.gripper_mirror import JawMirror  # noqa: E402
from mg400_agent.joint_limits import validate_joints  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402
from mg400_agent.runtime_paths import data_path, export_scene  # noqa: E402


async def load_mg400() -> None:
    scene_path = ROOT / "isaacsim/scene.usda"
    report_path = data_path("isaacsim/isaac_sim_validation.json")
    report = {"success": False, "scene": str(scene_path), "mode": "kinematic_pose_check"}
    app = omni.kit.app.get_app()
    timeline = omni.timeline.get_timeline_interface()
    try:
        if not scene_path.is_file():
            raise FileNotFoundError(f"Build the Isaac assets first: {scene_path}")
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        context = omni.usd.get_context()
        previous_stage = context.get_stage()
        if previous_stage is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = data_path(f"isaacsim/previous_scene_{stamp}.usda")
            if not export_scene(previous_stage, backup):
                raise RuntimeError("Could not back up the current scene")
            report["previous_scene_backup"] = str(backup)
        del previous_stage
        timeline.stop()
        await app.next_update_async()
        opened, error = await context.open_stage_async(str(scene_path))
        if not opened:
            raise RuntimeError(f"Could not open MG400 scene: {error}")
        stage = context.get_stage()
        if not stage.GetPrimAtPath("/World/MG400").IsValid():
            raise RuntimeError("Imported stage has no /World/MG400 prim")

        # Keep runtime edits out of the portable asset's source layers.
        stage.SetEditTarget(stage.GetSessionLayer())
        jaw_mirror = JawMirror(stage)
        inverse_root = (
            UsdGeom.XformCache()
            .GetLocalToWorldTransform(stage.GetPrimAtPath("/World/MG400"))
            .GetInverse()
        )
        viewport = get_active_viewport()
        if viewport is not None:
            viewport.camera_path = "/World/Camera"
        context.get_selection().set_selected_prim_paths(["/World/MG400"], True)
        target = validate_joints((20, 15, 35, 25))
        names = ["mg400_" + name for name in JOINT_NAMES]
        target_by_name = dict(zip(names, joint_angles(target), strict=True))
        flange_path = "/World/MG400/mg400_end_effector_flange"
        gripper = stage.GetPrimAtPath(f"{flange_path}/pgea_gripper")
        mount = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 90))
        if not gripper.IsA(UsdGeom.Xform) or not np.allclose(
            UsdGeom.Xformable(gripper).GetLocalTransformation(), mount, rtol=0, atol=1e-9
        ):
            raise RuntimeError("Missing or incorrect PGEA mount at the flange")
        for relative_path, depth in (
            ("mg400_flange", 0.0),
            ("pgea_gripper/pgea_jaw_center", -0.089),
            ("pgea_gripper/pgea_tip", -0.104),
        ):
            frame = stage.GetPrimAtPath(f"{flange_path}/{relative_path}")
            if not frame.IsA(UsdGeom.Xform) or not np.allclose(
                UsdGeom.Xformable(frame).GetLocalTransformation().ExtractTranslation(),
                (0, 0, depth),
                rtol=0,
                atol=1e-9,
            ):
                raise RuntimeError(f"Missing or incorrect gripper/flange frame: {relative_path}")
        for prim in stage.Traverse():
            if prim.GetName() in target_by_name and prim.IsA(UsdPhysics.RevoluteJoint):
                UsdPhysics.DriveAPI.Apply(prim, "angular").CreateTargetPositionAttr(
                    math.degrees(target_by_name[prim.GetName()])
                )

        SimulationManager.switch_physics_engine("physx")
        timeline.play(looping=False)
        await app.next_update_async()
        # Explicit initialization also handles existing sessions whose setup
        # callback has already run before this script opened a new stage.
        SimulationManager.initialize_physics()
        simulation = SimulationManager.get_physics_simulation_view()
        if simulation is None:
            raise RuntimeError("Isaac Sim did not initialize the physics tensor view")
        robot = simulation.create_articulation_view("/World/MG400")
        if robot.count != 1 or robot.max_dofs != 8:
            raise RuntimeError(
                f"Expected one 8-DOF articulation; got {robot.count}/{robot.max_dofs}"
            )
        metadata = robot.get_metatype(0)
        actual_names = list(metadata.dof_names)
        if set(actual_names) != set(names) or not metadata.fixed_base:
            raise RuntimeError(f"Unexpected joint names or unfixed base: {actual_names}")
        # Independent exact-CAD FK, also used by tests/test_mujoco_model.py.
        j1, j2, j3, j4 = map(math.radians, target)
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
        expected_flange = np.array(
            [
                -0.005 + radial * math.cos(j1) - lateral * math.sin(j1),
                radial * math.sin(j1) + lateral * math.cos(j1),
                height,
            ]
        )
        expected_quaternion = np.array([0, 0, math.sin((j1 + j4) / 2), math.cos((j1 + j4) / 2)])
        flange_index = metadata.link_indices["mg400_end_effector_flange"]
        base_index = metadata.link_indices["mg400_base_link"]
        positions_np = np.array([[target_by_name[name] for name in actual_names]], dtype=np.float32)
        device = robot.get_dof_positions().device
        positions = wp.array(positions_np, dtype=wp.float32, device=device)
        velocities = wp.zeros((1, 8), dtype=wp.float32, device=device)
        articulation_indices = wp.array([0], dtype=wp.int32, device=device)
        first_step = SimulationManager.get_num_physics_steps()
        peak_error = 0.0
        peak_flange_mm = peak_orientation_deg = peak_base_mm = 0.0
        for _ in range(30):
            robot.set_dof_positions(positions, articulation_indices)
            robot.set_dof_velocities(velocities, articulation_indices)
            robot.set_dof_position_targets(positions, articulation_indices)
            await app.next_update_async()
            actual = robot.get_dof_positions().numpy()
            if not np.isfinite(actual).all():
                raise RuntimeError("Isaac Sim returned a non-finite joint state")
            peak_error = max(peak_error, float(np.rad2deg(np.max(np.abs(actual - positions_np)))))
            if peak_error > 0.1:
                raise RuntimeError(f"Joint pose changed by {peak_error:.6f} degrees")
            # Tensor poses include live PhysX/Fabric state; USD transforms may lag.
            links = robot.get_link_transforms().numpy()[0].astype(np.float64)
            if not np.isfinite(links).all():
                raise RuntimeError("Isaac Sim returned a non-finite link pose")
            flange = links[flange_index]
            peak_flange_mm = max(
                peak_flange_mm, float(np.linalg.norm(flange[:3] - expected_flange) * 1000)
            )
            peak_base_mm = max(peak_base_mm, float(np.linalg.norm(links[base_index, :3]) * 1000))
            norm = np.linalg.norm(flange[3:])
            if abs(norm - 1) > 0.01:
                raise RuntimeError("Isaac Sim returned an invalid flange quaternion")
            quaternion = flange[3:] / norm  # Tensor order: x, y, z, w.
            flange_pose = Gf.Matrix4d(
                Gf.Matrix3d(Gf.Quatd(quaternion[3], Gf.Vec3d(*quaternion[:3]))),
                Gf.Vec3d(*flange[:3]),
            )
            jaw_mirror.apply(1000, flange_pose=flange_pose * inverse_root)
            orientation_deg = math.degrees(
                2 * math.acos(float(np.clip(abs(np.dot(quaternion, expected_quaternion)), 0, 1)))
            )
            peak_orientation_deg = max(peak_orientation_deg, orientation_deg)
            if peak_flange_mm > 0.1 or peak_base_mm > 0.1 or peak_orientation_deg > 0.1:
                raise RuntimeError(
                    f"CAD pose mismatch: flange={peak_flange_mm:.6f} mm, "
                    f"orientation={peak_orientation_deg:.6f} deg, base={peak_base_mm:.6f} mm"
                )
        physics_steps = SimulationManager.get_num_physics_steps() - first_step
        if physics_steps <= 0:
            raise RuntimeError("No physics steps occurred during the pose check")
        timeline.pause()
        report.update(
            success=True,
            articulation_count=robot.count,
            dof_count=robot.max_dofs,
            dof_names=actual_names,
            fixed_base=bool(metadata.fixed_base),
            physics_steps=physics_steps,
            target_joints_deg=target,
            actual_dof_radians=actual.tolist(),
            max_joint_error_deg=peak_error,
            flange_position_m=flange[:3].tolist(),
            expected_flange_position_m=expected_flange.tolist(),
            max_flange_position_error_mm=peak_flange_mm,
            max_flange_orientation_error_deg=peak_orientation_deg,
            max_base_drift_mm=peak_base_mm,
            gripper_frames_verified=True,
            physics_engine=SimulationManager.get_active_physics_engine(),
        )
    except Exception:
        timeline.pause()
        report["error"] = traceback.format_exc()
        raise
    finally:
        if report_path.parent.is_dir():
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


MG400_LOAD_TASK = asyncio.ensure_future(load_mg400())
