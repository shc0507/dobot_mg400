"""Check the independent browser panel without robot connections."""

import errno
import http.client
import json
import logging
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from panel_ws import PANEL, PanelServer, running

from mg400_agent.controller_ownership import ControllerOwnership

ReadOnlyPanelState = PANEL["ReadOnlyPanelState"]
PANEL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
ROBOT_HEADERS = {
    "Authorization": "Bearer test-token",
    "Content-Type": "application/json",
    "X-MG400-Panel": "1",
    "X-MG400-Client": "browser-a",
}


pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


def test_read_only_mode_is_inert_until_sync_and_has_no_hardware_commands():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise OSError("test offline")

    panel = ReadOnlyPanelState("test-host", connection_factory=forbidden)
    try:
        assert panel.snapshot()["actual"] == [None] * 5
        assert calls == []
        for action in (
            "enable",
            "disable",
            "home",
            "execute",
            "stop",
            "initialize_gripper",
            "clear_alarm",
            "keyboard",
        ):
            command = {"action": action}
            if action == "execute":
                command["targets"] = {"x": 250, "y": 0, "z": 100, "r": 0, "grip": 500}
            if action == "keyboard":
                command.update(enabled=True, speed=5, seq=2)
            with pytest.raises(PermissionError):
                panel.command(command)
        # A failed connection releases the robot, so sync can start again.
        for _ in range(2):
            panel.command({"action": "start_sync"})
            panel._reader.join(timeout=1)
            assert not panel.snapshot()["syncing"]
            assert "test offline" in panel.snapshot()["message"]
        assert calls == [("test-host",), ("test-host",)]
        contender = ControllerOwnership("test-host")
        try:
            contender.acquire()
        finally:
            contender.release()
    finally:
        panel.close()


def test_passive_feedback_cannot_connect_while_another_panel_owns_robot():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Feedback connection must not start")

    owner = ControllerOwnership("test-host")
    owner.acquire()
    panel = ReadOnlyPanelState("test-host", connection_factory=forbidden)
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            panel.command({"action": "start_sync"})
        assert not panel.snapshot()["syncing"]
        assert calls == []
    finally:
        panel.close()
        owner.release()


@pytest.mark.parametrize("finish", ["stop_sync", "close", "read_failure"])
def test_passive_feedback_releases_ownership_only_after_connection_closes(finish):
    reading, release_read = threading.Event(), threading.Event()
    exiting, release_exit = threading.Event(), threading.Event()

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            exiting.set()
            assert release_exit.wait(3)

        def read_latest(self):
            reading.set()
            assert release_read.wait(3)
            if finish == "read_failure":
                raise OSError("Feedback disconnected")
            return SimpleNamespace(joints_deg=(1, 2, 3, 4), mode=4, error_status=False)

    panel = ReadOnlyPanelState("test-host", connection_factory=Connection)
    contender = ControllerOwnership("test-host")
    closing = None
    try:
        panel.command({"action": "start_sync"})
        assert reading.wait(1)
        if finish == "close":
            closing = threading.Thread(target=panel.close)
            closing.start()
            assert panel._stop.wait(1)
        elif finish == "stop_sync":
            panel.command({"action": "stop_sync"})
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        release_read.set()
        assert exiting.wait(1)
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        release_exit.set()
        panel._reader.join(1)
        assert not panel._reader.is_alive()
        contender.acquire()
        assert not panel.snapshot()["syncing"]
        if finish == "read_failure":
            assert "Feedback disconnected" in panel.snapshot()["message"]
    finally:
        release_read.set()
        release_exit.set()
        panel.close()
        if closing is not None:
            closing.join(1)
        contender.release()


def test_passive_feedback_thread_failure_releases_ownership(monkeypatch):
    connection = Mock(side_effect=AssertionError("Unexpected connection"))
    panel = ReadOnlyPanelState("test-host", connection_factory=connection)

    def fail(thread):
        raise RuntimeError("Thread creation failed")

    monkeypatch.setattr(threading.Thread, "start", fail)
    try:
        with pytest.raises(RuntimeError, match="Thread creation failed"):
            panel.command({"action": "start_sync"})
        assert not panel.snapshot()["syncing"]
        connection.assert_not_called()
        contender = ControllerOwnership("test-host")
        try:
            contender.acquire()
        finally:
            contender.release()
    finally:
        panel.close()


