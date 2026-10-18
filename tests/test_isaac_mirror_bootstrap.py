import asyncio
import builtins
import runpy
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom, UsdUtils  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "existing_robot, explicit_scene", ((False, False), (True, False), (True, True))
)
def test_bootstrap_uses_default_or_explicit_scene_and_preserves_existing_robot(
    monkeypatch, tmp_path, existing_robot, explicit_scene
):
    data_directory = tmp_path / "external-data"
    monkeypatch.setenv("MG400_DATA_DIR", str(data_directory))
    monkeypatch.setattr(sys, "path", list(sys.path))
    connection = Mock(side_effect=AssertionError("No network"))
    monkeypatch.setattr(socket, "create_connection", connection)
    current = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(current, "/World/MG400" if existing_robot else "/Other")
    stages = [current]
    opened = []

    async def open_stage(path):
        opened.append(path)
        stages[0] = Usd.Stage.Open(path)
        return True, ""

    async def update():
        pass

    modules = {
        name: ModuleType(name)
        for name in (
            "omni",
            "omni.kit",
            "omni.kit.app",
            "omni.kit.viewport",
            "omni.kit.viewport.utility",
            "omni.timeline",
            "omni.usd",
        )
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attribute = name.rsplit(".", 1)
            setattr(modules[parent], attribute, module)
    modules["omni.kit.app"].get_app = lambda: SimpleNamespace(next_update_async=update)
    modules["omni.usd"].get_context = lambda: SimpleNamespace(
        get_stage=lambda: stages[0], open_stage_async=open_stage
    )
    stopped = Mock()
    modules["omni.timeline"].get_timeline_interface = lambda: SimpleNamespace(stop=stopped)
    modules["omni.kit.viewport.utility"].get_active_viewport = lambda: None
    real_run_path = runpy.run_path
    mirror_runs = []

    def start_mirror(path):
        mirror_runs.append(path)
        name = "_test_bootstrap_mirror"
        monkeypatch.setattr(
            builtins, name, SimpleNamespace(report={"status": "live"}), raising=False
        )
        return {"SINGLETON": name, "MG400_MIRROR_START_TASK": asyncio.sleep(0)}

    monkeypatch.setattr(runpy, "run_path", start_mirror)
    selected = ROOT / "isaacsim/grasp_scene.usda"
    globals_ = {"SCENE_PATH": str(selected)} if explicit_scene else {}
    with monkeypatch.context() as startup:
        startup.setattr(asyncio, "ensure_future", lambda coroutine: coroutine)
        namespace = real_run_path(
            str(ROOT / "apps/isaacsim/mirror_bootstrap.py"),
            init_globals=globals_,
            run_name="test_bootstrap",
        )
    asyncio.run(namespace["MG400_MIRROR_BOOTSTRAP_TASK"])

    if existing_robot and not explicit_scene:
        assert opened == []
        assert stages[0] is current
        stopped.assert_not_called()
    else:
        expected = selected if explicit_scene else ROOT / "00_OPEN_MG400.usda"
        assert opened == [str(expected)]
        stopped.assert_called_once_with()
        backups = list((data_directory / "isaacsim").glob("before_mirror_load_*.usda"))
        assert len(backups) == 1
        assert UsdUtils.ComputeAllDependencies(str(backups[0]))[2] == []
    assert mirror_runs == [str(ROOT / "apps/isaacsim/mirror.py")]
    connection.assert_not_called()
