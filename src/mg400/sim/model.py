"""Shared deterministic MG400 and gripper models, without transport side effects."""

import json
import math
import os
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from mg400.feedback import Feedback

START = (30.0, 0.0, 10.0, 0.0)

POSE = (250.0, 10.0, 40.0, 30.0)

MUTATIONS = (
    "SpeedFactor(",
    "SetUser(",
    "MoveJog(",
    "MovL(",
    "JointMovJ(",
    "EnableRobot(",
    "ResetRobot(",
    "DisableRobot(",
)

MOTION = ("MoveJog(", "MovL(", "JointMovJ(", "EnableRobot(", "ResetRobot(", "DisableRobot(")


def numbers(values):
    return ",".join(f"{value:.6f}" for value in values)


class Controller:
    """Simulate the MG400 dashboard (29999), the motion port (30003) and the feedback stream.

    The feedback reports mode 5 when idle, mode 11 while a MoveJog runs, and mode 4 after
    DisableRobot(). Its speed ratio follows SpeedFactor. With slow_jog_stop, the arm keeps
    mode 11 after MoveJog() until the test calls rest(). A J1 jog turns J1 by jog_rate
    degrees per feedback sample. JointMovJ arrives at once.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sample = Feedback(START, 5, 0, False, 100.0)
        self._log = []
        self._jog = None
        self.pose = POSE
        self.frames = {0: (0.0, 0.0, 0.0, 0.0), 8: (0.0, 0.0, 0.0, 0.0)}
        self.slow_jog_stop = False
        self.ignore_speed_factor = False
        self.jog_rate = 0.0
        self.unexpected = []
        self.reply_hooks = []
        self.connect_hooks = []
        self.closed = threading.Event()

    @property
    def sample(self):
        with self._lock:
            return self._sample

    def update(self, **changes):
        with self._lock:
            self._sample = replace(self._sample, **changes)

    def advance(self):
        """Move J1 for one feedback sample while a J1 jog runs. Return the new sample."""
        with self._lock:
            sample = self._sample
            sign = {"MoveJog(J1+)": 1, "MoveJog(J1-)": -1}.get(self._jog, 0)
            if sample.mode == 11 and sign and self.jog_rate:
                joints = (sample.joints_deg[0] + sign * self.jog_rate, *sample.joints_deg[1:])
                self._sample = sample = replace(sample, joints_deg=joints)
            return sample

    def rest(self):
        """End a slow jog stop: the arm reports idle (mode 5) from now on."""
        with self._lock:
            assert self._sample.mode == 11
            self._log.append(("note", None, "rest"))
            self._sample = replace(self._sample, mode=5)
            self._jog = None

    def acked(self, text):
        with self._lock:
            return any(kind == "ack" and entry == text for kind, _, entry in self._log)

    def ports(self):
        with self._lock:
            return [port for kind, port, _ in self._log if kind == "connect"]

    def mark(self):
        with self._lock:
            return len(self._log)

    def sent(self, since=0):
        with self._lock:
            return [text for kind, _, text in self._log[since:] if kind == "send"]

    def mutations(self):
        return [text for text in self.sent() if text.startswith(MUTATIONS)]

    def motion(self):
        return [text for text in self.sent() if text.startswith(MOTION)]

    def position(self, kind, text):
        """Return the log position of the first send, ack or note with this text."""
        with self._lock:
            for index, (entry_kind, _, entry_text) in enumerate(self._log):
                if entry_kind == kind and entry_text == text:
                    return index
            raise AssertionError(f"No {kind} of {text} in {self._log}")

    def hold(self, event, connection):
        """Block a reply until the test opens the gate. A stop can still shut the connection."""
        deadline = time.monotonic() + 5
        while not event.wait(0.002):
            if connection.shut.is_set():
                raise ConnectionError("Connection was shut down")
            if self.closed.is_set():
                raise ConnectionError("The simulated controller closed")
            if time.monotonic() > deadline:
                raise TimeoutError("The test did not open the gate")

    def connected(self, port):
        with self._lock:
            self._log.append(("connect", port, ""))
        for hook in list(self.connect_hooks):
            hook(port)

    def before_reply(self, connection, text):
        for hook in list(self.reply_hooks):
            hook(connection, text)
        if connection.shut.is_set():
            raise ConnectionError("Connection was shut down")
        with self._lock:
            self._log.append(("ack", connection.port, text))

    def execute(self, port, text):
        with self._lock:
            self._log.append(("send", port, text))
            try:
                return self._execute(port, text)
            except AssertionError:
                self.unexpected.append((port, text))
                raise

    def _execute(self, port, text):
        sample = self._sample
        if text.startswith("MoveJog("):
            assert port == 30003, f"{text} on port {port}"
            if text == "MoveJog()":
                if sample.mode == 11 and not self.slow_jog_stop:
                    self._sample = replace(sample, mode=5)
                    self._jog = None
            else:
                assert sample.mode == 5, f"{text} in mode {sample.mode}"
                self._sample = replace(sample, mode=11)
                self._jog = text
            return ""
        if text.startswith("JointMovJ("):
            assert port == 30003 and sample.mode == 5, f"{text} on {port} in mode {sample.mode}"
            joints = tuple(float(value) for value in text[len("JointMovJ(") : -1].split(",")[:4])
            if len(joints) != 4 or not all(math.isfinite(value) for value in joints):
                raise ValueError("Expected four finite joints")
            self._sample = replace(sample, joints_deg=joints)
            return ""
        if text.startswith("MovL("):
            assert port == 30003 and sample.mode == 5, f"{text} on {port} in mode {sample.mode}"
            pose = tuple(float(value) for value in text[len("MovL(") : -1].split(",")[:4])
            if len(pose) != 4 or not all(math.isfinite(value) for value in pose):
                raise ValueError("Expected four finite pose values")
            self.pose = pose
            return ""
        assert port == 29999, f"{text} on port {port}"
        if text == "RobotMode()":
            return str(sample.mode)
        if text == "GetErrorID()":
            return "[[],[],[],[],[]]"
        if text == "GetAngle()":
            return numbers((*sample.joints_deg, 0.0, 0.0))
        if text == "GetPose(0,0)":
            return numbers(self.pose)
        if text.startswith("CalcUser(") and text.endswith(",1,{0,0,0,0})"):
            return numbers(self.frames[int(text[len("CalcUser(") :].split(",")[0])])
        if text.startswith("SetUser("):
            index, values = text[len("SetUser(") : -2].split(",{")
            self.frames[int(index)] = tuple(float(value) for value in values.split(","))
            return ""
        if text.startswith("SpeedFactor("):
            if not self.ignore_speed_factor:
                ratio = float(text[len("SpeedFactor(") : -1])
                self._sample = replace(sample, speed_scaling_raw=ratio)
            return ""
        if text.startswith("InverseSolution("):
            return numbers(sample.joints_deg)
        if text == "ClearError()":
            self._sample = replace(sample, error_status=False, mode=4)
            self._jog = None
            return ""
        if text == "Continue()":
            return ""
        if text == "ResetRobot()":
            if sample.mode in (7, 11):
                self._sample = replace(sample, mode=5)
                self._jog = None
            return ""
        if text == "DisableRobot()":
            self._sample = replace(sample, mode=4)
            self._jog = None
            return ""
        if text == "EnableRobot()":
            self._sample = replace(sample, mode=5)
            return ""
        raise AssertionError(f"Unexpected controller command: {text}")


def arrive(request):
    if request.get("operation") == "initialize":
        return {"init": 1, "state": 1, "position": 1000}
    return {"state": 1, "position": request["position"]}


class Collector:
    """Simulate the USB collector: publish gripper feedback and answer position commands."""

    def __init__(self, directory=None, *, feedback_path=None):
        self.feedback_path = (
            Path(feedback_path) if feedback_path is not None else directory / "gripper.json"
        )
        self.command_path = self.feedback_path.with_name(self.feedback_path.name + ".command.json")
        self.command_lock_path = self.feedback_path.with_name(
            self.feedback_path.name + ".command-lock.json"
        )
        self._scratch = self.feedback_path.with_name(self.feedback_path.name + ".collector.tmp")
        self._lock = threading.Lock()
        self._sample = {
            "session": str(uuid.uuid4()),
            "seq": 0,
            "init": 1,
            "state": 1,
            "position": 500,
            "control": {
                "protocol": 3,
                "command_id": None,
                "operation": None,
                "target": None,
                "ack": False,
                "locked": False,
                "error": None,
                "io_mode": None,
            },
        }
        self._published = []
        self._answered = []
        self.answer = arrive
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mg400-sim-gripper", daemon=True)

    def start(self):
        self._emit()
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(2)

    def set(self, **changes):
        with self._lock:
            self._sample.update(changes)

    def lock(self, error):
        """Report a collector lock, as after a lost write reply."""
        with self._lock:
            self._sample["control"].update(locked=True, error=error)

    def targets(self, kind):
        with self._lock:
            requests = self._published if kind == "published" else self._answered
            return [request["position"] for request in requests]

    def _honor_command_lock(self):
        if self.command_lock_path.exists() or self.command_lock_path.is_symlink():
            self.lock("A persistent gripper command lock requires operator review.")

    def _emit(self):
        self._honor_command_lock()
        with self._lock:
            self._sample["seq"] += 1
            self._sample["sent_at"] = time.time()
            text = json.dumps(self._sample)
        # Replace the file in one step, so the adapter never reads a partial sample.
        with open(self._scratch, "w") as file:
            file.write(text)
        os.replace(self._scratch, self.feedback_path)

    def _run(self):
        while not self._stop.wait(0.01):
            self._serve()
            self._emit()

    def _serve(self):
        self._honor_command_lock()
        try:
            with open(self.command_path) as file:
                request = json.load(file)
        except (OSError, ValueError):
            return
        if not isinstance(request, dict):
            return
        operation = request.get("operation", "position")
        deadline = request.get("deadline_monotonic")
        if (
            not isinstance(request.get("id"), str)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline <= time.monotonic()
            or operation not in {"position", "initialize"}
            or (
                operation == "position"
                and (
                    type(request.get("position")) is not int or not 0 <= request["position"] <= 1000
                )
            )
        ):
            return
        with self._lock:
            if (
                request.get("session") != self._sample["session"]
                or self._sample["control"]["locked"]
            ):
                return
            if all(item["id"] != request["id"] for item in self._published):
                self._published.append(request)
            if any(item["id"] == request["id"] for item in self._answered):
                return
        changes = self.answer(request)
        if changes is None:
            return
        with self._lock:
            self._answered.append(request)
            self._sample["control"].update(
                command_id=request["id"],
                operation=operation,
                target=request.get("position") if operation == "position" else None,
                ack=True,
            )
            self._sample.update(changes)

    def answered_count(self):
        with self._lock:
            return len(self._answered)
