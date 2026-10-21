#!/usr/bin/env python3
"""Rebuild the offline models from pinned HarvestX source (no ROS installation).

Run: uv run --no-project --with pycollada==0.9.3 --with xacro==2.1.1 \
    python scripts/build_mujoco_model.py
"""

import argparse
import io
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import collada
import numpy as np
import xacro

REVISION = "1a8c7344be44c52030f7216c0f387873ed91fd11"
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "mg400_description"
PGEA_PARTS = ("body", "front", "jaw_left", "jaw_right", "cable")


def add_gripper(flange: ET.Element):
    """Match the photo's centred, side-exit mount; fingertip lengths are estimates."""
    flange.append(
        ET.Comment(
            " PGEA-2-10 O-S CAD selected for the photo's side cable exit. "
            "Rear face seated on existing flange; 15 mm fingertips are estimates. "
            "Visual geometry only; no added payload inertia or gripper actuation. "
        )
    )
    gripper = ET.SubElement(
        flange, "body", name="pgea_gripper", pos="0 0 0", euler="0 0 1.5707963267948966"
    )
    for part in PGEA_PARTS:
        silver = part == "front" or part.startswith("jaw")
        color = "0.65 0.68 0.72 1" if silver else "0.09 0.10 0.12 1"
        ET.SubElement(
            gripper,
            "geom",
            name=f"visual_pgea_{part}",
            type="mesh",
            mesh=f"pgea_{part}",
            rgba=color,
            mass="0",
        )
    for side, y in (("left", -0.01045), ("right", 0.01045)):
        ET.SubElement(
            gripper,
            "geom",
            name=f"pgea_fingertip_{side}",
            type="box",
            pos=f"0 {y} -0.0965",
            size="0.005 0.0045 0.0075",
            rgba="0.055 0.06 0.065 1",
            mass="0",
        )
    ET.SubElement(gripper, "site", name="pgea_jaw_center", pos="0 0 -0.089")
    ET.SubElement(gripper, "site", name="pgea_tip", pos="0 0 -0.104")


def source_blob(repository: Path, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), "show", f"{REVISION}:{path}"])


