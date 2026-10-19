"""Read gripper telemetry. Enable position commands only with --commands and --output."""

import argparse
import contextlib
import fcntl
import json
import math
import os
import select
import signal
import struct
import sys
import termios
import time
import uuid
from pathlib import Path

PORT = "/dev/cu.usbserial-BG02Q5TN"
PERIOD_S = 0.05
SYS_TTY = Path("/sys/class/tty")
DEV_ROOT = Path("/dev")


def resolve_port():
    if sys.platform == "darwin":
        return PORT
    if sys.platform != "linux":
        raise RuntimeError("Gripper telemetry supports macOS and Linux only.")
    matches = []
    for tty in SYS_TTY.glob("ttyUSB*"):
        try:
            device = (tty / "device").resolve(strict=True)
        except FileNotFoundError:
            continue
        for parent in (device, *device.parents):
            try:
                identity = tuple(
                    (parent / key).read_text().strip()
                    for key in ("idVendor", "idProduct", "serial")
                )
            except FileNotFoundError:
                continue
            if identity == ("0403", "6001", "BG02Q5TN"):
                matches.append(DEV_ROOT / tty.name)
            break
    if len(matches) != 1:
        raise RuntimeError(f"Expected one FTDI 0403:6001 BG02Q5TN device. Found {len(matches)}.")
    if not matches[0].exists():
        raise FileNotFoundError(f"The serial device is not accessible: {matches[0]}")
    return str(matches[0])


def crc(data):
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return struct.pack("<H", value)


