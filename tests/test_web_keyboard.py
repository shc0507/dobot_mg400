"""Keyboard jogging in the shared web panel core.

A fake KeyboardJogSession, a fake JointControl and a fake WebGripper stand in for
the robot, so no test opens a network connection.
"""

import json
import socket
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import PreparationRejectedError
from mg400_agent.web_control import RobotPanelState
from mg400_agent.web_gripper import GripperNotSentError

CLIENT = "browser-a"
OTHER = "browser-b"
POSE = (250, 0, 30, 0)
ALL_KEYS = "WASDQERFZX"
TARGETS = {"x": 250, "y": 0, "z": 30, "r": 0}
BUSY = "Another robot operation is in progress"
CHECKING = "Checking keyboard control..."
READY = "Keyboard ready. Hold one key; release requests a stop."
JOGGING = "Jogging... Release the key to stop."
STOPPED = "Stopped. Hold a key for the next move."
DEAD_MAN = "key updates stopped."
MISSING = object()


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def request(base, **changes):
    """Copy a request. Fields set to MISSING are removed."""
    result = {**base, **changes}
    return {key: value for key, value in result.items() if value is not MISSING}


ARM = {"action": "keyboard", "enabled": True, "speed": 5, "seq": 1}
DISARM = {"action": "keyboard", "enabled": False, "reason": "focus", "seq": 1}


class Feed:
    """Deliver the current sample about every 10 ms."""

    def __init__(self):
        self.sample = Feedback((1, 2, 3, 4), 5, 0, False)
        self.connections = 0
        self.read = threading.Event()
        self.read.set()

    def __call__(self, host, timeout_s):
        assert host == "robot.test"
        return self

    def __enter__(self):
        self.connections += 1
        return self

    def __exit__(self, *args):
        pass

    def read_latest(self):
        self.read.wait(0.6)
        time.sleep(0.01)
        return self.sample


class Control:
    """Fake parent JointControl. It records calls in order and never sends."""

    def __init__(self, log, host, feedback_snapshot, *, send_guard):
        self.log = log
        self.feedback = feedback_snapshot
        self.send_guard = send_guard
        self.calls = []
        self.stopped = False
        self.command_pending = False
        self.busy = False
        self.retention = None
        self.stop_ok = True
        self.pose = POSE
        self.pose_reads = 0
        self.pose_blocked = threading.Event()
        self.release_pose = threading.Event()
        self.release_pose.set()
        self.move_entered = threading.Event()
        self.release_move = threading.Event()
        self.release_move.set()

    def _record(self, call):
        self.calls.append(call)
        self.log.append(("control", call))

    def cancel(self, *, disabling=False):
        self._record(("cancel", disabling))
        self.stopped = True

    def read_pose(self):
        self.pose_reads += 1
        if not self.release_pose.is_set():
            self.pose_blocked.set()
            self.release_pose.wait(5)
        return self.pose

    def read_state(self):
        sample, _ = self.feedback()
        if sample.error_status or sample.mode not in {4, 5}:
            raise RuntimeError("Unexpected robot state")
        return sample

    def enable(self):
        self.send_guard()
        self._record("enable")

    def move(self, target):
        self._record(("joint", target))
        self._move()

    def move_linear(self, target):
        self._record(("linear", target))
        self._move()

    def _move(self):
        self.send_guard()
        self.move_entered.set()
        self.release_move.wait(5)
        if self.stopped:
            raise RuntimeError("Control stopped")

    def stop(self):
        self._record("stop")
        return {"ok": self.stop_ok, "error": "Command ordering remains unknown"}

    def disable(self):
        self._record("disable")

    def verify_stationary(self):
        self._record("stationary")

    def feedback_retention_reason(self):
        return self.retention


class Hold:
    """One session.run() call. The test decides how it ends."""

    def __init__(self, key, released):
        self.key = key
        self.released = released
        self.finish = threading.Event()
        self.exited = threading.Event()
        self.pose = None
        self.limit_reason = None
        self.error = None
        self.fault = None
        self.cancel_parent = False


class Session:
    """Fake KeyboardJogSession. It records calls and blocks until the test lets it go."""

    def __init__(self, rig, control, speed_percent):
        self.rig = rig
        self.control = control
        self.speed_percent = speed_percent
        self.calls = []
        self.fault = None
        self.limit_reason = None
        self.prepared = False
        self.closed = False
        self.prepare_entered = threading.Event()
        self.prepare_thread = None
        self.holds = []
        # A released hold returns at once. Clear this gate to model a slow stop.
        self.stop_gate = threading.Event()
        self.stop_gate.set()

    def _record(self, call):
        self.calls.append(call)
        self.rig.log.append(("session", call))

    @property
    def ready(self):
        return self.prepared and not self.closed and self.fault is None and not self.control.stopped

    def prepare(self):
        self._record("prepare")
        self.prepare_thread = threading.current_thread()
        self.prepare_entered.set()
        deadline = time.monotonic() + 5
        while not self.rig.prepare_gate.is_set() and not self.closed:
            if time.monotonic() > deadline:
                raise TimeoutError("The test did not end this preparation")
            time.sleep(0.002)
        if self.closed:
            # Like the real session, close() cuts the preflight short.
            raise RuntimeError("Keyboard preparation cancelled")
        if self.rig.prepare_fault is not None:
            self.fault = self.rig.prepare_fault
            self.control.cancel()
            raise RuntimeError(self.fault)
        if self.rig.prepare_error is not None:
            raise self.rig.prepare_error
        self.prepared = True
        return POSE

    def run(self, key, released):
        hold = Hold(key, released)
        self.limit_reason = None
        self.holds.append(hold)
        self._record(("run", key))
        try:
            deadline = time.monotonic() + 5
            while not hold.finish.is_set() and not (released.is_set() and self.stop_gate.is_set()):
                if time.monotonic() > deadline:
                    raise TimeoutError("The test did not end this hold")
                time.sleep(0.002)
            if hold.limit_reason is not None:
                self.limit_reason = hold.limit_reason
            if hold.fault is not None:
                self.fault = hold.fault
                if hold.cancel_parent:
                    self.control.cancel()
            if hold.error is not None:
                raise hold.error
            return hold.pose
        finally:
            hold.exited.set()

    def release(self):
        self._record("release")

    def close(self):
        self._record("close")
        self.closed = True


class Gripper:
    """Fake WebGripper with the keyboard step interface."""

    def __init__(self, position=500):
        self.position = position
        self.locked = False
        self.failed = False
        self.last_outcome = None
        self.outcome = "arrived"
        self.move_error = None
        self.commands = []
        self.polls = 0
        self.closed_checks = 0
        self._closed = False
        self.move_entered = threading.Event()
        self.release_move = threading.Event()
        self.release_move.set()
        self.publication_guard = None

    @property
    def closed(self):
        self.closed_checks += 1
        return self._closed

    def snapshot(self):
        return {
            "position": self.position,
            "available": True,
            "can_initialize": True,
            "busy": False,
            "locked": self.locked,
            "message": "Ready",
        }

    def poll(self):
        self.polls += 1

    def measure(self):
        return self.position, None

    def move(self, target):
        self.last_outcome = None
        if self.move_error is None:
            # Like WebGripper: publish first, then wait for arrival or contact.
            try:
                with self.publication_guard():
                    self.commands.append(target)
            except Exception as error:
                raise GripperNotSentError(f"No command sent. {error}") from error
        self.move_entered.set()
        self.release_move.wait(5)
        time.sleep(0.002)
        if self.move_error is not None:
            raise self.move_error
        if self.outcome == "arrived":
            self.position = target
        self.last_outcome = self.outcome

    def initialize(self):
        with self.publication_guard():
            self.commands.append("initialize")

    def close(self):
        self._closed = True
        self.release_move.set()


