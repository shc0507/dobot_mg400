import socket
import threading
import time
from unittest.mock import Mock

import pytest

from mg400_agent.controller_ownership import ControllerOwnership
from mg400_agent.feedback import Feedback
from mg400_agent.joint_control import JointControl, PreparationRejectedError, TargetRejectedError
from mg400_agent.web_control import RobotPanelState


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


class Feed:
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
    def __init__(self, host, feedback_snapshot, *, send_guard):
        self.feedback = feedback_snapshot
        self.send_guard = send_guard
        self.calls = []
        self.stopped = False
        self.pose = (250, 0, 30, 0)
        self.move_entered = threading.Event()
        self.release_move = threading.Event()
        self.release_move.set()
        self.pose_entered = threading.Event()
        self.release_pose = threading.Event()
        self.release_pose.set()
        self.failure = None
        self.stop_ok = True
        self.stop_entered = threading.Event()
        self.alarm_ids = ((72,), (), (), (), (), ())
        self.on_clear = lambda: None
        self.ordering_unknown = False
        self.command_pending = False
        self.release_disable = threading.Event()
        self.release_disable.set()

    def cancel(self, *, disabling=False):
        self.stopped = True

    def read_alarms(self):
        self.calls.append("read_alarms")
        return self.alarm_ids

    def clear_alarms(self):
        self.calls.append("clear_alarms")
        self.on_clear()
        # Like JointControl, wait briefly for feedback without the alarm.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.feedback()[0].mode == 9:
            time.sleep(0.005)
        return self.alarm_ids

    def read_pose(self):
        self.pose_entered.set()
        self.release_pose.wait(2)
        return self.pose

    def enable(self):
        self.send_guard()
        self.calls.append("enable")

    def move(self, target):
        self.calls.append(("joint", target))
        self._move()

    def move_linear(self, target):
        self.calls.append(("linear", target))
        self._move()

    def _move(self):
        self.send_guard()
        self.move_entered.set()
        self.release_move.wait(2)
        if self.failure:
            raise self.failure
        if self.stopped:
            raise RuntimeError("Control stopped")

    def stop(self):
        self.stop_entered.set()
        self.calls.append("stop")
        return {"ok": self.stop_ok, "error": "Command ordering remains unknown"}

    def disable(self):
        self.calls.append("disable")
        self.release_disable.wait(2)

    def verify_stationary(self):
        self.calls.append("stationary")

    def feedback_retention_reason(self):
        return None


@pytest.fixture(autouse=True)
def isolated_controller_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path))


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Network")))
    monkeypatch.setattr(socket, "socket", Mock(side_effect=AssertionError("Network")))
    feed = Feed()
    controls = []

    def factory(*args, **kwargs):
        control = Control(*args, **kwargs)
        controls.append(control)
        return control

    state = RobotPanelState("robot.test", connection_factory=feed, control_factory=factory)
    yield state, feed, controls
    for control in controls:
        control.release_move.set()
        control.release_pose.set()
    feed.read.set()
    state.close()


def connect(state):
    state.command({"action": "start_sync"}, "browser-a")
    wait_for(lambda: state.snapshot()["feedback_fresh"])


def execute(state, **extra):
    return state.command(
        {
            "action": "execute",
            "targets": {"x": 250, "y": 0, "z": 30, "r": 0, **extra},
        },
        "browser-a",
    )


def test_constructor_is_inert_and_state_is_robot(rig):
    state, feed, controls = rig
    assert feed.connections == 0
    assert controls[0].calls == []
    assert state.snapshot()["mode"] == "robot"
    assert state.snapshot()["actual"] == [None] * 5


@pytest.mark.parametrize("action", ["start_sync", "stop", "disable"])
def test_another_panel_cannot_connect_or_send_safety_commands(rig, action):
    state, feed, controls = rig
    connect(state)
    other_feed = Feed()
    other = RobotPanelState("robot.test", connection_factory=other_feed, control_factory=Control)
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            other.command({"action": action}, "browser-b")
        assert other_feed.connections == 0
        assert other._control.calls == []
        assert not other.snapshot()["syncing"]
        assert not other.snapshot()["locked"]
    finally:
        other.close()


