"""Safety order between keyboard workers and stop, disable, disconnect, and close.

The fakes record one global call log, so each test can check the order of the
session release, the hold exit, the parent cancel, and the stop or disable.
"""

import contextlib
import threading
import time

import pytest
from helpers import Feed, block_network, idle_lock, wait_for

from mg400_agent import web_control
from mg400_agent.controller_ownership import ControllerOwnership
from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import PreparationRejectedError
from mg400_agent.web_control import RobotPanelState
from mg400_agent.web_gripper import GripperNotSentError

OWNER = "browser-a"
OTHER = "browser-b"
JOGGING = Feedback((1, 2, 3, 4), 11, 0, False)

pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


def stays(predicate, duration=0.1):
    """Check that a condition stays true for a short window. Use only for negative checks."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        assert predicate()
        time.sleep(0.005)
    assert predicate()


def gate(event, timeout=5):
    """Block a fake until the test opens its gate."""
    if not event.wait(timeout):
        raise TimeoutError("The test did not open the gate")


def gate_any(*events, timeout=5):
    deadline = time.monotonic() + timeout
    while not any(event.is_set() for event in events):
        if time.monotonic() > deadline:
            raise TimeoutError("The test did not open the gate")
        time.sleep(0.002)


def acquired(ownership):
    try:
        ownership.acquire()
    except RuntimeError:
        return False
    return True


class Log:
    """Record calls from all threads in one order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []

    def add(self, entry):
        with self._lock:
            self._entries.append(entry)

    @property
    def entries(self):
        with self._lock:
            return list(self._entries)

    def __contains__(self, entry):
        return entry in self.entries

    def count(self, entry):
        return self.entries.count(entry)

    def assert_order(self, *expected):
        """Compare the first occurrence of each entry."""
        entries = self.entries
        for entry in expected:
            assert entry in entries, f"{entry!r} is missing from {entries}"
        positions = [entries.index(entry) for entry in expected]
        assert positions == sorted(set(positions)), f"Expected {expected} in {entries}"


class Control:
    """Parent control. Records cancel, stop, disable, and rest checks."""

    def __init__(self, rig, host, feedback_snapshot, *, send_guard):
        assert host == "robot.test"
        self.name = f"control{len(rig.controls) + 1}"
        self._log = rig.log
        self.feedback = feedback_snapshot
        self.send_guard = send_guard
        self.stopped = False
        self.command_pending = False
        self.retention = None
        self.stop_ok = True
        self.stop_entered = threading.Event()
        self.stop_gate = threading.Event()
        self.stop_gate.set()
        self.hold_polls = threading.Event()
        self.poll_blocked = threading.Event()
        self.poll_release = threading.Event()
        self.polls = 0
        self.on_cancel = None
        self.on_stop = None

    def log(self, event):
        self._log.add(f"{self.name}.{event}")

    def cancel(self, *, disabling=False):
        if self.on_cancel is not None:
            self.on_cancel()
        self.log("cancel:disable" if disabling else "cancel")
        self.stopped = True

    def stop(self):
        if self.on_stop is not None:
            self.on_stop()
        self.log("stop")
        self.stop_entered.set()
        gate(self.stop_gate)
        return {"ok": self.stop_ok, "error": "Robot stop was not confirmed"}

    def disable(self):
        self.log("disable")

    def verify_stationary(self):
        self.log("stationary")

    def enable(self):
        self.send_guard()
        self.log("enable")

    def feedback_retention_reason(self):
        return self.retention

    def read_pose(self):
        self.polls += 1
        if self.hold_polls.is_set():
            self.poll_blocked.set()
            gate(self.poll_release)
        return (250.0, 0.0, 30.0, 0.0)

    def read_state(self):
        return self.feedback()[0]

    def open_gates(self):
        self.stop_gate.set()
        self.hold_polls.clear()
        self.poll_release.set()