class Browser:
    """Number keyboard requests the way one page does."""

    def __init__(self, state, client):
        self.state = state
        self.client = client
        self.seq = 0

    def next_seq(self):
        self.seq += 1
        return self.seq

    def arm(self, speed=1):
        return self.state.command(
            {"action": "keyboard", "enabled": True, "speed": speed, "seq": self.next_seq()},
            self.client,
        )

    def disarm(self, reason="user"):
        return self.state.command(
            {"action": "keyboard", "enabled": False, "reason": reason, "seq": self.next_seq()},
            self.client,
        )

    def keys(self, *held):
        return self.state.keys({"seq": self.next_seq(), "held": list(held)}, self.client)

    def command(self, action, **fields):
        return self.state.command({"action": action, **fields}, self.client)


class Rig:
    def __init__(self, monkeypatch, *, gripper=None, watch=True):
        network = AssertionError("Network")
        monkeypatch.setattr(socket, "create_connection", Mock(side_effect=network))
        monkeypatch.setattr(socket, "socket", Mock(side_effect=network))
        if not watch:
            monkeypatch.setattr(RobotPanelState, "_watch", lambda self: None)
        self.log = []
        self.feed = Feed()
        self.controls = []
        self.sessions = []
        self.gripper = gripper
        self.prepare_gate = threading.Event()
        self.prepare_gate.set()
        self.prepare_error = None
        self.prepare_fault = None
        self.state = RobotPanelState(
            "robot.test",
            gripper=gripper,
            connection_factory=self.feed,
            control_factory=self._new_control,
            session_factory=self._new_session,
        )
        # Only the dead-man tests use a short key lease. A slow CI machine must not
        # turn keys off in the other tests.
        self.state.KEY_LEASE_SECONDS = 30.0
        # Keep close() short if a failed test leaves a fake blocked.
        self.state.KEYBOARD_JOIN_SECONDS = 2.0
        self.browser = Browser(self.state, CLIENT)

    def _new_control(self, host, feedback_snapshot, *, send_guard):
        control = Control(self.log, host, feedback_snapshot, send_guard=send_guard)
        self.controls.append(control)
        return control

    def _new_session(self, control, *, speed_percent):
        session = Session(self, control, speed_percent)
        self.sessions.append(session)
        return session

    @property
    def control(self):
        return self.controls[-1]

    @property
    def session(self):
        return self.sessions[-1]

    def snapshot(self):
        return self.state.snapshot()

    def connect(self, client=CLIENT):
        self.state.command({"action": "start_sync"}, client)
        wait_for(lambda: self.state.snapshot()["feedback_fresh"])

    def arm(self, speed=1):
        """Arm, wait until keys are ON, and return the new session."""
        count = len(self.sessions)
        self.browser.arm(speed)
        wait_for(lambda: self.state.snapshot()["keyboard_enabled"])
        assert len(self.sessions) == count + 1
        return self.sessions[-1]

    def hold(self, number, session=None):
        session = session or self.sessions[-1]
        wait_for(lambda: len(session.holds) >= number)
        return session.holds[number - 1]

    def idle(self):
        wait_for(lambda: not self.state.snapshot()["busy"])

    def close(self):
        self.prepare_gate.set()
        for session in list(self.sessions):
            session.stop_gate.set()
            for hold in list(session.holds):
                hold.finish.set()
        for control in list(self.controls):
            control.release_pose.set()
            control.release_move.set()
        self.feed.read.set()
        if self.gripper is not None:
            self.gripper.release_move.set()
        self.state.close()


@pytest.fixture(autouse=True)
def isolated_controller_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path))


@pytest.fixture
def make_rig(monkeypatch):
    rigs = []

    def make(**options):
        rig = Rig(monkeypatch, **options)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


@pytest.fixture
def rig(make_rig):
    return make_rig()


def block_pose_query(control):
    """Return when a pose query is in flight and blocked."""
    control.pose_blocked.clear()
    control.release_pose.clear()
    assert control.pose_blocked.wait(1)


@contextmanager
def lock_between_pose_queries(rig):
    """Hold the panel lock while no pose query runs.

    A press then starts its hold at once, and the hold worker cannot pass its
    start check until the lock is released.
    """
    deadline = time.monotonic() + 2
    while True:
        rig.state._lock.acquire()
        if not rig.state._working:
            break
        rig.state._lock.release()
        assert time.monotonic() < deadline
        time.sleep(0.002)
    try:
        yield
    finally:
        rig.state._lock.release()


def settle(rig):
    """Wait until the panel worker has run two more cycles (gripper polls)."""
    polls = rig.gripper.polls
    wait_for(lambda: rig.gripper.polls >= polls + 2)


def assert_not_armed(rig):
    snapshot = rig.snapshot()
    assert rig.sessions == []
    assert not snapshot["keyboard_enabled"]
    assert not snapshot["keyboard_arming"]


def start_sync(rig, client):
    """Start sync after the previous feedback reader has stopped."""

    def attempt():
        try:
            rig.state.command({"action": "start_sync"}, client)
        except RuntimeError as error:
            if "reader is stopping" not in str(error):
                raise
            return False
        return True

    wait_for(attempt)
    wait_for(lambda: rig.snapshot()["feedback_fresh"])


# Construction and snapshot fields


def test_construction_is_inert_and_keyboard_fields_have_defaults(rig):
    snapshot = rig.snapshot()
    assert rig.sessions == []
    assert rig.feed.connections == 0
    assert rig.control.calls == []
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_arming"] is False
    assert snapshot["keyboard_active_key"] is None
    assert snapshot["keyboard_speed_percent"] == 1
    assert snapshot["keyboard_disarm_reason"] is None
    assert snapshot["speed_factor"] is None
    assert snapshot["busy"] is False
    assert type(snapshot["version"]) is int


def test_keyboard_timing_constants_are_class_attributes():
    assert RobotPanelState.KEY_LEASE_SECONDS == 0.4
    assert RobotPanelState.KEYBOARD_JOIN_SECONDS == 12.0
    assert RobotPanelState.GRIPPER_HOLD_SECONDS == 5.0


def test_snapshot_version_increases_with_each_snapshot_and_response(rig):
    first = rig.snapshot()["version"]
    second = rig.snapshot()["version"]
    assert second > first
    rig.connect()
    response = rig.browser.keys()
    assert response["version"] > second
    assert rig.state.heartbeat(CLIENT)["version"] > response["version"]


def test_speed_factor_reports_the_measured_controller_ratio(rig):
    rig.connect()
    assert rig.snapshot()["speed_factor"] is None
    rig.feed.sample = Feedback((1, 2, 3, 4), 5, 0, False, 37.0)
    wait_for(lambda: rig.snapshot()["speed_factor"] == 37.0)
    rig.feed.sample = Feedback((1, 2, 3, 4), 5, 0, False, 5.0)
    wait_for(lambda: rig.snapshot()["speed_factor"] == 5.0)


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_speed_factor_hides_a_non_finite_controller_value(rig, raw):
    rig.feed.sample = Feedback((1, 2, 3, 4), 5, 0, False, raw)
    rig.connect()
    snapshot = rig.snapshot()
    assert snapshot["speed_factor"] is None
    # Every API response must stay valid JSON.
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_instance_is_stable_and_new_for_each_panel_process(make_rig):
    first, second = make_rig(), make_rig()
    before, after = first.snapshot(), first.snapshot()
    assert isinstance(before["instance"], str) and before["instance"]
    assert after["instance"] == before["instance"] and after["version"] > before["version"]
    assert second.snapshot()["instance"] != before["instance"]


def test_speed_percent_reports_the_current_or_last_armed_session(rig):
    rig.connect()
    rig.prepare_gate.clear()
    assert rig.browser.arm(42)["keyboard_speed_percent"] == 42
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    assert rig.snapshot()["keyboard_speed_percent"] == 42
    assert rig.browser.disarm("focus")["keyboard_speed_percent"] == 42