def test_confirmed_disconnect_releases_ownership_after_feedback_reader_exits(rig):
    state, feed, controls = rig
    connect(state)
    feed.read.clear()
    time.sleep(0.03)
    contender = ControllerOwnership("robot.test")
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
        contender.acquire()
    feed.read.set()

    def acquire_released():
        try:
            contender.acquire()
        except RuntimeError:
            return False
        return True

    try:
        wait_for(acquire_released)
        assert not state.snapshot()["syncing"]
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            state.command({"action": "start_sync"}, "browser-a")
    finally:
        contender.release()
    connect(state)


def test_initial_feedback_failure_releases_ownership_for_another_panel(rig):
    state, feed, controls = rig

    def fail():
        raise ConnectionError("Robot is offline")

    feed.read_latest = fail
    state.command({"action": "start_sync"}, "browser-a")
    wait_for(lambda: "Robot is offline" in state.snapshot()["message"])
    other_feed = Feed()
    other = RobotPanelState("robot.test", connection_factory=other_feed, control_factory=Control)

    def connect_other():
        try:
            other.command({"action": "start_sync"}, "browser-b")
        except RuntimeError:
            return False
        return True

    try:
        wait_for(connect_other)
        wait_for(lambda: other.snapshot()["feedback_fresh"])
        assert controls[0].calls == []
    finally:
        other.close()


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_safety_command_without_sync_holds_ownership_until_worker_exits(rig, action):
    state, feed, controls = rig
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    setattr(controls[0], action, block)
    contender = ControllerOwnership("robot.test")
    try:
        state.command({"action": action}, "browser-a")
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        assert feed.connections == 0
    finally:
        release.set()
    wait_for(lambda: not state.snapshot()["busy"])
    state.close()
    try:
        contender.acquire()
    finally:
        contender.release()


def test_close_releases_ownership_after_confirmed_stop(rig):
    state, feed, controls = rig
    connect(state)
    state.close()
    contender = ControllerOwnership("robot.test")
    try:
        contender.acquire()
        assert controls[0].calls == ["stop"]
    finally:
        contender.release()


def test_close_retains_ownership_after_unconfirmed_stop(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].stop_ok = False
    state.close()
    with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
        ControllerOwnership("robot.test").acquire()
    assert state.snapshot()["locked"]
    assert "unconfirmed" in state.snapshot()["message"]


def test_close_retains_ownership_while_pose_query_is_still_running(rig):
    state, feed, controls = rig
    entered, release = threading.Event(), threading.Event()

    def read_pose():
        entered.set()
        assert release.wait(3)
        return (250, 0, 30, 0)

    controls[0].read_pose = read_pose
    connect(state)
    assert entered.wait(1)
    try:
        state.close()
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            ControllerOwnership("robot.test").acquire()
    finally:
        release.set()


def test_close_retains_ownership_for_backend_command_uncertainty(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].feedback_retention_reason = lambda: "A controller acknowledgement is pending."
    state.close()
    with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
        ControllerOwnership("robot.test").acquire()


def test_feedback_thread_start_failure_releases_ownership(rig, monkeypatch):
    state, feed, controls = rig
    start = threading.Thread.start

    def start_thread(thread):
        if thread._target.__name__ == "_read_feedback":
            raise RuntimeError("Thread creation failed")
        return start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_thread)
    with pytest.raises(RuntimeError, match="Thread creation failed"):
        state.command({"action": "start_sync"}, "browser-a")
    state.close()
    contender = ControllerOwnership("robot.test")
    try:
        contender.acquire()
        assert feed.connections == 0
        assert controls[0].calls == []
        assert not state.snapshot()["syncing"]
        assert state.snapshot()["controller_id"] is None
    finally:
        contender.release()


