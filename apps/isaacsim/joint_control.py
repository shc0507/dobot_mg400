"""Arm and gripper control in the existing Isaac Sim / Olares process.

Run in Script Editor or with Kit --exec. Gripper initialization is manual by default.
Arm commands require user input. The viewport shows measured angles.
"""

import asyncio
import builtins
import importlib
import importlib.util
import inspect
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

backend = importlib.import_module("mg400_agent.joint_control")
limits = importlib.import_module("mg400_agent.joint_limits")

SINGLETON = "_mg400_isaac_joint_control"
MIRROR_SINGLETON = "_mg400_isaac_feedback_mirror"
REPORT = data_path("isaacsim/control_status.json")
ROBOT_CONFIG = ROOT / "config/robot.toml"
HOME_JOINTS_DEG = (0.0, 0.0, 0.0, 0.0)
GRIPPER_SINGLETON = "_mg400_isaac_real_gripper"
AUTO_INITIALIZE_GRIPPER = globals().get("AUTO_INITIALIZE_GRIPPER", False)
JOG_KEYS = frozenset("WASDQERFZX")
# Shift/Control/Alt/Super only; ignore lock states and omni.ui event flags.
SHORTCUT_MODIFIERS = 0x0F


class GripperUnavailableError(RuntimeError):
    """The gripper cannot start this step. No command was sent."""


