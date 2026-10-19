"""Exercise the real-control panel without Isaac Sim or robot connections."""

import asyncio
import builtins
import json
import runpy
import socket
import sys
import threading
import time
from dataclasses import replace
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from mg400_agent.feedback import Feedback

ROOT = Path(__file__).resolve().parents[1]


class FloatModel:
    def __init__(self, value):
        self.as_float = value

    def set_value(self, value):
        self.as_float = value


class Mirror:
    def __init__(self, sample):
        self.host = "192.168.1.6"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.network_error = None
        self.latest = (sample, time.monotonic())
        self.report = {"status": "live", "display_hz": 24.5, "feedback_age_ms": 10.0}
        self.task = None
        self.window = SimpleNamespace(visible=True, destroyed=False)
        self.window.destroy = lambda: setattr(self.window, "destroyed", True)
        self.stop_calls = []

    def stop(self, reason="Stopped by user"):
        self.stop_calls.append(reason)
        self.report["status"] = "stopped"
        self.stop_event.set()


@pytest.fixture
def panel_module(monkeypatch, tmp_path):
    instances = []
    sessions = []
    grippers = []
    gripper_loads = []
    reload_calls = []

    class Control:
        def __init__(self, host, snapshot):
            self.host = host
            self.snapshot = snapshot
            self.stopped = self.busy = False
            self.calls = []
            self.move_started = threading.Event()
            self.release_move = threading.Event()
            self.disable_finished = threading.Event()
            self.cancel_modes = []
            self.pose = (1.0, 2.0, 3.0, 4.0)
            self.pose_reads = 0
            instances.append(self)

        def read_state(self):
            return self.snapshot()[0]

        def read_pose(self):
            self.read_state()
            self.pose_reads += 1
            return self.pose

        def read_pose_measurement(self):
            requested_at = time.monotonic()
            before = self.read_state()
            pose = self.read_pose()
            return SimpleNamespace(
                control=self, pose=pose, requested_at=requested_at,
                before=before, after=self.read_state(),
            )

        def enable(self):
            self.calls.append(("enable",))
            return self.set_mode(5)

        def set_mode(self, mode):
            sample = replace(self.snapshot()[0], mode=mode)
            self.snapshot.__self__.mirror.latest = sample, time.monotonic()
            return sample

        def disable(self):
            self.calls.append(("disable",))
            sample = self.set_mode(4)
            self.disable_finished.set()
            return sample

        def move(self, target):
            self.calls.append(("move", target))
            self.wait_for_move()

        def move_linear(self, target):
            self.calls.append(("move_linear", target))
            self.wait_for_move()
            self.pose = target

        def wait_for_move(self):
            self.busy = True
            self.move_started.set()
            try:
                assert self.release_move.wait(3), "Test did not release the move worker"
                if self.stopped:
                    raise RuntimeError("Control cancelled")
            finally:
                self.busy = False

        def stop(self):
            self.calls.append(("stop",))
            self.stopped = True
            return {"ok": True}

        def cancel(self, disabling=False):
            self.cancel_modes.append(disabling)
            self.stopped = True

    backend = ModuleType("mg400_agent.joint_control")
    backend.JointControl = Control
    backend.TargetRejectedError = type("TargetRejectedError", (ValueError,), {})
    monkeypatch.setitem(sys.modules, backend.__name__, backend)

    class KeyboardSession:
        def __init__(self, control):
            self.control = control
            self.ready = self.closed = False
            self.expired = False
            self.fault = None
            self.limit_reason = None
            self.measured_pose = None
            self.prepare_started = asyncio.Event()
            self.prepare_allowed = asyncio.Event()
            self.prepare_allowed.set()
            self.run_started = asyncio.Event()
            self.hold_finished = asyncio.Event()
            self.finish_allowed = asyncio.Event()
            self.finish_allowed.set()
            self.run_calls = []
            self.initial_poses = []
            self.movements = []
            self.release_calls = 0
            self.released = None
            sessions.append(self)

        async def prepare(self):
            self.prepare_started.set()
            await self.prepare_allowed.wait()
            if self.control.read_state().mode != 5:
                raise RuntimeError("Robot must be enabled and idle")
            if self.closed:
                raise RuntimeError("Keyboard session closed")
            self.ready = True
            return self.control.pose

        async def run(self, key, released, initial_pose=None):
            self.run_calls.append(key)
            self.initial_poses.append(initial_pose)
            self.released = released
            self.expired = False
            if released.is_set() or self.closed:
                return self.control.pose
            self.movements.append(key)
            self.run_started.set()
            while not released.is_set() and not self.hold_finished.is_set():
                await asyncio.sleep(0)
            self.expired = self.hold_finished.is_set() and not released.is_set()
            await self.finish_allowed.wait()
            return self.control.pose

        def release(self):
            self.release_calls += 1
            if self.released is not None:
                self.released.set()

        def close(self):
            self.closed = True
            self.ready = False
            self.release()

    jog_backend = ModuleType("mg400_agent.keyboard_jog")
    jog_backend.KeyboardJogSession = KeyboardSession
    monkeypatch.setitem(sys.modules, jog_backend.__name__, jog_backend)
    real_to_thread = asyncio.to_thread

    async def to_thread(function, *args, **kwargs):
        # Model the blocking session contract without starting hardware workers.
        if isinstance(getattr(function, "__self__", None), KeyboardSession):
            return await function(*args, **kwargs)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def no_network(*args, **kwargs):
        pytest.fail("Panel tests must never connect to the real robot")

    monkeypatch.setattr(socket, "create_connection", no_network)

    def no_startup_task(*args, **kwargs):
        pytest.fail("Importing the panel must never schedule a task")

    with monkeypatch.context() as import_patch:
        import_patch.setattr(asyncio, "ensure_future", no_startup_task)
        namespace = runpy.run_path(
            str(ROOT / "apps/isaacsim/joint_control.py"), run_name="test_isaac_control"
        )
    panel_class = namespace["ControlPanel"]
    monkeypatch.setitem(
        panel_class.run.__globals__, "_reload_control_modules",
        lambda: reload_calls.append("reload"),
    )

    class Gripper:
        def __init__(self, feedback_path=None):
            self.feedback_path = feedback_path
            self.owner = None
            self.closed = self.closing = self.failed = False
            self.action = self.task = None
            self.calls = []
            self.windows = []
            self.build_count = 0
            self.run_started = asyncio.Event()
            self.run_finished = asyncio.Event()
            grippers.append(self)

        @property
        def busy(self):
            return self.action is not None and not self.action.done()

        def build_ui(self):
            import omni.ui as ui

            self.build_count += 1
            ui.Label("Embedded gripper")

        async def run(self, window=None):
            self.windows.append(window)
            self.run_started.set()
            try:
                while not self.closed and not self.closing and window.visible:
                    await asyncio.sleep(0)
            finally:
                if self.action:
                    await self.action
                self.closed = True
                self.run_finished.set()

    real_run_path = runpy.run_path

    def load_script(path, *args, **kwargs):
        if Path(path).name == "gripper_control.py":
            gripper_loads.append((path, args, kwargs))
            return {"GripperPanel": Gripper}
        return real_run_path(path, *args, **kwargs)

    monkeypatch.setitem(panel_class.run.__globals__, "runpy", SimpleNamespace(run_path=load_script))
    monkeypatch.setitem(
        panel_class.run.__globals__, "data_path", lambda name: tmp_path / "data" / name
    )
    monkeypatch.setitem(panel_class.run.__globals__, "REPORT", tmp_path / "status.json")
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "192.168.1.6"\n')
    monkeypatch.setitem(panel_class.__init__.__globals__, "ROBOT_CONFIG", config)
    monkeypatch.setattr(builtins, namespace["SINGLETON"] + "_start_lock", None, raising=False)
    monkeypatch.setattr(
        builtins, namespace["GRIPPER_SINGLETON"] + "_start_lock", None, raising=False
    )
    monkeypatch.setattr(builtins, namespace["GRIPPER_SINGLETON"], None, raising=False)
    return SimpleNamespace(
        **namespace, instances=instances, sessions=sessions, config_path=config,
        grippers=grippers, Gripper=Gripper, gripper_loads=gripper_loads,
        reload_calls=reload_calls,
        feedback_path=tmp_path / "data/work/gripper_olares_feedback.json",
    )


@pytest.fixture
def panel(panel_module):
    sample = Feedback((1.0, 2.0, 3.0, 4.0), mode=5, timestamp=0, error_status=False)
    mirror = Mirror(sample)
    panel = panel_module.ControlPanel(mirror)
    panel.models = [FloatModel(0.0) for _ in range(4)]
    panel.actual_pose = panel.control.read_pose()
    panel.pose_received = time.monotonic()
    return panel


async def arm_keyboard(panel):
    panel.request_keyboard()
    assert panel.keyboard_arming
    assert not panel.keyboard_enabled
    await asyncio.wait_for(panel.keyboard_task, 1)
    assert panel.keyboard_enabled
    assert not panel.keyboard_arming
    return panel.keyboard_session


