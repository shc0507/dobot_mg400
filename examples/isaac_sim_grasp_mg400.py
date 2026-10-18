"""PhysX pinch/lift/release test in Isaac Sim 6.1, or its existing Script Editor.

GUI: import runpy; runpy.run_path('/root/Documents/mg400-isaac/examples/isaac_sim_grasp_mg400.py')
Standalone: /isaac-sim/python.sh examples/isaac_sim_grasp_mg400.py
Only the initial robot state is assigned; all subsequent motion uses drives.
The object is a free, gravity-enabled rigid body throughout the test.
"""

import asyncio
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Launch only when executed by Isaac's python.sh, never from Script Editor.
simulation_app = None
if __name__ == "__main__":
    from isaacsim import SimulationApp

    simulation_app = SimulationApp({"headless": True, "width": 1280, "height": 720})

import numpy as np  # noqa: E402
import omni.kit.app  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
import warp as wp  # noqa: E402
from isaacsim.core.simulation_manager import SimulationManager  # noqa: E402
from omni.kit.viewport.utility import (  # noqa: E402
    capture_viewport_to_file,
    get_active_viewport,
)
from pxr import Gf, PhysxSchema, UsdGeom, UsdPhysics, UsdShade  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.joint_limits import validate_joints  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402

ROBOT = "/World/MG400"
FINGERS = [f"{ROBOT}/pgea_{side}_jaw" for side in ("left", "right")]
SLIDES = [f"pgea_{side}_slide" for side in ("left", "right")]
OBJECT = "/World/GraspObject"
OPEN_GAP = 0.0119
DT = 1 / 120
LOW_JOINTS = (0.0, 25.0, 45.0, 0.0)
MG400_WINDOW = None
MG400_CONTROL_TASK = None


def show_controls(robot, device, indices, slide_indices, report):
    """Keep a small reusable gripper control in the current streaming session."""
    import omni.ui as ui

    global MG400_WINDOW
    MG400_WINDOW = ui.Window("MG400 Gripper", width=340, height=170)
    MG400_WINDOW.setPosition(20, 80)
    with MG400_WINDOW.frame:
        with ui.VStack(spacing=8):
            ui.Label(f"PASS: lift {report['min_held_lift_m'] * 1000:.1f} mm; release verified")
            status = ui.Label("Open 11.9 mm / closed 1.9 mm; max 2 N per jaw")

            async def command(close):
                timeline = omni.timeline.get_timeline_interface()
                try:
                    timeline.play(looping=False)
                    targets = robot.get_dof_position_targets().numpy().copy()
                    targets[0, slide_indices] = close
                    robot.set_dof_position_targets(
                        wp.array(targets, dtype=wp.float32, device=device), indices
                    )
                    began = SimulationManager.get_num_physics_steps()
                    deadline = time.monotonic() + 30
                    while SimulationManager.get_num_physics_steps() - began < 240:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Gripper control timed out; rerun the test")
                        await omni.kit.app.get_app().next_update_async()
                    q = robot.get_dof_positions().numpy()[0]
                    gap = float(OPEN_GAP - q[slide_indices].sum())
                    status.text = f"Measured gap: {gap * 1000:.2f} mm"
                    return gap
                except Exception as error:
                    status.text = str(error)
                finally:
                    timeline.pause()

            def start(close):
                global MG400_CONTROL_TASK
                if MG400_CONTROL_TASK is None or MG400_CONTROL_TASK.done():
                    MG400_CONTROL_TASK = asyncio.ensure_future(command(close))

            with ui.HStack(spacing=8):
                ui.Button("Open", clicked_fn=lambda: start(0.0))
                ui.Button("Close", clicked_fn=lambda: start(0.005))
    return command