class ControlPanel:
    def __init__(self, mirror, gripper=None):
        self.host = load_robot_host(ROBOT_CONFIG)
        if getattr(mirror, "host", None) != self.host:
            raise RuntimeError(
                "Robot address does not match the live mirror. "
                "Restart the mirror and control panel."
            )
        self.mirror = mirror
        if not hasattr(mirror, "ownership"):
            raise RuntimeError("Restart synchronization to load robot connection ownership.")
        mirror.ownership.acquire()
        mirror.control_owner = self
        self.gripper = gripper
        if gripper is not None:
            gripper.owner = self
        self.control = JointControl(self.host, self.feedback_snapshot)
        self.action = self.disable_task = self.stop_task = self.task = None
        self.closed = False
        self.sync_enabled = True
        self.sync_task = None
        self.keep_mirror = False
        self.initialized = False
        self.message = "Waiting for fresh robot feedback."
        self.models = []
        self.sliders = []
        self.edit_callbacks = []
        self.targets_editable = False
        self.actual_pose = None
        self.pose_received = 0.0
        self.pose_requested = 0.0
        self.pose_task = None
        self.pose_measurement = None
        self.pose_lock = asyncio.Lock()
        self.keyboard_enabled = False
        self.keyboard_arming = False
        self.keyboard_disarm_reason = None
        self.keyboard_task = self.keyboard_session = None
        self.stream_observer = None
        self.keyboard_generation = 0
        self.active_key = self.jog_released = None
        self.held_keys = set()
        self.report = {
            "status": "starting",
            "motion_sent_by_startup": False,
            "gripper_auto_initialize_on_open": AUTO_INITIALIZE_GRIPPER,
        }

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
            for index, slider in enumerate(self.sliders):
                span = 10 if index == 3 else 50
                slider.min = self.actual_pose[index] - span
                slider.max = self.actual_pose[index] + span
            for model, value in zip(self.models, self.actual_pose, strict=True):
                model.set_value(value)
            self.initialized = True
            self.message = "Edit the targets, then click Execute targets."
        except Exception as error:
            self.message = str(error)

    async def refresh_pose(self):
        async with self.pose_lock:
            if self.closed or not self.sync_enabled or self.control.stopped:
                return
            control = self.control
            self.pose_requested = time.monotonic()

            def read():
                if self.closed or not self.sync_enabled or control.stopped:
                    return None
                return control.read_pose_measurement()

            try:
                measurement = await asyncio.to_thread(read)
                if measurement is not None and self.control is control:
                    self.pose_measurement = measurement
                    self.actual_pose = measurement.pose
                    self.pose_received = measurement.requested_at
                    return measurement
            except Exception:
                self.pose_measurement = None
                self.actual_pose = None

    def release_key(self):
        if self.jog_released is not None:
            self.jog_released.set()
        if self.keyboard_session is not None:
            self.keyboard_session.release()

    def set_keyboard(self, enabled, *, reason=None):
        was_active = self.keyboard_enabled or self.keyboard_arming
        self.keyboard_enabled = bool(enabled) and not self.closed and not self.control.stopped
        self.held_keys.clear()
        self.active_key = None
        if not self.keyboard_enabled:
            self.keyboard_generation += 1
            self.keyboard_arming = False
            self.release_key()
            if was_active and reason is not None:
                self.keyboard_disarm_reason = reason
                fault = self.keyboard_session and self.keyboard_session.fault
                if not self.control.stopped and not fault:
                    self.message = f"Keyboard off: {reason}"
        else:
            self.keyboard_disarm_reason = None

    def request_keyboard(self):
        if self.keyboard_enabled or self.keyboard_arming:
            self.set_keyboard(False)
            return
        if (
            self.closed
            or not self.sync_enabled
            or self.busy
            or self.control.stopped
            or (self.gripper is not None and self.gripper.busy)
        ):
            return
        try:
            limits.validate_measured_joints(self.control.read_state().joints_deg)
        except Exception as error:
            self.message = f"Keyboard unavailable: {error}"
            return
        if self.keyboard_session is not None:
            self.keyboard_session.close()
        self.keyboard_session = KeyboardJogSession(self.control)
        self.keyboard_generation += 1
        self.keyboard_arming = True
        self.keyboard_task = asyncio.ensure_future(
            self.prepare_keyboard(self.keyboard_session, self.keyboard_generation)
        )

    def begin_target_edit(self, _model=None):
        if self.keyboard_enabled or self.keyboard_arming:
            self.set_keyboard(False, reason="target editing.")
            self.window.exclusive_keyboard = False

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
        except backend.PreparationRejectedError as error:
            if generation == self.keyboard_generation:
                self.set_keyboard(False)
                if (
                    not self.closed
                    and not self.control.stopped
                    and self.stop_task is None
                    and not (self.disable_task and not self.disable_task.done())
                ):
                    self.message = (
                        f"Keyboard preparation deferred: {error} Click Keyboard to retry."
                    )
        except Exception as error:
            current_fault = (
                session is self.keyboard_session
                and session.control is self.control
                and session.fault is not None
            )
            if generation == self.keyboard_generation or current_fault:
                self.set_keyboard(False)
                if (
                    not self.closed
                    and self.stop_task is None
                    and not (self.disable_task and not self.disable_task.done())
                ):
                    self.message = f"ERROR: {session.fault or error}"
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
            self.set_keyboard(False, reason="shortcut modifier is active.")
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
            self.set_keyboard(False, reason="session is not ready. Click Keyboard to retry.")
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
                initial_pose = None
                if key not in "AD":
                    if session.reusable_pose(self.pose_measurement):
                        initial_pose = self.pose_measurement
                    elif self.pose_task:
                        initial_pose = await self.pose_task
                pose = await asyncio.to_thread(
                    session.run, key, released, initial_pose=initial_pose
                )
                self.actual_pose, self.pose_received = pose, time.monotonic()
                expired = False
            if (
                self.keyboard_enabled
                and not self.control.stopped
                and self.stop_task is None
                and not (self.disable_task and not self.disable_task.done())
            ):
                self.copy_actual()
                if key not in "ZX" and session.limit_reason:
                    self.message = (
                        f"{session.limit_reason} Release the key and choose another direction."
                    )
                elif key in "ZX" and self.gripper.report.get("contact_observed"):
                    self.message = (
                        f"{self.gripper.report['result']} Release the key before pressing again."
                    )
                else:
                    self.message = (
                        "Hold limit reached. Release the key before pressing again."
                        if expired
                        else "Stopped. Hold a key for the next move."
                    )
        except GripperUnavailableError as error:
            if (
                not self.closed
                and not self.control.stopped
                and self.stop_task is None
                and not (self.disable_task and not self.disable_task.done())
            ):
                self.message = f"Gripper unavailable: {error}"
                if self.keyboard_enabled:
                    self.message += " Arm keys remain ready."
        except Exception as error:
            self.set_keyboard(False)
            if self.stop_task is None and not (self.disable_task and not self.disable_task.done()):
                self.message = f"ERROR: {error}"
        print("MG400_KEYBOARD", self.message, flush=True)

    async def jog_gripper(self, key, released):
        gripper = self.gripper
        if gripper is None or gripper.closed:
            raise GripperUnavailableError("Gripper control is unavailable.")
        deadline = time.monotonic() + 5.0
        while not released.is_set() and time.monotonic() < deadline:
            self.control.read_state()
            if gripper.closed:
                raise GripperUnavailableError("Gripper control is unavailable.")
            if gripper.failed:
                raise RuntimeError(gripper.report["result"])
            if gripper.busy:
                raise RuntimeError("Wait for the current gripper command to finish.")
            position = gripper.measured_position()
            if position is None:
                raise GripperUnavailableError(
                    gripper.feedback.diagnostic() or "Waiting for fresh gripper position feedback."
                )
            target = max(0, min(1000, position + (25 if key == "Z" else -25)))
            if target == position:
                return
            if released.is_set():
                return
            action = gripper.submit_position(target)
            if action is None:
                if (
                    not gripper.failed
                    and not gripper.busy
                    and gripper.report.get("command_published") is False
                ):
                    raise GripperUnavailableError(gripper.report["result"])
                raise RuntimeError(gripper.report["result"])
            completed = await action
            if gripper.failed:
                raise RuntimeError(gripper.report["result"])
            if completed is False:
                raise GripperUnavailableError(gripper.report["result"])
            if gripper.report.get("contact_observed"):
                return
        return not released.is_set()

    def submit(self, name):
        # Capture the target once; edits and repeated clicks cannot queue movements.
        if (
            self.closed
            or not self.sync_enabled
            or self.busy
            or self.control.stopped
            or (self.gripper is not None and self.gripper.busy)
        ):
            return
        if name in ("move", "home", "execute") and not self.initialized:
            return
        self.set_keyboard(False)
        target = (
            HOME_JOINTS_DEG if name == "home" else tuple(model.as_float for model in self.models)
        )
        gripper_target = None
        if name == "execute" and self.gripper is not None:
            gripper_target = self.gripper.pending_target()
            if gripper_target is not None and self.gripper.position_sample() is None:
                self.message = "No command sent. Wait for fresh gripper position feedback."
                return
        self.action = asyncio.ensure_future(self.perform(name, target, gripper_target))

    async def perform(self, name, target, gripper_target=None):
        if self.closed:
            return
        self.message = "Enabling robot..." if name == "enable" else "Moving..."
        arm_arrived = False
        try:
            if gripper_target is not None and (
                self.gripper.closed
                or self.gripper.closing
                or self.gripper.failed
                or self.gripper.position_sample() is None
                or not 0 <= gripper_target <= 1000
            ):
                raise backend.PreparationRejectedError("Gripper position control is unavailable.")
            if name == "enable":
                await asyncio.to_thread(self.control.enable)
            elif name == "home":
                await asyncio.to_thread(self.control.move, target)
            else:
                await asyncio.to_thread(self.control.move_linear, target)
            arm_arrived = name != "enable"
            if gripper_target is not None:
                if self.closed or not self.sync_enabled or self.control.stopped:
                    return
                self.message = "Arm arrival verified. Moving gripper..."
                action = self.gripper.submit_position(gripper_target)
                if action is None:
                    raise RuntimeError(self.gripper.report["result"])
                completed = await action
                if completed is False or self.gripper.failed:
                    raise RuntimeError(self.gripper.report["result"])
                self.gripper.target_baseline = gripper_target
            if name != "enable":
                await self.refresh_pose()
                self.copy_actual()
            if not self.control.stopped:
                self.message = (
                    "Enabled. No movement sent."
                    if name == "enable"
                    else f"Arm arrival verified. {self.gripper.report['result']}"
                    if gripper_target is not None
                    else "Arrival verified."
                )
        except backend.PreparationRejectedError as error:
            if (
                not self.control.stopped
                and self.stop_task is None
                and not (self.disable_task and not self.disable_task.done())
            ):
                self.message = f"No command sent. {error} Retry when feedback is ready."
        except backend.TargetRejectedError as error:
            if (
                not self.control.stopped
                and self.stop_task is None
                and not (self.disable_task and not self.disable_task.done())
            ):
                self.message = f"{error} Adjust X/Y/Z/R and try again."
        except Exception as error:
            self.set_keyboard(False)
            if self.stop_task is None and not (self.disable_task and not self.disable_task.done()):
                prefix = "Arm arrival verified. Gripper incomplete" if arm_arrived else "ERROR"
                self.message = f"{prefix}: {error}"
        print("MG400_CONTROL", self.message, flush=True)

    def request_disable(self):
        self.set_keyboard(False)
        if not self.sync_enabled and self.mirror.stop_event.is_set():
            return
        if (
            self.closed
            or self.control.stopped
            or (self.disable_task and not self.disable_task.done())
        ):
            return
        try:
            self.mirror.ownership.acquire()
        except RuntimeError as error:
            self.message = str(error)
            return
        self.mirror.control_owner = self
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
                self.pose_measurement = None
                await self.refresh_pose()
                self.copy_actual()
            self.message = "Disabled. Click Enable robot to enable again."
        print("MG400_CONTROL", self.message, flush=True)

    def request_stop(self):
        self.set_keyboard(False)
        if self.stop_task is None:
            try:
                self.mirror.ownership.acquire()
            except RuntimeError as error:
                self.message = str(error)
                return
            self.mirror.control_owner = self
            self.control.cancel()
            self.stop_task = asyncio.ensure_future(self.stop())

    def request_close(self):
        self.set_keyboard(False)
        self.closed = True

    def request_sync_stop(self):
        if self.closed or not self.sync_enabled or (self.sync_task and not self.sync_task.done()):
            return
        self.sync_enabled = False
        self.set_keyboard(False)
        self.sync_task = asyncio.ensure_future(self.stop_synchronization())

    async def stop_synchronization(self):
        if self.keyboard_session is not None:
            self.keyboard_session.close()
        if self.keyboard_task:
            await self.keyboard_task
        if self.busy or self.control.busy:
            self.request_stop()
        if self.stop_task:
            await self.stop_task
        if self.action:
            await self.action
        if self.disable_task:
            await self.disable_task
        if self.gripper is not None and self.gripper.action:
            await self.gripper.action
        if self.pose_task:
            await self.pose_task
        await self.close_synchronization()
        self.initialized = False
        self.actual_pose = None
        if self.mirror.stop_event.is_set():
            self.message = "Synchronization stopped. Click Start sync to reconnect."

    def request_sync_start(self):
        if self.closed or self.sync_enabled or (self.sync_task and not self.sync_task.done()):
            return
        if not self.mirror.stop_event.is_set():
            self.message = "Verify that the robot stopped, then reload the control panel."
            return
        self.sync_task = asyncio.ensure_future(self.start_synchronization())

    async def start_synchronization(self):
        self.message = "Starting synchronization..."
        try:
            namespace = runpy.run_path(
                str(ROOT / "apps/isaacsim/mirror.py"),
                init_globals={"SHOW_WINDOW": False},
            )
            await namespace["MG400_MIRROR_START_TASK"]
            mirror = getattr(builtins, MIRROR_SINGLETON)
            if mirror.host != self.host:
                mirror.stop("Robot address does not match the control panel")
                if mirror.task:
                    await mirror.task
                raise RuntimeError("Robot address changed. Reload the control panel.")
            self.mirror = mirror
            if self.mirror.stop_event.is_set():
                raise RuntimeError("The feedback mirror could not start.")
            mirror.ownership.acquire()
            mirror.control_owner = self
            self.control = JointControl(self.host, self.feedback_snapshot)
            self.action = self.disable_task = self.stop_task = None
            self.pose_measurement = None
            self.sync_enabled = not self.closed
            if self.gripper is not None:
                self.gripper.target_initialized = False
            self.message = "Waiting for fresh robot feedback."
        except Exception as error:
            self.message = f"Synchronization failed: {error}"

    async def close_synchronization(self):
        if self.keep_mirror:
            return
        verified = None
        while not self.mirror.stop_event.is_set():
            if self.keep_mirror:
                return
            pending_task = next(
                (
                    task
                    for task in (self.disable_task, self.stop_task)
                    if task is not None and not task.done()
                ),
                None,
            )
            if pending_task is not None:
                await pending_task
                continue
            control = self.control
            generation = (control, self.disable_task, self.stop_task)
            retention_reason = control.feedback_retention_reason()
            if retention_reason is None and self.keyboard_session is not None:
                session_control = self.keyboard_session.control
                if session_control is not control:
                    retention_reason = session_control.feedback_retention_reason()
            if self.stop_task is not None:
                stop_result = self.stop_task.result()
                if not stop_result["ok"]:
                    retention_reason = stop_result.get("error") or "Controlled stop failed"
            if retention_reason is None and verified != generation:
                try:
                    await asyncio.to_thread(control.verify_stationary)
                except Exception as error:
                    if self.keep_mirror:
                        return
                    if self.mirror.stop_event.is_set():
                        break
                    if generation != (self.control, self.disable_task, self.stop_task):
                        continue
                    self.message += f" Stationary state is not verified: {error}."
                    retention_reason = str(error)
                else:
                    # Disable can start during verification. Drain it and check again.
                    verified = generation
                    continue
            if retention_reason is not None:
                self.message += " Control stop unconfirmed. Synchronization remains active."
                self.report["synchronization_retained"] = True
                return
            self.report.pop("synchronization_retained", None)
            self.mirror.stop("Control panel closed")
        if self.mirror.task:
            await self.mirror.task
        if self.mirror.window:
            self.mirror.window.destroy()
        if verified == (self.control, self.disable_task, self.stop_task):
            self.mirror.control_owner = None
            self.mirror.ownership.release()
        elif self.mirror.ownership.held:
            self.report["synchronization_retained"] = True
            self.message += " Control stop unconfirmed. Connection ownership remains held."

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
        import carb.eventdispatcher
        import carb.input
        import omni.kit.app
        import omni.timeline
        import omni.ui as ui
        import omni.usd

        app = omni.kit.app.get_app()
        context = omni.usd.get_context()
        stage = context.get_stage()
        timeline = omni.timeline.get_timeline_interface()
        self.window = ui.Window(
            "MG400 Control",
            width=560,
            height=560 if self.gripper is not None else 450,
        )
        self.window.setPosition(15, 15)
        self.window.set_focused_changed_fn(
            lambda focused: (
                None if focused else self.set_keyboard(False, reason="window lost focus.")
            )
        )
        with self.window.frame:
            with ui.VStack(spacing=6):
                with ui.HStack(height=26, spacing=6):
                    sync_label = ui.Label(
                        "Sync: connecting",
                        tooltip=(
                            "Synchronize measured robot and gripper positions with the scene. "
                            "Stop sync disables controls. "
                            "It stops robot feedback after a verified stop."
                        ),
                    )
                    start_sync = ui.Button(
                        "Start sync",
                        width=94,
                        clicked_fn=self.request_sync_start,
                    )
                    stop_sync = ui.Button(
                        "Stop sync",
                        width=94,
                        clicked_fn=self.request_sync_stop,
                    )
                ui.Label(
                    "Coordinates & Motion",
                    height=24,
                    tooltip=(
                        "Flange coordinates relative to the robot base. User=0, Tool=0. "
                        "X/Y/Z: mm. R: flange yaw in degrees. Home: all joints at 0 degrees."
                    ),
                )
                with ui.HStack(height=20, spacing=6):
                    ui.Label("Axis", width=64)
                    ui.Label("Actual", width=84)
                    ui.Label("Target")
                actual_labels, fields = [], []
                for index, axis in enumerate(("X mm", "Y mm", "Z mm", "R deg")):
                    with ui.HStack(height=28, spacing=6):
                        ui.Label(axis, width=64)
                        actual_labels.append(ui.Label("--", width=84))
                        model = ui.SimpleFloatModel(0.0)
                        self.models.append(model)
                        self.edit_callbacks.append(
                            (
                                model,
                                model.add_begin_edit_fn(self.begin_target_edit),
                            )
                        )
                        span = 10 if index == 3 else 50
                        slider = ui.FloatSlider(
                            model,
                            min=-span,
                            max=span,
                            step=0.1,
                            precision=3,
                            enabled=False,
                            tooltip=(
                                "Edit the target only. The slider covers a local adjustment range. "
                                "Use the number field for other values. "
                                "Click Execute targets to validate and send the target."
                            ),
                        )
                        self.sliders.append(slider)
                        fields.extend(
                            (
                                slider,
                                ui.FloatField(
                                    model,
                                    width=86,
                                    precision=3,
                                    enabled=False,
                                    tooltip=(
                                        "Double-click to type a target. "
                                        "Editing turns keyboard control off."
                                    ),
                                ),
                            )
                        )
                if self.gripper is not None:
                    self.gripper.build_compact_row()
                with ui.HStack(height=30, spacing=6):
                    home_button = ui.Button(
                        "Home",
                        width=110,
                        clicked_fn=lambda: self.submit("home"),
                    )
                    move_button = ui.Button(
                        "Execute targets",
                        clicked_fn=lambda: self.submit("execute"),
                        tooltip=(
                            "Move the arm first. After verified arrival, "
                            "apply any edited gripper target."
                        ),
                    )
                with ui.HStack(height=24, spacing=10):
                    keyboard_button = ui.Button(
                        "Keyboard: OFF",
                        width=122,
                        clicked_fn=self.request_keyboard,
                        tooltip=(
                            "Hold one key; release to request a stop. W/S: reach. A/D: base turn. "
                            "R/F: up/down. Q/E: flange yaw. Z/X: gripper open/close. "
                            "Esc: disable robot. Gripper hold: up to 5 seconds. "
                            "Gripper completes its current small step. "
                            "Leaving the panel or disconnecting the stream turns keys off."
                        ),
                    )
                    sync_angles = ui.Label("J1-J4: --")
                message_label = ui.Label(self.message, word_wrap=True, height=40)
                with ui.HStack(height=28, spacing=6):
                    state_label = ui.Label("CONNECTING", tooltip=f"Robot: {self.host}")
                    enable_button = ui.Button(
                        "Enable robot",
                        width=110,
                        clicked_fn=lambda: self.submit("enable"),
                    )
                    disable_button = ui.Button(
                        "Disable robot",
                        width=110,
                        clicked_fn=self.request_disable,
                        tooltip="Disable the robot. Keyboard shortcut: Esc.",
                    )
        if self.gripper is not None:
            self.gripper.task = asyncio.ensure_future(self.gripper.run(window=self.window))
        keys = {int(getattr(carb.input.KeyboardInput, key)): key for key in (*JOG_KEYS, "ESCAPE")}

        def on_key(key, modifiers, pressed):
            if not self.window.focused or not self.window.visible:
                self.set_keyboard(False, reason="window lost focus.")
                return
            if modifiers & SHORTCUT_MODIFIERS:
                self.set_keyboard(False, reason="shortcut modifier is active.")
                return
            key = keys.get(key)
            if key is not None:
                self.key_event(key, pressed, modifiers=modifiers)

        # Exclusive windows receive keys here before global carb subscriptions.
        self.window.set_key_pressed_fn(on_key)
        loop = asyncio.get_running_loop()

        def disarm_after_disconnect():
            self.set_keyboard(False, reason="stream disconnected.")
            if (
                not self.closed
                and not self.control.stopped
                and self.stop_task is None
                and not (self.disable_task and not self.disable_task.done())
            ):
                self.message = "Stream disconnected. Reconnect, then click Keyboard to enable keys."

        def on_disconnect(event):
            # The dispatcher can call this from a worker thread. Revoke input first.
            self.release_key()
            if self.keyboard_session is not None:
                self.keyboard_session.close()
            if not self.closed:
                try:
                    loop.call_soon_threadsafe(disarm_after_disconnect)
                except RuntimeError:
                    # Input is already revoked if the event loop closed during dispatch.
                    pass

        last_report = 0
        try:
            self.stream_observer = carb.eventdispatcher.get_eventdispatcher().observe_event(
                event_name="omni.kit.livestream.client_disconnected:immediate",
                on_event=on_disconnect,
                observer_name="MG400 control stream disconnect",
            )
            while not self.closed:
                await app.next_update_async()
                if (
                    not self.window.visible
                    or context.get_stage() != stage
                    or not timeline.is_stopped()
                    or (self.sync_enabled and self.mirror.stop_event.is_set())
                    or (
                        self.sync_enabled
                        and getattr(builtins, MIRROR_SINGLETON, None) is not self.mirror
                    )
                    or getattr(builtins, SINGLETON, None) is not self
                    or (self.gripper is not None and self.gripper.closed)
                ):
                    self.closed = True
                    break
                sample = None
                age = None
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
                sync_report = self.mirror.report
                changing_sync = self.sync_task is not None and not self.sync_task.done()
                sync_label.text = (
                    "Sync: changing..."
                    if changing_sync
                    else f"Sync: {sync_report.get('status', 'starting')}"
                    if self.sync_enabled
                    else "Sync: stopped"
                    if self.mirror.stop_event.is_set()
                    else "Sync: stop unconfirmed"
                )
                start_sync.enabled = (
                    not changing_sync and not self.sync_enabled and self.mirror.stop_event.is_set()
                )
                stop_sync.enabled = not changing_sync and self.sync_enabled
                if sample is not None:
                    sync_angles.text = "  ".join(
                        f"J{i + 1} {value:.2f}°" for i, value in enumerate(sample.joints_deg)
                    )
                gripper_busy = self.gripper is not None and self.gripper.busy
                idle = (
                    self.sync_enabled
                    and live
                    and sample.mode in (4, 5)
                    and not self.busy
                    and not self.control.stopped
                    and not gripper_busy
                )
                now = time.monotonic()
                if (
                    self.sync_enabled
                    and not self.busy
                    and not self.control.stopped
                    and not self.keyboard_arming
                    and (self.pose_task is None or self.pose_task.done())
                ) and now - self.pose_requested > 0.2:
                    self.pose_task = asyncio.ensure_future(self.refresh_pose())
                if self.busy and self.keyboard_session is not None:
                    measured = self.keyboard_session.measured_pose
                    if measured is not None and measured[1] > self.pose_received:
                        self.actual_pose, self.pose_received = measured
                for i, label in enumerate(actual_labels):
                    label.text = (
                        f"{self.actual_pose[i]:.3f}"
                        if self.actual_pose is not None and now - self.pose_received <= 0.5
                        else "--"
                    )
                if (
                    not self.sync_enabled
                    or not live
                    or self.control.stopped
                    or not self.window.focused
                ):
                    reason = (
                        "synchronization stopped."
                        if not self.sync_enabled
                        else "robot feedback is stale, unavailable, or reports a fault."
                        if not live
                        else "robot control is stopped."
                        if self.control.stopped
                        else "window lost focus."
                    )
                    self.set_keyboard(False, reason=reason)
                if idle and not self.initialized:
                    self.copy_actual()
                normal_range = False
                if live:
                    try:
                        limits.validate_measured_joints(sample.joints_deg)
                        normal_range = True
                    except ValueError:
                        pass
                if not normal_range:
                    self.set_keyboard(False, reason="joint feedback is outside the allowed range.")
                enable_button.enabled = idle and sample.mode == 4
                disable_button.enabled = not self.control.stopped and (
                    self.busy or (sample is not None and sample.mode != 4)
                )
                home_button.enabled = (
                    idle and sample.mode == 5 and self.initialized and normal_range
                )
                move_button.enabled = (
                    idle and sample.mode == 5 and self.initialized and normal_range
                )
                keyboard_button.enabled = (
                    self.sync_enabled
                    and live
                    and sample.mode in (5, 7)
                    and not self.control.stopped
                    and (self.keyboard_enabled or not gripper_busy)
                    and normal_range
                )
                keyboard_button.text = (
                    "Keyboard: CHECKING..."
                    if self.keyboard_arming
                    else f"Keyboard: {'ON' if self.keyboard_enabled else 'OFF'}"
                )
                self.window.exclusive_keyboard = self.keyboard_enabled
                self.targets_editable = idle and self.initialized and not self.keyboard_arming
                for field in fields:
                    field.enabled = self.targets_editable
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
                        keyboard_disarm_reason=self.keyboard_disarm_reason,
                        busy=self.busy,
                        message=self.message,
                        synchronization={
                            key: sync_report.get(key)
                            for key in ("status", "display_hz", "feedback_age_ms")
                        },
                    )
                    self.write_report()
                    last_report = now
        finally:
            self.closed = True
            self.targets_editable = False
            self.set_keyboard(False)
            if self.stream_observer is not None:
                self.stream_observer.reset()
                self.stream_observer = None
            if self.gripper is not None:
                self.gripper.closing = True
            if self.keyboard_session is not None:
                self.keyboard_session.close()
            if self.keyboard_task:
                await self.keyboard_task
            # A closing/reloaded panel must not leave its worker commanding the robot.
            disabling = self.disable_task and not self.disable_task.done()
            if self.disable_task:
                await self.disable_task
            if not disabling and (self.busy or self.control.busy):
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
            if self.sync_task:
                await self.sync_task
            await self.close_synchronization()
            for model, callback in self.edit_callbacks:
                model.remove_begin_edit_fn(callback)
            self.edit_callbacks.clear()
            self.window.destroy()
            self.report.update(
                status="closed",
                message=self.message,
                keyboard_enabled=self.keyboard_enabled,
                keyboard_arming=self.keyboard_arming,
                keyboard_disarm_reason=self.keyboard_disarm_reason,
                busy=self.busy,
                synchronization={
                    key: self.mirror.report.get(key)
                    for key in ("status", "display_hz", "feedback_age_ms")
                },
            )
            self.write_report()

    def write_report(self):
        temporary = REPORT.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(REPORT)


