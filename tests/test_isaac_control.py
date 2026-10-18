"""Exercise the real-control panel without Isaac Sim or robot connections."""

import asyncio
import builtins
import runpy
import socket
import sys
import threading
import time
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
            instances.append(self)

        def read_state(self):
            return self.snapshot()[0]

        def enable(self):
            self.calls.append(("enable",))

        def move(self, target):
            self.calls.append(("move", target))
            self.busy = True
            self.move_started.set()
            try:
                assert self.release_move.wait(3), "Test did not release the move worker"
            finally:
                self.busy = False

        def stop(self):
            self.calls.append(("stop",))
            self.stopped = True
            return {"ok": True}

        def cancel(self):
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
            str(ROOT / "examples/isaac_sim_control_mg400.py"), run_name="test_isaac_control"
        )
    panel_class = namespace["ControlPanel"]
    monkeypatch.setitem(panel_class.run.__globals__, "REPORT", tmp_path / "status.json")
    monkeypatch.setattr(builtins, namespace["SINGLETON"] + "_start_lock", None, raising=False)
    return SimpleNamespace(**namespace, instances=instances)


@pytest.fixture
def panel(panel_module):
    sample = Feedback((1.0, 2.0, 3.0, 4.0), mode=5, timestamp=0, error_status=False)
    mirror = SimpleNamespace(
        lock=threading.Lock(),
        stop_event=threading.Event(),
        network_error=None,
        latest=(sample, time.monotonic()),
    )
    panel = panel_module.ControlPanel(mirror)
    panel.models = [FloatModel(0.0) for _ in range(4)]
    return panel


def test_import_does_not_start_control_or_schedule_tasks(panel_module):
    assert panel_module.instances == []
    assert not hasattr(panel_module, "MG400_CONTROL_START_TASK")


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
    assert panel.message == "Enabled; no movement sent."


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
    widgets = []

    class Widget:
        def __init__(self, text=None, *args, **kwargs):
            self.text = text
            self.visible = self.enabled = True
            self.destroyed = False
            self.frame = self
            widgets.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setPosition(self, *args):
            pass

        def destroy(self):
            self.destroyed = True

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

    modules = {name: ModuleType(name) for name in (
        "omni", "omni.kit", "omni.kit.app", "omni.timeline", "omni.ui", "omni.usd"
    )}
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
        setattr(modules["omni.ui"], name, Widget)
    modules["omni.ui"].SimpleFloatModel = FloatModel
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(builtins, panel_module.MIRROR_SINGLETON, panel.mirror, raising=False)
    monkeypatch.setattr(builtins, panel_module.SINGLETON, panel, raising=False)
    panel.models = []

    asyncio.run(panel.run())

    assert frames == 2
    assert panel.closed
    assert panel.window.destroyed
    expected_calls = [("move", (1.0, 2.0, 3.0, 4.0)), ("stop",)] if moving_at_close else []
    assert panel.control.calls == expected_calls
    assert panel.report["status"] == "closed"
    assert panel.report["motion_sent_by_startup"] is False