def install_ui(panel_module, panel, monkeypatch, next_update):
    widgets = []
    containers = []

    class Widget:
        def __init__(self, text=None, *args, **kwargs):
            self.text = text
            self.visible = kwargs.get("visible", True)
            self.enabled = kwargs.get("enabled", True)
            self.clicked_fn = kwargs.get("clicked_fn")
            self.parent = containers[-1] if containers else None
            self.destroyed = False
            self.focused = True
            self.exclusive_keyboard = False
            self.key_pressed = None
            self.frame = self
            widgets.append(self)

        def __enter__(self):
            containers.append(self)
            return self

        def __exit__(self, *args):
            containers.pop()

        def setPosition(self, *args):
            pass

        def set_focused_changed_fn(self, callback):
            self.focused_changed = callback

        def set_key_pressed_fn(self, callback):
            self.key_pressed = callback

        def set_focus(self, focused):
            self.focused = focused
            self.focused_changed(focused)

        def destroy(self):
            self.destroyed = True

    class InputInterface:
        def __init__(self):
            self.callback = None
            self.keyboard = object()
            self.subscription = object()
            self.subscribed = []
            self.unsubscribed = []
            self.window_events = []
            self.global_events = []

        def subscribe_to_keyboard_events(self, keyboard, callback):
            assert keyboard is self.keyboard
            self.subscribed.append((keyboard, callback))
            self.callback = callback
            return self.subscription

        def unsubscribe_to_keyboard_events(self, keyboard, subscription):
            self.unsubscribed.append((keyboard, subscription))
            self.callback = None

        def emit(self, key, event_type="press", modifiers=0):
            assert type(key) is int
            assert event_type in ("press", "release", "repeat")
            window = panel.window
            if window.visible and window.focused and not window.destroyed:
                exclusive = window.exclusive_keyboard
                if window.key_pressed is not None:
                    event = (key, modifiers, event_type != "release")
                    self.window_events.append(event)
                    window.key_pressed(*event)
                if exclusive:
                    return False
            self.global_events.append((key, modifiers, event_type))
            if self.callback is not None:
                return self.callback(SimpleNamespace(
                    input=KeyboardInput(key), type=event_type, modifiers=modifiers
                ))
            return True

    class KeyboardInput(Enum):
        # Carbonite InputTypes.h values. Window callbacks receive their integer values.
        A = 19
        D = 22
        E = 23
        F = 24
        Q = 35
        R = 36
        S = 37
        W = 41
        X = 42
        Z = 44
        ESCAPE = 49

        def __int__(self):
            return self.value

    modules = {
        name: ModuleType(name)
        for name in (
            "carb", "carb.input", "omni", "omni.appwindow", "omni.kit", "omni.kit.app",
            "omni.timeline", "omni.ui", "omni.usd",
        )
    }
    input_interface = InputInterface()
    modules["carb"].input = modules["carb.input"]
    modules["carb.input"].acquire_input_interface = lambda: input_interface
    modules["carb.input"].KeyboardInput = KeyboardInput
    modules["carb.input"].KeyboardEventType = SimpleNamespace(
        KEY_PRESS="press", KEY_RELEASE="release", KEY_REPEAT="repeat"
    )
    modules["omni"].kit = modules["omni.kit"]
    modules["omni.kit"].app = modules["omni.kit.app"]
    for name in ("appwindow", "timeline", "ui", "usd"):
        setattr(modules["omni"], name, modules[f"omni.{name}"])
    modules["omni.appwindow"].get_default_app_window = lambda: SimpleNamespace(
        get_keyboard=lambda: input_interface.keyboard
    )
    modules["omni.kit.app"].get_app = lambda: SimpleNamespace(next_update_async=next_update)
    stage = object()
    modules["omni.usd"].get_context = lambda: SimpleNamespace(get_stage=lambda: stage)
    modules["omni.timeline"].get_timeline_interface = lambda: SimpleNamespace(
        is_stopped=lambda: True
    )
    for name in ("Window", "VStack", "HStack", "Label", "FloatField", "Button"):
        setattr(modules["omni.ui"], name, type(name, (Widget,), {}))
    modules["omni.ui"].SimpleFloatModel = FloatModel
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    panel.models = []
    return widgets


def test_import_does_not_start_control_or_schedule_tasks(panel_module):
    assert panel_module.instances == []
    assert panel_module.grippers == []
    assert panel_module.gripper_loads == []
    assert not hasattr(panel_module, "MG400_CONTROL_START_TASK")


def test_panel_uses_configured_host_after_matching_mirror(panel_module):
    panel_module.config_path.write_text('[robot]\nhost = "robot-new.test"\n')
    panel = panel_module.ControlPanel(SimpleNamespace(host="robot-new.test"))
    assert panel.host == panel.control.host == "robot-new.test"
    assert panel.control.calls == []


@pytest.mark.parametrize("mirror_host", (None, "192.168.1.6"))
def test_reload_rejects_old_mirror_after_configuration_change(
    panel_module, monkeypatch, mirror_host
):
    panel_module.config_path.write_text('[robot]\nhost = "robot-new.test"\n')
    mirror = SimpleNamespace(stop_event=threading.Event())
    if mirror_host is not None:
        mirror.host = mirror_host
    monkeypatch.setattr(builtins, panel_module.SINGLETON, None, raising=False)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, mirror, raising=False)
    with pytest.raises(RuntimeError, match="Restart the mirror and control panel"):
        asyncio.run(panel_module.start())
    assert panel_module.instances == []


@pytest.mark.parametrize("invalid", ("missing", "invalid"))
def test_invalid_configuration_prevents_control_creation(panel_module, invalid):
    if invalid == "missing":
        panel_module.config_path.unlink()
    else:
        panel_module.config_path.write_text('[robot]\nhost = ""\n')
    with pytest.raises((FileNotFoundError, ValueError)):
        panel_module.ControlPanel(SimpleNamespace(host="192.168.1.6"))
    assert panel_module.instances == []


def test_copy_actual_initializes_targets_and_editing_sends_nothing(panel):
    assert not panel.initialized
    panel.copy_actual()
    assert panel.initialized
    assert [model.as_float for model in panel.models] == [1.0, 2.0, 3.0, 4.0]
    panel.models[0].set_value(5.0)
    assert panel.models[0].as_float == 5.0
    assert panel.control.calls == []


@pytest.mark.parametrize("pose_state", ("missing", "stale"))
def test_target_initialization_requires_fresh_flange_coordinates(panel, pose_state):
    if pose_state == "missing":
        panel.actual_pose = None
    else:
        panel.pose_received = time.monotonic() - 1.0
    panel.copy_actual()
    assert not panel.initialized
    assert [model.as_float for model in panel.models] == [0.0] * 4
    assert panel.control.calls == []


def test_refresh_pose_reads_without_blocking_event_loop(panel, monkeypatch):
    read_started, release_read = threading.Event(), threading.Event()

    def delayed_pose():
        read_started.set()
        assert release_read.wait(3), "Test did not release the pose reader"
        return (200.0, 50.0, 150.0, 25.0)

    monkeypatch.setattr(panel.control, "read_pose", delayed_pose)

    async def exercise():
        refresh = asyncio.create_task(panel.refresh_pose())
        try:
            assert await asyncio.to_thread(read_started.wait, 1)
            assert not refresh.done()
            assert panel.actual_pose == (1.0, 2.0, 3.0, 4.0)
        finally:
            release_read.set()
            await refresh
        panel.copy_actual()
        assert [model.as_float for model in panel.models] == [200.0, 50.0, 150.0, 25.0]
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_pose_read_failure_removes_previous_coordinates(panel, monkeypatch):
    def failed_pose():
        raise RuntimeError("Pose query failed")

    monkeypatch.setattr(panel.control, "read_pose", failed_pose)
    asyncio.run(panel.refresh_pose())
    panel.copy_actual()
    assert panel.actual_pose is None
    assert not panel.initialized
    assert panel.control.calls == []


def test_slow_pose_reply_does_not_make_old_coordinates_fresh(panel, monkeypatch):
    clock = [10.0]
    monkeypatch.setitem(
        panel.refresh_pose.__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def slow_pose():
        clock[0] = 12.0
        return SimpleNamespace(pose=(200.0, 50.0, 150.0, 25.0), requested_at=10.0)

    monkeypatch.setattr(panel.control, "read_pose_measurement", slow_pose)
    asyncio.run(panel.refresh_pose())
    assert panel.pose_received == 10.0
    panel.copy_actual()
    assert not panel.initialized
    assert [model.as_float for model in panel.models] == [0.0] * 4


def test_concurrent_pose_refreshes_do_not_overlap(panel, monkeypatch):
    read_started, release_read = threading.Event(), threading.Event()
    reads = []

    def delayed_pose():
        reads.append(threading.get_ident())
        read_started.set()
        assert release_read.wait(3), "Test did not release the pose reader"
        return (200.0, 50.0, 150.0, 25.0)

    monkeypatch.setattr(panel.control, "read_pose", delayed_pose)

    async def exercise():
        first = asyncio.create_task(panel.refresh_pose())
        second = None
        try:
            assert await asyncio.to_thread(read_started.wait, 1)
            second = asyncio.create_task(panel.refresh_pose())
            await asyncio.sleep(0)
            assert len(reads) == 1
            assert not second.done()
        finally:
            release_read.set()
            await first
            if second:
                await second
        assert len(reads) == 2

    asyncio.run(exercise())


def test_old_control_pose_cannot_replace_current_control_coordinates(
    panel_module, panel, monkeypatch
):
    read_started, release_read = threading.Event(), threading.Event()

    def delayed_pose():
        read_started.set()
        assert release_read.wait(3), "Test did not release the pose reader"
        return (200.0, 50.0, 150.0, 25.0)

    monkeypatch.setattr(panel.control, "read_pose", delayed_pose)

    async def exercise():
        refresh = asyncio.create_task(panel.refresh_pose())
        try:
            assert await asyncio.to_thread(read_started.wait, 1)
            panel.control = panel_module.JointControl(panel.host, panel.feedback_snapshot)
            panel.actual_pose = (300.0, 60.0, 160.0, 30.0)
        finally:
            release_read.set()
            assert await refresh is None
        assert panel.actual_pose == (300.0, 60.0, 160.0, 30.0)

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ("missing", "disconnected", "stopped"))
def test_feedback_snapshot_rejects_missing_disconnected_or_stopped_mirror(panel, state):
    if state == "missing":
        panel.mirror.latest = None
    elif state == "disconnected":
        panel.mirror.network_error = "Connection closed"
    else:
        panel.mirror.stop_event.set()
    with pytest.raises(RuntimeError):
        panel.feedback_snapshot()
    panel.copy_actual()
    assert not panel.initialized
    assert panel.control.calls == []


