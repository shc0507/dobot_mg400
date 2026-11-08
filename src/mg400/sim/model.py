"""Shared deterministic MG400 model, without transport side effects."""

import math
import threading
import time
from dataclasses import replace

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
    "DOExecute(",
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
        self._sample = Feedback(START, 5, 0, False, 100.0, 0, 0, 0.0)
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
        if text.startswith("DOExecute("):
            index, status = (int(value) for value in text[len("DOExecute(") : -1].split(","))
            assert 1 <= index <= 16 and status in (0, 1), text
            outputs = (sample.digital_outputs or 0) & ~(1 << (index - 1)) | status << (index - 1)
            self._sample = replace(sample, digital_outputs=outputs)
            return ""
        if text == "EnableRobot()":
            self._sample = replace(sample, mode=5)
            return ""
        raise AssertionError(f"Unexpected controller command: {text}")
