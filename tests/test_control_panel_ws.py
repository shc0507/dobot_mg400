"""Check the robot service WebSocket endpoint over real sockets with fake panel states."""

import base64
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
from types import SimpleNamespace

import pytest

from mg400_agent.websocket import (
    BINARY,
    CLOSE,
    CONTINUATION,
    PING,
    PONG,
    TEXT,
    ProtocolError,
    accept_key,
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


def wait_for(predicate, timeout=3, describe=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), describe() if describe else "The condition was not reached"


class RecordingState:
    """Record what the service passes to a robot-mode panel state."""

    mode = "robot"

    def __init__(self):
        self.calls = []
        self.failure = None
        self.message = "Ready."
        self._versions = itertools.count(1)

    def snapshot(self):
        # Every snapshot has a new version, as in RobotPanelState.
        return {"mode": self.mode, "version": next(self._versions), "message": self.message}

    def _call(self, call):
        self.calls.append(call)
        if self.failure is not None:
            raise self.failure
        return self.snapshot()

    def command(self, payload, client_id=None):
        return self._call(("command", payload, client_id))

    def heartbeat(self, client_id):
        return self._call(("heartbeat", client_id))

    def keys(self, payload, client_id):
        return self._call(("keys", payload, client_id))

    def close(self):
        pass


class LosingState(RecordingState):
    """Also record connection_lost, which RobotPanelState provides."""

    def connection_lost(self, client_id):
        self.calls.append(("connection_lost", client_id))

    def losses(self, client=CLIENT):
        return self.calls.count(("connection_lost", client))


class ReadOnlyState:
    mode = "read-only"

    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"mode": self.mode, "message": "Read-only mode."}

    def command(self, payload):
        self.calls.append(("command", payload))
        return self.snapshot()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def closed_clients():
    yield
    while OPEN_CLIENTS:
        OPEN_CLIENTS.pop().discard()


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


