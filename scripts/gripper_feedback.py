"""Send read-only gripper telemetry to stdout or a JSON file. Serial settings are fixed."""

import argparse
import contextlib
import fcntl
import json
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


def run(output=None):
    session = str(uuid.uuid4())
    sequence = 0
    with connect() as fd:
        next_read = time.monotonic()
        while True:
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
    args = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        run(args.output)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Gripper feedback stopped: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