@pytest.mark.parametrize("state", ("uninitialized", "closed", "stopped"))
def test_move_rejected_until_initialized_and_after_close_or_stop(panel, state):
    if state != "uninitialized":
        panel.copy_actual()
    if state == "closed":
        panel.closed = True
    elif state == "stopped":
        panel.control.stopped = True
    panel.submit("move")
    assert panel.action is None
    assert panel.control.calls == []


def test_move_captures_one_target_ignores_repeat_clicks_and_stop_runs_in_parallel(panel):
    async def exercise():
        panel.copy_actual()
        panel.submit("move")
        first_action = panel.action
        panel.models[0].set_value(99.0)
        panel.submit("move")
        panel.submit("enable")
        assert panel.action is first_action
        try:
            assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            assert panel.busy
            panel.request_stop()
            assert panel.control.stopped
            first_stop = panel.stop_task
            panel.request_stop()
            assert panel.stop_task is first_stop
            await asyncio.wait_for(first_stop, 1)
            assert not first_action.done()
            assert panel.control.calls == [("move_linear", (1.0, 2.0, 3.0, 4.0)), ("stop",)]
            assert "STOPPED" in panel.message
        finally:
            panel.control.release_move.set()
            await first_action
        assert "STOPPED" in panel.message
        assert not panel.busy
        panel.submit("move")
        assert panel.action is first_action

    asyncio.run(exercise())


def test_enable_sends_no_move(panel):
    async def exercise():
        panel.submit("enable")
        await panel.action

    asyncio.run(exercise())
    assert panel.control.calls == [("enable",)]
    assert panel.message == "Enabled. No movement sent."


def test_manual_target_rejection_keeps_coordinates_and_allows_retry(
    panel_module, panel, monkeypatch
):
    move_linear = panel.control.move_linear
    rejected = True

    def reject_once(target):
        nonlocal rejected
        if rejected:
            rejected = False
            raise panel_module.backend.TargetRejectedError("Controller rejected the target")
        return move_linear(target)

    monkeypatch.setattr(panel.control, "move_linear", reject_once)

    async def exercise():
        panel.copy_actual()
        panel.models[0].set_value(406.8)
        panel.submit("move")
        first_action = panel.action
        await first_action
        assert "Adjust X/Y/Z/R and try again" in panel.message
        assert [model.as_float for model in panel.models] == [406.8, 2.0, 3.0, 4.0]
        assert not panel.control.stopped
        assert not panel.busy
        assert panel.control.calls == []
        panel.models[0].set_value(396.8)
        panel.control.release_move.set()
        panel.submit("move")
        assert panel.action is not first_action
        await panel.action
        assert panel.control.calls == [("move_linear", (396.8, 2.0, 3.0, 4.0))]
        assert panel.message == "Arrival verified."

    asyncio.run(exercise())


def test_disable_cancels_once_drains_move_and_allows_explicit_enable_then_move(
    panel_module, panel
):
    async def exercise():
        panel.copy_actual()
        old = panel.control
        panel.submit("move")
        action = panel.action
        try:
            assert await asyncio.to_thread(old.move_started.wait, 1)
            panel.request_disable()
            assert old.stopped
            assert old.cancel_modes == [True]
            disable_task = panel.disable_task
            panel.request_disable()
            assert panel.disable_task is disable_task
            assert await asyncio.to_thread(old.disable_finished.wait, 1)
            await asyncio.sleep(0)
            assert panel.control is old
            assert len(panel_module.instances) == 1
            assert not action.done()
            assert not disable_task.done()
            panel.submit("enable")
            panel.submit("move")
            assert panel.action is action
        finally:
            old.release_move.set()
            await action
            if panel.disable_task:
                await panel.disable_task
        assert old.calls == [("move_linear", (1.0, 2.0, 3.0, 4.0)), ("disable",)]
        assert old.cancel_modes == [True]
        replacement = panel.control
        assert replacement is not old
        assert len(panel_module.instances) == 2
        assert replacement.calls == []
        assert replacement.read_state().mode == 4
        assert not replacement.stopped
        assert not panel.busy
        assert panel.message == "Disabled. Click Enable robot to enable again."
        panel.submit("enable")
        await panel.action
        assert replacement.calls == [("enable",)]
        assert replacement.read_state().mode == 5
        panel.models[0].set_value(30.0)
        replacement.release_move.set()
        panel.submit("move")
        await panel.action
        assert replacement.calls == [("enable",), ("move_linear", (30.0, 2.0, 3.0, 4.0))]
        assert panel.message == "Arrival verified."

    asyncio.run(exercise())


def test_unconfirmed_disable_drains_move_keeps_old_control_and_retains_error(
    panel_module, panel, monkeypatch
):
    old = panel.control

    def unconfirmed_disable():
        old.calls.append(("disable",))
        old.disable_finished.set()
        raise RuntimeError("Disabled mode was not confirmed")

    monkeypatch.setattr(old, "disable", unconfirmed_disable)

    async def exercise():
        panel.copy_actual()
        panel.submit("move")
        action = panel.action
        try:
            assert await asyncio.to_thread(old.move_started.wait, 1)
            panel.request_disable()
            assert old.stopped
            assert await asyncio.to_thread(old.disable_finished.wait, 1)
            await asyncio.sleep(0)
            assert not action.done()
            assert not panel.disable_task.done()
            assert panel.control is old
        finally:
            old.release_move.set()
            await action
            if panel.disable_task:
                await panel.disable_task
        message = "DISABLE UNCONFIRMED: Disabled mode was not confirmed"
        assert panel.message == message
        assert panel.control is old
        assert old.stopped
        assert len(panel_module.instances) == 1
        panel.submit("enable")
        panel.submit("move")
        panel.request_disable()
        await asyncio.sleep(0)
        assert panel.action is action
        assert old.calls == [("move_linear", (1.0, 2.0, 3.0, 4.0)), ("disable",)]
        assert old.cancel_modes == [True]
        assert panel.message == message

    asyncio.run(exercise())


def test_disable_requires_fresh_target_initialization_after_feedback_expires(panel, monkeypatch):
    panel.copy_actual()
    panel.models[0].set_value(99.0)
    old = panel.control
    disable = old.disable

    def disable_then_expire_feedback():
        sample = disable()
        panel.mirror.latest = sample, time.monotonic() - 1.0
        return sample

    def read_fresh_state(control):
        sample, received = control.snapshot()
        if time.monotonic() - received > 0.25:
            raise RuntimeError("Robot feedback is absent or stale")
        return sample

    monkeypatch.setattr(old, "disable", disable_then_expire_feedback)
    monkeypatch.setattr(type(old), "read_state", read_fresh_state)

    async def exercise():
        panel.request_disable()
        await panel.disable_task
        assert panel.control is not old
        assert not panel.control.stopped
        assert not panel.initialized
        assert panel.control.calls == []
        assert old.calls == [("disable",)]
        panel.submit("move")
        panel.submit("home")
        assert panel.action is None
        sample, _ = panel.mirror.latest
        panel.mirror.latest = sample, time.monotonic()
        await panel.refresh_pose()
        panel.copy_actual()
        assert panel.initialized
        assert [model.as_float for model in panel.models] == [1.0, 2.0, 3.0, 4.0]
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_home_keeps_edited_coordinates_until_arrival_then_reads_measured_pose(panel):
    async def exercise():
        panel.copy_actual()
        for model in panel.models:
            model.set_value(45.0)
        panel.submit("home")
        assert [model.as_float for model in panel.models] == [45.0] * 4
        try:
            assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            assert [model.as_float for model in panel.models] == [45.0] * 4
            panel.control.pose = (200.0, 0.0, 150.0, 0.0)
        finally:
            panel.control.release_move.set()
            await panel.action
        assert [model.as_float for model in panel.models] == [200.0, 0.0, 150.0, 0.0]

    asyncio.run(exercise())
    assert panel.control.calls == [("move", (0.0, 0.0, 0.0, 0.0))]
    assert panel.message == "Arrival verified."


def test_keyboard_starts_disabled_and_ignores_unarmed_keys(panel):
    assert not panel.keyboard_enabled
    for key in "WASDQERFZX":
        panel.key_event(key, True)
        panel.key_event(key, True, repeat=True)
        panel.key_event(key, False)
    assert panel.action is None
    assert panel.held_keys == set()
    assert panel.control.calls == []


