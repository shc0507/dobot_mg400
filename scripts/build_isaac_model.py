#!/usr/bin/env python3
"""Build portable Isaac Sim USD assets from the project's existing CAD model.

Run: uv run --group usd python scripts/build_isaac_model.py

No Isaac Sim installation is needed for export. Geometry, frames and inertias
come from dynamics.xml; its MuJoCo actuator/loop model is not translated.
"""

import argparse
import math
import shutil
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics, UsdShade, Vt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/mg400_agent/assets/mg400"
JAW_TRAVEL = 0.005


def numbers(element, name, default="0 0 0"):
    return tuple(map(float, element.get(name, default).split()))


def transform(element):
    """MJCF local XYZ Euler rotation, returned in USD's row-vector convention."""
    result = Gf.Matrix4d(1)
    for axis, angle in zip(reversed(np.eye(3)), reversed(numbers(element, "euler"))):
        result *= Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(*axis), math.degrees(angle)))
    result.SetTranslateOnly(Gf.Vec3d(*numbers(element, "pos")))
    return result


def add_transform(prim, matrix):
    UsdGeom.Xformable(prim).AddTransformOp().Set(matrix)


def material(stage, color):
    name = "color_" + "_".join(str(round(value * 100000)) for value in color)
    material = UsdShade.Material.Define(stage, f"/MG400/materials/{name}")
    shader = UsdShade.Shader.Define(stage, material.GetPath().AppendChild("surface"))
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color[:3]))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(color[3])
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return material


def add_geometry(stage, body_path, geom, mesh_files):
    path = body_path.AppendChild(geom.get("name"))
    if geom.get("type") == "mesh":
        data = mesh_files[geom.get("mesh")].read_bytes()
        count = struct.unpack_from("<I", data, 80)[0]
        records = np.frombuffer(
            data,
            dtype=[("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("extra", "<u2")],
            count=count,
            offset=84,
        )
        points = records["vertices"].reshape(-1, 3).copy()
        shape = UsdGeom.Mesh.Define(stage, path)
        shape.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points))
        shape.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(count, 3, dtype=np.int32)))
        shape.CreateFaceVertexIndicesAttr(
            Vt.IntArray.FromNumpy(np.arange(count * 3, dtype=np.int32))
        )
        shape.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(records["normal"].copy()))
        shape.SetNormalsInterpolation(UsdGeom.Tokens.uniform)
        shape.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        shape.CreateExtentAttr(
            [Gf.Vec3f(*points.min(axis=0).tolist()), Gf.Vec3f(*points.max(axis=0).tolist())]
        )
        add_transform(shape.GetPrim(), transform(geom))
    elif geom.get("type") == "box":
        shape = UsdGeom.Cube.Define(stage, path)
        shape.CreateSizeAttr(2)
        matrix = Gf.Matrix4d().SetScale(Gf.Vec3d(*numbers(geom, "size"))) * transform(geom)
        add_transform(shape.GetPrim(), matrix)
    else:
        raise ValueError(f"Unsupported robot geometry: {geom.attrib}")
    color = numbers(geom, "rgba", "1 1 1 1")
    shape.CreateDisplayColorAttr([Gf.Vec3f(*color[:3])])
    UsdShade.MaterialBindingAPI.Apply(shape.GetPrim()).Bind(material(stage, color))


def add_mass(prim, inertial):
    mass = UsdPhysics.MassAPI.Apply(prim)
    mass.CreateMassAttr(float(inertial.get("mass")))
    mass.CreateCenterOfMassAttr(Gf.Vec3f(*numbers(inertial, "pos")))
    xx, yy, zz, xy, xz, yz = numbers(inertial, "fullinertia")
    values, axes = np.linalg.eigh([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]])
    if np.linalg.det(axes) < 0:
        axes[:, 0] *= -1
    rotation = Gf.Matrix3d(*axes.T.ravel()).ExtractRotation().GetQuat()
    mass.CreateDiagonalInertiaAttr(Gf.Vec3f(*values))
    mass.CreatePrincipalAxesAttr(Gf.Quatf(rotation))


