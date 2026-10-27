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

from mg400_agent.controller_ownership import ControllerOwnership
from mg400_agent.feedback import Feedback

ROOT = Path(__file__).resolve().parents[1]


class FloatModel:
    def __init__(self, value):
        self.as_float = value
        self.begin_callbacks = {}

    def set_value(self, value):
        self.as_float = value

    @property
    def as_int(self):
        return int(self.as_float)

    def add_begin_edit_fn(self, callback):
        token = len(self.begin_callbacks) + 1
        self.begin_callbacks[token] = callback
        return token

    def remove_begin_edit_fn(self, token):
        del self.begin_callbacks[token]

    def begin_edit(self):
        for callback in self.begin_callbacks.values():
            callback(self)


class Mirror:
    instances = []

    def __init__(self, sample):
        self.host = "192.168.1.6"
        self.ownership = ControllerOwnership(self.host)
        self.control_owner = None
        self.instances.append(self)
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
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path / "controller-locks"))
    monkeypatch.setattr(Mirror, "instances", [])
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
            self.stationary_checks = 0
            self.command_pending = False
            self.retention_reason = None
            instances.append(self)

        def read_state(self):
            return self.snapshot()[0]

        def verify_stationary(self):
            self.stationary_checks += 1
            return self.read_state()

        def feedback_retention_reason(self):
            return self.retention_reason

        def read_pose(self):
            self.read_state()
            self.pose_reads += 1
            return self.pose

        def read_pose_measurement(self):
            requested_at = time.monotonic()
            before = self.read_state()
            pose = self.read_pose()
            return SimpleNamespace(
                control=self,
                pose=pose,
                requested_at=requested_at,
                before=before,
                after=self.read_state(),
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
            self.retention_reason = None
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
            self.retention_reason = None
            return {"ok": True}

        def cancel(self, disabling=False):
            self.cancel_modes.append(disabling)
            self.stopped = True
            self.retention_reason = self.retention_reason or "Control cancellation is not confirmed"

    backend = ModuleType("mg400_agent.joint_control")
    backend.JointControl = Control
    backend.TargetRejectedError = type("TargetRejectedError", (ValueError,), {})
    backend.PreparationRejectedError = type("PreparationRejectedError", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, backend.__name__, backend)

    class KeyboardSession:
        def __init__(self, control, *, speed_percent=1):
            self.control = control
            self.speed_percent = speed_percent
            self.ready = self.closed = False
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
            self.reusable_checks = []
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
            if released.is_set() or self.closed:
                return self.control.pose
            self.movements.append(key)
            self.run_started.set()
            while not released.is_set() and not self.hold_finished.is_set():
                await asyncio.sleep(0)
            await self.finish_allowed.wait()
            return self.control.pose

        def reusable_pose(self, measurement):
            self.reusable_checks.append(measurement)
            return (
                measurement is not None
                and measurement.control is self.control
                and 0 <= time.monotonic() - measurement.requested_at <= 0.25
                and measurement.before == measurement.after == self.control.read_state()
            )

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
        panel_class.run.__globals__,
        "_reload_control_modules",
        lambda: reload_calls.append("reload"),
    )

    class Gripper:
        def __init__(self, feedback_path=None):
            self.feedback_path = feedback_path
            self.owner = None
            self.closed = self.closing = self.failed = False
            self.action = self.task = None
            self.auto_initialize_pending = True
            self.auto_initialize_at_start = None
            self.calls = []
            self.windows = []
            self.build_count = 0
            self.run_started = asyncio.Event()
            self.run_finished = asyncio.Event()
            grippers.append(self)

        @property
        def busy(self):
            return self.action is not None and not self.action.done()

        def build_compact_row(self):
            import omni.ui as ui

            self.build_count += 1
            ui.Label("Embedded gripper")

        async def run(self, window=None):
            self.windows.append(window)
            self.auto_initialize_at_start = self.auto_initialize_pending
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
    monkeypatch.setattr(builtins, namespace["SINGLETON"] + "_error_window", None, raising=False)
    monkeypatch.setattr(
        builtins, namespace["GRIPPER_SINGLETON"] + "_start_lock", None, raising=False
    )
    monkeypatch.setattr(builtins, namespace["GRIPPER_SINGLETON"], None, raising=False)
    yield SimpleNamespace(
        **namespace,
        instances=instances,
        sessions=sessions,
        config_path=config,
        grippers=grippers,
        Gripper=Gripper,
        gripper_loads=gripper_loads,
        reload_calls=reload_calls,
        feedback_path=tmp_path / "data/work/gripper_olares_feedback.json",
    )
    for mirror in Mirror.instances:
        mirror.ownership.release()


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
            self.tooltip = kwargs.get("tooltip")
            self.model = kwargs.get("model", text if isinstance(text, FloatModel) else None)
            if type(self).__name__ == "IntSlider" and self.model is None:
                self.model = FloatModel(0)
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
                return self.callback(
                    SimpleNamespace(input=KeyboardInput(key), type=event_type, modifiers=modifiers)
                )
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

    class Observer:
        def __init__(self, event_name, on_event, observer_name):
            self.event_name = event_name
            self.on_event = on_event
            self.observer_name = observer_name
            self.active = True
            self.reset_count = 0

        def reset(self):
            self.active = False
            self.reset_count += 1

    class EventDispatcher:
        def __init__(self):
            self.observers = []

        def observe_event(self, **kwargs):
            observer = Observer(**kwargs)
            self.observers.append(observer)
            return observer

        def disconnect(self):
            for observer in self.observers:
                if observer.active:
                    assert observer.event_name == (
                        "omni.kit.livestream.client_disconnected:immediate"
                    )
                    observer.on_event(SimpleNamespace())

    modules = {
        name: ModuleType(name)
        for name in (
            "carb",
            "carb.input",
            "carb.eventdispatcher",
            "omni",
            "omni.appwindow",
            "omni.kit",
            "omni.kit.app",
            "omni.timeline",
            "omni.ui",
            "omni.usd",
        )
    }
    input_interface = InputInterface()
    dispatcher = EventDispatcher()
    modules["carb"].input = modules["carb.input"]
    modules["carb"].eventdispatcher = modules["carb.eventdispatcher"]
    modules["carb.eventdispatcher"].get_eventdispatcher = lambda: dispatcher
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
    for name in (
        "Window",
        "VStack",
        "HStack",
        "Label",
        "FloatField",
        "FloatSlider",
        "IntField",
        "IntSlider",
        "Button",
    ):
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
    ownership = ControllerOwnership("robot-new.test")
    try:
        panel = panel_module.ControlPanel(
            SimpleNamespace(host="robot-new.test", ownership=ownership)
        )
        assert panel.host == panel.control.host == "robot-new.test"
        assert panel.control.calls == []
    finally:
        ownership.release()


@pytest.mark.parametrize("mirror_host", (None, "192.168.1.6"))
def test_reload_rejects_old_mirror_after_configuration_change(
    panel_module, monkeypatch, mirror_host
):
    panel_module.config_path.write_text('[robot]\nhost = "robot-new.test"\n')
    mirror = SimpleNamespace(stop_event=threading.Event())
    if mirror_host is not None:
        mirror.host = mirror_host
    install_ui(panel_module, SimpleNamespace(mirror=mirror), monkeypatch, lambda: None)
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
    panel.pose_measurement = panel.control.read_pose_measurement()

    def failed_pose():
        raise RuntimeError("Pose query failed")

    monkeypatch.setattr(panel.control, "read_pose", failed_pose)
    asyncio.run(panel.refresh_pose())
    panel.copy_actual()
    assert panel.actual_pose is None
    assert panel.pose_measurement is None
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


