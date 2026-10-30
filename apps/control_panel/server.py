"""Serve the MG400 robot service and its browser panel.

Run with Python 3.12 or later. Robot mode can send hardware commands.
"""

import argparse
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mg400.controller_ownership import ControllerOwnership  # noqa: E402
from mg400.feedback import FeedbackConnection  # noqa: E402
from mg400.robot_config import load_robot_host  # noqa: E402
from mg400.web_control import RobotPanelState  # noqa: E402
from mg400.web_gripper import WebGripper  # noqa: E402
from mg400.websocket import (  # noqa: E402
    Connection,
    ConnectionClosed,
    ProtocolError,
    accept_key,
    valid_client_key,
)

ASSETS = Path(__file__).resolve().parent
MAX_SEQUENCE = 2**53
# The WebSocket message format. Change it when a message changes incompatibly.
PROTOCOL = 1


def normalize_base_path(value):
    if (
        not value.startswith("/")
        or any(char in value for char in "?#\\")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Base path must be an absolute URL path without dot segments or a query")
    return value.rstrip("/") + "/"


def valid_client(value):
    """Accept 1 to 128 printable ASCII characters. The ID appears in the service log."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and value.isprintable()
        and bool(value.strip())
    )


def valid_token(value):
    """Accept a token that a browser sends unchanged in the Authorization header.

    Browsers send only these characters and strip spaces at either end of a value.
    """
    return all(" " <= char <= "~" for char in value) and value == value.strip(" ")


def dispatch(state, kind, payload, client):
    """Apply one API request. The HTTP API and the WebSocket share this routing.

    Raise ValueError (400), PermissionError (403) or RuntimeError (409) for a refusal.
    """
    if state.mode == "robot":
        if kind == "heartbeat":
            if payload != {}:
                raise ValueError("Heartbeat must be an empty JSON object")
            return state.heartbeat(client)
        if kind == "keys":
            return state.keys(payload, client)
        return state.command(payload, client_id=client)
    if kind == "heartbeat":
        raise ValueError("Heartbeat is only available in robot mode")
    if kind == "keys":
        raise PermissionError("Keyboard control is unavailable in read-only mode")
    return state.command(payload)


def refusal_status(error):
    """Return the HTTP status for a refusal, or None for an unexpected failure."""
    if isinstance(error, ValueError):
        return 400
    if isinstance(error, PermissionError):
        return 403
    if isinstance(error, RuntimeError):
        return 409
    return None


def _host_port(value, default_port):
    try:
        parts = urlsplit("//" + value.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        return None
    return parts.hostname.lower(), port or default_port


def loopback_host(value):
    """Check that a Host header names this machine's loopback interface."""
    parts = _host_port(value, 80)
    if parts is None:
        return False
    try:
        return ipaddress.ip_address(parts[0]).is_loopback
    except ValueError:
        return parts[0] == "localhost"


def same_origin(headers):
    """Refuse a WebSocket opened by a page from another origin.

    Browsers always send Origin on a WebSocket handshake, and a page script cannot set
    Origin, Host or X-Forwarded-Host. Programs may omit Origin.
    """
    if headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
        return False
    origins = headers.get_all("Origin") or []
    if not origins:
        return True
    if len(origins) != 1:
        return False
    try:
        parts = urlsplit(origins[0].strip())
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path:
        return False
    default_port = 443 if parts.scheme == "https" else 80
    origin = (parts.hostname.lower(), port or default_port)
    hosts = [headers.get("Host", ""), headers.get("X-Forwarded-Host", "").split(",")[0]]
    return any(host.strip() and _host_port(host, default_port) == origin for host in hosts)


class ReadOnlyPanelState:
    """Read joint feedback without sending robot commands."""

    mode = "read-only"

    def __init__(self, robot_host, *, connection_factory=FeedbackConnection):
        if not robot_host:
            raise ValueError("Read-only mode requires a robot host")
        self.robot_host = robot_host
        self.syncing = False
        self.joints = None
        self.robot_mode = None
        self.robot_error = None
        self.received = None
        self.message = "Read-only mode. Start sync to read joint feedback."
        self._lock = threading.RLock()
        self._reader = None
        self._stop = threading.Event()
        self._connection_factory = connection_factory
        self._ownership = ControllerOwnership(robot_host)

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self.received if self.received is not None else None
            return {
                "mode": self.mode,
                "robot_host": self.robot_host,
                "enabled": None,
                "syncing": self.syncing,
                "actual": [None] * 5,
                "joints": list(self.joints) if self.joints is not None else None,
                "robot_mode": self.robot_mode,
                "robot_error": self.robot_error,
                "feedback_fresh": self.syncing and age is not None and age < 0.5,
                "message": self.message,
            }

    def command(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object")
        action = payload.get("action")
        if not isinstance(action, str) or action not in {
            "start_sync",
            "stop_sync",
            "enable",
            "disable",
            "home",
            "execute",
            "stop",
            "initialize_gripper",
            "keyboard",
            "clear_alarm",
        }:
            raise ValueError("Unknown action")
        if action == "keyboard":
            raise PermissionError("Keyboard control is unavailable in read-only mode")
        if set(payload) != ({"action", "targets"} if action == "execute" else {"action"}):
            raise ValueError("Unexpected command fields")
        with self._lock:
            if action == "start_sync":
                if not self.syncing:
                    if self._reader is not None and self._reader.is_alive():
                        raise RuntimeError("Feedback reader is stopping. Retry start sync.")
                    self._ownership.acquire()
                    self.syncing = True
                    self.received = None
                    self.message = "Connecting to the passive feedback stream."
                    self._stop = threading.Event()
                    try:
                        self._reader = threading.Thread(target=self._read_feedback, daemon=True)
                        self._reader.start()
                    except Exception:
                        self.syncing = False
                        self._reader = None
                        self._ownership.release()
                        raise
            elif action == "stop_sync":
                self._stop.set()
                self.syncing = False
                self.message = "Feedback sync stopped."
            else:
                raise PermissionError("Robot commands are unavailable in read-only mode")
            return self.snapshot()

    def _read_feedback(self):
        try:
            with self._connection_factory(self.robot_host, timeout_s=1.0) as connection:
                while not self._stop.is_set():
                    sample = connection.read_latest()
                    with self._lock:
                        if self._stop.is_set():
                            return
                        self.joints = sample.joints_deg
                        self.robot_mode = sample.mode
                        self.robot_error = sample.error_status
                        self.received = time.monotonic()
                        self.message = (
                            "Live joint feedback. Pose and gripper feedback are unavailable."
                        )
        except (OSError, ValueError) as error:
            with self._lock:
                if not self._stop.is_set():
                    self.syncing = False
                    self.message = f"Feedback unavailable: {error}"
        finally:
            with self._lock:
                self.syncing = False
                self._ownership.release()

    def close(self):
        self._stop.set()
        with self._lock:
            self.syncing = False
        if self._reader is not None:
            self._reader.join(timeout=2)


class SocketSession:
    """Serve one WebSocket: hello, state pushes, and one reply per request in order.

    The handler thread reads. Sends only queue frames for the connection's writer, so
    a peer that stops reading never delays the processing of its later requests.
    """

    HELLO_SECONDS = 3.0
    # Linux ends a connection whose sent data stays unacknowledged this long.
    UNACKNOWLEDGED_MS = 5000
    IDLE_SECONDS = 15.0
    PUSH_SECONDS = 0.05
    KEEPALIVE_SECONDS = 1.0
    PING_SECONDS = 5.0
    DRAIN_SECONDS = 2.0

    def __init__(self, server, sock, connection, log=None):
        self.server = server
        self.client = None
        self._log = log
        self._socket = sock
        self._connection = connection
        self._registered = False
        self._done = threading.Event()
        self._replied = threading.Event()
        self._pusher = None

    def run(self):
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            # Detect a vanished peer in seconds rather than at the 15 s read timeout.
            try:
                self._socket.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, self.UNACKNOWLEDGED_MS
                )
            except OSError:
                pass
        try:
            self.client = self._hello()
            if self.client is None:
                return
            self._socket.settimeout(self.IDLE_SECONDS)
            if not self.server.register_socket(self):
                self._refuse(1013, 503, "Too many open panel connections")
            self._registered = True
            self._pusher = threading.Thread(target=self._push, name="MG400-ws-push", daemon=True)
            self._pusher.start()
            while (text := self._connection.receive()) is not None:
                self._request(text)
        except ProtocolError as error:
            self._close(error.code, None, str(error))
        except (_Refused, EOFError, OSError, ConnectionClosed):
            pass
        except Exception as error:
            self.server.report_error(f"WebSocket session failed: {repr(error)[:200]}")
            self._close(1011, None, "The robot service failed")
        finally:
            self._done.set()
            # Revoke keys before the close handshake, which can take seconds.
            if self._registered:
                self.server.unregister_socket(self)
            self._finish()
            if self._pusher is not None:
                self._pusher.join(timeout=1)

    def _hello(self):
        # A slow sender must not hold a connection slot without authenticating.
        deadline = threading.Timer(self.HELLO_SECONDS, self._connection.abort)
        deadline.daemon = True
        self._socket.settimeout(self.HELLO_SECONDS)
        deadline.start()
        try:
            text = self._connection.receive()
        finally:
            deadline.cancel()
        if text is None:
            return None
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):
            message = None
        if (
            not isinstance(message, dict)
            or set(message) != {"type", "protocol", "client", "token"}
            or message["type"] != "hello"
            or type(message["protocol"]) is not int
            or not isinstance(message["token"], str)
            or not valid_client(message["client"])
        ):
            self._refuse(
                4400, 400, "Send hello with the protocol, a client ID and the access token first"
            )
        if message["protocol"] != PROTOCOL:
            self._refuse(4426, 426, f"The robot service uses protocol {PROTOCOL}. Reload the page.")
        expected = self.server.token
        # surrogatepass: JSON can carry a lone surrogate, which must compare as unequal.
        if expected and not hmac.compare_digest(
            message["token"].encode("utf-8", "surrogatepass"), expected.encode("utf-8")
        ):
            self._refuse(4401, 401, "Enter the panel access token")
        return message["client"]

    def _request(self, text):
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):
            message = None
        if (
            not isinstance(message, dict)
            or set(message) != {"type", "id", "body"}
            or not isinstance(message["type"], str)
            or message["type"] not in {"command", "heartbeat", "keys"}
            or type(message["id"]) is not int
            or not 0 <= message["id"] <= MAX_SEQUENCE
        ):
            self._refuse(4400, 400, "Send requests as JSON objects with type, id and body")
        reply = {"type": "reply", "id": message["id"]}
        try:
            reply["state"] = self.server.dispatch_live(self, message["type"], message["body"])
            reply["status"] = 200
        except _Replaced:
            # Requests still buffered on a replaced connection are stale.
            raise _Refused from None
        except Exception as error:
            status = refusal_status(error)
            if status is None:
                self.server.report_error(f"WebSocket {message['type']} request failed: {error!r}")
                status, error = 500, "The robot service failed to handle the request"
            reply.update(status=status, error=str(error))
        if message["type"] == "command" and self._log is not None:
            self._log_command(message["body"], reply)
        self._send(reply)
        # The reply may carry a state the pusher has not seen. Push the current one next.
        self._replied.set()

    def _log_command(self, body, reply):
        """Log robot commands like HTTP requests. Keys, heartbeats and pushes stay quiet."""
        action = body.get("action") if isinstance(body, dict) else None
        action = action if isinstance(action, str) and action.isidentifier() else "invalid"
        detail = f" {reply['error']}" if reply["status"] != 200 else ""
        # A short client prefix tells pages apart without a long ID in every line.
        self._log(
            '"WS command %s" %s client=%s%s', action, reply["status"], self.client[:8], detail
        )

    def _send(self, message):
        self._connection.send_text(json.dumps(message, allow_nan=False))

    def _push(self):
        """Push the state on change and at least once per second. Ping every 5 s."""
        last = None
        sent = pinged = time.monotonic()
        try:
            while not self._done.is_set():
                now = time.monotonic()
                # Skip a push while frames wait, so a slow reader gets the latest state later.
                if not self._connection.backlog:
                    # Clear only a flag that was seen, so a reply landing now gets the next push.
                    replied = self._replied.is_set()
                    if replied:
                        self._replied.clear()
                    snapshot = self.server.state.snapshot()
                    content = {key: value for key, value in snapshot.items() if key != "version"}
                    if replied or content != last or now - sent >= self.KEEPALIVE_SECONDS:
                        self._send({"type": "state", "state": snapshot})
                        last, sent = content, now
                if now - pinged >= self.PING_SECONDS:
                    self._connection.ping()
                    pinged = now
                self._done.wait(self.PUSH_SECONDS)
        except ConnectionClosed:
            pass
        except Exception as error:
            self.server.report_error(f"WebSocket push failed: {error!r}")
            self._connection.abort()

    def replace(self):
        """End this connection because the same client opened a newer one."""
        self._connection.close(4409, "Replaced by a newer connection")
        deadline = threading.Timer(self.DRAIN_SECONDS, self._connection.abort)
        deadline.daemon = True
        deadline.start()

    def evict(self):
        """End a connection that has not sent hello, to free its slot."""
        self._connection.abort()

    def stop_service(self):
        """Send the last state and a going-away close when the service stops."""
        try:
            self._send({"type": "state", "state": self.server.state.snapshot()})
        except Exception:
            pass
        self._connection.close(1001, "The robot service stopped")

    def wait_stopped(self, timeout):
        self._connection.close_written.wait(timeout)

    def _close(self, code, status, error):
        if status is not None:
            try:
                self._send({"type": "error", "status": status, "error": error})
            except (ConnectionClosed, ValueError):
                pass
        self._connection.close(code, error)

    def _refuse(self, code, status, error):
        self._close(code, status, error)
        raise _Refused

    def _finish(self):
        """Close politely, then shut the socket down before the handler closes it.

        The peer's close is awaited briefly, because closing with unread input can reset
        the connection and lose the close frame. The writer has ended when this returns,
        so it never writes to a closed and reused descriptor.
        """
        connection = self._connection
        connection.close(1000)
        if connection.aborted:
            connection.join()
            return
        deadline = threading.Timer(self.DRAIN_SECONDS, connection.abort)
        deadline.daemon = True
        deadline.start()
        try:
            connection.close_written.wait(self.DRAIN_SECONDS)
            if connection.close_received is None and connection.close_written.is_set():
                self._socket.settimeout(self.DRAIN_SECONDS)
                while connection.receive() is not None:
                    pass
        except Exception:
            pass
        finally:
            deadline.cancel()
            connection.abort()
            connection.join()