def test_snapshot_reports_an_arm_hold(rig):
    rig.connect()
    wait_for(lambda: rig.snapshot()["actual"][:4] == list(POSE))
    session = rig.arm(5)
    assert rig.snapshot()["actual"][:4] == list(POSE)
    session.stop_gate.clear()
    rig.browser.keys("W")
    # A pose query in flight can delay the start, so wait for the hold.
    rig.hold(1)
    snapshot = rig.snapshot()
    assert snapshot["keyboard_active_key"] == "W"
    assert snapshot["busy"] is True
    assert snapshot["actual"][:4] == [None] * 4
    assert snapshot["message"] == JOGGING
    snapshot = rig.browser.keys()
    # The released hold is still stopping.
    assert snapshot["keyboard_active_key"] is None
    assert snapshot["busy"] is True
    assert snapshot["actual"][:4] == [None] * 4
    session.stop_gate.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["keyboard_active_key"] is None
    assert snapshot["keyboard_speed_percent"] == 5
    assert snapshot["message"] == STOPPED
    # Pose queries continue after the hold.
    wait_for(lambda: rig.snapshot()["actual"][:4] == list(POSE))


def test_active_key_is_cleared_when_a_hold_ends_while_its_key_is_held(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    rig.hold(1).finish.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_active_key"] is None
    assert snapshot["keyboard_enabled"] is True


# Request validation and ownership

INVALID_KEYBOARD = [
    pytest.param(request(ARM, seq=MISSING), id="arm-without-seq"),
    pytest.param(request(ARM, speed=MISSING), id="arm-without-speed"),
    pytest.param(request(ARM, enabled=MISSING), id="without-enabled"),
    pytest.param(request(ARM, reason="user"), id="arm-with-reason"),
    pytest.param(request(ARM, held=[]), id="arm-with-held"),
    pytest.param(request(DISARM, seq=MISSING), id="disarm-without-seq"),
    pytest.param(request(DISARM, reason=MISSING), id="disarm-without-reason"),
    pytest.param(request(DISARM, reason=MISSING, speed=5), id="disarm-with-speed-only"),
    pytest.param(request(DISARM, speed=5), id="disarm-with-speed"),
    *[
        pytest.param(request(ARM, enabled=value), id=f"enabled-{value!r}")
        for value in (1, 0, "true", None)
    ],
    *[
        pytest.param(request(ARM, speed=value), id=f"speed-{value!r}")
        for value in (0, 101, -1, 5.0, 1.5, True, "5", None)
    ],
    *[
        pytest.param(request(ARM, seq=value), id=f"arm-seq-{value!r}")
        for value in (-1, 2**53 + 1, 1.0, True, False, "1", None)
    ],
    *[
        pytest.param(request(DISARM, seq=value), id=f"disarm-seq-{value!r}")
        for value in (-1, 2**53 + 1, 2.0, True)
    ],
    *[
        pytest.param(request(DISARM, reason=value), id=f"reason-{value!r}")
        for value in ("blur", "", "Focus", None, 1)
    ],
]


@pytest.mark.parametrize("payload", INVALID_KEYBOARD)
def test_invalid_keyboard_request_changes_nothing(rig, payload):
    rig.connect()
    with pytest.raises(ValueError):
        rig.state.command(payload, CLIENT)
    assert_not_armed(rig)
    # The rejected request used no sequence number, so seq 1 is still new.
    rig.state.command(ARM, CLIENT)
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])


@pytest.mark.parametrize("speed", [1, 100])
def test_speed_limits_are_accepted(rig, speed):
    rig.connect()
    session = rig.arm(speed)
    assert session.speed_percent == speed
    assert rig.snapshot()["keyboard_speed_percent"] == speed


def test_sequence_numbers_run_from_zero_to_two_to_the_53(rig):
    rig.connect()
    # Zero is valid, but it is never newer than the start value.
    snapshot = rig.state.command(request(ARM, seq=0), CLIENT)
    assert not snapshot["keyboard_arming"]
    assert rig.sessions == []
    rig.state.command(request(ARM, seq=2**53), CLIENT)
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    # No number is newer than the largest one.
    snapshot = rig.state.keys({"seq": 2**53, "held": ["W"]}, CLIENT)
    assert snapshot["keyboard_active_key"] is None
    assert rig.session.holds == []


DISARM_TEXT = [
    ("user", None),
    ("focus", "window lost focus."),
    ("edit", "target editing."),
    ("speed", "speed editing. Click Keyboard to apply the speed."),
    ("modifier", "shortcut modifier is active."),
]


@pytest.mark.parametrize(("reason", "text"), DISARM_TEXT)
def test_disarm_turns_keys_off_with_the_client_reason(rig, reason, text):
    rig.connect()
    session = rig.arm()
    snapshot = rig.browser.disarm(reason)
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_disarm_reason"] == text
    assert snapshot["message"] == (f"Keyboard off: {text}" if text else READY)
    assert "close" in session.calls
    assert not snapshot["locked"]
    assert rig.control.calls == []
    # A new arming clears the reason.
    rig.arm()
    assert rig.snapshot()["keyboard_disarm_reason"] is None


def test_disarm_while_keys_are_off_shows_no_reason(rig):
    rig.connect()
    message = rig.snapshot()["message"]
    snapshot = rig.browser.disarm("focus")
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_disarm_reason"] is None
    assert snapshot["message"] == message


INVALID_KEYS = [
    pytest.param({"seq": 2}, id="without-held"),
    pytest.param({"held": ["W"]}, id="without-seq"),
    pytest.param({"seq": 2, "held": ["W"], "speed": 5}, id="extra-field"),
    pytest.param({"seq": 2, "held": "W"}, id="held-string"),
    pytest.param({"seq": 2, "held": ("W",)}, id="held-tuple"),
    pytest.param({"seq": 2, "held": {"W": True}}, id="held-object"),
    pytest.param({"seq": 2, "held": None}, id="held-null"),
    pytest.param({"seq": 2, "held": ["w"]}, id="lowercase"),
    pytest.param({"seq": 2, "held": ["KeyW"]}, id="event-code"),
    pytest.param({"seq": 2, "held": ["Escape"]}, id="other-key"),
    pytest.param({"seq": 2, "held": ["W", "W"]}, id="duplicate"),
    pytest.param({"seq": 2, "held": ["W", 1]}, id="number"),
    pytest.param({"seq": 2, "held": [None]}, id="null-key"),
    pytest.param({"seq": 2, "held": [*ALL_KEYS, "W"]}, id="eleven-keys"),
    pytest.param({"seq": -1, "held": ["W"]}, id="negative-seq"),
    pytest.param({"seq": 2**53 + 1, "held": ["W"]}, id="large-seq"),
    pytest.param({"seq": 2.0, "held": ["W"]}, id="float-seq"),
    pytest.param({"seq": True, "held": ["W"]}, id="bool-seq"),
    pytest.param({"seq": "2", "held": ["W"]}, id="string-seq"),
    pytest.param([2, ["W"]], id="list-request"),
    pytest.param(None, id="null-request"),
]


@pytest.mark.parametrize("payload", INVALID_KEYS)
def test_invalid_key_input_changes_nothing(rig, payload):
    rig.connect()
    session = rig.arm()
    with pytest.raises(ValueError):
        rig.state.keys(payload, CLIENT)
    assert rig.snapshot()["keyboard_active_key"] is None
    assert session.holds == []
    # The rejected request used no sequence number, so seq 2 is still new.
    rig.state.keys({"seq": 2, "held": ["W"]}, CLIENT)
    assert rig.hold(1).key == "W"


def test_key_input_accepts_no_keys_and_all_ten_keys(rig):
    rig.connect()
    session = rig.arm()
    snapshot = rig.browser.keys(*ALL_KEYS)
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["busy"] is False
    assert rig.browser.keys()["keyboard_enabled"] is True
    assert session.holds == []


@pytest.mark.parametrize("client", [None, "", "   ", "x" * 129, 7])
def test_key_input_requires_a_browser_identity(rig, client):
    with pytest.raises(ValueError, match="client ID"):
        rig.state.keys({"seq": 1, "held": []}, client)


