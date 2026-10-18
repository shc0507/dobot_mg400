"""Check Olares startup without Isaac Sim, network connections, or robot actions."""

import asyncio
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def startup_module(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ISAACSIM_ROOT", str(tmp_path / "isaac"))
    (tmp_path / "isaac").mkdir()
    (tmp_path / "isaac/VERSION").write_text("6.1.0-rc.26+test\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    monkeypatch.setattr(
        asyncio, "ensure_future", Mock(side_effect=AssertionError("No startup task"))
    )
    namespace = runpy.run_path(
        str(ROOT / "apps/isaacsim/startup.py"), run_name="test_olares_startup"
    )
    monkeypatch.setattr(
        subprocess, "check_output", Mock(side_effect=["test-commit\n", " M config/robot.toml\n"])
    )
    return SimpleNamespace(**namespace)


def test_read_only_validation_imports_panels_without_scheduling_or_connections(startup_module):
    startup_module.validate_control_imports()
    asyncio.ensure_future.assert_not_called()
    socket.create_connection.assert_not_called()


def install_kit(monkeypatch, scene, opened=True):
    app = SimpleNamespace(next_update_async=AsyncMock())
    timeline = SimpleNamespace(stop=Mock())
    context = SimpleNamespace(
        open_stage_async=AsyncMock(return_value=(opened, "missing scene" if not opened else "")),
        get_stage=lambda: SimpleNamespace(
            GetRootLayer=lambda: SimpleNamespace(realPath=str(scene))
        ),
    )
    omni = ModuleType("omni")
    omni.kit = ModuleType("omni.kit")
    omni.kit.app = ModuleType("omni.kit.app")
    omni.kit.app.get_app = lambda: app
    omni.timeline = ModuleType("omni.timeline")
    omni.timeline.get_timeline_interface = lambda: timeline
    omni.usd = ModuleType("omni.usd")
    omni.usd.get_context = lambda: context
    for module in (omni, omni.kit, omni.kit.app, omni.timeline, omni.usd):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return context, timeline


@pytest.mark.parametrize("opened", [True, False])
def test_startup_records_result_and_only_opens_simulation_controls(
    startup_module, monkeypatch, tmp_path, opened
):
    scene = tmp_path / "actual_scene.usda"
    context, timeline = install_kit(monkeypatch, scene, opened)
    original_run_path = runpy.run_path
    simulation_controls = []

    def run_path(path, **kwargs):
        if Path(path).name == "actions.py":
            simulation_controls.append(path)
            return {}
        return original_run_path(path, **kwargs)

    monkeypatch.setattr(runpy, "run_path", run_path)
    if opened:
        asyncio.run(startup_module.startup())
    else:
        with pytest.raises(RuntimeError, match="Could not open MG400 scene: missing scene"):
            asyncio.run(startup_module.startup())
    report = json.loads(startup_module.REPORT.read_text())
    assert report["commit"] == "test-commit"
    assert report["dirty"] is True
    assert report["isaac_version"] == "6.1.0-rc.26+test"
    assert report["python_version"] == sys.version
    assert report["data_directory"] == str(tmp_path / "data")
    assert report["config_sources"]["robot_file"] == str(ROOT / "config/robot.toml")
    assert report["config_sources"]["environment"]["MG400_DATA_DIR"] == str(tmp_path / "data")
    context.open_stage_async.assert_awaited_once_with(str(ROOT / "00_OPEN_MG400.usda"))
    timeline.stop.assert_called_once_with()
    if opened:
        assert report["status"] == "ready"
        assert report["control_imports"] == "ok"
        assert report["scene"] == str(scene)
        assert simulation_controls == [str(ROOT / "apps/isaacsim/actions.py")]
    else:
        assert report["status"] == "error"
        assert report["error"] == "Could not open MG400 scene: missing scene"
        assert not simulation_controls
    assert not startup_module.REPORT.with_suffix(".tmp").exists()
    asyncio.ensure_future.assert_not_called()
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize("custom_startup", [False, True])
def test_launcher_preserves_paths_arguments_and_site_settings(tmp_path, custom_startup):
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "start_isaac_olares.sh"
    shutil.copyfile(ROOT / "scripts/start_isaac_olares.sh", launcher)
    installation = tmp_path / "isaac installation"
    installation.mkdir()
    runner = installation / "runheadless.sh"
    runner.write_text('#!/bin/sh\nexec "$MG400_TEST_PYTHON" "$MG400_TEST_RECORDER" "$@"\n')
    runner.chmod(0o755)
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, pathlib, sys\n"
        "names = ['ISAACSIM_HOST', 'ISAACSIM_SIGNAL_PORT', 'ISAACSIM_STREAM_PORT', "
        "'MG400_DATA_DIR']\n"
        "record = {'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'env': {n: os.environ[n] for n in names}}\n"
        "pathlib.Path(os.environ['MG400_TEST_OUTPUT']).write_text(json.dumps(record))\n"
    )
    output = tmp_path / "launcher.json"
    workspace = tmp_path / "isaaclab"
    workspace.mkdir()
    environment = os.environ.copy()
    for name in ("ISAACSIM_HOST", "ISAACSIM_SIGNAL_PORT", "ISAACSIM_STREAM_PORT", "MG400_DATA_DIR"):
        environment.pop(name, None)
    environment.update(
        ISAACSIM_ROOT=str(installation),
        MG400_TEST_PYTHON=sys.executable,
        MG400_TEST_RECORDER=str(recorder),
        MG400_TEST_OUTPUT=str(output),
        MG400_TEST_WORKSPACE=str(workspace),
    )
    # Map only the fixed Olares working directory to this test's temporary directory.
    shell = """
cd() {
    if [ "$#" -eq 1 ] && [ "$1" = /workspace/isaaclab ]; then
        command cd "$MG400_TEST_WORKSPACE"
    else
        command cd "$@"
    fi
}
. "$0"
"""
    extra_arguments = ["--extra-setting=value with spaces"]
    if custom_startup:
        extra_arguments += ["--exec", str(project / "custom startup.py")]
    subprocess.run(
        ["sh", "-c", shell, str(launcher), *extra_arguments],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    record = json.loads(output.read_text())
    assert record["cwd"] == str(workspace)
    assert record["env"] == {
        "ISAACSIM_HOST": "100.64.0.1",
        "ISAACSIM_SIGNAL_PORT": "49100",
        "ISAACSIM_STREAM_PORT": "47998",
        "MG400_DATA_DIR": "/root/Documents/mg400-data",
    }
    startup_arguments = (
        [] if custom_startup else ["--exec", str(project / "apps/isaacsim/startup.py")]
    )
    assert record["args"] == [
        "--/exts/isaacsim.core.throttling/enable_async=false",
        "--/app/asyncRendering=false",
        "--/app/asyncRenderingLowLatency=false",
        *startup_arguments,
        *extra_arguments,
    ]
