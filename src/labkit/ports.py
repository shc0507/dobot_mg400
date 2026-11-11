"""The loopback ports of the lab services. This table is the only copy.

Every service listens on 127.0.0.1 only. The proxy is the only entry on the VPN, and
MG400_PROXY_PORT can change its port. No other port is a setting.

deploy/install.py and deploy/mg400ctl load this file by path with the system Python,
so it imports nothing.
"""

HOST = "127.0.0.1"

PORTS = {
    "robot": 8766,
    "stage": 8767,
    "thermal": 8768,
    "furnace": 8769,
    "microscope": 8770,
    "vision": 8771,
    "orch": 8772,
    # The console control socket. Waitress cannot upgrade a connection.
    "orch-socket": 8774,
    "camera": 1985,
    "go2rtc": 1984,
    "go2rtc-rtsp": 8554,
    # The default proxy port. MG400_PROXY_PORT can change it.
    "proxy": 8080,
}


def url(name, path=""):
    """Return the loopback URL of one service, with an optional path after the slash."""
    return f"http://{HOST}:{PORTS[name]}/{path}"