@pytest.mark.parametrize(
    "name,method", [("enable", "enable"), ("home", "move"), ("move", "move_linear")]
)
def test_preparation_rejection_leaves_action_available_for_explicit_retry(
    panel_module,
    panel,
    monkeypatch,
    name,
    method,
):
    original = getattr(panel.control, method)
    reject = True

    def command(*args):
        nonlocal reject
        if reject:
            reject = False
            raise panel_module.backend.PreparationRejectedError("Read connection timed out.")
        return original(*args)

    monkeypatch.setattr(panel.control, method, command)

    async def exercise():
        panel.copy_actual()
        panel.submit(name)
        await panel.action
        assert panel.message == (
            "No command sent. Read connection timed out. Retry when feedback is ready."
        )
        assert panel.stop_task is None
        assert not panel.control.stopped
        assert panel.control.calls == []
        panel.control.release_move.set()
        panel.submit(name)
        await panel.action
        assert len(panel.control.calls) == 1
        assert panel.control.calls[0][0] == method
        assert not panel.control.stopped

    asyncio.run(exercise())


def test_disabled_precheck_rejection_keeps_enable_button_and_gripper_unchanged(
    panel_module,
    panel,
    monkeypatch,
):
    panel.control.set_mode(4)
    child = panel_module.Gripper()
    child.auto_initialize_pending = False
    child.owner = panel
    panel.gripper = child
    enable = panel.control.enable
    rejected = False

    def reject_once():
        nonlocal rejected
        if not rejected:
            rejected = True
            raise panel_module.backend.PreparationRejectedError(
                "The disabled robot is not stationary. Wait until it is still."
            )
        return enable()

    def gripper_command(*args, **kwargs):
        child.calls.append((args, kwargs))
        raise AssertionError("Enable must not send a gripper command")

    monkeypatch.setattr(panel.control, "enable", reject_once)
    child.submit_position = child.submit_initialization = gripper_command
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        panel.mirror.latest = panel.control.read_state(), time.monotonic()
        button = next(widget for widget in widgets if widget.text == "Enable robot")
        if frames == 2:
            assert button.enabled
            button.clicked_fn()
            await panel.action
            assert "No command sent." in panel.message
            assert "not stationary" in panel.message
            assert panel.stop_task is None
            assert not panel.control.stopped
            assert not panel.busy
            assert panel.control.calls == panel.control.cancel_modes == child.calls == []
            assert not panel.keyboard_enabled
        elif frames == 3:
            assert button.enabled
            assert any(widget.text == panel.message for widget in widgets)
            button.clicked_fn()
            await panel.action
            assert panel.message == "Enabled. No movement sent."
            assert panel.control.calls == [("enable",)]
            assert child.calls == []
        elif frames == 4:
            assert not button.enabled
            assert not panel.control.stopped
            assert panel.stop_task is None
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 4
    assert panel.control.calls == [("enable",)]
    assert panel.control.cancel_modes == child.calls == []


def test_disable_cancels_once_drains_move_and_allows_explicit_enable_then_move(panel_module, panel):
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


def test_speed_edit_closes_session_and_next_keyboard_uses_selected_speed(panel):
    panel.window = SimpleNamespace(exclusive_keyboard=True)

    async def exercise():
        old = await arm_keyboard(panel)
        panel.keyboard_speed_model = FloatModel(10)
        panel.begin_speed_edit()
        assert old.closed and not panel.keyboard_enabled
        assert not panel.window.exclusive_keyboard
        assert panel.control.calls == []
        new = await arm_keyboard(panel)
        assert new is not old and new.speed_percent == 10
        panel.keyboard_speed_model.set_value(20)
        assert new.speed_percent == 10

    asyncio.run(exercise())


def test_speed_edit_cancels_preparation_waiting_for_pose(panel):
    panel.window = SimpleNamespace(exclusive_keyboard=False)

    async def exercise():
        waiting = asyncio.Event()
        panel.pose_task = asyncio.create_task(waiting.wait())
        panel.request_keyboard()
        old = panel.keyboard_session
        await asyncio.sleep(0)
        panel.begin_speed_edit()
        waiting.set()
        await panel.keyboard_task
        assert old.closed and not old.prepare_started.is_set()
        assert not panel.keyboard_enabled
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_speed_controls_disable_during_keyboard_hold(panel_module, panel, monkeypatch):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        panel.mirror.latest = panel.control.read_state(), time.monotonic()
        controls = [
            widget for widget in widgets if type(widget).__name__ in ("IntSlider", "IntField")
        ]
        if frames == 2:
            assert len(controls) == 2 and all(widget.enabled for widget in controls)
            assert controls[0].model is controls[1].model is panel.keyboard_speed_model
            panel.keyboard_speed_model.begin_edit()
            panel.keyboard_speed_model.set_value(10)
            session = await arm_keyboard(panel)
            assert session.speed_percent == 10
            panel.key_event("Q", True)
            await session.run_started.wait()
        elif frames == 3:
            assert all(not widget.enabled for widget in controls)
            panel.key_event("Q", False)
            await panel.action
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 3
    assert not panel.keyboard_speed_model.begin_callbacks


@pytest.mark.parametrize("j4", (-360.0, -359.0, -220.0, 220.0, 359.0, 360.0))
def test_keyboard_request_accepts_measured_j4_within_raw_range(panel, j4):
    sample = replace(panel.control.read_state(), joints_deg=(1.0, 2.0, 3.0, j4))
    panel.mirror.latest = sample, time.monotonic()
    asyncio.run(arm_keyboard(panel))
    assert panel.keyboard_enabled
    assert panel.control.calls == []