@pytest.mark.parametrize("key", tuple("WASDRFQE"))
def test_keyboard_passes_one_hold_to_prepared_session(panel, key):
    async def exercise():
        session = await arm_keyboard(panel)
        for model in panel.models:
            model.set_value(99.0)
        panel.key_event(key, True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        assert not panel.action.done()
        assert session.run_calls == [key]
        assert session.movements == [key]
        panel.key_event(key, False)
        assert panel.jog_released.is_set()
        await asyncio.wait_for(panel.action, 1)
        assert panel.held_keys == set()
        assert panel.active_key is None
        assert panel.keyboard_enabled
        assert [model.as_float for model in panel.models] == list(panel.control.pose)
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_release_before_worker_starts_does_not_move(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("W", True)
        panel.key_event("W", False)
        assert panel.jog_released.is_set()
        await asyncio.wait_for(panel.action, 1)
        assert session.movements == []
        assert panel.held_keys == set()
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_keyboard_receives_measurement_returned_by_pose_task(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.pose_task = asyncio.create_task(panel.refresh_pose())
        measurement = await panel.pose_task
        assert measurement.control is panel.control
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        assert session.initial_poses == [measurement]
        assert session.initial_poses[0] is panel.pose_task.result()
        assert panel.actual_pose == measurement.pose
        panel.key_event("W", False)
        await asyncio.wait_for(panel.action, 1)

    asyncio.run(exercise())


def test_release_while_waiting_for_pose_read_does_not_start_motion(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        measurement = panel.control.read_pose_measurement()
        panel.pose_task = asyncio.get_running_loop().create_future()
        panel.key_event("W", True)
        await asyncio.sleep(0)
        assert panel.busy
        assert session.run_calls == []
        panel.key_event("W", False)
        assert panel.jog_released.is_set()
        panel.pose_task.set_result(measurement)
        await asyncio.wait_for(panel.action, 1)
        assert session.initial_poses == [measurement]
        assert session.movements == []
        assert panel.held_keys == set()
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_keyboard_repeats_do_not_start_another_hold(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("R", True)
        first_action = panel.action
        await asyncio.wait_for(session.run_started.wait(), 1)
        for _ in range(5):
            panel.key_event("R", True, repeat=True)
            panel.key_event("R", True)
        assert panel.action is first_action
        assert session.run_calls == ["R"]
        panel.key_event("R", False)
        await asyncio.wait_for(first_action, 1)
        panel.key_event("R", True, repeat=True)
        assert panel.action is first_action
        assert session.run_calls == ["R"]

    asyncio.run(exercise())


def test_hold_limit_requires_keyup_before_another_hold(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        # Backend ends the hold at its five-second deadline without a key release.
        session.hold_finished.set()
        first_action = panel.action
        await asyncio.wait_for(first_action, 1)
        assert "Hold limit reached" in panel.message
        for repeat in (True, False):
            panel.key_event("W", True, repeat=repeat)
        assert panel.action is first_action
        assert session.run_calls == ["W"]
        panel.key_event("W", False)
        session.hold_finished.clear()
        session.run_started.clear()
        panel.key_event("W", True)
        assert panel.action is not first_action
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("W", False)
        await asyncio.wait_for(panel.action, 1)
        assert session.run_calls == ["W", "W"]

    asyncio.run(exercise())


def test_keyboard_target_limit_keeps_keys_on_but_requires_release(panel, monkeypatch):
    async def exercise():
        session = await arm_keyboard(panel)
        run = type(session).run

        async def reject_first_hold(self, key, released, initial_pose=None):
            if key == "W":
                self.run_calls.append(key)
                self.limit_reason = "Controller rejected the target. This target was not sent."
                return self.control.pose
            self.limit_reason = None
            return await run(self, key, released, initial_pose=initial_pose)

        monkeypatch.setattr(type(session), "run", reject_first_hold)
        panel.key_event("W", True)
        first_action = panel.action
        await asyncio.wait_for(first_action, 1)
        assert "Release the key and choose another direction" in panel.message
        assert panel.keyboard_enabled
        assert not panel.control.stopped
        assert panel.held_keys == {"W"}
        for repeat in (True, False):
            panel.key_event("W", True, repeat=repeat)
        assert panel.action is first_action
        panel.key_event("S", True)
        assert panel.action is first_action
        panel.key_event("S", False)
        panel.key_event("W", False)
        assert panel.held_keys == set()
        panel.key_event("S", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        assert panel.action is not first_action
        panel.key_event("S", False)
        await asyncio.wait_for(panel.action, 1)
        assert session.run_calls == ["W", "S"]
        assert panel.keyboard_enabled
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_second_held_key_releases_active_motion_without_starting_another(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("R", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        first_action = panel.action
        panel.key_event("W", True)
        assert panel.jog_released.is_set()
        assert panel.action is first_action
        await asyncio.wait_for(first_action, 1)
        panel.key_event("W", False)
        panel.key_event("R", True)
        assert panel.action is first_action
        panel.key_event("R", False)
        assert panel.held_keys == set()
        assert session.run_calls == ["R"]

    asyncio.run(exercise())


@pytest.mark.parametrize("confirmed", (True, False))
def test_late_keyboard_completion_preserves_explicit_stop_result(panel, monkeypatch, confirmed):
    async def exercise():
        session = await arm_keyboard(panel)
        session.finish_allowed.clear()
        monkeypatch.setattr(
            panel.control, "stop", lambda: {"ok": confirmed, "error": "test stop failure"}
        )
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.request_stop()
        try:
            await asyncio.wait_for(panel.stop_task, 1)
            stop_message = panel.message
            assert stop_message.startswith("STOPPED:" if confirmed else "STOP UNCONFIRMED:")
            assert not panel.action.done()
        finally:
            session.finish_allowed.set()
            await asyncio.wait_for(panel.action, 1)
        assert panel.message == stop_message
        assert not panel.keyboard_enabled

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", (4, 7))
def test_keyboard_preparation_rejects_robot_that_is_not_enabled_and_idle(panel, mode):
    async def exercise():
        panel.control.set_mode(mode)
        panel.request_keyboard()
        await asyncio.wait_for(panel.keyboard_task, 1)
        assert not panel.keyboard_enabled
        assert not panel.keyboard_arming
        panel.key_event("W", True)
        assert panel.action is None
        assert panel.keyboard_session.run_calls == []
        assert "enabled and idle" in panel.message
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.fixture
def failed_preparation(panel_module, monkeypatch):
    faulted = asyncio.Event()
    finish = asyncio.Event()

    async def prepare(session):
        session.fault = "Keyboard preflight failed"
        session.control.cancel()
        faulted.set()
        await finish.wait()
        raise RuntimeError("Keyboard preparation cancelled")

    monkeypatch.setattr(panel_module.KeyboardJogSession, "prepare", prepare)
    return SimpleNamespace(faulted=faulted, finish=finish)


def test_window_shows_prepare_fault_after_stopped_frame_invalidates_generation(
    panel_module, panel, monkeypatch, failed_preparation
):
    frames = 0
    generation = None

    async def next_update():
        nonlocal frames, generation
        frames += 1
        if frames == 1:
            button = next(widget for widget in widgets if widget.text == "Keyboard: OFF")
            button.clicked_fn()
            generation = panel.keyboard_generation
            await asyncio.wait_for(failed_preparation.faulted.wait(), 1)
        elif frames == 2:
            try:
                assert panel.control.stopped
                assert panel.keyboard_generation > generation
                assert not panel.keyboard_arming
                assert not panel.keyboard_task.done()
            finally:
                failed_preparation.finish.set()
            await asyncio.wait_for(panel.keyboard_task, 1)
            assert panel.message == "ERROR: Keyboard preflight failed"
            assert not panel.keyboard_enabled
            panel.key_event("W", True)
            assert panel.action is None
        else:
            assert frames == 3
            assert any(widget.text == "ERROR: Keyboard preflight failed" for widget in widgets)
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.control.stopped
    assert panel.control.calls == []
    assert panel.keyboard_session.movements == []
    assert not panel.keyboard_enabled
    assert not panel.keyboard_arming


@pytest.mark.parametrize("owner", ("stop", "disable", "close"))
def test_prepare_fault_does_not_overwrite_shutdown_message(
    panel, failed_preparation, owner
):
    async def exercise():
        panel.request_keyboard()
        await asyncio.wait_for(failed_preparation.faulted.wait(), 1)
        panel.set_keyboard(False)
        if owner == "stop":
            panel.request_stop()
            await asyncio.wait_for(panel.stop_task, 1)
        elif owner == "disable":
            panel.disable_task = asyncio.create_task(asyncio.Event().wait())
            panel.message = "Disabling robot..."
        else:
            panel.closed = True
            panel.message = "Control panel closed"
        message = panel.message
        failed_preparation.finish.set()
        await asyncio.wait_for(panel.keyboard_task, 1)
        assert panel.message == message
        assert panel.control.stopped
        assert not panel.keyboard_enabled
        if panel.disable_task:
            panel.disable_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await panel.disable_task

    asyncio.run(exercise())


@pytest.mark.parametrize("replacement", ("session", "control"))
def test_old_preparation_fault_does_not_overwrite_current_owner(
    panel_module, panel, failed_preparation, replacement
):
    async def exercise():
        panel.request_keyboard()
        await asyncio.wait_for(failed_preparation.faulted.wait(), 1)
        panel.set_keyboard(False)
        if replacement == "session":
            panel.keyboard_session = panel_module.KeyboardJogSession(panel.control)
        else:
            panel.control = panel_module.JointControl(panel.host, panel.feedback_snapshot)
        panel.message = "Current control state"
        failed_preparation.finish.set()
        await asyncio.wait_for(panel.keyboard_task, 1)
        assert panel.message == "Current control state"
        assert not panel.keyboard_enabled

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", ("off", "focus", "close"))
def test_cancelled_preparation_cannot_arm_later(panel, cancel):
    async def exercise():
        panel.request_keyboard()
        session = panel.keyboard_session
        session.prepare_allowed.clear()
        await asyncio.wait_for(session.prepare_started.wait(), 1)
        assert panel.busy and panel.keyboard_arming
        panel.key_event("W", True)
        assert panel.action is None
        if cancel == "off":
            panel.request_keyboard()
        else:
            # Window focus loss and close both invalidate the preparation generation.
            panel.closed = cancel == "close"
            panel.set_keyboard(False)
        session.prepare_allowed.set()
        await asyncio.wait_for(panel.keyboard_task, 1)
        assert not panel.keyboard_enabled
        assert not panel.keyboard_arming
        assert session.closed
        assert session.movements == []
        assert panel.held_keys == set()

    asyncio.run(exercise())


@pytest.mark.parametrize("modifiers", (16, 32, 48, 1 << 30))
def test_keyboard_lock_modifiers_allow_motion_and_release(panel, modifiers):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("R", True, modifiers=modifiers)
        assert panel.keyboard_enabled
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("R", False, modifiers=modifiers)
        await asyncio.wait_for(panel.action, 1)
        assert panel.keyboard_enabled
        assert panel.held_keys == set()
        assert session.movements == ["R"]

    asyncio.run(exercise())


@pytest.mark.parametrize("modifiers", (1, 2, 4, 8))
def test_keyboard_modifier_disarms_and_releases_active_hold(panel, modifiers):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("R", True, modifiers=modifiers)
        assert not panel.keyboard_enabled
        assert panel.held_keys == set()
        assert panel.jog_released.is_set()
        assert session.release_calls
        await asyncio.wait_for(panel.action, 1)
        assert session.movements == ["W"]
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("modifiers", (0, 16, 32, 48))
def test_escape_disables_robot_and_releases_keyboard_hold(panel, modifiers):
    async def exercise():
        old = panel.control
        session = await arm_keyboard(panel)
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("ESCAPE", True, modifiers=modifiers)
        assert not panel.keyboard_enabled
        assert panel.held_keys == set()
        assert panel.jog_released.is_set()
        assert old.cancel_modes == [True]
        await asyncio.wait_for(panel.disable_task, 1)
        assert old.calls == [("disable",)]
        assert panel.control.read_state().mode == 4

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("key", "position", "target"),
    (("Z", 500, 525), ("X", 500, 475), ("Z", 990, 1000), ("X", 10, 0)),
)
def test_gripper_keyboard_awaits_measured_step_before_continuing(
    panel_module, panel, monkeypatch, key, position, target
):
    async def exercise():
        await arm_keyboard(panel)
        reads, targets = [], []
        current = [position]
        step_done = asyncio.get_running_loop().create_future()
        submitted = asyncio.Event()

        def measured_position():
            reads.append(current[0])
            return current[0]

        def submit_position(value):
            targets.append(value)
            submitted.set()
            return step_done

        gripper = SimpleNamespace(
            closed=False, failed=False, busy=False, measured_position=measured_position,
            submit_position=submit_position, report={},
        )
        panel.gripper = gripper
        monkeypatch.setattr(
            builtins, panel_module.GRIPPER_SINGLETON, SimpleNamespace(closed=True), raising=False
        )
        panel.key_event(key, True)
        await asyncio.wait_for(submitted.wait(), 1)
        first_action = panel.action
        panel.key_event(key, True, repeat=True)
        assert panel.action is first_action
        assert reads == [position]
        assert targets == [target]
        assert not first_action.done()
        panel.key_event(key, False)
        current[0] = target
        step_done.set_result(None)
        await asyncio.wait_for(first_action, 1)
        assert targets == [target]
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("key", ("Z", "X"))
@pytest.mark.parametrize(
    ("init", "message"),
    (
        (0, "The gripper is not initialized."),
        (2, "Gripper initialization is in progress."),
        (None, "Waiting for fresh gripper position feedback."),
    ),
)
def test_gripper_keyboard_reports_feedback_reason_without_sending(panel, key, init, message):
    from mg400_agent.gripper_feedback import FeedbackState

    async def exercise():
        await arm_keyboard(panel)
        feedback = FeedbackState()
        if init is not None:
            feedback.receive({
                "session": "b8af7f0a-d1a1-4887-adfe-d470d5527de6", "seq": 1,
                "sent_at": time.time(), "init": init, "state": 65535, "position": 920,
            })
        targets = []
        panel.gripper = SimpleNamespace(
            closed=False, failed=False, busy=False, feedback=feedback,
            measured_position=lambda: None, submit_position=targets.append,
        )
        panel.key_event(key, True)
        await asyncio.wait_for(panel.action, 1)
        assert panel.message == f"ERROR: {message}"
        assert not panel.keyboard_enabled
        assert targets == []
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_gripper_hold_repeats_after_each_measured_step(panel):
    async def exercise():
        await arm_keyboard(panel)
        current, targets = [500], []

        async def complete(value):
            await asyncio.sleep(0)
            current[0] = value
            if len(targets) == 3:
                panel.key_event("Z", False)

        def submit_position(value):
            targets.append(value)
            return asyncio.create_task(complete(value))

        gripper = SimpleNamespace(
            closed=False, failed=False, busy=False, measured_position=lambda: current[0],
            submit_position=submit_position, report={},
        )
        panel.gripper = gripper
        panel.key_event("Z", True)
        await asyncio.wait_for(panel.action, 1)
        assert targets == [525, 550, 575]
        assert panel.keyboard_session.movements == []

    asyncio.run(exercise())


@pytest.mark.parametrize("key", ["Z", "X"])
def test_gripper_contact_ends_hold_without_repeating_until_keyup(panel, key):
    async def exercise():
        await arm_keyboard(panel)
        targets = []
        result = "The gripper reports object contact before the target."
        gripper = SimpleNamespace(
            closed=False, failed=False, busy=False, measured_position=lambda: 350,
            report={},
        )

        async def contact():
            gripper.report.update(contact_observed=True, result=result)

        def submit_position(value):
            targets.append(value)
            gripper.report["contact_observed"] = False
            return asyncio.create_task(contact())

        gripper.submit_position = submit_position
        panel.gripper = gripper
        panel.key_event(key, True)
        await asyncio.wait_for(panel.action, 1)
        first_action = panel.action
        target = 375 if key == "Z" else 325
        assert targets == [target]
        assert panel.keyboard_enabled and not panel.control.stopped
        assert result in panel.message and "Release the key" in panel.message
        panel.key_event(key, True, repeat=True)
        panel.key_event(key, True)
        assert panel.action is first_action
        assert targets == [target]
        panel.key_event(key, False)
        panel.key_event(key, True)
        await asyncio.wait_for(panel.action, 1)
        panel.key_event(key, False)
        assert targets == [target, target]
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["before", "rejected", "action"])
def test_gripper_keyboard_preserves_specific_failure(panel, stage):
    async def exercise():
        await arm_keyboard(panel)
        targets = []
        reason = "Control is locked. Modbus reply length or CRC is invalid. No retry was sent."
        gripper = SimpleNamespace(
            closed=False, failed=stage == "before", busy=False,
            measured_position=lambda: 350, report={"result": reason},
        )

        async def fail():
            gripper.failed = True

        def submit_position(value):
            targets.append(value)
            return None if stage == "rejected" else asyncio.create_task(fail())

        gripper.submit_position = submit_position
        panel.gripper = gripper
        panel.key_event("X", True)
        await asyncio.wait_for(panel.action, 1)
        assert panel.message == f"ERROR: {reason}"
        assert not panel.keyboard_enabled
        assert targets == ([] if stage == "before" else [325])
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_gripper_hold_deadline_waits_for_keyup_before_restarting(panel, monkeypatch):
    async def exercise():
        await arm_keyboard(panel)
        clock, current, targets = [0.0], [500], []
        monkeypatch.setitem(
            panel.jog_gripper.__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0])
        )

        async def complete(value):
            current[0] = value
            clock[0] += 5.0

        def submit_position(value):
            targets.append(value)
            return asyncio.create_task(complete(value))

        gripper = SimpleNamespace(
            closed=False, failed=False, busy=False, measured_position=lambda: current[0],
            submit_position=submit_position, report={},
        )
        panel.gripper = gripper
        panel.key_event("Z", True)
        await asyncio.wait_for(panel.action, 1)
        first_action = panel.action
        assert "Hold limit reached" in panel.message
        panel.key_event("Z", True)
        assert panel.action is first_action
        assert targets == [525]
        panel.key_event("Z", False)
        panel.key_event("Z", True)
        await asyncio.wait_for(panel.action, 1)
        panel.key_event("Z", False)
        assert targets == [525, 550]

    asyncio.run(exercise())


def test_start_constructs_owned_gripper_from_shared_feedback_path(
    panel_module, panel, monkeypatch
):
    monkeypatch.setattr(builtins, panel_module.SINGLETON, None, raising=False)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)

    async def run_until_closed(self):
        while not self.closed:
            await asyncio.sleep(0)

    monkeypatch.setattr(panel_module.ControlPanel, "run", run_until_closed)

    async def exercise():
        created = await panel_module.start()
        try:
            gripper = created.gripper
            assert panel_module.grippers == [gripper]
            assert gripper.owner is created
            assert gripper.feedback_path == str(panel_module.feedback_path)
            assert getattr(builtins, panel_module.GRIPPER_SINGLETON) is gripper
            assert gripper.calls == created.control.calls == []
            assert not created.keyboard_enabled
            assert created.mirror is panel.mirror
            assert not created.mirror.window.visible
            assert created.mirror.stop_calls == []
            path, args, kwargs = panel_module.gripper_loads[0]
            assert Path(path) == ROOT / "apps/isaacsim/gripper_control.py"
            assert args == ()
            assert kwargs["run_name"] not in ("__main__", "<run_path>")
        finally:
            created.closed = True
            await created.task

    asyncio.run(exercise())


def test_combined_panel_uses_one_window_and_starts_child_without_commands(
    panel_module, panel, monkeypatch
):
    child = panel_module.Gripper()
    panel.gripper = child
    child.owner = panel

    async def next_update():
        await asyncio.wait_for(child.run_started.wait(), 1)
        windows = [widget for widget in widgets if type(widget).__name__ == "Window"]
        assert windows == [panel.window]
        assert panel.window.text == "MG400 Control"
        assert child.windows == [panel.window]
        assert child.build_count == 1
        assert child.calls == panel.control.calls == []
        assert not panel.keyboard_enabled
        panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert child.closed
    assert child.task.done()
    assert child.run_finished.is_set()
    assert panel.window.destroyed


def test_sync_status_and_measured_joints_are_shown_in_the_control_window(
    panel_module, panel, monkeypatch
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 2:
            labels = [widget.text for widget in widgets if type(widget).__name__ == "Label"]
            assert any("LIVE | 24.5 updates/s" in text and "ms old" in text for text in labels)
            assert "  ".join(f"J{i}: {i:.2f} deg" for i in range(1, 5)) in labels
            assert sum(type(widget).__name__ == "Window" for widget in widgets) == 1
            panel.request_sync_stop()

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.control.calls == []
    assert panel.mirror.stop_event.is_set()


@pytest.mark.parametrize("trigger", ("button", "window"))
def test_sync_shutdown_waits_for_stop_actions_and_receiver_before_destroying_window(
    panel_module, panel, monkeypatch, trigger
):
    child = panel_module.Gripper()
    panel.gripper = child
    child.owner = panel
    stop_started, stop_allowed = threading.Event(), threading.Event()
    original_stop = panel.control.stop

    def stop():
        stop_started.set()
        assert stop_allowed.wait(2), "Test did not release stop confirmation"
        return original_stop()

    monkeypatch.setattr(panel.control, "stop", stop)

    async def next_update():
        await asyncio.wait_for(child.run_started.wait(), 1)
        if trigger == "button":
            next(widget for widget in widgets if widget.text == "Stop synchronization").clicked_fn()
        else:
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)

    async def exercise():
        arm_done = asyncio.get_running_loop().create_future()
        child_done = asyncio.get_running_loop().create_future()
        receiver_done = asyncio.Event()
        panel.action = arm_done
        child.action = child_done
        panel.mirror.task = asyncio.create_task(receiver_done.wait())
        task = asyncio.create_task(panel.run())
        try:
            assert await asyncio.to_thread(stop_started.wait, 1)
            assert child.closing
            assert not child.closed
            assert not panel.mirror.stop_event.is_set()
            assert not panel.window.destroyed
            stop_allowed.set()
            await asyncio.wait_for(panel.stop_task, 1)
            assert not panel.mirror.stop_event.is_set()
            arm_done.set_result(None)
            await asyncio.sleep(0)
            assert not panel.mirror.stop_event.is_set()
            child_done.set_result(None)
            assert await asyncio.to_thread(panel.mirror.stop_event.wait, 1)
            assert child.task.done()
            assert not task.done()
            assert not panel.window.destroyed
            receiver_done.set()
            await asyncio.wait_for(task, 1)
            assert panel.window.destroyed
            assert len(panel.mirror.stop_calls) == 1
            assert panel.control.calls == [("stop",)]
            assert panel.report["status"] == "closed"
            assert panel.report["keyboard_enabled"] is False
            assert panel.report["keyboard_arming"] is False
            assert panel.report["busy"] is False
            assert panel.report["synchronization"]["status"] == "stopped"
        finally:
            stop_allowed.set()
            for pending in (arm_done, child_done):
                if not pending.done():
                    pending.set_result(None)
            receiver_done.set()
            panel.closed = True
            await asyncio.wait_for(task, 1)

    asyncio.run(exercise())


def test_sync_shutdown_releases_keyboard_and_waits_for_its_current_action(
    panel_module, panel, monkeypatch
):
    closing = asyncio.Event()
    sessions = []
    frames = 0
    moving_pose = (310.0, 12.0, 50.0, 4.0)

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            session = await arm_keyboard(panel)
            sessions.append(session)
            session.finish_allowed.clear()
            panel.key_event("W", True)
            await asyncio.wait_for(session.run_started.wait(), 1)
            session.measured_pose = (moving_pose, time.monotonic())
        else:
            assert panel.actual_pose == moving_pose
            assert panel.report["keyboard_enabled"] is True
            assert panel.report["busy"] is True
            assert panel.report["synchronization"]["status"] == "live"
            panel.request_sync_stop()
            closing.set()

    install_ui(panel_module, panel, monkeypatch, next_update)

    async def exercise():
        task = asyncio.create_task(panel.run())
        try:
            await asyncio.wait_for(closing.wait(), 1)
            session = sessions[0]
            assert session.closed
            assert session.released.is_set()
            assert panel.held_keys == set()
            assert not panel.keyboard_enabled
            assert not panel.action.done()
            assert not panel.mirror.stop_event.is_set()
            session.finish_allowed.set()
            await asyncio.wait_for(task, 1)
            assert panel.mirror.stop_event.is_set()
            assert panel.window.destroyed
            saved = json.loads(panel.write_report.__globals__["REPORT"].read_text())
            for report in (panel.report, saved):
                assert report["status"] == "closed"
                assert report["keyboard_enabled"] is False
                assert report["keyboard_arming"] is False
                assert report["busy"] is False
                assert report["synchronization"]["status"] == "stopped"
        finally:
            for session in sessions:
                session.finish_allowed.set()
            panel.closed = True
            await asyncio.wait_for(task, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ("unconfirmed_stop", "pending_ack"))
def test_sync_shutdown_keeps_feedback_when_robot_stop_is_uncertain(
    panel_module, panel, monkeypatch, fault
):
    async def next_update():
        panel.request_sync_stop()

    install_ui(panel_module, panel, monkeypatch, next_update)

    async def exercise():
        if fault == "pending_ack":
            panel.control._pending = {"done": threading.Event(), "sent": True, "ok": False}
        else:
            monkeypatch.setattr(panel.control, "stop", lambda: {"ok": False, "error": "lost ACK"})
            panel.request_stop()
            await asyncio.wait_for(panel.stop_task, 1)
        await asyncio.wait_for(panel.run(), 1)
        assert not panel.mirror.stop_event.is_set()
        assert panel.mirror.stop_calls == []
        assert not panel.keyboard_enabled
        assert panel.report["synchronization_retained"]
        assert "Synchronization remains active" in panel.message
        assert panel.report["status"] == "closed"
        assert panel.report["keyboard_enabled"] is False
        assert panel.report["keyboard_arming"] is False
        assert panel.report["busy"] is False
        assert panel.report["synchronization"]["status"] == "live"

    asyncio.run(exercise())


def test_closing_stopped_sync_preserves_error_and_waits_for_receiver(panel):
    panel.mirror.stop_event.set()
    panel.mirror.report.update(
        status="error", reason="Stage changed", error="Mirror scene is no longer available"
    )
    original_report = dict(panel.mirror.report)

    async def exercise():
        receiver_done = asyncio.Event()
        panel.mirror.task = asyncio.create_task(receiver_done.wait())
        closing = asyncio.create_task(panel.close_synchronization())
        try:
            await asyncio.sleep(0)
            assert not closing.done()
            assert not panel.mirror.window.destroyed
            assert panel.mirror.stop_calls == []
            assert panel.mirror.report == original_report
        finally:
            receiver_done.set()
            await asyncio.wait_for(closing, 1)
        assert panel.mirror.window.destroyed
        assert panel.mirror.stop_calls == []
        assert panel.mirror.report == original_report

    asyncio.run(exercise())


def test_gripper_task_failure_does_not_prevent_parent_window_cleanup(
    panel_module, panel, monkeypatch
):
    child = panel_module.Gripper()
    panel.gripper = child
    child.owner = panel

    async def fail_child(window=None):
        child.run_started.set()
        child.closed = True
        raise RuntimeError("Gripper feedback task failed")

    monkeypatch.setattr(child, "run", fail_child)

    async def next_update():
        await asyncio.wait_for(child.run_started.wait(), 1)
        panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(asyncio.wait_for(panel.run(), 1))
    assert child.task.done()
    assert panel.closed
    assert panel.window.destroyed
    assert panel.report["status"] == "closed"
    assert panel.report["message"] == "Gripper control stopped: Gripper feedback task failed"
    assert panel.control.calls == []


def test_reload_replaces_idle_gripper_after_its_task_failed(panel_module, panel, monkeypatch):
    old_child = panel_module.Gripper()
    panel.gripper = old_child
    old_child.owner = panel
    old_child.closed = panel.closed = True

    async def next_update():
        await asyncio.sleep(0)

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    monkeypatch.setattr(builtins, panel_module.GRIPPER_SINGLETON, old_child, raising=False)

    async def fail_child():
        raise RuntimeError("Old gripper task failed")

    async def exercise():
        old_child.task = asyncio.create_task(fail_child())
        await asyncio.sleep(0)
        assert old_child.task.done()
        replacement = await asyncio.wait_for(panel_module.start(), 1)
        try:
            await asyncio.wait_for(replacement.gripper.run_started.wait(), 1)
            assert replacement is not panel
            assert replacement.gripper is not old_child
            assert replacement.gripper.owner is replacement
            assert getattr(builtins, panel_module.SINGLETON) is replacement
            assert getattr(builtins, panel_module.GRIPPER_SINGLETON) is replacement.gripper
            assert not replacement.closed
            assert not replacement.gripper.closed
            windows = [widget for widget in widgets if type(widget).__name__ == "Window"]
            assert windows == [replacement.window]
            assert replacement.control.calls == replacement.gripper.calls == []
        finally:
            replacement.closed = True
            await asyncio.wait_for(replacement.task, 1)

    asyncio.run(exercise())


def test_closing_parent_waits_for_gripper_action_before_closing_child(
    panel_module, panel, monkeypatch
):
    child = panel_module.Gripper()
    panel.gripper = child
    child.owner = panel

    async def next_update():
        await asyncio.wait_for(child.run_started.wait(), 1)
        child.action = asyncio.get_running_loop().create_future()
        panel.held_keys.add("Z")
        panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)

    async def exercise():
        task = asyncio.create_task(panel.run())
        try:
            async def wait_for_closing():
                while not child.closing:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_closing(), 1)
            assert panel.held_keys == set()
            assert not child.closed
            assert not child.task.done()
            assert not task.done()
            assert not panel.window.destroyed
        finally:
            if child.action is not None and not child.action.done():
                child.action.set_result(None)
            await asyncio.wait_for(task, 1)
        assert child.closed
        assert child.task.done()
        assert panel.window.destroyed

    asyncio.run(exercise())


@pytest.mark.parametrize("owned", (False, True))
def test_start_refuses_busy_gripper_before_closing_either_panel(
    panel_module, panel, monkeypatch, owned
):
    child = panel_module.Gripper()
    if owned:
        panel.gripper = child
        child.owner = panel
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    monkeypatch.setattr(builtins, panel_module.GRIPPER_SINGLETON, child, raising=False)

    async def exercise():
        child.action = asyncio.get_running_loop().create_future()
        try:
            with pytest.raises(RuntimeError, match="gripper"):
                await panel_module.start()
            assert not panel.closed
            assert not child.closed
            assert not child.closing
            assert panel_module.gripper_loads == []
            assert len(panel_module.instances) == 1
        finally:
            child.action.set_result(None)

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ("move", "home", "enable", "keyboard"))
def test_arm_commands_do_not_start_while_owned_gripper_is_busy(panel_module, panel, operation):
    child = panel_module.Gripper()
    panel.gripper = child
    panel.copy_actual()

    async def exercise():
        child.action = asyncio.get_running_loop().create_future()
        try:
            if operation == "keyboard":
                panel.request_keyboard()
            else:
                panel.submit(operation)
            assert panel.action is None
            assert panel.keyboard_task is None
            assert panel.control.calls == []
        finally:
            child.action.set_result(None)

    asyncio.run(exercise())


def test_repeated_start_replaces_old_window_and_owned_child(panel_module, panel, monkeypatch):
    async def next_update():
        await asyncio.sleep(0)

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)

    async def wait_for_child(created):
        await asyncio.wait_for(created.gripper.run_started.wait(), 1)

    async def exercise():
        first = await panel_module.start()
        second = None
        try:
            await wait_for_child(first)
            second = await panel_module.start()
            await wait_for_child(second)
            assert first.closed
            assert first.task.done()
            assert first.window.destroyed
            assert first.gripper.closed
            assert first.gripper.task.done()
            windows = [widget for widget in widgets if type(widget).__name__ == "Window"]
            assert [window for window in windows if not window.destroyed] == [second.window]
            assert second.gripper.owner is second
            assert first.keep_mirror
            assert first.mirror is second.mirror is panel.mirror
            assert second.mirror.stop_calls == []
            assert not second.mirror.stop_event.is_set()
            assert not second.mirror.window.visible
            assert getattr(builtins, panel_module.SINGLETON) is second
            assert getattr(builtins, panel_module.GRIPPER_SINGLETON) is second.gripper
            assert all(control.calls == [] for control in panel_module.instances)
            assert all(child.calls == [] for child in panel_module.grippers)
        finally:
            for created in (first, second):
                if created is not None:
                    created.closed = True
                    await asyncio.wait_for(created.task, 1)

    asyncio.run(exercise())


