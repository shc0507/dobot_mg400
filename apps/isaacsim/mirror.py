"""Mirror receive-only MG400 feedback in the current Isaac Sim 6.1 scene.

Script Editor: import runpy; runpy.run_path('/path/to/apps/isaacsim/mirror.py')
Only TCP 30004 is opened. Physics stays stopped: this displays measured poses,
without sending robot commands or simulating contacts. Stop with the UI button.
"""

import asyncio
import builtins
import json
import math
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from pxr import Gf, Usd, UsdGeom, UsdPhysics

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.controller_ownership import ControllerOwnership  # noqa: E402
from mg400_agent.feedback import FeedbackConnection  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402
from mg400_agent.robot_config import load_robot_host  # noqa: E402
from mg400_agent.runtime_paths import data_path, export_scene  # noqa: E402

ROBOT = "/World/MG400"
STALE_SECONDS = 0.5
REPORT = data_path("isaacsim/mirror_status.json")
ROBOT_CONFIG = ROOT / "config/robot.toml"
SINGLETON = "_mg400_isaac_feedback_mirror"


def frame(joint, index):
    matrix = Gf.Matrix4d().SetRotate(
        Gf.Rotation(joint.GetAttribute(f"physics:localRot{index}").Get())
    )
    matrix.SetTranslateOnly(Gf.Vec3d(joint.GetAttribute(f"physics:localPos{index}").Get()))
    return matrix


def set_transform(prim, matrix):
    """Replace the composed transform in the session layer, retaining source assets."""
    xform = UsdGeom.Xformable(prim)
    attr = prim.GetAttribute("xformOp:transform:mg400Mirror")
    op = UsdGeom.XformOp(attr) if attr else xform.AddTransformOp(opSuffix="mg400Mirror")
    op.Set(matrix)
    xform.SetXformOpOrder([op])


class UsdPoseMirror:
    """Evaluate the existing USD joint tree; arm radians and jaw metres stay distinct."""

    def __init__(self, stage, jaw_positions=None):
        self.stage = stage
        self.root = stage.GetPrimAtPath(ROBOT)
        if not self.root or not self.root.HasAPI(UsdPhysics.ArticulationRootAPI):
            raise ValueError(f"Load the project's MG400 articulation at {ROBOT} first")
        self.jaws = jaw_positions or {}
        self.joints = []
        seen = set()
        pending = [p for p in Usd.PrimRange(self.root) if p.IsA(UsdPhysics.Joint)]
        while pending:
            ready = [
                p
                for p in pending
                if not UsdPhysics.Joint(p).GetBody0Rel().GetTargets()
                or UsdPhysics.Joint(p).GetBody0Rel().GetTargets()[0] in seen
            ]
            if not ready:
                raise ValueError("MG400 joint tree has an orphan or loop")
            for prim in ready:
                joint = UsdPhysics.Joint(prim)
                parents = joint.GetBody0Rel().GetTargets()
                children = joint.GetBody1Rel().GetTargets()
                if len(children) != 1 or len(parents) > 1:
                    raise ValueError(f"Unexpected body bindings: {prim.GetPath()}")
                child = stage.GetPrimAtPath(children[0])
                if child.GetParent() != self.root:
                    raise ValueError("Expected the exported MG400's sibling link layout")
                kind = (
                    "angular"
                    if prim.IsA(UsdPhysics.RevoluteJoint)
                    else "linear"
                    if prim.IsA(UsdPhysics.PrismaticJoint)
                    else "fixed"
                )
                axis = Gf.Vec3d(0)
                if kind != "fixed":
                    axis["XYZ".index(prim.GetAttribute("physics:axis").Get())] = 1
                self.joints.append(
                    (
                        prim.GetName(),
                        parents[0] if parents else None,
                        children[0],
                        frame(prim, 0),
                        frame(prim, 1).GetInverse(),
                        kind,
                        axis,
                    )
                )
                seen.add(children[0])
                pending.remove(prim)
        names = {name for name, *_, kind, axis in self.joints if kind == "angular"}
        if names != {"mg400_" + name for name in JOINT_NAMES}:
            raise ValueError(f"Unexpected MG400 arm joints: {sorted(names)}")
        for name, parent, child, local0, inverse1, kind, axis in self.joints:
            if kind == "linear" and name not in self.jaws:
                child_pose = UsdGeom.Xformable(stage.GetPrimAtPath(child)).GetLocalTransformation()
                parent_pose = UsdGeom.Xformable(
                    stage.GetPrimAtPath(parent)
                ).GetLocalTransformation()
                motion = inverse1.GetInverse() * child_pose * parent_pose.GetInverse()
                motion *= local0.GetInverse()
                self.jaws[name] = float(Gf.Dot(motion.ExtractTranslation(), axis))

    def update(self, joints_deg):
        values = dict(
            zip(("mg400_" + n for n in JOINT_NAMES), joint_angles(joints_deg), strict=True)
        )
        poses = {}
        with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
            for name, parent, child, local0, inverse1, kind, axis in self.joints:
                motion = Gf.Matrix4d(1)
                if kind == "angular":
                    motion.SetRotate(Gf.Rotation(axis, math.degrees(values[name])))
                elif kind == "linear":
                    motion.SetTranslate(axis * self.jaws[name])
                pose = inverse1 * motion * local0 * poses.get(parent, Gf.Matrix4d(1))
                poses[child] = pose
                set_transform(self.stage.GetPrimAtPath(child), pose)
        return poses


