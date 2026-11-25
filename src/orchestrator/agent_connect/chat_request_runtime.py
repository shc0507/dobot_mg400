"""Cancel model HTTP requests and bound the local wait for their completion."""

import http.client
import socket
import threading
import urllib.request
from time import monotonic

from labkit.errors import ServiceError


class ModelRequestCancelled(ServiceError):
    def __init__(self):
        super().__init__(409, "The model request was cancelled. No command was sent.")


def check_cancelled(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise ModelRequestCancelled()


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


class RequestControl:
    """Close sockets on cancellation, including a socket waiting for HTTP headers."""

    def __init__(self, cancelled, timeout_s):
        self.cancelled = cancelled
        self.deadline = monotonic() + timeout_s
        self._lock = threading.Lock()
        self._stopped = False
        self._sockets = []

    def check(self):
        check_cancelled(self.cancelled)
        if self._stopped or monotonic() >= self.deadline:
            raise TimeoutError("The model request exceeded its deadline.")

    def attach(self, connection):
        with self._lock:
            stopped = self._stopped
            if not stopped:
                self._sockets.append(connection)
        if stopped:
            _shutdown(connection)
        self.check()

    def abort(self):
        with self._lock:
            self._stopped = True
            connections, self._sockets = self._sockets, []
        for connection in connections:
            _shutdown(connection)

    def run(self, operation):
        """Return promptly if DNS or an injected transport cannot be interrupted."""
        self.check()
        done, result, errors = threading.Event(), [], []

        def worker():
            try:
                self.check()
                result.append(operation())
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        threading.Thread(target=worker, name="portal-model-request", daemon=True).start()
        try:
            while not done.wait(0.05):
                self.check()
            self.check()
        except (ModelRequestCancelled, TimeoutError):
            self.abort()
            raise
        if errors:
            raise errors[0]
        return result[0]


class _TrackedConnection:
    def __init__(self, *args, control, **kwargs):
        super().__init__(*args, **kwargs)
        self._control = control
        create_connection = self._create_connection

        def connect(*args, **kwargs):
            control.check()
            connection = create_connection(*args, **kwargs)
            control.attach(connection)
            return connection

        self._create_connection = connect

    def send(self, data):
        self._control.check()
        return super().send(data)


class _HTTPConnection(_TrackedConnection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_TrackedConnection, http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        self._control.check()
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False,
        )
        self._control.attach(self.sock)
        self.sock.do_handshake()
        self._control.check()


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def http_open(self, req):
        return self.do_open(_HTTPConnection, req, control=self.control)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, control=self.control, context=self._context)


def request_handlers(control):
    return _HTTPHandler(control), _HTTPSHandler(control)
