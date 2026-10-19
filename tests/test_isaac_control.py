"""Exercise the real-control panel without Isaac Sim or robot connections."""

import asyncio
import builtins
import runpy
import socket
import sys
import threading
import time
from dataclasses import replace
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


@pytest.fixture
def panel_module(monkeypatch, tmp_path):
    instances = []

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
    monkeypatch.setitem(sys.modules, backend.__name__, backend)
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
    monkeypatch.setitem(panel_class.run.__globals__, "REPORT", tmp_path / "status.json")
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "192.168.1.6"\n')
    monkeypatch.setitem(panel_class.__init__.__globals__, "ROBOT_CONFIG", config)
    monkeypatch.setattr(builtins, namespace["SINGLETON"] + "_start_lock", None, raising=False)
    return SimpleNamespace(**namespace, instances=instances, config_path=config)


@pytest.fixture
def panel(panel_module):
    sample = Feedback((1.0, 2.0, 3.0, 4.0), mode=5, timestamp=0, error_status=False)
    mirror = SimpleNamespace(
        host="192.168.1.6",
        lock=threading.Lock(),
        stop_event=threading.Event(),
        network_error=None,
        latest=(sample, time.monotonic()),
    )
    panel = panel_module.ControlPanel(mirror)
    panel.models = [FloatModel(0.0) for _ in range(4)]
    panel.actual_pose = panel.control.read_pose()
    panel.pose_received = time.monotonic()
    return panel


def install_ui(panel_module, panel, monkeypatch, next_update):
    widgets = []
    containers = []

    class Widget:
        def __init__(self, text=None, *args, **kwargs):
            self.text = text
            self.visible = True
            self.enabled = kwargs.get("enabled", True)
            self.clicked_fn = kwargs.get("clicked_fn")
            self.parent = containers[-1] if containers else None
            self.destroyed = False
            self.focused = True
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
            self.unsubscribed = []

        def subscribe_to_keyboard_events(self, keyboard, callback):
            assert keyboard is self.keyboard
            self.callback = callback
            return self.subscription

        def unsubscribe_to_keyboard_events(self, keyboard, subscription):
            self.unsubscribed.append((keyboard, subscription))
            self.callback = None

        def emit(self, key, event_type="press", modifiers=0):
            return self.callback(SimpleNamespace(input=key, type=event_type, modifiers=modifiers))

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
    modules["carb.input"].KeyboardInput = SimpleNamespace(
        **{key: key for key in (*panel_module.JOG_KEYS, "ESCAPE")}
    )
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
        return (200.0, 50.0, 150.0, 25.0)

    monkeypatch.setattr(panel.control, "read_pose", slow_pose)
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
            await refresh
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


@pytest.mark.parametrize(
    ("key", "method", "target"),
    (
        ("W", "move_linear", (1.0, 3.0, 3.0, 4.0)),
        ("S", "move_linear", (1.0, 1.0, 3.0, 4.0)),
        ("A", "move", (91.0, 2.0, 3.0, 4.0)),
        ("D", "move", (89.0, 2.0, 3.0, 4.0)),
        ("R", "move_linear", (1.0, 2.0, 4.0, 4.0)),
        ("F", "move_linear", (1.0, 2.0, 2.0, 4.0)),
        ("Q", "move_linear", (1.0, 2.0, 3.0, 5.0)),
        ("E", "move_linear", (1.0, 2.0, 3.0, 3.0)),
    ),
)
def test_keyboard_uses_measured_pose_and_joint_angle(panel, key, method, target):
    sample, received = panel.mirror.latest
    panel.mirror.latest = replace(sample, joints_deg=(90.0, 2.0, 3.0, 4.0)), received
    for model in panel.models:
        model.set_value(99.0)

    async def exercise():
        panel.control.release_move.set()
        panel.set_keyboard(True)
        panel.key_event(key, True)
        await panel.action

    asyncio.run(exercise())
    assert len(panel.control.calls) == 1
    assert panel.control.calls[0][0] == method
    assert panel.control.calls[0][1] == pytest.approx(target)


