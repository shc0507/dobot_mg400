"""Check the movable gripper display without an Isaac installation."""

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROBOT = "/MG400"
MOUNT = ROBOT + "/mg400_end_effector_flange/pgea_gripper"


@pytest.fixture
def stage():
    return Usd.Stage.Open(str(ROOT / "isaacsim/mg400_mirror.usda"))


def frame(joint, index):
    transform = Gf.Matrix4d().SetRotate(
        Gf.Rotation(joint.GetAttribute(f"physics:localRot{index}").Get())
    )
    transform.SetTranslateOnly(Gf.Vec3d(joint.GetAttribute(f"physics:localPos{index}").Get()))
    return np.array(transform).T


def test_gripper_has_two_jaw_coordinates_and_fixed_housing(stage):
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path(ROBOT)
    assert stage.GetRootLayer().GetExternalReferences() == ("./mg400_with_gripper.usd",)
    root = stage.GetDefaultPrim()
    assert sum(p.HasAPI(UsdPhysics.RigidBodyAPI) for p in stage.Traverse()) == 11
    assert sum(p.IsA(UsdPhysics.RevoluteJoint) for p in stage.Traverse()) == 8
    assert sum(p.IsA(UsdPhysics.PrismaticJoint) for p in stage.Traverse()) == 2
    assert sum(p.IsA(UsdPhysics.FixedJoint) for p in stage.Traverse()) == 4
    for name in ("housing", "left_jaw", "right_jaw"):
        prim = stage.GetPrimAtPath(f"{ROBOT}/pgea_{name}")
        assert prim.GetParent() == root
        assert not prim.HasAPI(UsdPhysics.RigidBodyAPI)
        assert not prim.HasAPI(UsdPhysics.MassAPI)
    mount = UsdPhysics.FixedJoint(stage.GetPrimAtPath(f"{ROBOT}/joints/pgea_housing_mount"))
    assert mount.GetBody0Rel().GetTargets() == [Sdf.Path(f"{ROBOT}/mg400_end_effector_flange")]
    assert mount.GetBody1Rel().GetTargets() == [Sdf.Path(f"{ROBOT}/pgea_housing")]
    for side in ("left", "right"):
        joint = UsdPhysics.PrismaticJoint(stage.GetPrimAtPath(f"{ROBOT}/joints/pgea_{side}_slide"))
        assert joint.GetBody0Rel().GetTargets() == [Sdf.Path(f"{ROBOT}/pgea_housing")]
        assert joint.GetBody1Rel().GetTargets() == [Sdf.Path(f"{ROBOT}/pgea_{side}_jaw")]
        assert joint.GetAxisAttr().Get() == "Y"
        assert joint.GetLowerLimitAttr().Get() == 0
        assert joint.GetUpperLimitAttr().Get() == pytest.approx(0.005)
        assert not joint.GetPrim().HasAPI(UsdPhysics.DriveAPI, "linear")


@pytest.mark.parametrize("closure", (0, 0.0025, 0.005))
def test_opposed_slide_frames_preserve_mount_and_make_expected_gap(stage, closure):
    cache = UsdGeom.XformCache()
    mount = np.array(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(MOUNT))).T
    housing = np.array(
        cache.GetLocalToWorldTransform(stage.GetPrimAtPath(f"{ROBOT}/pgea_housing"))
    ).T
    assert housing == pytest.approx(mount, abs=1e-12)
    centers = []
    for side, direction in (("left", 1), ("right", -1)):
        joint = stage.GetPrimAtPath(f"{ROBOT}/joints/pgea_{side}_slide")
        motion = np.eye(4)
        motion[1, 3] = closure
        body = housing @ frame(joint, 0) @ motion @ np.linalg.inv(frame(joint, 1))
        expected = mount.copy()
        expected[:3, 3] += mount[:3, 1] * closure * direction
        assert body == pytest.approx(expected, abs=1e-12)
        geom = stage.GetPrimAtPath(f"{ROBOT}/pgea_{side}_jaw/pgea_fingertip_{side}")
        local = np.array(UsdGeom.Xformable(geom).GetLocalTransformation()).T
        centers.append((body @ local)[:3, 3])
        assert np.diag(local)[:3] == pytest.approx((0.005, 0.0045, 0.0075))
    gap = np.dot(centers[1] - centers[0], mount[:3, 1]) - 0.009
    assert gap == pytest.approx(0.0119 - 2 * closure, abs=1e-12)
    tip = cache.GetLocalToWorldTransform(stage.GetPrimAtPath(MOUNT + "/pgea_tip"))
    assert tip.ExtractTranslation() == pytest.approx((mount @ [0, 0, -0.104, 1])[:3])


def test_display_has_no_colliders_or_duplicate_source_visuals(stage):
    collision_paths = {
        str(p.GetPath()) for p in stage.Traverse() if p.HasAPI(UsdPhysics.CollisionAPI)
    }
    assert not collision_paths
    for side in ("left", "right"):
        for name in (f"visual_pgea_jaw_{side}", f"pgea_fingertip_{side}"):
            assert not stage.GetPrimAtPath(MOUNT + "/" + name).IsActive()
            assert stage.GetPrimAtPath(f"{ROBOT}/pgea_{side}_jaw/{name}").IsActive()
    assert sum(p.IsA(UsdGeom.Mesh) for p in stage.Traverse()) == 19
