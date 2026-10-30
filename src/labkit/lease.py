"""Thread-safe control leases. The service calls poll from its own poll loop."""

import math
import threading
import time
from contextlib import contextmanager

from .errors import ServiceError


def validate_client(client):
    """Require the contract's printable ASCII client identifier."""
    if not isinstance(client, str) or not 1 <= len(client) <= 128:
        raise ValueError("Expected a client ID with 1 to 128 printable ASCII characters.")
    if any(not " " <= char <= "~" for char in client):
        raise ValueError("Expected a client ID with 1 to 128 printable ASCII characters.")
    return client


class Lease:
    """Own one client lease without a timer thread.

    Call poll even when there are no HTTP requests. Expiry callbacks run outside
    the lease lock. Acquisition stays blocked until the callback finishes. A
    callback failure blocks further control and is not retried. Replace the lease
    only after the service's recovery procedure resolves that failure.
    """

    def __init__(self, seconds=3, clock=time.monotonic, *, on_expire=None):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("Lease duration must be a finite positive number.")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Lease duration must be a finite positive number.")
        self.seconds = seconds
        self.clock = clock
        self.on_expire = on_expire
        self._lock = threading.RLock()
        self._holder = None
        self._deadline = None
        self._expired = False
        self._expiring = False
        self._expiry_failed = False

    def _due(self):
        return self._holder is not None and self.clock() >= self._deadline

    @property
    def holder(self):
        self.poll()
        with self._lock:
            return self._holder

    @property
    def expired(self):
        with self._lock:
            return self._expired or self._due()

    def poll(self):
        """Call the expiry callback once per expired lease; return whether it ran."""
        with self._lock:
            if not self._due():
                return False
            self._holder = self._deadline = None
            self._expired = self._expiring = True
            callback = self.on_expire
        try:
            if callback is not None:
                callback()
        except BaseException:
            with self._lock:
                self._expiry_failed = True
            raise
        finally:
            with self._lock:
                self._expiring = False
        return True

    def _available(self):
        if self._expiring:
            raise ServiceError(409, "Lease expiry is still in progress.")
        if self._expiry_failed:
            raise ServiceError(409, "Lease expiry failed. Check the service before taking control.")

    def _snapshot(self):
        return {
            "holder": self._holder,
            "remaining_s": max(0, self._deadline - self.clock()) if self._holder else 0,
        }

    def snapshot(self):
        """Read lease state without running a callback; stop responses use this path."""
        with self._lock:
            return self._snapshot()

    def acquire(self, client):
        validate_client(client)
        while True:
            self.poll()
            with self._lock:
                if self._due():
                    continue
                self._available()
                if self._holder not in (None, client):
                    raise ServiceError(409, "Another client holds the control lease.")
                self._holder = client
                self._deadline = self.clock() + self.seconds
                self._expired = False
                return self._snapshot()

    def renew(self, client):
        validate_client(client)
        with self.control(client):
            self._deadline = self.clock() + self.seconds
            return self._snapshot()

    def release(self, client):
        validate_client(client)
        while True:
            self.poll()
            with self._lock:
                if self._due():
                    continue
                self._available()
                if self._holder not in (None, client):
                    raise ServiceError(409, "Another client holds the control lease.")
                self._holder = self._deadline = None
                return self._snapshot()

    @contextmanager
    def control(self, client, *, require_holder=True):
        """Keep lease authorization atomic through a short command dispatch.

        Set require_holder=False only for a command declared any_client. Expiry
        transitions and failures still block that command.
        Do not wait for physical command completion in this context. Service
        polling must call poll before taking the device lock.
        """
        validate_client(client)
        while True:
            self.poll()
            with self._lock:
                if self._due():
                    continue
                self._available()
                if require_holder and self._holder != client:
                    raise ServiceError(409, "Acquire the control lease before sending a command.")
                yield
                return
