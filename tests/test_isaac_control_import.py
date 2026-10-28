"""Check panel imports in separate processes without Isaac Sim or hardware."""

import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
        "controller_alarms.py",
        "robot_config.py",
        "runtime_paths.py",
        "keyboard_jog.py",
        "keyboard_control.py",
    ):
        shutil.copyfile(ROOT / "src/mg400_agent" / filename, package_dir / filename)
    apps = tmp_path / "apps/isaacsim"
    apps.mkdir(parents=True)
    shutil.copyfile(ROOT / "apps/isaacsim/joint_control.py", apps / "joint_control.py")
    check_import(tmp_path, tmp_path / "external-data", ROOT / "src/mg400_agent/joint_control.py")


@pytest.mark.parametrize("cache", ["modules", "legacy_path", "bytecode", "loader", "control_only"])
def test_control_source_reload_replaces_legacy_code_and_preserves_feedback(tmp_path, cache):
    root = tmp_path / "deployed"
    legacy = tmp_path / "legacy" if cache == "legacy_path" else root
    old_sources = {
        "controller_protocol.py": "def parse_controller_response(*args): pass\n",
        "joint_control.py": ("class JointControl:\n    def __init__(self, host, snapshot): pass\n"),
        "keyboard_jog.py": "class KeyboardJogSession: pass\n",
        "keyboard_control.py": (
            "class KeyboardController: pass\n"
            "class GripperUnavailableError(RuntimeError): pass\n"
            "async def run_keyboard_hold(*args, **kwargs): pass\n"
        ),
        "gripper_feedback.py": "class FeedbackState: pass\n",
    }
    if cache == "control_only":
        old_sources["joint_control.py"] += "class TargetRejectedError(ValueError): pass\n"
    for directory in {root, legacy}:
        package_dir = directory / "src/mg400_agent"
        package_dir.mkdir(parents=True)
        for filename in (
            "__init__.py",
            "feedback.py",
            "joint_limits.py",
            "config.py",
            "controller_alarms.py",
            "robot_config.py",
            "runtime_paths.py",
            *old_sources,
        ):
            shutil.copyfile(ROOT / "src/mg400_agent" / filename, package_dir / filename)
    for filename, source in old_sources.items():
        path = legacy / "src/mg400_agent" / filename
        if cache == "bytecode":
            # A valid timestamp cache checks source size and whole-second mtime.
            source += "#" * (path.stat().st_size - len(source.encode()))
        path.write_text(source)
        if cache == "bytecode":
            py_compile.compile(
                path, doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP
            )
    apps = root / "apps/isaacsim"
    apps.mkdir(parents=True)
    shutil.copyfile(ROOT / "apps/isaacsim/joint_control.py", apps / "joint_control.py")
    script = """
import asyncio
import importlib
import inspect
import os
import runpy
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

root, legacy, source = map(Path, sys.argv[1:4])
cache = sys.argv[4]
sys.path.insert(0, str(legacy / "src"))
package = importlib.import_module("mg400_agent")
feedback = importlib.import_module("mg400_agent.feedback")
old_control = importlib.import_module("mg400_agent.joint_control")
old_class = old_control.JointControl
old_jog = (
    None if cache == "control_only"
    else importlib.import_module("mg400_agent.keyboard_jog").KeyboardJogSession
)
old_gripper = importlib.import_module("mg400_agent.gripper_feedback").FeedbackState
old_keyboard = importlib.import_module("mg400_agent.keyboard_control").KeyboardController
importlib.import_module("mg400_agent.controller_protocol")
assert hasattr(old_control, "TargetRejectedError") is (cache == "control_only")
assert not hasattr(old_control, "PoseMeasurement")
assert "parent" not in inspect.signature(old_class).parameters
names = (
    "controller_protocol", "joint_control", "keyboard_jog", "keyboard_control", "gripper_feedback"
)
old_sources = {
    name: (legacy / "src/mg400_agent" / f"{name}.py").read_bytes() for name in names
}
for name in names:
    path = root / "src/mg400_agent" / f"{name}.py"
    previous = path.stat()
    shutil.copyfile(source / "src/mg400_agent" / path.name, path)
    if cache == "bytecode":
        assert previous.st_size == path.stat().st_size
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))

if cache == "loader":
    class CachedLoader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            return compile(old_sources[fullname.rsplit(".", 1)[1]], self.path, "exec")

    class CachedFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {f"mg400_agent.{name}" for name in names}:
                filename = root / "src/mg400_agent" / f"{fullname.rsplit('.', 1)[1]}.py"
                loader = CachedLoader(fullname, str(filename))
                return importlib.util.spec_from_loader(fullname, loader)

    sys.meta_path.insert(0, CachedFinder())

if cache in {"bytecode", "loader"}:
    # Confirm this fixture reproduces stale code even though the source is current.
    assert "parent" in (root / "src/mg400_agent/joint_control.py").read_text()
    old_control = importlib.reload(old_control)
    old_class = old_control.JointControl
    assert "parent" not in inspect.signature(old_class).parameters
if cache == "legacy_path":
    assert Path(package.__path__[0]) == legacy / "src/mg400_agent"

def forbidden(*args, **kwargs):
    raise AssertionError("Loading modules must not connect to hardware or start tasks")

socket.socket = socket.create_connection = forbidden
asyncio.ensure_future = asyncio.create_task = forbidden
threading.Thread.start = forbidden
namespace = runpy.run_path(str(root / "apps/isaacsim/joint_control.py"), run_name="test_import")
assert namespace["JointControl"] is old_class
if cache == "control_only":
    assert namespace["KeyboardJogSession"] is not None
    assert not hasattr(old_control, "PoseMeasurement")
else:
    assert namespace["KeyboardJogSession"] is old_jog
namespace["_reload_control_modules"]()
state = namespace["_reload_control_modules"].__globals__
assert state["JointControl"] is not old_class
assert state["KeyboardJogSession"] is not old_jog
assert state["KeyboardController"] is not old_keyboard
keyboard = sys.modules["mg400_agent.keyboard_control"]
assert state["run_keyboard_hold"] is keyboard.run_keyboard_hold
assert state["GripperUnavailableError"] is keyboard.GripperUnavailableError
assert sys.modules["mg400_agent.gripper_feedback"].FeedbackState is not old_gripper
assert state["backend"].TargetRejectedError.__module__ == "mg400_agent.joint_control"
protocol = sys.modules["mg400_agent.controller_protocol"]
assert state["backend"].ControllerCommandError is protocol.ControllerCommandError
jog = sys.modules["mg400_agent.keyboard_jog"]
assert jog.joint_control.JointControl is state["JointControl"]
assert jog.TargetRejectedError is state["backend"].TargetRejectedError
assert sys.modules["mg400_agent"] is package
assert sys.modules["mg400_agent.feedback"] is feedback
assert state["JointControl"].read_state.__annotations__["return"] is feedback.Feedback
for name in names:
    module = sys.modules[f"mg400_agent.{name}"]
    expected_path = root / "src/mg400_agent" / f"{name}.py"
    assert Path(module.__file__).resolve() == expected_path
    assert Path(module.__spec__.origin).resolve() == expected_path
    assert getattr(package, name) is module
for function, name in (
    (protocol.parse_controller_response, "controller_protocol"),
    (state["JointControl"].__init__, "joint_control"),
    (jog.KeyboardJogSession.prepare, "keyboard_jog"),
):
    assert Path(function.__code__.co_filename) == root / "src/mg400_agent" / f"{name}.py"

control_class = state["JointControl"]
sample = feedback.Feedback((0.0, 0.0, 0.0, 0.0), 5, 0, False, speed_scaling_raw=1)
snapshot = lambda: (sample, time.monotonic())
parent = control_class("robot.test", snapshot)
child = control_class("robot.test", snapshot, parent=parent, command_timeout=0.5)
assert child._parent is parent
assert child._command_timeout == 0.5
assert child._operation_lock is parent._operation_lock
assert child.read_state() is sample
prepared = []
pose = (250.0, 0.0, 50.0, 0.0)
def preflight(active, modes):
    assert type(active) is control_class
    assert active._parent is parent
    assert active._command_timeout == 0.5
    assert active._operation_lock is parent._operation_lock
    assert active.read_state() is sample
    assert modes == {5}
    prepared.append(active)
    return sample, sample.joints_deg
control_class._preflight = preflight
def set_speed(active, command, **kwargs):
    assert command == "SpeedFactor(1)"
    assert kwargs == dict(modes={5}, send_modes={5}, expected=sample.joints_deg)
control_class._guarded_command = set_speed
control_class._read_pose_measurement = lambda active, modes: state["backend"].PoseMeasurement(
    control=active, pose=pose, requested_at=time.monotonic(), before=sample, after=sample
)
session = state["KeyboardJogSession"](parent)
assert session.prepare() == pose
assert len(prepared) == 1
assert session.ready
assert not session.busy
assert not parent.busy
assert not parent.stopped
assert "omni.ui" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(root), str(legacy), str(ROOT), cache],
        capture_output=True,
        text=True,
        env={**os.environ, "MG400_DATA_DIR": str(tmp_path / "external-data")},
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_panel_import_preserves_cached_limits_until_safe_reload(tmp_path):
    script = """
