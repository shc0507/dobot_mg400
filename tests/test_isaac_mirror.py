"""Exercise the live mirror's actual USD poses and passive reconnect worker."""

import asyncio
import builtins
import contextlib
import json
import math
import runpy
import socket
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: E402

from mg400_agent.controller_ownership import ControllerOwnership  # noqa: E402
from mg400_agent.feedback import Feedback  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "apps/isaacsim/mirror.py"), run_name="test_mirror")
UsdPoseMirror = MODULE["UsdPoseMirror"]
LiveMirror = MODULE["LiveMirror"]


@pytest.fixture(autouse=True)
def isolated_controller_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path / "controller-locks"))


def test_start_rejects_competing_panel_before_feedback_or_scene_changes(monkeypatch):
    owner = ControllerOwnership("192.168.1.6")
    owner.acquire()
    monkeypatch.setattr(builtins, MODULE["SINGLETON"], None, raising=False)
    monkeypatch.setattr(builtins, MODULE["SINGLETON"] + "_start_lock", None, raising=False)
    receiver = Mock(side_effect=AssertionError("No feedback before ownership"))
    monkeypatch.setattr(LiveMirror, "run", receiver)
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            asyncio.run(MODULE["start"](show_window=False))
        receiver.assert_not_called()
        assert getattr(builtins, MODULE["SINGLETON"]) is None
    finally:
        owner.release()


@pytest.mark.parametrize("scene", ("isaacsim/scene.usda", "00_OPEN_MG400.usda"))
@pytest.mark.parametrize("joints", ((0, 0, 0, 0), (20, 15, 35, 25), (-90, 45, 60, -30)))
def test_authored_live_pose_matches_independent_cad_fk_without_editing_source(scene, joints):
    stage = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(ROOT / scene)), Sdf.Layer.CreateAnonymous())
    before = {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    edit_target = stage.GetEditTarget()
    mirror = UsdPoseMirror(stage)
    mirror.update(joints)
    mirror.update(joints)  # Rerun uses the existing transform op, never appends a second one.
    j1, j2, j3, j4 = map(math.radians, joints)
    radial = (
        0.1095
        - 0.0010512570171516 * math.cos(j2)
        + 0.175001164344716 * math.sin(j2)
        + 0.175 * math.cos(j3)
        + 0.00325 * math.sin(j3)
    )
    lateral = -0.0000001243781102
    height = (
        0.175
        + 0.0010512570171516 * math.sin(j2)
        + 0.175001164344716 * math.cos(j2)
        - 0.175 * math.sin(j3)
        + 0.00325 * math.cos(j3)
    )
    expected = (
        -0.005 + radial * math.cos(j1) - lateral * math.sin(j1),
        radial * math.sin(j1) + lateral * math.cos(j1),
        height,
    )
    flange = stage.GetPrimAtPath("/World/MG400/mg400_end_effector_flange")
    pose = UsdGeom.XformCache().GetLocalToWorldTransform(flange)
    assert pose.ExtractTranslation() == pytest.approx(expected, abs=3e-8)
    rotation = Gf.Matrix3d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(j1 + j4)))
    assert np.array(pose.ExtractRotationMatrix()) == pytest.approx(np.array(rotation), abs=3e-7)
    assert stage.GetEditTarget() == edit_target
    assert before == {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    assert len(UsdGeom.Xformable(flange).GetOrderedXformOps()) == 1


def test_live_arm_changes_hold_measured_jaw_opening():
    stage = Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "00_OPEN_MG400.usda")), Sdf.Layer.CreateAnonymous()
    )
    mirror = UsdPoseMirror(stage, {"pgea_left_slide": 0.001, "pgea_right_slide": 0.003})
    for joints in ((0, 0, 0, 0), (60, 30, 40, 20), (-30, 10, 60, -50)):
        mirror.update(joints)
        cache = UsdGeom.XformCache()
        housing = cache.GetLocalToWorldTransform(stage.GetPrimAtPath("/World/MG400/pgea_housing"))
        centers = []
        for side in ("left", "right"):
            fingertip = stage.GetPrimAtPath(f"/World/MG400/pgea_{side}_jaw/pgea_fingertip_{side}")
            world = cache.GetLocalToWorldTransform(fingertip)
            centers.append(np.array(world.ExtractTranslation()))
        y_axis = np.array(housing.ExtractRotationMatrix())[1]
        gap = np.dot(centers[1] - centers[0], y_axis) - 0.009
        assert gap == pytest.approx(0.0119 - 0.001 - 0.003, abs=3e-8)
    restarted = UsdPoseMirror(stage)
    assert restarted.jaws == pytest.approx(mirror.jaws, abs=3e-8)