def http_request(method, path, *, host="127.0.0.1", headers=(), body=b""):
    lines = [f"{method} {path} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines += ["Connection: close", *headers]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


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


def exchange(server, raw):
    """Send raw bytes, read until the service closes, and return (status, headers, body)."""
    received = b""
    with contextlib.closing(connect(server, timeout=5)) as sock:
        sock.sendall(raw)
        try:
            while chunk := sock.recv(65536):
                received += chunk
        except ConnectionResetError:
            pass
        except TimeoutError:
            pytest.fail(f"The service kept the connection open after {received[:60]!r}")
    head, _, body = received.partition(b"\r\n\r\n")
    status, headers = parse_head(head)
    return status, headers, body


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


def opened(server, raw=None, *, client=CLIENT, token=TOKEN):
    """Open a WebSocket and complete hello."""
    connection = WsClient(server, raw)
    connection.hello(client, token)
    return connection


@pytest.fixture
def losing():
    state = LosingState()
    with running(state) as server:
        yield server, state


# Handshake


def test_upgrade_switches_protocols_without_extensions_or_subprotocols(losing):
    server, state = losing
    offered = ["Sec-WebSocket-Extensions: permessage-deflate", "Sec-WebSocket-Protocol: chat"]
    client = WsClient(server, upgrade_request(extra=offered))
    assert client.headers["upgrade"].lower() == "websocket"
    assert client.headers["connection"].lower() == "upgrade"
    assert client.headers["sec-websocket-accept"] == accept_key(KEY)
    assert "sec-websocket-extensions" not in client.headers
    assert "sec-websocket-protocol" not in client.headers
    assert state.calls == []
    assert client.hello()["mode"] == "robot"


@pytest.mark.parametrize(
    "request_bytes",
    [
        upgrade_request(version="HTTP/1.0"),
        upgrade_request(change={"Upgrade": None}),
        upgrade_request(change={"Upgrade": "h2c"}),
        upgrade_request(change={"Connection": None}),
        upgrade_request(change={"Connection": "keep-alive"}),
        upgrade_request(change={"Sec-WebSocket-Version": "8"}),
        upgrade_request(change={"Sec-WebSocket-Version": None}),
    ],
    ids=[
        "http-1.0",
        "no-upgrade",
        "upgrade-h2c",
        "no-connection",
        "connection-without-upgrade",
        "version-8",
        "no-version",
    ],
)
def test_upgrade_without_the_websocket_tokens_or_version_13_gets_426(losing, request_bytes):
    server, state = losing
    status, headers, _ = exchange(server, request_bytes)
    assert status == 426
    assert headers["upgrade"] == "websocket"
    assert headers["sec-websocket-version"] == "13"
    assert headers["connection"].lower() == "close"
    assert state.calls == []


@pytest.mark.parametrize(
    "request_bytes",
    [
        upgrade_request(change={"Connection": "keep-alive, Upgrade"}),
        upgrade_request(
            change={"Connection": None}, extra=["Connection: keep-alive", "Connection: upgrade"]
        ),
        upgrade_request(change={"Connection": "UPGRADE", "Upgrade": "WebSocket"}),
    ],
    ids=["firefox-token-list", "tokens-on-two-lines", "upper-case"],
)
def test_upgrade_accepts_token_lists_in_any_case(losing, request_bytes):
    server, _ = losing
    client = WsClient(server, request_bytes)
    assert client.headers["sec-websocket-accept"] == accept_key(KEY)
    assert client.hello()["mode"] == "robot"


@pytest.mark.parametrize(
    "request_bytes",
    [
        upgrade_request(change={"Sec-WebSocket-Key": None}),
        upgrade_request(change={"Sec-WebSocket-Key": "abc"}),
        upgrade_request(change={"Sec-WebSocket-Key": base64.b64encode(bytes(15)).decode()}),
        upgrade_request(change={"Sec-WebSocket-Key": base64.b64encode(bytes(17)).decode()}),
        upgrade_request(extra=[f"Sec-WebSocket-Key: {base64.b64encode(bytes(16)).decode()}"]),
    ],
    ids=["missing", "short", "15-bytes", "17-bytes", "duplicate"],
)
def test_upgrade_with_a_missing_invalid_or_duplicate_key_gets_400(losing, request_bytes):
    server, state = losing
    status, headers, body = exchange(server, request_bytes)
    assert status == 400
    assert json.loads(body) == {"error": "The WebSocket key is invalid"}
    assert headers["connection"].lower() == "close"
    assert state.calls == []


@pytest.mark.parametrize(
    ("host", "headers"),
    [
        ("panel.test:8766", ["Origin: http://evil.test"]),
        ("panel.test:8766", ["Origin: null"]),
        ("panel.test:8766", ["Origin: http://panel.test:8766", "Origin: http://panel.test:8766"]),
        ("panel.test:8766", ["Origin: http://panel.test:8766", "Sec-Fetch-Site: cross-site"]),
        ("panel.test:8766", ["Origin: http://panel.test:8766/panel"]),
        ("panel.test:8766", ["Origin: ws://panel.test:8766"]),
        ("panel.test:8766", ["Origin: http://panel.test:8767"]),
        ("panel.test:8766", ["Origin: http://panel.test:port"]),
        ("panel.test", ["Origin: http://panel.test.evil.test"]),
        ("panel.example.com:80", ["Origin: https://panel.example.com"]),
        ("panel.example.com", ["Origin: http://panel.example.com:443"]),
        (
            "127.0.0.1:8766",
            [
                "X-Forwarded-Host: proxy.internal, panel.example.com",
                "Origin: https://panel.example.com",
            ],
        ),
        ("127.0.0.1:8766", ["Sec-Fetch-Site: cross-site"]),
    ],
    ids=[
        "other-site",
        "null",
        "two-origins",
        "sec-fetch-site-cross-site",
        "origin-with-path",
        "ws-scheme",
        "other-port",
        "bad-port",
        "suffix-host",
        "https-origin-vs-port-80",
        "http-origin-on-port-443",
        "second-forwarded-host",
        "cross-site-without-origin",
    ],
)
def test_cross_site_upgrade_gets_403(losing, host, headers):
    server, state = losing
    status, response_headers, body = exchange(server, upgrade_request(host=host, extra=headers))
    assert status == 403
    assert json.loads(body) == {"error": "Open the panel page from this server"}
    assert response_headers["connection"].lower() == "close"
    assert state.calls == []


@pytest.mark.parametrize(
    ("host", "headers"),
    [
        ("panel.test:8766", ["Origin: http://panel.test:8766"]),
        ("panel.test:8766", ["Origin: http://PANEL.Test:8766", "Sec-Fetch-Site: same-origin"]),
        ("127.0.0.1:8766", ["Origin: http://127.0.0.1:8766"]),
        ("[::1]:8766", ["Origin: http://[::1]:8766"]),
        ("panel.test", ["Origin: http://panel.test:80"]),
        ("panel.example.com", ["Origin: https://panel.example.com"]),
        ("panel.example.com:443", ["Origin: https://panel.example.com"]),
        (
            "127.0.0.1:8766",
            ["X-Forwarded-Host: panel.example.com", "Origin: https://panel.example.com"],
        ),
        (
            "127.0.0.1:8766",
            [
                "X-Forwarded-Host: panel.example.com, proxy.internal",
                "Origin: https://panel.example.com",
            ],
        ),
        ("127.0.0.1:8766", []),
    ],
    ids=[
        "same-host-and-port",
        "host-case-and-same-origin-fetch",
        "loopback",
        "ipv6-loopback",
        "explicit-default-port",
        "https-origin-host-without-port",
        "https-origin-host-with-443",
        "forwarded-host",
        "first-forwarded-host",
        "program-without-origin",
    ],
)
def test_same_origin_and_programs_without_origin_are_accepted(losing, host, headers):
    server, _ = losing
    client = WsClient(server, upgrade_request(host=host, extra=headers))
    assert client.hello()["mode"] == "robot"


def test_upgrade_request_with_a_body_is_refused(losing):
    server, state = losing
    raw = upgrade_request(extra=["Content-Length: 5"]) + b"hello"
    status, headers, _ = exchange(server, raw)
    assert status == 400
    assert headers["connection"].lower() == "close"
    assert state.calls == []


def test_websocket_routes_under_the_base_path():
    state = RecordingState()
    # The --base-path value without a trailing slash, as an operator types it.
    with running(state, base_path="/mg400-panel") as server:
        for path in ("/api/ws", "/mg400-panelapi/ws", "/mg400-panel/api/ws/", "/mg400-panel/ws"):
            assert exchange(server, upgrade_request(path))[0] == 404, path
        client = opened(server, upgrade_request("/mg400-panel/api/ws"))
        assert client.request("heartbeat", {})["status"] == 200
    assert state.calls == [("heartbeat", CLIENT)]


ROBOT_POST = ["Content-Type: application/json", "X-MG400-Panel: 1", f"X-MG400-Client: {CLIENT}"]
STOP = json.dumps({"action": "stop"}).encode()


def panel_requests(host, extra=()):
    extra = list(extra)
    return {
        "page": http_request("GET", "/", host=host, headers=extra),
        "state": http_request("GET", "/api/state", host=host, headers=extra),
        "command": http_request(
            "POST", "/api/command", host=host, headers=[*ROBOT_POST, *extra], body=STOP
        ),
        "websocket": upgrade_request(host=host, extra=extra) if host is not None else None,
    }


@pytest.mark.parametrize(
    "host",
    [
        "panel.example.com",
        "panel.example.com:8766",
        "192.168.1.5:8766",
        "localhost.evil.test",
        "evil.test:8766@127.0.0.1",
        "",
        None,
    ],
    ids=[
        "name",
        "name-and-port",
        "lan-address",
        "localhost-prefix",
        "userinfo",
        "empty",
        "missing",
    ],
)
def test_tokenless_service_refuses_hosts_other_than_loopback(host):
    state = RecordingState()
    with running(state, token=None) as server:
        for kind, raw in panel_requests(host).items():
            if raw is None:
                raw = upgrade_request().replace(b"Host: 127.0.0.1\r\n", b"")
            status, _, body = exchange(server, raw)
            assert status == 403, kind
            assert json.loads(body) == {"error": "Open the panel at 127.0.0.1 or localhost"}
    assert state.calls == []


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8766",
        "127.8.9.10:1",
        "localhost:8766",
        "LOCALHOST",
        "[::1]:8766",
        "[::1]",
    ],
)
def test_tokenless_service_accepts_loopback_hosts(host):
    state = RecordingState()
    with running(state, token=None) as server:
        requests = panel_requests(host)
        for kind in ("page", "state", "command"):
            assert exchange(server, requests[kind])[0] == 200, kind
        # Without a token, hello may carry any token string.
        client = opened(server, requests["websocket"], token="anything")
        assert client.request("heartbeat", {})["status"] == 200
    assert state.calls == [("command", {"action": "stop"}, CLIENT), ("heartbeat", CLIENT)]


