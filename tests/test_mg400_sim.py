"""Exercise the runnable simulator through production network and file clients."""

import os
import socket
import subprocess
import sys
from contextlib import closing

import pytest
from helpers import wait_for
from panel_ws import WsClient, running

from mg400.feedback import Feedback, FeedbackConnection, decode_feedback
from mg400.sim import Simulator
from mg400.sim.runtime import PORTS, feedback_frame
from mg400.web_control import RobotPanelState


@pytest.fixture
def loopback_host():
    # Each process gets a separate part of 127/8. Probe all controller ports before use.
    for suffix in range(1, 255):
        host = f"127.201.{os.getpid() % 254 + 1}.{suffix}"
        probes = []
        try:
            for port in PORTS:
                probe = socket.socket()
                probes.append(probe)
                probe.bind((host, port))
        except OSError:
            continue
        else:
            return host
        finally:
            for probe in probes:
                probe.close()
    pytest.fail("No free simulator address in the test's loopback range")


def test_feedback_wire_format_preserves_controller_fields():
    sample = Feedback((1.25, -2.5, 3.75, 4.0), 4, 1234, True, 37.0, 0b1, 0b10, 0.2)
    assert decode_feedback(feedback_frame(sample)) == sample


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "localhost", "robot.test"])
def test_refuse_non_numeric_or_non_loopback_addresses(host):
    with pytest.raises(ValueError, match="numeric loopback"):
        Simulator(host)


def test_construction_does_not_open_transports(loopback_host):
    simulator = Simulator(loopback_host)
    with closing(socket.socket()) as listener:
        listener.bind((loopback_host, 29999))
    simulator.close()


def test_start_failure_releases_already_bound_ports(loopback_host):
    with closing(socket.socket()) as occupied:
        occupied.bind((loopback_host, 30003))
        with pytest.raises(OSError):
            Simulator(loopback_host).start()
        with closing(socket.socket()) as available:
            available.bind((loopback_host, 29999))


def test_live_feedback_and_fragmented_coalesced_commands(loopback_host):
    with Simulator(loopback_host) as simulator:
        with FeedbackConnection(loopback_host) as feed:
            first = feed.read_latest()
            assert first.mode == 4
            with socket.create_connection((loopback_host, 29999), timeout=2) as dashboard:
                dashboard.sendall(b"EnableRo")
                dashboard.sendall(b"bot()RobotMode()Bogus()")
                reply = b""
                while reply.count(b";") != 3:
                    reply += dashboard.recv(4096)
                assert reply == b"0,{},EnableRobot();0,{5},RobotMode();-1,{},Bogus();"
            wait_for(lambda: simulator.controller.sample.timestamp > first.timestamp)
            assert feed.read_latest().mode == 5
        with socket.create_connection((loopback_host, 30003), timeout=2) as motion:
            motion.sendall(b"JointMovJ(31,2,11,4)")
            assert motion.recv(1024) == b"0,{},JointMovJ(31,2,11,4);"
            assert simulator.controller.sample.joints_deg == (31.0, 2.0, 11.0, 4.0)
            motion.sendall(b"JointMovJ(nan,2,11,4)")
            assert motion.recv(1024).startswith(b"-1,")
            assert simulator.controller.sample.joints_deg == (31.0, 2.0, 11.0, 4.0)
    for port in PORTS:
        with closing(socket.socket()) as available:
            available.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            available.bind((loopback_host, port))


@pytest.mark.usefixtures("isolated_controller_locks", "closed_clients")
def test_real_panel_sequence_over_loopback(loopback_host):
    with Simulator(loopback_host) as simulator:
        state = RobotPanelState(loopback_host)
        try:
            with running(state) as server:
                page = WsClient(server)
                page.hello()

                def command(action, **fields):
                    reply = page.request("command", {"action": action, **fields})
                    assert reply["status"] == 200, reply

                def settled(condition):
                    def current():
                        reply = page.request("heartbeat", {})
                        assert reply["status"] == 200, reply
                        snapshot = reply["state"]
                        return not snapshot["busy"] and condition(snapshot)

                    wait_for(current, timeout=8, describe=lambda: repr(state.snapshot()))

                command("start_sync")
                settled(lambda s: s["feedback_fresh"] and s["robot_mode"] == 4)
                command("enable")
                settled(lambda s: s["robot_mode"] == 5)
                target = {"x": 260, "y": 12, "z": 42, "r": 31}
                command("execute", targets=target)
                settled(lambda s: s["actual"] == list(target.values()))
                command("disable")
                settled(lambda s: s["robot_mode"] == 4 and not s["locked"])
                command("stop_sync")
                wait_for(lambda: not state.snapshot()["syncing"], timeout=8)
                assert not state.snapshot()["locked"]
                assert state.snapshot()["message"] == "Robot disable is confirmed."
                assert not state.snapshot()["stop_unconfirmed"]
                assert simulator.controller.sample.mode == 4
        finally:
            state.close()


def test_cli_refuses_external_address():
    result = subprocess.run(
        [sys.executable, "-m", "mg400.sim", "--host", "192.0.2.1"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "numeric loopback" in result.stderr
