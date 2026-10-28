"""Simulate an MG400 controller and a USB gripper collector for panel tests.

The real KeyboardJogSession and JointControl talk to the simulated MG400 through
socket fakes. The real WebGripper talks to the simulated collector through
temporary files. rig_factory builds RobotPanelState rigs on them.
"""

import collections
import itertools
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import replace
from functools import partial

import helpers

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl, _StationaryTimeoutError
from mg400_agent.web_control import RobotPanelState
from mg400_agent.web_gripper import WebGripper

HOST = "robot.test"
CLIENT = "browser-a"
START = (30.0, 0.0, 10.0, 0.0)
POSE = (250.0, 10.0, 40.0, 30.0)
JOGS = {"A": "MoveJog(J1+)", "W": "MoveJog(X+,CoordType=0,User=8,Tool=0)"}
RADIAL_FRAME = "SetUser(8,{0.000000,0.000000,0.000000,30.000000})"
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
READY = "Keyboard ready. Hold one key; release requests a stop."
REAL_WAIT_STABLE = JointControl._wait_stable
wait_for = partial(helpers.wait_for, timeout=3)


def numbers(values):
    return ",".join(f"{value:.6f}" for value in values)


def short_wait_stable(
    control,
    target=None,
    *,
    modes,
    final_modes,
    timeout=5.0,
    stopping=False,
    jog_stop_ack=None,
    jog_speed_percent=1,
):
    """Confirm rest from three steady fresh receipts instead of 0.4 s.

    A jog stop keeps the real check: fresh receipts after the MoveJog() ACK at the jog speed.
    """
    if jog_stop_ack is not None:
        return REAL_WAIT_STABLE(
            control,
            target,
            modes=modes,
            final_modes=final_modes,
            timeout=timeout,
            stopping=stopping,
            jog_stop_ack=jog_stop_ack,
            jog_speed_percent=jog_speed_percent,
        )
    deadline = time.monotonic() + timeout
    previous = anchor = None
    count = 0
    while time.monotonic() < deadline:
        if control.stopped and not stopping:
            raise RuntimeError("Control has been stopped; reopen the panel")
        sample, received = control._fresh(modes)
        if received != previous:
            previous = received
            if sample.mode not in final_modes or (
                target is not None and not control._near(sample.joints_deg, target)
            ):
                anchor, count = None, 0
            elif anchor is None or not control._near(sample.joints_deg, anchor, 0.025):
                anchor, count = sample.joints_deg, 1
            else:
                count += 1
                if count >= 3:
                    return sample
        time.sleep(0.002)
    raise _StationaryTimeoutError("Robot did not reach a fresh, verified stationary state")


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
        self.frames = {0: (0.0, 0.0, 0.0, 0.0), 8: (0.0, 0.0, 0.0, 0.0)}
        self.slow_jog_stop = False
        self.ignore_speed_factor = False
        self.jog_rate = 0.0
        self.unexpected = []
        self.reply_hooks = []
        self.connect_hooks = []
        self.closed = threading.Event()

    def install(self, monkeypatch):
        def getaddrinfo(host, port, *args, **kwargs):
            assert host == HOST
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

        def create_connection(address, *args, **kwargs):
            assert address[0] == HOST
            connection = Connection(self, address[1])
            self.connected(address[1])
            return connection

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", create_connection)
        monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: Connection(self))

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

    def block(self, prefix):
        gate = Gate(self, prefix)
        self.reply_hooks.append(gate)
        return gate

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
            joints = tuple(float(value) for value in text[len("JointMovJ(") :].split(",")[:4])
            self._sample = replace(sample, joints_deg=joints)
            return ""
        assert port == 29999, f"{text} on port {port}"
        if text == "RobotMode()":
            return str(sample.mode)
        if text == "GetErrorID()":
            return "[[],[],[],[],[]]"
        if text == "GetAngle()":
            return numbers((*sample.joints_deg, 0.0, 0.0))
        if text == "GetPose(0,0)":
            return numbers(POSE)
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


class Connection:
    """Socket fake. Each sent command gets one controller reply."""

    def __init__(self, controller, port=None):
        self.controller = controller
        self.port = port
        self.shut = threading.Event()
        self.text = None
        self.reply = b""

    def setblocking(self, flag):
        pass

    def settimeout(self, timeout):
        pass

    def connect_ex(self, address):
        self.port = address[1]
        self.controller.connected(self.port)
        return 0

    def sendall(self, data):
        self.text = data.decode("ascii")
        payload = self.controller.execute(self.port, self.text)
        self.reply = f"0,{{{payload}}},{self.text};".encode("ascii")

    def recv(self, size):
        self.controller.before_reply(self, self.text)
        reply, self.reply = self.reply, b""
        return reply

    def shutdown(self, how):
        self.shut.set()

    def close(self):
        self.shut.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Gate:
    """Hold the first controller reply to one command until the test opens the gate."""

    def __init__(self, controller, prefix):
        self._controller = controller
        self._prefix = prefix
        self._open = threading.Event()
        self.entered = threading.Event()
        self.passed = threading.Event()

    def __call__(self, connection, text):
        if not text.startswith(self._prefix) or self.entered.is_set():
            return
        self.entered.set()
        self._controller.hold(self._open, connection)
        self.passed.set()

    def open(self):
        self._open.set()


class Feed:
    """Feedback connection fake. It reports the simulated state every few milliseconds."""

    def __init__(self, controller):
        self.controller = controller
        self.reads = 0

    def __call__(self, host, timeout_s):
        assert host == HOST
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read_latest(self):
        time.sleep(0.003)
        self.reads += 1
        return self.controller.advance()


def arrive(request):
    return {"state": 1, "position": request["position"]}