FORWARDING = [
    "Forwarded: for=192.0.2.1;host=127.0.0.1",
    "X-Forwarded-For: 192.0.2.1",
    "X-Forwarded-Host: 127.0.0.1",
    "X-Real-IP: 192.0.2.1",
    "X-Forwarded-For: ",
]


@pytest.mark.parametrize(
    "header",
    FORWARDING,
    ids=["forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "empty-value"],
)
def test_tokenless_service_refuses_proxied_requests_with_a_loopback_host(header):
    state = RecordingState()
    # A proxy on this host can rewrite Host to a loopback name. Proxies need a token.
    with running(state, token=None) as server:
        for kind, raw in panel_requests("127.0.0.1:8766", [header]).items():
            status, _, body = exchange(server, raw)
            assert status == 403, kind
            assert json.loads(body) == {"error": "Open the panel at 127.0.0.1 or localhost"}
    assert state.calls == []


def test_token_service_accepts_proxied_requests(losing):
    server, state = losing
    proxy = [line for line in FORWARDING if not line.startswith("X-Forwarded-Host")]
    authorized = [f"Authorization: Bearer {TOKEN}", *proxy]
    requests = panel_requests("panel.example.com", authorized)
    for kind in ("state", "command"):
        assert exchange(server, requests[kind])[0] == 200, kind
    # X-Forwarded-Host still feeds the origin check.
    origin = ["X-Forwarded-Host: panel.example.com", "Origin: https://panel.example.com"]
    raw = upgrade_request(host="127.0.0.1:8766", extra=[*proxy, *origin])
    assert opened(server, raw).request("heartbeat", {})["status"] == 200
    assert state.calls == [("command", {"action": "stop"}, CLIENT), ("heartbeat", CLIENT)]


def test_api_keys_over_http_is_404(losing):
    server, state = losing
    keys = json.dumps({"seq": 1, "held": ["W"]}).encode()
    headers = [f"Authorization: Bearer {TOKEN}", *ROBOT_POST]
    assert exchange(server, http_request("POST", "/api/keys", headers=headers, body=keys))[0] == 404
    assert exchange(server, http_request("GET", "/api/keys", headers=headers))[0] == 404
    assert state.calls == []


def test_upgrade_is_logged_once_and_messages_are_not(losing, capsys):
    server, _ = losing
    client = opened(server)
    for _ in range(3):
        assert client.request("heartbeat", {})["status"] == 200
    assert client.close() == 1000
    log = capsys.readouterr().err
    assert log.count('"GET /api/ws HTTP/1.1" 101') == 1
    assert "heartbeat" not in log and "test-token" not in log


def test_websocket_commands_are_logged_and_keys_and_heartbeats_are_quiet(losing, capsys):
    server, state = losing
    client = opened(server, client="browser-0123456789abcdef")
    assert client.request("command", {"action": "start_sync"})["status"] == 200
    for seq in range(1, 4):
        assert client.request("keys", {"seq": seq, "held": ["W"]})["status"] == 200
        assert client.request("heartbeat", {})["status"] == 200
    assert client.request("heartbeat", {"refused": True})["status"] == 400
    state.failure = PermissionError("Start sync in this browser before issuing commands")
    assert client.request("command", {"action": "stop"})["status"] == 403
    state.failure = KeyError("internal detail")
    assert client.request("command", {"action": "home"})["status"] == 500
    state.failure = None
    for body in ({"action": 'stop" 200 client=forged'}, {"action": 5}, {}, ["stop"]):
        assert client.request("command", body)["status"] == 200
    assert client.close() == 1000
    log = capsys.readouterr().err
    lines = [line.split("] ", 1)[1] for line in log.splitlines() if '"WS ' in line]
    assert lines == [
        '"WS command start_sync" 200 client=browser-',
        '"WS command stop" 403 client=browser- Start sync in this browser before issuing commands',
        '"WS command home" 500 client=browser- The robot service failed to handle the request',
        *['"WS command invalid" 200 client=browser-'] * 4,
    ]
    assert "keys" not in log and "heartbeat" not in log
    assert TOKEN not in log and "browser-0123456789abcdef" not in log
    assert "forged" not in log


@pytest.mark.skipif(not hasattr(socket, "TCP_USER_TIMEOUT"), reason="Linux socket option")
def test_service_sets_tcp_user_timeout_on_the_connection(losing):
    server, _ = losing
    sessions = []
    admit = server.admit_socket
    server.admit_socket = lambda session: (sessions.append(session), admit(session))[1]
    opened(server)
    value = sessions[0]._socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)
    assert value == SocketSession.UNACKNOWLEDGED_MS == 5000