def test_request_errors_come_before_owner_checks(rig):
    rig.connect()
    with pytest.raises(ValueError):
        rig.state.keys({"seq": 1, "held": ["w"]}, OTHER)
    with pytest.raises(ValueError):
        rig.state.command(request(ARM, speed=0), OTHER)


def test_keyboard_requests_and_key_input_are_owner_only(rig):
    with pytest.raises(PermissionError):
        rig.state.command(ARM, CLIENT)
    with pytest.raises(PermissionError):
        rig.state.keys({"seq": 1, "held": ["W"]}, CLIENT)
    rig.connect()
    for payload in (request(ARM, seq=5), request(DISARM, seq=5)):
        with pytest.raises(PermissionError):
            rig.state.command(payload, OTHER)
    with pytest.raises(PermissionError):
        rig.state.keys({"seq": 5, "held": ["W"]}, OTHER)
    assert rig.sessions == []
    # Requests from another page used none of the owner's sequence numbers.
    session = rig.arm()
    with pytest.raises(PermissionError):
        rig.state.keys({"seq": 50, "held": ["W"]}, OTHER)
    with pytest.raises(PermissionError):
        rig.state.command(request(DISARM, seq=50), OTHER)
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["controller_id"] == CLIENT
    assert session.holds == []


def test_key_input_and_keyboard_requests_refresh_the_owner_lease(rig):
    rig.connect()
    for send in (rig.browser.keys, rig.browser.disarm):
        with rig.state._lock:
            rig.state._heartbeat -= rig.state.LEASE_SECONDS - 1.0
        send()
        with rig.state._lock:
            assert time.monotonic() - rig.state._heartbeat < 1.0


def test_key_input_after_lease_expiry_turns_keys_off_and_is_refused(rig):
    rig.connect()
    session = rig.arm()
    with rig.state._lock:
        rig.state._heartbeat -= rig.state.LEASE_SECONDS + 1
        # Key input must not revive the lease.
        with pytest.raises(PermissionError):
            rig.browser.keys("W")
        snapshot = rig.state.snapshot()
    assert snapshot["controller_id"] is None
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_disarm_reason"] == "browser control lease expired."
    assert snapshot["locked"] is True
    assert session.holds == []


# Arming preconditions. A refused arming builds no session.


def test_arm_refuses_locked_control(rig):
    rig.connect()
    rig.browser.command("stop")
    rig.idle()
    with pytest.raises(RuntimeError, match="locked"):
        rig.browser.arm()
    assert_not_armed(rig)


def test_arm_after_lease_expiry_is_refused(rig):
    rig.connect()
    with rig.state._lock:
        rig.state._heartbeat -= rig.state.LEASE_SECONDS + 1
        with pytest.raises((PermissionError, RuntimeError)):
            rig.browser.arm()
    assert_not_armed(rig)
    assert rig.snapshot()["controller_id"] is None


def test_arm_refuses_stale_feedback(rig):
    rig.connect()
    with rig.state._lock:
        rig.state._received -= 1
        with pytest.raises(RuntimeError, match="stale"):
            rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_feedback_that_reports_an_error(rig):
    rig.connect()
    with rig.state._lock:
        rig.state._sample = Feedback((1, 2, 3, 4), 5, 0, True)
        rig.state._received = time.monotonic()
        with pytest.raises(RuntimeError, match="error"):
            rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_while_a_job_runs(rig):
    rig.connect()
    rig.control.release_move.clear()
    rig.browser.command("home")
    assert rig.control.move_entered.wait(1)
    with pytest.raises(RuntimeError, match=BUSY):
        rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_while_a_stop_runs(rig):
    rig.connect()
    entered, release = threading.Event(), threading.Event()

    def stop():
        entered.set()
        release.wait(2)
        return {"ok": True}

    rig.control.stop = stop
    try:
        rig.browser.command("stop")
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="locked"):
            rig.browser.arm()
        assert_not_armed(rig)
    finally:
        release.set()


@pytest.mark.parametrize("field", ["_safety_workers", "_gripper_active"])
def test_arm_refuses_while_a_safety_worker_or_gripper_command_runs(rig, field):
    rig.connect()
    # A stop normally also locks the panel. Set only this condition, so its own
    # check refuses the arm.
    with rig.state._lock:
        setattr(rig.state, field, 1 if field == "_safety_workers" else True)
    try:
        with pytest.raises(RuntimeError, match=BUSY):
            rig.browser.arm()
        assert_not_armed(rig)
    finally:
        with rig.state._lock:
            setattr(rig.state, field, 0 if field == "_safety_workers" else False)


def test_arm_refuses_while_a_keyboard_worker_still_runs(rig):
    rig.connect()
    session = rig.arm()
    session.stop_gate.clear()
    rig.browser.keys("W")
    hold = rig.hold(1)
    snapshot = rig.browser.disarm("focus")
    assert hold.released.is_set()
    assert snapshot["busy"] is True
    assert snapshot["keyboard_enabled"] is False
    with pytest.raises(RuntimeError, match=BUSY):
        rig.browser.arm()
    # Jobs also wait for the keyboard worker, although keys are OFF.
    with pytest.raises(RuntimeError, match=BUSY):
        rig.browser.command("home")
    assert len(rig.sessions) == 1
    assert rig.control.calls == []
    session.stop_gate.set()
    rig.idle()
    rig.arm()


@pytest.mark.parametrize("mode", [4, 7])
def test_arm_requires_an_enabled_idle_robot(rig, mode):
    rig.feed.sample = Feedback((1, 2, 3, 4), mode, 0, False)
    rig.connect()
    with pytest.raises(RuntimeError, match="Enable the robot before keyboard control"):
        rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_a_locked_gripper(make_rig):
    gripper = Gripper()
    gripper.locked = True
    rig = make_rig(gripper=gripper)
    rig.connect()
    with pytest.raises(RuntimeError, match="Gripper control is locked"):
        rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_joints_outside_the_allowed_range(rig):
    rig.feed.sample = Feedback((170, 2, 3, 4), 5, 0, False)
    rig.connect()
    with pytest.raises(RuntimeError, match="Keyboard unavailable: J1=170"):
        rig.browser.arm()
    assert_not_armed(rig)


def test_arm_refuses_control_that_must_keep_feedback(rig):
    rig.connect()
    rig.control.retention = "A controller acknowledgement is pending."
    with pytest.raises(
        RuntimeError, match="Keyboard unavailable: A controller acknowledgement is pending."
    ):
        rig.browser.arm()
    assert_not_armed(rig)


# Arming and preparation


def test_arm_prepares_the_session_on_a_worker_and_turns_keys_on(rig):
    rig.connect()
    wait_for(lambda: rig.snapshot()["actual"][:4] == list(POSE))
    rig.prepare_gate.clear()
    snapshot = rig.browser.arm(37)
    assert snapshot["keyboard_arming"] is True
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["message"] == CHECKING
    assert snapshot["keyboard_speed_percent"] == 37
    assert snapshot["busy"] is True
    assert snapshot["actual"][:4] == [None] * 4
    [session] = rig.sessions
    assert session.control is rig.control
    assert session.speed_percent == 37
    assert session.prepare_entered.wait(1)
    assert session.prepare_thread is not threading.current_thread()
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    snapshot = rig.snapshot()
    assert snapshot["keyboard_arming"] is False
    assert snapshot["message"] == READY
    assert snapshot["busy"] is False
    assert snapshot["actual"][:4] == list(POSE)
    assert session.calls == ["prepare"]
    assert rig.control.calls == []


