"""Exercise owned demo services while rejecting every non-loopback connection."""

import ipaddress
import socket

import pytest

from orchestrator.rehearsal import Rehearsal


@pytest.fixture
def isolated_network(monkeypatch):
    connected = []
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def check_host(host):
        assert ipaddress.ip_address(host).is_loopback, f"Unexpected destination: {host}"

    def connect(sock, address):
        check_host(address[0])
        connected.append(address)
        return original_connect(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        check_host(host)
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # These are deliberately unusable sentinels. The connection guard above
    # fails before any network access if production settings leak into a demo.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://production.invalid:9")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    for name in ("ROBOT", "STAGE", "THERMAL", "VISION", "FURNACE", "MICROSCOPE"):
        monkeypatch.setenv(f"MG400_ORCH_{name}_URL", "http://production.invalid:9")
        monkeypatch.setenv(f"MG400_ORCH_{name}_TOKEN", "unused-production-sentinel-token")

    import serial
    import usb.core

    def no_device(*args, **kwargs):
        pytest.fail("Rehearsal attempted to open a real device")

    monkeypatch.setattr(serial, "Serial", no_device)
    monkeypatch.setattr(usb.core, "find", no_device)
    return connected


def test_demo_factories_ignore_production_settings_and_proxies(tmp_path, isolated_network):
    with Rehearsal(tmp_path, ("robot", "stage", "thermal"), identity="demo-isolation") as demo:
        assert set(demo.clients) == {"robot", "stage", "thermal"}
        for client in demo.clients.values():
            assert client.describe()["demo"] is True
            try:
                client.acquire()
                state = client.state()
                assert client.owned(state)
                assert client.healthy(state)
                # This reaches the actual robot HTTP handler and both generic contract adapters.
                assert client.stop()["stop"] == "confirmed"
                client.release()
            finally:
                client.close()
    assert isolated_network
