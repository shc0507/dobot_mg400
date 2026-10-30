"""Standard-library robot service client. Lost requests are never replayed."""

import base64
import copy
import http.client
import json
import math
import os
import socket
import ssl
import threading
import time
from contextlib import nullcontext
from urllib.parse import urlsplit

from labkit.auth import valid_token_for_bind
from labkit.lease import validate_client

from .websocket import PONG, TEXT, MessageReader, ProtocolError, accept_key, encode_frame


def _connect_socket(address, timeout):
    return socket.create_connection(address, timeout)


class RobotClientError(RuntimeError):
    """A refusal, connection failure, or uncertain command outcome."""

    def __init__(self, message, *, status=None, close_code=None):
        super().__init__(message)
        self.status = status
        self.close_code = close_code


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timeout must be a finite positive number.")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("Timeout must be a finite positive number.") from None
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Timeout must be a finite positive number.")
    return result


class RobotClient:
    """Connect to a panel base URL with an explicit, unique client identity.

    Construction opens nothing. connect() starts the receive and heartbeat threads.
    close() ends them without releasing control; the service lease then expires.
    Context exit requests stop_sync first unless keep_control is true.
    """

    PROTOCOL = 1
    HEARTBEAT_SECONDS = 0.5
    RECONNECT_SECONDS = 2.5

    def __init__(self, url, token, client_id, *, timeout=5, keep_control=False):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https", "ws", "wss"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(ord(char) < 33 or ord(char) > 126 for char in url)
            or "\\" in parts.path
            or any(part in {".", ".."} for part in parts.path.split("/"))
        ):
            raise ValueError("Expected a service base URL without credentials, query, or fragment.")
        if not isinstance(token, str) or not valid_token_for_bind(
            token, parts.hostname, require_long_token=False
        ):
            raise ValueError("Invalid robot service token.")
        self.client_id = validate_client(client_id)
        if not self.client_id.strip():
            raise ValueError("Client identity must not be blank.")
        self.token = token
        self.timeout = _positive(timeout)
        self.keep_control = keep_control
        self._secure = parts.scheme in {"https", "wss"}
        self._host = parts.hostname
        self._port = parts.port or (443 if self._secure else 80)
        self._authority = parts.netloc
        self._path = parts.path.rstrip("/") + "/api/"
        self._changed = threading.Condition(threading.RLock())
        self._send_lock = threading.Lock()
        self._command_lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._done = threading.Event()
        self._socket = None
        self._state = None
        self._error = None
        self._pending = {}
        self._sequence = 0
        self._last_message_at = 0.0
        self._reader_thread = self._heartbeat_thread = None

    @property
    def state(self):
        """Return an independent copy of the latest state, or None before connection."""
        with self._changed:
            return copy.deepcopy(self._state)

    @property
    def connected(self):
        with self._changed:
            return self._socket is not None

    def _dial(self, timeout):
        sock = _connect_socket((self._host, self._port), timeout)
        try:
            if self._secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self._host)
            return sock
        except BaseException:
            sock.close()
            raise

    def _http(self, route, body=None, *, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        data = b"" if body is None else json.dumps(body, allow_nan=False).encode()
        headers = [
            f"{'GET' if body is None else 'POST'} {self._path}{route} HTTP/1.1",
            f"Host: {self._authority}",
            "Connection: close",
            "Accept: application/json",
        ]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        if body is not None:
            headers += [
                "Content-Type: application/json",
                "X-MG400-Panel: 1",
                f"X-MG400-Client: {self.client_id}",
                f"Content-Length: {len(data)}",
            ]
        try:
            with self._dial(timeout) as sock:
                sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + data)
                with http.client.HTTPResponse(sock) as response:
                    response.begin()
                    payload = response.read(1_048_577)
                    if len(payload) > 1_048_576:
                        raise RobotClientError("The HTTP reply is too large.")
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError("Expected a JSON object")
                    if response.status != 200:
                        raise RobotClientError(
                            value.get("error", "The HTTP request was refused."),
                            status=response.status,
                        )
                    return value
        except (OSError, EOFError, ValueError, http.client.HTTPException) as error:
            raise RobotClientError(
                f"HTTP request failed; outcome may be unknown: {error}"
            ) from error

    def _read_exact(self, sock, count, deadline=None):
        data = bytearray()
        expires = time.monotonic() + 2 if deadline is None else deadline
        while len(data) < count:
            if self._done.is_set():
                raise EOFError("The client closed.")
            if time.monotonic() >= expires:
                raise TimeoutError("The service stopped sending data.")
            try:
                part = sock.recv(count - len(data))
            except TimeoutError:
                continue
            if not part:
                raise EOFError("The service closed the connection.")
            data.extend(part)
        return bytes(data)

    def _message(self, sock, reader):
        while True:
            kind, *parts = reader.next()
            if kind == "text":
                value = json.loads(parts[0])
                if not isinstance(value, dict):
                    raise RobotClientError("The service message must be a JSON object.")
                return value
            if kind == "ping":
                self._write(sock, PONG, parts[0])
            elif kind == "close":
                code, reason = parts
                raise RobotClientError(reason or "The service closed.", close_code=code)

    def _write(self, sock, opcode, data):
        with self._send_lock:
            sock.sendall(encode_frame(opcode, data, mask=True))

    def _open(self, deadline):
        sock = self._dial(min(0.5, max(0.01, deadline - time.monotonic())))
        try:
            sock.settimeout(0.2)
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET {self._path}ws HTTP/1.1\r\nHost: {self._authority}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            head = bytearray()
            while not head.endswith(b"\r\n\r\n"):
                if len(head) >= 16_384:
                    raise RobotClientError("The WebSocket upgrade header is too large.")
                head.extend(self._read_exact(sock, 1, deadline))
            first, *lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines:
                if line:
                    name, separator, value = line.partition(":")
                    if not separator or name.lower() in headers:
                        raise RobotClientError("Invalid WebSocket upgrade headers.")
                    headers[name.lower()] = value.strip()
            if (
                first.split()[:2] != ["HTTP/1.1", "101"]
                or headers.get("sec-websocket-accept") != accept_key(key)
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade"
                not in {value.strip() for value in headers.get("connection", "").lower().split(",")}
                or "sec-websocket-extensions" in headers
                or "sec-websocket-protocol" in headers
            ):
                raise RobotClientError("The service refused the WebSocket upgrade.")
            self._write(
                sock,
                TEXT,
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": self.PROTOCOL,
                        "client": self.client_id,
                        "token": self.token,
                    }
                ).encode(),
            )
            reader = MessageReader(
                lambda n: self._read_exact(sock, n, deadline), masked=False, max_bytes=1_048_576
            )
            first = self._message(sock, reader)
            if first.get("type") == "error":
                try:
                    self._message(sock, reader)
                except RobotClientError as error:
                    error.status = first.get("status")
                    raise
                raise RobotClientError(first.get("error", "Hello was refused."))
            if first.get("type") != "state" or not isinstance(first.get("state"), dict):
                raise RobotClientError("The service did not send its initial state.")
            self._check_contract(max(0.01, deadline - time.monotonic()))
            reader = MessageReader(
                lambda n: self._read_exact(sock, n), masked=False, max_bytes=1_048_576
            )
            return sock, reader, first["state"]
        except BaseException:
            sock.close()
            raise

    def _check_contract(self, timeout):
        description = self._http("describe", timeout=timeout)
        if type(description.get("contract")) is not int or description["contract"] != 1:
            raise RobotClientError("Unsupported robot service contract.", status=409)

    def connect(self):
        """Open the service connection and start its workers. Return this client."""
        with self._lifecycle:
            if self.connected:
                return self
            if self._reader_thread is not None or self._heartbeat_thread is not None:
                raise RobotClientError("Close the previous client session before reconnecting.")
            self._done.clear()
            try:
                sock, reader, state = self._open(time.monotonic() + self.timeout)
            except (OSError, EOFError, ValueError, ProtocolError) as error:
                raise RobotClientError(f"Connection failed: {error}") from error
            with self._changed:
                self._socket, self._state, self._error = sock, state, None
                self._last_message_at = time.monotonic()
            self._reader_thread = threading.Thread(
                target=self._receive, args=(sock, reader), name="RobotClient-receive", daemon=True
            )
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeats, name="RobotClient-heartbeat", daemon=True
            )
            try:
                self._reader_thread.start()
                self._heartbeat_thread.start()
            except BaseException:
                self._done.set()
                self._lost(sock, RobotClientError("Client worker startup failed."))
                self._join_workers()
                raise
            return self

    def _update(self, state):
        if not isinstance(state, dict):
            raise RobotClientError("The service state must be a JSON object.")
        # State pushes and replies can race. Versions are comparable within an instance.
        if (
            self._state is None
            or state.get("instance") != self._state.get("instance")
            or state.get("version", 0) >= self._state.get("version", 0)
        ):
            self._state = state
        self._changed.notify_all()

    def _lost(self, sock, error):
        with self._changed:
            if self._socket is sock:
                self._socket = None
            for entry in self._pending.values():
                if "reply" not in entry:
                    entry["error"] = RobotClientError(
                        "Connection lost; command delivery or completion may be unknown."
                    )
            self._error = error
            self._changed.notify_all()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _receive(self, sock, reader):
        while not self._done.is_set():
            try:
                message = self._message(sock, reader)
                with self._changed:
                    if self._socket is not sock or self._done.is_set():
                        raise EOFError("The client discarded this connection.")
                    self._last_message_at = time.monotonic()
                    if message.get("type") == "state":
                        self._update(message.get("state"))
                    elif message.get("type") == "reply":
                        entry = self._pending.get(message.get("id"))
                        if entry is not None and "error" not in entry:
                            entry["reply"] = message
                        if "state" in message:
                            self._update(message["state"])
                        self._changed.notify_all()
                    else:
                        raise RobotClientError(message.get("error", "Unexpected service message."))
            except (OSError, EOFError, ValueError, ProtocolError, RobotClientError) as error:
                self._lost(sock, error)
                if isinstance(error, (ProtocolError, RobotClientError)):
                    break
                # Detection time consumes the same budget as reconnect attempts.
                # A silent connection must not gain another full lease window.
                deadline = self._last_message_at + self.RECONNECT_SECONDS
                while not self._done.is_set() and time.monotonic() < deadline:
                    try:
                        sock, reader, state = self._open(deadline)
                        with self._changed:
                            if self._done.is_set():
                                sock.close()
                                return
                            self._socket, self._error = sock, None
                            self._last_message_at = time.monotonic()
                            self._update(state)
                        break
                    except (OSError, EOFError, ValueError) as retry_error:
                        with self._changed:
                            self._error = retry_error
                        self._done.wait(min(0.1, max(0, deadline - time.monotonic())))
                    except (ProtocolError, RobotClientError) as retry_error:
                        with self._changed:
                            self._error = retry_error
                        return
                else:
                    break
        with self._changed:
            self._changed.notify_all()

    def _request(self, kind, body, timeout):
        encoded_body = json.dumps(body, allow_nan=False)
        deadline = time.monotonic() + timeout
        with self._changed:
            sock = self._socket
            if sock is None or self._done.is_set():
                raise RobotClientError("The robot service socket is unavailable.")
            self._sequence += 1
            request_id = self._sequence
            entry = {}
            self._pending[request_id] = entry
        payload = (
            f'{{"type":{json.dumps(kind)},"id":{request_id},"body":{encoded_body}}}'
        ).encode()
        try:
            if len(payload) > 4096 or request_id > 2**53:
                raise ValueError("The robot service request exceeds the protocol limit.")
            try:
                self._write(sock, TEXT, payload)
            except OSError as error:
                self._lost(sock, error)
                raise RobotClientError(
                    "Request delivery is unknown; it was not retried."
                ) from error
            with self._changed:
                while "reply" not in entry and "error" not in entry:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Reply timed out; command delivery may be unknown.")
                    self._changed.wait(remaining)
                if "error" in entry:
                    raise entry["error"]
                return copy.deepcopy(entry["reply"])
        finally:
            with self._changed:
                self._pending.pop(request_id, None)

    def _heartbeats(self):
        while not self._done.wait(self.HEARTBEAT_SECONDS):
            with self._changed:
                holds = (
                    self._state is not None and self._state.get("controller_id") == self.client_id
                )
                available = self._socket is not None
            if holds and available:
                try:
                    self._request("heartbeat", {}, self.HEARTBEAT_SECONDS)
                except (RobotClientError, OSError):
                    pass

    def command(self, action, **fields):
        """Send once and return the protocol reply. Only stop/disable use HTTP fallback."""
        # Safety commands can interrupt a run that holds the ordinary command lock.
        if action in {"stop", "disable"}:
            return self._command(action, fields, self.timeout)
        with self._command_lock:
            return self._command(action, fields, self.timeout)

    def _command(self, action, fields, timeout):
        if not isinstance(action, str):
            raise ValueError("Action must be a string.")
        if action in {"stop", "disable"} and not self.connected:
            deadline = time.monotonic() + timeout
            self._check_contract(timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Contract check timed out before the safety request.")
            state = self._http("command", {"action": action, **fields}, timeout=remaining)
            with self._changed:
                self._update(state)
            return {"type": "reply", "id": None, "status": 200, "state": copy.deepcopy(state)}
        return self._request("command", {"action": action, **fields}, timeout)

    def wait(self, predicate, timeout):
        """Return the latest state when predicate(state) is true; raise on timeout."""
        deadline = time.monotonic() + _positive(timeout)
        with self._changed:
            while True:
                state = copy.deepcopy(self._state)
                if state is not None and predicate(state):
                    return state
                if self._done.is_set():
                    raise RobotClientError("The client is closed.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for robot state.")
                self._changed.wait(remaining)

    def run(self, action, timeout=30, **fields):
        """Send once, then await this action's successful result and idle state.

        A timeout or lost reply is uncertain. Neither condition retries the command.
        Safety commands may interrupt a run from another thread.
        """
        timeout = _positive(timeout)
        guard = nullcontext() if action in {"stop", "disable"} else self._command_lock
        with guard:
            before = self.state
            if before is None:
                raise RobotClientError("Connect before running a command.")
            baseline = (before.get("last_command") or {}).get("id")
            instance = before.get("instance")
            deadline = time.monotonic() + timeout
            reply = self._command(action, fields, min(self.timeout, timeout))
            if reply.get("status") != 200:
                raise RobotClientError(
                    reply.get("error", "Command refused."), status=reply.get("status")
                )

            accepted = reply["state"]
            if (
                accepted.get("busy")
                or (action == "start_sync" and not accepted.get("feedback_fresh"))
                or (action in {"stop", "disable", "stop_sync"} and accepted.get("gripper_busy"))
            ):
                # The accepted snapshot still describes a previous completion.
                baseline = (accepted.get("last_command") or {}).get("id")

            def complete(state):
                if state.get("instance") != instance:
                    raise RobotClientError("The service restarted; command completion is unknown.")
                result = state.get("last_command") or {}
                if result.get("id") == baseline or not result:
                    return False
                matches = result.get("action") == action and result.get("client") == self.client_id
                if matches:
                    success = (
                        {"ok", "stopped"} if action in {"stop", "disable", "stop_sync"} else {"ok"}
                    )
                    if result.get("result") not in success:
                        raise RobotClientError(result.get("message", "Command did not complete."))
                    if state.get("stop_unconfirmed") or (
                        state.get("locked") and action not in {"stop", "disable", "stop_sync"}
                    ):
                        raise RobotClientError("The service is locked; completion is not safe.")
                    if action == "start_sync" and not state.get("feedback_fresh"):
                        return False
                    if action in {"stop", "disable", "stop_sync"} and state.get("gripper_busy"):
                        return False
                    return state.get("busy") is False
                if (
                    result.get("action") in {"stop", "disable", "stop_sync"}
                    and result.get("result") != "rejected"
                ):
                    raise RobotClientError("A stop superseded the command; completion is unknown.")
                return False

            # A fast completion may be present only in its reply before another state arrives.
            if complete(reply["state"]):
                return copy.deepcopy(reply["state"])
            return self.wait(complete, max(0.000001, deadline - time.monotonic()))

    def close(self):
        """Stop client workers. Do not renew or release the service's control lease."""
        with self._lifecycle:
            self._done.set()
            with self._changed:
                sock = self._socket
            if sock is not None:
                self._lost(sock, RobotClientError("The client closed."))
            with self._changed:
                self._changed.notify_all()
            self._join_workers()

    def _join_workers(self):
        for name in ("_reader_thread", "_heartbeat_thread"):
            worker = getattr(self, name)
            if worker is None:
                continue
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(self.timeout + self.RECONNECT_SECONDS + 1)
            # A bounded join must not discard the only handle to a live worker.
            if not worker.is_alive():
                setattr(self, name, None)

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, traceback):
        try:
            state = self.state
            if not self.keep_control and state and state.get("controller_id") == self.client_id:
                self.run("stop_sync", timeout=self.timeout)
        except (RobotClientError, OSError):
            if exc_type is None:
                raise
        finally:
            self.close()
