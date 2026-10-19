"""Arm and gripper control in the existing Isaac Sim / Olares process.

Run in Script Editor or with Kit --exec. Opening the panel only reads feedback.
Buttons send commands only when clicked. The viewport shows measured angles.
"""

import asyncio
import builtins
import importlib
import json
import runpy
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
importlib.invalidate_caches()
from mg400_agent.joint_control import JointControl  # noqa: E402
from mg400_agent.keyboard_jog import KeyboardJogSession  # noqa: E402
from mg400_agent.robot_config import load_robot_host  # noqa: E402
from mg400_agent.runtime_paths import data_path  # noqa: E402

SINGLETON = "_mg400_isaac_joint_control"
MIRROR_SINGLETON = "_mg400_isaac_feedback_mirror"
REPORT = data_path("isaacsim/control_status.json")
ROBOT_CONFIG = ROOT / "config/robot.toml"
HOME_JOINTS_DEG = (0.0, 0.0, 0.0, 0.0)
GRIPPER_SINGLETON = "_mg400_isaac_real_gripper"
JOG_KEYS = frozenset("WASDQERFZX")
# Shift/Control/Alt/Super only; ignore lock states and omni.ui event flags.
SHORTCUT_MODIFIERS = 0x0F


class ControlPanel:
    def __init__(self, mirror, gripper=None):
        self.host = load_robot_host(ROBOT_CONFIG)
        if getattr(mirror, "host", None) != self.host:
            raise RuntimeError(
                "Robot address does not match the live mirror. "
                "Restart the mirror and control panel."
            )
        self.mirror = mirror
        self.gripper = gripper
        if gripper is not None:
            gripper.owner = self
        self.control = JointControl(self.host, self.feedback_snapshot)
        self.action = self.disable_task = self.stop_task = self.task = None
        self.closed = False
        self.initialized = False
        self.message = "Waiting for fresh robot feedback."
        self.models = []
        self.actual_pose = None
        self.pose_received = 0.0
        self.pose_requested = 0.0
        self.pose_task = None
        self.pose_lock = asyncio.Lock()
        self.keyboard_enabled = False
        self.keyboard_arming = False
        self.keyboard_task = self.keyboard_session = None
        self.keyboard_generation = 0
        self.active_key = self.jog_released = None
        self.held_keys = set()
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
        return any(
            task and not task.done()
            for task in (self.action, self.disable_task, self.stop_task, self.keyboard_task)
        )

    def copy_actual(self):
        try:
            self.control.read_state()
            if self.actual_pose is None or time.monotonic() - self.pose_received > 0.5:
                raise RuntimeError("Waiting for fresh flange coordinates.")
            for model, value in zip(self.models, self.actual_pose, strict=True):
                model.set_value(value)
            self.initialized = True
            self.message = "Edit X/Y/Z in mm and R in degrees, then click Move Linear."
        except Exception as error:
            self.message = str(error)

    async def refresh_pose(self):
        async with self.pose_lock:
            control = self.control
            self.pose_requested = time.monotonic()
            try:
                pose = await asyncio.to_thread(control.read_pose)
                if self.control is control:
                    self.actual_pose, self.pose_received = pose, self.pose_requested
            except Exception:
                self.actual_pose = None

    def release_key(self):
        if self.jog_released is not None:
            self.jog_released.set()
        if self.keyboard_session is not None:
            self.keyboard_session.release()

    def set_keyboard(self, enabled):
        self.keyboard_enabled = bool(enabled) and not self.closed and not self.control.stopped
        self.held_keys.clear()
        self.active_key = None
        if not self.keyboard_enabled:
            self.keyboard_generation += 1
            self.keyboard_arming = False
            self.release_key()

    def request_keyboard(self):
        if self.keyboard_enabled or self.keyboard_arming:
            self.set_keyboard(False)
            return
        if self.closed or self.busy or self.control.stopped or (
            self.gripper is not None and self.gripper.busy
        ):
            return
        if self.keyboard_session is not None:
            self.keyboard_session.close()
        self.keyboard_session = KeyboardJogSession(self.control)
        self.keyboard_generation += 1
        self.keyboard_arming = True
        self.keyboard_task = asyncio.ensure_future(
            self.prepare_keyboard(self.keyboard_session, self.keyboard_generation)
        )

    async def prepare_keyboard(self, session, generation):
        self.message = "Checking keyboard control..."
        try:
            # Finish an existing read before preparing the shared controller session.
            if self.pose_task:
                await self.pose_task
            pose = await asyncio.to_thread(session.prepare)
            if self.closed or generation != self.keyboard_generation:
                session.close()
                return
            self.actual_pose, self.pose_received = pose, time.monotonic()
            self.copy_actual()
            self.set_keyboard(True)
            self.message = "Keyboard ready. Hold one key; release requests a stop."
        except Exception as error:
            if generation == self.keyboard_generation:
                self.set_keyboard(False)
                self.message = f"ERROR: {error}"
        finally:
            if generation == self.keyboard_generation:
                self.keyboard_arming = False

    def key_event(self, key, pressed, *, repeat=False, modifiers=0):
        """Start one hold task. A release cancels preparation and requests a stop."""
        if not pressed:
            self.held_keys.discard(key)
            if key == self.active_key:
                self.release_key()
                self.active_key = None
            return
        if key == "ESCAPE" and self.keyboard_enabled:
            self.request_disable()
            return
        if modifiers & SHORTCUT_MODIFIERS:
            self.set_keyboard(False)
            return
        if key not in JOG_KEYS or not self.keyboard_enabled or self.closed:
            return
        if repeat and key not in self.held_keys:
            return
        already_held = key in self.held_keys
        self.held_keys.add(key)
        if len(self.held_keys) != 1:
            self.release_key()
            return
        if already_held or self.busy or self.control.stopped:
            return
        if self.keyboard_session is None or not self.keyboard_session.ready:
            self.set_keyboard(False)
            return
        self.active_key = key
        self.jog_released = threading.Event()
        self.action = asyncio.ensure_future(self.perform_keyboard(key, self.jog_released))

    async def perform_keyboard(self, key, released):
        session = self.keyboard_session
        self.message = "Jogging... Release the key to stop."
        try:
            if key in "ZX":
                expired = await self.jog_gripper(key, released)
            else:
                if self.pose_task:
                    await self.pose_task
                pose = await asyncio.to_thread(session.run, key, released)
                self.actual_pose, self.pose_received = pose, time.monotonic()
                expired = session.expired
            if not self.control.stopped and self.stop_task is None and not (
                self.disable_task and not self.disable_task.done()
            ):
                self.copy_actual()
                self.message = (
                    "Hold limit reached. Release the key before pressing again."
                    if expired
                    else "Stopped. Hold a key for the next move."
                )
        except Exception as error:
            self.set_keyboard(False)
            if self.stop_task is None and not (
                self.disable_task and not self.disable_task.done()
            ):
                self.message = f"ERROR: {error}"
        print("MG400_KEYBOARD", self.message, flush=True)

    async def jog_gripper(self, key, released):
        gripper = self.gripper
        if gripper is None or gripper.closed:
            raise RuntimeError("Gripper control is unavailable.")
        deadline = time.monotonic() + 5.0
        while not released.is_set() and time.monotonic() < deadline:
            self.control.read_state()
            if gripper.closed or gripper.failed:
                raise RuntimeError("Gripper control is unavailable.")
            if gripper.busy:
                raise RuntimeError("Wait for the current gripper command to finish.")
            position = gripper.measured_position()
            if position is None:
                raise RuntimeError("Waiting for fresh gripper position feedback.")
            target = max(0, min(1000, position + (25 if key == "Z" else -25)))
            if target == position:
                return
            if released.is_set():
                return
            action = gripper.submit_position(target)
            if action is None:
                raise RuntimeError("Gripper command was not accepted.")
            await action
            if gripper.failed:
                raise RuntimeError("Gripper movement was not confirmed.")
        return not released.is_set()

    def submit(self, name):
        # Capture the target once; edits and repeated clicks cannot queue movements.
        if self.closed or self.busy or self.control.stopped or (
            self.gripper is not None and self.gripper.busy
        ):
            return
        if name in ("move", "home") and not self.initialized:
            return
        self.set_keyboard(False)
        target = HOME_JOINTS_DEG if name == "home" else tuple(
            model.as_float for model in self.models
        )
        self.action = asyncio.ensure_future(self.perform(name, target))

    async def perform(self, name, target):
        if self.closed:
            return
        self.message = "Enabling robot..." if name == "enable" else "Moving..."
        try:
            if name == "enable":
                await asyncio.to_thread(self.control.enable)
            elif name == "home":
                await asyncio.to_thread(self.control.move, target)
            else:
                await asyncio.to_thread(self.control.move_linear, target)
            if name != "enable":
                await self.refresh_pose()
                self.copy_actual()
            if not self.control.stopped:
                self.message = (
                    "Enabled. No movement sent." if name == "enable" else "Arrival verified."
                )
        except Exception as error:
            self.set_keyboard(False)
            if self.stop_task is None and not (
                self.disable_task and not self.disable_task.done()
            ):
                self.message = f"ERROR: {error}"
        print("MG400_CONTROL", self.message, flush=True)

    def request_disable(self):
        self.set_keyboard(False)
        if self.closed or self.control.stopped or (
            self.disable_task and not self.disable_task.done()
        ):
            return
        self.control.cancel(disabling=True)
        self.disable_task = asyncio.ensure_future(self.disable())

    async def disable(self):
        self.message = "Disabling robot..."
        error_message = None
        try:
            await asyncio.to_thread(self.control.disable)
        except Exception as error:
            error_message = f"DISABLE UNCONFIRMED: {error}"
        if self.action:
            await self.action
        if error_message:
            self.message = error_message
        else:
            if not self.closed:
                self.control = JointControl(self.host, self.feedback_snapshot)
                self.initialized = False
                self.actual_pose = None
                await self.refresh_pose()
                self.copy_actual()
            self.message = "Disabled. Click Enable robot to enable again."
        print("MG400_CONTROL", self.message, flush=True)

    def request_stop(self):
        self.set_keyboard(False)
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
        import carb.input
        import omni.kit.app
        import omni.timeline
        import omni.ui as ui
        import omni.usd

        app = omni.kit.app.get_app()
        context = omni.usd.get_context()
        stage = context.get_stage()
        timeline = omni.timeline.get_timeline_interface()
        self.window = ui.Window("MG400 Control", width=920, height=570)
        self.window.setPosition(15, 15)
        self.window.set_focused_changed_fn(
            lambda focused: None if focused else self.set_keyboard(False)
        )
        with self.window.frame:
            with ui.HStack(spacing=16):
                with ui.VStack(width=500, spacing=6):
                    ui.Label(f"REAL ROBOT  |  {self.host}", height=22)
                    state_label = ui.Label("CONNECTING", height=24)
                    with ui.HStack(height=24):
                        ui.Label("Axis", width=55)
                        ui.Label("Actual", width=150)
                        ui.Label("Target")
                    actual_labels, fields = [], []
                    for axis in ("X mm", "Y mm", "Z mm", "R deg"):
                        with ui.HStack(height=27):
                            ui.Label(axis, width=55)
                            actual_labels.append(ui.Label("--", width=150))
                            model = ui.SimpleFloatModel(0.0)
                            self.models.append(model)
                            fields.append(ui.FloatField(model, precision=3, enabled=False))
                    with ui.HStack(height=30, spacing=6):
                        enable_button = ui.Button(
                            "Enable robot", clicked_fn=lambda: self.submit("enable")
                        )
                        disable_button = ui.Button("Disable robot", clicked_fn=self.request_disable)
                    home_button = ui.Button(
                        "Return to Home", height=32, clicked_fn=lambda: self.submit("home")
                    )
                    move_button = ui.Button(
                        "Move Linear", height=32, clicked_fn=lambda: self.submit("move")
                    )
                    ui.Label(
                        "Base frame / flange: User=0, Tool=0. Home: all joints 0 deg.", height=22,
                    )
                    keyboard_button = ui.Button(
                        "Keyboard: OFF", height=30,
                        clicked_fn=self.request_keyboard,
                    )
                    ui.Label("W/S reach | A/D base turn | R/F up/down | Q/E flange yaw", height=22)
                    ui.Label("Z/X open/close gripper | Esc disable robot", height=22)
                    ui.Label(
                        "Hold one key to jog; release requests a stop. Max hold: 5 seconds.\n"
                        "Gripper finishes its current small step. "
                        "Leaving this panel turns keys off.",
                        word_wrap=True, height=40,
                    )
                    message_label = ui.Label(self.message, word_wrap=True, height=45)
                if self.gripper is not None:
                    with ui.VStack(spacing=6):
                        self.gripper.build_ui()
        if self.gripper is not None:
            self.gripper.task = asyncio.ensure_future(self.gripper.run(window=self.window))
        keys = {int(getattr(carb.input.KeyboardInput, key)): key for key in (*JOG_KEYS, "ESCAPE")}

        def on_key(key, modifiers, pressed):
            if not self.window.focused or not self.window.visible:
                self.set_keyboard(False)
                return
            if modifiers & SHORTCUT_MODIFIERS:
                self.set_keyboard(False)
                return
            key = keys.get(key)
            if key is not None:
                self.key_event(key, pressed, modifiers=modifiers)

        # Exclusive windows receive keys here before global carb subscriptions.
        self.window.set_key_pressed_fn(on_key)
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
                    or (self.gripper is not None and self.gripper.closed)
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
                except Exception as error:
                    live = False
                    state_label.text = f"DISCONNECTED: {error}"
                gripper_busy = self.gripper is not None and self.gripper.busy
                idle = (
                    live and sample.mode in (4, 5) and not self.busy
                    and not self.control.stopped and not gripper_busy
                )
                now = time.monotonic()
                if (
                    not self.busy and not self.control.stopped
                    and not self.keyboard_arming
                    and (self.pose_task is None or self.pose_task.done())
                ) and now - self.pose_requested > 0.2:
                    self.pose_task = asyncio.ensure_future(self.refresh_pose())
                for i, label in enumerate(actual_labels):
                    label.text = (
                        f"{self.actual_pose[i]:.3f}"
                        if self.actual_pose is not None and now - self.pose_received <= 0.5
                        else "--"
                    )
                if not live or self.control.stopped or not self.window.focused:
                    self.set_keyboard(False)
                if idle and not self.initialized:
                    self.copy_actual()
                enable_button.enabled = idle and sample.mode == 4
                disable_button.enabled = not self.control.stopped and (
                    self.busy or (sample is not None and sample.mode != 4)
                )
                home_button.enabled = idle and sample.mode == 5 and self.initialized
                move_button.enabled = idle and sample.mode == 5 and self.initialized
                keyboard_button.enabled = (
                    live and sample.mode in (5, 7) and not self.control.stopped
                    and (self.keyboard_enabled or not gripper_busy)
                )
                keyboard_button.text = (
                    "Keyboard: CHECKING..." if self.keyboard_arming
                    else f"Keyboard: {'ON' if self.keyboard_enabled else 'OFF'}"
                )
                self.window.exclusive_keyboard = self.keyboard_enabled
                for field in fields:
                    field.enabled = idle and self.initialized and not self.keyboard_enabled
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
                        actual_pose=self.actual_pose,
                        target_pose=[model.as_float for model in self.models],
                        keyboard_enabled=self.keyboard_enabled,
                        keyboard_arming=self.keyboard_arming,
                        busy=self.busy,
                        message=self.message,
                    )
                    self.write_report()
                    last_report = now
        finally:
            self.closed = True
            self.set_keyboard(False)
            if self.gripper is not None:
                self.gripper.closing = True
            if self.keyboard_session is not None:
                self.keyboard_session.close()
            if self.keyboard_task:
                await self.keyboard_task
            # A closing/reloaded panel must not leave its worker commanding the robot.
            if self.disable_task and not self.disable_task.done():
                await self.disable_task
            elif self.busy or self.control.busy:
                self.request_stop()
            if self.stop_task:
                await self.stop_task
            if self.action:
                await self.action
            if self.pose_task:
                await self.pose_task
            if self.gripper is not None and self.gripper.task:
                try:
                    await self.gripper.task
                except Exception as error:
                    self.message = f"Gripper control stopped: {error}"
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
        gripper_lock_name = GRIPPER_SINGLETON + "_start_lock"
        gripper_lock = getattr(builtins, gripper_lock_name, None)
        if gripper_lock is None:
            gripper_lock = asyncio.Lock()
            setattr(builtins, gripper_lock_name, gripper_lock)
        async with gripper_lock:
            return await replace_panel()


