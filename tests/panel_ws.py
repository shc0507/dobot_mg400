"""WebSocket client and server helpers for robot service tests.

They speak to the real PanelServer of apps/control_panel/server.py over real sockets.
"""

import contextlib
import itertools
import json
import queue
import runpy
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from mg400.websocket import (
    CLOSE,
    PING,
    PONG,
    TEXT,
    ProtocolError,
    encode_close,
    encode_frame,
    read_frame,
)

PANEL = runpy.run_path(str(Path(__file__).resolve().parents[1] / "apps/control_panel/server.py"))
PanelServer = PANEL["PanelServer"]
SocketSession = PANEL["SocketSession"]
TOKEN = "test-token"
CLIENT = "browser-a"
KEY = "dGhlIHNhbXBsZSBub25jZQ=="
# The end-to-end test replaces socket.socket with a simulated MG400. Test clients keep
# the real class from import time.
RealSocket = socket.socket
OPEN_CLIENTS = []
DROP = object()


def hello_message(client=CLIENT, token=TOKEN, **changes):
    """Return a hello. changes replace fields; DROP removes one."""
    message = {"type": "hello", "protocol": 1, "client": client, "token": token, **changes}
    return {name: value for name, value in message.items() if value is not DROP}


@contextlib.contextmanager
def running(state, *, token=TOKEN, base_path="/", server_class=PanelServer):
    server = server_class(("127.0.0.1", 0), state, token=token, base_path=base_path)
    # A short poll interval keeps shutdown() fast.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def upgrade_request(path="/api/ws", *, version="HTTP/1.1", host="127.0.0.1", change=None, extra=()):
    """Return an upgrade request. change replaces base headers; None removes one."""
    headers = {
        "Host": host,
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": KEY,
        "Sec-WebSocket-Version": "13",
    }
    headers.update(change or {})
    lines = [f"GET {path} {version}"]
    lines += [f"{name}: {value}" for name, value in headers.items() if value is not None]
    lines += list(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def connect(server, timeout=3):
    sock = RealSocket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(server.server_address)
    return sock


def parse_head(head):
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        if line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    return int(status_line.split()[1]), headers


class WsClient:
    """Speak to api/ws in the client role over a raw socket. No WebSocket library.

    A reader thread turns server frames into events: ("message", dict), ("close", code,
    reason), ("ping", bytes), ("pong", bytes) and finally ("end", monotonic time, error).
    """

    def __init__(self, server, raw=None):
        self.sock = connect(server)
        OPEN_CLIENTS.append(self)
        self.sock.sendall(upgrade_request() if raw is None else raw)
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            chunk = self.sock.recv(1)
            assert chunk, f"The service closed the connection after {head!r}"
            head += chunk
        self.status, self.headers = parse_head(head[:-4])
        assert self.status == 101, head
        self.events = queue.Queue()
        self.replies = {}
        self._ids = itertools.count(1)
        self._send_lock = threading.Lock()
        self._stopping = threading.Event()
        # A short timeout lets reset() stop the reader without sending FIN.
        self.sock.settimeout(0.1)
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    def _read(self, count):
        data = b""
        while len(data) < count:
            try:
                chunk = self.sock.recv(count - len(data))
            except TimeoutError:
                if self._stopping.is_set():
                    raise EOFError("The test stopped reading") from None
                continue
            if not chunk:
                raise EOFError("The service closed the connection")
            data += chunk
        return data

    def _read_events(self):
        try:
            while True:
                fin, opcode, payload = read_frame(self._read, masked=False, max_payload=1 << 20)
                assert fin, "The service never fragments its messages"
                if opcode == TEXT:
                    message = json.loads(payload)
                    if message.get("type") == "reply":
                        self.replies[message["id"]] = message
                    self.events.put(("message", message))
                elif opcode == CLOSE:
                    code = struct.unpack("!H", payload[:2])[0] if payload else None
                    self.events.put(("close", code, payload[2:].decode("utf-8")))
                elif opcode in (PING, PONG):
                    self.events.put(("ping" if opcode == PING else "pong", payload))
                else:
                    self.events.put(("unexpected", opcode, payload))
        except (EOFError, OSError, ProtocolError) as error:
            self.events.put(("end", time.monotonic(), error))

    def send(self, opcode, payload=b"", *, fin=True, mask=True):
        self.send_raw(encode_frame(opcode, payload, fin=fin, mask=mask))

    def send_raw(self, data):
        with self._send_lock:
            self.sock.sendall(data)

    def send_json(self, message):
        self.send(TEXT, json.dumps(message).encode())

    def send_request(self, kind, body, request_id=None):
        request_id = next(self._ids) if request_id is None else request_id
        self.send_json({"type": kind, "id": request_id, "body": body})
        return request_id

    def next(self, timeout=2):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(f"No event arrived within {timeout} s")

    def wait(self, predicate, timeout=2, what="event"):
        """Consume events until one matches. The end of the connection fails the wait."""
        deadline = time.monotonic() + timeout
        seen = []
        while True:
            try:
                event = self.events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail(f"No {what} within {timeout} s; seen {seen[-5:]!r}")
            if predicate(event):
                return event
            seen.append(event)
            if event[0] == "end":
                pytest.fail(f"The connection ended before the {what}; seen {seen[-5:]!r}")

    def state(self, timeout=2):
        event = self.wait(lambda e: e[0] == "message" and e[1]["type"] == "state", timeout, "state")
        return event[1]["state"]

    def reply(self, request_id, timeout=2):
        event = self.wait(
            lambda e: e[0] == "message" and e[1].get("id") == request_id, timeout, "reply"
        )
        return event[1]

    def request(self, kind, body):
        return self.reply(self.send_request(kind, body))

    def hello(self, client=CLIENT, token=TOKEN):
        """Send hello and return the first message, which must be a state push."""
        self.send_json(hello_message(client, token))
        event = self.next()
        assert event[0] == "message" and event[1]["type"] == "state", event
        return event[1]["state"]

    def refused(self, timeout=3):
        """Wait for the service's error and close. Echo the close. Return (error, code)."""
        error = self.wait(lambda e: e[0] == "message" and e[1]["type"] == "error", timeout)[1]
        close = self.wait(lambda e: e[0] == "close", timeout, "close")
        self.send(CLOSE, encode_close(close[1]))
        self.ended(timeout)
        return error, close[1]

    def closed_by_service(self, timeout=3):
        """Wait for the service's close frame, echo it, and return (code, reason)."""
        close = self.wait(lambda e: e[0] == "close", timeout, "close")
        self.send(CLOSE, encode_close(close[1]))
        self.ended(timeout)
        return close[1], close[2]

    def ended(self, timeout=3):
        """Wait for the end of the connection. Return the time when the reader saw it."""
        return self.wait(lambda e: e[0] == "end", timeout, "end of the connection")[1]

    def close(self, code=1000, reason=""):
        """Close cleanly. Return the code of the service's echo."""
        self.send(CLOSE, encode_close(code, reason))
        echo = self.wait(lambda e: e[0] == "close", 3, "close echo")
        self.ended()
        return echo[1]

    def reset(self):
        """End the connection with a TCP reset, as a crashed browser or a lost link does."""
        self._stopping.set()
        self._reader.join(2)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.sock.close()

    def discard(self):
        self._stopping.set()
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self._reader.join(2)
        self.sock.close()
