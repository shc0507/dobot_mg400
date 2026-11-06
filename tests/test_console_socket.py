"""The console control socket runs the control routes of one page over a WebSocket."""

import base64
import json
import os
import socket

import pytest
from test_console_motion import HOLD, mode, sim, stage_ready, wait_for  # noqa: F401

from mg400.websocket import Connection
from orchestrator.app import create_app
from orchestrator.web.control_socket import ControlSocket

PAGE = "console-page-a"
SOCKET = "/lab/api/console/socket"


@pytest.fixture
def served(sim):  # noqa: F811
    control = ControlSocket(
        create_app(sim.service, base_path="/lab"), host="127.0.0.1", port=0, base_path="/lab/"
    ).start()
    sim.port = control.port
    yield sim
    control.close()


def handshake(port, *, path=SOCKET, query=f"client={PAGE}", origin=None, upgrade=True):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    lines = [f"GET {path}?{query} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    if upgrade:
        key = base64.b64encode(os.urandom(16)).decode()
        lines += ["Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}"]
        lines.append("Sec-WebSocket-Version: 13")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    rfile, wfile = sock.makefile("rb"), sock.makefile("wb")
    status = int(rfile.readline().split()[1])
    while rfile.readline() not in (b"\r\n", b""):
        pass
    if status != 101:
        sock.close()
        return status, None
    return status, Connection(sock, rfile, wfile, client=True)


def call(connection, path, body, seq=1):
    connection.send_text(json.dumps({"seq": seq, "path": path, "body": body}))
    return json.loads(connection.receive())


def test_socket_runs_stage_holds_for_the_page_that_opened_it(served):
    motor = stage_ready(served)[0]
    status, connection = handshake(served.port)
    assert status == 101
    hold = {"id": HOLD, "motor": motor, "direction": "cw", "speed": 50}
    reply = call(connection, "/api/console/ws/motion/stage/hold", hold)
    assert reply["seq"] == 1 and reply["status"] == 200
    assert reply["body"] == {"outcome": "accepted"} and isinstance(reply["t"], float)
    wait_for(lambda: mode(served, motor) == "continuous")
    reply = call(connection, "/api/console/ws/motion/stage/release", {"id": HOLD}, seq=2)
    assert (reply["seq"], reply["status"]) == (2, 200)
    wait_for(lambda: mode(served, motor) == "idle")
    # The socket acts for its own page only. Another page has no control.
    _, other = handshake(served.port, query="client=console-page-b")
    reply = call(
        other, "/api/console/ws/motion/stage/hold", {**hold, "id": "hold-fedcba9876543210"}
    )
    assert reply["status"] == 409 and "Take control" in reply["body"]["error"]


def test_socket_stops_need_no_control(served):
    _, connection = handshake(served.port, query="client=console-page-b")
    reply = call(connection, "/api/console/stop/stage", {})
    assert reply["status"] == 200 and reply["body"]["device"] == "stage"
    reply = call(connection, "/api/dashboard/motion", {}, seq=2)
    assert reply["status"] == 202 and "operation_id" in reply["body"]
    for seq, action in ((3, "heat"), (4, "gas")):
        reply = call(connection, f"/api/dashboard/{action}", {"confirmed": True}, seq=seq)
        assert reply["status"] == 202 and "operation_id" in reply["body"]
    reply = call(connection, "/api/dashboard/heat", {}, seq=5)
    assert reply["status"] == 400


def test_socket_sends_other_requests_back_to_http(served):
    _, connection = handshake(served.port)
    reply = call(connection, "/api/console/control/stage/take", {})
    assert reply["status"] == 404 and "HTTP" in reply["body"]["error"]
    reply = call(connection, "/api/console/stop/stage", [], seq=2)
    assert reply["status"] == 400
    # A message without a whole-number seq closes the socket.
    connection.send_text(json.dumps({"seq": "1", "path": "/api/dashboard/motion", "body": {}}))
    assert connection.receive() is None and connection.close_received == 1007


@pytest.mark.parametrize(
    "options,status",
    [
        ({"origin": "http://other.example"}, 403),
        ({"query": "client="}, 400),
        ({"query": ""}, 400),
        ({"upgrade": False}, 426),
        ({"path": "/lab/api/console/state"}, 404),
    ],
)
def test_socket_handshake_refuses_other_sites_and_plain_requests(served, options, status):
    assert handshake(served.port, **options)[0] == status


def test_socket_accepts_the_console_origin(served):
    status, connection = handshake(served.port, origin=f"http://127.0.0.1:{served.port}")
    assert status == 101
    connection.close()
