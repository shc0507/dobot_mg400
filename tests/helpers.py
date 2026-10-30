"""Plain helpers and fakes that several test modules share."""

import contextlib
import socket
import threading
import time
from unittest.mock import Mock

from mg400.feedback import Feedback


def wait_for(predicate, timeout=2, describe=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), describe() if describe else "The condition was not reached"


def block_network(monkeypatch, *names, message="Network"):
    """Make the named socket functions fail the test. Default: create_connection and socket."""
    for name in names or ("create_connection", "socket"):
        monkeypatch.setattr(socket, name, Mock(side_effect=AssertionError(message)))


def fake_connection(monkeypatch, parts, *, raw_socket=True):
    """Make socket.create_connection return one Mock connection whose recv returns parts.

    With raw_socket, socket.socket also returns it and its connect_ex() succeeds.
    """
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.recv.side_effect = parts
    monkeypatch.setattr(socket, "create_connection", Mock(return_value=connection))
    if raw_socket:
        connection.connect_ex.return_value = 0
        monkeypatch.setattr(socket, "socket", Mock(return_value=connection))
    return connection


@contextlib.contextmanager
def idle_lock(state):
    """Hold the panel lock between worker iterations, so a key press starts its hold at once.

    The hold worker cannot pass its start check until the lock is released.
    """
    deadline = time.monotonic() + 2
    while True:
        state._lock.acquire()
        if not state._working:
            break
        state._lock.release()
        assert time.monotonic() < deadline, "The panel worker did not become idle"
        time.sleep(0.002)
    try:
        yield
    finally:
        state._lock.release()


class Feed:
    """Feedback connection fake. It delivers the current sample about every 10 ms."""

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


class PanelControl:
    """Fake JointControl for RobotPanelState, with gates for moves, pose reads and disable."""

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
