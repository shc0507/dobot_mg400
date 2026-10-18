#!/usr/bin/env python3
"""Save the complete MG400 workbench as USD, ready for current-stage controls.

Run: uv run --no-project --with usd-core==26.8 python scripts/build_isaac_workspace.py

This offline authoring tool runs only when rebuilding the scene. It references
the existing assets and stores the robot pose, physics, props and camera in USD.
Joint gains, friction and the 5 g test object retain the original grasp demo's
simulation assumptions; they are not calibrated physical-robot parameters.
"""

import argparse
import math
import sys
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.joint_limits import validate_joints  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402

ROBOT = "/World/MG400"
PICKUP = (0.0, 25.0, 45.0, 0.0)
# Existing grasp demo's CAD IK solutions, respectively 50 and 80 mm above pickup.
READY = (0.0, 14.994247007322407, 28.893303073703972, 0.0)
LIFT = (0.0, 11.100607502974146, 19.146049844509935, 0.0)
EYE = Gf.Vec3d(0.75, -0.85, 0.60)
LOOK_AT = Gf.Vec3d(0.16, 0, 0.18)


def joint_frame(joint, side):
    matrix = Gf.Matrix4d().SetRotate(
        Gf.Rotation(joint.GetAttribute(f"physics:localRot{side}").Get())
    )
    matrix.SetTranslateOnly(Gf.Vec3d(joint.GetAttribute(f"physics:localPos{side}").Get()))
    return matrix


def body_transforms(stage, joints_deg):
    """Solve the imported joint tree; USD matrices use row-vector ordering."""
    coordinates = dict(
        zip((f"mg400_{name}" for name in JOINT_NAMES), joint_angles(joints_deg), strict=True)
    )
    coordinates.update(pgea_left_slide=0.0, pgea_right_slide=0.0)
    pending = list(stage.GetPrimAtPath(ROBOT + "/joints").GetChildren())
    result = {}
    while pending:
        available = [
            joint
            for joint in pending
            if not joint.GetRelationship("physics:body0").GetTargets()
            or joint.GetRelationship("physics:body0").GetTargets()[0] in result
        ]
        if not available:
            raise ValueError("MG400 joint tree is disconnected or contains a loop")
        for joint in available:
            parent = joint.GetRelationship("physics:body0").GetTargets()
            child = joint.GetRelationship("physics:body1").GetTargets()[0]
            motion = Gf.Matrix4d(1)
            coordinate = coordinates.get(joint.GetName(), 0.0)
            if joint.IsA(UsdPhysics.RevoluteJoint):
                axis = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[
                    joint.GetAttribute("physics:axis").Get()
                ]
                motion.SetRotate(Gf.Rotation(Gf.Vec3d(*axis), math.degrees(coordinate)))
            parent_world = result[parent[0]] if parent else Gf.Matrix4d(1)
            result[child] = (
                joint_frame(joint, 1).GetInverse() * motion * joint_frame(joint, 0) * parent_world
            )
            pending.remove(joint)
    return result, coordinates


def box(stage, path, position, dimensions, color, mass=None):
    shape = UsdGeom.Cube.Define(stage, path)
    shape.CreateSizeAttr(1)
    shape.AddTranslateOp().Set(Gf.Vec3d(*position))
    shape.AddScaleOp().Set(Gf.Vec3f(*dimensions))
    shape.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    prim = shape.GetPrim()
    UsdPhysics.CollisionAPI.Apply(prim)
    prim.AddAppliedSchema("PhysxCollisionAPI")
    prim.CreateAttribute("physxCollision:contactOffset", Sdf.ValueTypeNames.Float).Set(0.0002)
    prim.CreateAttribute("physxCollision:restOffset", Sdf.ValueTypeNames.Float).Set(0)
    if mass is not None:
        UsdPhysics.RigidBodyAPI.Apply(prim)
        UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(mass)
    return prim