def add_joint(stage, body, parent_path, child_path):
    source_joint = body.find("joint")
    name = source_joint.get("name") if source_joint is not None else f"fixed_{body.get('name')}"
    path = f"/MG400/joints/{name}"
    if source_joint is None:
        joint = UsdPhysics.FixedJoint.Define(stage, path)
    else:
        joint = UsdPhysics.RevoluteJoint.Define(stage, path)
        axis = numbers(source_joint, "axis")
        joint.CreateAxisAttr({(1, 0, 0): "X", (0, 1, 0): "Y", (0, 0, 1): "Z"}[axis])
        low, high = numbers(source_joint, "range")
        joint.CreateLowerLimitAttr(math.degrees(low))
        joint.CreateUpperLimitAttr(math.degrees(high))
        # Holding gains only: the runtime assigns all eight coupled coordinates.
        drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "angular")
        drive.CreateTypeAttr("acceleration")
        drive.CreateStiffnessAttr(1000)
        drive.CreateDampingAttr(100)
        drive.CreateTargetPositionAttr(0)
        drive.CreateTargetVelocityAttr(0)
    if parent_path is not None:
        joint.CreateBody0Rel().SetTargets([parent_path])
    joint.CreateBody1Rel().SetTargets([child_path])
    local = transform(body)
    joint.CreateLocalPos0Attr(Gf.Vec3f(local.ExtractTranslation()))
    joint.CreateLocalRot0Attr(Gf.Quatf(local.ExtractRotationQuat()))
    joint.CreateLocalPos1Attr(Gf.Vec3f(0))
    joint.CreateLocalRot1Attr(Gf.Quatf(1))
    joint.CreateCollisionEnabledAttr(False)


def add_gripper(stage, document, mesh_files):
    """Add separate jaws and joint frames for measured-position display."""
    robot = Sdf.Path("/MG400")
    flange = robot.AppendChild("mg400_end_effector_flange")
    mount_path = flange.AppendChild("pgea_gripper")
    mount = UsdGeom.Xformable(stage.GetPrimAtPath(mount_path)).GetLocalTransformation()
    world = UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath(mount_path))
    housing_path = robot.AppendChild("pgea_housing")
    housing = UsdGeom.Xform.Define(stage, housing_path).GetPrim()
    add_transform(housing, world)
    fixed = UsdPhysics.FixedJoint.Define(stage, "/MG400/joints/pgea_housing_mount")
    fixed.CreateBody0Rel().SetTargets([flange])
    fixed.CreateBody1Rel().SetTargets([housing_path])
    fixed.CreateLocalPos0Attr(Gf.Vec3f(mount.ExtractTranslation()))
    fixed.CreateLocalRot0Attr(Gf.Quatf(mount.ExtractRotationQuat()))
    fixed.CreateLocalPos1Attr(Gf.Vec3f(0))
    fixed.CreateLocalRot1Attr(Gf.Quatf(1))
    fixed.CreateCollisionEnabledAttr(False)
    # The mirror reads these frames. PhysX must not attach the flange to a non-rigid node.
    fixed.CreateJointEnabledAttr(False)

    for side in ("left", "right"):
        body_path = robot.AppendChild(f"pgea_{side}_jaw")
        body = UsdGeom.Xform.Define(stage, body_path).GetPrim()
        add_transform(body, world)
        for geom_name in (f"visual_pgea_jaw_{side}", f"pgea_fingertip_{side}"):
            stage.RemovePrim(mount_path.AppendChild(geom_name))
            geom = document.find(f".//geom[@name='{geom_name}']")
            add_geometry(stage, body_path, geom, mesh_files)
        joint = UsdPhysics.PrismaticJoint.Define(stage, f"/MG400/joints/pgea_{side}_slide")
        joint.CreateBody0Rel().SetTargets([housing_path])
        joint.CreateBody1Rel().SetTargets([body_path])
        joint.CreateAxisAttr("Y")
        joint.CreateLowerLimitAttr(0)
        joint.CreateUpperLimitAttr(JAW_TRAVEL)
        orientation = Gf.Quatf(1) if side == "left" else Gf.Quatf(0, Gf.Vec3f(0, 0, 1))
        joint.CreateLocalPos0Attr(Gf.Vec3f(0))
        joint.CreateLocalRot0Attr(orientation)
        joint.CreateLocalPos1Attr(Gf.Vec3f(0))
        joint.CreateLocalRot1Attr(orientation)
        joint.CreateCollisionEnabledAttr(False)
        joint.CreateJointEnabledAttr(False)