def test_passive_feedback_stops_without_accepting_a_late_sample():
    waiting = threading.Event()
    release = threading.Event()

    class Connection:
        count = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read_latest(self):
            self.count += 1
            if self.count > 1:
                waiting.set()
                release.wait(timeout=2)
            return SimpleNamespace(joints_deg=(self.count, 2, 3, 4), mode=4, error_status=False)

    panel = ReadOnlyPanelState("test-host", connection_factory=Connection)
    try:
        panel.command({"action": "start_sync"})
        assert waiting.wait(timeout=1)
        assert panel.snapshot()["joints"] == [1, 2, 3, 4]
        panel.command({"action": "stop_sync"})
        assert not panel.snapshot()["feedback_fresh"]
        release.set()
        panel._reader.join(timeout=1)
        assert panel.snapshot()["joints"] == [1, 2, 3, 4]
        assert panel.snapshot()["actual"] == [None] * 5
    finally:
        release.set()
        panel.close()


@pytest.fixture
def server():
    state = ReadOnlyPanelState(
        "unused.invalid",
        connection_factory=Mock(side_effect=AssertionError("Unexpected connection")),
    )
    instance = PanelServer(("127.0.0.1", 0), state, token="test-token")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    instance.state.close()
    thread.join(timeout=2)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


def test_http_authentication_and_command_guard(server):
    assert request(server, "GET", "/")[0] == 200
    assert request(server, "GET", "/api/state")[0] == 401
    auth = {"Authorization": "Bearer test-token"}
    assert request(server, "GET", "/api/state", headers=auth)[0] == 200
    body = json.dumps({"action": "stop_sync"})
    assert request(server, "POST", "/api/command", body, auth)[0] == 403
    headers = {**auth, "Content-Type": "application/json", "X-MG400-Panel": "1"}
    assert request(server, "POST", "/api/command", body, headers)[0] == 200
    assert request(server, "POST", "/api/command", '{"action":"enable"}', headers)[0] == 403
    for invalid in ([], {}, None, True):
        assert (
            request(server, "POST", "/api/command", json.dumps({"action": invalid}), headers)[0]
            == 400
        )
    assert request(server, "GET", "/../config/robot.toml")[0] == 404


@pytest.mark.parametrize(
    ("token", "valid"),
    [
        ("a" * 30, True),
        ("lab panel key for the mg400 arm", True),
        ("!~" * 15, True),
        ("é" * 30, False),
        ("a" * 30 + " ", False),
        (" " + "a" * 30, False),
        ("a" * 15 + "\t" + "a" * 15, False),
        ("a" * 30 + "\n", False),
    ],
    ids=["plain", "inner-spaces", "symbols", "non-ascii", "trailing", "leading", "tab", "newline"],
)
def test_tokens_are_those_a_browser_sends_unchanged(token, valid):
    assert bool(PANEL["valid_token"](token)) is valid


@pytest.mark.parametrize(
    ("argv", "token", "message"),
    [
        (["--bind", "0.0.0.0"], None, None),
        # A browser cannot send other characters, and it strips spaces at either end.
        (["--bind", "127.0.0.1"], "a" * 30 + " ", None),
        (["--bind", "0.0.0.0"], "é" * 30, None),
        (["--mode", "read-only", "--gripper-feedback", "unused.json"], None, None),
        (["--mode", "demo"], None, "invalid choice: 'demo'"),
    ],
    ids=[
        "remote-without-token",
        "loopback-unsendable-token",
        "remote-unsendable-token",
        "gripper-feedback-read-only",
        "demo",
    ],
)
def test_main_refuses_bad_arguments_before_constructing_a_backend(
    monkeypatch, capsys, argv, token, message
):
    main = PANEL["main"]
    monkeypatch.delenv("MG400_PANEL_BIND", raising=False)
    monkeypatch.delenv("MG400_PANEL_PORT", raising=False)
    if token is None:
        monkeypatch.delenv("MG400_PANEL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MG400_PANEL_TOKEN", token)
    backends = {name: Mock() for name in ("RobotPanelState", "ReadOnlyPanelState", "PanelServer")}
    for name, backend in backends.items():
        monkeypatch.setitem(main.__globals__, name, backend)

    with pytest.raises(SystemExit) as error:
        main(argv)

    assert error.value.code == 2
    if message is not None:
        assert message in capsys.readouterr().err
    for backend in backends.values():
        backend.assert_not_called()