import asyncio
import importlib
import runpy
import socket
import sys
import threading
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
limits = importlib.import_module("mg400_agent.joint_limits")
old_validate = limits.validate_joints
old_measured = limits.validate_measured_joints
assert "mg400_agent.joint_control" not in sys.modules

def forbidden(*args, **kwargs):
    raise AssertionError("Import and reload must not start tasks or connect to hardware")

socket.socket = socket.create_connection = forbidden
asyncio.ensure_future = asyncio.create_task = forbidden
threading.Thread.start = forbidden
namespace = runpy.run_path(str(root / "apps/isaacsim/joint_control.py"), run_name="test_import")
assert limits.validate_joints is old_validate
assert limits.validate_measured_joints is old_measured
assert namespace["backend"].validate_joints is old_validate
assert "MG400_CONTROL_START_TASK" not in namespace

namespace["_reload_control_modules"]()
state = namespace["_reload_control_modules"].__globals__
assert state["limits"] is limits
assert state["backend"].validate_joints is limits.validate_joints
assert state["backend"].validate_measured_joints is limits.validate_measured_joints
assert sys.modules["mg400_agent.keyboard_jog"].validate_joints is limits.validate_joints
assert (
    sys.modules["mg400_agent.keyboard_jog"].validate_measured_joints
    is limits.validate_measured_joints
)
assert limits.validate_joints is not old_validate
assert limits.validate_measured_joints is not old_measured
assert limits.validate_joints((0, 0, 0, -220)) == (0, 0, 0, -220)
assert limits.validate_measured_joints((0, 0, 0, -359)) == (0, 0, 0, -359)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT)],
        capture_output=True,
        text=True,
        env={**os.environ, "MG400_DATA_DIR": str(tmp_path / "external-data")},
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
