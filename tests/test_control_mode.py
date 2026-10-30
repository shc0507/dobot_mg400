"""Check mode switching without connecting to a robot."""

import json
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest
from helpers import block_network, fake_connection

from mg400 import control_mode as mode
from mg400.controller_ownership import ControllerOwnership

MISSING = object()


def disabled_status():
    return {
        "controlMode": "disable",
        "prjState": "stopped",
        "isCollision": 0,
        "isSafeSuspend": 0,
        "alarms": [None, [], [], [], [], None],
    }


class FakeTransport:
    host = "192.168.1.6"

    def __init__(self, tmp_path, config=None):
        self.original = json.dumps(config or {"mode": "tp", "name": ""}).encode()
        self.content = self.original
        self.backup_dir = tmp_path / "backups"
        self.events = []
        self.status = disabled_status()
        self.reply = {"status": True}
        self.failure = None
        self.dashboard_reply = "4"

    def read_config(self):
        self.events.append("read_config")
        if self.failure == "readback" and "write_config" in self.events:
            return self.original
        return self.content

    def write_config(self, content):
        backups = list(self.backup_dir.glob("*.json"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == self.original
        self.events.append("write_config")
        if self.failure == "write":
            raise OSError("SMB write failed")
        self.content = content

    def http(self, method, path, body=None):
        self.events.append((method, path, body))
        if path == mode.MODE_PATH:
            assert method == "POST"
            assert body == self.content
            if self.failure == "post":
                raise TimeoutError("HTTP reply timed out")
            if self.failure == "post_state":
                self.status["controlMode"] = "enable"
            return self.reply
        if path == "/settings/version":
            assert method == "POST" and body == b""
            return {"control": "1.7.0.4"}
        assert (method, path, body) == ("GET", "/protocol/exchange", None)
        return self.status.copy()

    def dashboard_mode(self):
        self.events.append("dashboard")
        if self.failure == "dashboard":
            raise RuntimeError("RobotMode() was rejected")
        return self.dashboard_reply


@pytest.fixture(autouse=True)
def isolated_locks_and_no_network(monkeypatch, isolated_controller_locks):
    block_network(monkeypatch, "create_connection")
    monkeypatch.setattr(
        mode.http.client, "HTTPConnection", Mock(side_effect=AssertionError("Network"))
    )


def test_switch_backs_up_then_writes_and_checks_in_order(tmp_path):
    transport = FakeTransport(tmp_path, {"mode": "tp", "name": "", "extra": {"value": 7}})
    result = mode.switch_to_tcp(transport, transport.backup_dir)
    content = transport.content
    assert json.loads(content) == {"mode": "tcp", "name": "", "extra": {"value": 7}}
    assert result["changed"] is True and result["tcp_verified"] is True
    assert transport.events == [
        "read_config",
        ("GET", "/protocol/exchange", None),
        "read_config",
        ("GET", "/protocol/exchange", None),
        "write_config",
        "read_config",
        ("POST", mode.MODE_PATH, content),
        "read_config",
        ("GET", "/protocol/exchange", None),
        "dashboard",
    ]
    assert list(transport.backup_dir.glob("*.json"))[0].read_bytes() == transport.original


@pytest.mark.parametrize(
    "field,bad",
    [
        ("controlMode", "enable"),
        ("controlMode", "drag"),
        ("prjState", "running"),
        ("isCollision", 1),
        ("isSafeSuspend", 1),
        ("isCollision", None),
        ("isCollision", "0"),
        ("isSafeSuspend", False),
        ("alarms", None),
        ("alarms", []),
        ("alarms", [[], [17]]),
        ("alarms", ""),
        ("controlMode", MISSING),
        ("prjState", MISSING),
        ("isCollision", MISSING),
        ("isSafeSuspend", MISSING),
        ("alarms", MISSING),
    ],
)
def test_state_guard_refuses_before_backup_or_write(tmp_path, field, bad):
    transport = FakeTransport(tmp_path)
    if bad is MISSING:
        del transport.status[field]
    else:
        transport.status[field] = bad
    with pytest.raises(RuntimeError, match="Switch refused"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "write_config" not in transport.events
    assert "dashboard" not in transport.events
    assert not transport.backup_dir.exists()


@pytest.mark.parametrize("saved_mode", ["io", "modbus", "unknown"])
def test_other_modes_are_preserved_and_refused(tmp_path, saved_mode):
    transport = FakeTransport(tmp_path, {"mode": saved_mode, "name": "project"})
    with pytest.raises(RuntimeError, match="saved mode"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert transport.content == transport.original
    assert transport.events == ["read_config"]


def test_backup_failure_prevents_robot_write(tmp_path, monkeypatch):
    transport = FakeTransport(tmp_path)
    monkeypatch.setattr(mode.os, "fsync", Mock(side_effect=OSError("Disk failure")))
    with pytest.raises(OSError, match="Disk failure"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "write_config" not in transport.events


def test_state_is_checked_again_immediately_before_write(tmp_path):
    transport = FakeTransport(tmp_path)
    original_read = transport.read_config

    def change_state_after_backup():
        if transport.backup_dir.exists():
            transport.status["controlMode"] = "enable"
        return original_read()

    transport.read_config = change_state_after_backup
    with pytest.raises(RuntimeError, match="controlMode"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "write_config" not in transport.events


def test_changed_config_prevents_overwriting_another_client(tmp_path):
    transport = FakeTransport(tmp_path)
    original_read = transport.read_config

    def change_config_after_backup():
        if transport.backup_dir.exists():
            transport.content = b'{"mode":"io","name":"other"}'
        return original_read()

    transport.read_config = change_config_after_backup
    with pytest.raises(RuntimeError, match="configuration changed"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "write_config" not in transport.events


@pytest.mark.parametrize(
    "failure,post_count",
    [
        ("write", 0),
        ("readback", 0),
        ("post", 1),
        ("post_state", 1),
        ("dashboard", 1),
    ],
)
def test_failure_after_write_reports_backup_and_does_not_retry(tmp_path, failure, post_count):
    transport = FakeTransport(tmp_path)
    transport.failure = failure
    with pytest.raises(RuntimeError, match="state may have changed.*Backup:") as caught:
        mode.switch_to_tcp(transport, transport.backup_dir)
    backup = next(transport.backup_dir.glob("*.json"))
    assert str(backup) in str(caught.value)
    assert transport.events.count("write_config") == 1
    assert (
        sum(isinstance(e, tuple) and e[1] == mode.MODE_PATH for e in transport.events) == post_count
    )


@pytest.mark.parametrize("status", [False, 1, "true", None])
def test_post_requires_boolean_true(tmp_path, status):
    transport = FakeTransport(tmp_path)
    transport.reply = {"status": status}
    with pytest.raises(RuntimeError, match="rejected the mode request"):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "dashboard" not in transport.events


def test_saved_tcp_mode_checks_dashboard_without_writing(tmp_path):
    transport = FakeTransport(tmp_path, {"mode": "tcp", "name": ""})
    result = mode.switch_to_tcp(transport, transport.backup_dir)
    assert result == {"configured_mode": "tcp", "tcp_verified": True, "changed": False}
    assert transport.events == [
        "read_config",
        ("GET", "/protocol/exchange", None),
        "dashboard",
        ("GET", "/protocol/exchange", None),
    ]
    assert not transport.backup_dir.exists()


def test_new_explicit_invocation_can_apply_a_saved_but_inactive_tcp_mode(tmp_path):
    first = FakeTransport(tmp_path / "first")
    first.failure = "post"
    with pytest.raises(RuntimeError, match="state may have changed"):
        mode.switch_to_tcp(first, first.backup_dir)
    assert json.loads(first.content)["mode"] == "tcp"

    second = FakeTransport(tmp_path / "second", json.loads(first.content))
    second.dashboard_mode = Mock(
        side_effect=[
            mode.ControllerCommandError(-1, "RobotMode()", "-1,{},RobotMode();"),
            "4",
        ]
    )
    result = mode.switch_to_tcp(second, second.backup_dir)
    assert result["changed"] is True and result["tcp_verified"] is True
    assert second.dashboard_mode.call_count == 2
    assert second.events.count("write_config") == 1
    assert (
        sum(isinstance(event, tuple) and event[1] == mode.MODE_PATH for event in second.events) == 1
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timeout"),
        RuntimeError("Malformed reply"),
        mode.ControllerCommandError(-2, "RobotMode()", "-2,{},RobotMode();"),
    ],
)
def test_saved_tcp_only_applies_after_complete_minus_one_rejection(tmp_path, error):
    transport = FakeTransport(tmp_path, {"mode": "tcp", "name": ""})
    transport.dashboard_mode = Mock(side_effect=error)
    with pytest.raises(type(error)):
        mode.switch_to_tcp(transport, transport.backup_dir)
    assert "write_config" not in transport.events
    assert not transport.backup_dir.exists()


def test_ownership_collision_prevents_all_network_operations(tmp_path):
    transport = FakeTransport(tmp_path)
    owner = ControllerOwnership(transport.host)
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="another panel"):
            mode.switch_to_tcp(transport, transport.backup_dir)
        assert transport.events == []
        assert mode.read_status(transport)["configured_mode"] == "tp"
    finally:
        owner.release()


@pytest.mark.parametrize(
    "reply",
    [
        b"-1,{},RobotMode();",
        b"0,{4},GetAngle();",
        b"0,{4},RobotMode()",
        b"0,{4},RobotMode();0,{4},RobotMode();",
        b"garbage",
    ],
)
def test_dashboard_rejects_failed_mismatched_and_incomplete_replies(monkeypatch, reply):
    connection = fake_connection(monkeypatch, [reply, b""], raw_socket=False)
    transport = mode.ControllerTransport("192.168.1.6")
    with pytest.raises(RuntimeError):
        payload = transport.dashboard_mode()
        if payload != "4":
            raise RuntimeError("Unexpected mode")
    connection.sendall.assert_called_once_with(b"RobotMode()")


def test_dashboard_accepts_fragmented_disabled_reply(monkeypatch):
    connection = fake_connection(monkeypatch, [b"0,{", b"4},RobotMode();"], raw_socket=False)
    assert mode.ControllerTransport("192.168.1.6").dashboard_mode() == "4"
    connection.sendall.assert_called_once_with(b"RobotMode()")


@pytest.mark.parametrize("reply", ["5", "4.0", "4,5"])
def test_switch_requires_disabled_dashboard_payload(tmp_path, reply):
    transport = FakeTransport(tmp_path)
    transport.dashboard_reply = reply
    with pytest.raises(RuntimeError, match="RobotMode.*disabled mode 4"):
        mode.switch_to_tcp(transport, transport.backup_dir)


def test_cli_defaults_to_read_only(tmp_path, monkeypatch, capsys):
    transport = FakeTransport(tmp_path)
    monkeypatch.setattr(mode, "ControllerTransport", lambda host, timeout: transport)
    assert mode.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configured_mode"] == "tp"
    assert result["tcp_verified"] is False
    assert transport.events == [
        "read_config",
        ("GET", "/protocol/exchange", None),
        ("POST", "/settings/version", b""),
    ]
    assert not transport.backup_dir.exists()


def test_smb_transport_uses_project_share_and_closes_each_connection(monkeypatch):
    connection = Mock()
    connection.connect.return_value = True

    def retrieve(share, path, stream, timeout):
        assert (share, path, timeout) == ("project", mode.CONFIG_PATH, 3.0)
        stream.write(b'{"mode":"tp","name":""}')

    connection.retrieveFile.side_effect = retrieve
    module = ModuleType("smb.SMBConnection")
    module.SMBConnection = Mock(return_value=connection)
    monkeypatch.setitem(sys.modules, "smb.SMBConnection", module)
    transport = mode.ControllerTransport("192.168.1.6", timeout=3.0)
    assert json.loads(transport.read_config())["mode"] == "tp"
    transport.write_config(b'{"mode":"tcp","name":""}')
    assert module.SMBConnection.call_args.args == ("", "", "mg400mode", "MG400")
    assert module.SMBConnection.call_args.kwargs == {"use_ntlm_v2": True, "is_direct_tcp": True}
    connection.connect.assert_called_with("192.168.1.6", 445, timeout=3.0)
    share, path, stream = connection.storeFile.call_args.args
    assert (share, path, stream.read()) == (
        "project",
        mode.CONFIG_PATH,
        b'{"mode":"tcp","name":""}',
    )
    assert connection.close.call_count == 2


def test_http_transport_does_not_accept_redirects(monkeypatch):
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 302
    response.read.return_value = b'{"status":true}'
    monkeypatch.setattr(mode.http.client, "HTTPConnection", Mock(return_value=connection))
    with pytest.raises(RuntimeError, match="Invalid HTTP response"):
        mode.ControllerTransport("192.168.1.6").http("POST", mode.MODE_PATH, b"{}")
    connection.request.assert_called_once_with(
        "POST", mode.MODE_PATH, b"{}", {"Content-Type": "application/json"}
    )
    connection.close.assert_called_once()