def held_until(event):
    """Answer gripper requests only after the test sets the event."""
    return lambda request: arrive(request) if event.is_set() else None


class Collector:
    """Simulate the USB collector: publish gripper feedback and answer position commands."""

    def __init__(self, directory):
        self.feedback_path = directory / "gripper.json"
        self.command_path = directory / "gripper.json.command.json"
        self._scratch = directory / "collector.tmp"
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
        self._thread = threading.Thread(target=self._run, name="test-collector", daemon=True)

    def start(self):
        self._emit()
        self._thread.start()

    def close(self):
        self._stop.set()
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

    def _emit(self):
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
        try:
            with open(self.command_path) as file:
                request = json.load(file)
        except FileNotFoundError:
            return
        with self._lock:
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
                operation="position",
                target=request["position"],
                ack=True,
            )
            self._sample.update(changes)

    def answered_count(self):
        with self._lock:
            return len(self._answered)


class Rig:
    def __init__(self, controller, state, passes, feed, collector=None, gripper=None):
        self.controller = controller
        self.state = state
        self.collector = collector
        self.gripper = gripper
        self._passes = passes
        self._feed = feed
        self._sequence = itertools.count(1)

    def snapshot(self):
        return self.state.snapshot()

    def describe(self):
        return f"snapshot={self.snapshot()!r} sent={self.controller.sent()[-12:]!r}"

    def wait(self, condition, timeout=3):
        wait_for(lambda: condition(self.snapshot()), timeout=timeout, describe=self.describe)

    def wait_idle(self, message):
        self.wait(lambda snapshot: snapshot["message"] == message and not snapshot["busy"])

    def passes(self, loop):
        return self._passes[loop, id(self.state)]

    def wait_passes(self, *, worker=0, watchdog=0):
        """Wait until the panel worker and the watchdog finish more passes."""
        first_worker, first_watchdog = self.passes("worker"), self.passes("watchdog")
        wait_for(
            lambda: (
                self.passes("worker") >= first_worker + worker
                and self.passes("watchdog") >= first_watchdog + watchdog
            ),
            describe=self.describe,
        )

    def wait_reads(self, count):
        """Wait until the panel reads more feedback samples. This works also during close()."""
        first = self._feed.reads
        wait_for(lambda: self._feed.reads >= first + count, describe=self.describe)

    def command(self, action):
        return self.state.command({"action": action}, CLIENT)

    def keys(self, *held):
        return self.state.keys({"seq": next(self._sequence), "held": list(held)}, CLIENT)

    def arm(self, speed=1):
        payload = {"action": "keyboard", "enabled": True, "speed": speed}
        return self.state.command({**payload, "seq": next(self._sequence)}, CLIENT)

    def disarm(self, reason):
        payload = {"action": "keyboard", "enabled": False, "reason": reason}
        return self.state.command({**payload, "seq": next(self._sequence)}, CLIENT)

    def connect(self):
        self.command("start_sync")
        self.wait(lambda snapshot: snapshot["feedback_fresh"] and snapshot["robot_mode"] == 5)

    def arm_ready(self, speed=1):
        self.arm(speed)
        self.wait(lambda snapshot: snapshot["keyboard_enabled"])
        assert self.snapshot()["message"] == READY

    def hold(self, key):
        """Press a jog key and wait until the controller reports the continuous jog."""
        self.keys(key)
        self.wait(lambda snapshot: snapshot["robot_mode"] == 11)
        assert JOGS[key] in self.controller.sent()


def rig_factory(monkeypatch, tmp_path):
    """Yield a factory of rigs that share one simulated controller. Close them afterwards."""
    # The key and browser leases are not under test here. Long leases keep a slow run
    # from turning keys off or stopping the robot in the middle of a scenario.
    monkeypatch.setattr(RobotPanelState, "KEY_LEASE_SECONDS", 30.0)
    monkeypatch.setattr(RobotPanelState, "LEASE_SECONDS", 30.0)
    monkeypatch.setattr(JointControl, "_wait_stable", short_wait_stable)
    # Count finished passes of the panel worker and watchdog. The original code runs unchanged.
    passes = collections.Counter()
    watch_once, reconcile = RobotPanelState._watch_once, RobotPanelState._kb_reconcile

    def counted_watch_once(state):
        try:
            return watch_once(state)
        finally:
            passes["watchdog", id(state)] += 1

    def counted_reconcile(state):
        try:
            return reconcile(state)
        finally:
            if threading.current_thread() is state._worker:
                passes["worker", id(state)] += 1

    monkeypatch.setattr(RobotPanelState, "_watch_once", counted_watch_once)
    monkeypatch.setattr(RobotPanelState, "_kb_reconcile", counted_reconcile)
    controller = Controller()
    controller.install(monkeypatch)
    rigs, collectors = [], []

    def make(*, gripper=False):
        collector = adapter = None
        if gripper:
            directory = tmp_path / "collector"
            directory.mkdir()
            collector = Collector(directory)
            collectors.append(collector)
            collector.start()
            adapter = WebGripper(collector.feedback_path)
        feed = Feed(controller)
        state = RobotPanelState(HOST, gripper=adapter, connection_factory=feed)
        rig = Rig(controller, state, passes, feed, collector, adapter)
        rigs.append(rig)
        return rig

    try:
        yield make
    finally:
        # Open every gate and end every slow stop, so that close() never waits for a fake.
        controller.closed.set()
        controller.slow_jog_stop = False
        if controller.sample.mode == 11:
            controller.update(mode=5)
        for collector in collectors:
            collector.answer = arrive
        try:
            for rig in rigs:
                rig.state.close()
        finally:
            for collector in collectors:
                collector.close()
    assert controller.unexpected == []