def flange_position(joints):
    """Exact CAD FK used by the import validation, in metres."""
    j1, j2, j3, _ = map(math.radians, joints)
    radial = (
        0.1095
        - 0.0010512570171516 * math.cos(j2)
        + 0.175001164344716 * math.sin(j2)
        + 0.175 * math.cos(j3)
        + 0.00325 * math.sin(j3)
    )
    lateral = -0.0000001243781102
    height = (
        0.175
        + 0.0010512570171516 * math.sin(j2)
        + 0.175001164344716 * math.cos(j2)
        - 0.175 * math.sin(j3)
        + 0.00325 * math.cos(j3)
    )
    return np.array(
        [
            -0.005 + radial * math.cos(j1) - lateral * math.sin(j1),
            radial * math.sin(j1) + lateral * math.cos(j1),
            height,
        ]
    )


def raised_pose(height):
    """Solve J2/J3 to lift vertically from the pickup pose."""
    target = flange_position(LOW_JOINTS) + (0, 0, height)
    joints = np.array(LOW_JOINTS)
    for _ in range(20):
        error = target[[0, 2]] - flange_position(joints)[[0, 2]]
        if np.linalg.norm(error) < 1e-9:
            return validate_joints(joints.tolist())
        jacobian = np.column_stack(
            [
                (flange_position(joints + delta)[[0, 2]] - flange_position(joints)[[0, 2]]) / 1e-4
                for delta in (np.array([0, 1e-4, 0, 0]), np.array([0, 0, 1e-4, 0]))
            ]
        )
        joints[1:3] += np.linalg.solve(jacobian, error)
    raise RuntimeError("Could not solve the vertical pickup trajectory")


def box(stage, path, position, size, color, mass=None):
    shape = UsdGeom.Cube.Define(stage, path)
    shape.CreateSizeAttr(1)
    shape.AddTranslateOp().Set(Gf.Vec3d(*position))
    shape.AddScaleOp().Set(Gf.Vec3f(*size))
    shape.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    UsdPhysics.CollisionAPI.Apply(shape.GetPrim())
    collision = PhysxSchema.PhysxCollisionAPI.Apply(shape.GetPrim())
    collision.CreateContactOffsetAttr(0.0002)
    collision.CreateRestOffsetAttr(0)
    if mass is not None:
        UsdPhysics.RigidBodyAPI.Apply(shape.GetPrim())
        UsdPhysics.MassAPI.Apply(shape.GetPrim()).CreateMassAttr(mass)
    return shape.GetPrim()