def test_rerunning_entry_after_sync_stop_starts_one_hidden_mirror_and_waits_for_feedback(
    panel_module, panel, monkeypatch
):
    async def next_update():
        await asyncio.sleep(0)

    install_ui(panel_module, panel, monkeypatch, next_update)
    globals_ = panel_module.start.__globals__
    original_load = globals_["runpy"].run_path
    bootstraps = []
    sample = panel.mirror.latest[0]

    def load(path, *args, **kwargs):
        if Path(path).name != "mirror_bootstrap.py":
            return original_load(path, *args, **kwargs)
        assert panel.mirror.stop_event.is_set()
        assert args == ()
        assert kwargs == {"init_globals": {"SHOW_WINDOW": False}}
        mirror = Mirror(sample)
        mirror.latest = None
        mirror.window = None
        mirror.report["status"] = "stale"
        bootstraps.append(mirror)

        async def bootstrap():
            setattr(builtins, panel_module.MIRROR_SINGLETON, mirror)

        return {"MG400_MIRROR_BOOTSTRAP_TASK": asyncio.create_task(bootstrap())}

    monkeypatch.setitem(globals_, "runpy", SimpleNamespace(run_path=load))

    async def exercise():
        first = await panel_module.start()
        second = None
        try:
            await asyncio.wait_for(first.gripper.run_started.wait(), 1)
            first.request_sync_stop()
            await asyncio.wait_for(first.task, 1)
            assert first.window.destroyed
            assert panel.mirror.stop_event.is_set()
            second = await panel_module.start()
            await asyncio.wait_for(second.gripper.run_started.wait(), 1)
            assert bootstraps == [second.mirror]
            assert second.mirror is not first.mirror
            assert not second.mirror.stop_event.is_set()
            assert not second.initialized
            assert not second.keyboard_enabled
            assert second.actual_pose is None
            assert second.control.calls == []
        finally:
            for created in (first, second):
                if created is not None:
                    created.request_sync_stop()
                    await asyncio.wait_for(created.task, 1)

    asyncio.run(exercise())