def build_workspace(project_root=ROOT):
    output = project_root / "isaacsim"
    stage = Usd.Stage.CreateNew(str(output / "workspace_scene.usda"))
    stage.GetRootLayer().subLayerPaths = ["./grasp_scene.usda"]
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(7200)
    stage.SetTimeCodesPerSecond(60)
    stage.SetFramesPerSecond(60)

    robot = stage.GetPrimAtPath(ROBOT)
    for name, values in (("pickup", PICKUP), ("ready", READY), ("lift", LIFT)):
        validate_joints(values)
        robot.CreateAttribute(f"mg400:{name}JointsDeg", Sdf.ValueTypeNames.Double4).Set(
            Gf.Vec4d(*values)
        )
    transforms, coordinates = body_transforms(stage, READY)
    for path, matrix in transforms.items():
        stage.GetPrimAtPath(path).GetAttribute("xformOp:transform").Set(matrix)
    for name, value in coordinates.items():
        kind = "linear" if name.startswith("pgea_") else "angular"
        drive = UsdPhysics.DriveAPI(stage.GetPrimAtPath(f"{ROBOT}/joints/{name}"), kind)
        drive.CreateTargetPositionAttr(value if kind == "linear" else math.degrees(value))
        if kind == "angular":
            drive.CreateStiffnessAttr(10000)
            drive.CreateDampingAttr(500)
    for side in ("left", "right"):
        jaw = stage.GetPrimAtPath(f"{ROBOT}/pgea_{side}_jaw")
        jaw.AddAppliedSchema("PhysxContactReportAPI")
        jaw.CreateAttribute("physxContactReport:threshold", Sdf.ValueTypeNames.Float).Set(0)

    physics = stage.GetPrimAtPath("/World/physicsScene")
    physics.AddAppliedSchema("PhysxSceneAPI")
    for name, kind, value in (
        ("timeStepsPerSecond", Sdf.ValueTypeNames.UInt, 120),
        ("enableGPUDynamics", Sdf.ValueTypeNames.Bool, False),
        ("broadphaseType", Sdf.ValueTypeNames.Token, "MBP"),
        ("solverType", Sdf.ValueTypeNames.Token, "TGS"),
    ):
        physics.CreateAttribute(f"physxScene:{name}", kind).Set(value)

    low, _ = body_transforms(stage, PICKUP)
    flange = low[Sdf.Path(ROBOT + "/mg400_end_effector_flange")].ExtractTranslation()
    object_center = flange - Gf.Vec3d(0, 0, 0.0965)
    support_height = object_center[2] - 0.01
    box(
        stage,
        "/World/GraspSupport",
        (flange[0], flange[1], support_height / 2),
        (0.12, 0.12, support_height),
        (0.18, 0.23, 0.28),
    )
    cube = box(
        stage,
        "/World/GraspObject",
        object_center + Gf.Vec3d(0, 0, 0.0005),
        (0.008, 0.008, 0.02),
        (0.95, 0.24, 0.045),
        mass=0.005,
    )
    material = UsdShade.Material.Define(stage, "/World/GraspMaterial")
    contact = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    contact.CreateStaticFrictionAttr(1)
    contact.CreateDynamicFrictionAttr(1)
    contact.CreateRestitutionAttr(0)
    UsdShade.MaterialBindingAPI.Apply(cube).Bind(material, materialPurpose="physics")

    camera = UsdGeom.Camera.Define(stage, "/World/GraspCamera")
    camera.AddTransformOp().Set(
        Gf.Matrix4d().SetLookAt(EYE, LOOK_AT, Gf.Vec3d(0, 0, 1)).GetInverse()
    )
    camera.CreateFocalLengthAttr(24)
    camera.CreateHorizontalApertureAttr(29.386264)
    camera.CreateVerticalApertureAttr(16.527725)
    camera.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100))
    stage.GetRootLayer().Save()

    entry = Usd.Stage.CreateNew(str(project_root / "00_OPEN_MG400.usda"))
    entry.GetRootLayer().subLayerPaths = ["./isaacsim/workspace_scene.usda"]
    entry.SetDefaultPrim(entry.GetPrimAtPath("/World"))
    UsdGeom.SetStageMetersPerUnit(entry, 1)
    UsdGeom.SetStageUpAxis(entry, UsdGeom.Tokens.z)
    entry.SetStartTimeCode(0)
    entry.SetEndTimeCode(7200)
    entry.SetTimeCodesPerSecond(60)
    entry.SetFramesPerSecond(60)
    entry.GetRootLayer().customLayerData = {
        "cameraSettings": {
            "boundCamera": "/World/GraspCamera",
            "Perspective": {"position": EYE, "target": LOOK_AT},
        },
    }
    entry.GetRootLayer().Save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    args = parser.parse_args()
    if not (args.project_root / "isaacsim/grasp_scene.usda").is_file():
        parser.error("Build the base and grasp assets first")
    build_workspace(args.project_root)
    print(f"Wrote {args.project_root / '00_OPEN_MG400.usda'} and isaacsim/workspace_scene.usda")


if __name__ == "__main__":
    main()