class Session:
    """Keyboard session: prepare once, then one hold that runs until release."""

    def __init__(self, rig, control, speed_percent):
        self.name = f"session{len(rig.sessions) + 1}"
        self._log = rig.log
        self.control = control
        self.speed_percent = speed_percent
        self.fault = None
        self.limit_reason = None
        self.prepared = False
        self.running = False
        self.closed = threading.Event()
        self.keys = []
        self.release_error = None
        self.ignore_release = False
        self.fault_on_prepare = None
        self.fault_on_run = None
        self.error_on_run = None
        self.prepare_entered = threading.Event()
        self.prepare_gate = threading.Event()
        self.prepare_gate.set()
        self.abort_gate = threading.Event()
        self.abort_gate.set()
        self.run_entered = threading.Event()
        self.exit_gate = threading.Event()
        self.exit_gate.set()
        self.finish_gate = threading.Event()
        self._released = None

    def log(self, event):
        self._log.add(f"{self.name}.{event}")

    @property
    def ready(self):
        return (
            self.prepared
            and not self.running
            and not self.closed.is_set()
            and self.fault is None
            and not self.control.stopped
        )

    def prepare(self):
        self.log("prepare")
        self.prepare_entered.set()
        try:
            if self.fault_on_prepare is not None:
                self.fault = self.fault_on_prepare
                self.control.cancel()
                raise RuntimeError(self.fault)
            gate_any(self.prepare_gate, self.closed)
            if self.closed.is_set():
                # close() cuts the preflight short after its current query.
                gate(self.abort_gate)
                raise RuntimeError("Keyboard preparation cancelled")
            self.prepared = True
            return (250.0, 0.0, 30.0, 0.0)
        finally:
            self.log("prepare.exit")

    def run(self, key, released):
        self.log(f"run:{key}")
        self.keys.append(key)
        self.running = True
        self._released = released
        self.run_entered.set()
        try:
            if self.fault_on_run is not None:
                # An unconfirmed child stop faults the session and cancels the parent.
                self.fault = self.fault_on_run
                self.control.cancel()
                raise RuntimeError(self.fault)
            if self.error_on_run is not None:
                raise self.error_on_run
            if self.ignore_release:
                gate(self.finish_gate)
            else:
                gate(released)
                self.log("run.released")
                # The session sends its own jog stop and verifies it before it returns.
                gate(self.exit_gate)
            if self.control.stopped:
                raise RuntimeError("Parent control stopped during keyboard movement")
            return None
        finally:
            self.running = False
            self.log("run.exit")

    def release(self):
        self.log("release")
        if self.release_error is not None:
            raise self.release_error
        if self._released is not None:
            self._released.set()

    def close(self):
        self.log("close")
        self.closed.set()
        if self.release_error is None and self._released is not None:
            self._released.set()

    def open_gates(self):
        for event in (self.prepare_gate, self.abort_gate, self.exit_gate, self.finish_gate):
            event.set()


class Gripper:
    """Gripper adapter with the keyboard step API."""

    def __init__(self, log):
        self._log = log
        self.position = 500
        self.failed = False
        self.last_outcome = None
        self.commands = []
        self.publication_guard = contextlib.nullcontext
        self.move_entered = threading.Event()
        self.move_gate = threading.Event()
        self.move_gate.set()
        self._closed = threading.Event()

    @property
    def closed(self):
        return self._closed.is_set()

    def request_collector(self):
        pass

    def snapshot(self):
        return {
            "position": self.position,
            "available": True,
            "can_initialize": False,
            "busy": False,
            "locked": False,
            "message": "The gripper is ready.",
        }

    def poll(self):
        pass

    def measure(self):
        return self.position, None

    def move(self, target):
        self.last_outcome = None
        try:
            with self.publication_guard():
                self.commands.append(target)
                self._log.add(f"gripper.move:{target}")
        except Exception as error:
            raise GripperNotSentError(f"No command sent. {error}") from error
        self.move_entered.set()
        try:
            gate(self.move_gate)
            self.position = target
            self.last_outcome = "arrived"
        finally:
            self._log.add("gripper.move.exit")

    def close(self):
        self._log.add("gripper.close")
        self._closed.set()


class Rig:
    def __init__(self):
        self.log = Log()
        self.feed = Feed()
        self.controls = []
        self.sessions = []
        self.gripper = Gripper(self.log)
        self.session_setup = None
        self._seq = 0
        self.state = RobotPanelState(
            "robot.test",
            gripper=self.gripper,
            connection_factory=self.feed,
            control_factory=self._new_control,
            session_factory=self._new_session,
        )
        # Holds stay in flight without key renewals. Other tests cover the dead-man.
        self.state.KEY_LEASE_SECONDS = 30.0

    def _new_control(self, host, feedback_snapshot, *, send_guard):
        control = Control(self, host, feedback_snapshot, send_guard=send_guard)
        self.controls.append(control)
        return control

    def _new_session(self, control, *, speed_percent):
        session = Session(self, control, speed_percent)
        if self.session_setup is not None:
            self.session_setup(session)
        self.sessions.append(session)
        return session

    def seq(self):
        self._seq += 1
        return self._seq

    def connect(self):
        self.state.command({"action": "start_sync"}, OWNER)
        wait_for(lambda: self.state.snapshot()["feedback_fresh"])

    def arm(self, speed=1):
        payload = {"action": "keyboard", "enabled": True, "speed": speed, "seq": self.seq()}
        return self.state.command(payload, OWNER)

    def arm_ready(self, speed=1):
        self.arm(speed)
        wait_for(lambda: self.state.snapshot()["keyboard_enabled"])
        return self.sessions[-1]

    def keys(self, *held):
        return self.state.keys({"seq": self.seq(), "held": list(held)}, OWNER)

    def hold(self, key):
        """Press an arm key. Return the hold worker after the session runs the hold."""
        session = self.sessions[-1]
        self.keys(key)
        wait_for(session.run_entered.is_set)
        return self.state._kb_thread

    def press_gripper(self, key):
        """Press Z or X. Return the hold worker while its first step waits for arrival."""
        self.gripper.move_gate.clear()
        self.keys(key)
        assert self.gripper.move_entered.wait(1)
        return self.state._kb_thread

    def open_gates(self):
        for session in self.sessions:
            session.open_gates()
        for control in self.controls:
            control.open_gates()
        self.gripper.move_gate.set()
        self.feed.read.set()


@pytest.fixture
def rig(monkeypatch):
    block_network(monkeypatch)
    rig = Rig()
    try:
        yield rig
    finally:
        rig.open_gates()
        rig.state.close()


def record_alive_at_cancel(control, worker):
    alive = []
    control.on_cancel = lambda: alive.append(worker.is_alive())
    return alive


