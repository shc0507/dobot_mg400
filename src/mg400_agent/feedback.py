"""Passive reader for the MG400's 1440-byte feedback stream on TCP port 30004."""

import math
import socket
import struct
import time
from dataclasses import dataclass

FRAME_SIZE = 1440
FEEDBACK_PORT = 30004
_MAGIC = 0x0123456789ABCDEF
_RECV_SIZE = FRAME_SIZE * 64
_MAX_DRAIN_READS = 32


@dataclass(frozen=True)
class Feedback:
    joints_deg: tuple[float, float, float, float]
    mode: int
    # Raw controller_timer field; older MG400 firmware reserves it. Not Unix time
    # or a reliable freshness signal. Receipt deadlines use local monotonic time.
    timestamp: int
    error_status: bool


def decode_feedback(data: bytes) -> Feedback:
    """Decode Dobot's four-axis SDK layout; leave joints in controller degrees.

    Layout: github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py
    """
    if len(data) != FRAME_SIZE:
        raise ValueError(f"Expected {FRAME_SIZE} feedback bytes, got {len(data)}")
    if struct.unpack_from("<H", data, 0)[0] != FRAME_SIZE:
        raise ValueError("Unexpected feedback length field")
    if struct.unpack_from("<Q", data, 48)[0] != _MAGIC:
        raise ValueError("Unexpected feedback magic")
    joints = struct.unpack_from("<4d", data, 432)
    if not all(math.isfinite(joint) for joint in joints):
        raise ValueError("Feedback joint angles must be finite")
    return Feedback(
        joints_deg=joints,
        mode=struct.unpack_from("<Q", data, 24)[0],
        timestamp=struct.unpack_from("<Q", data, 32)[0],
        error_status=bool(data[1029]),
    )


class FeedbackConnection:
    """Receive robot state only; never send controller commands.

    Use as a context manager. Each read waits for a new complete frame, then
    drains available data to return the newest frame. Partial tails survive the
    next read. Draining is bounded so a continuous producer cannot starve callers.
    """

    def __init__(self, host: str, source_host: str | None = None, timeout_s: float = 2.0):
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Feedback timeout must be positive and finite")
        self.host = host
        self.source_host = source_host
        self.timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._buffer = bytearray()

    def __enter__(self):
        if self._socket is not None:
            raise RuntimeError("Feedback connection is already open")
        self._socket = socket.create_connection(
            (self.host, FEEDBACK_PORT),
            timeout=self.timeout_s,
            source_address=(self.source_host, 0) if self.source_host else None,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._buffer.clear()

    def read_latest(self) -> Feedback:
        if self._socket is None:
            raise RuntimeError("Feedback connection is not open")
        deadline = time.monotonic() + self.timeout_s
        latest = None
        drain_reads = 0
        while True:
            while len(self._buffer) >= FRAME_SIZE:
                latest = decode_feedback(bytes(self._buffer[:FRAME_SIZE]))
                del self._buffer[:FRAME_SIZE]
            if latest is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for a complete feedback frame")
                self._socket.settimeout(remaining)
            else:
                if drain_reads >= _MAX_DRAIN_READS or time.monotonic() >= deadline:
                    return latest
                self._socket.settimeout(0)
                drain_reads += 1
            try:
                part = self._socket.recv(_RECV_SIZE)
            except BlockingIOError:
                if latest is not None:
                    return latest
                raise
            except TimeoutError as exc:
                raise TimeoutError("Timed out waiting for a complete feedback frame") from exc
            if not part:
                raise ConnectionError("Robot feedback connection closed")
            self._buffer.extend(part)