# Hello


def test_first_message_must_be_hello(losing):
    server, state = losing
    client = WsClient(server)
    client.send_request("command", {"action": "stop"})
    error, code = client.refused()
    assert error == {
        "type": "error",
        "status": 400,
        "error": "Send hello with the protocol, a client ID and the access token first",
    }
    assert code == 4400
    assert state.calls == []


@pytest.mark.parametrize("token", ["wrong", "", TOKEN + "x", TOKEN[:-1], "tést-token"])
def test_hello_with_a_wrong_or_missing_token_gets_401_and_close_4401(losing, token):
    server, state = losing
    client = WsClient(server)
    client.send_json(hello_message(token=token))
    error, code = client.refused()
    assert error == {"type": "error", "status": 401, "error": "Enter the panel access token"}
    assert code == 4401
    assert state.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "[]",
        json.dumps(hello_message(token=DROP)),
        json.dumps(hello_message(client=DROP)),
        json.dumps(hello_message(protocol=DROP)),
        json.dumps(hello_message(extra=1)),
        json.dumps(hello_message(type="Hello")),
        json.dumps(hello_message(protocol="1")),
        json.dumps(hello_message(protocol=True)),
        json.dumps(hello_message(protocol=1.0)),
        json.dumps(hello_message(protocol=None)),
        json.dumps(hello_message(client="")),
        json.dumps(hello_message(client="   ")),
        json.dumps(hello_message(client="c" * 129)),
        json.dumps(hello_message(client="brówser")),
        json.dumps(hello_message(client="browser\na")),
        json.dumps(hello_message(client="browser\ta")),
        json.dumps(hello_message(client="browser\x1b[31m")),
        json.dumps(hello_message(client="browser\x7f")),
        json.dumps(hello_message(client=7)),
        json.dumps(hello_message(token=None)),
        "[" * 2000 + "]" * 2000,
    ],
    ids=[
        "not-json",
        "array",
        "no-token",
        "no-client",
        "no-protocol",
        "extra-field",
        "wrong-type",
        "string-protocol",
        "boolean-protocol",
        "float-protocol",
        "null-protocol",
        "empty-client",
        "blank-client",
        "long-client",
        "non-ascii-client",
        "newline-client",
        "tab-client",
        "escape-client",
        "delete-client",
        "number-client",
        "null-token",
        "deep-nesting",
    ],
)
def test_malformed_hello_gets_400_and_close_4400(losing, text):
    server, state = losing
    client = WsClient(server)
    client.send(TEXT, text.encode())
    error, code = client.refused()
    assert error["status"] == 400 and code == 4400
    assert state.calls == []


@pytest.mark.parametrize("protocol", [0, 2, -1, 2**53])
def test_hello_with_another_protocol_gets_426_and_close_4426(losing, protocol):
    server, state = losing
    client = WsClient(server)
    client.send_json(hello_message(protocol=protocol))
    error, code = client.refused()
    assert error == {
        "type": "error",
        "status": 426,
        "error": "The robot service uses protocol 1. Reload the page.",
    }
    assert code == 4426
    assert state.calls == []


@pytest.mark.parametrize(
    "client_id",
    ["c" * 128, "x", "browser a", "~!@#$%^&*()_+{}|:<>?`-=[]\\;',./\""],
    ids=["128-characters", "one-character", "inner-space", "punctuation"],
)
def test_hello_accepts_printable_ascii_client_ids(losing, client_id):
    server, state = losing
    client = opened(server, client=client_id)
    assert client.request("heartbeat", {})["status"] == 200
    assert state.calls == [("heartbeat", client_id)]


def test_hello_sent_with_the_upgrade_request_is_accepted(losing):
    server, _ = losing
    hello = json.dumps(hello_message()).encode()
    client = WsClient(server, upgrade_request() + encode_frame(TEXT, hello, mask=True))
    assert client.state()["mode"] == "robot"


def test_fragmented_hello_is_accepted(losing):
    server, _ = losing
    client = WsClient(server)
    hello = json.dumps(hello_message()).encode()
    client.send(TEXT, hello[:10], fin=False)
    client.send(PING, b"between")
    client.send(CONTINUATION, hello[10:])
    assert client.next() == ("pong", b"between")
    assert client.state()["mode"] == "robot"


def track_handler_ends(server):
    """Record when each WebSocket handler finishes its session."""
    ends = []
    discharge = server.discharge_socket

    def recorded(session):
        discharge(session)
        ends.append(time.monotonic())

    server.discharge_socket = recorded
    return ends


def test_hello_must_arrive_before_the_deadline(losing, monkeypatch):
    server, state = losing
    assert SocketSession.HELLO_SECONDS == 3.0
    monkeypatch.setattr(SocketSession, "HELLO_SECONDS", 0.3)
    handler_ends = track_handler_ends(server)
    started = time.monotonic()
    client = WsClient(server)
    ended = client.ended()
    assert 0.25 < ended - started < 1.5
    # The aborted session skips the close wait (DRAIN_SECONDS = 2 s).
    wait_for(lambda: handler_ends, timeout=1.5)
    assert handler_ends[0] - ended < 0.5
    assert state.calls == []