def test_start_refuses_to_replace_busy_panel(panel_module, panel, monkeypatch):
    panel.control.busy = True
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(panel_module.start())
    assert not panel.closed
    assert len(panel_module.instances) == 1
    assert panel_module.reload_calls == []


def test_start_refuses_reload_while_stopped_command_thread_is_pending(
    panel_module, panel, monkeypatch
):
    panel.control.stopped = True
    panel.control._pending = {"done": threading.Event()}
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    assert not panel.busy and not panel.control.busy
    with pytest.raises(RuntimeError, match="acknowledgement is pending"):
        asyncio.run(panel_module.start())
    assert not panel.closed
    assert panel_module.reload_calls == []
    assert len(panel_module.instances) == 1


def test_reload_control_modules_loads_source_and_binds_matching_dependencies(
    panel_module, monkeypatch, tmp_path
):
    sources = {
        "controller_protocol": (
            "class ControllerCommandError(RuntimeError): pass\n"
            "def parse_controller_response(packet, command): return 'current'\n"
        ),
        "joint_control": (
            "from .controller_protocol import ControllerCommandError, parse_controller_response\n"
            "class JointControl:\n"
            "    def __init__(self, host, snapshot, *, parent=None, command_timeout=2):\n"
            "        self.parent = parent\n"
            "    def solve_pose(self, target): return target\n"
            "class TargetRejectedError(ValueError): pass\n"
        ),
        "keyboard_jog": (
            "from .joint_control import JointControl, TargetRejectedError\n"
            "class KeyboardJogSession:\n"
            "    control_type = JointControl\n"
            "    def _start(self): return JointControl\n"
        ),
        "gripper_feedback": "class FeedbackState: pass\n",
    }
    package = sys.modules["mg400_agent"]
    feedback = sys.modules["mg400_agent.feedback"]
    source_dir = tmp_path / "src/mg400_agent"
    source_dir.mkdir(parents=True)
    for name, source in sources.items():
        (source_dir / f"{name}.py").write_text(source)
        cached = ModuleType(f"mg400_agent.{name}")
        cached.__file__ = f"/legacy/mg400_agent/{name}.py"
        monkeypatch.setitem(sys.modules, cached.__name__, cached)
        monkeypatch.setattr(package, name, cached, raising=False)
    globals_ = panel_module._reload_control_modules.__globals__
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    panel_module._reload_control_modules()
    protocol, control, jog, gripper = (sys.modules[f"mg400_agent.{name}"] for name in sources)
    assert gripper.FeedbackState.__module__ == "mg400_agent.gripper_feedback"
    assert globals_["backend"] is control
    assert globals_["JointControl"] is control.JointControl
    assert globals_["KeyboardJogSession"] is jog.KeyboardJogSession
    assert control.ControllerCommandError is protocol.ControllerCommandError
    assert control.parse_controller_response is protocol.parse_controller_response
    assert jog.JointControl is jog.KeyboardJogSession.control_type is control.JointControl
    assert jog.TargetRejectedError is control.TargetRejectedError
    assert control.JointControl("robot.test", lambda: None, parent="parent").parent == "parent"
    assert Path(control.JointControl.solve_pose.__code__.co_filename) == (
        source_dir / "joint_control.py"
    )
    assert sys.modules["mg400_agent.feedback"] is feedback
    for name in sources:
        module = sys.modules[f"mg400_agent.{name}"]
        assert getattr(package, name) is module
        assert Path(module.__file__) == source_dir / f"{name}.py"


