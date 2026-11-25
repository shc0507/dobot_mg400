"""Restore only a live observer connection after the PC portal starts."""

import threading

from labkit.errors import ServiceError

from .portal_runtime import PortalSession
from .portal_store import PAIRING_FIELDS, PairingStorageError

RESTORE_ATTEMPTS = 5
RESTORE_INTERVAL_S = 2
RESTORE_CLOSE_WAIT_S = 4


class PortalContinuity:
    def __init__(self, control, store, origin):
        self.control, self.store, self.origin = control, store, origin
        self.cancelled = threading.Event()
        self.thread = None
        self.closing = False
        self.attempts = 0
        self.state = "disabled"
        self.message = "Pairing is kept in memory for this process."
        if store is None:
            return
        control.records["live"].update(store.load(origin))
        status = store.status()
        if not status["storage_available"]:
            self.state, self.message = "unavailable", status["storage_message"]
        elif control.records["live"].get("connect_intent"):
            self.state, self.message = "pending", "Restoring the saved observer connection."
        else:
            self.state, self.message = "disconnected", "The agent is disconnected."

    def snapshot(self):
        record = self.control.records["live"]
        return {
            "state": self.state,
            "message": self.message,
            "attempts": self.attempts,
            "storage_available": bool(self.store and self.store.status()["storage_available"]),
            "paired": bool(record.get("credential")),
            "name": record.get("name"),
            "connect_intent": bool(record.get("connect_intent")),
        }

    def start(self):
        with self.control.lock:
            if (
                self.state != "pending"
                or self.thread is not None
                or self.control.closed
                or self.closing
            ):
                return
            self.thread = threading.Thread(
                target=self._restore, daemon=True, name="agent-portal-restore"
            )
            self.thread.start()

    def cancel(self, source="live"):
        if source == "live":
            self.cancelled.set()
            if self.state in {"pending", "connecting", "retrying"}:
                self.state, self.message = (
                    "disconnected",
                    "Automatic reconnection stopped. Select Connect to reconnect.",
                )

    def manual_connect(self, source):
        self.cancel(source)
        if source == "live" and self.thread is not None and self.thread.is_alive():
            raise ServiceError(409, "Connection restoration is stopping. Select Connect again.")

    def save(self, source, *, connected):
        if source != "live" or self.store is None:
            return
        record = self.control.records[source]
        record["connect_intent"] = connected
        self.state = "connected" if connected else "disconnected"
        self.message = (
            "The agent connected. Operator control requires a new grant."
            if connected
            else "The agent is disconnected."
        )
        try:
            self.store.save(
                self.origin,
                {key: record.get(key) for key in PAIRING_FIELDS}
                if record.get("credential")
                else {},
            )
        except PairingStorageError:
            self.state, self.message = "unavailable", "The pairing could not be saved securely."
            # Never leave an older connected intent after an explicit disconnect.
            if not connected:
                try:
                    self.store.forget()
                except PairingStorageError:
                    self.message = (
                        "Disconnect was local only. The saved pairing could not be removed."
                    )

    def forget(self, source):
        self.cancel(source)
        self.control.records[source].clear()
        session = self.control.sessions.get(source)
        if session is not None:
            session.stop("Connection access ended. Use a new invitation.")
        self.save(source, connected=False)
        if source == "live":
            self.state, self.message = "forgotten", "Connection access ended. Use a new invitation."

    def close(self):
        with self.control.lock:
            self.cancel()
            self.closing = True
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=RESTORE_CLOSE_WAIT_S)
        with self.control.lock:
            if self.store is not None and (self.thread is None or not self.thread.is_alive()):
                self.store.close()

    def _restore(self):
        try:
            self._restore_attempts()
        finally:
            with self.control.lock:
                # A late connect can replace a newer portal session. Keep file ownership
                # until both that request and its disconnect cleanup have finished.
                if self.closing and self.store is not None:
                    self.store.close()

    def _restore_attempts(self):
        for attempt in range(1, RESTORE_ATTEMPTS + 1):
            with self.control.lock:
                record = self.control.records["live"]
                if (
                    self.cancelled.is_set()
                    or self.control.closed
                    or not record.get("connect_intent")
                ):
                    return
                credential = record["credential"]
                self.attempts = attempt
                self.state, self.message = "connecting", "Restoring the saved observer connection."
            try:
                # Do not hold the local lock during a network timeout.
                result = self.control.transport("live", "connect", {"credential": credential})
                self._accept(result, credential)
                return
            except ServiceError as error:
                with self.control.lock:
                    if self.cancelled.is_set() or self.control.closed:
                        return
                    if error.status in {401, 403}:
                        self.forget("live")
                        return
                    if error.status < 500:
                        self.state, self.message = (
                            "failed",
                            "The Console refused reconnection. Connect again.",
                        )
                        return
            except (OSError, ValueError):
                pass
            with self.control.lock:
                if self.cancelled.is_set() or self.control.closed:
                    return
                self.state, self.message = (
                    ("offline", "The Console is offline. Connect LarePass, then select Connect.")
                    if attempt == RESTORE_ATTEMPTS
                    else (
                        "retrying",
                        "The Console is offline. Retrying the saved observer connection.",
                    )
                )
            if attempt < RESTORE_ATTEMPTS and self.cancelled.wait(RESTORE_INTERVAL_S):
                return

    def _accept(self, result, credential):
        token = result.get("session_token")
        state = result.get("state") or {}
        state = state if isinstance(state, dict) else {}
        principal = state.get("principal") or {}
        principal = principal if isinstance(principal, dict) else {}
        operator = state.get("operator") or {}
        valid = (
            isinstance(token, str)
            and bool(token)
            and bool(result.get("session_id"))
            and principal.get("id") == result["session_id"]
            and principal.get("kind") == "agent"
            and isinstance(operator, dict)
            and operator.get("id") != principal.get("id")
        )
        with self.control.lock:
            record = self.control.records["live"]
            valid = valid and principal.get("client_id") == record.get("client_id")
            current = (
                not self.cancelled.is_set()
                and not self.control.closed
                and record.get("credential") == credential
                and record.get("connect_intent")
                and not record.get("session_token")
            )
            if current and valid:
                record["session_token"] = token
                session = PortalSession(
                    "live", result, self.control.transport, self.control.live_reader
                )
                self.control.sessions["live"] = session
                session.start()
                self.state, self.message = (
                    "connected",
                    "Restored as observer. Grant operator control from the Console.",
                )
                return
            if current:
                self.state, self.message = (
                    "failed",
                    "The Console did not return an observer connection. Connect again.",
                )
        if isinstance(token, str) and token:
            try:
                self.control.transport("live", "disconnect", {}, token=token)
            except (ServiceError, OSError, ValueError):
                pass