def test_disconnect_retains_ownership_until_active_pose_query_exits(rig):
    state, feed, controls = rig
    control = controls[0]
    control.release_pose.clear()
    connect(state)
    assert control.pose_entered.wait(1)
    contender = ControllerOwnership("robot.test")
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["syncing"])
    with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
        contender.acquire()
    assert not state.snapshot()["busy"]
    control.release_pose.set()
    state.close()
    try:
        contender.acquire()
    finally:
        contender.release()


def test_sync_reads_feedback_and_pose_without_moving(rig):
    state, feed, controls = rig
    connect(state)
    wait_for(lambda: state.snapshot()["actual"][:4] == [250, 0, 30, 0])
    assert state.snapshot()["joints"] == [1, 2, 3, 4]
    assert state.snapshot()["controller_id"] == "browser-a"
    assert controls[0].calls == []


def test_browser_identity_and_owner_are_required(rig):
    state, feed, controls = rig
    with pytest.raises(ValueError, match="client ID"):
        state.command({"action": "enable"})
    with pytest.raises(PermissionError, match="Start sync"):
        state.command({"action": "enable"}, "browser-a")
    connect(state)
    with pytest.raises(PermissionError, match="Another browser"):
        state.command({"action": "start_sync"}, "browser-b")
    with pytest.raises(PermissionError, match="Another browser"):
        state.command({"action": "enable"}, "browser-b")
    assert state.heartbeat("browser-b")["controller_id"] == "browser-a"


