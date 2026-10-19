"""Check panel imports in separate processes without Isaac Sim or hardware."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IMPORT_CHECK = """
import asyncio
import builtins
import importlib
import os
import runpy
import shutil
import socket
import sys
import threading
from pathlib import Path

root = Path(sys.argv[1])
assert "mg400_agent" not in sys.modules
old_package = old_feedback = None
if len(sys.argv) > 2:
    sys.path.insert(0, str(root / "src"))
    import mg400_agent as old_package
    import mg400_agent.feedback as old_feedback
    package_dir = root / "src/mg400_agent"
    assert Path(old_package.__file__).parent == package_dir
    assert not (package_dir / "joint_control.py").exists()
    try:
        importlib.import_module("mg400_agent.joint_control")
    except ModuleNotFoundError as error:
        assert error.name == "mg400_agent.joint_control"
    else:
        raise AssertionError("The control module must be absent before deployment")
    directory_stat = package_dir.stat()
    shutil.copyfile(sys.argv[2], package_dir / "joint_control.py")
    os.utime(package_dir, ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns))
    assert importlib.util.find_spec("mg400_agent.joint_control") is None

def forbidden(*args, **kwargs):
    raise AssertionError("Import must not open sockets or start asynchronous tasks")

socket.socket = forbidden
socket.create_connection = forbidden
asyncio.ensure_future = forbidden
asyncio.create_task = forbidden
threading.Thread.start = forbidden
namespace = runpy.run_path(
    str(root / "apps/isaacsim/joint_control.py"),
    run_name="test_isaac_control_import",
)
control = namespace["JointControl"]
module = sys.modules[control.__module__]
assert Path(module.__file__).resolve() == root / "src/mg400_agent/joint_control.py"
jog_module = sys.modules[namespace["KeyboardJogSession"].__module__]
assert Path(jog_module.__file__).resolve() == root / "src/mg400_agent/keyboard_jog.py"
assert "MG400_CONTROL_START_TASK" not in namespace
assert "omni.ui" not in sys.modules
assert not hasattr(builtins, namespace["SINGLETON"])
assert not hasattr(builtins, namespace["GRIPPER_SINGLETON"])
assert namespace["REPORT"] == Path(os.environ["MG400_DATA_DIR"]) / "isaacsim/control_status.json"
if old_package is not None:
    assert sys.modules["mg400_agent"] is old_package
    assert sys.modules["mg400_agent.feedback"] is old_feedback
    assert control.read_state.__annotations__["return"] is old_feedback.Feedback
"""


def check_import(root, data_directory, *paths):
    result = subprocess.run(
        [sys.executable, "-B", "-c", IMPORT_CHECK, str(root), *map(str, paths)],
        capture_output=True,
        text=True,
        env={**os.environ, "MG400_DATA_DIR": str(data_directory)},
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_panel_import_in_fresh_process(tmp_path):
    check_import(ROOT, tmp_path / "external-data")


def test_panel_import_discovers_module_added_after_failed_import(tmp_path):
    package_dir = tmp_path / "src/mg400_agent"
    package_dir.mkdir(parents=True)
    for filename in (
        "__init__.py",
        "feedback.py",
        "joint_limits.py",
        "config.py",
        "controller_protocol.py",
        "robot_config.py",
        "runtime_paths.py",
        "keyboard_jog.py",
    ):
        shutil.copyfile(ROOT / "src/mg400_agent" / filename, package_dir / filename)
    apps = tmp_path / "apps/isaacsim"
    apps.mkdir(parents=True)
    shutil.copyfile(ROOT / "apps/isaacsim/joint_control.py", apps / "joint_control.py")
    check_import(tmp_path, tmp_path / "external-data", ROOT / "src/mg400_agent/joint_control.py")
