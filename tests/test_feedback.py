import socket
import struct
from collections import deque
from unittest.mock import Mock

import pytest

from mg400.feedback import FRAME_SIZE, Feedback, FeedbackConnection, decode_feedback


def frame(joints=(10.0, -20.0, 30.0, 40.0), *, timestamp=12345, mode=5, error=False):
    data = bytearray(FRAME_SIZE)
    struct.pack_into("<H", data, 0, FRAME_SIZE)
    struct.pack_into("<Q", data, 24, mode)
    struct.pack_into("<Q", data, 32, timestamp)
    struct.pack_into("<Q", data, 48, 0x0123456789ABCDEF)
    struct.pack_into("<6d", data, 432, *joints, 0, 0)
    data[1029] = int(error)
    return bytes(data)


class ReceiveOnlySocket:
    def __init__(self, *parts):
        self.parts = deque(parts)
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        if not self.parts:
            raise BlockingIOError
        part = self.parts.popleft()
        if isinstance(part, Exception):
            raise part
        if len(part) > size:
            self.parts.appendleft(part[size:])
        return part[:size]

    def close(self):
        self.closed = True


def connection_for(monkeypatch, receiver, **kwargs):
    factory = Mock(return_value=receiver)
    monkeypatch.setattr(socket, "create_connection", factory)
    return FeedbackConnection("robot.test", **kwargs), factory


def test_decodes_controller_state_and_preserves_raw_timestamp():
    assert decode_feedback(frame(error=True, mode=9)) == Feedback(
        joints_deg=(10, -20, 30, 40),
        mode=9,
        timestamp=12345,
        error_status=True,
        speed_scaling_raw=0.0,
    )
    assert decode_feedback(frame(timestamp=0)).timestamp == 0


@pytest.mark.parametrize("value", [1.0, 68.0, 0.125])
def test_preserves_raw_speed_scaling(value):
    data = bytearray(frame())
    struct.pack_into("<d", data, 64, value)
    assert decode_feedback(bytes(data)).speed_scaling_raw == value


def flip(data, offset):
    data = bytearray(data)
    data[offset] ^= 1
    return data


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "feedback bytes"),
        (frame()[:-1], "feedback bytes"),
        (frame() + b"x", "feedback bytes"),
        (flip(frame(), 0), "length field"),
        (flip(frame(), 48), "magic"),
        (frame((10, float("nan"), 30, 40)), "finite"),
        (frame((10, float("inf"), 30, 40)), "finite"),
        (frame((10, -float("inf"), 30, 40)), "finite"),
    ],
    ids=["empty", "short", "long", "length", "magic", "nan", "inf", "-inf"],
)
def test_decode_rejects_invalid_frames(data, message):
    with pytest.raises(ValueError, match=message):
        decode_feedback(data)


def test_fragmented_and_coalesced_frames_return_latest_and_preserve_tail(monkeypatch):
    first, second, third = [frame(timestamp=i) for i in range(3)]
    receiver = ReceiveOnlySocket(first[:20], first[20:] + second + third[:100])
    connection, factory = connection_for(monkeypatch, receiver, source_host="192.168.1.10")
    with connection:
        assert connection.read_latest().timestamp == 1
        receiver.parts.append(third[100:])
        assert connection.read_latest().timestamp == 2
    factory.assert_called_once_with(
        ("robot.test", 30004), timeout=2.0, source_address=("192.168.1.10", 0)
    )
    assert receiver.closed
    assert 0 in receiver.timeouts


@pytest.mark.parametrize("parts", [(b"",), (frame()[:10], b""), (frame(), b"")])
def test_eof_is_reported_and_connection_is_closed(monkeypatch, parts):
    receiver = ReceiveOnlySocket(*parts)
    connection, _ = connection_for(monkeypatch, receiver)
    with pytest.raises(ConnectionError, match="closed"), connection:
        connection.read_latest()
    assert receiver.closed


def test_partial_frame_timeout_does_not_return_stale_state(monkeypatch):
    receiver = ReceiveOnlySocket(frame()[:10], TimeoutError())
    connection, _ = connection_for(monkeypatch, receiver)
    with pytest.raises(TimeoutError, match="complete feedback frame"), connection:
        connection.read_latest()
    assert receiver.closed


def test_drip_feed_cannot_extend_frame_deadline(monkeypatch):
    receiver = ReceiveOnlySocket(frame()[:1], frame()[1:])
    connection, _ = connection_for(monkeypatch, receiver, timeout_s=2)
    monkeypatch.setattr("mg400.feedback.time.monotonic", Mock(side_effect=[0, 1, 3]))
    with pytest.raises(TimeoutError, match="complete feedback frame"), connection:
        connection.read_latest()


def test_drain_is_bounded_for_continuously_available_frames(monkeypatch):
    receiver = ReceiveOnlySocket(*(frame(timestamp=i) for i in range(1000)))
    connection, _ = connection_for(monkeypatch, receiver)
    with connection:
        result = connection.read_latest()
    assert 0 < result.timestamp < 999
    assert receiver.parts


def test_invalid_queued_frame_is_not_silently_skipped(monkeypatch):
    receiver = ReceiveOnlySocket(bytes(FRAME_SIZE) + frame())
    connection, _ = connection_for(monkeypatch, receiver)
    with pytest.raises(ValueError, match="length field"), connection:
        connection.read_latest()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        FeedbackConnection("robot.test", timeout_s=timeout)


def test_requires_context_manager():
    with pytest.raises(RuntimeError, match="not open"):
        FeedbackConnection("robot.test").read_latest()