@pytest.mark.parametrize("j4", (-360.001, 360.001))
def test_keyboard_request_rejects_measured_j4_outside_raw_range(panel, j4):
    sample = replace(panel.control.read_state(), joints_deg=(1.0, 2.0, 3.0, j4))
    panel.mirror.latest = sample, time.monotonic()
    panel.request_keyboard()
    assert panel.keyboard_task is None
    assert panel.keyboard_session is None
    assert not panel.keyboard_enabled
    assert not panel.keyboard_arming
    assert panel.control.calls == []
    assert panel.message.startswith("Keyboard unavailable:")


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
        assert panel.pose_measurement is measurement
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        assert session.initial_poses == [measurement]
        assert session.initial_poses[0] is panel.pose_task.result()
        assert panel.actual_pose == measurement.pose
        panel.key_event("W", False)
        await asyncio.wait_for(panel.action, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("key", tuple("WASDRFQE"))
def test_continuous_jog_does_not_wait_for_pending_pose_query(panel, key):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.pose_task = asyncio.get_running_loop().create_future()
        panel.key_event(key, True)
        try:
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert not panel.pose_task.done()
            assert session.initial_poses == [None]
            assert session.reusable_checks == []
        finally:
            panel.key_event(key, False)
            await asyncio.wait_for(panel.action, 1)
            panel.pose_task.cancel()

    asyncio.run(exercise())


@pytest.mark.parametrize("key", tuple("WS"))
def test_fresh_pose_cache_skips_pending_pose_query(panel, key):
    async def exercise():
        session = await arm_keyboard(panel)
        measurement = await panel.refresh_pose()
        panel.pose_task = asyncio.get_running_loop().create_future()
        panel.key_event(key, True)
        try:
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert not panel.pose_task.done()
            assert session.initial_poses == [measurement]
            assert session.initial_poses[0] is panel.pose_measurement
            assert session.reusable_checks == []
        finally:
            panel.key_event(key, False)
            await asyncio.wait_for(panel.action, 1)
            panel.pose_task.cancel()

    asyncio.run(exercise())


@pytest.mark.parametrize("key", tuple("WS"))
def test_stale_pose_cache_does_not_delay_continuous_jog(panel, key):
    async def exercise():
        session = await arm_keyboard(panel)
        stale = await panel.refresh_pose()
        stale.requested_at = time.monotonic() - 1
        panel.pose_task = asyncio.get_running_loop().create_future()
        panel.key_event(key, True)
        try:
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert not panel.pose_task.done()
            assert session.reusable_checks == []
            assert session.initial_poses == [stale]
        finally:
            panel.key_event(key, False)
            await asyncio.wait_for(panel.action, 1)
            panel.pose_task.cancel()

    asyncio.run(exercise())


def test_release_with_pending_pose_query_does_not_start_delayed_motion(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        measurement = panel.control.read_pose_measurement()
        panel.pose_task = asyncio.get_running_loop().create_future()
        panel.key_event("W", True)
        assert panel.busy
        assert session.run_calls == []
        panel.key_event("W", False)
        assert panel.jog_released.is_set()
        await asyncio.wait_for(panel.action, 1)
        assert not panel.pose_task.done()
        assert session.initial_poses == [None]
        assert session.reusable_checks == []
        panel.pose_task.set_result(measurement)
        await asyncio.sleep(0)
        assert session.run_calls == ["W"]
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


@pytest.mark.parametrize("next_key", ["S", "W"])
def test_keyboard_keeps_new_press_until_previous_stop_finishes(panel, next_key):
    async def exercise():
        session = await arm_keyboard(panel)
        session.finish_allowed.clear()
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        first_action = panel.action
        session.run_started.clear()
        panel.key_event("W", False)
        panel.key_event(next_key, True)
        assert panel.action is first_action
        assert session.run_calls == ["W"]
        session.finish_allowed.set()
        try:
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert first_action.done()
            assert session.run_calls == ["W", next_key]
        finally:
            panel.key_event(next_key, False)
            await asyncio.wait_for(panel.action, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("keep_latest", [False, True])
def test_keyboard_does_not_replay_released_presses_during_stop(panel, keep_latest):
    async def exercise():
        session = await arm_keyboard(panel)
        session.finish_allowed.clear()
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        first_action = panel.action
        session.run_started.clear()
        panel.key_event("W", False)
        panel.key_event("S", True)
        panel.key_event("S", False)
        if keep_latest:
            panel.key_event("R", True)
        session.finish_allowed.set()
        try:
            if keep_latest:
                await asyncio.wait_for(session.run_started.wait(), 1)
            else:
                await asyncio.wait_for(first_action, 1)
                await asyncio.sleep(0)
            assert session.run_calls == (["W", "R"] if keep_latest else ["W"])
        finally:
            panel.key_event("R", False)
            await asyncio.wait_for(panel.action, 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["disarm", "stop", "disable", "close"])
def test_keyboard_revocation_clears_direction_waiting_for_stop(panel, operation):
    async def exercise():
        session = await arm_keyboard(panel)
        session.finish_allowed.clear()
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("W", False)
        panel.key_event("S", True)
        if operation == "disarm":
            panel.set_keyboard(False, reason="window lost focus.")
        else:
            getattr(panel, f"request_{operation}")()
        session.finish_allowed.set()
        await asyncio.wait_for(panel.action, 1)
        for task in (panel.stop_task, panel.disable_task):
            if task:
                await asyncio.wait_for(task, 1)
        await asyncio.sleep(0)
        assert session.run_calls == ["W"]
        assert panel.held_keys == set()
        assert not panel.keyboard_enabled

    asyncio.run(exercise())


def test_keyboard_fault_clears_direction_waiting_for_stop(panel, monkeypatch):
    async def exercise():
        session = await arm_keyboard(panel)
        run = type(session).run

        async def fail(self, *args, **kwargs):
            await run(self, *args, **kwargs)
            session.fault = "Keyboard stop unconfirmed"
            raise RuntimeError(session.fault)

        monkeypatch.setattr(type(session), "run", fail)
        session.finish_allowed.clear()
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        panel.key_event("W", False)
        panel.key_event("S", True)
        session.finish_allowed.set()
        await asyncio.wait_for(panel.action, 1)
        await asyncio.sleep(0)
        assert session.run_calls == ["W"]
        assert panel.held_keys == set()
        assert not panel.keyboard_enabled
        assert "stop unconfirmed" in panel.message

    asyncio.run(exercise())


def test_completed_arm_hold_requires_keyup_before_another_hold(panel):
    async def exercise():
        session = await arm_keyboard(panel)
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        # A completed backend operation must not restart from repeated key events.
        session.hold_finished.set()
        first_action = panel.action
        await asyncio.wait_for(first_action, 1)
        assert "Stopped. Hold a key" in panel.message
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


def test_keyboard_preparation_rejection_allows_explicit_retry_without_stop(
    panel_module,
    panel,
    monkeypatch,
):
    original = panel_module.KeyboardJogSession.prepare
    reject = True

    async def prepare(session):
        nonlocal reject
        if reject:
            reject = False
            raise panel_module.backend.PreparationRejectedError("Read connection timed out.")
        return await original(session)

    monkeypatch.setattr(panel_module.KeyboardJogSession, "prepare", prepare)

    async def exercise():
        panel.request_keyboard()
        await panel.keyboard_task
        assert panel.message == (
            "Keyboard preparation deferred: Read connection timed out. Click Keyboard to retry."
        )
        assert not panel.keyboard_enabled
        assert not panel.keyboard_arming
        assert not panel.control.stopped
        assert panel.stop_task is None
        assert panel.control.calls == []
        await arm_keyboard(panel)
        assert panel.keyboard_enabled
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
def test_prepare_fault_does_not_overwrite_shutdown_message(panel, failed_preparation, owner):
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
            closed=False,
            failed=False,
            busy=False,
            measured_position=measured_position,
            submit_position=submit_position,
            report={},
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


@pytest.mark.parametrize("revoke", [False, True])
def test_arm_key_waits_for_released_gripper_step(panel_module, panel, revoke):
    async def exercise():
        session = await arm_keyboard(panel)
        child = panel_module.Gripper()
        child.owner = panel
        child.report = {}
        child.measured_position = lambda: 500
        panel.gripper = child
        targets = []
        submitted = asyncio.Event()
        step_done = asyncio.get_running_loop().create_future()

        def submit(position):
            targets.append(position)
            child.action = step_done
            submitted.set()
            return step_done

        child.submit_position = submit
        panel.key_event("Z", True)
        await asyncio.wait_for(submitted.wait(), 1)
        gripper_action = panel.action
        panel.key_event("Z", False)
        panel.key_event("W", True)
        await asyncio.sleep(0)
        assert panel.action is gripper_action
        assert not gripper_action.done()
        assert session.run_calls == []
        assert targets == [525]
        if revoke:
            panel.set_keyboard(False)
        step_done.set_result(None)
        if revoke:
            await asyncio.wait_for(gripper_action, 1)
            await asyncio.sleep(0)
            assert session.run_calls == []
            assert not panel.keyboard_enabled
        else:
            await asyncio.wait_for(session.run_started.wait(), 1)
            assert gripper_action.done()
            assert session.run_calls == ["W"]
            panel.key_event("W", False)
            await asyncio.wait_for(panel.action, 1)
        assert targets == [525]
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
            feedback.receive(
                {
                    "session": "b8af7f0a-d1a1-4887-adfe-d470d5527de6",
                    "seq": 1,
                    "sent_at": time.time(),
                    "init": init,
                    "state": 65535,
                    "position": 920,
                }
            )
        targets = []
        panel.gripper = SimpleNamespace(
            closed=False,
            failed=False,
            busy=False,
            feedback=feedback,
            measured_position=lambda: None,
            submit_position=targets.append,
        )
        panel.key_event(key, True)
        await asyncio.wait_for(panel.action, 1)
        assert panel.message == f"Gripper unavailable: {message} Arm keys remain ready."
        assert panel.keyboard_enabled
        assert targets == []
        assert panel.control.calls == []
        panel.key_event(key, False)
        panel.key_event("W", True)
        await asyncio.wait_for(panel.keyboard_session.run_started.wait(), 1)
        panel.key_event("W", False)
        await asyncio.wait_for(panel.action, 1)
        assert panel.keyboard_session.movements == ["W"]

    asyncio.run(exercise())


@pytest.mark.parametrize("stop", [False, True])
def test_gripper_prepublication_rejection_keeps_arm_keys_or_preserves_requested_stop(panel, stop):
    async def exercise():
        await arm_keyboard(panel)
        started, finish = asyncio.Event(), asyncio.Event()
        reason = "No command sent. The USB collector feedback changed. Check feedback and retry."

        async def reject():
            started.set()
            await finish.wait()
            return False

        panel.gripper = SimpleNamespace(
            closed=False,
            failed=False,
            busy=False,
            measured_position=lambda: 500,
            report={"result": reason},
            submit_position=lambda target: asyncio.create_task(reject()),
        )
        panel.key_event("Z", True)
        await started.wait()
        if stop:
            panel.request_stop()
            await panel.stop_task
            stop_message = panel.message
        finish.set()
        await panel.action
        if stop:
            assert panel.message == stop_message
            assert not panel.keyboard_enabled
            assert panel.control.stopped
            assert panel.control.calls == [("stop",)]
        else:
            assert panel.message == f"Gripper unavailable: {reason} Arm keys remain ready."
            assert panel.keyboard_enabled
            assert not panel.control.stopped
            assert panel.control.calls == []
            panel.key_event("Z", False)

    asyncio.run(exercise())


def test_gripper_feedback_loss_between_measurement_and_submit_keeps_arm_keys(panel):
    async def exercise():
        await arm_keyboard(panel)
        reason = "Position control needs a fresh --commands collector."
        targets = []
        panel.gripper = SimpleNamespace(
            closed=False,
            failed=False,
            busy=False,
            measured_position=lambda: 500,
            report={"result": reason, "command_published": False},
            submit_position=targets.append,
        )
        panel.key_event("X", True)
        await panel.action
        assert panel.keyboard_enabled
        assert panel.message == f"Gripper unavailable: {reason} Arm keys remain ready."
        assert targets == [475]
        assert panel.control.calls == []
        panel.key_event("X", False)

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
            closed=False,
            failed=False,
            busy=False,
            measured_position=lambda: current[0],
            submit_position=submit_position,
            report={},
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
            closed=False,
            failed=False,
            busy=False,
            measured_position=lambda: 350,
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
            closed=False,
            failed=stage == "before",
            busy=False,
            measured_position=lambda: 350,
            report={"result": reason},
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


def test_gripper_hold_deadline_waits_for_keyup_before_restarting(panel_module, panel, monkeypatch):
    async def exercise():
        await arm_keyboard(panel)
        clock, current, targets = [0.0], [500], []
        monkeypatch.setitem(
            panel_module.run_keyboard_hold.__globals__,
            "time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        )

        async def complete(value):
            current[0] = value
            clock[0] += 5.0

        def submit_position(value):
            targets.append(value)
            return asyncio.create_task(complete(value))

        gripper = SimpleNamespace(
            closed=False,
            failed=False,
            busy=False,
            measured_position=lambda: current[0],
            submit_position=submit_position,
            report={},
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


def test_start_constructs_owned_gripper_from_shared_feedback_path(panel_module, panel, monkeypatch):
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
            assert "Sync: live" in labels
            assert "  ".join(f"J{i} {i:.2f}°" for i in range(1, 5)) in labels
            assert sum(type(widget).__name__ == "Window" for widget in widgets) == 1
            panel.request_close()

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
            panel.request_close()
        else:
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)

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
            panel.request_close()
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
        panel.request_close()

    install_ui(panel_module, panel, monkeypatch, next_update)

    async def exercise():
        if fault == "pending_ack":
            panel.control.command_pending = True
            panel.control.retention_reason = "A controller acknowledgement is pending"
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
        assert panel.report["synchronization_retained"]
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            ControllerOwnership(panel.host).acquire()

    asyncio.run(exercise())


@pytest.mark.parametrize("disable_first", [False, True])
def test_sync_stop_verifies_stationary_feedback_before_disconnect(panel, disable_first):
    async def exercise():
        if disable_first:
            panel.request_disable()
            await panel.disable_task
            assert panel.control.read_state().mode == 4
        panel.request_sync_stop()
        await panel.sync_task
        assert panel.control.stationary_checks == 1
        assert panel.mirror.stop_event.is_set()
        assert panel.message == "Synchronization stopped. Click Start sync to reconnect."

    asyncio.run(exercise())


def test_sync_stop_after_disable_retains_feedback_if_stationarity_is_unverified(
    panel,
    monkeypatch,
):
    async def exercise():
        panel.request_disable()
        await panel.disable_task
        disabled_message = panel.message

        def verify():
            assert not panel.sync_enabled
            assert not panel.mirror.stop_event.is_set()
            raise TimeoutError("Joint feedback changed during the stationary check")

        monkeypatch.setattr(panel.control, "verify_stationary", verify)
        panel.request_sync_stop()
        await panel.sync_task
        assert not panel.mirror.stop_event.is_set()
        assert panel.report["synchronization_retained"]
        assert panel.message.startswith(disabled_message)
        assert "Joint feedback changed" in panel.message
        assert "Synchronization remains active" in panel.message
        assert panel.control.calls == []

    asyncio.run(exercise())


def test_sync_stop_preserves_original_disable_error_without_rechecking(panel):
    panel.control.retention_reason = "Disable acknowledgement was lost"
    panel.control.cancel(disabling=True)
    panel.message = "DISABLE UNCONFIRMED: Disable acknowledgement was lost"

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        assert panel.control.stationary_checks == 0
        assert panel.control.calls == []
        assert not panel.mirror.stop_event.is_set()
        assert panel.message.startswith("DISABLE UNCONFIRMED: Disable acknowledgement was lost")
        assert "Synchronization remains active" in panel.message

    asyncio.run(exercise())


def test_sync_stop_rechecks_command_uncertainty_after_stationary_verification(panel, monkeypatch):
    def verify():
        panel.control.retention_reason = "Controlled stop is not confirmed"
        return panel.control.read_state()

    monkeypatch.setattr(panel.control, "verify_stationary", verify)

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        assert not panel.mirror.stop_event.is_set()
        assert panel.report["synchronization_retained"]
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("confirmed", (False, True))
def test_sync_stop_uses_controller_verdict_after_keyboard_stop(panel_module, panel, confirmed):
    panel.keyboard_session = panel_module.KeyboardJogSession(panel.control)
    panel.control.stopped = True
    panel.control.retention_reason = None if confirmed else "Keyboard stop is not confirmed"

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        assert panel.keyboard_session.closed
        assert panel.control.stationary_checks == int(confirmed)
        assert panel.mirror.stop_event.is_set() is confirmed
        assert panel.report.get("synchronization_retained", False) is not confirmed
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("stop_confirmed", (False, True))
def test_sync_stop_preserves_keyboard_verdict_across_disable_replacement(
    panel_module,
    panel,
    monkeypatch,
    stop_confirmed,
):
    original_run = panel_module.KeyboardJogSession.run

    async def run(session, *args, **kwargs):
        result = await original_run(session, *args, **kwargs)
        if not stop_confirmed:
            session.control.retention_reason = "Keyboard stop is not confirmed"
            session.fault = session.control.retention_reason
            raise RuntimeError(session.fault)
        return result

    monkeypatch.setattr(panel_module.KeyboardJogSession, "run", run)

    async def exercise():
        session = await arm_keyboard(panel)
        original_control = panel.control
        session.finish_allowed.clear()
        panel.key_event("W", True)
        await asyncio.wait_for(session.run_started.wait(), 1)
        try:
            panel.request_disable()
            assert await asyncio.to_thread(original_control.disable_finished.wait, 1)
            assert panel.control is original_control
            assert not panel.action.done()
            assert not panel.disable_task.done()
        finally:
            session.finish_allowed.set()
            await asyncio.wait_for(panel.action, 1)
            await asyncio.wait_for(panel.disable_task, 1)
        assert panel.control is not original_control
        assert session.control is original_control
        assert panel.control.feedback_retention_reason() is None
        assert panel.message == "Disabled. Click Enable robot to enable again."

        panel.request_sync_stop()
        await asyncio.wait_for(panel.sync_task, 1)
        assert panel.mirror.stop_event.is_set() is stop_confirmed
        assert panel.control.stationary_checks == int(stop_confirmed)
        assert panel.report.get("synchronization_retained", False) is not stop_confirmed
        if not stop_confirmed:
            panel.keyboard_session = panel_module.KeyboardJogSession(panel.control)
            await panel.close_synchronization()
            assert panel.mirror.stop_event.is_set()
            assert panel.control.stationary_checks == 1
            assert "synchronization_retained" not in panel.report

    asyncio.run(exercise())


@pytest.mark.parametrize("disable_confirmed", [False, True])
@pytest.mark.parametrize("verification_failed", [False, True])
def test_sync_stop_drains_disable_started_during_stationary_verification(
    panel,
    monkeypatch,
    disable_confirmed,
    verification_failed,
):
    control = panel.control
    verifying, verify_allowed, verify_finished = (threading.Event() for _ in range(3))
    disabling, disable_allowed = threading.Event(), threading.Event()
    original_disable = control.disable

    def verify():
        control.stationary_checks += 1
        verifying.set()
        assert verify_allowed.wait(2), "Test did not release stationary verification"
        verify_finished.set()
        if verification_failed:
            raise RuntimeError("Verification invalidated by disable")
        return control.read_state()

    def disable():
        disabling.set()
        assert disable_allowed.wait(2), "Test did not release disable"
        if not disable_confirmed:
            control.retention_reason = "Disable acknowledgement was lost"
            raise RuntimeError(control.retention_reason)
        return original_disable()

    monkeypatch.setattr(control, "verify_stationary", verify)
    monkeypatch.setattr(control, "disable", disable)

    async def exercise():
        try:
            panel.request_sync_stop()
            assert await asyncio.to_thread(verifying.wait, 1)
            panel.request_disable()
            assert await asyncio.to_thread(disabling.wait, 1)
            verify_allowed.set()
            assert await asyncio.to_thread(verify_finished.wait, 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(panel.sync_task), 0.05)
            assert not panel.mirror.stop_event.is_set()
            disable_allowed.set()
            await asyncio.wait_for(panel.sync_task, 1)
            assert control.stationary_checks == 1
            if disable_confirmed:
                assert panel.control is not control
                assert panel.control.stationary_checks == 1
                assert panel.mirror.stop_event.is_set()
                assert panel.message == "Synchronization stopped. Click Start sync to reconnect."
            else:
                assert panel.control is control
                assert not panel.mirror.stop_event.is_set()
                assert panel.report["synchronization_retained"]
                assert panel.message.startswith(
                    "DISABLE UNCONFIRMED: Disable acknowledgement was lost"
                )
                assert "Synchronization remains active" in panel.message
            assert "Verification invalidated" not in panel.message
        finally:
            verify_allowed.set()
            disable_allowed.set()
            for task in (panel.disable_task, panel.sync_task):
                if task is not None:
                    await asyncio.wait_for(task, 1)

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


@pytest.mark.parametrize("auto_initialize", [None, False, True])
def test_repeated_start_replaces_old_window_and_owned_child(
    panel_module,
    panel,
    monkeypatch,
    auto_initialize,
):
    if auto_initialize is not None:
        monkeypatch.setitem(
            panel_module.start.__globals__, "AUTO_INITIALIZE_GRIPPER", auto_initialize
        )
    expected_auto_initialize = auto_initialize is True

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
            assert all(
                child.auto_initialize_at_start is expected_auto_initialize
                for child in panel_module.grippers
            )
            assert all(
                created.report["gripper_auto_initialize_on_open"] is expected_auto_initialize
                for created in (first, second)
            )
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
            first.request_close()
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
                    created.request_close()
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
    panel.control.command_pending = True
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
        "joint_limits": (
            "def validate_joints(joints): return joints\n"
            "def validate_measured_joints(joints): return joints\n"
        ),
        "joint_control": (
            "from .controller_protocol import ControllerCommandError, parse_controller_response\n"
            "class JointControl:\n"
            "    def __init__(self, host, snapshot, *, parent=None, command_timeout=2, "
            "send_guard=None):\n"
            "        self.parent = parent\n"
            "    def begin_operation(self, *, command_timeout=0.5, send_guard=None):\n"
            "        return JointControl('unused', lambda: None, parent=self, "
            "send_guard=send_guard)\n"
            "    def solve_pose(self, target): return target\n"
            "class TargetRejectedError(ValueError): pass\n"
        ),
        "keyboard_jog": (
            "from . import joint_control\n"
            "from .joint_control import TargetRejectedError\n"
            "class KeyboardJogSession:\n"
            "    def __init__(self, control): self.control = control\n"
            "    def _start(self): return self.control.begin_operation()\n"
        ),
        "gripper_feedback": "class FeedbackState: pass\n",
        "keyboard_control": (
            "class KeyboardController: pass\n"
            "class GripperUnavailableError(RuntimeError): pass\n"
            "async def run_keyboard_hold(*args, **kwargs): pass\n"
        ),
    }
    package = sys.modules["mg400_agent"]
    feedback = sys.modules["mg400_agent.feedback"]
    source_dir = tmp_path / "src/mg400_agent"
    source_dir.mkdir(parents=True)
    (source_dir / "feedback.py").write_bytes(
        (Path(__file__).resolve().parents[1] / "src/mg400_agent/feedback.py").read_bytes()
    )
    for name, source in sources.items():
        (source_dir / f"{name}.py").write_text(source)
        cached = ModuleType(f"mg400_agent.{name}")
        cached.__file__ = f"/legacy/mg400_agent/{name}.py"
        monkeypatch.setitem(sys.modules, cached.__name__, cached)
        monkeypatch.setattr(package, name, cached, raising=False)
    globals_ = panel_module._reload_control_modules.__globals__
    monkeypatch.setitem(globals_, "ROOT", tmp_path)
    panel_module._reload_control_modules()
    protocol, limits, control, jog, gripper, keyboard = (
        sys.modules[f"mg400_agent.{name}"] for name in sources
    )
    assert globals_["limits"] is limits
    assert gripper.FeedbackState.__module__ == "mg400_agent.gripper_feedback"
    assert globals_["backend"] is control
    assert globals_["JointControl"] is control.JointControl
    assert globals_["KeyboardJogSession"] is jog.KeyboardJogSession
    assert globals_["KeyboardController"] is keyboard.KeyboardController
    assert globals_["run_keyboard_hold"] is keyboard.run_keyboard_hold
    assert globals_["GripperUnavailableError"] is keyboard.GripperUnavailableError
    assert control.ControllerCommandError is protocol.ControllerCommandError
    assert control.parse_controller_response is protocol.parse_controller_response
    assert jog.joint_control is control
    assert jog.TargetRejectedError is control.TargetRejectedError
    assert control.JointControl("robot.test", lambda: None, parent="parent").parent == "parent"
    parent = control.JointControl("robot.test", lambda: None)
    child = jog.KeyboardJogSession(parent)._start()
    assert type(child) is control.JointControl
    assert child.parent is parent
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


@pytest.mark.parametrize("operation", (None, "move"))
def test_run_initializes_passively_and_stops_active_move_on_close(
    panel_module, panel, monkeypatch, operation
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
            if operation:
                panel.submit(operation)
                assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    panel.actual_pose = None
    panel.pose_received = 0.0
    asyncio.run(panel.run())

    assert frames == 3
    assert panel.closed
    assert panel.window.destroyed
    expected_calls = {
        None: [],
        "move": [("move_linear", (1.0, 2.0, 3.0, 4.0)), ("stop",)],
    }[operation]
    assert panel.control.calls == expected_calls
    assert panel.control.cancel_modes == ([False] if operation else [])
    assert panel.report["status"] == "closed"
    assert panel.report["motion_sent_by_startup"] is False
    input_interface = sys.modules["carb.input"].acquire_input_interface()
    assert input_interface.subscribed == []
    assert input_interface.unsubscribed == []
    assert input_interface.callback is None


@pytest.mark.parametrize(
    ("mode", "j4", "busy", "expected_enabled"),
    (
        (4, 4.0, False, [True, False, False, False, False]),
        (5, 4.0, False, [False, True, True, True, True]),
        (5, 4.0, True, [False, True, False, False, True]),
        (4, -227.81, False, [True, False, False, False, False]),
        (5, -227.81, False, [False, True, True, True, True]),
        (5, 227.81, False, [False, True, True, True, True]),
        (5, -227.81, True, [False, True, False, False, True]),
        (5, -359.0, False, [False, True, True, True, True]),
        (5, 359.0, False, [False, True, True, True, True]),
        (5, -360.001, False, [False, True, False, False, False]),
        (5, 360.001, False, [False, True, False, False, False]),
    ),
)
def test_ui_button_rows_and_enabled_states(
    panel_module, panel, monkeypatch, mode, j4, busy, expected_enabled
):
    sample = replace(panel.control.read_state(), mode=mode, joints_deg=(1.0, 2.0, 3.0, j4))
    panel.mirror.latest = sample, time.monotonic()
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
                "Start sync",
                "Stop sync",
                "Home",
                "Execute targets",
                "Keyboard: OFF",
                "Enable robot",
                "Disable robot",
            ]
            start, stop, home, move, keyboard, enable, disable = buttons
            assert not start.enabled
            assert stop.enabled
            assert start.clicked_fn == panel.request_sync_start
            assert stop.clicked_fn == panel.request_sync_stop
            assert enable.parent is disable.parent
            assert type(enable.parent).__name__ == "HStack"
            assert home.parent is move.parent
            assert keyboard.parent.parent is home.parent.parent
            assert [button.enabled for button in (enable, disable, home, move, keyboard)] == (
                expected_enabled
            )
            sliders = [widget for widget in widgets if type(widget).__name__ == "FloatSlider"]
            assert len(sliders) == 4
            fields = [widget for widget in widgets if type(widget).__name__ == "FloatField"]
            assert len(fields) == 4
            assert all(field.enabled == (not busy) for field in fields + sliders)
            assert all(field.model is slider.model for field, slider in zip(fields, sliders))
            title = next(widget for widget in widgets if widget.text == "Coordinates & Motion")
            assert "User=0, Tool=0" in title.tooltip
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
            assert all(field.enabled for field in fields)
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
                (key, 1 << 30, True),
                (key, 1 << 30, True),
                (key, 1 << 30, False),
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


@pytest.mark.parametrize("worker_thread", [False, True])
def test_stream_disconnect_revokes_immediately_and_requires_explicit_rearming(
    panel_module, panel, monkeypatch, worker_thread
):
    frames = 0
    observer = None
    sessions = []
    child = panel_module.Gripper()
    child.owner = panel
    child.report = {"result": "Unexpected gripper request"}
    child.measured_position = lambda: 500
    panel.gripper = child
    targets = []

    def submit(position):
        targets.append(position)
        return None

    child.submit_position = submit

    async def next_update():
        nonlocal frames, observer
        frames += 1
        if frames == 1:
            sessions.append(await arm_keyboard(panel))
        if frames == 2:
            session = sessions[0]
            session.finish_allowed.clear()
            panel.key_event("W", True)
            await asyncio.wait_for(session.run_started.wait(), 1)
            action = panel.action
            panel.key_event("W", False)
            panel.key_event("Z", True)
            assert panel.held_keys == {"Z"}
            assert not action.done()
            dispatcher = sys.modules["carb.eventdispatcher"].get_eventdispatcher()
            observer = dispatcher.observers[0]
            assert observer is panel.stream_observer
            errors = []

            def disconnect():
                try:
                    dispatcher.disconnect()
                    assert panel.jog_released.is_set()
                    assert session.closed
                    assert panel.keyboard_enabled  # UI mutation waits for the event loop.
                except BaseException as error:
                    errors.append(error)

            if worker_thread:
                worker = threading.Thread(target=disconnect)
                worker.start()
                worker.join(timeout=1)
                assert not worker.is_alive()
            else:
                disconnect()
            assert errors == []
            assert targets == []
            session.finish_allowed.set()
            await asyncio.sleep(0)
            await asyncio.wait_for(action, 1)
            assert targets == []
            assert not panel.keyboard_enabled
            assert panel.held_keys == set()
            assert "Stream disconnected" in panel.message
            panel.key_event("W", True)
            assert panel.action is action
            assert session.run_calls == ["W"]
            sessions.append(await arm_keyboard(panel))
            assert sessions[1] is not session
            panel.key_event("W", True)
            await asyncio.wait_for(sessions[1].run_started.wait(), 1)
            panel.key_event("W", False)
            await asyncio.wait_for(panel.action, 1)
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert observer.reset_count == 1 and not observer.active
    assert panel.stream_observer is None
    assert targets == []
    assert all(session.closed for session in sessions)
    message = panel.message
    release_calls = sessions[-1].release_calls
    observer.on_event(SimpleNamespace())  # A late callback must not use the closed event loop.
    assert sessions[-1].release_calls > release_calls
    assert panel.message == message


def test_stream_disconnect_cancels_keyboard_action_before_worker_starts(
    panel_module, panel, monkeypatch
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
        if frames == 2:
            panel.key_event("A", True)
            session = panel.keyboard_session
            sys.modules["carb.eventdispatcher"].get_eventdispatcher().disconnect()
            assert panel.jog_released.is_set() and session.closed
            await asyncio.wait_for(panel.action, 1)
            assert not panel.keyboard_enabled
            assert session.movements == []
            assert "Stream disconnected" in panel.message
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.control.calls == []


def test_stream_disconnect_stops_gripper_after_current_step(panel_module, panel, monkeypatch):
    frames = 0
    child = panel_module.Gripper()
    child.owner = panel
    child.report = {}
    panel.gripper = child
    targets = []
    started, finish = asyncio.Event(), asyncio.Event()

    async def step():
        started.set()
        await finish.wait()

    def submit(position):
        targets.append(position)
        child.action = asyncio.create_task(step())
        return child.action

    child.measured_position = lambda: 500
    child.submit_position = submit

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
        if frames == 2:
            panel.key_event("Z", True)
            await asyncio.wait_for(started.wait(), 1)
            sys.modules["carb.eventdispatcher"].get_eventdispatcher().disconnect()
            assert panel.jog_released.is_set()
            assert not panel.action.done()
            finish.set()
            await asyncio.wait_for(panel.action, 1)
            assert targets == [525]
            assert not panel.keyboard_enabled
            assert "Stream disconnected" in panel.message
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert child.closed


@pytest.mark.parametrize("disarm", ("focus", "hidden", "modifier"))
def test_window_keyboard_disarms_and_releases_active_hold(panel_module, panel, monkeypatch, disarm):
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


def test_sync_stop_keeps_window_open_and_start_reconnects_without_commands(
    panel_module, panel, monkeypatch
):
    frames = 0
    original_mirror = panel.mirror
    replacement = Mirror(original_mirror.latest[0])
    replacement.latest = None
    replacement.window = None
    reloads = []

    def restart(path, **kwargs):
        reloads.append((Path(path).name, kwargs))

        async def start():
            setattr(builtins, panel_module.MIRROR_SINGLETON, replacement)

        return {"MG400_MIRROR_START_TASK": asyncio.create_task(start())}

    monkeypatch.setitem(panel.run.__globals__, "runpy", SimpleNamespace(run_path=restart))

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 2:
            panel.request_sync_stop()
            first = panel.sync_task
            panel.request_sync_stop()
            assert panel.sync_task is first
            await asyncio.wait_for(first, 1)
            assert original_mirror.stop_event.is_set()
            assert not panel.closed
            assert not panel.window.destroyed
            panel.submit("enable")
            panel.request_keyboard()
            assert panel.action is None
            assert panel.keyboard_task is None
            assert panel.control.calls == []
            panel.request_sync_start()
            await asyncio.wait_for(panel.sync_task, 1)
            assert panel.sync_enabled
            assert panel.mirror is replacement
            assert not panel.initialized
            assert panel.actual_pose is None
            assert panel.control.calls == []
            panel.request_close()

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert reloads == [("mirror.py", {"init_globals": {"SHOW_WINDOW": False}})]
    assert all(control.calls == [] for control in panel_module.instances)


def test_sync_stop_drains_active_move_before_stopping_feedback(panel):
    panel.copy_actual()
    original_stop = panel.control.stop

    def stop():
        result = original_stop()
        panel.control.release_move.set()
        return result

    panel.control.stop = stop

    async def exercise():
        panel.submit("move")
        assert await asyncio.to_thread(panel.control.move_started.wait, 1)
        panel.request_sync_stop()
        assert not panel.sync_enabled
        await asyncio.wait_for(panel.sync_task, 1)
        assert panel.action.done()
        assert panel.stop_task.done()
        assert panel.mirror.stop_event.is_set()
        assert not panel.closed
        assert not panel.keyboard_enabled

    asyncio.run(exercise())


def test_sync_stop_keeps_feedback_and_blocks_restart_when_stop_is_uncertain(panel):
    panel.control.command_pending = True
    panel.control.retention_reason = "A controller acknowledgement is pending"

    async def exercise():
        panel.request_sync_stop()
        await asyncio.wait_for(panel.sync_task, 1)
        assert not panel.mirror.stop_event.is_set()
        assert not panel.sync_enabled
        assert not panel.closed
        old = panel.sync_task
        panel.request_sync_start()
        assert panel.sync_task is old
        assert "reload" in panel.message
        panel.submit("enable")
        assert panel.control.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("disconnect", ("request_sync_stop", "request_close"))
def test_disconnect_drops_pose_refresh_waiting_for_another_read(panel, disconnect):
    panel.control.retention_reason = "Disable acknowledgement was lost"
    reads = panel.control.pose_reads

    async def exercise():
        await panel.pose_lock.acquire()
        queued = asyncio.create_task(panel.refresh_pose())
        await asyncio.sleep(0)
        getattr(panel, disconnect)()
        panel.pose_lock.release()
        await queued
        if panel.sync_task:
            await panel.sync_task
        await panel.refresh_pose()
        assert panel.control.pose_reads == reads
        assert panel.control.calls == []
        assert not panel.mirror.stop_event.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("confirmed", (True, False))
def test_sync_disconnect_releases_ownership_only_after_safe_receiver_shutdown(panel, confirmed):
    contender = ControllerOwnership(panel.host)
    if not confirmed:
        panel.control.retention_reason = "Disable acknowledgement was lost"

    async def exercise():
        receiver_done = asyncio.Event()
        panel.mirror.task = asyncio.create_task(receiver_done.wait())
        panel.request_sync_stop()
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        receiver_done.set()
        await panel.sync_task
        if confirmed:
            contender.acquire()
            contender.release()
        else:
            assert not panel.mirror.stop_event.is_set()
            with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
                contender.acquire()

    asyncio.run(exercise())


def test_reload_cannot_clear_failed_disconnect_or_release_ownership(
    panel_module, panel, monkeypatch
):
    panel.control.retention_reason = "Disable acknowledgement was lost"
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await panel_module.start()
        assert not panel.closed
        assert not panel.keep_mirror
        assert panel_module.reload_calls == []
        assert len(panel_module.instances) == 1
        assert panel.control.calls == []
        contender = ControllerOwnership(panel.host)
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()

    asyncio.run(exercise())


def test_first_panel_open_shows_competing_owner_without_safety_commands(
    panel_module, panel, monkeypatch
):
    widgets = install_ui(panel_module, panel, monkeypatch, lambda: None)
    monkeypatch.setattr(builtins, panel_module.SINGLETON, None, raising=False)
    panel.mirror.ownership.release()
    owner = ControllerOwnership(panel.host)
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            asyncio.run(panel_module.start())
        assert any(
            widget.text and "Robot controlled by another panel" in widget.text
            for widget in widgets
            if type(widget).__name__ == "Label"
        )
        assert len(panel_module.instances) == 1
        assert panel_module.gripper_loads == []
        assert panel.control.calls == []
        assert not panel.mirror.stop_event.is_set()
    finally:
        owner.release()


def test_stop_after_disconnect_cannot_send_against_another_panel(panel):
    contender = ControllerOwnership(panel.host)

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        contender.acquire()
        try:
            panel.request_stop()
            panel.request_disable()
            if panel.stop_task:
                await panel.stop_task
            assert panel.control.calls == []
            assert panel.control.cancel_modes == []
            assert panel.stop_task is None
            assert "Robot controlled by another panel" in panel.message
        finally:
            contender.release()

    asyncio.run(exercise())


def execute_gripper(panel, target=500):
    child = SimpleNamespace(
        busy=False,
        closed=False,
        closing=False,
        failed=False,
        target_baseline=925,
        report={"result": "Gripper arrival verified."},
        calls=[],
        pending_target=lambda: target,
        position_sample=lambda: {"position": 925},
    )

    def submit(position):
        async def perform():
            child.calls.append(position)

        return asyncio.create_task(perform())

    child.submit_position = submit
    panel.gripper = child
    return child


@pytest.mark.parametrize("target", (None, 500))
def test_execute_sends_changed_gripper_only_after_arm_arrival(panel, target):
    panel.copy_actual()
    child = execute_gripper(panel, target)

    async def exercise():
        panel.submit("execute")
        first = panel.action
        panel.submit("execute")
        assert panel.action is first
        assert await asyncio.to_thread(panel.control.move_started.wait, 1)
        assert child.calls == []
        panel.control.release_move.set()
        await asyncio.wait_for(first, 1)
        assert child.calls == ([] if target is None else [500])
        assert len(panel.control.calls) == 1

    asyncio.run(exercise())


def test_execute_rejects_changed_gripper_without_feedback_before_arm_motion(panel):
    panel.copy_actual()
    child = execute_gripper(panel)
    child.position_sample = lambda: None
    panel.submit("execute")
    assert panel.action is None
    assert panel.control.calls == []
    assert "No command sent" in panel.message


def test_execute_reports_gripper_failure_after_arm_arrival(panel):
    panel.copy_actual()
    child = execute_gripper(panel)
    child.submit_position = lambda target: None
    child.report["result"] = "USB feedback expired."
    panel.control.release_move.set()

    async def exercise():
        panel.submit("execute")
        await asyncio.wait_for(panel.action, 1)

    asyncio.run(exercise())
    assert len(panel.control.calls) == 1
    assert panel.message == "Arm arrival verified. Gripper incomplete: USB feedback expired."
    assert child.target_baseline == 925


def test_disable_during_arm_motion_does_not_send_pending_gripper_target(panel):
    panel.copy_actual()
    child = execute_gripper(panel)

    async def exercise():
        panel.submit("execute")
        assert await asyncio.to_thread(panel.control.move_started.wait, 1)
        panel.request_disable()
        panel.control.release_move.set()
        await asyncio.wait_for(panel.disable_task, 1)
        assert child.calls == []

    asyncio.run(exercise())


def test_sync_restart_rejects_changed_host_before_binding_new_feedback(
    panel_module, panel, monkeypatch
):
    original_mirror = panel.mirror
    replacement = Mirror(original_mirror.latest[0])
    replacement.host = "192.168.1.7"

    def restart(path, **kwargs):
        async def start():
            setattr(builtins, panel_module.MIRROR_SINGLETON, replacement)

        return {"MG400_MIRROR_START_TASK": asyncio.create_task(start())}

    monkeypatch.setitem(panel.run.__globals__, "runpy", SimpleNamespace(run_path=restart))

    async def exercise():
        panel.request_sync_stop()
        await panel.sync_task
        panel.request_sync_start()
        await panel.sync_task
        assert not panel.sync_enabled
        assert panel.mirror is original_mirror
        assert replacement.stop_event.is_set()
        panel.submit("enable")
        panel.request_disable()
        assert panel.control.calls == []
        assert panel.disable_task is None
        assert "Robot address changed" in panel.message

    asyncio.run(exercise())


@pytest.mark.parametrize("index", (0, 3))
def test_target_edit_turns_keyboard_off_before_text_input(panel_module, panel, monkeypatch, index):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
        elif frames == 2:
            fields = [widget for widget in widgets if type(widget).__name__ == "FloatField"]
            assert all(field.enabled for field in fields)
            assert panel.keyboard_enabled
            fields[index].model.begin_edit()
            assert not panel.keyboard_enabled
            assert not panel.window.exclusive_keyboard
            assert panel.message == "Keyboard off: target editing."
            assert panel.keyboard_disarm_reason == "target editing."
            fields[index].model.set_value(12.345)
            panel.key_event("W", True)
            assert panel.action is None
            assert panel.control.calls == []
        elif frames == 3:
            assert panel.models[index].as_float == 12.345
            assert panel.targets_editable
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.report["keyboard_disarm_reason"] == "target editing."
    assert all(not model.begin_callbacks for model in panel.models)


@pytest.mark.parametrize("state", ("preparing", "jogging"))
def test_target_inputs_stay_disabled_during_keyboard_operations(
    panel_module, panel, monkeypatch, state
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            if state == "preparing":
                panel.request_keyboard()
                panel.keyboard_session.prepare_allowed.clear()
                await panel.keyboard_session.prepare_started.wait()
            else:
                await arm_keyboard(panel)
                panel.key_event("W", True)
                await panel.keyboard_session.run_started.wait()
        elif frames == 2:
            fields = [
                widget
                for widget in widgets
                if type(widget).__name__ in ("FloatSlider", "FloatField")
            ]
            assert not panel.targets_editable
            assert all(not field.enabled for field in fields)
            if state == "preparing":
                panel.keyboard_session.prepare_allowed.set()
                await panel.keyboard_task
            else:
                panel.key_event("W", False)
                await panel.action
        elif frames == 3:
            assert panel.targets_editable
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert panel.control.calls == []


@pytest.mark.parametrize("cause", ("modifier", "not_ready"))
def test_keyboard_disarm_reports_reason_without_overwriting_it_on_idle_frames(panel, cause):
    async def exercise():
        await arm_keyboard(panel)
        if cause == "not_ready":
            panel.keyboard_session.ready = False
        panel.key_event("W", True, modifiers=2 if cause == "modifier" else 0)
        reason = (
            "shortcut modifier is active."
            if cause == "modifier"
            else "session is not ready. Click Keyboard to retry."
        )
        assert panel.keyboard_disarm_reason == reason
        assert panel.message == f"Keyboard off: {reason}"
        panel.set_keyboard(False, reason="window lost focus.")
        assert panel.keyboard_disarm_reason == reason
        assert panel.message == f"Keyboard off: {reason}"
        assert panel.action is None

    asyncio.run(exercise())


@pytest.mark.parametrize("cause", ("focus", "stale"))
def test_passive_keyboard_disarm_explains_focus_and_feedback_loss(
    panel_module, panel, monkeypatch, cause
):
    frames = 0

    async def next_update():
        nonlocal frames
        frames += 1
        if frames == 1:
            await arm_keyboard(panel)
            if cause == "focus":
                panel.window.set_focus(False)
            else:
                sample = panel.mirror.latest[0]
                panel.mirror.latest = sample, time.monotonic() - 1
        elif frames == 2:
            reason = (
                "window lost focus."
                if cause == "focus"
                else "robot feedback is stale, unavailable, or reports a fault."
            )
            assert not panel.keyboard_enabled
            assert panel.keyboard_disarm_reason == reason
            assert panel.message == f"Keyboard off: {reason}"
            assert panel.control.calls == []
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())


@pytest.mark.parametrize("fault", ("stopped", "session"))
def test_disarm_reason_does_not_replace_existing_control_error(panel, fault):
    async def exercise():
        await arm_keyboard(panel)
        panel.message = "ERROR: controller acknowledgement was lost."
        if fault == "stopped":
            panel.control.stopped = True
        else:
            panel.keyboard_session.fault = "controller acknowledgement was lost"
        panel.set_keyboard(False, reason="window lost focus.")
        assert panel.message == "ERROR: controller acknowledgement was lost."
        assert panel.keyboard_disarm_reason == "window lost focus."

    asyncio.run(exercise())