# Stop, disable, disconnect, and lease expiry during an arm hold.


def test_stop_during_arm_hold_releases_at_once_and_cancels_after_hold_exits(rig):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)
    snapshot = state.snapshot()
    assert snapshot["keyboard_active_key"] == "W"
    assert snapshot["busy"]
    assert snapshot["actual"][:4] == [None] * 4

    snapshot = state.command({"action": "stop"}, OTHER)

    # The request revokes key input at once. The session sends its own jog stop.
    assert "session1.release" in rig.log
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert snapshot["keyboard_disarm_reason"] is None
    assert not control.stopped
    # The parent cancel waits, but the send guard already refuses every new mutation.
    with pytest.raises(RuntimeError, match="locked"):
        control.send_guard()
    rig.keys("A")
    stays(lambda: "control1.cancel" not in rig.log and "control1.stop" not in rig.log)
    assert worker.is_alive()
    assert state.snapshot()["busy"]

    session.exit_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order(
        "session1.release", "session1.run.exit", "control1.cancel", "control1.stop"
    )
    assert alive_at_cancel == [False]
    assert "control1.disable" not in rig.log
    assert session.keys == ["W"]
    assert len(rig.sessions) == 1
    assert session.closed.is_set()
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    assert snapshot["message"].startswith("Robot stop is confirmed")


@pytest.mark.parametrize(
    "actions", [["disable"], ["stop", "disable"]], ids=["disable", "stop-disable"]
)
def test_disable_during_arm_hold_waits_for_hold_exit_and_recovers(rig, actions):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)

    for action in actions:
        snapshot = state.command({"action": action}, OTHER)

    assert "session1.release" in rig.log
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert not control.stopped
    stays(
        lambda: (
            not control.stopped
            and "control1.cancel:disable" not in rig.log
            and "control1.disable" not in rig.log
        )
    )
    assert len(rig.controls) == 1

    session.exit_gate.set()
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order(
        "session1.release",
        "session1.run.exit",
        "control1.cancel:disable",
        "control1.disable",
        "control1.stationary",
    )
    if "stop" in actions:
        rig.log.assert_order("session1.run.exit", "control1.cancel", "control1.stop")
    else:
        assert "control1.stop" not in rig.log
    assert alive_at_cancel == [False] * len(actions)
    snapshot = state.snapshot()
    assert not snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]
    # The recovered panel prepares keyboard control on the new control.
    assert rig.arm_ready().control is rig.controls[1]


@pytest.mark.parametrize("confirmed", [True, False])
def test_stop_sync_during_arm_hold_disconnects_only_after_confirmed_stop(rig, confirmed):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    control = rig.controls[0]
    control.stop_ok = confirmed
    at_stop = []
    control.on_stop = lambda: at_stop.append((state._syncing, state._owner, worker.is_alive()))

    state.command({"action": "stop_sync"}, OWNER)

    assert "session1.release" in rig.log
    assert not control.stopped
    stays(lambda: state.snapshot()["syncing"] and state.snapshot()["controller_id"] == OWNER)
    assert "control1.cancel" not in rig.log
    session.exit_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order(
        "session1.release", "session1.run.exit", "control1.cancel", "control1.stop"
    )
    assert at_stop == [(True, OWNER, False)]
    snapshot = state.snapshot()
    assert snapshot["locked"]
    if confirmed:
        assert not snapshot["syncing"]
        assert snapshot["controller_id"] is None
        assert not snapshot["stop_unconfirmed"]
    else:
        assert snapshot["syncing"]
        assert snapshot["controller_id"] == OWNER
        assert snapshot["stop_unconfirmed"]


LEASE_TRIGGERS = ("watchdog", "heartbeat", "keys")
INTERNAL_STOP_REASONS = {
    "watchdog": "browser control lease expired.",
    "heartbeat": "browser control lease expired.",
    "keys": "browser control lease expired.",
    "robot-error": "robot feedback is stale, unavailable, or reports a fault.",
}


@pytest.mark.parametrize(
    "trigger", ["watchdog", "heartbeat", "keys", "feedback-failure", "robot-error"]
)
def test_internal_stop_during_arm_hold_waits_for_hold_exit(rig, trigger):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)

    if trigger in LEASE_TRIGGERS:
        with state._lock:
            state._heartbeat -= state.LEASE_SECONDS + 1
            if trigger == "heartbeat":
                assert state.heartbeat(OWNER)["controller_id"] is None
            elif trigger == "keys":
                # A late renewal can neither revive the lease nor keep the hold.
                with pytest.raises(PermissionError):
                    rig.keys("W")
        wait_for(lambda: state.snapshot()["controller_id"] is None)
    elif trigger == "feedback-failure":

        def fail():
            raise ConnectionError("Feedback connection reset")

        rig.feed.read_latest = fail
        wait_for(lambda: state.snapshot()["locked"])
    else:
        rig.feed.sample = Feedback((1, 2, 3, 4), 9, 0, True)
        wait_for(lambda: state.snapshot()["locked"])

    snapshot = state.snapshot()
    assert not snapshot["keyboard_enabled"]
    if trigger in INTERNAL_STOP_REASONS:
        assert snapshot["keyboard_disarm_reason"] == INTERNAL_STOP_REASONS[trigger]
    assert snapshot["locked"]
    assert "session1.release" in rig.log
    stays(lambda: "control1.cancel" not in rig.log and not control.stopped)
    assert worker.is_alive()
    session.exit_gate.set()
    wait_for(lambda: "control1.stop" in rig.log)
    rig.log.assert_order(
        "session1.release", "session1.run.exit", "control1.cancel", "control1.stop"
    )
    assert alive_at_cancel == [False]