def test_trickled_hello_is_cut_off_at_the_deadline(losing, monkeypatch):
    server, state = losing
    monkeypatch.setattr(SocketSession, "HELLO_SECONDS", 0.4)
    handler_ends = track_handler_ends(server)
    client = WsClient(server)
    started = time.monotonic()
    hello = json.dumps(hello_message()).encode()
    frame = encode_frame(TEXT, hello, mask=True)
    # Each byte resets a socket timeout. Only the total deadline ends the connection.
    sent = 0
    with contextlib.suppress(OSError):
        for byte in frame:
            client.send_raw(bytes([byte]))
            sent += 1
            with contextlib.suppress(queue.Empty):
                event = client.events.get(timeout=0.03)
                assert event[0] == "end", event
                break
    ended = client.ended(timeout=2) if sent == len(frame) else time.monotonic()
    assert sent < len(frame)
    assert 0.35 < ended - started < 1.2
    # The handler finishes promptly: an aborted session skips the 2 s close wait.
    wait_for(lambda: handler_ends, timeout=1.5)
    assert handler_ends[0] - started < 1.2
    assert state.calls == []


def test_control_frames_do_not_extend_the_hello_deadline(losing, monkeypatch):
    server, state = losing
    monkeypatch.setattr(SocketSession, "HELLO_SECONDS", 0.4)
    client = WsClient(server)
    started = time.monotonic()
    pongs = 0
    with contextlib.suppress(OSError):
        while time.monotonic() - started < 2:
            client.send(PING, b"wait")
            event = client.next()
            if event[0] == "end":
                ended = event[1]
                break
            assert event == ("pong", b"wait")
            pongs += 1
            time.sleep(0.03)
    assert pongs >= 3
    assert 0.35 < ended - started < 1.2
    assert state.calls == []


def test_tokenless_hello_accepts_any_token():
    state = RecordingState()
    with running(state, token=None) as server:
        client = opened(server, token="anything at all")
        assert client.request("keys", {"seq": 1, "held": []})["status"] == 200
    assert state.calls == [("keys", {"seq": 1, "held": []}, CLIENT)]


# Requests


def test_requests_reach_the_state_with_the_hello_client_id(losing):
    server, state = losing
    client = opened(server)
    command = client.request("command", {"action": "start_sync"})
    heartbeat = client.request("heartbeat", {})
    keys = client.request("keys", {"seq": 2, "held": ["W"]})
    for request_id, reply in enumerate((command, heartbeat, keys), start=1):
        assert set(reply) == {"type", "id", "status", "state"}
        assert reply["type"] == "reply" and reply["id"] == request_id
        assert reply["status"] == 200 and reply["state"]["mode"] == "robot"
    assert state.calls == [
        ("command", {"action": "start_sync"}, CLIENT),
        ("heartbeat", CLIENT),
        ("keys", {"seq": 2, "held": ["W"]}, CLIENT),
    ]


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (ValueError("Key input must contain only seq and held"), 400),
        (PermissionError("Start sync in this browser before issuing commands"), 403),
        (RuntimeError("The panel is closing"), 409),
    ],
    ids=["invalid", "not-owner", "conflict"],
)
@pytest.mark.parametrize("kind", ["command", "heartbeat", "keys"])
def test_state_refusals_become_error_replies_and_keep_the_connection(losing, kind, failure, status):
    server, state = losing
    client = opened(server)
    body = {"heartbeat": {}, "keys": {"seq": 1, "held": []}, "command": {"action": "stop"}}[kind]
    state.failure = failure
    reply = client.request(kind, body)
    assert reply == {"type": "reply", "id": 1, "status": status, "error": str(failure)}
    state.failure = None
    assert client.request(kind, body)["status"] == 200
    assert state.losses() == 0


def test_unexpected_state_failure_gives_500_and_keeps_the_connection(losing, capsys):
    server, state = losing
    client = opened(server)
    state.failure = KeyError("internal detail")
    reply = client.request("command", {"action": "stop"})
    assert reply == {
        "type": "reply",
        "id": 1,
        "status": 500,
        "error": "The robot service failed to handle the request",
    }
    state.failure = None
    assert client.request("command", {"action": "stop"})["status"] == 200
    assert "WebSocket command request failed" in capsys.readouterr().err


def test_heartbeat_with_a_body_gets_400_and_keeps_the_connection(losing):
    server, state = losing
    client = opened(server)
    reply = client.request("heartbeat", {"seq": 1})
    assert reply == {
        "type": "reply",
        "id": 1,
        "status": 400,
        "error": "Heartbeat must be an empty JSON object",
    }
    assert client.request("heartbeat", {})["status"] == 200
    assert state.calls == [("heartbeat", CLIENT)]


def test_replies_follow_request_order(losing):
    server, state = losing
    client = opened(server)
    kinds = itertools.cycle(["command", "heartbeat", "keys"])
    bodies = {"command": {"action": "stop"}, "heartbeat": {}, "keys": {"seq": 1, "held": []}}
    requests = [(next(kinds), request_id) for request_id in range(1, 31)]
    client.send_raw(
        b"".join(
            encode_frame(
                TEXT,
                json.dumps({"type": kind, "id": request_id, "body": bodies[kind]}).encode(),
                mask=True,
            )
            for kind, request_id in requests
        )
    )
    replies = []
    while len(replies) < len(requests):
        event = client.wait(lambda e: e[0] == "message" and e[1]["type"] == "reply")
        replies.append(event[1]["id"])
    assert replies == list(range(1, 31))
    assert [call[0] for call in state.calls] == [kind for kind, _ in requests]


