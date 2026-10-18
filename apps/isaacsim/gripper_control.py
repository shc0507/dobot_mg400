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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.robot_config import load_robot_host  # noqa: E402
from mg400_agent.runtime_paths import data_path  # noqa: E402

REPORT = data_path("isaacsim/real_gripper_status.json")
ROBOT_CONFIG = ROOT / "config/robot.toml"
SINGLETON = "_mg400_isaac_real_gripper"


class GripperPanel:
    def __init__(self, feedback_path=None):
        self.host = load_robot_host(ROBOT_CONFIG)
        self.feedback_path = feedback_path
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

        from mg400_agent.gripper_feedback import FeedbackState, FileFeedback
        from mg400_agent.gripper_mirror import JawMirror

        app = omni.kit.app.get_app()
        feedback = FeedbackState()
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

        self.window = ui.Window("MG400 Real Gripper", width=440, height=260)
        with self.window.frame:
            with ui.VStack(spacing=8):
                ui.Label(f"REAL GRIPPER | {self.host} | DO1 / DO2", height=24)
                inputs = ui.Label("DI1 / DI2: waiting", height=24)
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
                        await asyncio.to_thread(self.refresh)
                        self.report["read_error"] = None
                        if self.report["request"] is None:
                            self.report["result"] = "Ready. Choose Open gripper or Close gripper."
                    except Exception as error:
                        self.report["read_error"] = str(error)
                    self.write_report()
                    next_read = time.monotonic() + 0.5
                inputs.text = f"Mode: {self.report['mode']} | DI1 / DI2 raw: {self.report['di']}"
                status.text = self.report.get("read_error") or self.report["result"]
                measured = self.report["mirror"]
                mirror_status.text = measured["status"]
                if "position" in measured:
                    mirror_status.text += (
                        f"\nActual: {measured['position']} / 1000 | "
                        f"Each jaw: {measured['q_m'] * 1000:.2f} mm"
                    )
                for button in buttons:
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
