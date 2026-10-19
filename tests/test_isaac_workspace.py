"""Check the saved mirror scene without Isaac Sim or robot hardware."""

from pathlib import Path

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stage():
    return Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "00_OPEN_MG400.usda")), Sdf.Layer.CreateAnonymous()
    )


def test_main_entry_opens_mirror_scene_without_python(stage):
    assert stage.GetSessionLayer().empty
    assert not stage.GetCompositionErrors()
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/World")
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1
    assert UsdGeom.GetStageUpAxis(stage) == "Z"
    assert stage.GetPrimAtPath("/World/MG400").HasAPI(UsdPhysics.ArticulationRootAPI)
    assert sum(p.IsA(UsdPhysics.RevoluteJoint) for p in stage.Traverse()) == 8
    assert sum(p.IsA(UsdPhysics.PrismaticJoint) for p in stage.Traverse()) == 2
    assert {p.GetName() for p in stage.GetDefaultPrim().GetChildren()} == {
        "MG400",
        "Ground",
        "DomeLight",
        "KeyLight",
        "Camera",
    }
    for path, kind in (("DomeLight", UsdLux.DomeLight), ("KeyLight", UsdLux.DistantLight)):
        light = stage.GetPrimAtPath("/World/" + path)
        assert light.IsA(kind)
        assert light.GetAttribute("inputs:intensity").Get() > 0
    assert stage.GetPrimAtPath("/World/Ground").IsA(UsdGeom.Cube)


def test_mirror_scene_has_no_grasp_demo_parameters(stage):
    assert not stage.GetPrimAtPath("/World/physicsScene").IsActive()
    for prim in stage.Traverse():
        assert not prim.HasAPI(UsdPhysics.CollisionAPI)
        assert "PhysxContactReportAPI" not in prim.GetAppliedSchemas()
        assert not any(
            attribute.GetName().startswith("mg400:") for attribute in prim.GetAttributes()
        )
    for name in ("housing", "left_jaw", "right_jaw"):
        prim = stage.GetPrimAtPath(f"/World/MG400/pgea_{name}")
        assert prim.IsA(UsdGeom.Xform)
        assert not prim.HasAPI(UsdPhysics.RigidBodyAPI)
        assert not prim.HasAPI(UsdPhysics.MassAPI)


def test_main_entry_selects_existing_camera(stage):
    settings = stage.GetRootLayer().customLayerData["cameraSettings"]
    camera = stage.GetPrimAtPath(settings["boundCamera"])
    assert camera.IsA(UsdGeom.Camera)
    transform = UsdGeom.Xformable(camera).GetLocalTransformation()
    eye = transform.ExtractTranslation()
    target = eye - transform.TransformDir(Gf.Vec3d(0, 0, 1))
    assert settings["Perspective"]["position"] == eye
    assert settings["Perspective"]["target"] == target