async def grasp_test():
    report_path = ROOT / "isaacsim/grasp_validation.json"
    report = {"success": False, "mode": "force_driven_friction_grasp", "phases": []}
    app = omni.kit.app.get_app()
    timeline = omni.timeline.get_timeline_interface()
    samples = []
    try:
        context = omni.usd.get_context()
        previous = context.get_stage()
        if previous is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = ROOT / f"isaacsim/before_grasp_{stamp}.usda"
            if not previous.Export(str(backup)):
                raise RuntimeError("Could not back up the current stage")
            report["previous_scene_backup"] = str(backup)
        del previous
        timeline.stop()
        await app.next_update_async()
        opened, error = await context.open_stage_async(str(ROOT / "isaacsim/grasp_scene.usda"))
        if not opened:
            raise RuntimeError(error)
        stage = context.get_stage()
        stage.SetEditTarget(stage.GetSessionLayer())
        stage.SetEndTimeCode(7200)
        stage.SetTimeCodesPerSecond(60)
        physics = PhysxSchema.PhysxSceneAPI.Apply(stage.GetPrimAtPath("/World/physicsScene"))
        physics.CreateTimeStepsPerSecondAttr(120)
        physics.CreateEnableGPUDynamicsAttr(False)
        physics.CreateBroadphaseTypeAttr("MBP")
        physics.CreateSolverTypeAttr("TGS")
        low = flange_position(LOW_JOINTS)
        object_start = low - (0, 0, 0.0965)
        support_top = float(object_start[2] - 0.01)
        box(
            stage,
            "/World/GraspSupport",
            (low[0], low[1], support_top / 2),
            (0.12, 0.12, support_top),
            (0.18, 0.23, 0.28),
        )
        cube = box(
            stage,
            OBJECT,
            object_start + (0, 0, 0.0005),
            (0.008, 0.008, 0.02),
            (0.95, 0.24, 0.045),
            mass=0.005,
        )
        friction = UsdShade.Material.Define(stage, "/World/GraspMaterial")
        mat = UsdPhysics.MaterialAPI.Apply(friction.GetPrim())
        mat.CreateStaticFrictionAttr(1.0)
        mat.CreateDynamicFrictionAttr(1.0)
        mat.CreateRestitutionAttr(0)
        UsdShade.MaterialBindingAPI.Apply(cube).Bind(friction, materialPurpose="physics")
        for path in FINGERS:
            PhysxSchema.PhysxContactReportAPI.Apply(stage.GetPrimAtPath(path)).CreateThresholdAttr(
                0
            )
        # Drive gains are simulation settings; linkage coupling is commanded, not closed-loop.
        for name in JOINT_NAMES:
            drive = UsdPhysics.DriveAPI(
                stage.GetPrimAtPath(f"{ROBOT}/joints/mg400_{name}"), "angular"
            )
            drive.CreateStiffnessAttr(10000)
            drive.CreateDampingAttr(500)
        high = raised_pose(0.08)
        start = raised_pose(0.05)
        by_name = dict(
            zip(["mg400_" + name for name in JOINT_NAMES], joint_angles(start), strict=True)
        )
        by_name.update(dict.fromkeys(SLIDES, 0.0))
        for name, value in by_name.items():
            kind = "linear" if name in SLIDES else "angular"
            target = value if kind == "linear" else math.degrees(value)
            UsdPhysics.DriveAPI(
                stage.GetPrimAtPath(f"{ROBOT}/joints/{name}"), kind
            ).CreateTargetPositionAttr(target)
        viewport = get_active_viewport()
        if viewport is not None:
            camera = UsdGeom.Camera.Define(stage, "/World/GraspCamera")
            camera.AddTransformOp().Set(
                Gf.Matrix4d()
                .SetLookAt(Gf.Vec3d(0.70, -0.65, 0.48), Gf.Vec3d(0.18, 0, 0.19), Gf.Vec3d(0, 0, 1))
                .GetInverse()
            )
            camera.CreateFocalLengthAttr(35)
            camera.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100))
            viewport.camera_path = "/World/GraspCamera"
        context.get_selection().set_selected_prim_paths([], True)
        SimulationManager.switch_physics_engine("physx")
        timeline.play(looping=False)
        await app.next_update_async()
        SimulationManager.initialize_physics()
        actual_dt = SimulationManager.get_physics_scenes()[0].get_dt()
        if abs(actual_dt - DT) > 1e-7:
            raise RuntimeError(f"Expected 120 Hz physics, got dt={actual_dt}")
        simulation = SimulationManager.get_physics_simulation_view()
        robot = simulation.create_articulation_view(ROBOT)
        meta = robot.get_metatype(0)
        names = list(meta.dof_names)
        if (
            robot.count != 1
            or robot.max_dofs != 10
            or set(names) != set(by_name)
            or not meta.fixed_base
        ):
            raise RuntimeError(f"Expected fixed-base 10-DOF MG400, got {names}")
        device = robot.get_dof_positions().device
        indices = wp.array([0], dtype=wp.int32, device=device)
        initial = wp.array([[by_name[name] for name in names]], dtype=wp.float32, device=device)
        robot.set_dof_positions(initial, indices)
        robot.set_dof_velocities(wp.zeros((1, 10), dtype=wp.float32, device=device), indices)
        robot.set_dof_position_targets(initial, indices)
        object_view = simulation.create_rigid_body_view(OBJECT)
        contacts = simulation.create_rigid_contact_view(
            FINGERS, [[OBJECT], [OBJECT]], max_contact_data_count=64
        )
        slide_indices = [names.index(name) for name in SLIDES]
        flange_index = meta.link_indices["mg400_end_effector_flange"]
        base_index = meta.link_indices["mg400_base_link"]
        report.update(
            dof_names=names,
            fixed_base=bool(meta.fixed_base),
            object_mass_kg=0.005,
            object_size_m=[0.008, 0.008, 0.02],
            support_top_m=support_top,
            friction=1.0,
            physics_dt=DT,
            object_pose_assignments_after_setup=0,
            robot_pose_assignments=1,
            physics_engine=SimulationManager.get_active_physics_engine(),
        )
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        first_step = SimulationManager.get_num_physics_steps()

        def measure(phase):
            q = robot.get_dof_positions().numpy()[0]
            obj = object_view.get_transforms().numpy()[0]
            velocity = object_view.get_velocities().numpy()[0]
            links = robot.get_link_transforms().numpy()[0]
            forces = contacts.get_contact_force_matrix(DT).numpy()[:, 0, :]
            if not all(np.isfinite(value).all() for value in (q, obj, velocity, links, forces)):
                raise RuntimeError("Non-finite physics state")
            sample = dict(
                phase=phase,
                step=SimulationManager.get_num_physics_steps() - first_step,
                object_xyz_m=obj[:3].tolist(),
                object_speed_m_s=float(np.linalg.norm(velocity[:3])),
                flange_xyz_m=links[flange_index, :3].tolist(),
                gap_m=float(OPEN_GAP - q[slide_indices].sum()),
                jaw_positions_m=q[slide_indices].tolist(),
                jaw_contact_N=np.linalg.norm(forces, axis=1).tolist(),
                base_drift_m=float(np.linalg.norm(links[base_index, :3])),
            )
            samples.append(sample)
            return sample

        async def move(phase, duration, start_joints, end_joints, start_close, end_close):
            began = SimulationManager.get_num_physics_steps()
            count = max(1, round(duration / DT))
            deadline = time.monotonic() + 60
            while (step := SimulationManager.get_num_physics_steps() - began) < count:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Physics did not finish phase {phase} within 60 seconds")
                fraction = min(1.0, step / count)
                blend = fraction * fraction * (3 - 2 * fraction)
                joints = tuple(
                    a + blend * (b - a) for a, b in zip(start_joints, end_joints, strict=True)
                )
                targets = dict(
                    zip(
                        ["mg400_" + name for name in JOINT_NAMES], joint_angles(joints), strict=True
                    )
                )
                targets.update(
                    dict.fromkeys(SLIDES, start_close + blend * (end_close - start_close))
                )
                robot.set_dof_position_targets(
                    wp.array([[targets[name] for name in names]], dtype=wp.float32, device=device),
                    indices,
                )
                await app.next_update_async()
                measure(phase)
            last = samples[-1]
            report["phases"].append(last)
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            print(
                f"MG400 grasp: {phase}: gap={last['gap_m'] * 1000:.2f} mm, "
                f"z={last['object_xyz_m'][2] * 1000:.2f} mm, "
                f"contacts={last['jaw_contact_N']}"
            )
            return last

        await move("settle_open", 1.0, start, start, 0, 0)
        closed_empty = await move("close_empty", 1.0, start, start, 0, 0.005)
        if abs(closed_empty["gap_m"] - 0.0019) > 0.0005:
            raise RuntimeError("Empty gripper did not complete its 10 mm total stroke")
        opened = await move("reopen", 1.0, start, start, 0.005, 0)
        if abs(opened["gap_m"] - OPEN_GAP) > 0.0005:
            raise RuntimeError("Gripper failed to reopen")
        await move("approach", 2.0, start, LOW_JOINTS, 0, 0)
        before = await move("at_object", 0.5, LOW_JOINTS, LOW_JOINTS, 0, 0)
        grasped = await move("close_on_object", 1.5, LOW_JOINTS, LOW_JOINTS, 0, 0.005)
        if min(grasped["jaw_contact_N"]) < 0.1 or not 0.0065 < grasped["gap_m"] < 0.009:
            raise RuntimeError("No verified two-sided contact with the object")
        await move("lift", 3.0, LOW_JOINTS, high, 0.005, 0.005)
        held = await move("hold", 2.0, high, high, 0.005, 0.005)
        hold_samples = [sample for sample in samples if sample["phase"] == "hold"]
        lift = min(sample["object_xyz_m"][2] for sample in hold_samples) - before["object_xyz_m"][2]
        slip = max(sample["object_xyz_m"][2] for sample in hold_samples) - min(
            sample["object_xyz_m"][2] for sample in hold_samples
        )
        if lift < 0.06 or slip > 0.003 or min(held["jaw_contact_N"]) < 0.1:
            raise RuntimeError(f"Object was not held securely: lift={lift}, slip={slip}")
        if viewport is not None:
            await capture_viewport_to_file(
                viewport, str(ROOT / "isaacsim/grasp_held.png")
            ).wait_for_result()
        await move("release", 1.0, high, high, 0.005, 0)
        released = await move("settle_released", 2.0, high, high, 0, 0)
        settled = [
            sample
            for sample in samples
            if sample["phase"] == "settle_released" and sample["step"] >= released["step"] - 60
        ]
        final_xyz = np.array(released["object_xyz_m"])
        if (
            abs(final_xyz[2] - (support_top + 0.01)) > 0.002
            or np.linalg.norm(final_xyz[:2] - low[:2]) > 0.04
            or max(released["jaw_contact_N"]) > 0.05
            or max(sample["object_speed_m_s"] for sample in settled) > 0.01
            or any(
                abs(sample["object_xyz_m"][2] - support_top - 0.01) > 0.002 for sample in settled
            )
        ):
            raise RuntimeError("Released object did not fall and settle on the support")
        if max(sample["base_drift_m"] for sample in samples) > 0.0001:
            raise RuntimeError("Robot base drifted")
        report.update(
            success=True,
            min_held_lift_m=lift,
            hold_vertical_slip_m=slip,
            physics_steps=SimulationManager.get_num_physics_steps() - first_step,
            held_contact_N=held["jaw_contact_N"],
            final_object_xyz_m=final_xyz.tolist(),
            open_gap_m=opened["gap_m"],
            empty_closed_gap_m=closed_empty["gap_m"],
        )
        # Save reproducible setup separately; recording the live result does not pin the object.
        stage.Export(str(ROOT / "isaacsim/grasp_test_scene.usda"))
        if viewport is not None:
            await capture_viewport_to_file(
                viewport, str(ROOT / "isaacsim/grasp_released.png")
            ).wait_for_result()
        if simulation_app is None:
            command = show_controls(robot, device, indices, slide_indices, report)
            # Exercise the same callbacks used by the visible buttons, ending open.
            closed_gap = await command(0.005)
            open_gap = await command(0.0)
            if (
                closed_gap is None
                or open_gap is None
                or abs(closed_gap - 0.0019) > 0.0005
                or abs(open_gap - OPEN_GAP) > 0.0005
            ):
                raise RuntimeError("GUI gripper controls did not complete close/open")
            report.update(control_closed_gap_m=closed_gap, control_open_gap_m=open_gap)
    except Exception:
        report["success"] = False
        report["error"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        timeline.pause()
        report["samples"] = samples
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(
            "MG400_GRASP_RESULT",
            json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2),
        )
    return report


MG400_GRASP_TASK = asyncio.ensure_future(grasp_test())
if simulation_app is not None:
    while not MG400_GRASP_TASK.done():
        simulation_app.update()
    result = MG400_GRASP_TASK.result()
    simulation_app.close()
    raise SystemExit(0 if result["success"] else 1)