def test_preparation_waits_for_an_in_flight_pose_query_and_stops_polls(make_rig):
    rig = make_rig(gripper=Gripper())
    rig.connect()
    control = rig.control
    block_pose_query(control)
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    # The preparation must not share the dashboard with a running pose query.
    assert not session.prepare_entered.wait(0.15)
    control.release_pose.set()
    assert session.prepare_entered.wait(1)
    reads, polls = control.pose_reads, rig.gripper.polls
    # Let the panel worker run two more cycles during the preparation.
    wait_for(lambda: rig.gripper.polls >= polls + 2)
    assert control.pose_reads == reads
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    # Pose queries continue while keys are ON and idle.
    wait_for(lambda: control.pose_reads > reads)


def test_arm_while_on_or_arming_keeps_the_session_speed(rig):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm(5)
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    assert rig.browser.arm(5)["keyboard_arming"] is True
    with pytest.raises(RuntimeError, match=r"Keyboard is on at 5%\. Turn it off to change"):
        rig.browser.arm(6)
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    assert rig.browser.arm(5)["keyboard_enabled"] is True
    with pytest.raises(RuntimeError, match=r"Keyboard is on at 5%\. Turn it off to change"):
        rig.browser.arm(100)
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["keyboard_speed_percent"] == 5
    assert rig.sessions == [session]
    assert session.calls == ["prepare"]


def test_deferred_preparation_turns_keys_off_without_a_stop(rig):
    rig.connect()
    rig.prepare_error = PreparationRejectedError("Robot is not at rest.")
    rig.browser.arm()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_arming"] is False
    assert snapshot["message"] == (
        "Keyboard preparation deferred: Robot is not at rest. Click Keyboard to retry."
    )
    assert snapshot["locked"] is False
    assert rig.control.calls == []
    assert "close" in rig.session.calls
    rig.prepare_error = None
    rig.arm()


def test_failed_current_preparation_reports_the_error_without_a_stop(rig):
    rig.connect()
    rig.prepare_error = RuntimeError("Keyboard speed feedback was not confirmed")
    rig.browser.arm()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_arming"] is False
    assert snapshot["message"] == "ERROR: Keyboard speed feedback was not confirmed"
    # The parent control was not cancelled, so no stop is needed.
    assert snapshot["locked"] is False
    assert rig.control.calls == []
    assert "close" in rig.session.calls


def test_faulted_preparation_stops_and_locks_until_a_verified_disable(rig):
    rig.connect()
    rig.prepare_fault = "Keyboard speed feedback does not confirm SpeedFactor(5)"
    rig.browser.arm(5)
    first = rig.control
    wait_for(lambda: "stop" in first.calls)
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["locked"] is True
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_arming"] is False
    assert "close" in rig.session.calls
    with pytest.raises(RuntimeError, match="locked"):
        rig.browser.arm(5)
    assert len(rig.sessions) == 1
    rig.browser.command("disable")
    wait_for(lambda: len(rig.controls) == 2 and not rig.snapshot()["locked"])
    assert first.calls[-2:] == ["disable", "stationary"]
    rig.prepare_fault = None
    session = rig.arm(5)
    assert session.control is rig.controls[1]


def test_disarm_while_preparation_waits_for_a_pose_query_never_prepares(rig):
    rig.connect()
    block_pose_query(rig.control)
    rig.browser.arm()
    [session] = rig.sessions
    snapshot = rig.browser.disarm("focus")
    assert snapshot["keyboard_arming"] is False
    # The worker leaves its wait although the pose query still runs.
    rig.idle()
    rig.control.release_pose.set()
    snapshot = rig.snapshot()
    assert "prepare" not in session.calls
    assert "close" in session.calls
    assert snapshot["message"] == "Keyboard off: window lost focus."
    assert snapshot["keyboard_disarm_reason"] == "window lost focus."
    assert snapshot["locked"] is False
    assert rig.control.calls == []


@pytest.mark.parametrize("pending", [False, True])
def test_disarm_during_preparation_closes_the_session_only_without_a_pending_command(rig, pending):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    rig.control.command_pending = pending
    snapshot = rig.browser.disarm("speed")
    assert snapshot["keyboard_arming"] is False
    assert snapshot["keyboard_enabled"] is False
    if pending:
        # Cancelling now could leave the SpeedFactor ordering unknown.
        assert session.calls == ["prepare"]
        assert snapshot["busy"] is True
        with pytest.raises(RuntimeError, match=BUSY):
            rig.browser.arm()
        rig.control.command_pending = False
        rig.prepare_gate.set()
    else:
        assert session.calls[:2] == ["prepare", "close"]
    rig.idle()
    snapshot = rig.snapshot()
    assert "close" in session.calls
    assert "release" not in session.calls
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["message"] == "Keyboard off: speed editing. Click Keyboard to apply the speed."
    assert snapshot["locked"] is False
    assert rig.control.calls == []
    assert len(rig.sessions) == 1


def test_control_send_guard_refuses_a_preparation_after_its_disarm(rig):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    control = rig.control
    # The current preparation may send SpeedFactor.
    control.send_guard()
    # Keep the session open, so the worker stays in prepare().
    control.command_pending = True
    rig.browser.disarm("focus")
    with pytest.raises(PreparationRejectedError, match="Keyboard preparation cancelled."):
        control.send_guard()
    control.command_pending = False
    rig.prepare_gate.set()
    rig.idle()
    # Later commands use the normal guard again.
    control.send_guard()
    assert rig.snapshot()["locked"] is False


def test_watchdog_cancels_arming_with_a_reason(rig):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    rig.feed.sample = Feedback((170, 2, 3, 4), 5, 0, False)
    wait_for(lambda: not rig.snapshot()["keyboard_arming"])
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_disarm_reason"] == "joint feedback is outside the allowed range."
    assert snapshot["message"] == "Keyboard off: joint feedback is outside the allowed range."
    assert "close" in session.calls


@pytest.mark.parametrize("mode", [4, 7])
def test_current_preparation_that_cannot_be_applied_turns_keys_off(rig, mode):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    rig.feed.sample = Feedback((1, 2, 3, 4), mode, 0, False)
    wait_for(lambda: rig.snapshot()["robot_mode"] == mode)
    # The idle-mode rule applies only while keys are ON.
    assert rig.snapshot()["keyboard_arming"] is True
    rig.prepare_gate.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_arming"] is False
    assert snapshot["keyboard_disarm_reason"] == "robot state changed during preparation."
    assert snapshot["message"] == "Keyboard off: robot state changed during preparation."
    assert "close" in session.calls
    assert snapshot["locked"] is False


# One sequence for arm, disarm and key input


def test_arm_disarm_and_key_input_share_one_sequence(rig):
    rig.connect()
    state = rig.state

    def arm(seq):
        return state.command(request(ARM, seq=seq), CLIENT)

    def disarm(seq):
        return state.command(request(DISARM, seq=seq), CLIENT)

    def keys(seq, *held):
        return state.keys({"seq": seq, "held": list(held)}, CLIENT)

    arm(10)
    wait_for(lambda: state.snapshot()["keyboard_enabled"])
    [session] = rig.sessions
    assert keys(9, "W")["keyboard_active_key"] is None
    assert keys(10, "W")["keyboard_active_key"] is None
    assert disarm(10)["keyboard_enabled"] is True
    assert disarm(4)["keyboard_enabled"] is True
    assert session.holds == []
    keys(11, "W")
    hold = rig.hold(1)
    # A duplicate release is stale.
    assert keys(11)["keyboard_active_key"] == "W"
    assert not hold.released.is_set()
    keys(12)
    assert hold.released.is_set()
    rig.idle()
    assert disarm(13)["keyboard_enabled"] is False
    # An arming request older than the disarm is stale.
    snapshot = arm(12)
    assert snapshot["keyboard_arming"] is False
    assert snapshot["keyboard_enabled"] is False
    assert len(rig.sessions) == 1
    arm(14)
    wait_for(lambda: state.snapshot()["keyboard_enabled"])
    assert len(rig.sessions) == 2