@pytest.mark.parametrize(
    ("environment", "argv", "address"),
    [
        ({}, [], ("127.0.0.1", 8766)),
        ({"MG400_PANEL_BIND": "", "MG400_PANEL_PORT": ""}, [], ("127.0.0.1", 8766)),
        ({"MG400_PANEL_BIND": "localhost", "MG400_PANEL_PORT": "9000"}, [], ("localhost", 9000)),
        (
            {"MG400_PANEL_BIND": "0.0.0.0", "MG400_PANEL_PORT": "9000"},
            ["--bind", "::1", "--port", "9001"],
            ("::1", 9001),
        ),
    ],
    ids=["defaults", "empty-means-unset", "environment", "options-win"],
)
def test_listen_address_comes_from_options_then_environment(
    monkeypatch, signal_table, environment, argv, address
):
    main = PANEL["main"]
    for name in ("MG400_PANEL_BIND", "MG400_PANEL_PORT", "MG400_PANEL_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    server = Mock(server_port=address[1])
    panel_server = Mock(return_value=server)
    monkeypatch.setitem(main.__globals__, "RobotPanelState", Mock(return_value=closing_state()))
    monkeypatch.setitem(main.__globals__, "PanelServer", panel_server)
    monkeypatch.setitem(main.__globals__, "load_robot_host", Mock(return_value="robot.test"))

    assert main(argv) == 0
    assert panel_server.call_args.args[0] == address


@pytest.mark.parametrize("value", ["0", "65536", "80a", " 80", "-1"])
def test_invalid_port_environment_is_refused_before_a_backend(monkeypatch, capsys, value):
    main = PANEL["main"]
    monkeypatch.delenv("MG400_PANEL_BIND", raising=False)
    monkeypatch.setenv("MG400_PANEL_PORT", value)
    backends = {name: Mock() for name in ("RobotPanelState", "ReadOnlyPanelState", "PanelServer")}
    for name, backend in backends.items():
        monkeypatch.setitem(main.__globals__, name, backend)

    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 2
    assert "MG400_PANEL_PORT must be a whole number" in capsys.readouterr().err
    for backend in backends.values():
        backend.assert_not_called()


def test_non_ascii_authorization_header_gets_401():
    state = RecordingState()
    # The server decodes header bytes as Latin-1, so a UTF-8 é arrives as non-ASCII text.
    raw = http11("GET", "/api/state", b"", ["Authorization: Bearer tést-token"])
    with running(state) as server:
        code, headers, _ = exchange(server, raw)
    assert code == 401
    assert headers.get("connection", "").lower() == "close"


def test_robot_http_starts_without_connecting_and_rejects_motion_before_ownership(server):
    robot = PANEL["RobotPanelState"]("unused.invalid")
    original, server.state = server.state, robot
    headers = ROBOT_HEADERS
    try:
        status, body = request(server, "GET", "/api/state", headers=headers)
        data = json.loads(body)
        assert status == 200 and data["mode"] == "robot"
        assert not data["syncing"] and data["actual"] == [None] * 5
        assert request(server, "POST", "/api/heartbeat", "{}", headers)[0] == 200
        assert request(server, "POST", "/api/command", '{"action":"enable"}', headers)[0] == 403
        assert robot.snapshot()["controller_id"] is None
    finally:
        server.state = original
        robot.close()


class RecordingState:
    """Record what the HTTP handler passes to a robot-mode panel state."""

    mode = "robot"

    def __init__(self):
        self.calls = []
        self.failure = None

    def snapshot(self):
        return {"mode": "robot", "busy": False}

    def heartbeat(self, client_id):
        self.calls.append(("heartbeat", client_id))
        if self.failure is not None:
            raise self.failure
        return {"mode": "robot", "controller_id": client_id}

    def command(self, payload, client_id=None):
        self.calls.append(("command", payload, client_id))
        return self.snapshot()

    def keys(self, payload, client_id):
        self.calls.append(("keys", payload, client_id))
        return self.snapshot()

    def close(self):
        pass


@pytest.mark.parametrize("base_path", ["/", "/mg400-panel/"])
def test_key_updates_have_no_http_endpoint(base_path):
    state = RecordingState()
    with running(state, base_path=base_path) as server:
        body = json.dumps({"seq": 7, "held": ["W", "Z"]})
        path = f"{server.base_path}api/keys"
        assert request(server, "POST", path, body, ROBOT_HEADERS)[0] == 404
    # Key updates travel only over the WebSocket.
    assert state.calls == []


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"Authorization": None}, 401),
        ({"Authorization": "Bearer wrong-token"}, 401),
        ({"Content-Type": None}, 403),
        ({"Content-Type": "text/plain"}, 403),
        ({"X-MG400-Panel": None}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"X-MG400-Client": None}, 400),
        ({"X-MG400-Client": "x" * 129}, 400),
    ],
    ids=[
        "no-token",
        "wrong-token",
        "no-content-type",
        "text-content-type",
        "no-panel-header",
        "cross-site",
        "no-client-id",
        "long-client-id",
    ],
)
def test_robot_posts_require_the_panel_request_guard(change, status):
    state = RecordingState()
    headers = {
        name: value for name, value in {**ROBOT_HEADERS, **change}.items() if value is not None
    }
    with running(state) as server:
        assert request(server, "POST", "/api/heartbeat", "{}", headers)[0] == status
    assert state.calls == []