@pytest.mark.parametrize("key", ["W", "Z"])
def test_stop_before_hold_worker_reaches_session_sends_nothing_from_it(rig, key):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    control = rig.controls[0]
    with idle_lock(state):
        rig.keys(key)
        worker = state._kb_thread
        assert worker is not None
        state.command({"action": "stop"}, OTHER)
        # Only an arm hold defers the parent cancel.
        assert control.stopped is (key == "Z")
    worker.join(2)
    assert not worker.is_alive()
    wait_for(lambda: "control1.stop" in rig.log)
    assert "session1.run:W" not in rig.log
    assert rig.gripper.commands == []
    rig.log.assert_order("control1.cancel", "control1.stop")
    assert "ERROR" not in state.snapshot()["message"]


@pytest.mark.parametrize("failure", ["session-fault", "retained-command"])
def test_hold_failure_becomes_verified_stop_and_lock_that_disable_recovers(rig, failure):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    control = rig.controls[0]
    control.stop_gate.clear()
    if failure == "session-fault":
        session.fault_on_run = "Keyboard stop unconfirmed: no rest"
        detail = session.fault_on_run
    else:
        control.retention = "A controller acknowledgement failed."
        session.error_on_run = RuntimeError("MoveJog acknowledgement failed")
        detail = "MoveJog acknowledgement failed"

    rig.keys("W")

    assert control.stop_entered.wait(1)
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert snapshot["message"] == f"Keyboard control failed: {detail}"
    control.stop_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order("session1.run.exit", "control1.stop")
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    with pytest.raises(RuntimeError, match="locked"):
        rig.arm()

    state.command({"action": "disable"}, OTHER)
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["locked"])
    rig.log.assert_order("control1.stop", "control1.disable", "control1.stationary")


def test_preparation_fault_becomes_verified_stop_and_lock_that_disable_recovers(rig):
    state = rig.state
    rig.connect()
    control = rig.controls[0]
    control.stop_gate.clear()
    fault = "SpeedFactor acknowledgement failed"
    rig.session_setup = lambda session: setattr(session, "fault_on_prepare", fault)

    rig.arm()

    assert control.stop_entered.wait(1)
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert not snapshot["keyboard_arming"]
    assert snapshot["message"] == f"Keyboard control failed: {fault}"
    control.stop_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order("session1.prepare.exit", "control1.stop")
    assert rig.sessions[0].closed.is_set()
    assert not state.snapshot()["stop_unconfirmed"]

    state.command({"action": "disable"}, OTHER)
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["locked"])


# Gripper steps are not deferred.


@pytest.mark.parametrize(("key", "target"), [("Z", 525), ("X", 475)])
def test_stop_during_gripper_hold_cancels_parent_at_once(rig, key, target):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    worker = rig.press_gripper(key)
    control = rig.controls[0]
    assert state.snapshot()["gripper_busy"]

    state.command({"action": "stop"}, OTHER)

    # The arm is idle, so the stop does not wait for the gripper step.
    assert control.stopped
    assert control.stop_entered.wait(1)
    assert worker.is_alive()
    rig.gripper.move_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order(
        f"gripper.move:{target}", "control1.cancel", "control1.stop", "gripper.move.exit"
    )
    assert rig.gripper.commands == [target]
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert not snapshot["gripper_busy"]


def test_recovery_after_verified_disable_waits_for_keyboard_worker(rig):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    worker = rig.press_gripper("X")
    control = rig.controls[0]

    state.command({"action": "disable"}, OTHER)

    assert control.stopped
    wait_for(lambda: "control1.stationary" in rig.log)
    stays(lambda: len(rig.controls) == 1 and state.snapshot()["locked"])
    assert worker.is_alive()
    rig.gripper.move_gate.set()
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["locked"])
    rig.log.assert_order(
        "control1.cancel:disable", "control1.disable", "control1.stationary", "gripper.move.exit"
    )
    assert not state.snapshot()["busy"]


# Stop and disable during preparation.


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_stop_during_preparation_closes_session_and_cancels_after_worker_exits(rig, action):
    state = rig.state
    rig.connect()

    def slow_preparation(session):
        session.prepare_gate.clear()
        session.abort_gate.clear()

    rig.session_setup = slow_preparation
    rig.arm()
    session = rig.sessions[0]
    assert session.prepare_entered.wait(1)
    worker = state._kb_thread
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)
    snapshot = state.snapshot()
    assert snapshot["keyboard_arming"]
    assert snapshot["busy"]

    snapshot = state.command({"action": action}, OTHER)

    # No controller command is pending, so the session is closed at once.
    assert "session1.close" in rig.log
    assert snapshot["locked"]
    assert not snapshot["keyboard_arming"]
    assert not snapshot["keyboard_enabled"]
    stays(lambda: not control.stopped)
    assert worker.is_alive()
    # A cancelled preparation cannot send SpeedFactor. The refusal does not fault the session.
    with pytest.raises(PreparationRejectedError, match="Keyboard preparation cancelled"):
        control.send_guard()

    session.abort_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    if action == "stop":
        rig.log.assert_order(
            "session1.close", "session1.prepare.exit", "control1.cancel", "control1.stop"
        )
    else:
        rig.log.assert_order(
            "session1.close",
            "session1.prepare.exit",
            "control1.cancel:disable",
            "control1.disable",
            "control1.stationary",
        )
    assert alive_at_cancel == [False]
    snapshot = state.snapshot()
    assert not snapshot["keyboard_enabled"]
    assert "ERROR" not in snapshot["message"]
    if action == "stop":
        assert snapshot["locked"]
    else:
        assert not snapshot["locked"]
        assert len(rig.controls) == 2


