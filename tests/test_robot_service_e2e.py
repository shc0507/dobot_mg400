"""Drive the real panel state through the robot service WebSocket.

The real PanelServer, RobotPanelState and keyboard stack control the simulated MG400 of
sim_mg400. A raw WebSocket client plays the page.
"""

import itertools
import socket
import threading
import time

import pytest
from panel_ws import PanelServer, RealSocket, WsClient, running
from sim_mg400 import CLIENT, JOGS, RADIAL_FRAME, READY, wait_for

from mg400.web_control import RobotPanelState

TOKEN = "e2e-test-token"
CLOSED = "browser connection closed."

pytestmark = pytest.mark.usefixtures("closed_clients", "isolated_controller_locks")


class SimulatedNetworkServer(PanelServer):
    """The real panel server. It accepts with the real socket class.

    The simulated MG400 replaces socket.socket, which socket.accept() calls.
    """

    def get_request(self):
        descriptor, address = self.socket._accept()
        return RealSocket(self.address_family, self.socket_type, fileno=descriptor), address


@pytest.fixture
def service():
    # The listener must exist before make_sim_rig replaces socket.socket.
    assert socket.socket is RealSocket
    with running(None, token=TOKEN, server_class=SimulatedNetworkServer) as server:
        yield server


@pytest.fixture
def rig(service, request):
    # Request make_sim_rig only now, after the listener exists.
    rig = request.getfixturevalue("make_sim_rig")()
    service.state = rig.state
    return rig


class Page:
    """Play the panel page: one WebSocket, one client ID and the shared key sequence."""

    def __init__(self, server):
        self.server = server
        self.seq = itertools.count(1)
        self.client = None

    def connect(self):
        self.client = WsClient(self.server)
        return self.client.hello(CLIENT, TOKEN)

    def command(self, body):
        reply = self.client.request("command", body)
        assert reply["status"] == 200, reply
        return reply["state"]

    def arm(self, speed=1):
        return self.command(
            {"action": "keyboard", "enabled": True, "speed": speed, "seq": next(self.seq)}
        )


@pytest.fixture
def page(service):
    return Page(service)


class Renewals:
    """Send the held keys every 100 ms over the socket, as the focused page does."""

    def __init__(self, page, *held):
        self._page = page
        self._held = list(held)
        self._stop = threading.Event()
        self.ids = []
        self.last_sent = None
        self._thread = threading.Thread(target=self._run, name="test-renewals", daemon=True)

    def start(self):
        self._send()
        self._thread.start()
        return self

    def _send(self):
        body = {"seq": next(self._page.seq), "held": self._held}
        self.ids.append(self._page.client.send_request("keys", body))
        self.last_sent = time.monotonic()

    def _run(self):
        while not self._stop.wait(0.1):
            self._send()

    def acknowledged(self):
        replies = [self._page.client.replies.get(request_id) for request_id in self.ids]
        assert all(reply is None or reply["status"] == 200 for reply in replies), replies
        return sum(reply is not None for reply in replies)

    def stop(self):
        """Stop renewing. Return the time of the last renewal."""
        self._stop.set()
        self._thread.join(2)
        return self.last_sent


def hold_w(rig, page):
    """Connect, sync and arm over the socket, then hold W with renewals until it jogs."""
    first = page.connect()
    assert first["controller_id"] is None and not first["keyboard_enabled"]
    page.command({"action": "start_sync"})
    rig.wait(lambda s: s["feedback_fresh"] and s["robot_mode"] == 5)
    assert rig.snapshot()["controller_id"] == CLIENT
    page.arm()
    rig.wait(lambda s: s["keyboard_enabled"])
    assert rig.snapshot()["message"] == READY
    renewals = Renewals(page, "W").start()
    rig.wait(lambda s: s["robot_mode"] == 11)
    assert rig.controller.mutations() == ["SpeedFactor(1)", RADIAL_FRAME, JOGS["W"]]
    return renewals