def test_api_heartbeat_routes_under_the_base_path():
    state = RecordingState()
    # The --base-path value without a trailing slash, as an operator types it.
    with running(state, base_path="/mg400-panel") as server:
        auth = {"Authorization": "Bearer test-token"}
        assert request(server, "GET", "/mg400-panel")[0] == 308
        assert request(server, "GET", "/mg400-panel/")[0] == 200
        assert request(server, "GET", "/mg400-panel/api/state", headers=auth)[0] == 200
        assert request(server, "GET", "/api/state", headers=auth)[0] == 404
        assert request(server, "POST", "/mg400-panel/api/heartbeat", "{}", ROBOT_HEADERS)[0] == 200
        assert request(server, "POST", "/api/heartbeat", "{}", ROBOT_HEADERS)[0] == 404
        assert request(server, "POST", "/mg400-panelapi/heartbeat", "{}", ROBOT_HEADERS)[0] == 404
    assert state.calls == [("heartbeat", "browser-a")]


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (ValueError("A browser client ID is required"), 400),
        (PermissionError("Start sync in this browser before issuing commands"), 403),
        (RuntimeError("The panel is closing"), 409),
    ],
    ids=["invalid", "not-owner", "conflict"],
)
def test_robot_posts_report_state_errors_with_their_http_status(failure, status):
    state = RecordingState()
    state.failure = failure
    with running(state) as server:
        code, data = request(server, "POST", "/api/heartbeat", "{}", ROBOT_HEADERS)
    assert code == status
    assert json.loads(data) == {"error": str(failure)}


@pytest.mark.parametrize(
    "body",
    ["{", "", "{" + " " * 4096 + "}", '{"action":"enable"}'],
    ids=["bad-json", "empty", "too-large", "heartbeat-with-body"],
)
def test_robot_posts_reject_malformed_bodies_before_the_state(body):
    state = RecordingState()
    with running(state) as server:
        assert request(server, "POST", "/api/heartbeat", body, ROBOT_HEADERS)[0] == 400
    assert state.calls == []


