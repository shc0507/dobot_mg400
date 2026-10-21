"""Check the complete USD export in an empty directory without Isaac Sim."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdUtils  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_rebuilt_workspace_contains_all_dependencies_and_notices():
    with TemporaryDirectory(prefix="mg400-usd-test-") as directory:
        project = Path(directory)
        output = project / "isaacsim"
        source = ROOT / "src/mg400_agent/assets/mg400"
        for script, arguments in (
            ("build_isaac_model.py", ["--source", str(source), "--output", str(output)]),
            ("build_isaac_workspace.py", ["--project-root", str(project)]),
        ):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script), *arguments],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stdout + result.stderr

        for filename in ("LICENSE", "NOTICE.txt"):
            assert (output / filename).read_bytes() == (ROOT / "isaacsim" / filename).read_bytes()

        entry = project / "00_OPEN_MG400.usda"
        stage = Usd.Stage.Open(str(entry))
        assert stage
        assert not stage.GetCompositionErrors()
        assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/World")
        for path in (
            "/World/MG400",
            "/World/MG400/pgea_housing",
            "/World/MG400/pgea_left_jaw",
            "/World/MG400/pgea_right_jaw",
            "/World/Ground",
            "/World/Camera",
        ):
            assert stage.GetPrimAtPath(path), path
        for path in ("/World/GraspObject", "/World/GraspSupport", "/World/GraspMaterial"):
            assert not stage.GetPrimAtPath(path), path
        for filename in (
            "mg400_mirror.usda",
            "MIRROR_NOTICE.txt",
            "mg400_grasp.usda",
            "grasp_scene.usda",
            "GRASP_NOTICE.txt",
        ):
            assert not (output / filename).exists()
        layers, assets, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(entry)))
        assert not unresolved
        assert {Path(layer.realPath).relative_to(project).as_posix() for layer in layers} == {
            "00_OPEN_MG400.usda",
            "isaacsim/workspace_scene.usda",
            "isaacsim/scene.usda",
            "isaacsim/mg400_with_gripper.usd",
        }
        assert all(Path(layer.realPath).is_relative_to(project) for layer in layers)
        assert all(Path(asset).is_relative_to(project) for asset in assets)