def assert_verified_jog_stop(rig, reason):
    rig.wait(lambda s: not s["busy"] and s["robot_mode"] == 5)
    assert rig.controller.mutations() == ["SpeedFactor(1)", RADIAL_FRAME, JOGS["W"], "MoveJog()"]
    snapshot = rig.snapshot()
    assert not snapshot["keyboard_enabled"] and snapshot["keyboard_disarm_reason"] == reason
    assert snapshot["message"] == f"Keyboard off: {reason}"
    # Only keyboard input ended: no core stop, no lock, and the page keeps control.
    assert not snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert snapshot["controller_id"] == CLIENT and snapshot["syncing"]
    assert "ResetRobot()" not in rig.controller.sent()


def test_stopped_key_renewals_stop_the_jog_within_the_dead_man(rig, page, monkeypatch):
    assert RobotPanelState.KEY_LEASE_SECONDS == 30.0
    monkeypatch.setattr(RobotPanelState, "KEY_LEASE_SECONDS", 0.4)
    renewals = hold_w(rig, page)
    # Eight renewals span twice the dead-man. The hold continues while they arrive.
    wait_for(lambda: renewals.acknowledged() >= 8, describe=rig.describe)
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 11 and snapshot["keyboard_active_key"] == "W"
    assert snapshot["keyboard_enabled"] and "MoveJog()" not in rig.controller.sent()
    last = renewals.stop()
    wait_for(lambda: not rig.snapshot()["keyboard_enabled"], timeout=2, describe=rig.describe)
    keys_off = time.monotonic()
    wait_for(lambda: "MoveJog()" in rig.controller.sent(), timeout=2, describe=rig.describe)
    jog_stop = time.monotonic()
    # The dead-man is 0.4 s and the watchdog checks every 50 ms. Allow scheduling slack.
    assert keys_off - last < 1.0
    assert jog_stop - last < 1.0
    assert_verified_jog_stop(rig, "key updates stopped.")


@pytest.mark.parametrize("ending", ["close-frame", "tcp-reset"])
def test_closed_page_connection_stops_the_jog_without_the_dead_man(rig, page, ending):
    # A 30 s dead-man cannot stop the jog in this test. Only the lost connection can.
    assert RobotPanelState.KEY_LEASE_SECONDS == 30.0
    renewals = hold_w(rig, page)
    wait_for(lambda: renewals.acknowledged() >= 3, describe=rig.describe)
    renewals.stop()
    closed = time.monotonic()
    if ending == "close-frame":
        assert page.client.close() == 1000
    else:
        page.client.reset()
    wait_for(lambda: "MoveJog()" in rig.controller.sent(), timeout=2, describe=rig.describe)
    assert time.monotonic() - closed < 1.0
    snapshot = rig.snapshot()
    assert not snapshot["keyboard_enabled"] and snapshot["keyboard_disarm_reason"] == CLOSED
    assert_verified_jog_stop(rig, CLOSED)


@pytest.mark.parametrize("ending", ["closed", "replaced"])
def test_reconnect_within_the_lease_keeps_control_with_keys_off(rig, page, ending):
    renewals = hold_w(rig, page)
    wait_for(lambda: renewals.acknowledged() >= 3, describe=rig.describe)
    renewals.stop()
    older = page.client
    if ending == "closed":
        assert older.close() == 1000
    # The page reloads or reconnects with the same client ID.
    first = page.connect()
    if ending == "replaced":
        assert older.closed_by_service() == (4409, "Replaced by a newer connection")
    assert first["controller_id"] == CLIENT and not first["locked"]
    assert not first["keyboard_enabled"]
    assert_verified_jog_stop(rig, CLOSED)
    # The new connection holds the browser lease: it renews it and arms keys again.
    reply = page.client.request("heartbeat", {})
    assert reply["status"] == 200 and reply["state"]["controller_id"] == CLIENT
    page.arm()
    rig.wait(lambda s: s["keyboard_enabled"])
    snapshot = rig.snapshot()
    assert snapshot["message"] == READY and snapshot["controller_id"] == CLIENT
    assert not snapshot["locked"]