def test_request_ids_cover_zero_to_two_to_the_53(losing):
    server, _ = losing
    client = opened(server)
    for request_id in (0, 2**53):
        client.send_request("heartbeat", {}, request_id)
        assert client.reply(request_id)["status"] == 200


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1]",
        json.dumps({"type": "heartbeat", "body": {}}),
        json.dumps({"type": "heartbeat", "id": 1}),
        json.dumps({"type": "heartbeat", "id": 1, "body": {}, "extra": 1}),
        json.dumps({"type": "hello", "id": 1, "body": {}}),
        json.dumps({"type": "heartbeat", "id": -1, "body": {}}),
        json.dumps({"type": "heartbeat", "id": 2**53 + 1, "body": {}}),
        json.dumps({"type": "heartbeat", "id": 1.0, "body": {}}),
        json.dumps({"type": "heartbeat", "id": True, "body": {}}),
        json.dumps({"type": "heartbeat", "id": "1", "body": {}}),
        json.dumps(hello_message()),
    ],
    ids=[
        "not-json",
        "array",
        "no-id",
        "no-body",
        "extra-field",
        "unknown-type",
        "negative-id",
        "id-over-2-53",
        "float-id",
        "boolean-id",
        "string-id",
        "second-hello",
    ],
)
def test_bad_envelope_gets_400_and_close_4400(losing, text):
    server, state = losing
    client = opened(server)
    client.send(TEXT, text.encode())
    error, code = client.refused()
    assert error == {
        "type": "error",
        "status": 400,
        "error": "Send requests as JSON objects with type, id and body",
    }
    assert code == 4400
    assert state.calls == [("connection_lost", CLIENT)]


def test_read_only_service_refuses_heartbeat_and_keys_over_the_socket():
    state = ReadOnlyState()
    with running(state) as server:
        client = opened(server)
        heartbeat = client.request("heartbeat", {})
        keys = client.request("keys", {"seq": 1, "held": ["W"]})
        command = client.request("command", {"action": "start_sync"})
        assert heartbeat == {
            "type": "reply",
            "id": 1,
            "status": 400,
            "error": "Heartbeat is only available in robot mode",
        }
        assert keys == {
            "type": "reply",
            "id": 2,
            "status": 403,
            "error": "Keyboard control is unavailable in read-only mode",
        }
        assert command["status"] == 200 and command["state"]["mode"] == "read-only"
        # A read-only state has no connection_lost. The end of a session skips it.
        assert client.close() == 1000
        assert opened(server).request("command", {"action": "stop_sync"})["status"] == 200
    # Read-only commands carry no client ID.
    assert state.calls == [
        ("command", {"action": "start_sync"}),
        ("command", {"action": "stop_sync"}),
    ]


# Pushes


def test_service_pushes_the_state_right_after_hello(losing):
    server, state = losing
    state.message = "First."
    client = WsClient(server)
    sent = time.monotonic()
    first = client.hello()
    assert time.monotonic() - sent < 0.5
    assert first["message"] == "First."


def test_state_changes_are_pushed_without_the_keepalive(losing, monkeypatch):
    server, state = losing
    monkeypatch.setattr(SocketSession, "KEEPALIVE_SECONDS", 30.0)
    client = opened(server)
    # Only the version changes between snapshots. That is not a change to push.
    with pytest.raises(queue.Empty):
        client.events.get(timeout=0.3)
    state.message = "Changed."
    changed = time.monotonic()
    pushed = client.state(timeout=1)
    assert pushed["message"] == "Changed."
    assert time.monotonic() - changed < 0.5


def test_unchanged_state_is_pushed_at_the_keepalive_interval(losing, monkeypatch):
    server, _ = losing
    assert SocketSession.KEEPALIVE_SECONDS <= 1.0
    monkeypatch.setattr(SocketSession, "KEEPALIVE_SECONDS", 0.2)
    client = opened(server)
    times, versions = [], []
    for _ in range(4):
        versions.append(client.state(timeout=1)["version"])
        times.append(time.monotonic())
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    assert all(0.1 < gap < 0.8 for gap in gaps), gaps
    assert versions == sorted(versions)


def test_a_state_push_follows_each_reply(losing, monkeypatch):
    server, _ = losing
    monkeypatch.setattr(SocketSession, "KEEPALIVE_SECONDS", 30.0)
    client = opened(server)
    for _ in range(3):
        reply = client.request("heartbeat", {})
        event = client.next(timeout=0.5)
        assert event[0] == "message" and event[1]["type"] == "state", event
        assert event[1]["state"]["version"] > reply["state"]["version"]


class QueuedConnection:
    """Stand in for websocket.Connection in SocketSession: record sends, set the backlog."""

    def __init__(self):
        self.backlog = 0
        self.sent = []
        self.pings = 0

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def ping(self, payload=b""):
        self.pings += 1


@contextlib.contextmanager
def pushing(state, connection):
    errors = []
    server = SimpleNamespace(state=state, report_error=errors.append)
    session = SocketSession(server, None, connection)
    thread = threading.Thread(target=session._push, daemon=True)
    thread.start()
    try:
        yield session
    finally:
        session._done.set()
        thread.join(2)
    assert errors == []


def test_pushes_wait_while_frames_are_queued(monkeypatch):
    monkeypatch.setattr(SocketSession, "PUSH_SECONDS", 0.01)
    monkeypatch.setattr(SocketSession, "KEEPALIVE_SECONDS", 0.02)
    state, connection = RecordingState(), QueuedConnection()
    connection.backlog = 3
    with pushing(state, connection) as session:
        session._replied.set()
        state.message = "Changed while queued."
        # Many push ticks, keepalive intervals and a reply pass while frames wait.
        time.sleep(0.2)
        assert connection.sent == []
        state.message = "Latest."
        connection.backlog = 0
        wait_for(lambda: connection.sent)
    assert connection.sent[0]["state"]["message"] == "Latest."