def _reload_control_modules():
    """Load control changes only after the previous panel has finished its workers."""
    global backend, limits, JointControl, KeyboardJogSession
    modules = []
    for name in (
        "controller_protocol",
        "joint_limits",
        "joint_control",
        "keyboard_jog",
        "gripper_feedback",
    ):
        path = ROOT / "src" / "mg400_agent" / f"{name}.py"
        fullname = f"mg400_agent.{name}"
        spec = importlib.util.spec_from_file_location(fullname, path)
        module = sys.modules.get(fullname)
        if module is None:
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
        module.__file__ = str(path)
        module.__spec__ = spec
        module.__loader__ = spec.loader
        module.__package__ = "mg400_agent"
        setattr(sys.modules["mg400_agent"], name, module)
        # Read source directly. Kit loaders and old package paths can retain old code.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        modules.append(module)
    protocol_module, limits, control_module, jog_module, _ = modules
    control_module.ControllerCommandError = protocol_module.ControllerCommandError
    control_module.parse_controller_response = protocol_module.parse_controller_response
    jog_module.TargetRejectedError = control_module.TargetRejectedError
    inspect.signature(control_module.JointControl).bind(
        "unused",
        lambda: None,
        parent=None,
        command_timeout=0.5,
        send_guard=None,
    )
    inspect.signature(control_module.JointControl.begin_operation).bind(
        None,
        command_timeout=0.5,
        send_guard=None,
    )
    if not callable(getattr(control_module.JointControl, "solve_pose", None)):
        raise RuntimeError("The control module does not provide Cartesian target validation.")
    if control_module.JointControl.begin_operation.__globals__["JointControl"] is not (
        control_module.JointControl
    ):
        raise RuntimeError("The operation factory uses a different control class.")
    backend = control_module
    JointControl = control_module.JointControl
    KeyboardJogSession = jog_module.KeyboardJogSession


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
            error_window_name = SINGLETON + "_error_window"
            error_window = getattr(builtins, error_window_name, None)
            if error_window is not None:
                error_window.destroy()
                setattr(builtins, error_window_name, None)
            try:
                return await replace_panel()
            except RuntimeError as error:
                panel = getattr(builtins, SINGLETON, None)
                if panel is not None and not panel.closed:
                    panel.message = str(error)
                else:
                    import omni.ui as ui

                    error_window = ui.Window("MG400 Control unavailable", width=480, height=110)
                    setattr(builtins, error_window_name, error_window)
                    with error_window.frame:
                        ui.Label(str(error), word_wrap=True)
                raise