def test_keyboard_repeats_never_queue_and_release_prevents_later_repeats(panel):
    async def exercise():
        panel.set_keyboard(True)
        panel.key_event("R", True)
        first_action = panel.action
        try:
            assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            for _ in range(5):
                panel.key_event("R", True, repeat=True)
            panel.key_event("W", True)
            assert panel.action is first_action
            panel.key_event("W", False)
            panel.key_event("R", False)
        finally:
            panel.control.release_move.set()
            await first_action
        panel.key_event("R", True, repeat=True)
        assert panel.action is first_action
        assert panel.control.calls == [("move_linear", (1.0, 2.0, 4.0, 4.0))]
        panel.key_event("R", True)
        await panel.action
        panel.key_event("R", True, repeat=True)
        await panel.action
        panel.key_event("R", False)
        assert panel.control.calls == [
            ("move_linear", (1.0, 2.0, 4.0, 4.0)),
            ("move_linear", (1.0, 2.0, 5.0, 4.0)),
            ("move_linear", (1.0, 2.0, 6.0, 4.0)),
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", (4, 7))
def test_keyboard_waits_for_enabled_idle_robot(panel, mode):
    panel.control.set_mode(mode)
    panel.set_keyboard(True)
    panel.key_event("W", True)
    assert panel.action is None
    assert panel.control.calls == []


def test_keyboard_modifier_disarms_and_clears_held_keys(panel):
    panel.set_keyboard(True)
    panel.held_keys.add("W")
    panel.key_event("R", True, modifiers=2)
    assert not panel.keyboard_enabled
    assert panel.held_keys == set()
    assert panel.action is None
    assert panel.control.calls == []


def test_escape_disables_robot_and_clears_keyboard_state(panel):
    async def exercise():
        old = panel.control
        panel.set_keyboard(True)
        panel.held_keys.add("W")
        panel.key_event("ESCAPE", True)
        assert not panel.keyboard_enabled
        assert panel.held_keys == set()
        assert old.cancel_modes == [True]
        await panel.disable_task
        assert old.calls == [("disable",)]
        assert panel.control.read_state().mode == 4

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("key", "position", "target"),
    (("Z", 500, 525), ("X", 500, 475), ("Z", 990, 1000), ("X", 10, 0)),
)
def test_gripper_keyboard_steps_start_from_measured_position(
    panel_module, panel, monkeypatch, key, position, target
):
    reads, targets = [], []

    def measured_position():
        reads.append(position)
        return position

    gripper = SimpleNamespace(
        closed=False, busy=False, measured_position=measured_position,
        submit_position=targets.append,
    )
    monkeypatch.setattr(builtins, panel_module.GRIPPER_SINGLETON, gripper, raising=False)
    panel.set_keyboard(True)
    panel.key_event(key, True)
    assert reads == [position]
    assert targets == [target]
    assert panel.action is None
    assert panel.control.calls == []
    gripper.busy = True
    panel.key_event(key, True, repeat=True)
    assert reads == [position]
    assert targets == [target]


def test_start_refuses_to_replace_busy_panel(panel_module, panel, monkeypatch):
    panel.control.busy = True
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(panel_module.start())
    assert not panel.closed
    assert len(panel_module.instances) == 1


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
    assert input_interface.unsubscribed == [
        (input_interface.keyboard, input_interface.subscription)
    ]


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
                "Enable robot", "Disable robot", "Return to Home", "Move Linear", "Keyboard: OFF"
            ]
            enable, disable, home, move, keyboard = buttons
            assert enable.parent is disable.parent
            assert type(enable.parent).__name__ == "HStack"
            assert home.parent is move.parent is enable.parent.parent
            assert keyboard.parent is home.parent
            assert [button.enabled for button in buttons] == expected_enabled
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


@pytest.mark.parametrize("disarm", ("focus", "hidden", "modifier"))
def test_keyboard_subscription_handles_input_and_disarms_on_context_change(
    panel_module, panel, monkeypatch, disarm
):
    frames = 0
    panel.copy_actual()
    panel.control.release_move.set()

    async def next_update():
        nonlocal frames
        frames += 1
        input_interface = sys.modules["carb.input"].acquire_input_interface()
        if frames == 1:
            assert input_interface.emit("R") is True
            assert panel.action is None
            keyboard_button = next(widget for widget in widgets if widget.text == "Keyboard: OFF")
            keyboard_button.clicked_fn()
            assert panel.keyboard_enabled
        if frames == 2:
            assert panel.window.exclusive_keyboard
            fields = [widget for widget in widgets if type(widget).__name__ == "FloatField"]
            assert all(not field.enabled for field in fields)
            assert input_interface.emit("R") is False
            await panel.action
            completed_action = panel.action
            assert input_interface.emit("R", "release") is False
            assert input_interface.emit("R", "repeat") is False
            assert panel.action is completed_action
            assert panel.control.calls == [("move_linear", (1.0, 2.0, 4.0, 4.0))]
            if disarm == "focus":
                panel.window.set_focus(False)
            elif disarm == "hidden":
                panel.window.visible = False
                assert input_interface.emit("W") is True
            else:
                assert input_interface.emit("LEFT_SHIFT", modifiers=2) is True
            assert not panel.keyboard_enabled
            assert panel.held_keys == set()
            panel.window.visible = False

    widgets = install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())
    assert frames == 2
    assert panel.control.calls == [("move_linear", (1.0, 2.0, 4.0, 4.0))]
    assert panel.window.destroyed
