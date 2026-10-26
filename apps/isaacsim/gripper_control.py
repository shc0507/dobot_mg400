"""Control the real gripper. Initialization is manual by default."""

import asyncio
import builtins
import contextlib
import json
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.gripper_feedback import FeedbackState  # noqa: E402
from mg400_agent.robot_config import load_robot_host  # noqa: E402
from mg400_agent.runtime_paths import data_path  # noqa: E402

REPORT = data_path("isaacsim/real_gripper_status.json")
ROBOT_CONFIG = ROOT / "config/robot.toml"
SINGLETON = "_mg400_isaac_real_gripper"


class GripperPanel:
    def __init__(self, feedback_path=None):
        self.host = load_robot_host(ROBOT_CONFIG)
        self.feedback_path = feedback_path
        self.feedback = FeedbackState()
        self.lock = threading.Lock()
        self.action = self.task = None
        self.closed = self.closing = self.failed = False
        self.owner = None
        self.compact = False
        self.edit_callback = None
        self.mirror_task = None
        self.auto_initialize_pending = False
        self.initialize_button = self.initialization_status = None
        self.report = {
            "request": None,
            "result": "Waiting for robot state.",
            "mode": None,
            "di": None,
            "ack": False,
            "arrival_observed": False,
            "contact_observed": False,
            "write_acks": [],
        }
        self.report["mirror"] = {"status": "Waiting for USB position feedback."}

    def request_collector_start(self):
        if self.feedback_path is None:
            return
        request = Path(self.feedback_path).with_name("gripper_collector.start")
        temporary = request.with_suffix(".tmp")
        try:
            temporary.write_text(str(uuid4()) + "\n")
            temporary.replace(request)
            self.report["collector_start_error"] = None
        except OSError as error:
            self.report["collector_start_error"] = f"Could not request USB feedback: {error}"
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    async def mirror(self):
        import carb.eventdispatcher
        import omni.kit.app
        import omni.timeline
        import omni.usd

        from mg400_agent.gripper_feedback import FileFeedback
        from mg400_agent.gripper_mirror import JawMirror

        app = omni.kit.app.get_app()
        feedback = self.feedback
        subscription = None
        file_feedback = None
        if self.feedback_path is not None:
            file_feedback = FileFeedback(self.feedback_path, feedback)
        else:
            manager = app.get_extension_manager()
            name = "omni.kit.livestream.messaging"
            if not manager.is_extension_enabled(name):
                manager.set_extension_enabled_immediate(name, True)
            subscription = carb.eventdispatcher.get_eventdispatcher().observe_event(
                event_name="mg400.gripper.feedback",
                on_event=lambda event: feedback.receive(dict(event.payload)),
            )
        mirror = None
        next_file_read = 0
        try:
            while not self.closed:
                await app.next_update_async()
                if file_feedback is not None and time.monotonic() >= next_file_read:
                    file_feedback.poll()
                    next_file_read = time.monotonic() + 0.05
                sample = feedback.get()
                if sample is None:
                    self.report["mirror"]["status"] = (
                        feedback.diagnostic() or "Feedback stale or invalid."
                    ) + " Holding last position."
                    continue
                if self.owner is not None and not getattr(self.owner, "sync_enabled", True):
                    self.report["mirror"]["status"] = "Scene synchronization stopped."
                    continue
                if not omni.timeline.get_timeline_interface().is_stopped():
                    self.report["mirror"]["status"] = (
                        "Stop the simulation timeline to mirror the gripper."
                    )
                    continue
                stage = omni.usd.get_context().get_stage()
                if mirror is None or mirror.stage != stage:
                    mirror = JawMirror(stage)
                arm = getattr(builtins, "_mg400_isaac_feedback_mirror", None)
                pose = getattr(arm, "pose_mirror", None)
                if arm is not None and not arm.stop_event.is_set() and pose is None:
                    self.report["mirror"]["status"] = (
                        "Restart the arm mirror with the updated script. Holding last position."
                    )
                    continue
                result = mirror.apply(sample["position"])
                if pose is not None and pose.stage == stage:
                    pose.jaws.update(pgea_left_slide=result["q_m"], pgea_right_slide=result["q_m"])
                self.report["mirror"] = {
                    "status": (
                        "Object dropped. Following measured USB position."
                        if sample["state"] == 3
                        else "Following measured USB position."
                    ),
                    **sample,
                    **result,
                }
        except Exception as error:
            self.report["mirror"]["status"] = f"Mirror stopped: {error}"
        finally:
            del subscription

    @property
    def busy(self):
        return self.action is not None and not self.action.done()

    @property
    def manual_enabled(self):
        if self.closed or self.closing or self.failed or self.busy or self.report.get("read_error"):
            return False
        return self.owner is None or not (
            self.owner.busy
            or self.owner.keyboard_enabled
            or self.owner.keyboard_arming
            or self.owner.closed
            or not getattr(self.owner, "sync_enabled", True)
        )

    @property
    def target_edit_enabled(self):
        if not self.compact:
            return self.manual_enabled
        return (
            not self.closed
            and not self.closing
            and not self.failed
            and not self.busy
            and not self.report.get("read_error")
            and self.target_initialized
            and self.owner.targets_editable
            and not self.owner.busy
            and not self.owner.keyboard_arming
            and not self.owner.closed
            and self.owner.sync_enabled
            and not self.owner.control.stopped
        )

    def _submit_manual(self, request):
        if self.manual_enabled:
            return self.submit(request)
        return None

    def _submit_position_manual(self):
        if self.manual_enabled:
            return self.submit_position(self.target.model.get_value_as_int())
        return None

    def measured_position(self):
        """Return the fresh measured position, or None when feedback is unavailable."""
        sample = self.feedback.get()
        return None if sample is None else sample["position"]

    def _initialization_error(self, sample):
        if self.closed or self.closing or self.failed:
            return "Gripper control is closed or locked."
        if self.feedback_path is None:
            return "Initialization requires the Olares USB collector."
        if sample is None:
            return "Wait for fresh USB feedback before initialization."
        control = sample.get("control")
        if control is None or control["protocol"] != 3:
            return "Update and restart the USB collector to enable initialization."
        if control["locked"]:
            return control.get("error") or "The collector is locked."
        if sample["init"] != 0:
            return (
                "The gripper is already initialized."
                if sample["init"] == 1
                else "Gripper initialization is in progress."
            )
        if self.owner is not None:
            if (
                self.owner.busy
                or self.owner.keyboard_enabled
                or self.owner.keyboard_arming
                or self.owner.closed
                or not self.owner.sync_enabled
                or self.owner.control.stopped
            ):
                return "Keep the robot idle and keyboard control off before initialization."
            try:
                self.owner.control.read_state()
            except Exception as error:
                return f"Wait for fresh idle robot feedback. {error}"
        return None

    @property
    def initialization_enabled(self):
        return not self.busy and self._initialization_error(self.feedback.get_status()) is None

    def maybe_initialize(self):
        """Check startup feedback and submit at most one initialization request."""
        if not self.auto_initialize_pending:
            return None
        sample = self.feedback.get_status()
        if sample is not None and sample["init"] in (1, 2):
            self.auto_initialize_pending = False
            return None
        if self.initialization_enabled:
            return self.submit_initialization()
        return None

    def submit_initialization(self):
        """Initialize through the collector. Initialization opens the gripper."""
        if self.busy:
            return None
        sample = self.feedback.get_status()
        error = self._initialization_error(sample)
        if error:
            self.report["result"] = error
            return None
        self.auto_initialize_pending = False
        self.action = asyncio.ensure_future(self.perform_initialization(sample))
        return self.action

    async def perform_initialization(self, initial):
        request_id = str(uuid4())
        self.report.update(
            request="initialize",
            target=None,
            result="Initializing gripper. The jaws will open.",
            ack=False,
            arrival_observed=False,
            contact_observed=False,
            write_acks=[],
            command_published=False,
            read_error=None,
        )
        temporary = None
        try:
            current = self.feedback.get_status()
            error = self._initialization_error(current)
            if error or current["session"] != initial["session"]:
                self.report["result"] = error or "The USB collector session changed."
                return False
            path = Path(self.feedback_path)
            path = path.with_name(path.name + ".command.json")
            temporary = path.with_name(path.name + ".tmp")
            deadline = time.monotonic() + 15
            try:
                temporary.write_text(
                    json.dumps(
                        {
                            "id": request_id,
                            "session": initial["session"],
                            "sent_at": time.time(),
                            "operation": "initialize",
                            "deadline_monotonic": deadline,
                        }
                    )
                    + "\n"
                )
            except OSError as error:
                self.report["result"] = (
                    f"No command sent. Could not prepare initialization: {error}"
                )
                return False
            self.report["command_published"] = None
            temporary.replace(path)
            self.report["command_published"] = True
            while not self.closed and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                if time.monotonic() >= deadline:
                    break
                sample = self.feedback.get_status()
                if sample is None:
                    continue
                if sample["session"] != initial["session"]:
                    raise RuntimeError("The USB collector session changed.")
                control = sample.get("control")
                if control is None or control["protocol"] != 3:
                    raise RuntimeError("The collector no longer supports initialization.")
                if control["locked"]:
                    raise RuntimeError(control.get("error") or "The collector is locked.")
                if (
                    sample["seq"] > initial["seq"]
                    and control["command_id"] == request_id
                    and control.get("operation") == "initialize"
                    and control["ack"]
                    and sample["init"] == 1
                    and sample["state"] == 1
                    and 0 <= sample["position"] <= 1000
                ):
                    self.report.update(
                        ack=True,
                        arrival_observed=True,
                        result="Gripper initialization complete.",
                    )
                    self.target_initialized = False
                    return True
            raise TimeoutError("Gripper initialization did not complete within 15 seconds.")
        except Exception as error:
            self.failed = True
            self.report["result"] = f"Control is locked. Check the gripper. {error}"
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
            self.write_report()

    def pending_target(self):
        """Return a user-edited opening, or None when no opening edit is pending."""
        if not self.target_initialized:
            return None
        target = self.target.model.get_value_as_int()
        return target if target != self.target_baseline else None

    def position_sample(self):
        sample = self.feedback.get()
        if self._position_error(sample):
            return None
        return sample

    def _position_error(self, sample):
        if self.feedback_path is None:
            return "Position control needs a fresh --commands collector."
        if sample is None:
            return (
                self.feedback.diagnostic()
                or self.report.get("collector_start_error")
                or "USB feedback is stale or unavailable. Check the collector on Olares."
            )
        control = sample.get("control")
        if control is None:
            return "Start the USB collector with --commands."
        if control["protocol"] not in (2, 3):
            return (
                "Update and restart the USB collector for position commands with deadline support."
            )
        if control["locked"]:
            return control.get("error") or "The collector is locked."
        return None

    def submit_position(self, position):
        """Send one target from 0 (closed) to 1000 (open) through the USB collector."""
        if type(position) is not int or not 0 <= position <= 1000:
            raise ValueError("The target position must be an integer from 0 to 1000.")
        if self.closed or self.closing or self.failed or self.busy:
            return None
        sample = self.feedback.get()
        error = self._position_error(sample)
        if error:
            self.report["command_published"] = False
            self.report["result"] = error
            return None
        self.action = asyncio.ensure_future(self.perform_position(position, sample))
        return self.action

    async def perform_position(self, position, initial):
        request_id = str(uuid4())
        self.report.update(
            request="position",
            target=position,
            result="The position command is in progress.",
            ack=False,
            arrival_observed=False,
            contact_observed=False,
            write_acks=[],
            command_published=False,
        )
        try:
            if (
                self.closed
                or self.closing
                or (
                    self.owner is not None
                    and (
                        self.owner.closed
                        or not getattr(self.owner, "sync_enabled", True)
                        or getattr(getattr(self.owner, "control", None), "stopped", False)
                    )
                )
            ):
                self.report["result"] = "The panel cancelled the command before sending it."
                return False
            current = self.feedback.get()
            reason = self._position_error(current)
            if reason or current["session"] != initial["session"]:
                reason = reason or "The USB collector feedback changed before the command."
                self.report["result"] = f"No command sent. {reason} Check feedback and retry."
                return False
            path = Path(self.feedback_path)
            path = path.with_name(path.name + ".command.json")
            temporary = path.with_name(path.name + ".tmp")
            deadline = time.monotonic() + 5
            try:
                temporary.write_text(
                    json.dumps(
                        {
                            "id": request_id,
                            "session": initial["session"],
                            "sent_at": time.time(),
                            "position": position,
                            "deadline_monotonic": deadline,
                        }
                    )
                    + "\n"
                )
            except OSError as error:
                self.report["result"] = (
                    f"No command sent. Could not prepare the command file: {error} "
                    "Retry after repair."
                )
                return False
            self.report["command_published"] = None
            temporary.replace(path)
            self.report["command_published"] = True
            while not self.closed and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                if time.monotonic() >= deadline:
                    break
                sample = self.feedback.get()
                if sample is None:
                    diagnostic = self.feedback.diagnostic()
                    if diagnostic:
                        raise RuntimeError(diagnostic)
                    continue
                if sample["session"] != initial["session"]:
                    raise RuntimeError("The USB collector session changed.")
                error = self._position_error(sample)
                if error:
                    raise RuntimeError(error)
                control = sample["control"]
                if (
                    sample["seq"] <= initial["seq"]
                    or control.get("command_id") != request_id
                    or control.get("target") != position
                    or not control.get("ack")
                ):
                    continue
                self.report["ack"] = True
                if sample["state"] == 1 and abs(sample["position"] - position) <= 5:
                    self.report.update(
                        arrival_observed=True,
                        result="Measured position is within 5 / 1000 of the target.",
                    )
                    return
                if sample["state"] == 2:
                    self.report.update(
                        contact_observed=True,
                        result="The gripper reports object contact before the target.",
                    )
                    return
            raise TimeoutError("The gripper target arrival is not confirmed.")
        except Exception as error:
            self.failed = True
            self.report["result"] = f"Control is locked. Check the gripper. {error}"
        finally:
            self.write_report()

    def command(self, text):
        allowed = {
            "RobotMode()",
            "GetErrorID()",
            "DI(1)",
            "DI(2)",
            "DOExecute(2,0)",
            "DOExecute(1,0)",
            "DOExecute(1,1)",
        }
        if text not in allowed:
            raise ValueError("This command is not allowed.")
        deadline = time.monotonic() + 2
        with socket.create_connection((self.host, 29999), timeout=2) as connection:
            connection.sendall(text.encode("ascii"))
            data = bytearray()
            while b";" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("The controller response timed out.")
                connection.settimeout(remaining)
                part = connection.recv(4096 - len(data))
                if not part:
                    raise ConnectionError("The controller closed the connection.")
                data.extend(part)
                if len(data) >= 4096:
                    raise ValueError("The controller response is too large.")
        response = data.decode("ascii").strip()
        match = re.fullmatch(r"(-?\d+),\{(.*)\}," + re.escape(text) + ";", response, re.DOTALL)
        if match is None:
            raise ValueError(f"The controller response does not match: {response}")
        if int(match[1]) != 0:
            raise RuntimeError(f"The controller rejected the command: {response}")
        return match[2]

    def state(self):
        mode = int(self.command("RobotMode()"))
        errors = json.loads(self.command("GetErrorID()"))
        if mode not in (4, 5) or not isinstance(errors, list) or not errors or any(errors):
            raise RuntimeError(f"The robot is not ready. Mode: {mode}. Alarms: {errors}")
        di = [self.command("DI(1)"), self.command("DI(2)")]
        if any(value not in ("0", "1") for value in di):
            raise ValueError(f"The digital input response is invalid: {di}")
        self.report.update(mode=mode, di=list(map(int, di)))
        return tuple(map(int, di))

    def operate(self, request):
        with self.lock:
            self.state()
            if self.closed or self.closing or self.failed:
                raise RuntimeError("The panel cannot send another command.")
            for text in ("DOExecute(2,0)", f"DOExecute(1,{int(request == 'close')})"):
                if self.closed or self.failed:
                    raise RuntimeError("The panel cannot send another command.")
                self.command(text)
                self.report["write_acks"].append(text)
            self.report["ack"] = True
            moving = False
            deadline = time.monotonic() + 5
            while not self.closed and time.monotonic() < deadline:
                di = self.state()
                moving = moving or di == (0, 0)
                if moving and di == (1, 0):
                    self.report.update(
                        arrival_observed=True, result="DI changed from 00 to 10. Arrival observed."
                    )
                    return
                time.sleep(0.1)
            self.report["result"] = "The controller accepted the command. Arrival is not confirmed."

    def refresh(self):
        with self.lock:
            self.state()

    def submit(self, request):
        if request not in ("open", "close"):
            raise ValueError("Use open or close.")
        if self.feedback_path is not None:
            return self.submit_position(1000 if request == "open" else 0)
        if self.closed or self.closing or self.failed or self.busy:
            return None
        self.action = asyncio.ensure_future(self.perform(request))
        return self.action

    async def perform(self, request):
        self.report.update(
            request=request,
            result="The command is in progress.",
            ack=False,
            arrival_observed=False,
            write_acks=[],
        )
        self.write_report()
        try:
            if self.closed or self.closing:
                self.report["result"] = "The panel cancelled the command before sending it."
                return
            await asyncio.to_thread(self.operate, request)
        except Exception as error:
            self.failed = True
            self.report["result"] = f"Control is locked. Check the gripper. {error}"
        finally:
            self.write_report()

    def write_report(self):
        self.report.update(time=datetime.now(timezone.utc).isoformat(), locked=self.failed)
        temporary = REPORT.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(REPORT)

    def build_ui(self):
        """Create the gripper controls inside the current UI frame."""
        import omni.ui as ui

        self.target = None
        self.target_initialized = False
        self.target_baseline = None
        with ui.VStack(spacing=8):
            transport = "USB position" if self.feedback_path is not None else "DO1 / DO2"
            ui.Label(f"REAL GRIPPER | {self.host} | {transport}", height=24)
            self.inputs = ui.Label("DI1 / DI2: waiting", height=24)
            self.position_widgets = []
            if self.feedback_path is not None:
                ui.Label("Opening: 0 closed, 1000 open | Z / X: open / close", height=20)
                self.target = ui.IntSlider(min=0, max=1000, height=24)
                with ui.HStack(height=28, spacing=8):
                    field = ui.IntField(model=self.target.model)
                    move = ui.Button(
                        "Move to opening",
                        clicked_fn=self._submit_position_manual,
                    )
                self.position_widgets = [self.target, field, move]
                ui.Label(
                    "First move disables I/O control. Force and speed stay unchanged.",
                    height=34,
                    word_wrap=True,
                )
            with ui.HStack(height=32, spacing=8):
                self.buttons = [
                    ui.Button(label, clicked_fn=lambda name=name: self._submit_manual(name))
                    for label, name in (("Open gripper", "open"), ("Close gripper", "close"))
                ]
            self.status = ui.Label(self.report["result"], word_wrap=True, height=65)
            self.mirror_status = ui.Label(
                self.report["mirror"]["status"],
                word_wrap=True,
                height=50,
            )
            if self.feedback_path is not None:
                self.build_initialization_controls()

    def build_initialization_controls(self):
        import omni.ui as ui

        with ui.HStack(height=30, spacing=6):
            self.initialize_button = ui.Button(
                "Initialize gripper",
                width=150,
                enabled=False,
                clicked_fn=self.submit_initialization,
                tooltip="Initialization opens the gripper to find the travel limit.",
            )
            ui.Label("Initialization opens the gripper.", word_wrap=True)
        self.initialization_status = ui.Label("", height=32, word_wrap=True)

    def build_compact_row(self):
        """Add one target row to the arm table. Edits do not send commands."""
        import omni.ui as ui

        self.compact = True
        self.target_initialized = False
        self.target_baseline = None
        self.buttons = []
        with ui.HStack(height=28, spacing=6):
            ui.Label(
                "Gripper",
                width=64,
                tooltip=(
                    "Opening: 0 closed, 1000 open. Execute targets moves the arm first. "
                    "It then applies an edited gripper target. "
                    "The first position command disables I/O control. "
                    "Force and speed do not change."
                ),
            )
            self.actual_label = ui.Label("--", width=84)
            self.target = ui.IntSlider(min=0, max=1000, enabled=False)
            self.edit_callback = self.target.model.add_begin_edit_fn(self.owner.begin_target_edit)
            field = ui.IntField(
                model=self.target.model,
                width=86,
                enabled=False,
                tooltip="Double-click to type an opening. Editing turns keyboard control off.",
            )
        self.position_widgets = [self.target, field]
        self.build_initialization_controls()

    async def run(self, window=None):
        import omni.kit.app
        import omni.ui as ui

        self.request_collector_start()
        standalone = window is None
        if standalone:
            window = ui.Window(
                "MG400 Real Gripper",
                width=460,
                height=520 if self.feedback_path is not None else 260,
            )
            with window.frame:
                self.build_ui()
        self.window = window
        self.mirror_task = asyncio.ensure_future(self.mirror())
        next_read = 0
        try:
            while not self.closed and not self.closing and self.window.visible:
                await omni.kit.app.get_app().next_update_async()
                if self.window.visible:
                    self.maybe_initialize()
                if not self.busy and time.monotonic() >= next_read:
                    try:
                        if self.feedback_path is None:
                            await asyncio.to_thread(self.refresh)
                        else:
                            error = self._position_error(self.feedback.get())
                            if error:
                                raise RuntimeError(error)
                        self.report["read_error"] = None
                        if self.report["request"] is None:
                            self.report["result"] = (
                                "Ready. Edit the gripper target, then click Execute targets."
                                if self.compact
                                else "Ready. Set an opening or choose Open gripper / Close gripper."
                                if self.feedback_path is not None
                                else "Ready. Choose Open gripper or Close gripper."
                            )
                    except Exception as error:
                        self.report["read_error"] = str(error)
                    self.write_report()
                    next_read = time.monotonic() + 0.5
                if self.target is not None and not self.target_initialized:
                    position = self.measured_position()
                    if position is not None:
                        self.target.model.set_value(position)
                        self.target_baseline = position
                        self.target_initialized = True
                if self.compact:
                    position = self.measured_position()
                    self.actual_label.text = "--" if position is None else str(position)
                    self.actual_label.tooltip = (
                        self.report.get("read_error") or self.report["result"]
                    )
                else:
                    self.inputs.text = (
                        "USB feedback and position commands share one serial connection."
                        if self.feedback_path is not None
                        else f"Mode: {self.report['mode']} | DI1 / DI2 raw: {self.report['di']}"
                    )
                    self.status.text = self.report.get("read_error") or self.report["result"]
                    measured = self.report["mirror"]
                    self.mirror_status.text = measured["status"]
                    if "position" in measured:
                        self.mirror_status.text += (
                            f"\nActual: {measured['position']} / 1000 | "
                            f"Each jaw: {measured['q_m'] * 1000:.2f} mm"
                        )
                for button in self.buttons + self.position_widgets:
                    button.enabled = self.target_edit_enabled
                if self.initialize_button is not None:
                    self.initialize_button.enabled = self.initialization_enabled
                    self.initialization_status.text = (
                        self.report["result"]
                        if self.busy or self.failed
                        else self.report.get("read_error")
                        or (
                            self.report["result"]
                            if self.report["request"] == "initialize"
                            else self._initialization_error(self.feedback.get_status()) or ""
                        )
                    )
        finally:
            self.closing = True
            if self.edit_callback is not None:
                self.target.model.remove_begin_edit_fn(self.edit_callback)
                self.edit_callback = None
            for button in self.buttons + self.position_widgets:
                button.enabled = False
            if self.initialize_button is not None:
                self.initialize_button.enabled = False
            try:
                if self.action:
                    await self.action
            finally:
                self.closed = True
                if self.mirror_task:
                    await self.mirror_task
                if standalone:
                    self.window.destroy()


async def start(feedback_path=None):
    lock_name = SINGLETON + "_start_lock"
    lock = getattr(builtins, lock_name, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(builtins, lock_name, lock)
    async with lock:
        return await replace_panel(feedback_path)


async def replace_panel(feedback_path=None):
    old = getattr(builtins, SINGLETON, None)
    if old:
        if getattr(old, "owner", None) is not None and not old.closed:
            raise RuntimeError("Use the MG400 Control panel.")
        if old.busy:
            raise RuntimeError("The gripper command is in progress. Wait before loading the panel.")
        if hasattr(old, "closing"):
            old.closing = True
        else:
            old.closed = True
        if old.task:
            await old.task
        old.closed = True
    panel = GripperPanel(feedback_path)
    setattr(builtins, SINGLETON, panel)
    panel.task = asyncio.ensure_future(panel.run())
    return panel


if __name__ in ("__main__", "<run_path>"):
    MG400_GRIPPER_START_TASK = asyncio.ensure_future(start(globals().get("FEEDBACK_PATH")))
