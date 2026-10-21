"""Test gripper display geometry without Isaac Sim or hardware connections."""

import runpy
from pathlib import Path

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics  # noqa: E402

from mg400_agent.gripper_mirror import JawMirror  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROBOT = "/World/MG400"
JAW_PATHS = {f"{ROBOT}/pgea_{side}_jaw" for side in ("left", "right")}


@pytest.fixture
def stage():
    return Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "00_OPEN_MG400.usda")),
        Sdf.Layer.CreateAnonymous(),
    )


def matrices(stage):
    return {
        str(prim.GetPath()): UsdGeom.Xformable(prim).GetLocalTransformation()
        for prim in stage.Traverse()
        if prim.IsA(UsdGeom.Xformable)
    }


@pytest.mark.parametrize("position", [0, 500, 1000])
def test_jaws_follow_fresh_housing_pose_without_changing_other_transforms(stage, position):
    namespace = runpy.run_path(str(ROOT / "apps/isaacsim/mirror.py"), run_name="test_jaw_mirror")
    arm = namespace["UsdPoseMirror"](stage)
    mirror = JawMirror(stage)
    sources = {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    edit_target = stage.GetEditTarget()
    for joints in ((0, 0, 0, 0), (60, 30, 40, 20), (-30, 10, 60, -50)):
        arm.update(joints)
        before = matrices(stage)
        result = mirror.apply(position)
        q = 0.005 * (1 - position / 1000)
        assert result == pytest.approx({"q_m": q, "gap_m": 0.0119 - 2 * q}, abs=1e-9)
        after = matrices(stage)
        assert {path: matrix for path, matrix in after.items() if path not in JAW_PATHS} == {
            path: matrix for path, matrix in before.items() if path not in JAW_PATHS
        }
        housing = after[f"{ROBOT}/pgea_housing"]
        for side, direction in (("left", 1), ("right", -1)):
            relative = after[f"{ROBOT}/pgea_{side}_jaw"] * housing.GetInverse()
            assert tuple(relative.ExtractTranslation()) == pytest.approx((0, direction * q, 0))
        assert mirror.apply(position) == pytest.approx(result)
        for path in JAW_PATHS:
            assert len(UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetOrderedXformOps()) == 1
    assert stage.GetEditTarget() == edit_target
    assert sources == {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }


@pytest.mark.parametrize("position", [0, 500, 1000])
def test_tensor_flange_pose_moves_only_display_parts_in_session_layer(stage, position):
    mirror = JawMirror(stage)
    sources = {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    edit_target = stage.GetEditTarget()
    before = matrices(stage)
    flange_path = f"{ROBOT}/mg400_end_effector_flange"
    actual_flange = Gf.Matrix4d(
        Gf.Matrix3d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 65)),
        Gf.Vec3d(0.1, 0.2, 0.4),
    )
    assert actual_flange != before[flange_path]
    mount = before[f"{flange_path}/pgea_gripper"]
    expected_housing = mount * actual_flange
    q = 0.005 * (1 - position / 1000)
    for _ in range(2):
        result = mirror.apply(position, flange_pose=actual_flange)
        assert result == pytest.approx({"q_m": q, "gap_m": 0.0119 - 2 * q}, abs=1e-9)
        after = matrices(stage)
        display_paths = JAW_PATHS | {f"{ROBOT}/pgea_housing"}
        assert {p: m for p, m in after.items() if p not in display_paths} == {
            p: m for p, m in before.items() if p not in display_paths
        }
        assert Gf.IsClose(after[f"{ROBOT}/pgea_housing"], expected_housing, 1e-7)
        for side, direction in (("left", 1), ("right", -1)):
            relative = after[f"{ROBOT}/pgea_{side}_jaw"] * expected_housing.GetInverse()
            assert tuple(relative.ExtractTranslation()) == pytest.approx(
                (0, direction * q, 0), abs=1e-9
            )
        for path in display_paths:
            assert len(UsdGeom.Xformable(stage.GetPrimAtPath(path)).GetOrderedXformOps()) == 1
    assert stage.GetEditTarget() == edit_target
    assert sources == {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }


@pytest.mark.parametrize(
    "position", [None, "500", True, False, float("nan"), float("inf"), -1, 1001, -0.01, 1000.01]
)
def test_invalid_position_changes_nothing(stage, position):
    mirror = JawMirror(stage)
    before = stage.GetSessionLayer().ExportToString()
    with pytest.raises(ValueError, match="finite number"):
        mirror.apply(position)
    assert stage.GetSessionLayer().ExportToString() == before


@pytest.mark.parametrize("fault", ["body", "axis", "limit", "frame", "missing", "units"])
def test_incompatible_joint_structure_is_rejected(stage, fault):
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        joint = UsdPhysics.PrismaticJoint(stage.GetPrimAtPath(f"{ROBOT}/joints/pgea_right_slide"))
        if fault == "body":
            joint.GetBody0Rel().SetTargets([f"{ROBOT}/mg400_end_effector_flange"])
        elif fault == "axis":
            joint.GetAxisAttr().Set("X")
        elif fault == "limit":
            joint.GetUpperLimitAttr().Set(0.01)
        elif fault == "frame":
            joint.GetLocalRot0Attr().Set(Gf.Quatf(1))
        elif fault == "missing":
            joint.GetPrim().SetActive(False)
        else:
            UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    before = stage.GetSessionLayer().ExportToString()
    with pytest.raises(ValueError):
        JawMirror(stage)
    assert stage.GetSessionLayer().ExportToString() == before
