"""Verify the exported USD against the existing CAD model without Isaac Sim."""

import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/mg400_agent/assets/mg400"
ASSET = ROOT / "isaacsim/mg400_with_gripper.usd"
DOCUMENT = ET.parse(SOURCE / "dynamics.xml").getroot()


@pytest.fixture
def stage():
    stage = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(ASSET)), Sdf.Layer.CreateAnonymous())
    stage.SetEditTarget(stage.GetSessionLayer())
    return stage


def rotation(axis, degrees):
    x, y, z = axis
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    angle = math.radians(degrees)
    matrix = np.eye(4)
    matrix[:3, :3] = np.eye(3) + math.sin(angle) * skew + (1 - math.cos(angle)) * skew @ skew
    return matrix


def xml_pose(element):
    matrix = np.eye(4)
    for axis, angle in zip(np.eye(3), map(float, element.get("euler", "0 0 0").split())):
        matrix = matrix @ rotation(axis, math.degrees(angle))
    matrix[:3, 3] = list(map(float, element.get("pos", "0 0 0").split()))
    return matrix


def joint_frame(joint, index):
    quat = joint.GetPrim().GetAttribute(f"physics:localRot{index}").Get()
    matrix = Gf.Matrix4d().SetRotate(Gf.Rotation(quat))
    matrix.SetTranslateOnly(
        Gf.Vec3d(joint.GetPrim().GetAttribute(f"physics:localPos{index}").Get())
    )
    return np.array(matrix).T


def test_articulation_topology_mass_and_drives(stage):
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/MG400")
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1
    assert UsdGeom.GetStageUpAxis(stage) == "Z"
    assert stage.GetDefaultPrim().HasAPI(UsdPhysics.ArticulationRootAPI)
    assert not stage.GetRootLayer().GetExternalReferences()
    bodies = [prim for prim in stage.Traverse() if prim.HasAPI(UsdPhysics.RigidBodyAPI)]
    assert len(bodies) == 11
    assert all(prim.GetParent() == stage.GetDefaultPrim() for prim in bodies)
    for prim in bodies:
        assert prim.GetAttribute("physxRigidBody:disableGravity").Get() is True
        source = DOCUMENT.find(f".//body[@name='{prim.GetName()}']/inertial")
        mass = UsdPhysics.MassAPI(prim)
        assert mass.GetMassAttr().Get() == pytest.approx(float(source.get("mass")))
        assert mass.GetCenterOfMassAttr().Get() == pytest.approx(
            list(map(float, source.get("pos").split())), abs=1e-8
        )
        xx, yy, zz, xy, xz, yz = map(float, source.get("fullinertia").split())
        expected = np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]])
        quaternion = Gf.Quatd(mass.GetPrincipalAxesAttr().Get()).GetNormalized()
        axes = np.array(Gf.Matrix3d(quaternion)).T
        tensor = axes @ np.diag(mass.GetDiagonalInertiaAttr().Get()) @ axes.T
        assert tensor == pytest.approx(expected, abs=3e-9)
    joints = [
        UsdPhysics.RevoluteJoint(prim)
        for prim in stage.Traverse()
        if prim.IsA(UsdPhysics.RevoluteJoint)
    ]
    assert len(joints) == 8
    assert sum(prim.IsA(UsdPhysics.FixedJoint) for prim in stage.Traverse()) == 3
    for joint in joints:
        source = DOCUMENT.find(f".//joint[@name='{joint.GetPrim().GetName()}']")
        limits = [math.degrees(float(value)) for value in source.get("range").split()]
        assert joint.GetLowerLimitAttr().Get() == pytest.approx(limits[0])
        assert joint.GetUpperLimitAttr().Get() == pytest.approx(limits[1])
        drive = UsdPhysics.DriveAPI(joint, "angular")
        assert drive.GetTargetPositionAttr().Get() == 0
        assert drive.GetTypeAttr().Get() == "acceleration"
        assert drive.GetStiffnessAttr().Get() > 0
        assert drive.GetDampingAttr().Get() > 0
    assert not any(prim.HasAPI(UsdPhysics.CollisionAPI) for prim in stage.Traverse())