def build_robot(source, output):
    document = ET.parse(source / "dynamics.xml").getroot()
    mesh_files = {
        mesh.get("name"): source / "meshes" / mesh.get("file")
        for mesh in document.findall("asset/mesh")
    }
    stage = Usd.Stage.CreateNew(str(output / "mg400_with_gripper.usd"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, "/MG400").GetPrim()
    stage.SetDefaultPrim(root)
    UsdPhysics.ArticulationRootAPI.Apply(root)
    root.SetDocumentation(
        "MG400 CAD articulation with movable visual PGEA jaws. Metres, Z up. "
        "No contacts, payload inertia, jaw actuation or physical closed-loop constraints. "
        "Drive all eight joint coordinates using the four-axis controller mapping. See NOTICE.txt."
    )
    UsdGeom.Scope.Define(stage, "/MG400/joints")
    UsdGeom.Scope.Define(stage, "/MG400/materials")

    def add_body(body, parent_path=None, parent_world=Gf.Matrix4d(1)):
        local = transform(body)
        world = local * parent_world
        inertial = body.find("inertial")
        # Physics bodies are siblings; non-rigid gripper and frame nodes inherit
        # their link's transform without introducing another mass or joint.
        path = Sdf.Path("/MG400").AppendChild(body.get("name"))
        if inertial is None:
            path = parent_path.AppendChild(body.get("name"))
        prim = UsdGeom.Xform.Define(stage, path).GetPrim()
        add_transform(prim, world if inertial is not None else local)
        if inertial is not None:
            UsdPhysics.RigidBodyAPI.Apply(prim)
            add_mass(prim, inertial)
            # usd-core has no PhysxSchema plugin; preserve the schema and its
            # native attribute for Isaac Sim to resolve when loading the file.
            prim.AddAppliedSchema("PhysxRigidBodyAPI")
            prim.CreateAttribute("physxRigidBody:disableGravity", Sdf.ValueTypeNames.Bool).Set(True)
            add_joint(stage, body, parent_path, path)
        for geom in body.findall("geom"):
            add_geometry(stage, path, geom, mesh_files)
        for site in body.findall("site"):
            frame = UsdGeom.Xform.Define(stage, path.AppendChild(site.get("name")))
            add_transform(frame.GetPrim(), transform(site))
        for child in body.findall("body"):
            add_body(child, path, world)

    add_body(document.find("worldbody/body"))
    add_gripper(stage, document, mesh_files)
    stage.GetRootLayer().Save()
    return document


def build_scene(document, output):
    stage = Usd.Stage.CreateNew(str(output / "scene.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(600)
    stage.SetTimeCodesPerSecond(60)
    stage.SetFramesPerSecond(60)
    world = UsdGeom.Xform.Define(stage, "/World").GetPrim()
    stage.SetDefaultPrim(world)
    robot = UsdGeom.Xform.Define(stage, "/World/MG400").GetPrim()
    robot.GetReferences().AddReference("./mg400_with_gripper.usd")
    physics = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    physics.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    physics.CreateGravityMagnitudeAttr(9.81)
    ground = UsdGeom.Cube.Define(stage, "/World/Ground")
    ground.CreateSizeAttr(2)
    ground.CreateDisplayColorAttr([Gf.Vec3f(0.29, 0.32, 0.35)])
    ground.AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.012))
    ground.AddScaleOp().Set(Gf.Vec3f(1, 1, 0.01))
    dome = UsdLux.DomeLight.Define(stage, "/World/DomeLight")
    dome.CreateIntensityAttr(700)
    key = UsdLux.DistantLight.Define(stage, "/World/KeyLight")
    key.CreateIntensityAttr(1800)
    key.CreateAngleAttr(1)
    key.AddRotateXYZOp().Set(Gf.Vec3f(25, -35, -25))
    source_camera = document.find("worldbody/camera")
    camera = UsdGeom.Camera.Define(stage, "/World/Camera")
    x, y = np.array(numbers(source_camera, "xyaxes")).reshape(2, 3)
    rotation = np.stack((x, y, np.cross(x, y)))
    matrix = Gf.Matrix4d(Gf.Matrix3d(*rotation.ravel()), Gf.Vec3d(*numbers(source_camera, "pos")))
    add_transform(camera.GetPrim(), matrix)
    aperture = 2 * 24 * math.tan(math.radians(float(source_camera.get("fovy"))) / 2)
    camera.CreateFocalLengthAttr(24)
    camera.CreateVerticalApertureAttr(aperture)
    camera.CreateHorizontalApertureAttr(aperture * 16 / 9)
    camera.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100))
    stage.GetRootLayer().Save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / "isaacsim")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    document = build_robot(args.source, args.output)
    build_scene(document, args.output)
    shutil.copy2(args.source / "LICENSE", args.output / "LICENSE")
    notice = (args.source / "NOTICE.txt").read_text()
    notice += (
        "\nIsaac Sim USD export (2026-10-13):\n"
        "  Rebuilt by scripts/build_isaac_model.py from dynamics.xml and its STL meshes.\n"
        "  Self-contained triangle meshes, source colors and exact CAD frames retained.\n"
        "  Eleven explicit rigid-body masses/inertias, eight revolute joints, fixed base.\n"
        "  Full tensors diagonalized to USD principal axes; no added gripper payload.\n"
        "  Per-body gravity disabled; contacts and physical linkage constraints omitted.\n"
        "  Acceleration drives (stiffness 1000, damping 100) hold the initial zero pose.\n"
        "  Gains are visualization settings, not calibrated motor dynamics.\n"
        "  The four controller axes must drive all eight joints using the mapping above.\n"
        "  USD angular joint limits and drive targets are degrees; Isaac articulation\n"
        "  runtime joint APIs use radians. Gripper and fingertips remain visual only.\n"
        "  Separate jaw transforms contain the two jaw carriers and estimated fingertips.\n"
        "  The builder removes the original fixed jaw instances. "
        "The housing frame follows the flange.\n"
        "  Two opposed prismatic joints define 0 to 0.005 m inward travel per jaw.\n"
        "  Zero travel preserves the open CAD pose and reference frames.\n"
        "  The builder disables housing and jaw joints in physics. "
        "Mirror code reads their frames.\n"
        "  The jaws have no rigid bodies, masses, force drives, "
        "collision geometry or contact materials.\n"
        "  The manufacturer specifies 10 mm total stroke for PGEA-2-10: https://en.dh-robotics.com/product/pgea\n"
        "  The estimated fingertips have an inner gap of 0.0119 m when open.\n"
        "  The gap is 0.0019 m at full inward travel. "
        "These are model dimensions, not hardware measurements.\n"
        "  workspace_scene.usda contains the robot, ground, lights and camera.\n"
        "  Its physics scene is inactive. Keep the physics timeline stopped.\n"
        "  These assets do not validate gripping performance or robot dynamics.\n"
    )
    (args.output / "NOTICE.txt").write_text(notice)
    print(f"Wrote mg400_with_gripper.usd, scene.usda, LICENSE and NOTICE.txt to {args.output}")


if __name__ == "__main__":
    main()
