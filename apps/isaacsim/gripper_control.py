"""Control the real gripper from Isaac Script Editor. Startup only reads state."""

import asyncio
import builtins
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
        self.closed = self.failed = False
        self.mirror_task = None
        self.report = {
            "request": None,
            "result": "Waiting for robot state.",
            "mode": None,
            "di": None,
            "ack": False,
            "arrival_observed": False,
            "write_acks": [],
        }
        self.report["mirror"] = {"status": "Waiting for USB position feedback."}

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
                        "Feedback stale or invalid. Holding last position."
                    )
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
                    "status": "Following measured USB position.",
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

    def measured_position(self):
        """Return the fresh measured position, or None when feedback is unavailable."""
        sample = self.feedback.get()
        return None if sample is None else sample["position"]

    def position_sample(self):
        sample = self.feedback.get()
        if self.feedback_path is None or sample is None:
            return None
        control = sample.get("control")
        if control is None or control["locked"]:
            return None
        return sample

    def submit_position(self, position):
        """Send one target from 0 (closed) to 1000 (open) through the USB collector."""
        if type(position) is not int or not 0 <= position <= 1000:
            raise ValueError("The target position must be an integer from 0 to 1000.")
        if self.closed or self.failed or self.busy:
            return None
        sample = self.position_sample()
        if sample is None:
            self.report["result"] = "Position control needs a fresh --commands collector."
            return None
        self.action = asyncio.ensure_future(self.perform_position(position, sample))
        return self.action

    async def perform_position(self, position, initial):
        request_id = str(uuid4())
        self.report.update(
            request="position", target=position, result="The position command is in progress.",
            ack=False, arrival_observed=False, write_acks=[],
        )
        try:
            if self.closed:
                return
            current = self.position_sample()
            if current is None or current["session"] != initial["session"]:
                raise RuntimeError("The USB collector feedback changed before the command.")
            path = Path(self.feedback_path)
            path = path.with_name(path.name + ".command.json")
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps({
                "id": request_id, "session": initial["session"],
                "sent_at": time.time(), "position": position,
            }) + "\n")
            temporary.replace(path)
            deadline = time.monotonic() + 5
            while not self.closed and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                sample = self.feedback.get()
                if sample is None or sample["session"] != initial["session"]:
                    raise RuntimeError("The USB collector feedback is stale or changed.")
                control = sample.get("control", {})
                if control.get("locked"):
                    raise RuntimeError(control.get("error") or "The USB collector is locked.")
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
                    self.report["result"] = "The gripper reports object contact before the target."
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
        if self.closed or self.failed or self.busy:
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

    async def run(self):
        import omni.kit.app
        import omni.ui as ui

        self.window = ui.Window(
            "MG400 Real Gripper", width=460,
            height=410 if self.feedback_path is not None else 260,
        )
        target = None
        target_initialized = False
        with self.window.frame:
            with ui.VStack(spacing=8):
                transport = "USB position" if self.feedback_path is not None else "DO1 / DO2"
                ui.Label(f"REAL GRIPPER | {self.host} | {transport}", height=24)
                inputs = ui.Label("DI1 / DI2: waiting", height=24)
                position_widgets = []
                if self.feedback_path is not None:
                    ui.Label("Opening: 0 closed, 1000 open | Z / X: open / close", height=20)
                    target = ui.IntSlider(min=0, max=1000, height=24)
                    with ui.HStack(height=28, spacing=8):
                        field = ui.IntField(model=target.model)
                        move = ui.Button(
                            "Move to opening",
                            clicked_fn=lambda: self.submit_position(
                                target.model.get_value_as_int()
                            ),
                        )
                    position_widgets = [target, field, move]
                    ui.Label(
                        "First move disables I/O control. Force and speed stay unchanged.",
                        height=34, word_wrap=True,
                    )
                with ui.HStack(height=32, spacing=8):
                    buttons = [
                        ui.Button(label, clicked_fn=lambda name=name: self.submit(name))
                        for label, name in (("Open gripper", "open"), ("Close gripper", "close"))
                    ]
                status = ui.Label(self.report["result"], word_wrap=True, height=65)
                mirror_status = ui.Label(self.report["mirror"]["status"], word_wrap=True, height=50)
        self.mirror_task = asyncio.ensure_future(self.mirror())
        next_read = 0
        try:
            while not self.closed and self.window.visible:
                await omni.kit.app.get_app().next_update_async()
                if not self.busy and time.monotonic() >= next_read:
                    try:
                        if self.feedback_path is None:
                            await asyncio.to_thread(self.refresh)
                        else:
                            sample = self.feedback.get()
                            if sample is None:
                                raise RuntimeError("USB feedback is stale or unavailable.")
                            control = sample.get("control")
                            if control is None:
                                raise RuntimeError("Start the USB collector with --commands.")
                            if control["locked"]:
                                raise RuntimeError(
                                    control.get("error") or "The collector is locked."
                                )
                        self.report["read_error"] = None
                        if self.report["request"] is None:
                            self.report["result"] = (
                                "Ready. Set an opening or choose Open gripper / Close gripper."
                                if self.feedback_path is not None
                                else "Ready. Choose Open gripper or Close gripper."
                            )
                    except Exception as error:
                        self.report["read_error"] = str(error)
                    self.write_report()
                    next_read = time.monotonic() + 0.5
                if target is not None and not target_initialized:
                    position = self.measured_position()
                    if position is not None:
                        target.model.set_value(position)
                        target_initialized = True
                inputs.text = (
                    "USB feedback and position commands share one serial connection."
                    if self.feedback_path is not None else
                    f"Mode: {self.report['mode']} | DI1 / DI2 raw: {self.report['di']}"
                )
                status.text = self.report.get("read_error") or self.report["result"]
                measured = self.report["mirror"]
                mirror_status.text = measured["status"]
                if "position" in measured:
                    mirror_status.text += (
                        f"\nActual: {measured['position']} / 1000 | "
                        f"Each jaw: {measured['q_m'] * 1000:.2f} mm"
                    )
                for button in buttons + position_widgets:
                    button.enabled = not (self.busy or self.failed or self.report.get("read_error"))
        finally:
            self.closed = True
            if self.mirror_task:
                await self.mirror_task
            if self.action:
                await self.action
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
        if old.busy:
            raise RuntimeError("The gripper command is in progress. Wait before loading the panel.")
        old.closed = True
        if old.task:
            await old.task
    panel = GripperPanel(feedback_path)
    setattr(builtins, SINGLETON, panel)
    panel.task = asyncio.ensure_future(panel.run())
    return panel


if __name__ in ("__main__", "<run_path>"):
    MG400_GRIPPER_START_TASK = asyncio.ensure_future(start(globals().get("FEEDBACK_PATH")))
