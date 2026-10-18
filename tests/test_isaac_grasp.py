"""Verify the additive physical-gripper asset without an Isaac installation."""

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROBOT = "/MG400"
MOUNT = ROBOT + "/mg400_end_effector_flange/pgea_gripper"


@pytest.fixture
def stage():
    return Usd.Stage.Open(str(ROOT / "isaacsim/mg400_grasp.usda"))


def frame(joint, index):
    transform = Gf.Matrix4d().SetRotate(
        Gf.Rotation(joint.GetAttribute(f"physics:localRot{index}").Get())
    )
    transform.SetTranslateOnly(Gf.Vec3d(joint.GetAttribute(f"physics:localPos{index}").Get()))
    return np.array(transform).T


def test_gripper_is_additive_with_two_jaw_dofs_and_fixed_housing(stage):
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path(ROBOT)
    assert stage.GetRootLayer().GetExternalReferences() == ("./mg400_with_gripper.usd",)
    root = stage.GetDefaultPrim()
    assert root.GetAttribute("physxArticulation:enabledSelfCollisions").Get() is False
    assert root.GetAttribute("physxArticulation:solverPositionIterationCount").Get() == 32
    assert root.GetAttribute("physxArticulation:solverVelocityIterationCount").Get() == 8
    assert sum(p.HasAPI(UsdPhysics.RigidBodyAPI) for p in stage.Traverse()) == 14
    assert sum(p.IsA(UsdPhysics.RevoluteJoint) for p in stage.Traverse()) == 8
    assert sum(p.IsA(UsdPhysics.PrismaticJoint) for p in stage.Traverse()) == 2
    assert sum(p.IsA(UsdPhysics.FixedJoint) for p in stage.Traverse()) == 4
    total_added_mass = 0
    for name, expected_mass in (("housing", 0.13), ("left_jaw", 0.01), ("right_jaw", 0.01)):
        prim = stage.GetPrimAtPath(f"{ROBOT}/pgea_{name}")
        assert prim.GetParent() == root
        assert prim.GetAttribute("physxRigidBody:disableGravity").Get() is False
        mass = UsdPhysics.MassAPI(prim)
        assert mass.GetMassAttr().Get() == pytest.approx(expected_mass)
        assert min(mass.GetDiagonalInertiaAttr().Get()) > 0
        total_added_mass += mass.GetMassAttr().Get()
    assert total_added_mass == pytest.approx(0.15)
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
        drive = UsdPhysics.DriveAPI(joint.GetPrim(), "linear")
        assert drive.GetTypeAttr().Get() == "force"
        assert drive.GetMaxForceAttr().Get() == 2
        assert drive.GetStiffnessAttr().Get() == 1000
        assert drive.GetDampingAttr().Get() == 10
        assert drive.GetTargetPositionAttr().Get() == 0


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


def test_only_fingertips_collide_and_source_visuals_have_no_duplicates(stage):
    collision_paths = {
        str(p.GetPath()) for p in stage.Traverse() if p.HasAPI(UsdPhysics.CollisionAPI)
    }
    assert collision_paths == {
        f"{ROBOT}/pgea_{side}_jaw/pgea_fingertip_{side}" for side in ("left", "right")
    }
    for side in ("left", "right"):
        for name in (f"visual_pgea_jaw_{side}", f"pgea_fingertip_{side}"):
            assert not stage.GetPrimAtPath(MOUNT + "/" + name).IsActive()
            assert stage.GetPrimAtPath(f"{ROBOT}/pgea_{side}_jaw/{name}").IsActive()
        finger = stage.GetPrimAtPath(f"{ROBOT}/pgea_{side}_jaw/pgea_fingertip_{side}")
        assert finger.GetAttribute("physxCollision:contactOffset").Get() == pytest.approx(0.0002)
        assert finger.GetAttribute("physxCollision:restOffset").Get() == 0
        material, _ = UsdShade.MaterialBindingAPI(finger).ComputeBoundMaterial(
            materialPurpose="physics"
        )
        physics = UsdPhysics.MaterialAPI(material.GetPrim())
        assert physics.GetStaticFrictionAttr().Get() == 1
        assert physics.GetDynamicFrictionAttr().Get() == 1
        assert physics.GetRestitutionAttr().Get() == 0
    assert sum(p.IsA(UsdGeom.Mesh) for p in stage.Traverse()) == 19


def test_grasp_scene_replaces_reference_and_adds_ground_collision():
    stage = Usd.Stage.Open(str(ROOT / "isaacsim/grasp_scene.usda"))
    assert stage.GetPrimAtPath("/World/Ground").HasAPI(UsdPhysics.CollisionAPI)
    assert stage.GetPrimAtPath("/World/Camera").IsA(UsdGeom.Camera)
    assert stage.GetPrimAtPath("/World/MG400/pgea_housing")
    assert sum(p.IsA(UsdPhysics.PrismaticJoint) for p in stage.Traverse()) == 2
    assert stage.GetTimeCodesPerSecond() == stage.GetFramesPerSecond() == 60