def test_robot_targets_have_no_demo_bounds_and_home_uses_joint_zero(rig):
    state, feed, controls = rig
    connect(state)
    execute(state, x=-500, z=-10, r=300)
    wait_for(lambda: not state.snapshot()["busy"])
    assert controls[0].calls == [("linear", (-500, 0, -10, 300))]
    state.command({"action": "home"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert controls[0].calls[-1] == ("joint", (0, 0, 0, 0))


@pytest.mark.parametrize(
    "change",
    [
        {"x": True},
        {"z": float("nan")},
        {"grip": 0.1},
        {"grip": 1001},
    ],
)
def test_invalid_targets_never_reach_control(rig, change):
    state, feed, controls = rig
    connect(state)
    with pytest.raises(ValueError):
        execute(state, **change)
    assert controls[0].calls == []


def test_motion_requires_fresh_enabled_feedback(rig):
    state, feed, controls = rig
    feed.sample = Feedback((0, 0, 0, 0), 4, 0, False)
    connect(state)
    with pytest.raises(RuntimeError, match="Enable"):
        execute(state)
    with state._lock:
        state._received -= 1
        with pytest.raises(RuntimeError, match="stale"):
            state.command({"action": "enable"}, "browser-a")
    assert controls[0].calls == []


def test_blocked_motion_does_not_block_snapshot_or_other_browser_stop(rig):
    state, feed, controls = rig
    connect(state)
    control = controls[0]
    control.release_move.clear()
    execute(state)
    assert control.move_entered.wait(1)
    started = time.monotonic()
    assert state.snapshot()["busy"]
    assert state.snapshot()["actual"][:4] == [None] * 4
    state.command({"action": "stop"}, "browser-b")
    assert time.monotonic() - started < 0.2
    assert control.stopped
    assert control.stop_entered.wait(1)
    control.release_move.set()
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["locked"]


def test_disable_recovers_only_after_previous_operation_exits(rig):
    state, feed, controls = rig
    connect(state)
    original = controls[0]
    original.release_move.clear()
    execute(state)
    assert original.move_entered.wait(1)
    state.command({"action": "disable"}, "browser-b")
    wait_for(lambda: "stationary" in original.calls)
    assert len(controls) == 1
    assert state.snapshot()["locked"]
    original.release_move.set()
    wait_for(lambda: len(controls) == 2)
    assert not state.snapshot()["locked"]


def test_unconfirmed_stop_cannot_be_cleared_by_disable(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].stop_ok = False
    state.command({"action": "stop"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert "unconfirmed" in state.snapshot()["message"]
    state.command({"action": "disable"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["locked"]
    assert len(controls) == 1
    with pytest.raises(RuntimeError, match="locked"):
        state.command({"action": "enable"}, "browser-a")


def test_expired_lease_cannot_be_revived_by_late_heartbeat(rig):
    state, feed, controls = rig
    connect(state)
    with state._lock:
        state._heartbeat -= 4
    snapshot = state.heartbeat("browser-a")
    assert snapshot["controller_id"] is None
    assert snapshot["locked"]
    assert controls[0].stop_entered.wait(1)
    state.command({"action": "start_sync"}, "browser-b")
    assert state.snapshot()["controller_id"] == "browser-b"
    assert state.snapshot()["locked"]


@pytest.mark.parametrize("revocation", ["owner", "lease"])
def test_real_control_rechecks_browser_authority_before_sending(monkeypatch, revocation):
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Network")))
    monkeypatch.setattr(socket, "socket", Mock(side_effect=AssertionError("Network")))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("Network")))
    monkeypatch.setattr(RobotPanelState, "_watch", lambda self: None)
    feed = Feed()
    feed.sample = Feedback((0, 0, 0, 0), 4, 0, False)
    monkeypatch.setattr(JointControl, "read_pose", lambda self: (250, 0, 30, 0))
    monkeypatch.setattr(
        JointControl,
        "_preflight",
        lambda self, modes: (4, feed.sample.joints_deg),
    )
    monkeypatch.setattr(JointControl, "_wait_stable", lambda self, **kwargs: feed.sample)
    sent = []
    connected = False

    class Connection:
        def __enter__(self):
            nonlocal connected
            if not connected:
                connected = True
                with state._lock:
                    assert state._owner == "browser-a"
                    if revocation == "owner":
                        state._owner = None
                    else:
                        state._heartbeat -= state.LEASE_SECONDS + 1
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def sendall(self, packet):
            self.packet = packet
            sent.append(packet)

        def recv(self, size):
            return b"0,{}," + self.packet + b";"

    monkeypatch.setattr(JointControl, "_connect", lambda self, *args: Connection())
    state = RobotPanelState("robot.test", connection_factory=feed)
    try:
        connect(state)
        state.command({"action": "enable"}, "browser-a")
        wait_for(lambda: not state.snapshot()["busy"])
        assert connected
        assert sent == [b"ResetRobot()"]
        assert state.snapshot()["locked"]
        assert state._control.stopped
    finally:
        state.close()


def test_watchdog_stops_for_robot_error(rig):
    state, feed, controls = rig
    connect(state)
    feed.sample = Feedback((1, 2, 3, 4), 9, 0, True)
    assert controls[0].stop_entered.wait(1)
    assert state.snapshot()["locked"]


def test_watchdog_stops_for_stale_feedback(rig):
    state, feed, controls = rig
    connect(state)
    feed.read.clear()
    with state._lock:
        state._started -= 2
    assert controls[0].stop_entered.wait(1)
    assert state.snapshot()["locked"]


def test_pose_reads_are_serial_with_motion(rig):
    state, feed, controls = rig
    control = controls[0]
    control.release_pose.clear()
    connect(state)
    assert control.pose_entered.wait(1)
    execute(state)
    assert not control.move_entered.wait(0.05)
    control.release_pose.set()
    assert control.move_entered.wait(1)


@pytest.mark.parametrize(
    "error",
    [
        TargetRejectedError("outside limits"),
        PreparationRejectedError("retry"),
    ],
)
def test_preparation_rejection_keeps_control_available(rig, error):
    state, feed, controls = rig
    connect(state)
    controls[0].failure = error
    execute(state)
    wait_for(lambda: not state.snapshot()["busy"])
    assert not state.snapshot()["locked"]
    assert "stop" not in controls[0].calls
    assert state.snapshot()["message"] == str(error)


def test_value_error_after_operation_starts_locks_control(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].failure = ValueError("Invalid controller response")
    execute(state)
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["locked"]
    assert "stop" in controls[0].calls


def test_stopped_control_rejects_new_commands_before_queue(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].stopped = True
    with pytest.raises(RuntimeError, match="locked"):
        execute(state)
    assert controls[0].calls == []


def test_stop_sync_disconnects_only_after_confirmed_stop(rig):
    state, feed, controls = rig
    connect(state)
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert not state.snapshot()["syncing"]
    assert state.snapshot()["controller_id"] is None
    assert state.snapshot()["locked"]


def test_disable_disconnect_reconnect_can_enable_without_another_disable(rig):
    state, feed, controls = rig
    connect(state)
    feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)
    wait_for(lambda: state.snapshot()["robot_mode"] == 4)
    state.command({"action": "disable"}, "browser-a")
    wait_for(lambda: len(controls) == 2)
    disconnected_control = controls[1]

    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"] and not state.snapshot()["locked"])
    assert disconnected_control.calls == ["disable", "stationary"]
    assert not state.snapshot()["syncing"]
    assert not state.snapshot()["locked"]
    assert state.snapshot()["controller_id"] is None

    state._reader.join(1)
    connect(state)
    state.command({"action": "enable"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert controls[-1].calls == ["enable"]


def test_disabled_disconnect_keeps_feedback_and_lock_if_verification_fails(rig):
    state, feed, controls = rig
    feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)
    connect(state)

    def fail():
        raise RuntimeError("Disable acknowledgement is unknown")

    controls[0].disable = fail
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["syncing"]
    assert state.snapshot()["locked"]
    assert "unconfirmed" in state.snapshot()["message"]
    state.command({"action": "start_sync"}, "browser-a")
    with pytest.raises(RuntimeError, match="locked"):
        state.command({"action": "enable"}, "browser-a")
    assert len(controls) == 1


def test_stale_disabled_feedback_cannot_unlock_disconnect(rig):
    state, feed, controls = rig
    feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)
    connect(state)
    with state._lock:
        state._received -= 1
        state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert controls[0].calls == ["stop"]
    assert state.snapshot()["locked"]


def test_unconfirmed_stop_preserves_feedback(rig):
    state, feed, controls = rig
    connect(state)
    controls[0].stop_ok = False
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["syncing"]


def test_failed_disconnect_stops_pose_queries_and_preserves_feedback_and_safety_lock(rig):
    state, feed, controls = rig
    control = controls[0]
    control.read_pose = Mock(wraps=control.read_pose)
    connect(state)
    wait_for(lambda: control.read_pose.call_count > 0)
    control.stop_ok = False
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    queries_after_failure = control.read_pose.call_count
    feed.sample = Feedback((2, 3, 4, 5), 4, 0, False)
    wait_for(lambda: state.snapshot()["joints"] == [2, 3, 4, 5])
    time.sleep(0.2)
    assert control.read_pose.call_count == queries_after_failure
    assert state.snapshot()["feedback_fresh"]
    assert state.snapshot()["locked"]
    assert "unconfirmed" in state.snapshot()["message"]
    state.command({"action": "disable"}, "browser-b")
    wait_for(lambda: not state.snapshot()["busy"])
    assert control.calls == ["stop", "disable", "stationary"]
    assert state.snapshot()["locked"]
    assert state.snapshot()["syncing"]
    assert len(controls) == 1


def test_confirmed_stop_suspends_pose_queries_until_disable_recovers(rig):
    state, feed, controls = rig
    control = controls[0]
    control.read_pose = Mock(wraps=control.read_pose)
    connect(state)
    wait_for(lambda: control.read_pose.call_count > 0)
    state.command({"action": "stop"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    queries_after_stop = control.read_pose.call_count
    time.sleep(0.2)
    assert control.read_pose.call_count == queries_after_stop
    assert state.snapshot()["feedback_fresh"]
    assert state.snapshot()["locked"]
    state.command({"action": "disable"}, "browser-a")
    wait_for(lambda: not state.snapshot()["locked"])
    assert controls[-1].pose_entered.wait(1)


def test_shutdown_requests_stop(rig):
    state, feed, controls = rig
    connect(state)
    state.close()
    assert controls[0].stopped
    assert "stop" in controls[0].calls


def test_gripper_target_requires_adapter(rig):
    state, feed, controls = rig
    connect(state)
    with pytest.raises(RuntimeError, match="Gripper control is unavailable"):
        execute(state, grip=500)
    assert controls[0].calls == []


def test_failed_reconnect_does_not_claim_browser_lease(rig):
    state, feed, controls = rig
    connect(state)
    feed.read.clear()
    time.sleep(0.03)
    state.command({"action": "stop_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    with pytest.raises(RuntimeError, match="reader is stopping"):
        state.command({"action": "start_sync"}, "browser-b")
    assert state.snapshot()["controller_id"] is None
    feed.read.set()
    state._reader.join(1)
    state.command({"action": "start_sync"}, "browser-b")
    assert state.snapshot()["controller_id"] == "browser-b"


def test_initial_connection_failure_does_not_send_robot_commands(rig):
    state, feed, controls = rig

    def fail():
        raise ConnectionError("Robot is offline")

    feed.read_latest = fail
    state.command({"action": "start_sync"}, "browser-a")
    wait_for(lambda: not state.snapshot()["syncing"])
    assert "Robot is offline" in state.snapshot()["message"]
    assert not state.snapshot()["locked"]
    assert controls[0].calls == []


def test_repeated_stop_requests_share_one_worker(rig):
    state, feed, controls = rig
    connect(state)
    release = threading.Event()
    started = threading.Event()

    def stop():
        started.set()
        assert release.wait(2)
        return {"ok": True}

    controls[0].stop = stop
    try:
        state.command({"action": "stop"}, "browser-a")
        assert started.wait(1)
        for _ in range(20):
            state.command({"action": "stop"}, "browser-b")
        assert state._safety_workers == 1
        assert len(state._safety_threads) == 1
    finally:
        release.set()


class Gripper:
    def __init__(self):
        self.position = 1000
        self.commands = []
        self.preparing = threading.Event()
        self.publish = threading.Event()
        self.publish.set()

    def snapshot(self):
        return {
            "position": self.position,
            "available": True,
            "can_initialize": True,
            "busy": False,
            "locked": False,
            "message": "Ready",
        }

    def poll(self):
        pass

    def move(self, target):
        self.preparing.set()
        assert self.publish.wait(2)
        with self.publication_guard():
            self.commands.append(target)
            self.position = target

    def initialize(self):
        with self.publication_guard():
            self.commands.append("initialize")

    def close(self):
        self.publish.set()


def add_gripper(state):
    gripper = Gripper()
    with state._lock:
        state._gripper = gripper
        state._grip = gripper.snapshot()
        gripper.publication_guard = state._gripper_publication_guard
    return gripper


def test_execute_always_sends_explicit_gripper_target(rig):
    state, feed, controls = rig
    gripper = add_gripper(state)
    connect(state)
    execute(state, grip=1000)
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == [1000]
    execute(state, grip=500)
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == [1000, 500]
    assert state.snapshot()["actual"][4] == 500


def test_initialize_gripper_accepts_disabled_idle_robot(rig):
    state, feed, controls = rig
    feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)
    gripper = add_gripper(state)
    connect(state)
    state.command({"action": "initialize_gripper"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == ["initialize"]
    assert controls[0].calls == []


@pytest.mark.parametrize("mode", [4, 5])
@pytest.mark.parametrize("position", [0, 1000])
def test_grip_moves_only_gripper_on_idle_robot(rig, mode, position):
    state, feed, controls = rig
    feed.sample = Feedback((1, 2, 3, 4), mode, 0, False)
    gripper = add_gripper(state)
    connect(state)
    state.command({"action": "grip", "position": position}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == [position]
    assert controls[0].calls == []
    assert state.snapshot()["actual"][4] == position


@pytest.mark.parametrize(
    "position", [-1, 1001, 0.5, float("nan"), float("inf"), True, "500", None, 10**400]
)
def test_invalid_grip_position_never_connects_or_sends(rig, position):
    state, feed, controls = rig
    gripper = add_gripper(state)
    with pytest.raises(ValueError):
        state.command({"action": "grip", "position": position}, "browser-a")
    assert feed.connections == 0
    assert gripper.commands == []
    assert controls[0].calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "grip"},
        {"action": "grip", "targets": {"grip": 500}},
        {"action": "grip", "position": 500, "extra": True},
    ],
)
def test_grip_requires_only_position_field(rig, payload):
    state, feed, controls = rig
    with pytest.raises(ValueError, match="Unexpected command fields"):
        state.command(payload, "browser-a")
    assert feed.connections == 0
    assert controls[0].calls == []


def test_grip_requires_connected_owner_and_available_adapter(rig):
    state, feed, controls = rig
    payload = {"action": "grip", "position": 500}
    with pytest.raises(PermissionError, match="Start sync"):
        state.command(payload, "browser-a")
    connect(state)
    with pytest.raises(PermissionError, match="Another browser"):
        state.command(payload, "browser-b")
    with pytest.raises(RuntimeError, match="Gripper control is unavailable"):
        state.command(payload, "browser-a")
    assert controls[0].calls == []


def test_grip_rejects_moving_robot_before_queue(rig):
    state, feed, controls = rig
    feed.sample = Feedback((1, 2, 3, 4), 7, 0, False)
    gripper = add_gripper(state)
    connect(state)
    with pytest.raises(RuntimeError, match="must be idle"):
        state.command({"action": "grip", "position": 500}, "browser-a")
    assert gripper.commands == []
    assert controls[0].calls == []
    assert not state.snapshot()["busy"]


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_stop_or_disable_prevents_queued_grip(rig, action):
    state, feed, controls = rig
    control = controls[0]
    control.release_pose.clear()
    gripper = add_gripper(state)
    connect(state)
    assert control.pose_entered.wait(1)
    state.command({"action": "grip", "position": 500}, "browser-a")
    state.command({"action": action}, "browser-b")
    control.release_pose.set()
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == []
    assert not gripper.preparing.is_set()


@pytest.mark.parametrize("action", ["stop", "disable", "lease_expiry"])
@pytest.mark.parametrize("command", ["execute", "grip"])
def test_stop_or_expired_lease_prevents_gripper_publication(rig, action, command):
    state, feed, controls = rig
    gripper = add_gripper(state)
    gripper.publish.clear()
    connect(state)
    if command == "execute":
        execute(state, grip=500)
    else:
        state.command({"action": "grip", "position": 500}, "browser-a")
    assert gripper.preparing.wait(1)
    if action == "lease_expiry":
        with state._lock:
            state._heartbeat -= 4
        state.heartbeat("browser-a")
    else:
        state.command({"action": action}, "browser-b")
    gripper.publish.set()
    wait_for(lambda: not state.snapshot()["busy"])
    assert gripper.commands == []


def alarm_rig(rig):
    """Connect, then let the controller raise alarm 72 so the panel locks."""
    state, feed, controls = rig
    connect(state)
    feed.sample = Feedback((1, 2, 3, 4), 9, 0, True)
    wait_for(lambda: state.snapshot()["locked"] and not state.snapshot()["busy"])
    return state, feed, controls


def test_alarm_ids_are_shown_with_names_and_cleared_with_the_alarm(rig):
    state, feed, controls = alarm_rig(rig)
    wait_for(lambda: state.snapshot()["alarms"])
    assert state.snapshot()["alarms"] == [
        {
            "id": 72,
            "source": "controller",
            "description": "Parallelogram positive limit",
            "solution": "Reverse jog out of limit",
        }
    ]
    feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)
    wait_for(lambda: state.snapshot()["alarms"] is None)


def test_clear_alarm_keeps_the_lock_while_the_alarm_remains(rig):
    state, feed, controls = alarm_rig(rig)
    state.command({"action": "clear_alarm"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    snapshot = state.snapshot()
    assert "clear_alarms" in controls[0].calls
    assert snapshot["locked"]
    assert snapshot["message"] == (
        "The alarm remains: 72 Parallelogram positive limit (Reverse jog out of limit)."
    )
    assert len(controls) == 1


def test_clear_alarm_confirms_disable_and_rest_before_unlocking(rig):
    state, feed, controls = alarm_rig(rig)
    controls[0].stop_ok = False
    controls[0].alarm_ids = ((), (), (), (), (), ())

    def cleared():
        feed.sample = Feedback((1, 2, 3, 4), 4, 0, False)

    controls[0].on_clear = cleared
    state.command({"action": "clear_alarm"}, "browser-a")
    wait_for(lambda: not state.snapshot()["locked"])
    recovery = controls[1]
    assert recovery.calls[:2] == ["disable", "stationary"]
    assert "Continue" not in str(controls[0].calls)
    assert not state.snapshot()["stop_unconfirmed"]
    assert state.snapshot()["alarms"] is None


def test_clear_alarm_keeps_the_lock_when_command_ordering_is_unknown(rig):
    state, feed, controls = alarm_rig(rig)
    controls[0].alarm_ids = ((), (), (), (), (), ())
    controls[0].ordering_unknown = True
    controls[0].on_clear = lambda: setattr(feed, "sample", Feedback((1, 2, 3, 4), 5, 0, False))
    state.command({"action": "clear_alarm"}, "browser-a")
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["locked"]
    assert "ordering remains unknown" in state.snapshot()["message"]
    assert len(controls) == 1


def test_stop_failure_during_alarm_recovery_keeps_the_lock(rig):
    state, feed, controls = alarm_rig(rig)
    controls[0].alarm_ids = ((), (), (), (), (), ())
    controls[0].on_clear = lambda: setattr(feed, "sample", Feedback((1, 2, 3, 4), 4, 0, False))
    original_factory = state._control_factory

    def factory(*args, **kwargs):
        control = original_factory(*args, **kwargs)
        control.release_disable.clear()
        control.stop_ok = False
        return control

    state._control_factory = factory
    state.command({"action": "clear_alarm"}, "browser-a")
    wait_for(lambda: len(controls) == 2 and "disable" in controls[1].calls)
    state.command({"action": "stop"}, "browser-a")
    wait_for(lambda: controls[1].stop_entered.is_set())
    wait_for(lambda: state.snapshot()["stop_unconfirmed"] and not state._safety_workers)
    controls[1].release_disable.set()
    wait_for(lambda: not state.snapshot()["busy"])
    assert state.snapshot()["locked"] and state.snapshot()["stop_unconfirmed"]


def test_clear_alarm_requires_an_alarm_and_the_controlling_browser(rig):
    state, feed, controls = rig
    connect(state)
    with pytest.raises(RuntimeError, match="no alarm"):
        state.command({"action": "clear_alarm"}, "browser-a")
    feed.sample = Feedback((1, 2, 3, 4), 9, 0, True)
    wait_for(lambda: state.snapshot()["locked"] and not state.snapshot()["busy"])
    with pytest.raises(PermissionError):
        state.command({"action": "clear_alarm"}, "browser-b")
    assert "clear_alarms" not in controls[0].calls