def test_stop_during_preparation_leaves_session_with_pending_command_alone(rig):
    state = rig.state
    rig.connect()
    rig.session_setup = lambda session: session.prepare_gate.clear()
    rig.arm()
    session = rig.sessions[0]
    assert session.prepare_entered.wait(1)
    worker = state._kb_thread
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)
    # The SpeedFactor acknowledgement is pending.
    control.command_pending = True

    state.command({"action": "stop"}, OTHER)

    assert "session1.close" not in rig.log
    assert "session1.release" not in rig.log
    stays(lambda: not control.stopped and "session1.close" not in rig.log)
    assert worker.is_alive()
    with pytest.raises(PreparationRejectedError, match="Keyboard preparation cancelled"):
        control.send_guard()
    control.command_pending = False
    session.prepare_gate.set()
    wait_for(lambda: "control1.stop" in rig.log)
    rig.log.assert_order(
        "session1.prepare.exit", "session1.close", "control1.cancel", "control1.stop"
    )
    assert alive_at_cancel == [False]
    snapshot = state.snapshot()
    assert not snapshot["keyboard_enabled"]
    assert "Keyboard ready" not in snapshot["message"]


def test_stop_while_preparation_waits_for_pose_poll_never_prepares(rig):
    state = rig.state
    rig.connect()
    control = rig.controls[0]
    control.hold_polls.set()
    assert control.poll_blocked.wait(1)
    rig.arm()
    session = rig.sessions[0]
    # Preparation waits for the pose query to finish.
    stays(lambda: not session.prepare_entered.is_set(), 0.05)
    assert state.snapshot()["keyboard_arming"]

    state.command({"action": "stop"}, OTHER)

    assert "session1.close" in rig.log
    # The deferred stop waits neither for the pose query nor for a preflight.
    wait_for(lambda: "control1.stop" in rig.log)
    assert not control.poll_release.is_set()
    control.hold_polls.clear()
    control.poll_release.set()
    wait_for(lambda: not state.snapshot()["busy"])
    assert "session1.prepare" not in rig.log
    rig.log.assert_order("session1.close", "control1.cancel", "control1.stop")


# Watchdog mode rules.


def test_watchdog_tolerates_mode_11_only_while_arm_hold_runs(rig):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    rig.hold("W")
    control = rig.controls[0]
    rig.feed.sample = JOGGING
    wait_for(lambda: state.snapshot()["robot_mode"] == 11)
    stays(lambda: not control.stopped and not state.snapshot()["locked"], 0.25)
    assert state.snapshot()["keyboard_enabled"]

    # Release W. The hold ends while feedback still reports a jog.
    rig.keys()

    wait_for(lambda: "control1.stop" in rig.log)
    rig.log.assert_order("session1.run.exit", "control1.cancel", "control1.stop")
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]


@pytest.mark.parametrize("phase", ["keys-off", "keys-idle", "gripper-step", "preparation"])
def test_watchdog_stops_for_mode_11_outside_arm_hold(rig, phase):
    state = rig.state
    rig.connect()
    if phase == "keys-idle":
        rig.arm_ready()
    elif phase == "gripper-step":
        rig.arm_ready()
        rig.press_gripper("Z")
    elif phase == "preparation":
        rig.session_setup = lambda session: session.prepare_gate.clear()
        rig.arm()
        assert rig.sessions[0].prepare_entered.wait(1)

    rig.feed.sample = JOGGING

    wait_for(lambda: "control1.stop" in rig.log)
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert not snapshot["keyboard_arming"]


# Panel close.


def test_close_during_arm_hold_joins_hold_before_gripper_close_and_ownership_release(rig):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    contender = ControllerOwnership("robot.test")
    closer = threading.Thread(target=state.close, daemon=True)
    closer.start()
    try:
        wait_for(lambda: "session1.release" in rig.log)
        stays(lambda: "control1.cancel" not in rig.log and "gripper.close" not in rig.log)
        assert worker.is_alive()
        assert closer.is_alive()
        assert not acquired(contender)

        session.exit_gate.set()
        closer.join(5)
        assert not closer.is_alive()
        rig.log.assert_order(
            "session1.release",
            "session1.run.exit",
            "control1.cancel",
            "control1.stop",
            "gripper.close",
        )
        assert acquired(contender)
    finally:
        session.exit_gate.set()
        closer.join(5)
        contender.release()


