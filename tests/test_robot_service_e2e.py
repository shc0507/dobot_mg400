"""Drive the real panel state through the robot service HTTP contract.

The real PanelServer, RobotPanelState and keyboard stack control the simulated MG400 of
sim_mg400. A labkit client plays the page.
"""

import itertools
import socket
import threading
import time

import pytest
from panel_http import PanelServer, RealSocket, real_opener, running
from sim_mg400 import CLIENT, JOGS, RADIAL_FRAME, READY, wait_for

from labkit.client import ServiceClient
from mg400.web_control import RobotPanelState

pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


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
    with running(None, server_class=SimulatedNetworkServer) as server:
        yield server


@pytest.fixture
def rig(service, request):
    # Request make_sim_rig only now, after the listener exists.
    rig = request.getfixturevalue("make_sim_rig")()
    service.state = rig.state
    return rig


class Page:
    """Play the page: one HTTP client, one client ID and the shared key sequence."""

    def __init__(self, server):
        self.server = server
        self.seq = itertools.count(1)
        self.http = ServiceClient(f"http://127.0.0.1:{server.server_port}/", CLIENT, timeout=3)
        self.http._opener = real_opener()

    def take(self):
        return self.http.heartbeat()

    def command(self, action, **fields):
        return self.http.command(action, **fields)["state"]

    def arm(self, speed=1):
        return self.command("keyboard", enabled=True, speed=speed, seq=next(self.seq))


@pytest.fixture
def page(service):
    return Page(service)


class Renewals:
    """Send the held keys every 100 ms, as the focused page does."""

    def __init__(self, page, *held):
        self._page = page
        self._held = list(held)
        self._stop = threading.Event()
        self.accepted = 0
        self.errors = []
        self.last_sent = None
        self._thread = threading.Thread(target=self._run, name="test-renewals", daemon=True)

    def start(self):
        self._send()
        self._thread.start()
        return self

    def _send(self):
        try:
            self._page.http.command("keys", seq=next(self._page.seq), held=self._held)
            self.accepted += 1
        except Exception as error:
            self.errors.append(error)
        self.last_sent = time.monotonic()

    def _run(self):
        while not self._stop.wait(0.1):
            self._send()

    def acknowledged(self):
        assert not self.errors, self.errors
        return self.accepted

    def stop(self):
        """Stop renewing. Return the time of the last renewal."""
        self._stop.set()
        self._thread.join(2)
        return self.last_sent


def hold_w(rig, page):
    """Take control and arm over HTTP, then hold W with renewals until it jogs."""
    first = page.http.state()
    assert first["controller_id"] is None and not first["keyboard_enabled"]
    page.take()
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
