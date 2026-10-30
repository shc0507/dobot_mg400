"""Startup must reserve its port before any camera acquisition begins."""

import socket
import sys

import pytest
import uvicorn

from thermal_cam import __main__ as cli
from thermal_cam import app


def unused_port():
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def test_busy_port_exits_before_creating_camera_app(monkeypatch, capsys):
    def unexpected_app(**kwargs):
        pytest.fail("An occupied port must not create the camera application")

    monkeypatch.setattr(app, "create_app", unexpected_app)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(sys, "argv", ["thermal-cam", "--port", str(port)])
        assert cli.main() == 1

    message = capsys.readouterr().err
    assert f"http://127.0.0.1:{port}" in message
    assert "No new camera connection was opened" in message
    assert "Ctrl+C" in message
    assert "--port" in message
    assert "Traceback" not in message


@pytest.mark.parametrize("server_fails", [False, True])
def test_reserved_socket_handoff_and_cleanup(monkeypatch, server_fails):
    port = unused_port()
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
            # The socket passed to the server owns the port, so another instance
            # cannot slip in between the availability check and server startup.
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
    assert captured["options"] == {"demo": True, "auto_connect": False}
    assert captured["listener"].fileno() == -1
    with socket.socket() as next_instance:
        next_instance.bind(("127.0.0.1", port))