@pytest.mark.parametrize("base_path", ["/", "/mg400-panel/"])
def test_successful_heartbeats_stay_out_of_the_request_log(capsys, base_path):
    state = RecordingState()
    with running(state, base_path=base_path) as server:
        heartbeat = f"{server.base_path}api/heartbeat"
        for _ in range(3):
            assert request(server, "POST", heartbeat, "{}", ROBOT_HEADERS)[0] == 200
        command = json.dumps({"action": "stop"})
        assert (
            request(server, "POST", f"{server.base_path}api/command", command, ROBOT_HEADERS)[0]
            == 200
        )
        state.failure = RuntimeError("Another robot operation is in progress")
        assert request(server, "POST", heartbeat, "{}", ROBOT_HEADERS)[0] == 409
        wrong = {**ROBOT_HEADERS, "Authorization": "Bearer wrong-token"}
        assert request(server, "POST", heartbeat, "{}", wrong)[0] == 401
    log = capsys.readouterr().err
    assert f'"POST {heartbeat} HTTP/1.1" 200' not in log
    assert f'"POST {heartbeat} HTTP/1.1" 409' in log
    assert f'"POST {heartbeat} HTTP/1.1" 401' in log
    # Only heartbeats are quiet. Robot commands stay in the request log.
    assert f'"POST {server.base_path}api/command HTTP/1.1" 200' in log


def test_read_only_mode_refuses_keyboard_control(server):
    headers = dict(ROBOT_HEADERS)
    for payload in (
        {"action": "keyboard", "enabled": True, "speed": 5, "seq": 2},
        {"action": "keyboard", "enabled": False, "reason": "user", "seq": 3},
    ):
        status, body = request(server, "POST", "/api/command", json.dumps(payload), headers)
        assert status == 403 and json.loads(body)["error"]
    # Read-only pages do not send a client ID; the refusal must not depend on it.
    del headers["X-MG400-Client"]
    payload = {"action": "keyboard", "enabled": True, "speed": 5, "seq": 4}
    assert request(server, "POST", "/api/command", json.dumps(payload), headers)[0] == 403
    assert not server.state.snapshot()["syncing"]


def test_successful_responses_keep_the_connection_open():
    state = RecordingState()
    with running(state) as server:
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            sockets = []
            for _ in (1, 2):
                connection.request("POST", "/api/heartbeat", "{}", ROBOT_HEADERS)
                response = connection.getresponse()
                assert json.loads(response.read()) == {
                    "mode": "robot",
                    "controller_id": "browser-a",
                }
                assert response.status == 200
                assert response.version == 11
                assert not response.will_close
                sockets.append(connection.sock)
        finally:
            connection.close()
    # The second heartbeat reused the first TCP connection.
    assert sockets[0] is not None and sockets[1] is sockets[0]
    assert state.calls == [("heartbeat", "browser-a")] * 2


def http11(method, path, body=b"", headers=()):
    lines = [f"{method} {path} HTTP/1.1", "Host: panel.test", *headers]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def exchange(server, raw):
    """Send raw bytes and read until the server closes the connection."""
    received = b""
    with socket.create_connection(server.server_address, timeout=2) as client:
        client.sendall(raw)
        try:
            while chunk := client.recv(65536):
                received += chunk
        except ConnectionResetError:
            pass
        except TimeoutError:
            pytest.fail(f"The server kept the connection open after {received[:40]!r}")
    head, _, body = received.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(status_line.split()[1]), headers, body


