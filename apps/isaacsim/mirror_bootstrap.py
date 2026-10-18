"""Kit --exec entry point: open the saved MG400 scene and start receive-only mirroring."""

import asyncio
import builtins
import runpy
import time
from datetime import datetime, timezone
from pathlib import Path

import omni.kit.app
import omni.timeline
import omni.usd
from omni.kit.viewport.utility import get_active_viewport
from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[2]


async def bootstrap():
    app = omni.kit.app.get_app()
    context = omni.usd.get_context()
    stage = context.get_stage()
    if stage is None or not stage.GetPrimAtPath("/World/MG400"):
        if stage is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = ROOT / f"isaacsim/before_mirror_load_{stamp}.usda"
            if not stage.Export(str(backup)):
                raise RuntimeError("Could not back up previous scene")
        omni.timeline.get_timeline_interface().stop()
        await app.next_update_async()
        scene = ROOT / "isaacsim/grasp_test_scene.usda"
        if not scene.exists():
            scene = ROOT / "isaacsim/grasp_scene.usda"
        opened, error = await context.open_stage_async(str(scene))
        if not opened:
            raise RuntimeError(f"Could not load MG400 scene: {error}")
    namespace = runpy.run_path(str(ROOT / "apps/isaacsim/mirror.py"))
    await namespace["MG400_MIRROR_START_TASK"]
    mirror = getattr(builtins, namespace["SINGLETON"])
    deadline = time.monotonic() + 15
    while mirror.report["status"] == "starting":
        if time.monotonic() >= deadline:
            raise TimeoutError("MG400 mirror did not finish scene setup")
        await app.next_update_async()
    if mirror.report["status"] not in ("live", "stale"):
        raise RuntimeError(f"MG400 mirror setup failed: {mirror.report}")

    viewport = get_active_viewport()
    if viewport is not None:
        stage = context.get_stage()
        camera_path = "/World/MirrorCamera"
        pose = (
            Gf.Matrix4d()
            .SetLookAt(Gf.Vec3d(0.9, -1.0, 0.72), Gf.Vec3d(0.16, 0, 0.20), Gf.Vec3d(0, 0, 1))
            .GetInverse()
        )
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            # Nearby camera guides can occlude the robot; retain their source definitions.
            for prim in stage.Traverse():
                if prim.IsA(UsdGeom.Camera) and str(prim.GetPath()) != camera_path:
                    UsdGeom.Imageable(prim).MakeInvisible()
            camera = UsdGeom.Camera.Define(stage, camera_path)
            UsdGeom.Imageable(camera).MakeVisible()
            xform = UsdGeom.Xformable(camera)
            attribute = camera.GetPrim().GetAttribute("xformOp:transform")
            op = UsdGeom.XformOp(attribute) if attribute else xform.AddTransformOp()
            xform.SetXformOpOrder([op])
            op.Set(pose)
            camera.CreateFocalLengthAttr(24)
            camera.CreateHorizontalApertureAttr(29.382624)
            camera.CreateVerticalApertureAttr(16.527725)
            camera.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100))
        viewport.camera_path = camera_path
        await asyncio.wait_for(viewport.wait_for_rendered_frames(1), timeout=15)
        # Kit may retain the old camera transform on selection until it is authored again.
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            op.Set(pose)
        await asyncio.wait_for(viewport.wait_for_rendered_frames(3), timeout=15)


MG400_MIRROR_BOOTSTRAP_TASK = asyncio.ensure_future(bootstrap())