async def replace_panel():
    old = getattr(builtins, SINGLETON, None)
    old_gripper = getattr(builtins, GRIPPER_SINGLETON, None)
    if old_gripper is not None and old_gripper.busy:
        raise RuntimeError("The gripper command is in progress. Wait before reloading.")
    if old and (old.busy or old.control.busy):
        raise RuntimeError("Control panel is busy. Stop and verify the robot before reloading.")
    if old and old.control.command_pending:
        raise RuntimeError("A controller acknowledgement is pending. Wait before reloading.")
    if old:
        reason = old.control.feedback_retention_reason()
        if reason is None and old.keyboard_session is not None:
            reason = old.keyboard_session.control.feedback_retention_reason()
        if reason is None and old.stop_task is not None and old.stop_task.done():
            result = old.stop_task.result()
            if not result["ok"]:
                reason = result.get("error") or "Controlled stop failed"
        if reason is not None or old.report.get("synchronization_retained"):
            raise RuntimeError(
                "Robot stop is unconfirmed. Keep the current panel for safety recovery."
            )
    if old_gripper is not None:
        old_gripper.closed = True
    if old:
        old.keep_mirror = True
        old.closed = True
        if old.task:
            await old.task
    if old_gripper is not None:
        if old_gripper.task:
            try:
                await old_gripper.task
            except Exception as error:
                print("MG400_GRIPPER_CLOSED", str(error), flush=True)
    _reload_control_modules()
    mirror = getattr(builtins, MIRROR_SINGLETON, None)
    if mirror is None or mirror.stop_event.is_set():
        namespace = runpy.run_path(
            str(ROOT / "apps/isaacsim/mirror_bootstrap.py"),
            init_globals={"SHOW_WINDOW": False},
        )
        await namespace["MG400_MIRROR_BOOTSTRAP_TASK"]
        mirror = getattr(builtins, MIRROR_SINGLETON)
    if getattr(mirror, "host", None) != load_robot_host(ROBOT_CONFIG):
        raise RuntimeError(
            "Robot address does not match the live mirror. Restart the mirror and control panel."
        )
    if not hasattr(mirror, "ownership"):
        raise RuntimeError("Restart synchronization to load robot connection ownership.")
    mirror.ownership.acquire()
    if getattr(mirror, "window", None):
        mirror.window.visible = False
    namespace = runpy.run_path(
        str(ROOT / "apps/isaacsim/gripper_control.py"), run_name="mg400_gripper_module"
    )
    gripper = namespace["GripperPanel"](
        feedback_path=str(data_path("work/gripper_olares_feedback.json"))
    )
    gripper.auto_initialize_pending = AUTO_INITIALIZE_GRIPPER
    panel = ControlPanel(mirror, gripper)
    setattr(builtins, GRIPPER_SINGLETON, gripper)
    setattr(builtins, SINGLETON, panel)
    panel.task = asyncio.ensure_future(panel.run())
    return panel


if __name__ in ("__main__", "<run_path>"):
    MG400_CONTROL_START_TASK = asyncio.ensure_future(start())