EMPTY_BODY = b"{}"
AUTH = "Authorization: Bearer test-token"
JSON_TYPE = "Content-Type: application/json"
PANEL_HEADER = "X-MG400-Panel: 1"
CLIENT = "X-MG400-Client: browser-a"


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (http11("POST", "/api/heartbeat", EMPTY_BODY, [JSON_TYPE, PANEL_HEADER, CLIENT]), 401),
        (http11("POST", "/api/heartbeat", EMPTY_BODY, [AUTH, JSON_TYPE, CLIENT]), 403),
        (http11("POST", "/api/other", EMPTY_BODY, [AUTH, JSON_TYPE, PANEL_HEADER, CLIENT]), 404),
        (http11("POST", "/api/heartbeat", b"{", [AUTH, JSON_TYPE, PANEL_HEADER, CLIENT]), 400),
        (
            http11("POST", "/api/heartbeat", EMPTY_BODY, [AUTH, JSON_TYPE, PANEL_HEADER, CLIENT]),
            409,
        ),
        (http11("GET", "/missing"), 404),
    ],
    ids=["401-unread-body", "403-unread-body", "404-unread-body", "400", "409", "404-get"],
)
def test_error_responses_close_the_connection(raw, status):
    state = RecordingState()
    state.failure = RuntimeError("Another robot operation is in progress")
    with running(state) as server:
        code, headers, body = exchange(server, raw)
    assert code == status
    assert headers.get("connection", "").lower() == "close"
    # Nothing follows the error: an unread request body is never parsed as a request.
    assert len(body) == int(headers["content-length"])


def test_handler_speaks_http11_with_the_specified_idle_timeout():
    handler = PANEL["PanelHandler"]
    assert handler.protocol_version == "HTTP/1.1"
    assert handler.timeout == 15
    # Small keep-alive responses must not wait for a delayed acknowledgement.
    assert handler.disable_nagle_algorithm is True


SMUGGLED = http11("POST", "/api/heartbeat", EMPTY_BODY, [AUTH, JSON_TYPE, PANEL_HEADER, CLIENT])


UPGRADE = [
    "Upgrade: websocket",
    "Connection: Upgrade",
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version: 13",
]


@pytest.mark.parametrize(
    ("base_path", "path", "headers"),
    [
        ("/", "/", [AUTH]),
        ("/", "/api/state", [AUTH]),
        ("/mg400-panel/", "/mg400-panel", [AUTH]),
        # An accepted body would be read as WebSocket frames.
        ("/", "/api/ws", [AUTH, *UPGRADE]),
    ],
    ids=["page", "state", "redirect", "websocket"],
)
def test_get_with_a_body_is_refused_and_cannot_smuggle_a_request(base_path, path, headers):
    state = RecordingState()
    raw = http11("GET", path, SMUGGLED, headers)
    with running(state, base_path=base_path) as server:
        code, headers, _ = exchange(server, raw)
    assert code == 400
    assert headers.get("connection", "").lower() == "close"
    assert state.calls == []


def test_post_with_transfer_encoding_is_refused_before_the_state_sees_it():
    state = RecordingState()
    # A valid JSON prefix covered by Content-Length, then a second request in the same body.
    framing = [f"Content-Length: {len(EMPTY_BODY)}", "Transfer-Encoding: chunked"]
    raw = http11("POST", "/api/heartbeat", b"", [AUTH, JSON_TYPE, PANEL_HEADER, CLIENT, *framing])
    with running(state) as server:
        code, headers, _ = exchange(server, raw + EMPTY_BODY + SMUGGLED)
    assert code == 400
    assert headers.get("connection", "").lower() == "close"
    assert state.calls == []


@pytest.fixture
def signal_table(monkeypatch):
    """Replace the process handlers of the panel signals with a dict."""
    real_signal, real_getsignal = signal.signal, signal.getsignal
    table = {signum: object() for signum in PANEL_SIGNALS}

    def set_handler(signum, handler):
        if signum not in table:
            return real_signal(signum, handler)
        previous, table[signum] = table[signum], handler
        return previous

    def get_handler(signum):
        return table[signum] if signum in table else real_getsignal(signum)

    monkeypatch.setattr(signal, "signal", set_handler)
    monkeypatch.setattr(signal, "getsignal", get_handler)
    return table


def closing_state(snapshot=None, close=None):
    state = Mock()
    state.snapshot.return_value = {
        "message": "Disconnected.",
        "stop_unconfirmed": False,
        "busy": False,
        **(snapshot or {}),
    }
    if close is not None:
        state.close.side_effect = close
    return state