@contextlib.contextmanager
def connect():
    fd = os.open(resolve_port(), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    saved = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = termios.tcgetattr(fd)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        attrs = termios.tcgetattr(fd)
        attrs[:6] = [
            0,
            0,
            termios.CLOCAL | termios.CREAD | termios.CS8,
            0,
            termios.B115200,
            termios.B115200,
        ]
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
        yield fd
    finally:
        try:
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
        finally:
            os.close(fd)


def decode(response):
    if len(response) not in (5, 11):
        raise ValueError(f"The Modbus reply length is invalid: {len(response)}")
    if crc(response[:-2]) != response[-2:]:
        raise ValueError("The Modbus reply CRC is invalid.")
    if response[:2] != b"\x01\x03":
        raise ValueError(f"The Modbus reply identity is invalid: {response.hex()}")
    if len(response) != 11 or response[2] != 6:
        raise ValueError("The Modbus reply does not contain three registers.")
    return struct.unpack(">3H", response[3:-2])


def read(fd):
    payload = struct.pack(">BBHH", 1, 3, 0x0200, 3)
    request = payload + crc(payload)
    if os.write(fd, request) != len(request):
        raise RuntimeError("The serial read request is incomplete. No retry was sent.")
    response = bytearray()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
        if ready:
            part = os.read(fd, 256)
            if not part:
                raise ConnectionError("The serial connection closed.")
            response.extend(part)
        expected = 5 if len(response) >= 2 and response[1] & 0x80 else 11
        if len(response) >= expected:
            return decode(response)
    raise TimeoutError(f"The Modbus read timed out after {len(response)} reply bytes.")


def register(fd, address, value=None):
    """Read one register or write one allowed control value. Never retry a request."""
    if value is not None and (
        type(value) is not int or not (
            (address == 0x0103 and 0 <= value <= 1000)
            or (address, value) == (0x0402, 0)
        )
    ):
        raise ValueError("The register write is not allowed.")
    function = 3 if value is None else 6
    payload = struct.pack(">BBHH", 1, function, address, 1 if value is None else value)
    request = payload + crc(payload)
    if os.write(fd, request) != len(request):
        raise RuntimeError("The serial request is incomplete. No retry was sent.")
    response = bytearray()
    deadline = time.monotonic() + 1
    expected = 7 if value is None else 8
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
        if ready:
            part = os.read(fd, 256)
            if not part:
                raise ConnectionError("The serial connection closed.")
            response.extend(part)
        if len(response) >= 2 and response[1] & 0x80:
            expected = 5
        if len(response) >= expected:
            break
    if len(response) != expected or crc(response[:-2]) != response[-2:]:
        raise ValueError("The Modbus reply length or CRC is invalid. No retry was sent.")
    if value is not None:
        if response != request:
            raise ValueError("The Modbus write reply does not match the request.")
        return value
    if response[:3] != b"\x01\x03\x02":
        raise ValueError("The Modbus register reply is invalid.")
    return struct.unpack(">H", response[3:5])[0]


def set_position(fd, position):
    """Set a PGEA target through Modbus. Do not initialize or save to flash."""
    if type(position) is not int or not 0 <= position <= 1000:
        raise ValueError("The target position must be an integer from 0 to 1000.")
    initialized, state, measured = read(fd)
    if initialized != 1 or state not in (0, 1, 2) or not 0 <= measured <= 1000:
        raise RuntimeError("The gripper feedback is not ready for a position command.")
    force, speed = register(fd, 0x0101), register(fd, 0x0104)
    if not 20 <= force <= 100 or not 1 <= speed <= 100:
        raise RuntimeError("The gripper force or speed setting is invalid.")
    mode = register(fd, 0x0402)
    if mode not in (0, 1):
        raise RuntimeError("The gripper I/O mode is invalid.")
    if mode == 1:
        register(fd, 0x0402, 0)
    if register(fd, 0x0402) != 0:
        raise RuntimeError("The gripper did not enter Modbus-only control.")
    register(fd, 0x0103, position)
    if register(fd, 0x0103) != position:
        raise RuntimeError("The gripper target readback does not match the command.")


class PositionMailbox:
    """Read fresh commands for this collector session. Read each request once."""

    def __init__(self, output, session):
        self.path = output.with_name(output.name + ".command.json")
        self.session = session
        self.previous = None
        self.seen = set()
        self.control = dict(
            protocol=1, command_id=None, target=None, ack=False,
            locked=False, error=None, io_mode=None,
        )

    def poll(self, fd):
        if self.control["locked"]:
            return
        try:
            content = self.path.read_text()
            if content == self.previous:
                return
            self.previous = content
            request = json.loads(content)
            if not isinstance(request, dict) or request.get("session") != self.session:
                return
            command_id = request.get("id")
            if not isinstance(command_id, str) or str(uuid.UUID(command_id)) != command_id:
                return
            if command_id in self.seen:
                return
            self.seen.add(command_id)
            sent_at, position = request.get("sent_at"), request.get("position")
            if type(sent_at) not in (int, float) or not math.isfinite(sent_at):
                return
            if not 0 <= time.time() - sent_at <= 0.75:
                return
            if type(position) is not int or not 0 <= position <= 1000:
                return
        except (OSError, ValueError, OverflowError):
            return
        self.control.update(command_id=command_id, target=position, ack=False, error=None)
        try:
            set_position(fd, position)
            self.control.update(ack=True, io_mode=0)
        except (OSError, ValueError, RuntimeError) as error:
            self.control.update(locked=True, error=str(error))


def emit(sample, output=None):
    text = json.dumps(sample, separators=(",", ":"))
    if output is None:
        print(text, flush=True)
        return
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text + "\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def run(output=None, commands=False):
    session = str(uuid.uuid4())
    sequence = 0
    mailbox = PositionMailbox(output, session) if commands else None
    with connect() as fd:
        next_read = time.monotonic()
        while True:
            if mailbox is not None:
                mailbox.poll(fd)
            initialized, state, position = read(fd)
            sequence += 1
            emit(
                {
                    "session": session,
                    "seq": sequence,
                    "sent_at": time.time(),
                    "init": initialized,
                    "state": state,
                    "position": position,
                    **({"control": mailbox.control} if mailbox is not None else {}),
                },
                output,
            )
            next_read = max(next_read + PERIOD_S, time.monotonic())
            time.sleep(max(0, next_read - time.monotonic()))


def terminate(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Replace this file with each complete sample.")
    parser.add_argument(
        "--commands", action="store_true",
        help="Accept fresh position commands beside --output through the same serial connection.",
    )
    args = parser.parse_args(argv)
    if args.commands and args.output is None:
        parser.error("--commands requires --output")
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        run(args.output, commands=args.commands)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Gripper feedback stopped: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
