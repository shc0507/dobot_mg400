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
            instances.append(self)

        def read_state(self):
            return self.snapshot()[0]

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
            self.frame = self
            widgets.append(self)

        def __enter__(self):
            containers.append(self)
            return self

        def __exit__(self, *args):
            containers.pop()

        def setPosition(self, *args):
            pass

        def destroy(self):
            self.destroyed = True

    modules = {
        name: ModuleType(name)
        for name in ("omni", "omni.kit", "omni.kit.app", "omni.timeline", "omni.ui", "omni.usd")
    }
    modules["omni"].kit = modules["omni.kit"]
    modules["omni.kit"].app = modules["omni.kit.app"]
    for name in ("timeline", "ui", "usd"):
        setattr(modules["omni"], name, modules[f"omni.{name}"])
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
            assert panel.control.calls == [("move", (1.0, 2.0, 3.0, 4.0)), ("stop",)]
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
        assert old.calls == [("move", (1.0, 2.0, 3.0, 4.0)), ("disable",)]
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
        assert replacement.calls == [("enable",), ("move", (30.0, 2.0, 3.0, 4.0))]
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
        assert old.calls == [("move", (1.0, 2.0, 3.0, 4.0)), ("disable",)]
        assert old.cancel_modes == [True]
        assert panel.message == message

    asyncio.run(exercise())


def test_home_moves_to_fixed_zero_angles_and_updates_edited_targets(panel):
    async def exercise():
        panel.copy_actual()
        for model in panel.models:
            model.set_value(45.0)
        panel.control.release_move.set()
        panel.submit("home")
        assert [model.as_float for model in panel.models] == [0.0] * 4
        await panel.action

    asyncio.run(exercise())
    assert panel.control.calls == [("move", (0.0, 0.0, 0.0, 0.0))]
    assert panel.message == "Arrival verified."


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
            assert panel.initialized
            assert [model.as_float for model in panel.models] == [1.0, 2.0, 3.0, 4.0]
            assert panel.control.calls == []
            if moving_at_close:
                panel.submit("move")
                assert await asyncio.to_thread(panel.control.move_started.wait, 1)
            panel.window.visible = False

    install_ui(panel_module, panel, monkeypatch, next_update)
    asyncio.run(panel.run())

    assert frames == 2
    assert panel.closed
    assert panel.window.destroyed
    expected_calls = [("move", (1.0, 2.0, 3.0, 4.0)), ("stop",)] if moving_at_close else []
    assert panel.control.calls == expected_calls
    assert panel.report["status"] == "closed"
    assert panel.report["motion_sent_by_startup"] is False


@pytest.mark.parametrize(
    ("mode", "busy", "expected_enabled"),
    (
        (4, False, [True, False, False, False]),
        (5, False, [False, True, True, True]),
        (5, True, [False, True, False, False]),
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
                "Enable robot", "Disable robot", "Return to Home", "Move to Target"
            ]
            enable, disable, home, move = buttons
            assert enable.parent is disable.parent
            assert type(enable.parent).__name__ == "HStack"
            assert home.parent is move.parent is enable.parent.parent
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
