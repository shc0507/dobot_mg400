#!/usr/bin/env python3
"""Add movable PGEA jaws for measured-position display in the MG400 USD asset.

Run: uv run --group usd python scripts/build_isaac_mirror.py

Joint frames define each jaw's movement for the mirror code. This layer adds
no rigid bodies, masses, drives or collision geometry. Zero travel preserves
the open CAD pose. Each jaw moves inward by up to 5 mm.
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from build_isaac_model import ROOT, SOURCE, add_geometry, add_transform
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

ROBOT = Sdf.Path("/MG400")
FLANGE = ROBOT.AppendChild("mg400_end_effector_flange")
MOUNT = FLANGE.AppendChild("pgea_gripper")
TRAVEL = 0.005


def build_robot(source, output):
    document = ET.parse(source / "dynamics.xml").getroot()
    mesh_files = {
        mesh.get("name"): source / "meshes" / mesh.get("file")
        for mesh in document.findall("asset/mesh")
    }
    stage = Usd.Stage.CreateNew(str(output / "mg400_mirror.usda"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, ROBOT).GetPrim()
    root.GetReferences().AddReference("./mg400_with_gripper.usd")
    stage.SetDefaultPrim(root)
    root.SetDocumentation(__doc__)
    mount = UsdGeom.Xformable(stage.GetPrimAtPath(MOUNT)).GetLocalTransformation()
    world = UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath(MOUNT))
    housing_path = ROBOT.AppendChild("pgea_housing")
    housing = UsdGeom.Xform.Define(stage, housing_path).GetPrim()
    add_transform(housing, world)
    fixed = UsdPhysics.FixedJoint.Define(stage, "/MG400/joints/pgea_housing_mount")
    fixed.CreateBody0Rel().SetTargets([FLANGE])
    fixed.CreateBody1Rel().SetTargets([housing_path])
    fixed.CreateLocalPos0Attr(Gf.Vec3f(mount.ExtractTranslation()))
    fixed.CreateLocalRot0Attr(Gf.Quatf(mount.ExtractRotationQuat()))
    fixed.CreateLocalPos1Attr(Gf.Vec3f(0))
    fixed.CreateLocalRot1Attr(Gf.Quatf(1))
    fixed.CreateCollisionEnabledAttr(False)

    for side in ("left", "right"):
        body_path = ROBOT.AppendChild(f"pgea_{side}_jaw")
        body = UsdGeom.Xform.Define(stage, body_path).GetPrim()
        add_transform(body, world)
        for geom_name in (f"visual_pgea_jaw_{side}", f"pgea_fingertip_{side}"):
            stage.OverridePrim(MOUNT.AppendChild(geom_name)).SetActive(False)
            geom = document.find(f".//geom[@name='{geom_name}']")
            add_geometry(stage, body_path, geom, mesh_files)
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
    (args.output / "MIRROR_NOTICE.txt").write_bytes(
        (ROOT / "isaacsim/MIRROR_NOTICE.txt").read_bytes()
    )
    print(f"Wrote mg400_mirror.usda and MIRROR_NOTICE.txt to {args.output}")


if __name__ == "__main__":
    main()
