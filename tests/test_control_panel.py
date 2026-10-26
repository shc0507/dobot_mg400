"""Check the independent browser panel without robot connections."""

import http.client
import json
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.controller_ownership import ControllerOwnership

PANEL = runpy.run_path(str(Path(__file__).resolve().parents[1] / "apps/control_panel/server.py"))
ReadOnlyPanelState = PANEL["ReadOnlyPanelState"]
PanelServer = PANEL["PanelServer"]


@pytest.fixture(autouse=True)
def isolated_controller_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path))


def test_read_only_mode_is_inert_until_sync_and_has_no_hardware_commands():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise OSError("test offline")

    panel = ReadOnlyPanelState("test-host", connection_factory=forbidden)
    assert panel.snapshot()["actual"] == [None] * 5
    assert calls == []
    for action in ("enable", "disable", "home", "execute", "stop", "initialize_gripper"):
        command = {"action": action}
        if action == "execute":
            command["targets"] = {"x": 250, "y": 0, "z": 100, "r": 0, "grip": 500}
        with pytest.raises(PermissionError):
            panel.command(command)
    panel.command({"action": "start_sync"})
    panel._reader.join(timeout=1)
    assert calls == [("test-host",)]
    assert not panel.snapshot()["syncing"]
    assert "test offline" in panel.snapshot()["message"]
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


def test_passive_feedback_connection_failure_allows_retry():
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args)
        raise OSError("Robot is offline")

    panel = ReadOnlyPanelState("test-host", connection_factory=fail)
    try:
        for _ in range(2):
            panel.command({"action": "start_sync"})
            panel._reader.join(1)
            assert not panel.snapshot()["syncing"]
            assert "Robot is offline" in panel.snapshot()["message"]
        assert attempts == [("test-host",), ("test-host",)]
        contender = ControllerOwnership("test-host")
        try:
            contender.acquire()
        finally:
            contender.release()
    finally:
        panel.close()


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
    assert request(server, "POST", "/api/command", "{", headers)[0] == 400
    for invalid in ([], {}, None, True):
        assert (
            request(server, "POST", "/api/command", json.dumps({"action": invalid}), headers)[0]
            == 400
        )
    assert (
        request(server, "POST", "/api/command", body, {**headers, "Sec-Fetch-Site": "cross-site"})[
            0
        ]
        == 403
    )
    assert request(server, "GET", "/../config/robot.toml")[0] == 404


def test_remote_bind_requires_access_token(monkeypatch):
    monkeypatch.delenv("MG400_PANEL_TOKEN", raising=False)
    with pytest.raises(SystemExit) as error:
        PANEL["main"](["--bind", "0.0.0.0"])
    assert error.value.code == 2


def test_cli_defaults_to_robot_without_starting_sync(monkeypatch):
    main = PANEL["main"]
    state = Mock()
    robot = Mock(return_value=state)
    server = Mock(server_port=8766)
    host_loader = Mock(return_value="robot.test")
    monkeypatch.setitem(main.__globals__, "RobotPanelState", robot)
    monkeypatch.setitem(main.__globals__, "PanelServer", Mock(return_value=server))
    monkeypatch.setitem(main.__globals__, "load_robot_host", host_loader)

    main([])

    host_loader.assert_called_once_with(PANEL["ROOT"] / "config/robot.toml")
    robot.assert_called_once_with("robot.test", gripper=None)
    state.command.assert_not_called()
    state.close.assert_called_once_with()


def test_cli_rejects_demo_before_constructing_a_backend(monkeypatch, capsys):
    main = PANEL["main"]
    robot, read_only = Mock(), Mock()
    monkeypatch.setitem(main.__globals__, "RobotPanelState", robot)
    monkeypatch.setitem(main.__globals__, "ReadOnlyPanelState", read_only)

    with pytest.raises(SystemExit) as error:
        main(["--mode", "demo"])

    assert error.value.code == 2
    assert "invalid choice: 'demo'" in capsys.readouterr().err
    robot.assert_not_called()
    read_only.assert_not_called()


def test_reverse_proxy_base_path(server):
    server.base_path = "/mg400-panel/"
    auth = {"Authorization": "Bearer test-token"}
    assert request(server, "GET", "/mg400-panel")[0] == 308
    assert request(server, "GET", "/mg400-panel/")[0] == 200
    assert request(server, "GET", "/mg400-panel/api/state", headers=auth)[0] == 200
    assert request(server, "GET", "/api/state", headers=auth)[0] == 404
    headers = {**auth, "Content-Type": "application/json", "X-MG400-Panel": "1"}
    assert (
        request(
            server, "POST", "/mg400-panel/api/command", json.dumps({"action": "stop_sync"}), headers
        )[0]
        == 200
    )


def test_robot_http_passes_browser_identity_and_keeps_heartbeat_separate(server):
    calls = []
    original = server.state

    class RobotState:
        mode = "robot"

        def command(self, payload, client_id=None):
            calls.append((payload, client_id))
            return {"mode": "robot", "busy": True}

        def heartbeat(self, client_id):
            calls.append(("heartbeat", client_id))
            return {"mode": "robot", "busy": True}

    server.state = RobotState()
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
        "X-MG400-Panel": "1",
        "X-MG400-Client": "browser-a",
    }
    try:
        status, body = request(
            server, "POST", "/api/command", json.dumps({"action": "stop"}), headers
        )
        assert status == 200 and json.loads(body)["busy"]
        assert request(server, "POST", "/api/heartbeat", "{}", headers)[0] == 200
        assert calls == [({"action": "stop"}, "browser-a"), ("heartbeat", "browser-a")]
        assert request(server, "POST", "/api/heartbeat", '{"action":"enable"}', headers)[0] == 400
        headers.pop("X-MG400-Client")
        assert request(server, "POST", "/api/command", '{"action":"enable"}', headers)[0] == 400
        assert len(calls) == 2
    finally:
        server.state = original


def test_gripper_feedback_requires_robot_mode():
    with pytest.raises(SystemExit) as error:
        PANEL["main"](["--mode", "read-only", "--gripper-feedback", "unused.json"])
    assert error.value.code == 2


def test_robot_http_starts_without_connecting_and_rejects_motion_before_ownership(server):
    robot = PANEL["RobotPanelState"]("unused.invalid")
    original, server.state = server.state, robot
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
        "X-MG400-Panel": "1",
        "X-MG400-Client": "browser-a",
    }
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
