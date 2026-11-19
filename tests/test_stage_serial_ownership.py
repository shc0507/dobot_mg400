"""Only one program can own the Arduino serial port; legacy boards remain stoppable."""

import errno
import os

import pytest
import serial
from test_stage_protocol import STATUS, FakeSerial

from stage_controller import protocol
from stage_controller.controller import MotorController
from stage_controller.protocol import ControllerError, LinkError, SerialTransport


def test_serial_port_is_opened_exclusively(monkeypatch):
    options = {}
    port = FakeSerial()

    def opened(**kwargs):
        options.update(kwargs)
        return port

    monkeypatch.setattr(protocol.serial, "Serial", opened)
    monkeypatch.setattr(protocol.time, "sleep", lambda _: None)
    transport = SerialTransport("COM7")
    try:
        assert options["exclusive"] is True
        assert options["baudrate"] == 115200
    finally:
        transport.close()


@pytest.mark.parametrize("code", [errno.EAGAIN, errno.EBUSY])
def test_busy_port_is_a_link_error(monkeypatch, code):
    def held(**kwargs):
        raise serial.SerialException(code, "port is held")

    monkeypatch.setattr(protocol.serial, "Serial", held)
    with pytest.raises(LinkError, match="in use by another program"):
        SerialTransport("COM7")


def test_legacy_pulse_status_can_connect_and_stop_but_cannot_move(monkeypatch):
    rows = [line.rstrip() + b" homed=1 limits=00 soft=1 awake=1\n" for line in STATUS]
    rows = [
        line.replace(b"ccw moving", b"cw idle").replace(b"cw continuous", b"cw idle")
        for line in rows
    ]
    port = FakeSerial([b"microstep=2\n", *rows])
    monkeypatch.setattr(protocol.serial, "Serial", lambda **_: port)
    monkeypatch.setattr(protocol.time, "sleep", lambda _: None)
    controller = MotorController(demo=True, transport_factory=SerialTransport)
    try:
        state = controller.connect("DEMO")
        assert not state["closed_loop"]
        assert all(motor["position_rotations"] is None for motor in state["motors"])
        port.lines.extend([b"microstep=2\n", *rows])
        with pytest.raises(ControllerError, match="closed-loop"):
            controller.move("bottom", "cw", 0.1, 0.1)
        assert not any(write.startswith((b"rev ", b"goto ", b"hold")) for write in port.writes)
        port.lines.extend([b"OK: All motors stopped.\n", b"microstep=2\n", *rows])
        assert controller.stop("anyone", 0) == "confirmed"
    finally:
        port.lines.extend([b"OK: All motors stopped.\n", b"microstep=2\n", *rows])
        controller.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX exclusive serial lock")
def test_second_transport_cannot_share_a_serial_port(monkeypatch):
    monkeypatch.setattr(protocol.time, "sleep", lambda _: None)
    master, slave = os.openpty()
    first = None
    try:
        name = os.ttyname(slave)
        first = SerialTransport(name)
        with pytest.raises(LinkError, match="in use by another program"):
            SerialTransport(name)
        first.close()
        SerialTransport(name).close()
    finally:
        if first is not None:
            first.close()
        os.close(slave)
        os.close(master)
