"""Startup reserves a fake listener before any camera acquisition begins."""

import errno
import socket
import sys

import pytest
import uvicorn

from thermal_cam import __main__ as cli
from thermal_cam import app


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for name in ("MG400_THERMAL_BIND", "MG400_THERMAL_PORT", "MG400_THERMAL_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sockets(monkeypatch):
    bound = {}
    created = []

    class FakeSocket:
        def __init__(self, family=socket.AF_INET, kind=socket.SOCK_STREAM):
            self.family = family
            self.address = None
            self.listening = False
            self.closed = False
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True
            if self.address is not None:
                del bound[self.address]

        def setsockopt(self, level, option, value):
            assert (level, option, value) == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        def bind(self, address):
            if address in bound:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            bound[address] = self
            self.address = address

        def listen(self, backlog=128):
            self.listening = True

        def setblocking(self, blocking):
            assert blocking is False

        def getsockname(self):
            return self.address

        def getsockopt(self, level, option):
            assert (level, option) == (socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            return self.listening

        def fileno(self):
            return -1 if self.closed else 42

    monkeypatch.setattr(cli.socket, "socket", FakeSocket)
    return created


def test_busy_port_exits_before_creating_camera_app(monkeypatch, capsys, sockets):
    def unexpected_app(**kwargs):
        pytest.fail("An occupied port must not create the camera application")

    monkeypatch.setattr(app, "create_app", unexpected_app)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 8768))
        occupied.listen()
        monkeypatch.setattr(sys, "argv", ["thermal-cam"])
        assert cli.main() == 1

    message = capsys.readouterr().err
    assert "http://127.0.0.1:8768" in message
    assert "No new camera connection was opened" in message
    assert "Ctrl+C" in message
    assert "--port" in message
    assert "Traceback" not in message
    assert all(listener.closed for listener in sockets)


@pytest.mark.parametrize("server_fails", [False, True])
def test_reserved_socket_handoff_and_cleanup(monkeypatch, server_fails, sockets):
    port = 9876
    monkeypatch.setattr(sys, "argv", ["thermal-cam", "--demo", "--no-connect", "--port", str(port)])
    captured = {}
    sentinel_app = object()

    def fake_app(**kwargs):
        captured["options"] = kwargs
        return sentinel_app

    class FakeServer:
        def __init__(self, config):
            assert config.app is sentinel_app
            assert config.workers == 1

        def run(self, sockets):
            assert len(sockets) == 1
            listener = captured["listener"] = sockets[0]
            assert listener.getsockname() == ("127.0.0.1", port)
            assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            with socket.socket() as competing:
                with pytest.raises(OSError):
                    competing.bind(("127.0.0.1", port))
            if server_fails:
                raise RuntimeError("server startup failed")

    monkeypatch.setattr(app, "create_app", fake_app)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    if server_fails:
        with pytest.raises(RuntimeError, match="server startup failed"):
            cli.main()
    else:
        assert cli.main() == 0
    assert captured["options"] == {"demo": True, "auto_connect": False, "token": ""}
    assert captured["listener"].fileno() == -1
    with socket.socket() as next_instance:
        next_instance.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "settings,args",
    [
        ({"MG400_THERMAL_BIND": "0.0.0.0"}, []),
        ({"MG400_THERMAL_TOKEN": "too-short"}, []),
        ({"MG400_THERMAL_TOKEN": "x" * 23}, []),
        ({"MG400_THERMAL_TOKEN": "x" * 24 + " "}, []),
        ({"MG400_THERMAL_TOKEN": " " + "x" * 24}, []),
        ({"MG400_THERMAL_TOKEN": "x" * 24 + "\n"}, []),
        ({"MG400_THERMAL_TOKEN": "x" * 24 + "é"}, []),
        ({"MG400_THERMAL_PORT": "invalid"}, []),
        ({"MG400_THERMAL_PORT": "65536"}, []),
        ({}, ["--port", "0"]),
        ({}, ["--bind", ""]),
        ({}, ["--bind", " 127.0.0.1"]),
    ],
)
def test_configuration_errors_precede_socket_or_camera_access(monkeypatch, settings, args):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid configuration must not open a socket or camera")

    monkeypatch.setattr(cli.socket, "socket", forbidden)
    monkeypatch.setattr(app, "create_app", forbidden)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["thermal-cam", *args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize("override", [False, True])
def test_environment_settings_cli_override_and_token_handoff(monkeypatch, sockets, override):
    token = "thermal-test-token-length-24"
    monkeypatch.setenv("MG400_THERMAL_BIND", "0.0.0.0")
    monkeypatch.setenv("MG400_THERMAL_PORT", "9999")
    monkeypatch.setenv("MG400_THERMAL_TOKEN", token)
    args = ["--bind", "::1", "--port", "8768"] if override else []
    monkeypatch.setattr(sys, "argv", ["thermal-cam", "--demo", "--no-connect", *args])
    sentinel = object()

    def fake_app(**kwargs):
        assert kwargs == {"demo": True, "auto_connect": False, "token": token}
        return sentinel

    class FakeServer:
        def __init__(self, config):
            assert config.app is sentinel
            assert config.workers == 1
            assert config.host == ("::1" if override else "0.0.0.0")
            assert config.port == (8768 if override else 9999)

        def run(self, sockets):
            assert sockets[0].family == (socket.AF_INET6 if override else socket.AF_INET)
            assert sockets[0].address == (("::1", 8768) if override else ("0.0.0.0", 9999))

    monkeypatch.setattr(app, "create_app", fake_app)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert cli.main() == 0
    assert sockets[0].closed