def test_close_during_gripper_hold_stops_at_once_and_closes_gripper_after_step(rig):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    worker = rig.press_gripper("Z")
    control = rig.controls[0]
    contender = ControllerOwnership("robot.test")
    closer = threading.Thread(target=state.close, daemon=True)
    closer.start()
    try:
        assert control.stop_entered.wait(1)
        assert worker.is_alive()
        stays(lambda: "gripper.close" not in rig.log)
        assert closer.is_alive()
        assert not acquired(contender)

        rig.gripper.move_gate.set()
        closer.join(5)
        assert not closer.is_alive()
        rig.log.assert_order(
            "gripper.move:525",
            "control1.cancel",
            "control1.stop",
            "gripper.move.exit",
            "gripper.close",
        )
        assert acquired(contender)
    finally:
        rig.gripper.move_gate.set()
        closer.join(5)
        contender.release()


def test_close_requests_stop_while_only_keyboard_worker_runs(rig):
    state = rig.state
    state.KEYBOARD_JOIN_SECONDS = 0.2
    rig.connect()
    session = rig.arm_ready()
    session.ignore_release = True
    worker = rig.hold("W")
    state.command({"action": "stop_sync"}, OWNER)
    wait_for(lambda: not state.snapshot()["syncing"])
    wait_for(lambda: state._safety_workers == 0)
    assert worker.is_alive()
    assert rig.log.count("control1.stop") == 1
    closer = threading.Thread(target=state.close, daemon=True)
    closer.start()
    try:
        # Keyboard activity alone makes close() request another stop.
        wait_for(lambda: rig.log.count("control1.stop") == 2)
    finally:
        session.finish_gate.set()
        closer.join(5)
    assert not closer.is_alive()
    entries = rig.log.entries
    last_stop = max(index for index, entry in enumerate(entries) if entry == "control1.stop")
    assert last_stop < entries.index("gripper.close")


def test_close_is_bounded_when_hold_never_exits_and_keeps_ownership(rig):
    state = rig.state
    state.KEYBOARD_JOIN_SECONDS = 0.2
    rig.connect()
    session = rig.arm_ready()
    session.ignore_release = True
    worker = rig.hold("W")
    contender = ControllerOwnership("robot.test")
    try:
        started = time.monotonic()
        state.close()
        # Both keyboard joins time out; the default join would take 12 s.
        assert time.monotonic() - started < 3
        assert worker.is_alive()
        rig.log.assert_order(
            "session1.release", "control1.cancel", "control1.stop", "gripper.close"
        )
        # The keyboard worker can still act, so the panel keeps controller ownership.
        assert not acquired(contender)
    finally:
        session.finish_gate.set()
        contender.release()


# Robustness of the safety paths.


@pytest.mark.parametrize("trigger", ["stop", "disable", "lease", "close"])
def test_failed_session_release_still_stops_after_hold_exits(rig, trigger):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    worker = rig.hold("W")
    session.release_error = RuntimeError("can't start new thread")
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)
    closer = threading.Thread(target=state.close, daemon=True)

    try:
        if trigger in {"stop", "disable"}:
            snapshot = state.command({"action": trigger}, OTHER)
            assert snapshot["locked"]
            assert not snapshot["keyboard_enabled"]
            # The cleanup failure is reported, not raised.
            report = f"{snapshot['keyboard_disarm_reason']} {snapshot['message']}"
            assert "can't start new thread" in report
        elif trigger == "lease":
            with state._lock:
                state._heartbeat -= state.LEASE_SECONDS + 1
        else:
            closer.start()
        wait_for(lambda: "session1.release" in rig.log)
        stays(lambda: not control.stopped)
    finally:
        session.exit_gate.set()
    if trigger == "close":
        closer.join(5)
        assert not closer.is_alive()

    cancel = "control1.cancel:disable" if trigger == "disable" else "control1.cancel"
    verify = "control1.disable" if trigger == "disable" else "control1.stop"
    wait_for(lambda: verify in rig.log)
    wait_for(lambda: not worker.is_alive())
    rig.log.assert_order("session1.release", "session1.run.exit", cancel, verify)
    assert alive_at_cancel == [False]
    assert not state.snapshot()["stop_unconfirmed"]
    if trigger == "lease":
        assert state.snapshot()["keyboard_disarm_reason"] == "browser control lease expired."
        stays(state._watchdog.is_alive)
    elif trigger == "close":
        rig.log.assert_order(verify, "gripper.close")


def test_failed_session_release_on_key_up_turns_keys_off_without_raising(rig):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    worker = rig.hold("W")
    session.release_error = RuntimeError("can't start new thread")

    snapshot = rig.keys()

    assert "session1.release" in rig.log
    assert not snapshot["keyboard_enabled"]
    assert "can't start new thread" in snapshot["keyboard_disarm_reason"]
    # The panel's own release gate still ends the hold.
    worker.join(2)
    assert not worker.is_alive()
    assert "session1.run.released" in rig.log
    wait_for(lambda: not state.snapshot()["busy"])
    assert session.closed.is_set()