class RacingReply(threading.Event):
    """A reply flag. Its armed is_set() returns False and lets a reply set it just after."""

    armed = False

    def is_set(self):
        result = super().is_set()
        if self.armed and not result:
            self.armed = False
            # The handler thread queues a reply and sets the flag right here.
            self.set()
        return result


def test_a_reply_that_lands_while_the_pusher_checks_still_gets_a_push(monkeypatch):
    monkeypatch.setattr(SocketSession, "PUSH_SECONDS", 0.01)
    monkeypatch.setattr(SocketSession, "KEEPALIVE_SECONDS", 30.0)
    state, connection = RecordingState(), QueuedConnection()
    with pushing(state, connection) as session:
        wait_for(lambda: len(connection.sent) == 1)
        session._replied = RacingReply()
        session._replied.armed = True
        wait_for(lambda: not session._replied.armed)
        wait_for(lambda: len(connection.sent) == 2, timeout=0.5)


def test_service_pushes_while_it_pings_on_its_interval(losing, monkeypatch):
    server, _ = losing
    assert SocketSession.PING_SECONDS == 5.0
    monkeypatch.setattr(SocketSession, "PING_SECONDS", 0.1)
    client = opened(server)
    assert client.wait(lambda e: e[0] == "ping", 1, "ping") == ("ping", b"")


# Ping, pong, close and loss


def test_client_ping_gets_a_pong_with_the_same_payload(losing):
    server, _ = losing
    client = WsClient(server)
    client.send(PING, b"before hello")
    assert client.next() == ("pong", b"before hello")
    client.hello()
    client.send(PING, b"after hello")
    assert client.wait(lambda e: e[0] == "pong") == ("pong", b"after hello")


def test_any_frame_keeps_the_connection_alive_and_silence_closes_it(losing, monkeypatch):
    server, state = losing
    assert SocketSession.IDLE_SECONDS == 15.0
    monkeypatch.setattr(SocketSession, "IDLE_SECONDS", 0.3)
    client = opened(server)
    started = time.monotonic()
    while time.monotonic() - started < 0.8:
        client.send(PONG, b"unsolicited")
        time.sleep(0.1)
    assert client.request("heartbeat", {})["status"] == 200
    assert state.losses() == 0
    silent = time.monotonic()
    code, _ = client.closed_by_service()
    assert code == 1000
    assert time.monotonic() - silent < 2
    assert state.losses() == 1


def test_client_close_is_echoed_and_revokes_keys(losing):
    server, state = losing
    client = opened(server)
    assert client.close(1000, "bye") == 1000
    wait_for(lambda: state.losses() == 1)
    assert state.calls == [("connection_lost", CLIENT)]


def test_close_without_a_code_gets_an_empty_echo(losing):
    server, state = losing
    client = opened(server)
    client.send(CLOSE)
    assert client.wait(lambda e: e[0] == "close") == ("close", None, "")
    client.ended()
    wait_for(lambda: state.losses() == 1)


def test_abrupt_reset_revokes_keys(losing):
    server, state = losing
    client = opened(server)
    client.reset()
    wait_for(lambda: state.losses() == 1, timeout=2)
    assert state.calls == [("connection_lost", CLIENT)]


# Each violation leaves no unread bytes, so the close frame cannot be lost to a reset.
VIOLATIONS = {
    "unmasked": (encode_frame(TEXT, b""), 1002),
    "reserved-bit": (bytes([0x80 | 0x40 | TEXT, 0x80]) + bytes(4), 1002),
    "unknown-opcode": (encode_frame(0x3, b"", mask=True), 1002),
    "continuation-without-start": (encode_frame(CONTINUATION, b"x", mask=True), 1002),
    "binary": (encode_frame(BINARY, b"\x00", mask=True), 1003),
    "invalid-utf8": (encode_frame(TEXT, b"\xff", mask=True), 1007),
    "too-big": (bytes([0x80 | TEXT, 0x80 | 126]) + struct.pack("!H", 4097), 1009),
    "bad-close-code": (encode_frame(CLOSE, struct.pack("!H", 1005), mask=True), 1002),
}


@pytest.mark.parametrize("violation", list(VIOLATIONS))
def test_protocol_violations_close_with_their_code_and_revoke_keys(losing, violation):
    server, state = losing
    data, expected = VIOLATIONS[violation]
    client = opened(server)
    client.send_raw(data)
    code, _ = client.closed_by_service()
    assert code == expected
    wait_for(lambda: state.losses() == 1)


@pytest.mark.parametrize("violation", ["binary", "invalid-utf8", "too-big"])
def test_protocol_violation_before_hello_touches_no_state(losing, violation):
    server, state = losing
    data, expected = VIOLATIONS[violation]
    client = WsClient(server)
    client.send_raw(data)
    assert client.closed_by_service()[0] == expected
    assert state.calls == []


# Registry


def track_unregistered(server):
    ended = []
    unregister = server.unregister_socket

    def recorded(session):
        unregister(session)
        ended.append(session.client)

    server.unregister_socket = recorded
    return ended


REPLACED = (4409, "Replaced by a newer connection")


def test_newer_connection_of_the_same_client_replaces_the_older_one(losing):
    server, state = losing
    ended = track_unregistered(server)
    older = opened(server)
    newer = opened(server)
    # The registry revoked the keys once, under its lock, before the newer session took over.
    assert state.calls == [("connection_lost", CLIENT)]
    # A request on the replaced connection is stale. It must not reach the state.
    older.send_request("keys", {"seq": 9, "held": ["W"]})
    assert older.closed_by_service() == REPLACED
    assert older.replies == {}
    # The older session's own end must revoke nothing.
    wait_for(lambda: ended == [CLIENT])
    assert state.calls == [("connection_lost", CLIENT)]
    assert newer.request("keys", {"seq": 1, "held": []})["status"] == 200
    assert newer.close() == 1000
    wait_for(lambda: state.losses() == 2)
    assert state.calls == [
        ("connection_lost", CLIENT),
        ("keys", {"seq": 1, "held": []}, CLIENT),
        ("connection_lost", CLIENT),
    ]