async def replace_panel():
    old = getattr(builtins, SINGLETON, None)
    old_gripper = getattr(builtins, GRIPPER_SINGLETON, None)
    if old_gripper is not None and old_gripper.busy:
        raise RuntimeError("The gripper command is in progress. Wait before reloading.")
    if old and (old.busy or old.control.busy):
        raise RuntimeError("Control panel is busy. Stop and verify the robot before reloading.")
    if old_gripper is not None:
        old_gripper.closed = True
    if old:
        old.closed = True
        if old.task:
            await old.task
    if old_gripper is not None:
        if old_gripper.task:
            try:
                await old_gripper.task
            except Exception as error:
                print("MG400_GRIPPER_CLOSED", str(error), flush=True)
    mirror = getattr(builtins, MIRROR_SINGLETON, None)
    if mirror is None or mirror.stop_event.is_set():
        namespace = runpy.run_path(str(ROOT / "apps/isaacsim/mirror_bootstrap.py"))
        await namespace["MG400_MIRROR_BOOTSTRAP_TASK"]
        mirror = getattr(builtins, MIRROR_SINGLETON)
    namespace = runpy.run_path(
        str(ROOT / "apps/isaacsim/gripper_control.py"), run_name="mg400_gripper_module"
    )
    gripper = namespace["GripperPanel"](
        feedback_path=str(data_path("work/gripper_olares_feedback.json"))
    )
    panel = ControlPanel(mirror, gripper)
    setattr(builtins, GRIPPER_SINGLETON, gripper)
    setattr(builtins, SINGLETON, panel)
    panel.task = asyncio.ensure_future(panel.run())
    return panel


if __name__ in ("__main__", "<run_path>"):
    MG400_CONTROL_START_TASK = asyncio.ensure_future(start())