class LiveMirror:
    def __init__(self, show_window=True):
        self.host = load_robot_host(ROBOT_CONFIG)
        self.ownership = ControllerOwnership(self.host)
        self.control_owner = None
        self.show_window = show_window
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.received = 0
        self.network_error = None
        self.window = None
        self.task = None
        self.pose_mirror = None
        self.report = {
            "status": "starting",
            "source": f"{self.host}:30004",
            "read_only": True,
            "physics": "stopped",
            "applied_frames": 0,
            "received_frames": 0,
        }

    def receive(self):
        while not self.stop_event.is_set():
            try:
                with FeedbackConnection(self.host, timeout_s=1.0) as connection:
                    while not self.stop_event.is_set():
                        feedback = connection.read_latest()
                        with self.lock:
                            self.received += 1
                            self.latest = (feedback, time.monotonic(), self.received)
                            self.network_error = None
            except (OSError, ValueError) as error:
                with self.lock:
                    self.network_error = str(error)
                self.stop_event.wait(1.0)

    def stop(self, reason="Stopped by user"):
        self.report.update(status="stopped", reason=reason)
        self.stop_event.set()

    def write_report(self):
        self.report["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = REPORT.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(REPORT)

    async def run(self):
        worker = None
        label = angles_label = details = None
        try:
            import omni.kit.app
            import omni.timeline
            import omni.ui as ui
            import omni.usd
            from isaacsim.core.simulation_manager import SimulationManager

            app = omni.kit.app.get_app()
            context = omni.usd.get_context()
            timeline = omni.timeline.get_timeline_interface()
            self.ownership.acquire()
            stage = context.get_stage()
            # Validate before any scene changes. Never replace the user's stage.
            UsdPoseMirror(stage)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = REPORT.parent / f"before_mirror_{stamp}.usda"
            if not export_scene(stage, backup):
                raise RuntimeError("Could not back up current stage")
            self.report["previous_scene_backup"] = str(backup)
            snapshots, jaws = {}, {}
            if not timeline.is_stopped():
                timeline.pause()
                simulation = SimulationManager.get_physics_simulation_view()
                if simulation is None:
                    raise RuntimeError("Cannot preserve live poses without the physics view")
                robot = simulation.create_articulation_view(ROBOT)
                names = list(robot.get_metatype(0).dof_names)
                q = robot.get_dof_positions().numpy()[0]
                jaws = {
                    name: float(q[i]) for i, name in enumerate(names) if name.startswith("pgea_")
                }
                cache = UsdGeom.XformCache()
                for prim in stage.Traverse():
                    if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                        view = simulation.create_rigid_body_view(str(prim.GetPath()))
                        xyzquat = view.get_transforms().numpy()[0].astype(float)
                        old = Gf.Transform(cache.GetLocalToWorldTransform(prim))
                        old.SetRotation(Gf.Rotation(Gf.Quatd(xyzquat[6], Gf.Vec3d(*xyzquat[3:6]))))
                        old.SetTranslation(Gf.Vec3d(*xyzquat[:3]))
                        snapshots[prim.GetPath()] = old.GetMatrix()
                del robot, simulation
            timeline.stop()
            await app.next_update_async()
            if context.get_stage() != stage:
                raise RuntimeError("Stage changed during mirror setup")
            with Usd.EditContext(stage, stage.GetSessionLayer()):
                cache = UsdGeom.XformCache()
                for path, world in snapshots.items():
                    prim = stage.GetPrimAtPath(path)
                    parent = cache.GetLocalToWorldTransform(prim.GetParent())
                    set_transform(prim, world * parent.GetInverse())
            # Existing gripper buttons own invalidated PhysX handles after Stop.
            gripper_window = ui.Workspace.get_window("MG400 Gripper")
            if gripper_window:
                gripper_window.visible = False
            mirror = UsdPoseMirror(stage, jaws)
            self.pose_mirror = mirror
            self.report["held_jaw_positions_m"] = mirror.jaws
            if self.show_window:
                self.window = ui.Window("MG400 Live Mirror", width=430, height=190)
                self.window.setPosition(15, 370)
                with self.window.frame:
                    with ui.VStack(spacing=7):
                        label = ui.Label("CONNECTING — real robot → simulation", word_wrap=True)
                        angles_label = ui.Label("")
                        details = ui.Label(
                            "Only receives TCP 30004; gripper held at its current pose.",
                            word_wrap=True,
                        )
                        ui.Button("Stop synchronization", clicked_fn=self.stop)
            worker = threading.Thread(target=self.receive, name="MG400-feedback", daemon=True)
            worker.start()
            last_applied = last_report = last_rate = 0
            rate_start = time.monotonic()
            while not self.stop_event.is_set():
                await app.next_update_async()
                if self.stop_event.is_set():
                    break
                if context.get_stage() != stage or not stage.GetPrimAtPath(ROBOT):
                    self.stop("Stage or MG400 model changed")
                    break
                if not timeline.is_stopped():
                    self.stop("Simulation started; stop physics and rerun to resume mirroring")
                    break
                now = time.monotonic()
                with self.lock:
                    latest, received, error = self.latest, self.received, self.network_error
                age = now - latest[1] if latest else None
                live = latest is not None and age < STALE_SECONDS and error is None
                self.report.update(
                    status="live" if live else "stale",
                    received_frames=received,
                    feedback_age_ms=age * 1000 if age is not None else None,
                    network_error=error,
                )
                if live and latest[2] != last_applied:
                    feedback, _, sequence = latest
                    poses = mirror.update(feedback.joints_deg)
                    last_applied = sequence
                    self.report["applied_frames"] += 1
                    cache = UsdGeom.XformCache()
                    flange = cache.GetLocalToWorldTransform(
                        stage.GetPrimAtPath(ROBOT + "/mg400_end_effector_flange")
                    )
                    transform_error = max(
                        abs(expected[row][column] - actual[row][column])
                        for path, expected in poses.items()
                        for actual in [
                            UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetLocalTransformation()
                        ]
                        for row in range(4)
                        for column in range(4)
                    )
                    if transform_error > 1e-9:
                        raise RuntimeError(f"USD pose readback mismatch: {transform_error}")
                    self.report.update(
                        joints_deg=feedback.joints_deg,
                        robot_mode=feedback.mode,
                        robot_error=feedback.error_status,
                        flange_position_m=list(flange.ExtractTranslation()),
                        max_transform_readback_error=transform_error,
                        verified_link_count=len(poses),
                    )
                if now - last_report >= 0.5:
                    count = self.report["applied_frames"]
                    rate = (count - last_rate) / (now - rate_start)
                    self.report["display_hz"] = rate
                    if label is not None:
                        label.text = (
                            f"LIVE · {rate:.1f} updates/s · {age * 1000:.0f} ms old"
                            if live
                            else "STALE / RECONNECTING — last pose held"
                        )
                        if latest:
                            angles_label.text = "  ".join(
                                f"J{i + 1}: {q:.2f}°" for i, q in enumerate(latest[0].joints_deg)
                            )
                        details.text = error or "Read-only · physics stopped · gripper pose held"
                    self.write_report()
                    last_report, last_rate, rate_start = now, count, now
        except Exception:
            self.report.update(status="error", error=traceback.format_exc())
            traceback.print_exc()
        finally:
            self.stop_event.set()
            if self.report["status"] != "error":
                self.report["status"] = "stopped"
            if worker:
                # The worker has a bounded one-second read; do not block Kit's UI.
                while worker.is_alive():
                    await app.next_update_async()
            if self.control_owner is None:
                self.ownership.release()
            if label is not None:
                label.text = "STOPPED — displayed pose is no longer live"
            if details is not None:
                details.text = self.report.get("reason", self.report.get("error", "Stopped"))
            self.write_report()
            print("MG400_MIRROR_RESULT", json.dumps(self.report))


async def start(show_window=True):
    lock_name = SINGLETON + "_start_lock"
    lock = getattr(builtins, lock_name, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(builtins, lock_name, lock)
    async with lock:
        old = getattr(builtins, SINGLETON, None)
        if old:
            if getattr(old, "control_owner", None) is not None:
                raise RuntimeError(
                    "The control panel owns synchronization. Stop sync from that panel."
                )
            old.stop("Replaced by a new mirror instance")
            if old.task:
                await old.task
            if old.window:
                old.window.destroy()
        mirror = LiveMirror(show_window=show_window)
        mirror.ownership.acquire()
        setattr(builtins, SINGLETON, mirror)
        mirror.task = asyncio.ensure_future(mirror.run())


if __name__ in ("__main__", "<run_path>"):
    MG400_MIRROR_START_TASK = asyncio.ensure_future(
        start(show_window=globals().get("SHOW_WINDOW", True))
    )