def test_all_mesh_triangles_and_fingers_match_source(stage):
    meshes = [UsdGeom.Mesh(prim) for prim in stage.Traverse() if prim.IsA(UsdGeom.Mesh)]
    assert len(meshes) == 19
    for mesh in meshes:
        name = mesh.GetPrim().GetName().removeprefix("visual_")
        data = (SOURCE / "meshes" / f"{name}.stl").read_bytes()
        count = struct.unpack_from("<I", data, 80)[0]
        records = np.frombuffer(
            data,
            dtype=[("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("extra", "<u2")],
            count=count,
            offset=84,
        )
        expected = records["vertices"].reshape(-1, 3)
        assert np.array_equal(mesh.GetPointsAttr().Get(), expected)
        assert list(mesh.GetFaceVertexCountsAttr().Get()) == [3] * count
        assert list(mesh.GetFaceVertexIndicesAttr().Get()) == list(range(count * 3))
        assert mesh.GetExtentAttr().Get() == pytest.approx(
            np.stack((expected.min(axis=0), expected.max(axis=0)))
        )
        source = DOCUMENT.find(f".//geom[@name='{mesh.GetPrim().GetName()}']")
        assert mesh.GetDisplayColorAttr().Get()[0] == pytest.approx(
            list(map(float, source.get("rgba").split()))[:3]
        )
    fingers = [UsdGeom.Cube(prim) for prim in stage.Traverse() if prim.IsA(UsdGeom.Cube)]
    assert len(fingers) == 2
    for finger in fingers:
        source = DOCUMENT.find(f".//geom[@name='{finger.GetPrim().GetName()}']")
        local = np.array(UsdGeom.Xformable(finger).GetLocalTransformation()).T
        assert np.diag(local)[:3] == pytest.approx(list(map(float, source.get("size").split())))
        assert local[:3, 3] == pytest.approx(list(map(float, source.get("pos").split())))


@pytest.mark.parametrize(
    "angles",
    [
        (0, 0, 0, 0),
        (90, 30, 70, -45),
        (-80, -15, -20, 55),
        (150, 80, 100, -140),
        (-150, 50, -5, 140),
    ],
)
def test_joint_frames_reproduce_cad_chain_and_gripper_mount(stage, angles):
    q1, q2, q3, q4 = angles
    degrees = dict(
        zip(
            (
                "mg400_j1",
                "mg400_j2_1",
                "mg400_j2_2",
                "mg400_j3_1",
                "mg400_j3_2",
                "mg400_j4_1",
                "mg400_j4_2",
                "mg400_j5",
            ),
            (q1, q2, q2, q3 - q2, -q2, -q3, q3, q4),
            strict=True,
        )
    )
    poses = {}
    pending = [UsdPhysics.Joint(prim) for prim in stage.Traverse() if prim.IsA(UsdPhysics.Joint)]
    while pending:
        progressed = False
        for joint in pending[:]:
            parents = joint.GetBody0Rel().GetTargets()
            if parents and parents[0] not in poses:
                continue
            parent = poses[parents[0]] if parents else np.eye(4)
            motion = np.eye(4)
            if joint.GetPrim().IsA(UsdPhysics.RevoluteJoint):
                axis = UsdPhysics.RevoluteJoint(joint).GetAxisAttr().Get()
                motion = rotation(np.eye(3)["XYZ".index(axis)], degrees[joint.GetPrim().GetName()])
            pose = parent @ joint_frame(joint, 0) @ motion @ np.linalg.inv(joint_frame(joint, 1))
            child = joint.GetBody1Rel().GetTargets()[0]
            poses[child] = pose
            if angles == (0, 0, 0, 0):
                authored = UsdGeom.Xformable(stage.GetPrimAtPath(child)).GetLocalTransformation()
                assert np.array(authored).T == pytest.approx(pose, abs=2e-8)
            UsdGeom.Xformable(stage.GetPrimAtPath(child)).GetOrderedXformOps()[0].Set(
                Gf.Matrix4d(*pose.T.ravel())
            )
            pending.remove(joint)
            progressed = True
        assert progressed, "USD joint topology contains an orphan or loop"
    cache = UsdGeom.XformCache()

    def compare(body, parent_pose=np.eye(4), parent_path=None):
        pose = xml_pose(body)
        joint = body.find("joint")
        if joint is not None:
            pose = pose @ rotation(
                tuple(map(float, joint.get("axis").split())), degrees[joint.get("name")]
            )
        pose = parent_pose @ pose
        path = Sdf.Path("/MG400") if body.find("inertial") is not None else parent_path
        path = path.AppendChild(body.get("name"))
        actual = np.array(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(path))).T
        assert actual == pytest.approx(pose, abs=2e-8)
        for site in body.findall("site"):
            frame = stage.GetPrimAtPath(path.AppendChild(site.get("name")))
            assert np.array(cache.GetLocalToWorldTransform(frame)).T == pytest.approx(
                pose @ xml_pose(site), abs=2e-8
            )
        for child in body.findall("body"):
            compare(child, pose, path)

    compare(DOCUMENT.find("worldbody/body"))


def test_scene_resolves_self_contained_robot_and_reference_frames():
    stage = Usd.Stage.Open(str(ROOT / "isaacsim/scene.usda"))
    assert stage.GetStartTimeCode() == 0
    assert stage.GetEndTimeCode() == 600
    assert stage.GetTimeCodesPerSecond() == stage.GetFramesPerSecond() == 60
    assert stage.GetPrimAtPath("/World/MG400/mg400_base_link")
    assert stage.GetPrimAtPath("/World/Camera").IsA(UsdGeom.Camera)
    cache = UsdGeom.XformCache()
    flange = "/World/MG400/mg400_end_effector_flange"
    expected = np.array([0.2784487429828484, -0.0000001243781102, 0.353251164344716])
    for suffix, offset in (("/mg400_tcp", 0), ("/pgea_gripper/pgea_tip", -0.104)):
        matrix = cache.GetLocalToWorldTransform(stage.GetPrimAtPath(flange + suffix))
        assert matrix.ExtractTranslation() == pytest.approx(expected + (0, 0, offset), abs=1e-12)
    bounds = (
        UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default"])
        .ComputeWorldBound(stage.GetPrimAtPath("/World/MG400"))
        .ComputeAlignedRange()
    )
    assert max(bounds.GetSize()) < 0.7
    assert min(bounds.GetSize()) > 0.1