def test_stale_arm_after_a_newer_disarm_during_checking_is_ignored(rig):
    rig.connect()
    rig.prepare_gate.clear()
    rig.state.command(request(ARM, seq=1), CLIENT)
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    assert rig.state.command(request(DISARM, seq=3), CLIENT)["keyboard_arming"] is False
    rig.idle()
    snapshot = rig.state.command(request(ARM, seq=2), CLIENT)
    assert snapshot["keyboard_arming"] is False
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["message"] == "Keyboard off: window lost focus."
    assert len(rig.sessions) == 1


@pytest.mark.parametrize("client", [CLIENT, OTHER])
def test_sequence_restarts_only_for_a_new_owner_page(rig, client):
    rig.connect()
    rig.state.command(request(ARM, seq=50), CLIENT)
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    rig.state.command({"action": "stop_sync"}, CLIENT)
    wait_for(lambda: rig.snapshot()["controller_id"] is None and not rig.snapshot()["busy"])
    rig.state.command({"action": "disable"}, client)
    wait_for(lambda: not rig.snapshot()["locked"])
    start_sync(rig, client)
    assert rig.snapshot()["controller_id"] == client
    snapshot = rig.state.command(request(ARM, seq=1), client)
    if client == OTHER:
        assert snapshot["keyboard_arming"] or snapshot["keyboard_enabled"]
        assert len(rig.sessions) == 2
    else:
        # The same page keeps counting, so its old numbers stay stale.
        assert snapshot["keyboard_arming"] is False
        assert len(rig.sessions) == 1
        rig.state.command(request(ARM, seq=51), client)
        assert len(rig.sessions) == 2
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])


# Dispatch: one hold at a time (Isaac KeyboardController semantics)


def test_keys_while_off_start_nothing(rig):
    rig.connect()
    for held in (["W"], ["W", "S"], [], ["Z"], ["A"]):
        snapshot = rig.browser.keys(*held)
        assert snapshot["busy"] is False
        assert snapshot["keyboard_active_key"] is None
    assert rig.sessions == []
    assert rig.control.calls == []


def test_key_held_during_arming_needs_a_new_press_after_keys_turn_on(rig):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    rig.browser.keys("W")
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])
    snapshot = rig.browser.keys("W")
    assert snapshot["busy"] is False
    assert snapshot["keyboard_active_key"] is None
    assert session.holds == []
    rig.browser.keys()
    rig.browser.keys("W")
    assert rig.hold(1).key == "W"


def test_arm_forgets_keys_reported_before_arming(rig):
    rig.connect()
    rig.browser.keys("W")
    session = rig.arm()
    assert session.holds == []
    # The page reports W again only after a new keydown.
    rig.browser.keys("W")
    assert rig.hold(1).key == "W"


