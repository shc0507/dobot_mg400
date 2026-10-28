"""Keyboard control through RobotPanelState with the real keyboard stack.

The real KeyboardJogSession and JointControl talk to a simulated MG400 through
socket fakes. The real WebGripper talks to a simulated USB collector through
temporary files. No test opens a network connection.
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
from pathlib import Path

import pytest

from mg400_agent.controller_ownership import ControllerOwnership
from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl, _StationaryTimeoutError
from mg400_agent.joint_limits import validate_joints
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
PREPARATION = ("RobotMode(", "GetErrorID(", "GetAngle(", "SpeedFactor(")
READY = "Keyboard ready. Hold one key; release requests a stop."
NEXT = "Stopped. Hold a key for the next move."
STOPPED = "Robot stop is confirmed. Disable the robot before recovery."
DISABLED = "Robot disable and rest are confirmed."
FOCUS = "Keyboard off: window lost focus."
CONTACT = (
    "The gripper reports object contact before the target. Release the key before pressing again."
)
HOLD_LIMIT = "Hold limit reached. Release the key before pressing again."
REAL_WAIT_STABLE = JointControl._wait_stable


def wait_for(predicate, timeout=3, describe=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), describe() if describe else "The condition was not reached"


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
        wait_for(lambda: condition(self.snapshot()), timeout, self.describe)

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


def before_command(monkeypatch, prefix, action):
    """Run action once on the calling worker, just before a guarded command is registered."""
    original = JointControl._guarded_command
    done = threading.Event()

    def guarded(control, text, **kwargs):
        if text.startswith(prefix) and not done.is_set():
            done.set()
            action()
        return original(control, text, **kwargs)

    monkeypatch.setattr(JointControl, "_guarded_command", guarded)
    return done


def before_speed_factor_send(rig, action):
    """Run action once when the registered SpeedFactor opens its connection, before the send."""
    captured = {}

    def opened(port):
        pending = rig.state._control._pending
        if (
            port == 29999
            and not captured
            and pending is not None
            and pending["command"].startswith("SpeedFactor(")
            and not pending["done"].is_set()
        ):
            captured.update(pending=pending, sent=pending["sent"])
            action()

    rig.controller.connect_hooks.append(opened)
    return captured


@pytest.fixture(autouse=True)
def isolated_controller_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture
def make_rig(monkeypatch, tmp_path):
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


@pytest.fixture
def rig(make_rig):
    return make_rig()


@pytest.fixture
def gripper_rig(make_rig):
    rig = make_rig(gripper=True)
    rig.connect()
    rig.arm_ready()
    rig.wait(lambda snapshot: snapshot["actual"][4] == 500)
    return rig


def test_released_continuous_hold_sends_one_jog_and_a_verified_jog_stop(rig):
    rig.connect()
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == ["SpeedFactor(25)"]
    assert rig.snapshot()["speed_factor"] == 25
    rig.hold("A")
    # Mode 11 lasts several worker and watchdog passes. Neither one stops the hold.
    rig.wait_passes(worker=2, watchdog=3)
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 11 and snapshot["keyboard_active_key"] == "A"
    assert snapshot["busy"] and not snapshot["locked"]
    assert snapshot["actual"][:4] == [None] * 4
    rig.controller.slow_jog_stop = True
    rig.keys()
    wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
    # The jog still slows down. Mode 11 is accepted while the session verifies its stop.
    rig.wait_passes(watchdog=3)
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 11 and snapshot["busy"] and not snapshot["locked"]
    rig.controller.rest()
    rig.wait_idle(NEXT)
    assert rig.controller.mutations() == ["SpeedFactor(25)", "MoveJog(J1+)", "MoveJog()"]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert snapshot["robot_mode"] == 5 and not snapshot["stop_unconfirmed"]
    assert not rig.state._control.stopped
    assert rig.state._control.feedback_retention_reason() is None


@pytest.mark.parametrize("key", ["A", "W"])
def test_stop_during_continuous_hold_stops_the_jog_before_the_core_stop(rig, key):
    rig.connect()
    rig.arm_ready(speed=25)
    rig.hold(key)
    rig.controller.slow_jog_stop = True
    rig.command("stop")
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["keyboard_enabled"]
    # The session sends its own jog stop at once.
    wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
    # The core stop waits while the jog slows down and the session verifies its stop.
    rig.wait_passes(watchdog=3)
    assert "ResetRobot()" not in rig.controller.sent()
    rig.controller.rest()
    rig.wait_idle(STOPPED)
    jog = JOGS[key]
    frame = [RADIAL_FRAME] if key == "W" else []
    expected = ["SpeedFactor(25)", *frame, jog, "MoveJog()", "ResetRobot()"]
    assert rig.controller.mutations() == expected
    assert rig.controller.position("note", "rest") < rig.controller.position("send", "ResetRobot()")
    # The session confirmed its stop, so the parent keeps no unconfirmed operation stop.
    assert rig.state._control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and snapshot["robot_mode"] == 5

    rig.command("disable")
    rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
    assert rig.controller.motion() == [jog, "MoveJog()", "ResetRobot()", "DisableRobot()"]
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 4 and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]


def test_disable_during_continuous_hold_stops_the_jog_before_disable_and_recovers(rig):
    rig.connect()
    rig.arm_ready(speed=25)
    rig.hold("A")
    held_control = rig.state._control
    rig.controller.slow_jog_stop = True
    rig.command("disable")
    assert not rig.snapshot()["keyboard_enabled"]
    wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
    rig.wait_passes(watchdog=3)
    assert "DisableRobot()" not in rig.controller.sent()
    rig.controller.rest()
    rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
    assert rig.controller.mutations() == [
        "SpeedFactor(25)",
        "MoveJog(J1+)",
        "MoveJog()",
        "DisableRobot()",
    ]
    assert rig.controller.position("note", "rest") < rig.controller.position(
        "send", "DisableRobot()"
    )
    assert held_control.feedback_retention_reason() is None
    assert rig.state._control is not held_control
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 4 and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]


def test_close_during_continuous_hold_stops_the_jog_before_the_core_stop(rig):
    rig.connect()
    rig.arm_ready(speed=25)
    rig.hold("A")
    rig.controller.slow_jog_stop = True
    contender = ControllerOwnership(HOST)
    closer = threading.Thread(target=rig.state.close, name="test-close", daemon=True)
    closer.start()
    try:
        wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
        # The panel loops end at close. Feedback still arrives while the jog slows down.
        rig.wait_reads(30)
        assert "ResetRobot()" not in rig.controller.sent()
        assert closer.is_alive()
        # The panel keeps the controller until the stop is verified.
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        rig.controller.rest()
    finally:
        contender.release()
        rig.controller.slow_jog_stop = False
        if rig.controller.sample.mode == 11:
            rig.controller.update(mode=5)
        closer.join(5)
    assert not closer.is_alive()
    assert rig.controller.mutations() == [
        "SpeedFactor(25)",
        "MoveJog(J1+)",
        "MoveJog()",
        "ResetRobot()",
    ]
    assert rig.controller.position("note", "rest") < rig.controller.position("send", "ResetRobot()")
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["syncing"] and not snapshot["busy"] and not snapshot["keyboard_enabled"]
    # The confirmed stop lets close() release the controller for another panel.
    contender.acquire()
    contender.release()


def test_unconfirmed_speed_factor_becomes_a_verified_stop_that_disable_recovers(rig):
    rig.connect()
    rig.controller.ignore_speed_factor = True
    rig.arm(speed=25)
    # The session faults and cancels the parent. The panel turns this into a verified stop.
    rig.wait_idle(STOPPED)
    assert rig.controller.mutations() == ["SpeedFactor(25)", "ResetRobot()"]
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and not snapshot["keyboard_arming"]
    assert snapshot["speed_factor"] == 100
    assert rig.state._control.feedback_retention_reason() is None
    rig.command("disable")
    rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
    # The new control enables the robot, and keyboard control works again.
    rig.controller.ignore_speed_factor = False
    rig.command("enable")
    rig.wait(
        lambda s: (
            s["message"] == "Robot operation completed." and s["robot_mode"] == 5 and not s["busy"]
        )
    )
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == [
        "SpeedFactor(25)",
        "ResetRobot()",
        "DisableRobot()",
        "EnableRobot()",
        "SpeedFactor(25)",
    ]


def test_joint_limit_keeps_keys_on_and_a_new_direction_starts_with_an_inward_step(rig):
    # J1 starts 0.15 degrees inside the target limit and turns while A is held.
    rig.controller.update(joints_deg=(158.8, *START[1:]))
    rig.controller.jog_rate = 0.005
    rig.connect()
    rig.arm_ready()
    rig.keys("A")
    rig.wait(
        lambda s: (
            s["message"].endswith("Release the key and choose another direction.") and not s["busy"]
        )
    )
    snapshot = rig.snapshot()
    assert snapshot["message"].startswith("J1=")
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    limit = rig.controller.sample.joints_deg[0]
    assert 158.95 < limit < 160
    assert rig.controller.mutations() == ["SpeedFactor(1)", "MoveJog(J1+)", "MoveJog()"]
    # After the release, the other direction first moves a short checked step inward.
    rig.keys()
    rig.keys("D")
    rig.wait(lambda s: s["robot_mode"] == 11)
    rig.keys()
    rig.wait_idle(NEXT)
    mutations = rig.controller.mutations()
    assert mutations[:3] == ["SpeedFactor(1)", "MoveJog(J1+)", "MoveJog()"]
    assert mutations[4:] == ["MoveJog(J1-)", "MoveJog()"]
    assert mutations[3].startswith("JointMovJ(") and mutations[3].endswith(",SpeedJ=5,AccJ=20)")
    target = tuple(float(value) for value in mutations[3][len("JointMovJ(") :].split(",")[:4])
    assert validate_joints(target) and target[0] < limit
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not rig.state._control.stopped


@pytest.mark.parametrize("phase", ["pose_poll", "preflight", "registration", "send"])
def test_disarm_during_preparation_sends_no_speed_factor_and_leaves_the_parent_clean(
    rig, monkeypatch, phase
):
    rig.connect()
    gate = speed_factor = None
    if phase == "pose_poll":
        gate = rig.controller.block("GetPose(0,0)")
        assert gate.entered.wait(2)
    elif phase == "preflight":
        gate = rig.controller.block("GetAngle()")
    elif phase == "registration":
        before_command(monkeypatch, "SpeedFactor(", lambda: rig.disarm("focus"))
    else:
        speed_factor = before_speed_factor_send(rig, lambda: rig.disarm("focus"))
    armed = rig.controller.mark()
    rig.arm(speed=25)
    if phase == "pose_poll":
        rig.disarm("focus")
        # The preparation ends at once. It does not wait for the pose query.
        rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
        assert not gate.passed.is_set()
        gate.open()
        assert not any(text.startswith(PREPARATION) for text in rig.controller.sent(armed))
    elif phase == "preflight":
        assert gate.entered.wait(2)
        rig.disarm("focus")
        # The disarm cuts the preflight short. A later preparation query would stay here.
        pose = rig.controller.block("GetPose(0,0)")
        gate.open()
        rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
        pose.open()
    rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
    if speed_factor is not None:
        # The disarm came after the registration: the send-time guard refused it.
        pending = speed_factor["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    assert not any(text.startswith("SpeedFactor(") for text in rig.controller.sent())
    assert rig.controller.motion() == []
    control = rig.state._control
    assert not control.stopped and control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["message"] == FOCUS
    assert not snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    # The parent control stays usable: the next arming prepares and confirms the speed.
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == ["SpeedFactor(25)"]


def test_disarm_while_speed_factor_is_pending_lets_it_finish_without_uncertainty(rig):
    rig.connect()
    gate = rig.controller.block("SpeedFactor(")
    rig.arm(speed=25)
    assert gate.entered.wait(2)
    rig.disarm("focus")
    gate.open()
    rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
    assert rig.controller.mutations() == ["SpeedFactor(25)"]
    control = rig.state._control
    assert not control.stopped and control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["message"] == FOCUS and snapshot["speed_factor"] == 25
    assert not snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == ["SpeedFactor(25)", "SpeedFactor(25)"]


@pytest.mark.parametrize(
    "phase", ["pose_poll", "preflight", "registration", "send", "acknowledgement"]
)
def test_stop_during_preparation_sends_no_speed_factor_after_the_lock(rig, monkeypatch, phase):
    rig.connect()
    locked_at = []

    def stop():
        locked_at.append(rig.controller.mark())
        rig.command("stop")

    gate = speed_factor = None
    if phase == "registration":
        before_command(monkeypatch, "SpeedFactor(", stop)
    elif phase == "send":
        speed_factor = before_speed_factor_send(rig, stop)
    else:
        prefix = {
            "pose_poll": "GetPose(0,0)",
            "preflight": "GetAngle()",
            "acknowledgement": "SpeedFactor(",
        }[phase]
        gate = rig.controller.block(prefix)
        if phase == "pose_poll":
            assert gate.entered.wait(2)
    armed = rig.controller.mark()
    rig.arm(speed=25)
    if gate is not None:
        assert gate.entered.wait(2)
        stop()
        snapshot = rig.snapshot()
        assert snapshot["locked"] and not snapshot["keyboard_arming"]
        if phase != "pose_poll":
            # The stop waits for the query or acknowledgement in flight.
            gate.open()
    rig.wait_idle(STOPPED)
    if gate is not None:
        gate.open()
    assert not any(text.startswith("SpeedFactor(") for text in rig.controller.sent(locked_at[0]))
    assert rig.controller.motion() == ["ResetRobot()"]
    if phase == "acknowledgement":
        assert rig.controller.mutations() == ["SpeedFactor(25)", "ResetRobot()"]
        assert rig.controller.position("ack", "SpeedFactor(25)") < rig.controller.position(
            "send", "ResetRobot()"
        )
    else:
        assert rig.controller.mutations() == ["ResetRobot()"]
    if phase == "pose_poll":
        # The preparation never started, and the stop did not wait for the pose query.
        assert not any(text.startswith(PREPARATION) for text in rig.controller.sent(armed))
        assert not gate.passed.is_set()
    if phase == "preflight":
        assert rig.controller.position("ack", "GetAngle()") < rig.controller.position(
            "send", "ResetRobot()"
        )
        # The stop cut the preflight short: no pose query followed the lock.
        assert "GetPose(0,0)" not in rig.controller.sent(locked_at[0])
    if speed_factor is not None:
        pending = speed_factor["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    assert rig.state._control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and not snapshot["keyboard_arming"]


@pytest.mark.parametrize("point", ["enqueue", "send"])
@pytest.mark.parametrize("revocation", ["stop", "lease"])
def test_revocation_before_a_jog_is_sent_sends_nothing(rig, monkeypatch, point, revocation):
    rig.connect()
    rig.arm_ready()
    captured = {}

    def revoke():
        if revocation == "stop":
            rig.command("stop")
        else:
            # The browser lease lapses between two watchdog passes.
            with rig.state._lock:
                rig.state._heartbeat -= rig.state.LEASE_SECONDS + 1

    def opened(port):
        if port != 30003 or captured:
            return
        # The jog is registered and its connection opens. Nothing was sent yet.
        pending = rig.state._control._pending
        captured.update(pending=pending, command=pending["command"], sent=pending["sent"])
        revoke()

    if point == "enqueue":
        before_command(monkeypatch, "MoveJog(J1+)", revoke)
    else:
        rig.controller.connect_hooks.append(opened)
    rig.keys("A")
    rig.wait_idle(STOPPED)
    if point == "enqueue":
        # The guards refused the registration, so the motion connection never opened.
        assert 30003 not in rig.controller.ports()
    else:
        assert captured["command"] == "MoveJog(J1+)" and captured["sent"] is False
        pending = captured["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    assert not any(text.startswith("MoveJog(") for text in rig.controller.sent())
    motion = rig.controller.motion()
    assert motion and set(motion) == {"ResetRobot()"}
    assert rig.state._control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]


def test_z_steps_open_by_25_from_each_measured_position_until_release(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    third = threading.Event()

    def answer(request):
        if collector.answered_count() == 2 and not third.is_set():
            return None
        # Arrive 2 / 1000 short of the target, inside the arrival tolerance.
        return {"state": 1, "position": request["position"] - 2}

    collector.answer = answer
    rig.keys("Z")
    wait_for(lambda: len(collector.targets("published")) == 3, describe=rig.describe)
    rig.keys()
    third.set()
    rig.wait_idle(NEXT)
    assert collector.targets("published") == [525, 548, 571]
    assert collector.targets("answered") == [525, 548, 571]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not snapshot["gripper_busy"]
    rig.wait(lambda s: s["actual"][4] == 569)
    assert not rig.gripper.failed and rig.gripper.last_outcome == "arrived"
    assert rig.controller.mutations() == ["SpeedFactor(1)"]


def test_x_at_the_closed_end_sends_nothing_and_z_from_it_opens(make_rig):
    rig = make_rig(gripper=True)
    rig.collector.set(position=0)
    rig.connect()
    rig.arm_ready()
    rig.wait(lambda snapshot: snapshot["actual"][4] == 0)
    rig.keys("X")
    rig.wait_idle(NEXT)
    assert rig.collector.targets("published") == []
    assert not rig.collector.command_path.exists()
    assert rig.snapshot()["keyboard_enabled"]
    rig.keys()
    allow = threading.Event()
    rig.collector.answer = held_until(allow)
    rig.keys("Z")
    wait_for(lambda: rig.collector.targets("published") == [25], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: rig.collector.targets("answered") == [25], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.collector.targets("published") == [25]
    assert rig.snapshot()["keyboard_enabled"]
    rig.wait(lambda s: s["actual"][4] == 25)
    assert not rig.gripper.failed
    assert rig.controller.mutations() == ["SpeedFactor(1)"]


@pytest.mark.parametrize("cause", ["mode", "stop"])
def test_publication_guard_refusal_publishes_nothing_and_leaves_the_gripper_unlocked(
    gripper_rig, monkeypatch, cause
):
    rig, collector, controller = gripper_rig, gripper_rig.collector, gripper_rig.controller
    write_text, unlink = Path.write_text, Path.unlink
    prepared = threading.Event()

    def prepare_request(path, *args, **kwargs):
        result = write_text(path, *args, **kwargs)
        if path.name.endswith(".command.json.tmp") and not prepared.is_set():
            prepared.set()
            if cause == "mode":
                # The robot reports motion while the step waits for its publication.
                controller.update(mode=7)
                wait_for(lambda: rig.snapshot()["robot_mode"] == 7)
            else:
                rig.command("stop")
        return result

    def remove_request(path, *args, **kwargs):
        if path.name.endswith(".command.json.tmp") and controller.sample.mode == 7:
            # The robot is idle again before the step reports its result.
            controller.update(mode=5)
            wait_for(lambda: rig.snapshot()["robot_mode"] == 5)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", prepare_request)
    monkeypatch.setattr(Path, "unlink", remove_request)
    rig.keys("Z")
    if cause == "mode":
        rig.wait(lambda s: s["message"].startswith("Gripper unavailable:") and not s["busy"])
    else:
        rig.wait(lambda s: s["message"].startswith(STOPPED) and not s["busy"])
    assert prepared.is_set()
    assert collector.targets("published") == []
    assert not collector.command_path.exists()
    assert not rig.gripper.failed and not rig.gripper.snapshot()["locked"]
    snapshot = rig.snapshot()
    assert not snapshot["stop_unconfirmed"]
    if cause == "stop":
        # The stop turns keys off and does not wait for the gripper step.
        assert not snapshot["keyboard_enabled"] and snapshot["locked"]
        assert "Gripper unavailable" not in snapshot["message"]
        assert controller.motion() == ["ResetRobot()"]
        return
    assert snapshot["message"].startswith("Gripper unavailable: No command sent.")
    assert "idle" in snapshot["message"]
    assert snapshot["message"].endswith("Arm keys remain ready.")
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert controller.mutations() == ["SpeedFactor(1)"]
    # The keys still work: a new press publishes one step.
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys()
    rig.keys("Z")
    wait_for(lambda: collector.targets("published") == [525], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: collector.targets("answered") == [525], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed


def test_replace_failure_latches_the_gripper_and_turns_keys_off_without_stopping_the_arm(
    gripper_rig, monkeypatch
):
    rig = gripper_rig
    replace_path = Path.replace

    def fail_publication(path, target):
        if path.name.endswith(".command.json.tmp"):
            raise OSError("Disk failure.")
        return replace_path(path, target)

    monkeypatch.setattr(Path, "replace", fail_publication)
    rig.keys("X")
    rig.wait(lambda s: s["message"].startswith("ERROR:") and not s["busy"])
    snapshot = rig.snapshot()
    assert snapshot["message"] == "ERROR: Control is locked. Check the gripper. Disk failure."
    assert not snapshot["keyboard_enabled"]
    assert rig.gripper.failed and rig.gripper.snapshot()["locked"]
    # The gripper latch shows as locked. The arm itself is not stopped or locked.
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert snapshot["robot_mode"] == 5
    assert not rig.state._locked and not rig.state._control.stopped
    rig.wait_passes(worker=1, watchdog=2)
    assert rig.controller.motion() == []
    assert rig.controller.mutations() == ["SpeedFactor(1)"]
    assert rig.collector.targets("published") == []
    # The latched gripper blocks keyboard arming.
    with pytest.raises(RuntimeError, match="Gripper control is locked"):
        rig.arm()


def test_collector_lock_turns_keys_off_without_stopping_the_arm(gripper_rig):
    rig = gripper_rig
    rig.collector.lock("Write reply lost.")
    rig.wait(lambda s: not s["keyboard_enabled"])
    snapshot = rig.snapshot()
    assert snapshot["message"] == "Keyboard off: gripper control is locked."
    assert snapshot["keyboard_disarm_reason"] == "gripper control is locked."
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"] and not snapshot["busy"]
    rig.wait_passes(worker=1, watchdog=2)
    assert not rig.state._locked and not rig.state._control.stopped
    assert rig.controller.motion() == []
    with pytest.raises(RuntimeError, match="Gripper control is locked"):
        rig.arm()


def test_collector_lock_during_a_step_keeps_the_keyboard_off_reason(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    write_text = Path.write_text
    prepared = threading.Event()

    def prepare_request(path, *args, **kwargs):
        result = write_text(path, *args, **kwargs)
        if path.name.endswith(".command.json.tmp") and not prepared.is_set():
            prepared.set()
            # The collector locks while the step prepares its request. Keys turn off.
            collector.lock("Write reply lost.")
            wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
        return result

    monkeypatch.setattr(Path, "write_text", prepare_request)
    rig.keys("Z")
    wait_for(prepared.is_set, describe=rig.describe)
    rig.wait(lambda s: not s["busy"] and not s["gripper_busy"])
    snapshot = rig.snapshot()
    # The adapter refused the locked collector before publication: no command, no latch.
    assert collector.targets("published") == [] and not collector.command_path.exists()
    assert not rig.gripper.failed
    assert not snapshot["keyboard_enabled"] and not snapshot["stop_unconfirmed"]
    assert rig.controller.motion() == []
    # Keys were off before the step ended, so the step result sets no message (spec §8).
    assert snapshot["keyboard_disarm_reason"] == "gripper control is locked."
    assert snapshot["message"] == "Keyboard off: gripper control is locked."


def test_contact_keeps_keys_on_and_needs_a_new_press_for_the_next_step(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    collector.answer = lambda request: {"state": 2, "position": 490}
    rig.keys("X")
    rig.wait_idle(CONTACT)
    assert collector.targets("published") == [475]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert rig.gripper.last_outcome == "contact" and not rig.gripper.failed
    # A renewal of the held key starts nothing, also in later worker passes.
    rig.keys("X")
    rig.wait_passes(worker=2)
    assert collector.targets("published") == [475]
    snapshot = rig.snapshot()
    assert not snapshot["busy"] and snapshot["message"] == CONTACT
    # Release and press again: one new step from the measured contact position.
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys()
    rig.keys("X")
    wait_for(lambda: collector.targets("published") == [475, 465], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: collector.targets("answered") == [475, 465], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed


def test_gripper_hold_limit_keeps_keys_on_and_needs_a_new_press(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    monkeypatch.setattr(RobotPanelState, "GRIPPER_HOLD_SECONDS", 0.15)
    rig.keys("Z")
    rig.wait_idle(HOLD_LIMIT)
    targets = collector.targets("published")
    assert targets == [525 + 25 * index for index in range(len(targets))]
    assert targets and collector.targets("answered") == targets
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed
    rig.keys("Z")
    rig.wait_passes(worker=2)
    assert collector.targets("published") == targets
    assert rig.snapshot()["message"] == HOLD_LIMIT


def test_stop_during_gripper_step_does_not_wait_for_the_step(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys("Z")
    wait_for(lambda: collector.targets("published") == [525], describe=rig.describe)
    try:
        rig.command("stop")
        wait_for(lambda: "ResetRobot()" in rig.controller.sent(), describe=rig.describe)
        # The core stop went out while the gripper step still waits for its result.
        assert collector.targets("answered") == []
        assert rig.snapshot()["gripper_busy"]
    finally:
        allow.set()
    rig.wait(lambda s: s["message"].startswith(STOPPED) and not s["busy"])
    assert collector.targets("answered") == [525]
    assert not rig.gripper.failed and not rig.gripper.snapshot()["locked"]
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]
    assert rig.controller.motion() == ["ResetRobot()"]


def test_close_during_gripper_step_closes_the_adapter_only_after_the_step(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys("Z")
    wait_for(lambda: collector.targets("published") == [525], describe=rig.describe)
    closer = threading.Thread(target=rig.state.close, name="test-close", daemon=True)
    try:
        closer.start()
        wait_for(lambda: "ResetRobot()" in rig.controller.sent(), describe=rig.describe)
        # close() waits for the step before it closes the adapter.
        assert closer.is_alive() and not rig.gripper.closed
    finally:
        allow.set()
    closer.join(5)
    assert not closer.is_alive()
    assert rig.gripper.closed and not rig.gripper.failed
    assert collector.targets("answered") == [525]
    assert rig.controller.motion() == ["ResetRobot()"]


def test_continuous_hold_does_not_leave_the_pre_hold_pose_visible(rig):
    rig.connect()
    rig.wait(lambda snapshot: snapshot["actual"][:4] == list(POSE))
    rig.arm_ready()
    rig.hold("A")
    # Block the pose query after the hold, so only the hold decides what the panel shows.
    gate = rig.controller.block("GetPose(0,0)")
    rig.keys()
    rig.wait_idle(NEXT)
    try:
        assert gate.entered.wait(2)
        assert rig.snapshot()["actual"][:4] == [None] * 4
    finally:
        gate.open()


def test_release_before_publication_sends_no_gripper_step(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    move = WebGripper.move
    released = []

    def release_then_move(gripper, position):
        # The key-up arrives after the step checked its release and before it publishes.
        if not released:
            released.append(rig.keys())
        return move(gripper, position)

    monkeypatch.setattr(WebGripper, "move", release_then_move)
    rig.keys("X")
    rig.wait_idle(NEXT)
    assert released
    assert collector.targets("published") == []
    assert not collector.command_path.exists()
    assert not rig.gripper.failed
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert snapshot["actual"][4] == 500