def expanded_urdf(repository: Path) -> ET.Element:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for relative in (
            "urdf/mg400.urdf.xacro",
            "urdf/mg400.xacro",
            "urdf/common/constants.xacro",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = source_blob(repository, f"{SOURCE}/{relative}").decode()
            path.write_text(text.replace("$(find mg400_description)", str(root)))
        return ET.fromstring(xacro.process_file(str(root / "urdf/mg400.urdf.xacro")).toxml())


def number_list(values) -> str:
    return " ".join(format(float(value), ".15g") for value in values)


def convert_mesh(repository: Path, filename: str, output: Path):
    """Bake Collada scene transforms, retaining separate diffuse material groups."""
    document = collada.Collada(io.BytesIO(source_blob(repository, f"{SOURCE}/meshes/{filename}")))
    if document.assetInfo.upaxis != "Z_UP" or document.assetInfo.unitmeter != 1.0:
        raise ValueError(f"Unexpected source coordinate convention: {filename}")
    parts = []
    for geometry in document.scene.objects("geometry"):
        # Bound primitives include the entire visual-scene node transform. Using
        # unbound document.geometries here would lose scale/rotation transforms.
        for primitive in geometry.primitives():
            if not isinstance(primitive, collada.triangleset.BoundTriangleSet):
                raise ValueError(f"Expected triangles in pinned mesh: {filename}")
            triangles = primitive.vertex[primitive.vertex_index].astype(np.float64)
            normal = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
            lengths = np.linalg.norm(normal, axis=1)
            keep = lengths > 0
            triangles, normal, lengths = triangles[keep], normal[keep], lengths[keep]
            normal /= lengths[:, None]
            name = f"{Path(filename).stem}_{len(parts)}"
            records = np.zeros(
                len(triangles),
                dtype=[("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")],
            )
            records["normal"], records["vertices"] = normal, triangles
            header = b"Modified: HarvestX MG400_ROS2 DAE to STL; see ../NOTICE.txt".ljust(80, b" ")
            (output / f"{name}.stl").write_bytes(
                header + struct.pack("<I", len(records)) + records.tobytes()
            )
            parts.append((name, number_list(primitive.material.effect.diffuse)))
    return parts


def make_scene(urdf: ET.Element, mesh_parts) -> ET.Element:
    scene = ET.Element("mujoco", model="MG400 offline kinematic model")
    scene.append(
        ET.Comment(" Modified from HarvestX MG400_ROS2; see NOTICE.txt. Kinematic playback only. ")
    )
    ET.SubElement(
        scene,
        "compiler",
        angle="radian",
        meshdir="meshes",
        inertiafromgeom="false",
        fusestatic="false",
        autolimits="true",
    )
    ET.SubElement(scene, "option", gravity="0 0 0")
    visual = ET.SubElement(scene, "visual")
    ET.SubElement(visual, "global", offwidth="1280", offheight="960")
    ET.SubElement(
        visual, "headlight", ambient="0.3 0.3 0.3", diffuse="0.6 0.6 0.6", specular="0.2 0.2 0.2"
    )
    ET.SubElement(visual, "quality", shadowsize="2048")
    default = ET.SubElement(scene, "default")
    ET.SubElement(default, "geom", contype="0", conaffinity="0", group="1")
    ET.SubElement(default, "site", size="0.004", group="3", rgba="0 0 0 0")
    assets = ET.SubElement(scene, "asset")
    ET.SubElement(
        assets,
        "texture",
        name="floor_texture",
        type="2d",
        builtin="checker",
        rgb1="0.29 0.32 0.35",
        rgb2="0.35 0.38 0.41",
        width="512",
        height="512",
    )
    ET.SubElement(
        assets,
        "material",
        name="floor_material",
        texture="floor_texture",
        texrepeat="4 4",
        texuniform="true",
        reflectance="0.1",
    )
    for parts in mesh_parts.values():
        for name, _ in parts:
            ET.SubElement(assets, "mesh", name=name, file=f"{name}.stl")
    for part in PGEA_PARTS:
        ET.SubElement(assets, "mesh", name=f"pgea_{part}", file=f"pgea_{part}.stl")
    world = ET.SubElement(scene, "worldbody")
    ET.SubElement(
        world,
        "geom",
        name="floor",
        type="plane",
        size="1 1 0.1",
        pos="0 0 -0.002",
        material="floor_material",
        group="0",
    )
    ET.SubElement(
        world,
        "light",
        name="key",
        pos="0.2 -0.5 1.5",
        dir="0 0 -1",
        diffuse="0.9 0.9 0.9",
        directional="true",
    )
    ET.SubElement(
        world,
        "light",
        name="fill",
        pos="-0.5 0.5 0.8",
        dir="0 0 -1",
        diffuse="0.4 0.4 0.4",
        castshadow="false",
    )
    eye = np.array((0.78, -0.85, 0.66))
    z = eye - (0.13, 0, 0.22)
    z /= np.linalg.norm(z)
    x = np.cross((0, 0, 1), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    ET.SubElement(
        world,
        "camera",
        name="overview",
        pos=number_list(eye),
        xyaxes=number_list((*x, *y)),
        fovy="38",
    )
    links = {link.attrib["name"]: link for link in urdf.findall("link")}
    children = {}
    for joint in urdf.findall("joint"):
        children.setdefault(joint.find("parent").attrib["link"], []).append(joint)

    def add_link(parent: ET.Element, link_name: str, joint=None):
        attributes = {"name": link_name}
        if joint is not None:
            origin = joint.find("origin")
            attributes.update(pos=origin.attrib["xyz"], euler=origin.attrib["rpy"])
        body = ET.SubElement(parent, "body", **attributes)
        link = links[link_name]
        inertial = link.find("inertial")
        if inertial is not None:
            origin, inertia = inertial.find("origin"), inertial.find("inertia")
            if origin.attrib["rpy"] != "0 0 0":
                raise ValueError("Expected unrotated inertia in pinned URDF")
            ET.SubElement(
                body,
                "inertial",
                pos=origin.attrib["xyz"],
                mass=inertial.find("mass").attrib["value"],
                fullinertia=" ".join(
                    inertia.attrib[key] for key in ("ixx", "iyy", "izz", "ixy", "ixz", "iyz")
                ),
            )
        if joint is not None and joint.attrib["type"] != "fixed":
            limits = joint.find("limit").attrib
            ET.SubElement(
                body,
                "joint",
                name=joint.attrib["name"],
                type="hinge",
                axis=joint.find("axis").attrib["xyz"],
                range=f"{limits['lower']} {limits['upper']}",
            )
        for item in link.findall("visual"):
            origin = item.find("origin")
            mesh = item.find("geometry/mesh").attrib["filename"].rsplit("/", 1)[1]
            for name, rgba in mesh_parts[mesh]:
                ET.SubElement(
                    body,
                    "geom",
                    name=f"visual_{name}",
                    type="mesh",
                    mesh=name,
                    rgba=rgba,
                    pos=origin.attrib["xyz"],
                    euler=origin.attrib["rpy"],
                )
        if link_name == "mg400_base_link":
            ET.SubElement(body, "site", name="mg400_base")
        elif link_name == "mg400_origin_link":
            body.append(
                ET.Comment(
                    " Controller origin is (-5, 0, 224) mm from the base; axes are parallel. "
                )
            )
            ET.SubElement(body, "site", name="mg400_controller_origin")
        elif link_name == "mg400_end_effector_flange":
            body.append(ET.Comment(" Zero tool offset: TCP coincides with the URDF flange frame. "))
            ET.SubElement(body, "site", name="mg400_flange")
            ET.SubElement(body, "site", name="mg400_tcp", group="0", rgba="1 0.35 0.05 1")
            add_gripper(body)
        for child in children.get(link_name, []):
            add_link(body, child.find("child").attrib["link"], child)

    add_link(world, "mg400_base_link")
    return scene


def make_dynamics_scene(urdf: ET.Element, mesh_parts) -> ET.Element:
    """Add four servos and the controller's parallel-linkage coupling equations."""
    scene = make_scene(urdf, mesh_parts)
    scene.set("model", "MG400 offline dynamics model")
    scene[0].text = (
        " Modified from HarvestX MG400_ROS2; see NOTICE.txt. "
        "Uncalibrated dynamics; contacts remain disabled. "
    )
    scene.find("option").attrib.update(
        gravity="0 0 -9.81", timestep="0.002", integrator="implicitfast"
    )
    # These are simulation tuning values, not measured motor/gearbox properties.
    ET.SubElement(scene.find("default"), "joint", damping="0.05", armature="0.001")
    scene.append(
        ET.Comment(
            " J4 limits from the MG400 User Guide: +/-360 degrees. Other limits "
            "from pinned mg400_common/kinematics.hpp, before its 1.05 degree planning "
            "margin. Runtime also applies J2 >= -19 degrees when J3 <= 0. "
        )
    )
    ranges_degrees = {
        "mg400_j1": (-160, 160),
        "mg400_j2_1": (-25, 85),
        "mg400_j2_2": (-25, 85),
        "mg400_j3_1": (-60, 60),
        "mg400_j3_2": (-85, 25),
        "mg400_j4_1": (-105, 25),
        "mg400_j4_2": (-25, 105),
        "mg400_j5": (-360, 360),
    }
    for joint in scene.findall("worldbody//joint"):
        joint.set("range", number_list(np.deg2rad(ranges_degrees[joint.attrib["name"]])))

    tendon = ET.SubElement(scene, "tendon")
    q3_range = number_list(np.deg2rad((-25, 105)))
    q3 = ET.SubElement(tendon, "fixed", name="mg400_q3", range=q3_range)
    ET.SubElement(q3, "joint", joint="mg400_j2_1", coef="1")
    ET.SubElement(q3, "joint", joint="mg400_j3_1", coef="1")
    # A fixed tendon is a linear joint sum, not a physical cable. Two zero-length
    # sums enforce the three-joint relations that a joint mimic cannot express.
    for name, terminal_joint, coefficient in (
        ("mg400_minus_q3_loop", "mg400_j4_1", "1"),
        ("mg400_q3_loop", "mg400_j4_2", "-1"),
    ):
        loop = ET.SubElement(tendon, "fixed", name=name, limited="false")
        ET.SubElement(loop, "joint", joint="mg400_j2_1", coef="1")
        ET.SubElement(loop, "joint", joint="mg400_j3_1", coef="1")
        ET.SubElement(loop, "joint", joint=terminal_joint, coef=coefficient)

    equality = ET.SubElement(scene, "equality")
    solver = {"solref": "0.004 1", "solimp": "0.999 0.999 0.001"}
    for name, joint, coefficient in (
        ("mg400_linkage_q2", "mg400_j2_2", "1"),
        ("mg400_linkage_minus_q2", "mg400_j3_2", "-1"),
    ):
        ET.SubElement(
            equality,
            "joint",
            name=name,
            joint1=joint,
            joint2="mg400_j2_1",
            polycoef=f"0 {coefficient} 0 0 0",
            **solver,
        )
    for name, tendon_name in (
        ("mg400_linkage_minus_q3", "mg400_minus_q3_loop"),
        ("mg400_linkage_q3", "mg400_q3_loop"),
    ):
        ET.SubElement(
            equality, "tendon", name=name, tendon1=tendon_name, polycoef="0 0 0 0 0", **solver
        )

    actuator = ET.SubElement(scene, "actuator")
    for axis, transmission, target, limits, kp, kv, force in (
        (1, "joint", "mg400_j1", (-160, 160), 200, 8, 20),
        (2, "joint", "mg400_j2_1", (-25, 85), 200, 4, 20),
        (3, "tendon", "mg400_q3", (-25, 105), 200, 4, 20),
        (4, "joint", "mg400_j5", (-360, 360), 20, 0.5, 3),
    ):
        ET.SubElement(
            actuator,
            "position",
            name=f"mg400_q{axis}_position",
            **{transmission: target},
            kp=str(kp),
            kv=str(kv),
            ctrlrange=number_list(np.deg2rad(limits)),
            forcerange=f"-{force} {force}",
        )
    return scene


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "work/references/MG400_ROS2")
    parser.add_argument("--output", type=Path, default=ROOT / "src/mg400_agent/assets/mg400")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    mesh_directory = args.output / "meshes"
    mesh_directory.mkdir(exist_ok=True)
    source_meshes = ROOT / "src/mg400_agent/assets/mg400/meshes"
    for part in PGEA_PARTS:
        source = source_meshes / f"pgea_{part}.stl"
        if source.parent.resolve() != mesh_directory.resolve():
            shutil.copy2(source, mesh_directory / source.name)
    urdf = expanded_urdf(args.repository)
    filenames = sorted(
        {
            item.attrib["filename"].rsplit("/", 1)[1]
            for item in urdf.findall("link/visual/geometry/mesh")
        }
    )
    mesh_parts = {
        filename: convert_mesh(args.repository, filename, mesh_directory) for filename in filenames
    }
    for filename, make_model in (("scene.xml", make_scene), ("dynamics.xml", make_dynamics_scene)):
        scene = make_model(urdf, mesh_parts)
        ET.indent(scene, space="  ")
        ET.ElementTree(scene).write(args.output / filename, encoding="utf-8", xml_declaration=True)
    (args.output / "LICENSE").write_bytes(source_blob(args.repository, "LICENSE"))
    notice = ROOT / "src/mg400_agent/assets/mg400/NOTICE.txt"
    (args.output / "NOTICE.txt").write_bytes(notice.read_bytes())
    print(
        f"Wrote {len(filenames)} source meshes as {sum(map(len, mesh_parts.values()))} "
        f"material parts, scene.xml and dynamics.xml to {args.output}"
    )


if __name__ == "__main__":
    main()