def test_replaced_connection_that_ignores_the_close_is_aborted(losing, monkeypatch):
    server, state = losing
    monkeypatch.setattr(SocketSession, "DRAIN_SECONDS", 0.3)
    older = opened(server)
    opened(server)
    close = older.wait(lambda e: e[0] == "close", what="close")
    replaced = time.monotonic()
    assert close[1:] == REPLACED
    # The older peer never answers the close. The service aborts it after DRAIN_SECONDS.
    assert older.ended() - replaced < 1.5
    assert state.calls == [("connection_lost", CLIENT)]


def test_connections_of_different_clients_are_independent(losing):
    server, state = losing
    first = opened(server, client="browser-a")
    second = opened(server, client="browser-b")
    assert first.close() == 1000
    wait_for(lambda: state.losses("browser-a") == 1)
    assert state.losses("browser-b") == 0
    assert second.request("heartbeat", {})["status"] == 200
    assert second.state(timeout=2)["mode"] == "robot"
    assert state.calls == [("connection_lost", "browser-a"), ("heartbeat", "browser-b")]


def test_unauthenticated_connections_beyond_the_pending_limit_evict_the_oldest(losing, monkeypatch):
    server, state = losing
    assert PanelServer.MAX_PENDING_SOCKETS == 8
    # Keep the hello deadline out of this scenario.
    monkeypatch.setattr(SocketSession, "HELLO_SECONDS", 30.0)
    clients = []
    for _ in range(9):
        client = WsClient(server)
        # The pong comes from the session, so the service has admitted this socket.
        client.send(PING, b"admitted")
        assert client.next() == ("pong", b"admitted")
        clients.append(client)
    oldest, *others = clients
    # Eviction aborts the oldest socket without a close frame.
    event = oldest.next(timeout=2)
    assert event[0] == "end", event
    for client in others:
        client.send(PING, b"alive")
        assert client.next() == ("pong", b"alive")
    assert state.calls == []
    # An evicted slot does not block authentication of the others.
    assert others[0].hello()["mode"] == "robot"


def test_ninth_distinct_client_gets_503_and_close_1013(losing):
    server, state = losing
    assert PanelServer.MAX_SOCKETS == 8
    clients = {f"client-{index}": opened(server, client=f"client-{index}") for index in range(8)}
    refused = WsClient(server)
    refused.send_json(hello_message("client-8"))
    error, code = refused.refused()
    assert error == {"type": "error", "status": 503, "error": "Too many open panel connections"}
    assert code == 1013
    assert state.calls == []
    # A client that already has a session may replace it while every slot is taken.
    replacement = opened(server, client="client-0")
    assert clients["client-0"].closed_by_service() == REPLACED
    assert state.calls == [("connection_lost", "client-0")]
    assert replacement.request("heartbeat", {})["status"] == 200
    # A freed slot admits a new client.
    assert clients["client-1"].close() == 1000
    wait_for(lambda: state.losses("client-1") == 1)
    assert opened(server, client="client-8").request("heartbeat", {})["status"] == 200


def test_stop_sockets_sends_a_final_state_and_close_1001(losing):
    server, state = losing
    first = opened(server, client="browser-a")
    second = opened(server, client="browser-b")
    state.message = "The panel is closing."
    server.stop_sockets()
    for client in (first, second):
        final = client.wait(
            lambda e: (
                e[0] == "close"
                or (e[0] == "message" and e[1]["state"]["message"] == "The panel is closing.")
            ),
            what="final state",
        )
        assert final[0] == "message"
        assert client.closed_by_service() == (1001, "The robot service stopped")
    wait_for(lambda: state.losses("browser-a") == state.losses("browser-b") == 1)


# Interoperability


def test_websockets_library_client_interoperates(losing):
    pytest.importorskip("websockets")
    from websockets.sync.client import connect as ws_connect

    server, state = losing
    url = f"ws://127.0.0.1:{server.server_port}/api/ws"
    with ws_connect(url, proxy=None, open_timeout=2, close_timeout=2) as connection:
        connection.send(json.dumps(hello_message()))
        assert json.loads(connection.recv(timeout=2))["type"] == "state"
        # A fragmented request, as the library sends an iterable.
        connection.send(['{"type": "command", "id": 1, ', '"body": {"action": "stop"}}'])
        while (message := json.loads(connection.recv(timeout=2)))["type"] != "reply":
            assert message["type"] == "state"
        assert message["id"] == 1 and message["status"] == 200
        assert connection.ping(b"interop").wait(2)
    assert connection.close_code == 1000
    wait_for(lambda: state.losses() == 1)
    assert state.calls == [("command", {"action": "stop"}, CLIENT), ("connection_lost", CLIENT)]


def test_websockets_library_client_sees_close_4401_for_a_wrong_token(losing):
    websockets = pytest.importorskip("websockets")
    from websockets.sync.client import connect as ws_connect

    server, state = losing
    url = f"ws://127.0.0.1:{server.server_port}/api/ws"
    with ws_connect(url, proxy=None, open_timeout=2, close_timeout=2) as connection:
        connection.send(json.dumps(hello_message(token="wrong")))
        error = json.loads(connection.recv(timeout=2))
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            connection.recv(timeout=2)
    assert error == {"type": "error", "status": 401, "error": "Enter the panel access token"}
    assert connection.close_code == 4401
    assert state.calls == []
