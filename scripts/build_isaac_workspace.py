#!/usr/bin/env python3
"""Save the MG400 mirror scene and its main entry file.

Run: uv run --group usd python scripts/build_isaac_workspace.py

The scene contains the robot, ground, lights and camera. The mirror code sets
robot and jaw transforms from measured positions while physics stays stopped.
"""

import argparse
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]


def build_workspace(project_root=ROOT):
    output = project_root / "isaacsim"
    stage = Usd.Stage.CreateNew(str(output / "workspace_scene.usda"))
    stage.GetRootLayer().subLayerPaths = ["./scene.usda"]
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.SetTimeCodesPerSecond(60)
    stage.SetFramesPerSecond(60)
    stage.OverridePrim("/World/MG400").GetReferences().SetReferences(
        [Sdf.Reference("./mg400_mirror.usda")]
    )
    stage.OverridePrim("/World/physicsScene").SetActive(False)
    stage.GetRootLayer().Save()

    entry = Usd.Stage.CreateNew(str(project_root / "00_OPEN_MG400.usda"))
    entry.GetRootLayer().subLayerPaths = ["./isaacsim/workspace_scene.usda"]
    entry.SetDefaultPrim(entry.GetPrimAtPath("/World"))
    UsdGeom.SetStageMetersPerUnit(entry, 1)
    UsdGeom.SetStageUpAxis(entry, UsdGeom.Tokens.z)
    entry.SetTimeCodesPerSecond(60)
    entry.SetFramesPerSecond(60)
    camera = UsdGeom.Xformable(entry.GetPrimAtPath("/World/Camera")).GetLocalTransformation()
    eye = camera.ExtractTranslation()
    target = eye - camera.TransformDir(Gf.Vec3d(0, 0, 1))
    entry.GetRootLayer().customLayerData = {
        "cameraSettings": {
            "boundCamera": "/World/Camera",
            "Perspective": {"position": eye, "target": target},
        },
    }
    entry.GetRootLayer().Save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    args = parser.parse_args()
    for filename in ("scene.usda", "mg400_mirror.usda"):
        if not (args.project_root / "isaacsim" / filename).is_file():
            parser.error("Build the base and mirror assets first")
    build_workspace(args.project_root)
    print(f"Wrote {args.project_root / '00_OPEN_MG400.usda'} and isaacsim/workspace_scene.usda")


if __name__ == "__main__":
    main()
