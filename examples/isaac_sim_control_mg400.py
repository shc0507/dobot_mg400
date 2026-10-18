"""Four-axis control panel in the existing Isaac Sim / Olares process.

Run in Script Editor or with Kit --exec. Opening the panel only reads feedback.
Enable and Move are explicit button actions; the viewport shows measured angles.
"""

import asyncio
import builtins
import importlib
import json
import runpy
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
importlib.invalidate_caches()
from mg400_agent.joint_control import JointControl  # noqa: E402

SINGLETON = "_mg400_isaac_joint_control"
MIRROR_SINGLETON = "_mg400_isaac_feedback_mirror"
REPORT = ROOT / "isaacsim/control_status.json"


class ControlPanel:
    def __init__(self, mirror):
        self.mirror = mirror
        self.control = JointControl("192.168.1.6", self.feedback_snapshot)
        self.action = self.stop_task = self.task = None
        self.closed = False
        self.initialized = False
        self.message = "Waiting for fresh robot feedback."
        self.models = []
        self.report = {"status": "starting", "motion_sent_by_startup": False}

    def feedback_snapshot(self):
        with self.mirror.lock:
            if self.mirror.stop_event.is_set() or self.mirror.network_error:
                raise RuntimeError("Live mirror disconnected or stopped")
            latest = self.mirror.latest
        if latest is None:
            raise RuntimeError("Waiting for robot feedback")
        return latest[0], latest[1]

    @property
    def busy(self):
        return any(task and not task.done() for task in (self.action, self.stop_task))

    def copy_actual(self):
        try:
            sample = self.control.read_state()
            for model, value in zip(self.models, sample.joints_deg, strict=True):
                model.set_value(value)
            self.initialized = True
            self.message = "Target copied from the real robot. Edit angles, then click Move."
        except Exception as error:
            self.message = str(error)

    def submit(self, name):
        # Capture the target once; edits and repeated clicks cannot queue movements.
        if self.closed or self.busy or self.control.stopped:
            return
        if name == "move" and not self.initialized:
            return
        target = tuple(model.as_float for model in self.models)
        self.action = asyncio.ensure_future(self.perform(name, target))

    async def perform(self, name, target):
        if self.closed:
            return
        self.message = "Enabling robot..." if name == "enable" else f"Moving to {target} degrees..."
        try:
            if name == "enable":
                await asyncio.to_thread(self.control.enable)
            else:
                await asyncio.to_thread(self.control.move, target)
            if not self.control.stopped:
                self.message = (
                    "Enabled; no movement sent." if name == "enable" else "Arrival verified."
                )
        except Exception as error:
            if self.stop_task is None:
                self.message = f"ERROR: {error}"
        print("MG400_CONTROL", self.message, flush=True)

    def request_stop(self):
        if self.stop_task is None:
            self.control.cancel()
            self.stop_task = asyncio.ensure_future(self.stop())

    async def stop(self):
        self.message = "Requesting controlled stop..."
        try:
            result = await asyncio.to_thread(self.control.stop)
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        self.message = (
            "STOPPED: stationary feedback verified. Rerun script to reconnect controls."
            if result["ok"]
            else f"STOP UNCONFIRMED: use physical emergency stop. {result.get('error', '')}"
        )
        print("MG400_CONTROL_STOP", json.dumps(result), flush=True)
        return result

    async def run(self):
        import omni.kit.app
        import omni.timeline
        import omni.ui as ui
        import omni.usd

        app = omni.kit.app.get_app()
        context = omni.usd.get_context()
        stage = context.get_stage()
        timeline = omni.timeline.get_timeline_interface()
        self.window = ui.Window("MG400 Joint Control", width=470, height=365)
        self.window.setPosition(15, 15)
        with self.window.frame:
            with ui.VStack(spacing=6):
                ui.Label("REAL ROBOT  |  192.168.1.6", height=22)
                state_label = ui.Label("CONNECTING", height=24)
                with ui.HStack(height=24):
                    ui.Label("Axis", width=55)
                    ui.Label("Actual (deg)", width=150)
                    ui.Label("Target (deg)")
                actual_labels, fields = [], []
                for i in range(4):
                    with ui.HStack(height=27):
                        ui.Label(f"J{i + 1}", width=55)
                        actual_labels.append(ui.Label("--", width=150))
                        model = ui.SimpleFloatModel(0.0)
                        self.models.append(model)
                        fields.append(ui.FloatField(model, precision=3, enabled=False))
                with ui.HStack(height=30, spacing=6):
                    copy_button = ui.Button("Use current angles", clicked_fn=self.copy_actual)
                    enable_button = ui.Button(
                        "Enable robot", clicked_fn=lambda: self.submit("enable")
                    )
                move_button = ui.Button(
                    "Move real robot to target", height=32, clicked_fn=lambda: self.submit("move")
                )
                ui.Button("STOP robot", height=32, clicked_fn=self.request_stop)
                ui.Label("Max change: 5 deg/axis. Speed/acceleration ratio: 5%.", height=22)
                message_label = ui.Label(self.message, word_wrap=True, height=45)
        last_report = 0
        try:
            while not self.closed:
                await app.next_update_async()
                if (
                    not self.window.visible
                    or context.get_stage() != stage
                    or not timeline.is_stopped()
                    or self.mirror.stop_event.is_set()
                    or getattr(builtins, MIRROR_SINGLETON, None) is not self.mirror
                    or getattr(builtins, SINGLETON, None) is not self
                ):
                    self.closed = True
                    break
                sample = None
                try:
                    sample, received = self.feedback_snapshot()
                    age = time.monotonic() - received
                    live = age < 0.25 and not sample.error_status
                    state_label.text = (
                        f"{'LIVE' if live else 'STALE / ALARM'} | mode {sample.mode}"
                        f" | {age * 1000:.0f} ms"
                    )
                    for label, value in zip(actual_labels, sample.joints_deg, strict=True):
                        label.text = f"{value:.3f}"
                except Exception as error:
                    live = False
                    state_label.text = f"DISCONNECTED: {error}"
                idle = live and sample.mode in (4, 5) and not self.busy and not self.control.stopped
                if idle and not self.initialized:
                    self.copy_actual()
                copy_button.enabled = idle
                enable_button.enabled = idle and sample.mode == 4
                move_button.enabled = idle and sample.mode == 5 and self.initialized
                for field in fields:
                    field.enabled = idle and self.initialized
                message_label.text = self.message
                now = time.monotonic()
                if now - last_report >= 0.5:
                    self.report.update(
                        status=(
                            "stopped" if self.control.stopped else "ready" if idle else "waiting"
                        ),
                        live=live,
                        robot_mode=sample.mode if sample else None,
                        actual_deg=sample.joints_deg if sample else None,
                        target_deg=[model.as_float for model in self.models],
                        busy=self.busy,
                        message=self.message,
                    )
                    self.write_report()
                    last_report = now
        finally:
            self.closed = True
            # A closing/reloaded panel must not leave its worker commanding the robot.
            if self.busy or self.control.busy:
                self.request_stop()
            if self.stop_task:
                await self.stop_task
            if self.action:
                await self.action
            self.window.destroy()
            self.report.update(status="closed", message=self.message)
            self.write_report()

    def write_report(self):
        temporary = REPORT.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(REPORT)


async def start():
    # Script Editor and --exec may run this file repeatedly, in separate namespaces.
    lock_name = SINGLETON + "_start_lock"
    lock = getattr(builtins, lock_name, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(builtins, lock_name, lock)
    async with lock:
        return await replace_panel()


async def replace_panel():
    old = getattr(builtins, SINGLETON, None)
    if old:
        if old.busy or old.control.busy:
            raise RuntimeError("Control panel is busy. Stop and verify the robot before reloading.")
        old.closed = True
        if old.task:
            await old.task
    mirror = getattr(builtins, MIRROR_SINGLETON, None)
    if mirror is None or mirror.stop_event.is_set():
        namespace = runpy.run_path(str(ROOT / "examples/isaac_sim_mirror_bootstrap.py"))
        await namespace["MG400_MIRROR_BOOTSTRAP_TASK"]
        mirror = getattr(builtins, MIRROR_SINGLETON)
    panel = ControlPanel(mirror)
    setattr(builtins, SINGLETON, panel)
    panel.task = asyncio.ensure_future(panel.run())
    return panel


if __name__ in ("__main__", "<run_path>"):
    MG400_CONTROL_START_TASK = asyncio.ensure_future(start())