@pytest.mark.parametrize("source", ["press", "poll"])
def test_hold_thread_start_failure_turns_keys_off_and_keeps_worker_loop(rig, monkeypatch, source):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    control = rig.controls[0]
    failing = threading.Event()
    start = threading.Thread.start

    def start_thread(thread):
        # The panel registers its keyboard worker before it starts the thread.
        if failing.is_set() and thread is state._kb_thread:
            raise RuntimeError("can't start new thread")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_thread)
    try:
        failing.set()
        if source == "press":
            with idle_lock(state):
                snapshot = rig.keys("W")
            assert not snapshot["keyboard_enabled"]
        else:
            # The press waits for the pose query. The worker loop starts the hold after it.
            control.hold_polls.set()
            assert control.poll_blocked.wait(1)
            assert rig.keys("W")["keyboard_enabled"]
            control.hold_polls.clear()
            control.poll_release.set()
        wait_for(lambda: not state.snapshot()["keyboard_enabled"])
    finally:
        failing.clear()

    snapshot = state.snapshot()
    assert snapshot["keyboard_disarm_reason"].startswith("keyboard worker could not start")
    assert snapshot["message"].startswith("Keyboard off: keyboard worker could not start")
    assert snapshot["keyboard_active_key"] is None
    assert not snapshot["busy"]
    assert not snapshot["locked"]
    with state._lock:
        assert state._kb_thread is None
        assert state._kb_kind is None
    assert "session1.run:W" not in rig.log
    assert not control.stopped
    # The worker loop keeps running: it polls again and runs the next job.
    assert state._worker.is_alive()
    polls = control.polls
    wait_for(lambda: control.polls > polls)
    state.command({"action": "enable"}, OWNER)
    wait_for(lambda: "control1.enable" in rig.log)


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_safety_thread_start_failure_faults_and_cancels_parent(rig, monkeypatch, action):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    session.exit_gate.clear()
    rig.hold("W")
    control = rig.controls[0]
    failing = threading.Event()
    start = threading.Thread.start

    def start_thread(thread):
        target = getattr(thread, "_target", None)
        if (
            failing.is_set()
            and getattr(target, "__self__", None) is state
            and getattr(target, "__name__", None) == "_stop"
        ):
            raise RuntimeError("can't start new thread")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_thread)
    failing.set()
    try:
        snapshot = state.command({"action": action}, OTHER)
    finally:
        failing.clear()

    cancel = "control1.cancel:disable" if action == "disable" else "control1.cancel"
    assert cancel in rig.log
    assert control.stopped
    assert "session1.release" in rig.log
    assert snapshot["stop_unconfirmed"]
    assert snapshot["locked"]
    assert not snapshot["keyboard_enabled"]
    assert snapshot["message"] == (
        "Robot stop could not start: can't start new thread Use the physical emergency stop."
    )
    with state._lock:
        assert state._safety_workers == 0
        assert state._safety_actions == set()

    session.exit_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    snapshot = state.snapshot()
    assert snapshot["stop_unconfirmed"]
    assert snapshot["locked"]
    assert snapshot["message"].startswith("Robot stop could not start")
    # The counters were undone, so a later request starts the stop worker.
    state.command({"action": action}, OTHER)
    verify = "control1.disable" if action == "disable" else "control1.stop"
    wait_for(lambda: verify in rig.log)


def test_watchdog_exception_requests_stop_and_watchdog_keeps_running(rig, monkeypatch):
    state = rig.state
    rig.connect()
    rig.arm_ready()
    control = rig.controls[0]
    control.stop_gate.clear()

    def crash(joints):
        raise RuntimeError("joint check crashed")

    # The watchdog checks measured joints while keys are on.
    monkeypatch.setattr(web_control, "validate_measured_joints", crash)

    assert control.stop_entered.wait(1)
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert snapshot["message"] == "Panel watchdog failed: joint check crashed"
    assert not snapshot["keyboard_enabled"]
    control.stop_gate.set()
    wait_for(lambda: not state.snapshot()["busy"])
    rig.log.assert_order("control1.cancel", "control1.stop")
    assert state._watchdog.is_alive()
    # The watchdog still enforces the browser lease.
    with state._lock:
        state._heartbeat -= state.LEASE_SECONDS + 1
    wait_for(lambda: state.snapshot()["controller_id"] is None)
    assert state._watchdog.is_alive()


def test_stop_worker_skips_join_for_keyboard_thread_that_never_started(rig):
    state = rig.state
    rig.connect()
    control = rig.controls[0]
    never_started = threading.Thread(target=lambda: None)
    with state._lock:
        state._locked = True
        state._safety_workers += 1
        state._safety_actions.add(False)

    state._stop(control, False, never_started)

    rig.log.assert_order("control1.cancel", "control1.stop")
    snapshot = state.snapshot()
    assert not snapshot["stop_unconfirmed"]
    assert snapshot["message"].startswith("Robot stop is confirmed")
    with state._lock:
        assert state._safety_workers == 0


def test_registered_keyboard_thread_that_never_started_does_not_defer_stop(rig):
    state = rig.state
    rig.connect()
    control = rig.controls[0]
    with state._lock:
        state._kb_thread = threading.Thread(target=lambda: None)
        state._kb_kind = "arm"
    try:
        state.command({"action": "stop"}, OTHER)
        assert control.stopped
        wait_for(lambda: state._safety_workers == 0)
        rig.log.assert_order("control1.cancel", "control1.stop")
        assert not state.snapshot()["stop_unconfirmed"]
    finally:
        with state._lock:
            state._kb_thread = state._kb_kind = None


# The keyboard join can time out.