class _Refused(Exception):
    """The session queued its refusal and a close frame."""


class _Replaced(Exception):
    """A newer connection of the same client replaced this session."""


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    # Authenticated connections, and connections that have not sent hello yet.
    MAX_SOCKETS = 8
    MAX_PENDING_SOCKETS = 8

    def __init__(self, address, state, token=None, base_path="/"):
        self.state = state
        self.token = token
        self.base_path = normalize_base_path(base_path)
        self._sockets_lock = threading.Lock()
        self._pending = []
        self._sessions = {}
        super().__init__(address, PanelHandler)

    def admit_socket(self, session):
        """Track a connection before hello. Evict the oldest one when the queue is full.

        Eviction keeps unauthenticated peers from holding every slot.
        """
        with self._sockets_lock:
            evicted = self._pending[: max(0, len(self._pending) - self.MAX_PENDING_SOCKETS + 1)]
            del self._pending[: len(evicted)]
            self._pending.append(session)
        for old in evicted:
            old.evict()

    def discharge_socket(self, session):
        with self._sockets_lock:
            if session in self._pending:
                self._pending.remove(session)

    def register_socket(self, session):
        """Make an authenticated session the live connection of its client.

        A newer connection of the same client revokes the keys at once and ends the older
        connection, whose own end then revokes nothing. Return False when every slot is
        taken.
        """
        with self._sockets_lock:
            if session in self._pending:
                self._pending.remove(session)
            old = self._sessions.get(session.client)
            if old is None and len(self._sessions) >= self.MAX_SOCKETS:
                return False
            if old is not None:
                # The page lost the older connection, so input armed through it ends now.
                self._connection_lost(session.client)
            self._sessions[session.client] = session
        if old is not None:
            old.replace()
        return True

    def dispatch_live(self, session, kind, payload):
        """Dispatch only while session is its client's live connection.

        The registry lock makes the check and the dispatch atomic with a replacement,
        so a request from a replaced connection cannot arm keys after the revocation.
        """
        with self._sockets_lock:
            if self._sessions.get(session.client) is not session:
                raise _Replaced
            return dispatch(self.state, kind, payload, session.client)

    def unregister_socket(self, session):
        """Revoke the keys of a client whose live connection ended.

        The registry lock stays held, so a newer connection of the same client cannot
        register and arm keys before this revocation.
        """
        with self._sockets_lock:
            if self._sessions.get(session.client) is not session:
                return
            del self._sessions[session.client]
            self._connection_lost(session.client)

    def _connection_lost(self, client):
        # Lock order: the registry lock, then the state lock. connection_lost never blocks.
        connection_lost = getattr(self.state, "connection_lost", None)
        if connection_lost is not None:
            try:
                connection_lost(client)
            except Exception as error:
                self.report_error(f"Keyboard revocation after a lost connection failed: {error!r}")

    def stop_sockets(self, timeout=1.0):
        """Send each page the final state and a 1001 close. Wait at most timeout in total.

        The writers are daemon threads, so the process must not exit before they write.
        """
        with self._sockets_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop_service()
        deadline = time.monotonic() + timeout
        for session in sessions:
            session.wait_stopped(max(0.0, deadline - time.monotonic()))

    def report_error(self, text):
        # A blocked stderr must not hold a connection or the registry lock.
        report(text, wait=0)


