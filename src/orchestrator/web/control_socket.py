"""Carry console control requests over one WebSocket per page.

Chrome opens at most 6 HTTP/1.1 connections to one host. The Motion workspace keeps 3
of them for camera streams, so control requests can wait behind polls. A WebSocket
does not use that pool. The page sends stage holds, arm keys and stops here as
``{"seq", "path", "body"}``. Each message runs the same POST route in-process, with
the page identity from the handshake. The reply is ``{"seq", "status", "body", "t"}``,
where ``t`` is the server monotonic time when the message arrived. HTTP stays the
fallback, and the page also sends every stop over HTTP.

Waitress cannot upgrade a connection, so this listener has its own loopback port. The
proxy routes ``/lab/api/console/socket`` here.
"""

import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response

from labkit.lease import validate_client
from mg400.websocket import (
    Connection,
    ConnectionClosed,
    ProtocolError,
    accept_key,
    valid_client_key,
)

# Only control requests. Every other request stays on HTTP.
ROUTES = re.compile(
    r"/api/(?:console/(?:ws/motion/(?:keys|stage/hold|stage/release)"
    r"|stop/[a-z][a-z0-9-]{0,31})|dashboard/(?:motion|heat|gas))"
)
MAX_SOCKETS = 32
MAX_PENDING = 8
# The proxy closes a quiet upstream after 60 s. A ping every 20 s keeps it open, and
# the browser's pong ends a read wait.
PING_S = 20.0
IDLE_S = 60.0

log = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug("%s %s", self.address_string(), format % args)

    def _refuse(self, status, message):
        data = json.dumps({"error": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 426:
            self.send_header("Upgrade", "websocket")
            self.send_header("Sec-WebSocket-Version", "13")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _tokens(self, name):
        values = ",".join(self.headers.get_all(name) or [])
        return {token.strip().lower() for token in values.split(",")}

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != self.server.path:
            return self._refuse(404, "Not found.")
        if (
            self.request_version != "HTTP/1.1"
            or "websocket" not in self._tokens("Upgrade")
            or "upgrade" not in self._tokens("Connection")
            or self.headers.get("Sec-WebSocket-Version", "").strip() != "13"
        ):
            return self._refuse(426, "Open this path as a version 13 WebSocket.")
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if len(self.headers.get_all("Sec-WebSocket-Key") or []) != 1 or not valid_client_key(key):
            return self._refuse(400, "The WebSocket key is invalid.")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "").lower()
        if origin is not None and urlsplit(origin).netloc.lower() != host:
            # HTTP requests need the X-MG400-Client header, which other sites cannot send.
            # Browsers send Origin with every WebSocket, so the same rule holds here.
            return self._refuse(403, "Open the socket from the console page.")
        clients = parse_qs(url.query).get("client", [])
        try:
            page = validate_client(clients[0] if len(clients) == 1 else None)
        except ValueError as error:
            return self._refuse(400, str(error))
        if not self.server.admit():
            return self._refuse(503, "Too many console sockets are open.")
        try:
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key(key))
            self.end_headers()
            # The connection now carries WebSocket frames, never another HTTP request.
            self.close_connection = True
            self._session(page)
        finally:
            self.server.discharge()

    def _session(self, page):
        self.connection.settimeout(IDLE_S)
        connection = Connection(self.connection, self.rfile, self.wfile)
        self.server.track(connection)
        done = threading.Event()
        slots = threading.BoundedSemaphore(MAX_PENDING)
        threading.Thread(
            target=self._ping, args=(connection, done), name="console-socket-ping", daemon=True
        ).start()
        try:
            while (text := connection.receive()) is not None:
                self._message(connection, page, text, slots)
        except ProtocolError as error:
            connection.close(error.code, str(error))
        except (EOFError, OSError, ConnectionClosed):
            pass
        finally:
            done.set()
            connection.close()
            connection.join(1)
            connection.abort()
            self.server.untrack(connection)

    @staticmethod
    def _ping(connection, done):
        while not done.wait(PING_S):
            try:
                connection.ping()
            except ConnectionClosed:
                return

    def _message(self, connection, page, text, slots):
        received = time.monotonic()
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        seq = message.get("seq") if isinstance(message, dict) else None
        if type(seq) is not int or not 0 <= seq < 2**53:
            raise ProtocolError(1007, "Send JSON objects with a whole-number seq.")
        path, body = message.get("path"), message.get("body")
        if not isinstance(path, str) or not ROUTES.fullmatch(path):
            self._reply(connection, seq, 404, {"error": "Send this request over HTTP."}, received)
        elif not isinstance(body, dict):
            self._reply(connection, seq, 400, {"error": "Expected a JSON object."}, received)
        elif not slots.acquire(blocking=False):
            self._reply(connection, seq, 503, {"error": "Too many requests wait."}, received)
        else:
            # A slow request must never delay the next one, for example a stop.
            threading.Thread(
                target=self._run,
                args=(connection, page, seq, path, body, received, slots),
                name="console-socket-request",
                daemon=True,
            ).start()

    def _run(self, connection, page, seq, path, body, received, slots):
        try:
            status, data = self.server.call(page, path, body, self.client_address[0])
        except Exception:
            log.exception("Console socket request failed")
            status, data = 500, {"error": "The console failed."}
        finally:
            slots.release()
        self._reply(connection, seq, status, data, received)

    @staticmethod
    def _reply(connection, seq, status, body, received):
        reply = {"seq": seq, "status": status, "body": body, "t": round(received, 4)}
        try:
            connection.send_text(json.dumps(reply))
        except ConnectionClosed:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app, prefix):
        self.app = app
        self.prefix = prefix
        self.path = prefix + "/api/console/socket"
        self.lock = threading.Lock()
        self.open = 0
        self.connections = set()
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, _Handler)

    def admit(self):
        with self.lock:
            if self.open >= MAX_SOCKETS:
                return False
            self.open += 1
            return True

    def discharge(self):
        with self.lock:
            self.open -= 1

    def track(self, connection):
        with self.lock:
            self.connections.add(connection)

    def untrack(self, connection):
        with self.lock:
            self.connections.discard(connection)

    def abort_all(self):
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            connection.abort()

    def call(self, page, path, body, address):
        """Run one POST route of the app, as the HTTP client of this page would."""
        builder = EnvironBuilder(
            path=self.prefix + path,
            method="POST",
            json=body,
            headers={"X-MG400-Client": page},
            environ_base={"REMOTE_ADDR": address},
        )
        try:
            environ = builder.get_environ()
        finally:
            builder.close()
        response = Response.from_app(self.app, environ, buffered=True)
        try:
            data = json.loads(response.get_data())
        except ValueError:
            data = None
        finally:
            response.close()
        if not isinstance(data, dict):
            data = {"error": "The console returned an unreadable response."}
        return response.status_code, data


class ControlSocket:
    """Serve the console control socket on host and port from a daemon thread."""

    def __init__(self, app, *, host, port, base_path):
        self._server = _Server((host, port), app, base_path.rstrip("/"))
        self.port = self._server.server_address[1]
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="console-socket", daemon=True
        )
        self._thread.start()
        return self

    def close(self):
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(5)
        self._server.abort_all()
        self._server.server_close()
