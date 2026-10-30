"""Loopback TCP transports for the shared controller and file gripper models."""

import fcntl
import ipaddress
import math
import os
import socket
import socketserver
import struct
import threading
import time

from mg400.feedback import FRAME_SIZE

from .model import Collector, Controller

FEEDBACK_PERIOD = 0.008
PORTS = (29999, 30003, 30004)


def loopback_address(host):
    """Accept numeric loopback addresses without resolving host names."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("Simulator host must be a numeric loopback address") from None
    if not address.is_loopback:
        raise ValueError("Simulator host must be a numeric loopback address")
    return address


def feedback_frame(sample):
    """Encode the fields consumed by the production feedback decoder."""
    frame = bytearray(FRAME_SIZE)
    struct.pack_into("<H", frame, 0, FRAME_SIZE)
    struct.pack_into("<Q", frame, 24, sample.mode)
    struct.pack_into("<Q", frame, 32, sample.timestamp)
    struct.pack_into("<Q", frame, 48, 0x0123456789ABCDEF)
    struct.pack_into("<d", frame, 64, sample.speed_scaling_raw or 0.0)
    struct.pack_into("<4d", frame, 432, *sample.joints_deg)
    frame[1029] = int(sample.error_status)
    return bytes(frame)


def complete_command(buffer):
    """Split one balanced controller command from a possibly partial TCP read."""
    depth = 0
    opened = False
    for index, character in enumerate(buffer):
        if character == "(":
            depth += 1
            opened = True
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced controller command")
            if opened and depth == 0:
                return buffer[: index + 1].strip(), buffer[index + 1 :].lstrip(";\r\n ")
    return None, buffer


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, runtime, port):
        self.runtime = runtime
        self.address_family = socket.AF_INET6 if runtime.address.version == 6 else socket.AF_INET
        super().__init__((str(runtime.address), port), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        runtime = self.server.runtime
        port = self.server.server_address[1]
        self.request.settimeout(0.2)
        runtime.controller.connected(port)
        try:
            if port == 30004:
                while not runtime.stopped.is_set():
                    self.request.sendall(feedback_frame(runtime.controller.sample))
                    runtime.stopped.wait(runtime.feedback_period)
                return
            buffer = ""
            while not runtime.stopped.is_set():
                try:
                    data = self.request.recv(4096)
                except TimeoutError:
                    continue
                if not data:
                    return
                buffer += data.decode("ascii")
                if len(buffer) > 65536:
                    return
                while True:
                    command, buffer = complete_command(buffer)
                    if command is None:
                        break
                    try:
                        payload = runtime.controller.execute(port, command)
                        reply = f"0,{{{payload}}},{command};"
                    except (AssertionError, ValueError, KeyError, IndexError):
                        reply = f"-1,{{}},{command};"
                    self.request.sendall(reply.encode("ascii"))
        except (OSError, ValueError, UnicodeError):
            # A malformed client or a disconnect must not stop the listeners.
            return


class Simulator:
    """Own three loopback listeners and an optional simulated gripper collector.

    Construction opens no sockets or files. Use start()/close() or a context manager.
    The shared controller models instantaneous targets, not motion physics or collisions.
    """

    def __init__(self, host="127.0.0.2", *, gripper_feedback=None, feedback_period=FEEDBACK_PERIOD):
        self.address = loopback_address(host)
        if not math.isfinite(feedback_period) or feedback_period <= 0:
            raise ValueError("Feedback period must be positive and finite")
        self.feedback_period = feedback_period
        self.controller = Controller()
        self.controller.update(mode=4)
        self.controller.jog_rate = 0.01
        self.collector = (
            Collector(feedback_path=gripper_feedback) if gripper_feedback is not None else None
        )
        self.stopped = threading.Event()
        self._servers = []
        self._threads = []
        self._collector_lock = None
        self._started = False

    def start(self):
        if self._started or self.stopped.is_set():
            raise RuntimeError("A simulator instance can start only once")
        self._started = True
        try:
            if self.collector is not None:
                path = self.collector.feedback_path
                path.parent.mkdir(parents=True, exist_ok=True)
                lock = path.with_name(path.name + ".sim.lock")
                self._collector_lock = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(self._collector_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                command_lock = path.with_name(path.name + ".command-lock.json")
                for existing in (
                    path,
                    self.collector.command_path,
                    command_lock,
                    self.collector._scratch,
                ):
                    if existing.exists() or existing.is_symlink():
                        raise ValueError(
                            f"Use a new simulator feedback path: {existing} already exists"
                        )
            # Bind every endpoint before starting any worker. A conflict leaves no partial service.
            for port in PORTS:
                self._servers.append(_Server(self, port))
            for server in self._servers:
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.02},
                    name=f"mg400-sim-{server.server_address[1]}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            ticker = threading.Thread(target=self._tick, name="mg400-sim-clock", daemon=True)
            ticker.start()
            self._threads.append(ticker)
            if self.collector is not None:
                self.collector.start()
            return self
        except BaseException:
            self.close()
            raise

    def _tick(self):
        started = time.monotonic()
        while not self.stopped.wait(self.feedback_period):
            self.controller.advance()
            self.controller.update(timestamp=int((time.monotonic() - started) * 1000))

    def close(self):
        self.stopped.set()
        self.controller.closed.set()
        if self.collector is not None:
            self.collector.close()
        for index, server in enumerate(self._servers):
            if index < len(self._threads):
                server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._servers.clear()
        self._threads.clear()
        if self._collector_lock is not None:
            os.close(self._collector_lock)
            self._collector_lock = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
