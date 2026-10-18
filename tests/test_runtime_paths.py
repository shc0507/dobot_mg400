import asyncio
import runpy
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from mg400_agent import runtime_paths

ROOT = Path(__file__).resolve().parents[1]


def test_default_data_path_uses_repository_work_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("MG400_DATA_DIR", raising=False)
    monkeypatch.setattr(runtime_paths, "ROOT", tmp_path / "repository")
    result = runtime_paths.data_path("isaacsim/status.json")
    assert result == tmp_path / "repository/work/isaacsim/status.json"
    assert result.parent.is_dir()


def test_external_data_path_creates_parent_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_DATA_DIR", str(tmp_path / "external-data"))
    result = runtime_paths.data_path("isaacsim/status.json")
    assert result == tmp_path / "external-data/isaacsim/status.json"
    assert result.is_absolute()
    assert result.parent.is_dir()


@pytest.mark.parametrize(
    "script, report",
    (
        ("joint_control.py", "control_status.json"),
        ("gripper_control.py", "real_gripper_status.json"),
        ("mirror.py", "mirror_status.json"),
    ),
)
def test_panel_reports_use_external_data_without_starting_hardware(
    monkeypatch, tmp_path, script, report
):
    if script == "mirror.py":
        pytest.importorskip("pxr")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MG400_DATA_DIR", str(tmp_path / "external-data"))
    connection = Mock(side_effect=AssertionError("No network"))
    tasks = Mock(side_effect=AssertionError("No startup task"))
    monkeypatch.setattr(socket, "create_connection", connection)
    monkeypatch.setattr(asyncio, "ensure_future", tasks)
    namespace = runpy.run_path(str(ROOT / "apps/isaacsim" / script), run_name="test_data_path")
    assert namespace["REPORT"] == tmp_path / "external-data/isaacsim" / report
    connection.assert_not_called()
    tasks.assert_not_called()


def test_export_preserves_references_variants_session_edits_and_asset_resolution(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdGeom, UsdUtils

    source = tmp_path / "repository"
    source.mkdir()
    texture = source / "texture.png"
    texture.write_bytes(b"texture")
    model = Usd.Stage.CreateNew(str(source / "model.usda"))
    model_prim = UsdGeom.Xform.Define(model, "/Model").GetPrim()
    model_prim.CreateAttribute("texture", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("./texture.png")
    )
    model.SetDefaultPrim(model_prim)
    model.GetRootLayer().Save()

    stage = Usd.Stage.CreateNew(str(source / "scene.usda"))
    prim = UsdGeom.Xform.Define(stage, "/World").GetPrim()
    prim.GetReferences().AddReference("./model.usda")
    variants = prim.GetVariantSets().AddVariantSet("shape")
    for name, value in (("small", 1), ("large", 2)):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            prim.CreateAttribute("size", Sdf.ValueTypeNames.Int).Set(value)
    variants.SetVariantSelection("small")
    stage.SetDefaultPrim(prim)
    stage.GetRootLayer().Save()
    stage.SetEditTarget(stage.GetSessionLayer())
    prim.CreateAttribute("sessionValue", Sdf.ValueTypeNames.Int).Set(7)
    before = {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}

    output = tmp_path / "external-data/isaacsim/snapshot.usda"
    assert runtime_paths.export_scene(stage, output)
    restored = Usd.Stage.Open(str(output))
    restored_prim = restored.GetPrimAtPath("/World")
    assert restored_prim.HasAuthoredReferences()
    assert restored_prim.GetAttribute("sessionValue").Get() == 7
    assert restored_prim.GetAttribute("texture").Get().resolvedPath == str(texture)
    restored_variants = restored_prim.GetVariantSet("shape")
    assert restored_variants.GetVariantNames() == ["large", "small"]
    assert restored_prim.GetAttribute("size").Get() == 1
    restored_variants.SetVariantSelection("large")
    assert restored_prim.GetAttribute("size").Get() == 2
    assert UsdUtils.ComputeAllDependencies(str(output))[2] == []
    assert before == {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}


@pytest.mark.parametrize("save_reference", (False, True))
def test_export_requires_saved_reference_edits_and_preserves_them_across_processes(
    tmp_path, save_reference
):
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdGeom

    source = tmp_path / "repository"
    source.mkdir()
    asset_path = source / "asset.usda"
    asset = Usd.Stage.CreateNew(str(asset_path))
    asset_prim = UsdGeom.Xform.Define(asset, "/Asset").GetPrim()
    asset_value = asset_prim.CreateAttribute("value", Sdf.ValueTypeNames.Int)
    asset_value.Set(1)
    asset.SetDefaultPrim(asset_prim)
    asset.GetRootLayer().Save()
    stage = Usd.Stage.CreateNew(str(source / "scene.usda"))
    UsdGeom.Xform.Define(stage, "/World").GetPrim().GetReferences().AddReference("./asset.usda")
    stage.GetRootLayer().Save()
    asset_value.Set(7)
    if save_reference:
        asset.GetRootLayer().Save()
    source_bytes = asset_path.read_bytes()
    output = tmp_path / "external-data/isaacsim/snapshot.usda"

    if not save_reference:
        with pytest.raises(RuntimeError, match="Save modified referenced assets") as error:
            runtime_paths.export_scene(stage, output)
        assert asset.GetRootLayer().identifier in str(error.value)
        assert not output.exists()
        assert asset.GetRootLayer().dirty
    else:
        assert runtime_paths.export_scene(stage, output)
        check = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from pxr import Usd; stage = Usd.Stage.Open(sys.argv[1]); "
                "prim = stage.GetPrimAtPath('/World'); assert prim.HasAuthoredReferences(); "
                "assert prim.GetAttribute('value').Get() == 7",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert check.returncode == 0, check.stdout + check.stderr
    assert asset_path.read_bytes() == source_bytes


@pytest.mark.parametrize("scene", ("00_OPEN_MG400.usda", "isaacsim/grasp_scene.usda"))
def test_project_scene_export_resolves_from_external_data_directory(tmp_path, scene):
    pytest.importorskip("pxr")
    from pxr import Usd, UsdUtils

    stage = Usd.Stage.Open(str(ROOT / scene))
    before = {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}
    output = tmp_path / "external-data/isaacsim/snapshot.usda"
    assert runtime_paths.export_scene(stage, output)
    restored = Usd.Stage.Open(str(output))
    assert restored.GetPrimAtPath("/World/MG400")
    assert UsdUtils.ComputeAllDependencies(str(output))[2] == []
    assert before == {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}