def test_join_timeout_stops_anyway_and_late_hold_exit_stays_locked_until_disable(rig):
    state = rig.state
    state.KEYBOARD_JOIN_SECONDS = 0.2
    rig.connect()
    session = rig.arm_ready()
    session.ignore_release = True
    worker = rig.hold("W")
    control = rig.controls[0]
    alive_at_cancel = record_alive_at_cancel(control, worker)

    state.command({"action": "stop"}, OTHER)

    assert "session1.release" in rig.log
    wait_for(lambda: "control1.stop" in rig.log)
    wait_for(lambda: state._safety_workers == 0)
    # The hold ignored its release. The join timed out and the stop went out anyway.
    assert alive_at_cancel == [True]
    assert worker.is_alive()
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert snapshot["busy"]
    assert not snapshot["stop_unconfirmed"]

    session.finish_gate.set()
    worker.join(2)
    assert not worker.is_alive()
    wait_for(lambda: not state.snapshot()["busy"])
    snapshot = state.snapshot()
    assert snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    assert "ERROR" not in snapshot["message"]
    assert rig.log.count("control1.stop") == 1
    assert len(rig.controls) == 1
    assert session.closed.is_set()
    with pytest.raises(RuntimeError, match="locked"):
        state.command({"action": "enable"}, OWNER)

    state.command({"action": "disable"}, OTHER)
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["locked"])
    rig.log.assert_order(
        "control1.stop",
        "session1.run.exit",
        "control1.cancel:disable",
        "control1.disable",
        "control1.stationary",
    )


def test_join_timeout_disable_recovers_only_after_late_hold_exit(rig):
    state = rig.state
    state.KEYBOARD_JOIN_SECONDS = 0.2
    rig.connect()
    session = rig.arm_ready()
    session.ignore_release = True
    worker = rig.hold("W")

    state.command({"action": "disable"}, OTHER)

    wait_for(lambda: "control1.stationary" in rig.log)
    wait_for(lambda: state._safety_workers == 0)
    assert worker.is_alive()
    # A verified disable cannot recover while the keyboard worker can still act.
    stays(lambda: len(rig.controls) == 1 and state.snapshot()["locked"])
    session.finish_gate.set()
    wait_for(lambda: len(rig.controls) == 2)
    wait_for(lambda: not state.snapshot()["locked"])
    rig.log.assert_order(
        "control1.cancel:disable", "control1.disable", "control1.stationary", "session1.run.exit"
    )
    assert not state.snapshot()["stop_unconfirmed"]


def test_disconnect_keeps_ownership_until_keyboard_worker_exits(rig):
    state = rig.state
    state.KEYBOARD_JOIN_SECONDS = 0.2
    rig.connect()
    session = rig.arm_ready()
    session.ignore_release = True
    worker = rig.hold("W")
    contender = ControllerOwnership("robot.test")
    try:
        state.command({"action": "stop_sync"}, OWNER)
        wait_for(lambda: not state.snapshot()["syncing"])
        state._reader.join(1)
        assert not state._reader.is_alive()
        assert worker.is_alive()
        assert state.snapshot()["busy"]
        stays(lambda: not acquired(contender), 0.15)

        session.finish_gate.set()
        wait_for(lambda: acquired(contender))
        assert not state.snapshot()["busy"]
    finally:
        session.finish_gate.set()
        contender.release()


# A closed live connection of the owner.


def test_closed_connection_of_another_client_changes_nothing(rig):
    state = rig.state
    rig.connect()
    session = rig.arm_ready()
    worker = rig.hold("W")
    before = state.snapshot()

    state.connection_lost(OTHER)

    stays(lambda: "session1.release" not in rig.log)
    assert worker.is_alive()
    snapshot = state.snapshot()
    assert snapshot["keyboard_enabled"] and snapshot["keyboard_active_key"] == "W"
    assert snapshot["message"] == before["message"]
    rig.keys()
    wait_for(lambda: not worker.is_alive())
    assert session.keys == ["W"]


def test_closed_owner_connection_cancels_keyboard_preparation(rig):
    state = rig.state
    rig.connect()
    rig.session_setup = lambda session: session.prepare_gate.clear()
    rig.arm()
    session = rig.sessions[-1]
    assert session.prepare_entered.wait(1)
    assert state.snapshot()["keyboard_arming"]

    state.connection_lost(OWNER)

    snapshot = state.snapshot()
    assert not snapshot["keyboard_arming"] and not snapshot["keyboard_enabled"]
    assert snapshot["keyboard_disarm_reason"] == "browser connection closed."
    wait_for(lambda: state.snapshot()["busy"] is False)
    assert session.closed.is_set()
    assert not state.snapshot()["keyboard_enabled"]
    assert not state.snapshot()["locked"]


def test_closed_owner_connection_while_keys_are_off_keeps_the_message(rig):
    state = rig.state
    rig.connect()
    message = state.snapshot()["message"]

    state.connection_lost(OWNER)

    snapshot = state.snapshot()
    assert snapshot["message"] == message
    assert snapshot["keyboard_disarm_reason"] is None
    assert snapshot["controller_id"] == OWNER and not snapshot["locked"]


@pytest.mark.parametrize("client", ["", " ", None, "x" * 129])
def test_connection_lost_requires_a_client_id(rig, client):
    with pytest.raises(ValueError):
        rig.state.connection_lost(client)