def test_direction_change_waits_for_the_stop(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    first = rig.hold(1)
    session.stop_gate.clear()
    snapshot = rig.browser.keys("S")
    assert first.released.is_set()
    assert "release" in session.calls
    assert snapshot["keyboard_active_key"] is None
    assert snapshot["busy"] is True
    assert len(session.holds) == 1
    session.stop_gate.set()
    second = rig.hold(2)
    assert second.key == "S"
    assert not second.released.is_set()
    assert rig.snapshot()["keyboard_active_key"] == "S"


def test_release_and_new_press_while_stopping_starts_a_fresh_hold(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("W")
    rig.browser.keys("W")
    assert len(session.holds) == 1
    session.stop_gate.set()
    second = rig.hold(2)
    assert second.key == "W"
    assert not second.released.is_set()


def test_key_released_before_its_turn_never_starts(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("S")
    rig.browser.keys()
    session.stop_gate.set()
    rig.idle()
    assert [hold.key for hold in session.holds] == ["W"]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["keyboard_active_key"] is None


def test_only_the_remaining_fresh_key_starts_after_the_stop(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("S")
    rig.browser.keys()
    rig.browser.keys("A")
    session.stop_gate.set()
    assert rig.hold(2).key == "A"
    rig.browser.keys()
    rig.idle()
    assert [hold.key for hold in session.holds] == ["W", "A"]


def test_second_key_stops_the_hold_and_starts_nothing_until_one_key_remains(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    first = rig.hold(1)
    rig.browser.keys("W", "S")
    assert first.released.is_set()
    assert "release" in session.calls
    rig.idle()
    assert len(session.holds) == 1
    rig.browser.keys("S")
    assert rig.hold(2).key == "S"


def test_releasing_the_second_key_does_not_restart_a_consumed_key(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    rig.browser.keys("W", "S")
    rig.browser.keys("W")
    rig.idle()
    assert rig.browser.keys("W")["busy"] is False
    assert len(session.holds) == 1
    rig.browser.keys()
    rig.browser.keys("W")
    assert rig.hold(2).key == "W"


def test_completed_hold_needs_a_new_press(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1).finish.set()
    rig.idle()
    assert rig.browser.keys("W")["busy"] is False
    assert len(session.holds) == 1
    rig.browser.keys()
    rig.browser.keys("W")
    assert rig.hold(2).key == "W"


def test_disarm_clears_held_and_pending_keys(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    first = rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("S")
    rig.browser.disarm()
    assert first.released.is_set()
    session.stop_gate.set()
    rig.idle()
    assert len(session.holds) == 1
    second = rig.arm()
    assert second.holds == []
    rig.browser.keys("S")
    assert rig.hold(1, second).key == "S"


def test_disarm_during_a_hold_releases_it_and_closes_the_session_after_it_ends(rig):
    rig.connect()
    session = rig.arm()
    session.stop_gate.clear()
    rig.browser.keys("W")
    hold = rig.hold(1)
    snapshot = rig.browser.disarm("edit")
    assert hold.released.is_set()
    assert "release" in session.calls
    assert "close" not in session.calls
    assert snapshot["message"] == "Keyboard off: target editing."
    session.stop_gate.set()
    rig.idle()
    assert "close" in session.calls
    assert rig.snapshot()["message"] == "Keyboard off: target editing."


def test_start_conditions_are_checked_again_before_a_pending_key_starts(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("S")
    rig.feed.sample = Feedback((1, 2, 3, 4), 7, 0, False)
    wait_for(lambda: rig.snapshot()["robot_mode"] == 7)
    session.stop_gate.set()
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    rig.idle()
    assert [hold.key for hold in session.holds] == ["W"]
    assert rig.snapshot()["keyboard_disarm_reason"] == "the robot is not enabled and idle."


def test_hold_waits_for_an_in_flight_pose_query(rig):
    rig.connect()
    session = rig.arm()
    block_pose_query(rig.control)
    snapshot = rig.browser.keys("W")
    assert snapshot["keyboard_active_key"] is None
    assert session.holds == []
    rig.control.release_pose.set()
    assert rig.hold(1).key == "W"


@pytest.mark.parametrize("revoke", ["release", "disarm"])
def test_hold_revoked_before_its_worker_runs_never_calls_the_session(rig, revoke):
    rig.connect()
    session = rig.arm()
    with lock_between_pose_queries(rig):
        # The hold is registered, but its worker waits for this lock.
        assert rig.browser.keys("W")["keyboard_active_key"] == "W"
        if revoke == "release":
            rig.browser.keys()
        else:
            rig.browser.disarm("focus")
    rig.idle()
    assert session.holds == []
    assert ("run", "W") not in session.calls
    assert rig.snapshot()["keyboard_enabled"] is (revoke == "release")


def test_press_with_a_session_that_is_not_ready_turns_keys_off(rig):
    rig.connect()
    session = rig.arm()
    session.prepared = False
    rig.browser.keys("W")
    # A pose query in flight can delay the start check.
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    snapshot = rig.snapshot()
    assert snapshot["keyboard_disarm_reason"] == "session is not ready. Click Keyboard to retry."
    assert session.holds == []


# Dead-man and passive disarm rules


def test_dead_man_releases_the_hold_when_key_updates_stop(rig):
    rig.connect()
    session = rig.arm()
    rig.state.KEY_LEASE_SECONDS = 0.15
    pressed = time.monotonic()
    rig.browser.keys("W")
    hold = rig.hold(1)
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    assert time.monotonic() - pressed >= 0.15
    assert hold.released.is_set()
    assert "release" in session.calls
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_disarm_reason"] == DEAD_MAN
    assert snapshot["message"] == f"Keyboard off: {DEAD_MAN}"
    assert snapshot["locked"] is False


def test_newer_key_updates_keep_the_hold_and_duplicates_do_not(rig):
    rig.connect()
    rig.arm()
    rig.state.KEY_LEASE_SECONDS = 0.3
    rig.browser.keys("W")
    hold = rig.hold(1)
    end = time.monotonic() + 0.7
    while time.monotonic() < end:
        rig.browser.keys("W")
        time.sleep(0.01)
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["keyboard_active_key"] == "W"
    assert not hold.released.is_set()
    duplicate = {"seq": rig.browser.seq, "held": ["W"]}

    def renew_with_duplicate():
        return not rig.state.keys(duplicate, CLIENT)["keyboard_enabled"]

    wait_for(renew_with_duplicate)
    assert hold.released.is_set()
    assert rig.snapshot()["keyboard_disarm_reason"] == DEAD_MAN


def test_dead_man_covers_held_keys_without_a_hold(rig):
    rig.connect()
    session = rig.arm()
    rig.state.KEY_LEASE_SECONDS = 0.15
    rig.browser.keys("W", "S")
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    assert rig.snapshot()["keyboard_disarm_reason"] == DEAD_MAN
    assert session.holds == []


def test_dead_man_ignores_keys_that_are_on_with_no_key_held(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    rig.browser.keys()
    rig.idle()
    rig.state.KEY_LEASE_SECONDS = 0.05
    rig.state.LEASE_SECONDS = 0.4
    # Many key leases pass before the browser lease expires. Only the browser
    # lease may turn these idle keys off.
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    assert rig.snapshot()["keyboard_disarm_reason"] == "browser control lease expired."


def test_can_start_refuses_a_press_that_was_not_renewed(make_rig):
    rig = make_rig(watch=False)
    rig.connect()
    session = rig.arm()
    rig.state.KEY_LEASE_SECONDS = 0.1
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("A")
    renewed = time.monotonic()
    # No watchdog runs, so only the start check can notice the stale press.
    wait_for(lambda: time.monotonic() - renewed > 0.15)
    session.stop_gate.set()
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    rig.idle()
    snapshot = rig.snapshot()
    assert [hold.key for hold in session.holds] == ["W"]
    assert snapshot["keyboard_disarm_reason"] == DEAD_MAN


def test_can_start_refuses_a_pending_press_after_the_browser_lease_lapsed(make_rig):
    rig = make_rig(gripper=Gripper(), watch=False)
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("A")
    with rig.state._lock:
        rig.state._heartbeat -= rig.state.LEASE_SECONDS + 1
    session.stop_gate.set()
    rig.idle()
    # The panel worker also retries the start after each of its cycles.
    settle(rig)
    assert [hold.key for hold in session.holds] == ["W"]


def test_can_start_refuses_a_pending_press_while_the_gripper_is_locked(make_rig):
    gripper = Gripper()
    rig = make_rig(gripper=gripper, watch=False)
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("A")
    gripper.locked = True
    wait_for(lambda: rig.snapshot()["locked"])
    session.stop_gate.set()
    rig.idle()
    settle(rig)
    assert [hold.key for hold in session.holds] == ["W"]


def test_key_input_is_accepted_while_control_is_locked_or_feedback_is_stale(rig):
    rig.connect()
    session = rig.arm()
    with rig.state._lock:
        rig.state._received -= 1
        # Stale feedback starts no hold, but key input is still accepted.
        assert rig.browser.keys("W")["keyboard_active_key"] is None
        rig.browser.keys()
    rig.browser.command("stop")
    rig.idle()
    snapshot = rig.browser.keys("W")
    assert snapshot["locked"] is True
    assert snapshot["keyboard_active_key"] is None
    assert rig.browser.keys()["keyboard_enabled"] is False
    assert session.holds == []


def stop_sync_flag(rig):
    with rig.state._lock:
        rig.state._syncing = False


def stop_feedback(rig):
    rig.feed.read.clear()


def report_error(rig):
    rig.feed.sample = Feedback((1, 2, 3, 4), 5, 0, True)


def stop_control(rig):
    rig.control.stopped = True


def lock_gripper(rig):
    rig.gripper.locked = True


def leave_joint_range(rig):
    rig.feed.sample = Feedback((170, 2, 3, 4), 5, 0, False)


def disable_robot(rig):
    rig.feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)


def move_robot(rig):
    rig.feed.sample = Feedback((1, 2, 3, 4), 7, 0, False)


STALE = "robot feedback is stale, unavailable, or reports a fault."
OUT_OF_RANGE = "joint feedback is outside the allowed range."
NOT_IDLE = "the robot is not enabled and idle."
# The error and stale rules can also request a stop, which replaces the message.
PASSIVE_RULES = [
    pytest.param(stop_sync_flag, "synchronization stopped.", True, id="sync"),
    pytest.param(stop_feedback, STALE, False, id="stale"),
    pytest.param(report_error, STALE, False, id="error"),
    pytest.param(stop_control, "robot control is stopped.", True, id="control-stopped"),
    pytest.param(lock_gripper, "gripper control is locked.", True, id="gripper-locked"),
    pytest.param(leave_joint_range, OUT_OF_RANGE, True, id="joints"),
    pytest.param(disable_robot, NOT_IDLE, True, id="mode-4"),
    pytest.param(move_robot, NOT_IDLE, True, id="mode-7"),
]


@pytest.mark.parametrize(("change", "reason", "shown"), PASSIVE_RULES)
def test_watchdog_turns_keys_off_with_a_reason(make_rig, change, reason, shown):
    rig = make_rig(gripper=Gripper())
    rig.connect()
    session = rig.arm()
    change(rig)
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    snapshot = rig.snapshot()
    assert snapshot["keyboard_disarm_reason"] == reason
    if shown:
        assert snapshot["message"] == f"Keyboard off: {reason}"
    assert "close" in session.calls


def test_moving_robot_during_a_hold_does_not_trip_the_idle_rule(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    hold = rig.hold(1)
    move_robot(rig)
    wait_for(lambda: rig.snapshot()["robot_mode"] == 7)
    rig.state.KEY_LEASE_SECONDS = 0.2
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
    # Only the later dead-man ends this hold.
    assert rig.snapshot()["keyboard_disarm_reason"] == DEAD_MAN
    assert hold.released.is_set()


# Jobs and keyboard activity

JOBS = [
    pytest.param({"action": "enable"}, id="enable"),
    pytest.param({"action": "home"}, id="home"),
    pytest.param({"action": "execute", "targets": TARGETS}, id="execute"),
    pytest.param({"action": "grip", "position": 500}, id="grip"),
    pytest.param({"action": "initialize_gripper"}, id="initialize_gripper"),
]


@pytest.mark.parametrize("job", JOBS)
def test_jobs_are_refused_while_a_preparation_runs(rig, job):
    rig.connect()
    rig.prepare_gate.clear()
    rig.browser.arm()
    [session] = rig.sessions
    assert session.prepare_entered.wait(1)
    with pytest.raises(RuntimeError, match=BUSY):
        rig.state.command(job, CLIENT)
    assert rig.snapshot()["keyboard_arming"] is True
    assert session.calls == ["prepare"]
    assert rig.control.calls == []
    rig.prepare_gate.set()
    wait_for(lambda: rig.snapshot()["keyboard_enabled"])


@pytest.mark.parametrize("job", JOBS)
def test_jobs_are_refused_while_a_hold_runs(rig, job):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    hold = rig.hold(1)
    with pytest.raises(RuntimeError, match=BUSY):
        rig.state.command(job, CLIENT)
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["keyboard_active_key"] == "W"
    assert not hold.released.is_set()
    assert rig.control.calls == []


@pytest.mark.parametrize(
    ("job", "call"),
    [
        ({"action": "enable"}, "enable"),
        ({"action": "home"}, ("joint", (0.0, 0.0, 0.0, 0.0))),
        ({"action": "execute", "targets": TARGETS}, ("linear", (250.0, 0.0, 30.0, 0.0))),
    ],
)
def test_job_while_keys_are_on_and_idle_turns_keys_off_silently(rig, job, call):
    rig.connect()
    session = rig.arm()
    snapshot = rig.state.command(job, CLIENT)
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["keyboard_disarm_reason"] is None
    assert "Keyboard off" not in snapshot["message"]
    assert "close" in session.calls
    rig.idle()
    assert rig.control.calls == [call]
    assert "Keyboard off" not in rig.snapshot()["message"]


def test_refused_job_leaves_keys_on(rig):
    rig.connect()
    session = rig.arm()
    with pytest.raises(RuntimeError, match="Gripper control is unavailable"):
        rig.state.command({"action": "execute", "targets": {**TARGETS, "grip": 500}}, CLIENT)
    with pytest.raises(RuntimeError, match="Gripper initialization is unavailable"):
        rig.state.command({"action": "initialize_gripper"}, CLIENT)
    assert rig.snapshot()["keyboard_enabled"] is True
    assert "close" not in session.calls
    rig.browser.keys("W")
    assert rig.hold(1).key == "W"


# Hold outcomes


def test_released_arm_hold_reports_a_normal_stop(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    rig.hold(1)
    rig.browser.keys()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["message"] == STOPPED
    assert "close" not in session.calls


def test_finite_hold_reports_its_measured_pose(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    hold = rig.hold(1)
    assert rig.snapshot()["actual"][:4] == [None] * 4
    # Block later pose queries, so only the hold result can fill the pose.
    rig.control.release_pose.clear()
    hold.pose = (260, 1, 31, 2)
    hold.finish.set()
    rig.idle()
    assert rig.snapshot()["actual"][:4] == [260, 1, 31, 2]


def test_limit_stop_keeps_keys_on_and_reports_the_limit(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    hold = rig.hold(1)
    hold.limit_reason = "J2=84 degrees is at the keyboard margin."
    hold.finish.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["message"] == (
        "J2=84 degrees is at the keyboard margin. Release the key and choose another direction."
    )


def test_failed_hold_turns_keys_off_drops_pending_keys_and_needs_a_new_arming(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("W")
    first = rig.hold(1)
    session.stop_gate.clear()
    rig.browser.keys()
    rig.browser.keys("S")
    first.error = RuntimeError("Stop was not confirmed")
    session.stop_gate.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert [hold.key for hold in session.holds] == ["W"]
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["message"] == "ERROR: Stop was not confirmed"
    assert snapshot["keyboard_disarm_reason"] is None
    assert snapshot["locked"] is False
    assert "close" in session.calls
    second = rig.arm()
    assert second.holds == []
    rig.browser.keys("S")
    assert rig.hold(1, second).key == "S"


def test_failed_hold_reports_the_session_fault(rig):
    rig.connect()
    rig.arm()
    rig.browser.keys("W")
    hold = rig.hold(1)
    hold.fault = "Keyboard jogging speed changed"
    hold.error = RuntimeError("Keyboard hold failed")
    hold.finish.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    assert snapshot["message"] == "ERROR: Keyboard jogging speed changed"


def test_faulted_hold_that_cancelled_the_parent_stops_and_locks(rig):
    rig.connect()
    rig.arm()
    first = rig.control
    rig.browser.keys("W")
    hold = rig.hold(1)
    hold.fault = "Keyboard stop unconfirmed: no reply"
    hold.cancel_parent = True
    hold.error = RuntimeError(hold.fault)
    hold.finish.set()
    wait_for(lambda: "stop" in first.calls)
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["locked"] is True
    assert snapshot["keyboard_enabled"] is False
    rig.browser.command("disable")
    wait_for(lambda: len(rig.controls) == 2 and not rig.snapshot()["locked"])


def test_gripper_key_without_a_gripper_keeps_arm_keys_ready(rig):
    rig.connect()
    session = rig.arm()
    rig.browser.keys("Z")
    wait_for(lambda: rig.snapshot()["message"].startswith("Gripper unavailable"))
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["message"] == (
        "Gripper unavailable: Gripper control is unavailable. Arm keys remain ready."
    )
    assert snapshot["keyboard_enabled"] is True
    assert session.holds == []
    rig.browser.keys()
    rig.browser.keys("W")
    assert rig.hold(1).key == "W"


def test_gripper_step_keeps_the_arm_pose_visible(make_rig):
    gripper = Gripper(position=500)
    rig = make_rig(gripper=gripper)
    rig.connect()
    wait_for(lambda: rig.snapshot()["actual"][:4] == list(POSE))
    session = rig.arm()
    gripper.release_move.clear()
    rig.browser.keys("Z")
    assert gripper.move_entered.wait(1)
    snapshot = rig.snapshot()
    assert snapshot["busy"] is True
    assert snapshot["keyboard_active_key"] == "Z"
    assert snapshot["actual"][:4] == list(POSE)
    assert snapshot["actual"][4] is None
    rig.browser.keys()
    gripper.release_move.set()
    rig.idle()
    snapshot = rig.snapshot()
    # Z opens by one step.
    assert gripper.commands == [525]
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["message"] == STOPPED
    assert snapshot["actual"][4] == 525
    assert session.holds == []


def test_gripper_contact_keeps_keys_on_and_needs_a_new_press(make_rig):
    gripper = Gripper(position=500)
    gripper.outcome = "contact"
    rig = make_rig(gripper=gripper)
    rig.connect()
    rig.arm()
    rig.browser.keys("X")
    # X closes by one step and stops at contact.
    wait_for(lambda: gripper.commands == [475])
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is True
    assert snapshot["message"].endswith("Release the key before pressing again.")
    assert rig.browser.keys("X")["busy"] is False
    settle(rig)
    assert gripper.commands == [475]
    rig.browser.keys()
    rig.browser.keys("X")
    wait_for(lambda: gripper.commands == [475, 475])


def test_gripper_hold_limit_keeps_keys_on(make_rig):
    gripper = Gripper(position=500)
    # Each step ends without a new measured position, so the hold limit ends the hold.
    gripper.outcome = "stalled"
    rig = make_rig(gripper=gripper)
    rig.state.GRIPPER_HOLD_SECONDS = 0.1
    rig.connect()
    rig.arm()
    rig.browser.keys("Z")
    limit = "Hold limit reached. Release the key before pressing again."
    wait_for(lambda: rig.snapshot()["message"] == limit)
    rig.idle()
    assert gripper.commands
    assert rig.snapshot()["keyboard_enabled"] is True


def test_gripper_hold_released_before_its_worker_runs_never_steps(make_rig):
    gripper = Gripper()
    rig = make_rig(gripper=gripper)
    rig.connect()
    rig.arm()
    with lock_between_pose_queries(rig):
        assert rig.browser.keys("Z")["keyboard_active_key"] == "Z"
        rig.browser.keys()
    rig.idle()
    assert gripper.closed_checks == 0
    assert gripper.commands == []
    assert not gripper.move_entered.is_set()


def test_unavailable_gripper_step_after_keys_turned_off_keeps_the_off_message(make_rig):
    gripper = Gripper()
    rig = make_rig(gripper=gripper)
    rig.connect()
    rig.arm()
    gripper.release_move.clear()
    rig.browser.keys("Z")
    assert gripper.move_entered.wait(1)
    rig.browser.disarm("focus")
    gripper.move_error = GripperNotSentError("No command sent. The panel cancelled the command.")
    gripper.release_move.set()
    rig.idle()
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] is False
    # Keys are no longer ON, so the "Gripper unavailable" outcome sets no message.
    assert snapshot["message"] == "Keyboard off: window lost focus."