def test_start_reloads_only_after_old_panel_and_gripper_tasks_finish(
    panel_module, panel, monkeypatch
):
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)
    child = panel_module.Gripper()
    monkeypatch.setattr(builtins, panel_module.GRIPPER_SINGLETON, child, raising=False)

    async def run_until_closed(self):
        while not self.closed:
            await asyncio.sleep(0)

    monkeypatch.setattr(panel_module.ControlPanel, "run", run_until_closed)

    async def exercise():
        panel_finished, child_finished = asyncio.Event(), asyncio.Event()
        panel.task = asyncio.create_task(panel_finished.wait())
        child.task = asyncio.create_task(child_finished.wait())
        start = asyncio.create_task(panel_module.start())
        replacement = None
        try:
            await asyncio.sleep(0)
            assert panel.closed
            assert panel_module.reload_calls == []
            panel_finished.set()
            await panel.task
            await asyncio.sleep(0)
            assert not start.done()
            assert panel_module.reload_calls == []
            child_finished.set()
            replacement = await asyncio.wait_for(start, 1)
            assert panel_module.reload_calls == ["reload"]
            assert panel.task.done() and child.task.done()
            assert replacement.control.calls == []
        finally:
            panel_finished.set()
            child_finished.set()
            if replacement is not None:
                replacement.closed = True
                await replacement.task

    asyncio.run(exercise())