def run_panel(monkeypatch, state, serve_forever=None, server_close_error=None):
    main = PANEL["main"]
    server = Mock(server_port=8766)
    if serve_forever is not None:
        server.serve_forever.side_effect = serve_forever
    if server_close_error is not None:
        server.server_close.side_effect = server_close_error
    monkeypatch.setitem(main.__globals__, "RobotPanelState", Mock(return_value=state))
    monkeypatch.setitem(main.__globals__, "PanelServer", Mock(return_value=server))
    monkeypatch.setitem(main.__globals__, "load_robot_host", Mock(return_value="robot.test"))
    return main([]), server


def test_cli_defaults_to_robot_without_starting_sync(monkeypatch, capsys, signal_table):
    original = dict(signal_table)
    before_close = []
    state = closing_state(close=lambda: before_close.append(capsys.readouterr().err))

    code, _ = run_panel(monkeypatch, state)

    backends = PANEL["main"].__globals__
    assert code == 0
    backends["load_robot_host"].assert_called_once_with(PANEL["ROOT"] / "config/robot.toml")
    backends["RobotPanelState"].assert_called_once_with("robot.test", gripper=None)
    state.command.assert_not_called()
    state.close.assert_called_once_with()
    assert len(before_close) == 1
    assert "Stopping robot control before exit." in before_close[0]
    # main() must not leak its signal handlers into the calling process.
    assert signal_table == original


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_termination_signals_stop_the_robot_before_exit(monkeypatch, signal_table, signum):
    original = dict(signal_table)
    ignored = dict.fromkeys(PANEL_SIGNALS, signal.SIG_IGN)
    while_serving, after_signal, during_close = {}, {}, []

    def serve_forever():
        while_serving.update(signal_table)
        try:
            signal_table[signum](signum, None)
        except KeyboardInterrupt:
            after_signal.update(signal_table)
            raise
        raise AssertionError("The panel signal handler must interrupt serve_forever")

    state = closing_state(close=lambda: during_close.append(dict(signal_table)))
    code, server = run_panel(monkeypatch, state, serve_forever)

    assert code == 0
    for installed in (signal.SIGTERM, signal.SIGHUP):
        assert callable(while_serving[installed])
        assert while_serving[installed] is not original[installed]
    # The first signal ignores repeats at once, so they cannot abort the stop.
    assert after_signal == ignored
    state.close.assert_called_once_with()
    assert during_close == [ignored]
    server.server_close.assert_called_once_with()
    assert signal_table == original


def test_an_ignored_sighup_stays_ignored_while_serving(monkeypatch, signal_table):
    signal_table[signal.SIGHUP] = signal.SIG_IGN
    original = dict(signal_table)
    while_serving = {}
    state = closing_state()

    code, _ = run_panel(monkeypatch, state, lambda: while_serving.update(signal_table))

    assert code == 0
    assert while_serving[signal.SIGHUP] == signal.SIG_IGN
    assert callable(while_serving[signal.SIGTERM])
    state.close.assert_called_once_with()
    assert signal_table == original


@pytest.mark.parametrize("flag", ["stop_unconfirmed", "busy"])
def test_unconfirmed_rest_after_close_exits_non_zero(monkeypatch, capsys, signal_table, flag):
    original = dict(signal_table)
    message = "Robot stop or disable is unconfirmed: test transport failed."
    state = closing_state({"message": message, flag: True})

    code, _ = run_panel(monkeypatch, state)

    output = capsys.readouterr()
    assert code == 1
    assert "Robot rest is unconfirmed. Use the physical emergency stop." in output.err + output.out
    assert message in output.err + output.out
    state.close.assert_called_once_with()
    assert signal_table == original


