"""Helpers that run the real PanelServer of apps/control_panel/server.py on a real socket."""

import contextlib
import http.client
import runpy
import socket
import threading
import urllib.request
from pathlib import Path

PANEL = runpy.run_path(str(Path(__file__).resolve().parents[1] / "apps/control_panel/server.py"))
PanelServer = PANEL["PanelServer"]
# The end-to-end test replaces socket.socket with a simulated MG400. The listener
# keeps the real class from import time.
RealSocket = socket.socket


@contextlib.contextmanager
def running(state, *, base_path="/", server_class=PanelServer):
    server = server_class(("127.0.0.1", 0), state, base_path=base_path)
    # A short poll interval keeps shutdown() fast.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _RealConnection(http.client.HTTPConnection):
    """Connect with the real socket class, which a simulated MG400 may replace."""

    def connect(self):
        self.sock = RealSocket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _RealHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_RealConnection, request)


def real_opener():
    """Return a urllib opener that reaches the service while the MG400 simulator runs."""
    return urllib.request.build_opener(_RealHandler())
