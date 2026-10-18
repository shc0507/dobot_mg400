#!/usr/bin/env python3
"""Convert the manufacturer's PGEA-2/15-10-O-S STEP to visual meshes.

Run: uv run --no-project --with cadquery==2.8.0 python scripts/build_gripper_mesh.py

Source: DH Robotics, PGEA 3D Model and 2D Drawing archive downloaded by the user.
The 89 mm O-S side-exit version matches the cable routing in the user's
installed-gripper photo. This is a visual fit, not a confirmed hardware model:
the earlier DT-AC-PGEA2-001 packaging specifies the 94 mm O-B dimensions.
The supplied model contains the stock jaw carriers and cable, but no extended
fingertips or Dobot mounting adapter. These meshes preserve the supplied jaw
positions.

All output coordinates are metres. The origin is the centre of the rear
18 x 30 mm face. +X is thickness, +Y is jaw travel, and -Z points to the jaws.
The jaw ends are at Z=-0.089; the cable exits sideways along +Y, below the
rear face (Z=-0.0225 to -0.007), and reaches Y=+0.050.
The STEP's original millimetre frame is rotated -90 degrees about X after
subtracting (10.2985785383, -337.2103196204, 14.3601205939).
"""

import argparse
import hashlib
import struct
import tempfile
import zipfile
from pathlib import Path

import cadquery as cq
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
STEP_NAME = "PGEA-【2-10】【15-10】-O-S_Model_20250624.STEP"
# The entity grouping below belongs to this exact manufacturer STEP revision.
STEP_SHA256 = "c067fb10adedf664a4defb3c2f686acd64c55d2fb73ad32e9d11e52b9c74c90e"
PARTS = {
    "pgea_body": (2, 3, 4, 5, 6, 7, 8, 9, 10),
    "pgea_front": (1,),
    "pgea_jaw_left": (11, 12, 13, 14, 15),
    "pgea_jaw_right": (16, 17, 18, 19, 20),
    "pgea_cable": (0,),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=ROOT / "downloads/PGEA-3D-Model-and-2D-Drawing-5.zip",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "src/mg400_agent/assets/mg400/meshes",
    )
    args = parser.parse_args()
    with zipfile.ZipFile(args.archive) as archive:
        member = next(name for name in archive.namelist() if name.endswith(STEP_NAME))
        source = archive.read(member)
    if hashlib.sha256(source).hexdigest() != STEP_SHA256:
        raise ValueError("Unexpected PGEA STEP revision; recheck solid grouping before export")
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / "pgea.step"
        source_path.write_bytes(source)
        solids = cq.importers.importStep(str(source_path)).solids().vals()
    if len(solids) != 21:
        raise ValueError(f"Expected 21 STEP solids, got {len(solids)}")

    housing = solids[2].BoundingBox()
    origin = (
        (housing.xmin + housing.xmax) / 2,
        housing.ymin,
        (housing.zmin + housing.zmax) / 2,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    for name, indices in PARTS.items():
        part = cq.Compound.makeCompound([solids[index] for index in indices])
        part = part.translate(tuple(-value for value in origin))
        part = part.rotate((0, 0, 0), (1, 0, 0), -90).scale(0.001)
        path = args.output / f"{name}.stl"
        part.exportStl(str(path), tolerance=0.00002, angularTolerance=0.2, relative=False)
        # Tiny CAD edges can collapse when STL vertices are converted to float32.
        records = np.frombuffer(
            path.read_bytes()[84:],
            dtype=[("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")],
        )
        vertices = records["vertices"]
        normals = np.cross(vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0])
        records = records[np.any(normals != 0, axis=1)]
        header = b"DH Robotics PGEA-2/15-10-O-S; see scripts/build_gripper_mesh.py".ljust(80, b" ")
        path.write_bytes(header + struct.pack("<I", len(records)) + records.tobytes())
        limits = np.concatenate((vertices.min(axis=(0, 1)), vertices.max(axis=(0, 1))))
        print(f"{path.name}: bounds (m)={tuple(round(float(value), 6) for value in limits)}")


if __name__ == "__main__":
    main()