class PanelHandler(BaseHTTPRequestHandler):
    # Keep-alive avoids a new TCP connection for each key update. Without Nagle,
    # small responses do not wait for the client's delayed acknowledgement.
    protocol_version = "HTTP/1.1"
    timeout = 15
    disable_nagle_algorithm = True

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=()):
        data = json.dumps(body, allow_nan=False).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        if status >= 400:
            # An early error can leave the request body unread on this connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _host_allowed(self):
        """Without a token, accept only loopback host names.

        A DNS rebinding page reaches a loopback server under its own host name, so its
        requests look same-origin. A token protects the other binds.
        """
        if self.server.token:
            return True
        # A proxy on this host can rewrite Host to a loopback name. Proxies need a token.
        forwarded = any(
            self.headers.get(name) is not None
            for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")
        )
        if not forwarded and loopback_host(self.headers.get("Host", "")):
            return True
        self._send(403, {"error": "Open the panel at 127.0.0.1 or localhost"})
        return False

    def _authorized(self):
        expected = self.server.token
        if expected and not hmac.compare_digest(
            self.headers.get("Authorization", "").encode("latin-1", "replace"),
            f"Bearer {expected}".encode("latin-1", "replace"),
        ):
            self._send(401, {"error": "Enter the panel access token"})
            return False
        return True

    def do_GET(self):
        # An unread body would be parsed as the next request on this connection.
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") is not None or length not in (None, "0"):
            self._send(400, {"error": "GET requests must not have a body"})
            return
        if not self._host_allowed():
            return
        path = urlsplit(self.path).path
        base = self.server.base_path.rstrip("/")
        if base and path == base:
            self.send_response(308)
            self.send_header("Location", self.server.base_path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = path[len(base) :] if path.startswith(base + "/") else None
        if path in {"/", "/index.html"}:
            self._send(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if self._authorized():
                self._send(200, self.server.state.snapshot())
        elif path == "/api/ws":
            self._serve_websocket()
        else:
            self._send(404, {"error": "Not found"})

    def _header_tokens(self, name):
        values = ",".join(self.headers.get_all(name) or [])
        return {token.strip().lower() for token in values.split(",")}

    def _serve_websocket(self):
        upgrade = (("Upgrade", "websocket"), ("Sec-WebSocket-Version", "13"))
        if (
            self.request_version != "HTTP/1.1"
            or "websocket" not in self._header_tokens("Upgrade")
            or "upgrade" not in self._header_tokens("Connection")
            or self.headers.get("Sec-WebSocket-Version", "").strip() != "13"
        ):
            self._send(426, {"error": "Open api/ws as a version 13 WebSocket"}, headers=upgrade)
            return
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if len(self.headers.get_all("Sec-WebSocket-Key") or []) != 1 or not valid_client_key(key):
            self._send(400, {"error": "The WebSocket key is invalid"})
            return
        if not same_origin(self.headers):
            self._send(403, {"error": "Open the panel page from this server"})
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        # The connection now carries WebSocket frames, never another HTTP request.
        self.close_connection = True
        session = SocketSession(
            self.server,
            self.connection,
            Connection(self.connection, self.rfile, self.wfile),
            log=self.log_message,
        )
        self.server.admit_socket(session)
        try:
            session.run()
        finally:
            self.server.discharge_socket(session)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == self.server.base_path + "api/keys":
            # A page loaded before a service update still posts key updates here.
            self._send(404, {"error": "The robot service was updated. Reload the page."})
            return
        if path not in {
            self.server.base_path + "api/command",
            self.server.base_path + "api/heartbeat",
        }:
            self._send(404, {"error": "Not found"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send(400, {"error": "Send the request body with Content-Length"})
            return
        if not self._host_allowed():
            return
        if not self._authorized():
            return
        if (
            self.headers.get("Content-Type", "").split(";")[0] != "application/json"
            or self.headers.get("X-MG400-Panel") != "1"
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            self._send(403, {"error": "Use the panel to send commands"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request body must contain 1 to 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            state = self.server.state
            client = None
            if state.mode == "robot":
                client = self.headers.get("X-MG400-Client", "")
                if not client or len(client) > 128 or not client.isascii():
                    raise ValueError("Open the panel to identify this browser")
            kind = "heartbeat" if path.endswith("/heartbeat") else "command"
            result = dispatch(state, kind, payload, client)
        except (ValueError, PermissionError, RuntimeError) as error:
            self._send(refusal_status(error), {"error": str(error)})
        else:
            self._send(200, result)

    def log_message(self, format, *args):
        # Keep tokens and request bodies out of the request log.
        if format.startswith("Request timed out"):
            return
        polls = (
            f"GET {self.server.base_path}api/state ",
            f"POST {self.server.base_path}api/heartbeat ",
        )
        if (
            len(args) > 1
            and isinstance(args[0], str)
            and args[0].startswith(polls)
            and (str(args[1]) == "200")
        ):
            return
        super().log_message(format, *args)


def report(text, *, wait=None):
    """Write a message to stderr if it works. Output must never skip or delay a stop.

    With wait, write on a helper thread and continue after at most wait seconds.
    """

    def write():
        try:
            print(text, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    if wait is None:
        write()
        return
    try:
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(wait)
    except RuntimeError:
        pass


def start_event_log():
    """Write robot service events to stderr on a helper thread.

    Events are logged under the state lock, so a blocked stderr must not delay a stop.
    Return a function that flushes for at most wait seconds and removes the handler.
    """
    events = queue.SimpleQueue()
    output = logging.StreamHandler(sys.stderr)
    output.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%d/%b/%Y %H:%M:%S")
    )
    listener = logging.handlers.QueueListener(events, output)
    handler = logging.handlers.QueueHandler(events)
    logger = logging.getLogger("mg400")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    listener.start()

    def stop(wait):
        logger.removeHandler(handler)
        logger.setLevel(level)
        try:
            stopper = threading.Thread(target=listener.stop, daemon=True)
            stopper.start()
            stopper.join(wait)
        except RuntimeError:
            pass

    return stop


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", help="HTTP address (default: MG400_PANEL_BIND or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default: MG400_PANEL_PORT or 8766)")
    parser.add_argument("--mode", choices=("robot", "read-only"), default="robot")
    parser.add_argument("--base-path", type=normalize_base_path, default="/")
    parser.add_argument(
        "--robot-host", help="Robot address. Default: MG400_ROBOT_HOST, then config/robot.toml"
    )
    parser.add_argument(
        "--gripper-feedback",
        type=Path,
        help="Feedback file from a --commands USB collector on this server's host",
    )
    args = parser.parse_args(argv)
    # An empty environment value counts as unset.
    bind = args.bind if args.bind is not None else os.environ.get("MG400_PANEL_BIND") or "127.0.0.1"
    port = args.port
    if port is None:
        value = os.environ.get("MG400_PANEL_PORT") or "8766"
        port = int(value) if re.fullmatch("[0-9]{1,5}", value) else 0
        if not 1 <= port <= 65535:
            parser.error("MG400_PANEL_PORT must be a whole number between 1 and 65535")
    token = os.environ.get("MG400_PANEL_TOKEN")
    if token and not valid_token(token):
        parser.error(
            "MG400_PANEL_TOKEN must contain only printable ASCII characters "
            "and must not begin or end with a space"
        )
    try:
        loopback = ipaddress.ip_address(bind).is_loopback
    except ValueError:
        loopback = bind == "localhost"
    if not loopback and (not token or len(token) < 24):
        parser.error(
            "Set MG400_PANEL_TOKEN to at least 24 characters before binding outside localhost"
        )
    host = args.robot_host
    if args.gripper_feedback and args.mode != "robot":
        parser.error("--gripper-feedback requires --mode robot")
    if not host:
        host = load_robot_host(ROOT / "config/robot.toml")
    state = (
        RobotPanelState(
            host,
            gripper=WebGripper(args.gripper_feedback) if args.gripper_feedback else None,
        )
        if args.mode == "robot"
        else ReadOnlyPanelState(host)
    )
    server = PanelServer((bind, port), state, token, args.base_path)
    print(
        f"MG400 panel: http://{bind}:{server.server_port}{args.base_path} ({args.mode})",
        flush=True,
    )
    stop_event_log = start_event_log()
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}

    def interrupted(signum, frame):
        # Repeated signals must not interrupt the bounded stop attempt.
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt

    # Ctrl+C, systemctl stop and a closed SSH session must all stop a running jog.
    for signum in signals:
        if previous[signum] != signal.SIG_IGN:
            signal.signal(signum, interrupted)
    exit_code = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for signum in signals:
            signal.signal(signum, signal.SIG_IGN)
        try:
            # A hung-up terminal or a closed pipe must not prevent the stop.
            report("Stopping robot control before exit.", wait=0.2)
            try:
                server.server_close()
            finally:
                state.close()
            snapshot = state.snapshot()
            report(snapshot.get("message", ""))
            # Open pages show the final state, then a closed connection.
            server.stop_sockets()
            if snapshot.get("stop_unconfirmed") or snapshot.get("busy"):
                exit_code = 1
                report("Robot rest is unconfirmed. Use the physical emergency stop.")
        except Exception as error:
            exit_code = 1
            report(f"Cleanup failed: {error}. Use the physical emergency stop.")
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            stop_event_log(0.2)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
