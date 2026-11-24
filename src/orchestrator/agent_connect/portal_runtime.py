"""Keep an explicit PC agent connection active without a browser page."""

import copy
import json
import threading
import time

from labkit.errors import ServiceError

from .live import STATE

HEARTBEAT_INTERVAL_S = 2
READ_INTERVAL_S = 1
SESSION_TIMEOUT_S = 10
MAX_STATE_BYTES = 262144


class PortalSession:
    """Own one connection generation. Never connect, grant control, or send commands."""

    def __init__(self, source, connection, transport, reader=None):
        self.source, self.transport, self.reader = source, transport, reader
        self.token = connection["session_token"]
        self.session_id = connection.get("session_id")
        self.boot_id = connection.get("state", {}).get("boot_id")
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.last_heartbeat_at = None
        self.last_heartbeat = time.monotonic()
        self.error = None
        self.latest = None
        self.received_at = None
        self.read_error = None
        self.threads = []

    def start(self):
        targets = [self._heartbeat]
        if self.reader is not None:
            targets.append(self._collect)
        for target in targets:
            thread = threading.Thread(
                target=target, daemon=True, name="agent-portal" + target.__name__
            )
            self.threads.append(thread)
            thread.start()

    def stop(self, error=None):
        with self.lock:
            if error and not self.stopped.is_set():
                self.error = error
            self.stopped.set()

    def join(self):
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=4)

    def _heartbeat(self):
        while not self.stopped.wait(HEARTBEAT_INTERVAL_S):
            try:
                state = self.transport(self.source, "heartbeat", {}, token=self.token)
                principal = state.get("principal")
                if (
                    not isinstance(principal, dict)
                    or principal.get("kind") != "agent"
                    or (self.session_id and principal.get("id") != self.session_id)
                    or (self.boot_id and state.get("boot_id") != self.boot_id)
                ):
                    self.stop("The Console session ended. Connect again.")
                    return
                with self.lock:
                    if self.stopped.is_set():
                        return
                    self.last_heartbeat = time.monotonic()
                    self.last_heartbeat_at = time.time()
                    self.error = None
            except (ServiceError, OSError, ValueError) as error:
                if isinstance(error, ServiceError) and error.status in {401, 403, 409}:
                    self.stop("The Console session ended. Connect again.")
                    return
                with self.lock:
                    if self.stopped.is_set():
                        return
                    self.error = "The PC process cannot reach the Console."
                if time.monotonic() - self.last_heartbeat >= SESSION_TIMEOUT_S:
                    self.stop("The connection heartbeat expired. Connect again.")
                    return

    def _collect(self):
        while not self.stopped.is_set():
            try:
                state = self.reader(STATE)
                if (
                    not isinstance(state, dict)
                    or len(json.dumps(state, allow_nan=False).encode()) > MAX_STATE_BYTES
                ):
                    raise ValueError("The state exceeds the local size limit.")
                with self.lock:
                    if self.stopped.is_set():
                        return
                    # Retain raw data only. The browser verifies its own data agreement.
                    self.latest = copy.deepcopy(state)
                    self.received_at = time.time()
                    self.read_error = None
            except (OSError, ValueError, TypeError, RecursionError, ServiceError):
                with self.lock:
                    if self.stopped.is_set():
                        return
                    self.read_error = "The PC process cannot read the latest state."
            if self.stopped.wait(READ_INTERVAL_S):
                return

    def snapshot(self, *, include_state=False):
        with self.lock:
            active = not self.stopped.is_set()
            state = self.latest or {}
            read = {
                "running": active and self.reader is not None,
                "received_at": self.received_at,
                "seq": state.get("seq"),
                "boot_id": state.get("boot_id"),
                "contract_id": state.get("contract_id"),
                "verification": "unverified",
                "quality": "unavailable"
                if self.latest is None
                else "unverified"
                if active and not self.read_error
                else "last_received",
                "error": self.read_error,
            }
            if include_state:
                read["raw_state"] = copy.deepcopy(self.latest)
            return {
                "running": active,
                "heartbeat_owner": "process",
                "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
                "last_heartbeat_at": self.last_heartbeat_at,
                "error": self.error,
                "read_collection": read,
            }