def test_worker_reconnects_on_invalid_frame_and_receives_without_sending(monkeypatch, tmp_path):
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "robot-first.test"\n')
    monkeypatch.setitem(LiveMirror.__init__.__globals__, "ROBOT_CONFIG", config)
    mirror = LiveMirror()
    config.write_text('[robot]\nhost = "robot-second.test"\n')
    received = threading.Event()
    attempts = []
    feedback = object()

    class Connection:
        def __init__(self, host, timeout_s):
            attempts.append((host, timeout_s))

        def __enter__(self):
            if len(attempts) == 1:
                raise ValueError("Bad feedback magic")
            return self

        def __exit__(self, *args):
            pass

        def read_latest(self):
            mirror.stop_event.set()
            received.set()
            return feedback

    monkeypatch.setitem(LiveMirror.receive.__globals__, "FeedbackConnection", Connection)
    began = time.monotonic()
    worker = threading.Thread(target=mirror.receive)
    worker.start()
    assert received.wait(3)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert attempts == [("robot-first.test", 1.0)] * 2
    assert mirror.host == "robot-first.test"
    assert mirror.report["source"] == "robot-first.test:30004"
    assert mirror.received == 1
    assert mirror.latest[0] is feedback
    assert began <= mirror.latest[1] <= time.monotonic()
    assert mirror.network_error is None


@pytest.mark.parametrize("invalid", ("missing", "invalid"))
def test_invalid_configuration_prevents_mirror_connections(monkeypatch, tmp_path, invalid):
    config = tmp_path / "robot.toml"
    if invalid == "invalid":
        config.write_text('[robot]\nhost = ""\n')
    monkeypatch.setitem(LiveMirror.__init__.__globals__, "ROBOT_CONFIG", config)
    connection = Mock(side_effect=AssertionError("No network"))
    monkeypatch.setattr(socket, "create_connection", connection)
    with pytest.raises((FileNotFoundError, ValueError)):
        LiveMirror()
    connection.assert_not_called()


def install_mirror_ui(monkeypatch, stage):
    modules = {
        name: ModuleType(name)
        for name in (
            "omni",
            "omni.kit",
            "omni.kit.app",
            "omni.timeline",
            "omni.ui",
            "omni.usd",
            "isaacsim",
            "isaacsim.core",
            "isaacsim.core.simulation_manager",
        )
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attribute = name.rsplit(".", 1)
            setattr(modules[parent], attribute, module)

    async def update():
        await asyncio.sleep(0.001)

    modules["omni.kit.app"].get_app = lambda: SimpleNamespace(next_update_async=update)
    modules["omni.usd"].get_context = lambda: SimpleNamespace(get_stage=lambda: stage)
    modules["omni.timeline"].get_timeline_interface = lambda: SimpleNamespace(
        is_stopped=lambda: True,
        stop=Mock(),
    )
    modules["isaacsim.core.simulation_manager"].SimulationManager = SimpleNamespace(
        get_physics_simulation_view=Mock(side_effect=AssertionError("No physics view")),
    )
    window = SimpleNamespace(
        frame=contextlib.nullcontext(),
        visible=True,
        setPosition=Mock(),
        destroy=Mock(),
    )
    ui = modules["omni.ui"]
    ui.Window = Mock(return_value=window)
    ui.VStack = lambda **kwargs: contextlib.nullcontext()
    ui.Label = Mock(side_effect=lambda text, **kwargs: SimpleNamespace(text=text))
    ui.Button = Mock()
    ui.Workspace = SimpleNamespace(get_window=Mock(return_value=None))
    return ui


@pytest.mark.parametrize("show_window", [True, False])
@pytest.mark.parametrize("control_owned", [True, False])
def test_run_preserves_feedback_without_visible_window_and_waits_for_receiver(
    monkeypatch, tmp_path, show_window, control_owned
):
    stage = Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "00_OPEN_MG400.usda")), Sdf.Layer.CreateAnonymous()
    )
    ui = install_mirror_ui(monkeypatch, stage)
    report = tmp_path / "mirror_status.json"
    monkeypatch.setitem(LiveMirror.run.__globals__, "REPORT", report)
    monkeypatch.setitem(LiveMirror.run.__globals__, "export_scene", Mock(return_value=True))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    mirror = LiveMirror(show_window=show_window)
    if control_owned:
        mirror.control_owner = object()
    contender = ControllerOwnership(mirror.host)
    entered, release, exited = (threading.Event() for _ in range(3))

    def receive():
        entered.set()
        try:
            assert release.wait(3)
        finally:
            exited.set()

    mirror.receive = Mock(side_effect=receive)
    mirror.stop = Mock(wraps=mirror.stop)

    async def wait_applied(count):
        while mirror.report["applied_frames"] < count:
            await asyncio.sleep(0.001)

    async def exercise():
        mirror.latest = (Feedback((0, 0, 0, 0), 5, 0, False), time.monotonic(), 1)
        mirror.received = 1
        mirror.task = asyncio.create_task(mirror.run())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.wait_for(wait_applied(1), 1)
            if show_window:
                assert mirror.window.visible
                mirror.window.visible = False
            else:
                assert mirror.window is None
                ui.Window.assert_not_called()
                ui.Label.assert_not_called()
            pose_mirror = mirror.pose_mirror
            pose_mirror.jaws.update(pgea_left_slide=0.002, pgea_right_slide=0.002)
            mirror.latest = (Feedback((1, 0, 0, 0), 5, 0, False), time.monotonic(), 2)
            mirror.received = 2
            await asyncio.wait_for(wait_applied(2), 1)
            assert mirror.pose_mirror is pose_mirror
            assert pose_mirror.jaws["pgea_left_slide"] == 0.002
            mirror.stop.assert_not_called()
            assert not mirror.stop_event.is_set()
            assert mirror.report["status"] == "live"
            mirror.stop("Test finished")
            await asyncio.sleep(0.01)
            assert not mirror.task.done()
            assert not exited.is_set()
            with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
                contender.acquire()
        finally:
            mirror.stop_event.set()
            release.set()
            await asyncio.wait_for(mirror.task, 1)
        assert exited.is_set()
        if control_owned:
            with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
                contender.acquire()
            mirror.ownership.release()
        contender.acquire()
        contender.release()

    asyncio.run(exercise())
    mirror.receive.assert_called_once_with()
    assert ui.Window.call_count == int(show_window)
    assert json.loads(report.read_text())["status"] == "stopped"
    socket.create_connection.assert_not_called()