@pytest.mark.parametrize(
    ("state_error", "server_error", "message"),
    [
        (RuntimeError("Stop transport failed"), None, "Stop transport failed"),
        # The state closes even if the HTTP server cannot.
        (None, OSError("Socket failure"), "Socket failure"),
    ],
    ids=["state-close", "server-close"],
)
def test_cleanup_failure_exits_non_zero_and_restores_handlers(
    monkeypatch, capsys, signal_table, state_error, server_error, message
):
    original = dict(signal_table)
    during_close = []

    def close():
        during_close.append(dict(signal_table))
        if state_error is not None:
            raise state_error

    state = closing_state(close=close)
    code, _ = run_panel(monkeypatch, state, server_close_error=server_error)

    output = capsys.readouterr()
    assert code == 1
    assert f"Cleanup failed: {message}" in output.err
    assert "Use the physical emergency stop." in output.err
    state.close.assert_called_once_with()
    assert during_close == [dict.fromkeys(PANEL_SIGNALS, signal.SIG_IGN)]
    assert signal_table == original


class BrokenStderr:
    """A hung-up terminal or a pipe without a reader."""

    def write(self, text):
        raise OSError(errno.EIO, "Input/output error")

    def flush(self):
        raise OSError(errno.EIO, "Input/output error")


@pytest.mark.parametrize("signum", PANEL_SIGNALS, ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_broken_stderr_cannot_skip_the_stop(monkeypatch, signal_table, signum):
    original = dict(signal_table)

    def serve_forever():
        monkeypatch.setattr(sys, "stderr", BrokenStderr())
        signal_table[signum](signum, None)

    state = closing_state()
    code, server = run_panel(monkeypatch, state, serve_forever)

    assert code == 0
    server.server_close.assert_called_once_with()
    state.close.assert_called_once_with()
    assert signal_table == original


def test_a_blocked_stderr_delays_the_stop_only_briefly(monkeypatch, signal_table):
    unblock, interrupted, closed = threading.Event(), [], []

    class BlockedStderr:
        def write(self, text):
            unblock.wait(5)
            return len(text)

        def flush(self):
            pass

    def serve_forever():
        monkeypatch.setattr(sys, "stderr", BlockedStderr())
        interrupted.append(time.monotonic())
        signal_table[signal.SIGTERM](signal.SIGTERM, None)

    def close():
        closed.append(time.monotonic())
        unblock.set()

    try:
        code, _ = run_panel(monkeypatch, closing_state(close=close), serve_forever)
    finally:
        unblock.set()
    assert code == 0
    assert closed and closed[0] - interrupted[0] < 1.0


def test_panel_events_reach_stderr_and_the_handler_is_removed(monkeypatch, capsys, signal_table):
    panel_log = logging.getLogger("mg400_agent.web_control")
    handlers = list(panel_log.handlers)

    def serve_forever():
        panel_log.info("Control locked.")
        raise KeyboardInterrupt

    code, _ = run_panel(monkeypatch, closing_state(), serve_forever)

    assert code == 0
    assert "INFO Control locked." in capsys.readouterr().err
    assert panel_log.handlers == handlers


def test_a_blocked_event_log_cannot_delay_the_stop(monkeypatch, signal_table):
    unblock, interrupted, closed = threading.Event(), [], []

    class BlockedStderr:
        def write(self, text):
            unblock.wait(5)
            return len(text)

        def flush(self):
            pass

    def serve_forever():
        # The time counts from the first event, so a blocking write would fail the check.
        interrupted.append(time.monotonic())
        for _ in range(3):
            logging.getLogger("mg400_agent.web_control").info("Control locked.")
        signal_table[signal.SIGTERM](signal.SIGTERM, None)

    def close():
        closed.append(time.monotonic())

    monkeypatch.setattr(sys, "stderr", BlockedStderr())
    try:
        code, _ = run_panel(monkeypatch, closing_state(close=close), serve_forever)
    finally:
        unblock.set()
    assert code == 0
    assert closed and closed[0] - interrupted[0] < 1.0


def test_read_only_exit_reads_a_snapshot_without_robot_fields(monkeypatch, capsys, signal_table):
    main = PANEL["main"]
    monkeypatch.setitem(main.__globals__, "PanelServer", Mock(return_value=Mock(server_port=1)))

    assert main(["--mode", "read-only", "--robot-host", "robot.test"]) == 0

    output = capsys.readouterr()
    assert "Cleanup failed" not in output.err + output.out
    assert "physical emergency stop" not in output.err + output.out
