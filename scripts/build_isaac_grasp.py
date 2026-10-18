#!/usr/bin/env python3
"""Add physical PGEA jaws as an overlay on the existing MG400 USD asset.

Run: uv run --group usd python scripts/build_isaac_grasp.py

The manufacturer specifies 10 mm total stroke, 0.15 kg mass and 2 N maximum
force per jaw for PGEA-2-10: https://en.dh-robotics.com/product/pgea .
The housing/jaw mass split, box inertias, friction and control gains are
simulation estimates. Existing 10 x 9 x 15 mm fingertips are photo estimates.
q=0 preserves the open CAD; q=.005 moves each jaw inward by 5 mm. The existing
finger shapes therefore have an inner gap of 11.9 mm open and 1.9 mm closed.
Arm linkage coupling still uses the existing controller-to-eight-joint mapping.
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from build_isaac_model import ROOT, SOURCE, add_geometry, add_transform
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

ROBOT = Sdf.Path("/MG400")
FLANGE = ROBOT.AppendChild("mg400_end_effector_flange")
MOUNT = FLANGE.AppendChild("pgea_gripper")
TRAVEL = 0.005
OPEN_GAP = 0.0119


def add_box_mass(prim, mass_kg, center, dimensions):
    """Use a documented uniform-box approximation, not a calibrated CAD inertia."""
    UsdPhysics.RigidBodyAPI.Apply(prim)
    prim.AddAppliedSchema("PhysxRigidBodyAPI")
    prim.CreateAttribute("physxRigidBody:disableGravity", Sdf.ValueTypeNames.Bool).Set(False)
    mass = UsdPhysics.MassAPI.Apply(prim)
    mass.CreateMassAttr(mass_kg)
    mass.CreateCenterOfMassAttr(Gf.Vec3f(*center))
    x, y, z = dimensions
    mass.CreateDiagonalInertiaAttr(
        Gf.Vec3f(
            mass_kg * (y * y + z * z) / 12,
            mass_kg * (x * x + z * z) / 12,
            mass_kg * (x * x + y * y) / 12,
        )
    )
    mass.CreatePrincipalAxesAttr(Gf.Quatf(1))


def add_collision(prim, material):
    UsdPhysics.CollisionAPI.Apply(prim)
    prim.AddAppliedSchema("PhysxCollisionAPI")
    prim.CreateAttribute("physxCollision:contactOffset", Sdf.ValueTypeNames.Float).Set(0.0002)
    prim.CreateAttribute("physxCollision:restOffset", Sdf.ValueTypeNames.Float).Set(0)
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, materialPurpose="physics")


def build_robot(source, output):
    document = ET.parse(source / "dynamics.xml").getroot()
    mesh_files = {
        mesh.get("name"): source / "meshes" / mesh.get("file")
        for mesh in document.findall("asset/mesh")
    }
    stage = Usd.Stage.CreateNew(str(output / "mg400_grasp.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, ROBOT).GetPrim()
    root.GetReferences().AddReference("./mg400_with_gripper.usd")
    stage.SetDefaultPrim(root)
    root.SetDocumentation(__doc__)
    root.AddAppliedSchema("PhysxArticulationAPI")
    for name, value, kind in (
        ("enabledSelfCollisions", False, Sdf.ValueTypeNames.Bool),
        ("solverPositionIterationCount", 32, Sdf.ValueTypeNames.Int),
        ("solverVelocityIterationCount", 8, Sdf.ValueTypeNames.Int),
    ):
        root.CreateAttribute(f"physxArticulation:{name}", kind).Set(value)

    material = UsdShade.Material.Define(stage, "/MG400/materials/grip_contact")
    physics_material = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    physics_material.CreateStaticFrictionAttr(1)
    physics_material.CreateDynamicFrictionAttr(1)
    physics_material.CreateRestitutionAttr(0)

    mount = UsdGeom.Xformable(stage.GetPrimAtPath(MOUNT)).GetLocalTransformation()
    world = UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath(MOUNT))
    housing_path = ROBOT.AppendChild("pgea_housing")
    housing = UsdGeom.Xform.Define(stage, housing_path).GetPrim()
    add_transform(housing, world)
    add_box_mass(housing, 0.13, (0, 0, -0.04225), (0.018, 0.030, 0.0845))
    fixed = UsdPhysics.FixedJoint.Define(stage, "/MG400/joints/pgea_housing_mount")
    fixed.CreateBody0Rel().SetTargets([FLANGE])
    fixed.CreateBody1Rel().SetTargets([housing_path])
    fixed.CreateLocalPos0Attr(Gf.Vec3f(mount.ExtractTranslation()))
    fixed.CreateLocalRot0Attr(Gf.Quatf(mount.ExtractRotationQuat()))
    fixed.CreateLocalPos1Attr(Gf.Vec3f(0))
    fixed.CreateLocalRot1Attr(Gf.Quatf(1))
    fixed.CreateCollisionEnabledAttr(False)

    for side, sign in (("left", -1), ("right", 1)):
        body_path = ROBOT.AppendChild(f"pgea_{side}_jaw")
        body = UsdGeom.Xform.Define(stage, body_path).GetPrim()
        add_transform(body, world)
        # The envelope includes the overlapping internal rack and estimated tip.
        add_box_mass(body, 0.01, (sign * 0.00025, sign * 0.0027, -0.08775), (0.011, 0.0266, 0.0325))
        for geom_name in (f"visual_pgea_jaw_{side}", f"pgea_fingertip_{side}"):
            stage.OverridePrim(MOUNT.AppendChild(geom_name)).SetActive(False)
            geom = document.find(f".//geom[@name='{geom_name}']")
            add_geometry(stage, body_path, geom, mesh_files)
        add_collision(
            stage.GetPrimAtPath(body_path.AppendChild(f"pgea_fingertip_{side}")), material
        )
        joint = UsdPhysics.PrismaticJoint.Define(stage, f"/MG400/joints/pgea_{side}_slide")
        joint.CreateBody0Rel().SetTargets([housing_path])
        joint.CreateBody1Rel().SetTargets([body_path])
        joint.CreateAxisAttr("Y")
        joint.CreateLowerLimitAttr(0)
        joint.CreateUpperLimitAttr(TRAVEL)
        orientation = Gf.Quatf(1) if side == "left" else Gf.Quatf(0, Gf.Vec3f(0, 0, 1))
        joint.CreateLocalPos0Attr(Gf.Vec3f(0))
        joint.CreateLocalRot0Attr(orientation)
        joint.CreateLocalPos1Attr(Gf.Vec3f(0))
        joint.CreateLocalRot1Attr(orientation)
        joint.CreateCollisionEnabledAttr(False)
        drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "linear")
        drive.CreateTypeAttr("force")
        drive.CreateMaxForceAttr(2)
        drive.CreateStiffnessAttr(1000)
        drive.CreateDampingAttr(10)
        drive.CreateTargetPositionAttr(0)
        drive.CreateTargetVelocityAttr(0)
    stage.GetRootLayer().Save()


def build_scene(output):
    stage = Usd.Stage.CreateNew(str(output / "grasp_scene.usda"))
    stage.GetRootLayer().subLayerPaths = ["./scene.usda"]
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(3600)
    stage.SetTimeCodesPerSecond(60)
    stage.SetFramesPerSecond(60)
    robot = stage.OverridePrim("/World/MG400")
    robot.GetReferences().SetReferences([Sdf.Reference("./mg400_grasp.usda")])
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Ground"))
    stage.GetRootLayer().Save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / "isaacsim")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not (args.output / "mg400_with_gripper.usd").is_file():
        parser.error("Build the base asset with scripts/build_isaac_model.py first")
    build_robot(args.source, args.output)
    build_scene(args.output)
    (args.output / "GRASP_NOTICE.txt").write_bytes(
        (ROOT / "isaacsim/GRASP_NOTICE.txt").read_bytes()
    )
    print(f"Wrote mg400_grasp.usda, grasp_scene.usda and GRASP_NOTICE.txt to {args.output}")


if __name__ == "__main__":
    main()
