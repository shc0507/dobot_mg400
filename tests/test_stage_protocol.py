import errno
import fcntl
import os
from collections import deque

import pytest
import serial

from stage_controller import protocol
from stage_controller.protocol import CommandRejected, LinkError, SerialTransport, parse_status

STATUS = [
    b"* bottom cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200\r\n",
    b"  top ccw moving pos=-12 remaining=-188 speed=300.0 accel=50.0 spr=200\r\n",
    b"  insert cw continuous pos=70 remaining=0 speed=100.0 accel=80.0 spr=400\r\n",
]


class FakeSerial:
    def __init__(self, lines=()):
        self.lines = deque(lines)
        self.writes = []
        self.is_closed = False
        self.reset = False

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def read_until(self, *_args):
        if not self.lines:
            raise serial.SerialException("device removed")
        return self.lines.popleft()

    def reset_input_buffer(self):
        self.reset = True

    def close(self):
        self.is_closed = True


@pytest.fixture
def make_transport(monkeypatch):
    def make(lines=()):
        port = FakeSerial(lines)

        def open_port(**options):
            port.options = options
            return port

        monkeypatch.setattr(protocol.serial, "Serial", open_port)
        monkeypatch.setattr(protocol.time, "sleep", lambda _delay: None)
        return SerialTransport("COM7"), port

    return make


def test_parses_actual_firmware_status_and_signed_counts(make_transport):
    transport, port = make_transport(STATUS)
    statuses = transport.status()
    assert port.reset
    assert port.writes == [b"status\n"]
    assert list(statuses) == ["bottom", "top", "insert"]
    assert statuses["top"].position == -12
    assert statuses["top"].remaining == -188
    assert statuses["insert"].mode == "continuous"
    assert statuses["insert"].steps_per_revolution == 400


def test_reassembles_status_split_by_read_timeout(make_transport):
    transport, _ = make_transport([STATUS[0][:20], STATUS[0][20:], *STATUS[1:]])
    assert transport.status()["bottom"].mode == "idle"


def test_each_command_needs_matching_acknowledgement(make_transport):
    transport, port = make_transport(
        [b"OK: top motor\n", b"OK: top dir\n", b"OK: top speed\n", b"OK: top steps\n"]
    )
    for command in ("motor top", "dir ccw", "speed 420", "steps 88"):
        transport.command(command)
    assert port.writes == [b"motor top\n", b"dir ccw\n", b"speed 420\n", b"steps 88\n"]


def test_wrong_ack_is_not_a_success(make_transport):
    transport, _ = make_transport([b"OK: bottom speed\n"])
    with pytest.raises(LinkError, match="Unexpected"):
        transport.command("steps 200")


def test_firmware_rejection_is_explicit(make_transport):
    transport, _ = make_transport([b"ERROR: motor busy\n"])
    with pytest.raises(CommandRejected, match="motor busy"):
        transport.command("dir ccw")


def test_timeout_and_unplug_are_errors(make_transport, monkeypatch):
    transport, port = make_transport()
    with pytest.raises(LinkError, match="device removed"):
        transport.status()
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(protocol.time, "monotonic", lambda: next(ticks))
    port.lines.append(b"")
    with pytest.raises(LinkError, match="did not reply"):
        transport.command("stop all")


def test_status_requires_three_distinct_motors(make_transport):
    transport, _ = make_transport([STATUS[0], STATUS[0], STATUS[2]])
    with pytest.raises(LinkError, match="Duplicate"):
        transport.status()


@pytest.mark.parametrize("line", ["OK: bottom steps", "bottom cw idle", "garbage", ""])
def test_non_status_lines_are_ignored(line):
    assert parse_status(line) is None


def test_short_write_is_not_success(make_transport, monkeypatch):
    transport, port = make_transport()
    monkeypatch.setattr(port, "write", lambda _payload: 2)
    with pytest.raises(LinkError, match="Incomplete serial write"):
        transport.command("steps 200")


def test_hold_renewal_and_motor_stop_require_matching_acknowledgements(make_transport):
    transport, port = make_transport(
        [
            b"OK: top motor\n",
            b"OK: top hold\n",
            b"OK: top keepalive\n",
            b"OK: top stop\n",
        ]
    )
    for command in ("motor top", "hold", "keepalive", "stop"):
        transport.command(command)
    assert port.writes == [b"motor top\n", b"hold\n", b"keepalive\n", b"stop\n"]


@pytest.mark.parametrize("divisor", [1, 2, 4, 8, 16])
def test_reads_global_microstep_header_with_motor_status(make_transport, divisor):
    transport, _ = make_transport([f"microstep={divisor}\n".encode(), *STATUS])
    assert len(transport.status()) == 3
    assert transport.microstep == divisor


def test_legacy_status_reports_unknown_mode_instead_of_assuming_full_step(make_transport):
    transport, _ = make_transport(STATUS)
    transport.microstep = 16
    transport.status()
    assert transport.microstep is None


@pytest.mark.parametrize("line", [b"microstep=3\n", b"microstep=0\n", b"microstep=16 extra\n"])
def test_invalid_mode_header_is_not_ignored(make_transport, line):
    transport, _ = make_transport([line, *STATUS])
    with pytest.raises(LinkError, match="microstep status"):
        transport.status()


def test_duplicate_mode_headers_are_rejected(make_transport):
    transport, _ = make_transport([b"microstep=1\n", b"microstep=16\n", *STATUS])
    with pytest.raises(LinkError, match="duplicate microstep"):
        transport.status()


def test_global_mode_ack_does_not_depend_on_selected_motor(make_transport):
    transport, port = make_transport([b"OK: top motor\n", b"OK: microstep 16\n"])
    transport.command("motor top")
    transport.command("microstep 16")
    assert port.writes == [b"motor top\n", b"microstep 16\n"]
    assert transport.selected == "top"


def test_wrong_mode_ack_is_not_accepted(make_transport):
    transport, _ = make_transport([b"OK: microstep 8\n"])
    with pytest.raises(LinkError, match="Unexpected"):
        transport.command("microstep 16")


def test_port_is_opened_exclusively(make_transport):
    _, port = make_transport()
    assert port.options["port"] == "COM7"
    assert port.options["exclusive"] is True


@pytest.mark.parametrize("code", [errno.EAGAIN, errno.EBUSY])
def test_port_held_by_another_program_is_a_link_error(monkeypatch, code):
    def held(**options):
        raise serial.SerialException(code, f"could not open port {options['port']}")

    monkeypatch.setattr(protocol.serial, "Serial", held)
    with pytest.raises(LinkError, match="COM7 is in use by another program"):
        SerialTransport("COM7")


@pytest.fixture
def pty_port(monkeypatch):
    """Return the device name of a pseudo-terminal that stands in for a USB serial port."""
    monkeypatch.setattr(protocol.time, "sleep", lambda _delay: None)
    controller_end, device_end = os.openpty()
    yield os.ttyname(device_end)
    os.close(device_end)
    os.close(controller_end)


def test_a_port_locked_like_the_gripper_collector_cannot_be_opened(pty_port):
    # scripts/gripper_feedback.py holds the same exclusive flock on its port.
    descriptor = os.open(pty_port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LinkError, match="in use by another program"):
            SerialTransport(pty_port)
    finally:
        os.close(descriptor)


def test_a_second_transport_cannot_share_an_open_port(pty_port):
    first = SerialTransport(pty_port)
    try:
        with pytest.raises(LinkError, match="in use by another program"):
            SerialTransport(pty_port)
    finally:
        first.close()
    SerialTransport(pty_port).close()
