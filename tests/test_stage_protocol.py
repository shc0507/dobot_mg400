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

CLOSED_STATUS = [
    b"* bottom cw idle pos=0.100098 remaining=0.000000 speed=0.250000 "
    b"accel=0.250000 spr=200 encoder=ok fault=none tolerance=0.000977 "
    b"max_speed=0.500000 measured_speed=0.000000\n",
    b"  top ccw moving pos=-1.500000 remaining=-0.500000 speed=0.100000 "
    b"accel=0.250000 spr=200 encoder=ok fault=none tolerance=0.000977 "
    b"max_speed=0.500000 measured_speed=-0.098000\n",
    b"  insert cw idle pos=3.000000 remaining=0.000000 speed=0.250000 "
    b"accel=0.250000 spr=400 encoder=offline fault=encoder tolerance=0.000732 "
    b"max_speed=0.250000 measured_speed=0.000000\n",
]


def closed_reply(lines=CLOSED_STATUS):
    return [b"motion=closed_loop_v1\n", b"microstep=8\n", *lines]


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

    @property
    def in_waiting(self):
        return sum(len(chunk) for chunk in self.lines)

    def read(self, size=1):
        result = bytearray()
        while self.lines and len(result) < size:
            chunk = self.lines.popleft()
            remaining = size - len(result)
            result.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.lines.appendleft(chunk[remaining:])
        return bytes(result)

    def reset_input_buffer(self):
        self.reset = True

    def close(self):
        self.is_closed = True


@pytest.fixture
def make_transport(monkeypatch):
    def make(lines=()):
        port = FakeSerial(lines)
        monkeypatch.setattr(protocol.serial, "Serial", lambda **_kwargs: port)
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
    assert transport.closed_loop is False


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


def test_closed_loop_status_keeps_measured_fractional_and_signed_units(make_transport):
    transport, _ = make_transport(closed_reply())
    states = transport.status()
    assert transport.closed_loop is True
    assert transport.microstep == 8
    assert states["bottom"].position == pytest.approx(0.100098)
    assert states["top"].position == -1.5
    assert states["top"].remaining == -0.5
    assert states["top"].measured_speed == pytest.approx(-0.098)
    assert states["bottom"].tolerance == 0.000977
    assert states["insert"].encoder == "offline"
    assert states["insert"].fault == "encoder"
    assert states["insert"].max_speed == 0.25


def test_closed_loop_capability_is_not_retained_after_legacy_reply(make_transport):
    transport, port = make_transport(closed_reply())
    transport.status()
    port.lines.extend(STATUS)
    transport.status()
    assert transport.closed_loop is False


def test_new_commands_need_matching_acknowledgements(make_transport):
    transport, port = make_transport(
        [b"OK: top motor\n", b"OK: top speed\n", b"OK: top rev\n", b"OK: top clear\n"]
    )
    for command in ("motor top", "speed 0.1", "rev 0.5", "clear"):
        transport.command(command)
    assert port.writes == [b"motor top\n", b"speed 0.1\n", b"rev 0.5\n", b"clear\n"]


@pytest.mark.parametrize(
    "header",
    [b"motion=open_loop\n", b"motion=closed_loop_v2\n", b"motion=closed_loop_v1 extra\n"],
)
def test_unknown_motion_protocol_is_rejected(make_transport, header):
    transport, _ = make_transport([header, b"microstep=1\n", *CLOSED_STATUS])
    with pytest.raises(LinkError):
        transport.status()


def test_duplicate_motion_header_is_rejected(make_transport):
    transport, _ = make_transport([b"motion=closed_loop_v1\n", *closed_reply()])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "original,replacement",
    [
        (b"pos=0.100098", b"pos=nan"),
        (b"remaining=0.000000", b"remaining=inf"),
        (b"speed=0.250000", b"speed=-0.250000"),
        (b"spr=200", b"spr=0"),
        (b"encoder=ok", b"encoder=unknown_code"),
        (b"fault=none", b"fault=unknown_code"),
        (b"tolerance=0.000977", b"tolerance=0.000000"),
        (b"max_speed=0.500000", b"max_speed=0.000000"),
        (b" measured_speed=0.000000", b""),
    ],
)
def test_invalid_feedback_status_cannot_unlock_motion(make_transport, original, replacement):
    bad = CLOSED_STATUS[0].replace(original, replacement)
    transport, _ = make_transport(closed_reply([bad, *CLOSED_STATUS[1:]]))
    with pytest.raises(LinkError):
        transport.status()


def test_closed_loop_header_cannot_relabel_legacy_pulse_counts_as_rotations(make_transport):
    transport, _ = make_transport(closed_reply(STATUS))
    with pytest.raises(LinkError):
        transport.status()