def test_concurrent_starts_wait_for_prior_panel_to_exit(panel_module, panel, monkeypatch):
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)

    async def run_until_closed(self):
        while not self.closed:
            await asyncio.sleep(0)

    monkeypatch.setattr(panel_module.ControlPanel, "run", run_until_closed)

    async def exercise():
        release_old_panel = asyncio.Event()
        panel.task = asyncio.create_task(release_old_panel.wait())
        starts = [asyncio.create_task(panel_module.start()) for _ in range(2)]
        await asyncio.sleep(0)
        assert panel.closed
        release_old_panel.set()
        replacements = await asyncio.wait_for(asyncio.gather(*starts), 1)
        try:
            first, second = replacements
            assert first is not second
            assert first.closed
            assert first.task.done()
            assert not second.closed
            assert getattr(builtins, panel_module.SINGLETON) is second
            assert all(control.calls == [] for control in panel_module.instances)
        finally:
            for replacement in replacements:
                replacement.closed = True
            await asyncio.gather(*(replacement.task for replacement in replacements))

    asyncio.run(exercise())


@pytest.mark.parametrize("moving_at_close", (False, True))
def test_run_initializes_passively_and_stops_active_move_on_close(
    panel_module, panel, monkeypatch, moving_at_close
):
    frames = 0

    stop = panel.control.stop

    def stop_and_release_move():
        result = stop()
        panel.control.release_move.set()
        return result

    panel.control.stop = stop_and_release_move

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 2:
            await panel.pose_task
        if frames == 3:
            assert panel.initialized
            assert [model.as_float for model in panel.models] == [1.0, 2.0, 3.0, 4.0]
            assert panel.control.calls == []
            if moving_at_close:
                panel.submit("move")
                assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    panel.actual_pose = None
    panel.pose_received = 0.0
    asyncio.run(panel.run())

    assert frames == 3
    assert panel.closed
    assert panel.window.destroyed
    expected_calls = [("move_linear", (1.0, 2.0, 3.0, 4.0)), ("stop",)] if moving_at_close else []
    assert panel.control.calls == expected_calls
    assert panel.report["status"] == "closed"
    assert panel.report["motion_sent_by_startup"] is False
    input_interface = sys.modules["carb.input"].acquire_input_interface()
    assert input_interface.subscribed == []
    assert input_interface.unsubscribed == []
    assert input_interface.callback is None


@pytest.mark.parametrize(
    ("mode", "busy", "expected_enabled"),
    (
        (4, False, [True, False, False, False, False]),
        (5, False, [False, True, True, True, True]),
        (5, True, [False, True, False, False, True]),
    ),
)
def test_ui_button_rows_and_enabled_states(
    panel_module, panel, monkeypatch, mode, busy, expected_enabled
):
    panel.control.set_mode(mode)
    panel.initialized = True
    frames = 0
    pending_action = None

    async def next_update():
        nonlocal frames, pending_action
        frames += 1
        if frames == 1 and busy:
            pending_action = asyncio.get_running_loop().create_future()
            panel.action = pending_action
        if frames == 2:
            buttons = [widget for widget in widgets if type(widget).__name__ == "Button"]
            assert [button.text for button in buttons] == [
                "Stop synchronization",
                "Enable robot", "Disable robot", "Return to Home", "Move Linear", "Keyboard: OFF"
            ]
            sync, enable, disable, home, move, keyboard = buttons
            assert sync.enabled
            assert sync.clicked_fn == panel.request_sync_stop
            assert enable.parent is disable.parent
            assert type(enable.parent).__name__ == "HStack"
            assert home.parent is move.parent is enable.parent.parent
            assert keyboard.parent is home.parent
            assert [button.enabled for button in buttons[1:]] == expected_enabled
            fields = [widget for widget in widgets if type(widget).__name__ == "FloatField"]
            assert len(fields) == 4
            assert all(field.enabled == (not busy) for field in fields)
            assert panel.control.calls == []
            if pending_action:
                pending_action.set_result(None)
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 2
    assert panel.control.calls == []


@pytest.mark.parametrize("key,name", ((41, "W"), (37, "S")))
def test_exclusive_window_receives_integer_key_press_and_release(
    panel_module, panel, monkeypatch, key, name
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        input_interface = sys.modules["carb.input"].acquire_input_interface()
        if frames == 1:
            assert input_interface.callback is None
            assert input_interface.emit(key) is True
            assert panel.action is None
            keyboard_button = next(widget for widget in widgets if widget.text == "Keyboard: OFF")
            keyboard_button.clicked_fn()
            assert panel.keyboard_arming
            assert not panel.keyboard_enabled
            await asyncio.wait_for(panel.keyboard_task, 1)
            assert panel.keyboard_enabled
        if frames == 2:
            assert panel.window.exclusive_keyboard
            fields = [widget for widget in widgets if type(widget).__name__ == "FloatField"]
            assert all(not field.enabled for field in fields)
            global_events = input_interface.global_events.copy()
            assert input_interface.emit(key, modifiers=1 << 30) is False
            assert panel.action is not None
            pending_action = panel.action
            session = panel.keyboard_session
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert input_interface.emit(key, "repeat", modifiers=1 << 30) is False
            assert panel.action is pending_action
            assert panel.held_keys == {name}
            assert input_interface.emit(key, "release", modifiers=1 << 30) is False
            assert panel.jog_released.is_set()
            await asyncio.wait_for(panel.action, 1)
            assert panel.held_keys == set()
            assert input_interface.window_events[-3:] == [
                (key, 1 << 30, True), (key, 1 << 30, True), (key, 1 << 30, False),
            ]
            assert input_interface.global_events == global_events
            assert session.run_calls == [name]
            assert session.movements == [name]
            assert panel.control.calls == []
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 2
    assert panel.control.calls == []
    assert panel.window.destroyed
    assert panel.keyboard_session.closed


@pytest.mark.parametrize("disarm", ("focus", "hidden", "modifier"))
def test_window_keyboard_disarms_and_releases_active_hold(
    panel_module, panel, monkeypatch, disarm
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        input_interface = sys.modules["carb.input"].acquire_input_interface()
        if frames == 1:
            await arm_keyboard(panel)
        if frames == 2:
            assert panel.window.exclusive_keyboard
            assert input_interface.emit(36) is False
            await asyncio.wait_for(panel.keyboard_session.run_started.wait(), 1)
            active_action = panel.action
            assert panel.held_keys == {"R"}
            if disarm == "hidden":
                panel.window.visible = False
                return
            if disarm == "focus":
                panel.window.set_focus(False)
                assert not panel.keyboard_enabled
                panel.window.set_focus(True)
            else:
                assert input_interface.emit(41, modifiers=2) is False
            assert not panel.keyboard_enabled
            assert panel.held_keys == set()
            assert panel.jog_released.is_set()
            input_interface.emit(41)
            assert not panel.keyboard_enabled
            assert panel.action is active_action
            await asyncio.wait_for(active_action, 1)
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 2
    assert not panel.keyboard_enabled
    assert panel.held_keys == set()
    assert panel.jog_released.is_set()
    assert panel.keyboard_session.movements == ["R"]
    assert panel.keyboard_session.closed
    assert panel.window.destroyed


@pytest.mark.parametrize("cancel", ("focus", "close", "off"))
def test_window_cancels_preparation_without_delayed_arming(
    panel_module, panel, monkeypatch, cancel
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            button = next(widget for widget in widgets if widget.text == "Keyboard: OFF")
            button.clicked_fn()
            session = panel.keyboard_session
            session.prepare_allowed.clear()
            await asyncio.wait_for(session.prepare_started.wait(), 1)
            if cancel == "focus":
                panel.window.set_focus(False)
                panel.window.set_focus(True)
            elif cancel == "close":
                panel.window.visible = False
                session.prepare_allowed.set()
                return
            else:
                button.clicked_fn()
            session.prepare_allowed.set()
            await asyncio.wait_for(panel.keyboard_task, 1)
            assert not panel.keyboard_enabled
            assert not panel.keyboard_arming
            assert session.closed
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 1
    assert panel.window.destroyed
    assert panel.keyboard_session.closed
    assert not panel.keyboard_enabled
    assert panel.keyboard_session.movements == []
    assert panel.control.calls == []


@pytest.mark.parametrize("modifiers", (16, 32, 48, 1 << 30))
def test_window_keyboard_accepts_lock_states_and_ui_event_flags(
    panel_module, panel, monkeypatch, modifiers
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
        if frames == 2:
            input_interface = sys.modules["carb.input"].acquire_input_interface()
            assert input_interface.emit(36, modifiers=modifiers) is False
            assert panel.keyboard_enabled
            await asyncio.wait_for(panel.keyboard_session.run_started.wait(), 1)
            assert input_interface.emit(36, "release", modifiers=modifiers) is False
            await asyncio.wait_for(panel.action, 1)
            assert panel.keyboard_enabled
            assert panel.held_keys == set()
            assert input_interface.global_events == []
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.keyboard_session.movements == ["R"]
    assert panel.control.calls == []


@pytest.mark.parametrize("shortcut", (1, 2, 4, 8))
def test_window_keyboard_rejects_shortcuts_despite_lock_and_ui_flags(
    panel_module, panel, monkeypatch, shortcut
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
            panel.held_keys.add("W")
        if frames == 2:
            input_interface = sys.modules["carb.input"].acquire_input_interface()
            assert input_interface.emit(36, modifiers=shortcut | 48 | (1 << 30)) is False
            assert not panel.keyboard_enabled
            assert panel.held_keys == set()
            assert panel.action is None
            input_interface.emit(36)
            assert panel.action is None
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.control.calls == []