def test_concurrent_starts_wait_for_old_receiver_and_keep_one_instance(monkeypatch):
    singleton = MODULE["SINGLETON"]
    lock_name = singleton + "_start_lock"
    monkeypatch.delattr(builtins, lock_name, raising=False)
    monkeypatch.setattr(builtins, singleton, None, raising=False)
    instances, active, peak = [], [0], [0]

    class Mirror:
        def __init__(self, show_window=True):
            self.ownership = ControllerOwnership("robot.test")
            self.show_window = show_window
            self.stop_event = threading.Event()
            self.task = None
            self.window = SimpleNamespace(destroy=Mock())
            instances.append(self)

        def stop(self, reason):
            self.stop_event.set()

        async def run(self):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            try:
                while not self.stop_event.is_set():
                    await asyncio.sleep(0)
            finally:
                active[0] -= 1
                self.ownership.release()

    monkeypatch.setitem(MODULE["start"].__globals__, "LiveMirror", Mirror)

    async def exercise():
        old = Mirror()
        instances.clear()
        stopped, release = asyncio.Event(), asyncio.Event()

        async def old_receiver():
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            stopped.set()
            await release.wait()
            active[0] -= 1

        old.task = asyncio.create_task(old_receiver())
        setattr(builtins, singleton, old)
        await stopped.wait()
        starts = [
            asyncio.create_task(MODULE["start"](show_window=value)) for value in (False, True)
        ]
        try:
            await asyncio.sleep(0)
            assert old.stop_event.is_set()
            assert not instances
            old.window.destroy.assert_not_called()
            release.set()
            await asyncio.wait_for(asyncio.gather(*starts), 1)
            current = getattr(builtins, singleton)
            assert [mirror.show_window for mirror in instances] == [False, True]
            assert current is instances[-1]
            assert old.task.done() and instances[0].task.done()
            old.window.destroy.assert_called_once_with()
            instances[0].window.destroy.assert_called_once_with()
            current.window.destroy.assert_not_called()
        finally:
            release.set()
            for mirror in instances:
                mirror.stop("Test finished")
            await asyncio.wait_for(asyncio.gather(*starts), 1)
            await asyncio.gather(*(mirror.task for mirror in instances))
            await old.task
        assert active[0] == 0

    try:
        asyncio.run(exercise())
        assert peak[0] == 1
    finally:
        delattr(builtins, lock_name)
