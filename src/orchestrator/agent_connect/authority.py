"""Authorize invited connections and select one operator without device access.

Keep one Authority instance per Console process. Call poll from the application's
poll loop. Check each delayed step again, or use guard for short atomic dispatch.
Neither this class nor its store sends commands to equipment.
"""

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from labkit.errors import ServiceError

from .authority_store import AuthorityStore

HEARTBEAT_INTERVAL_S = 2
SESSION_TIMEOUT_S = 10
INVITATION_TIMEOUT_S = 300
MAX_CLIENTS = 128
MAX_INVITATIONS = 16
INVITATION_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 256 or not value.isascii():
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _invitation_hash(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 32:
        return None
    normalized = "".join(char for char in value.upper() if char != "-" and not char.isspace())
    if len(normalized) != 8 or any(char not in INVITATION_ALPHABET for char in normalized):
        return None
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Server-issued identity, with no credential or session token."""

    kind: str
    id: str
    client_id: str
    name: str


@dataclass
class _Session:
    principal: Principal
    token_hash: str = field(repr=False)
    deadline: float
    seen: float
    seen_at: float


class Authority:
    def __init__(self, path=None, *, clock=time.monotonic, wall_clock=time.time):
        self.clock, self.wall_clock = clock, wall_clock
        self.lock = threading.RLock()
        self.boot_id = uuid.uuid4().hex
        self._human = Principal("human", uuid.uuid4().hex, "human", "Human")
        self._version = 0
        self._operator = None
        self._changed_at = self.wall_clock()
        self._reason = "console_started"
        self._warning = None
        self._sessions = {}
        self._tokens = {}
        self._stop_senders = {}
        self._invitations = {}
        self._closed = False
        self._store = AuthorityStore(path)
        try:
            self._clients = self._store.clients()
            if len(self._clients) > MAX_CLIENTS:
                raise ServiceError(503, "The saved connection list exceeds the supported limit.")
        except BaseException:
            self._store.close()
            raise

    @property
    def human(self):
        """Return the trusted Console identity for internal request dispatch."""
        with self.lock:
            self._available()
            return self._human

    @property
    def control_epoch(self):
        with self.lock:
            self._poll()
            return self._epoch()

    def _epoch(self):
        return f"{self.boot_id}:{self._version}"

    def _available(self):
        if self._closed:
            raise ServiceError(503, "The Console authority is closed.")

    def _change(self, principal, reason):
        previous = self._operator
        if reason == "connection_timeout" and previous is not None and previous.kind == "agent":
            self._warning = {
                "event_id": uuid.uuid4().hex,
                "warning_at": self.wall_clock(),
                "disconnected_at": self.wall_clock(),
                "reason": reason,
                "sender": self._identity(previous),
            }
        self._operator = principal
        self._version += 1
        self._changed_at = self.wall_clock()
        self._reason = reason

    def _remove_session(self, session_id, reason):
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self._tokens.pop(session.token_hash, None)
            self._stop_senders[session.token_hash] = session.principal
            if len(self._stop_senders) > 512:
                self._stop_senders.pop(next(iter(self._stop_senders)))
            if self._operator is session.principal:
                self._change(None, reason)

    def _poll(self):
        self._available()
        now = self.clock()
        expired = [key for key, session in self._sessions.items() if now >= session.deadline]
        for session_id in expired:
            self._remove_session(session_id, "connection_timeout")
        self._invitations = {
            key: value for key, value in self._invitations.items() if now < value[0]
        }
        return bool(expired)

    def poll(self):
        with self.lock:
            return self._poll()

    def _require_human(self, principal):
        self._poll()
        if principal is not self._human:
            raise ServiceError(403, "Use the Console to manage agent approval and control.")

    def _session(self, principal):
        self._poll()
        if not isinstance(principal, Principal) or not isinstance(principal.id, str):
            raise ServiceError(401, "Connect with a current approved session.")
        session = self._sessions.get(principal.id)
        if session is None or session.principal is not principal:
            raise ServiceError(401, "Connect with a current approved session.")
        return session

    def _new_session(self, client):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        connected_at = self.wall_clock()
        principal = Principal("agent", uuid.uuid4().hex, client["id"], client["name"])
        session = _Session(principal, _hash(token), now + SESSION_TIMEOUT_S, now, connected_at)
        client = dict(client, last_connected_at=connected_at)
        self._store.save(client)
        self._clients[client["id"]] = client
        for key, previous in list(self._sessions.items()):
            if previous.principal.client_id == client["id"]:
                self._remove_session(key, "agent_reconnected")
        self._sessions[principal.id] = session
        self._tokens[session.token_hash] = principal.id
        return {
            "session_token": token,
            "session_id": principal.id,
            "state": self._snapshot(principal),
        }

    def authenticate(self, token):
        with self.lock:
            self._poll()
            candidate = _hash(token)
            session = self._sessions.get(self._tokens.get(candidate))
            if session is None or not hmac.compare_digest(candidate, session.token_hash):
                raise ServiceError(401, "Connect with a current approved session.")
            return session.principal

    def stop_sender(self, token=None):
        """Identify a Stop without waiting for authority or persistent storage."""
        unknown = {
            "kind": "unverified",
            "name": "Unverified caller",
            "role": "unknown",
            "verified": False,
        }
        if not self.lock.acquire(blocking=False):
            return unknown
        try:
            if self._closed:
                return unknown
            if token is None:
                return {**self._identity(self._human), "role": "human", "verified": True}
            candidate = _hash(token)
            session = self._sessions.get(self._tokens.get(candidate))
            if session is not None:
                principal = session.principal
                role = (
                    "expired"
                    if self.clock() >= session.deadline
                    else "operator"
                    if self._operator is principal
                    else "observer"
                )
            else:
                principal = self._stop_senders.get(candidate)
                role = "expired"
            if principal is None:
                return unknown
            return {
                **self._identity(principal),
                "session_id": principal.id,
                "role": role,
                "verified": True,
            }
        finally:
            self.lock.release()

    def create_invitation(self, human):
        with self.lock:
            self._require_human(human)
            if len(self._invitations) >= MAX_INVITATIONS:
                raise ServiceError(409, "Use an existing invitation or wait for it to expire.")
            for _ in range(32):
                code = "".join(secrets.choice(INVITATION_ALPHABET) for _ in range(8))
                fingerprint = _invitation_hash(code)
                if fingerprint not in self._invitations:
                    self._invitations[fingerprint] = (self.clock() + INVITATION_TIMEOUT_S, False)
                    return {
                        "invitation": code[:4] + "-" + code[4:],
                        "expires_in_s": INVITATION_TIMEOUT_S,
                    }
            raise ServiceError(503, "Could not create a new invitation. Try again.")

    def pair(self, invitation, name):
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 64
            or not all(char.isprintable() for char in name)
        ):
            raise ValueError("Use an agent name with 1 to 64 printable characters.")
        with self.lock:
            self._poll()
            candidate = _invitation_hash(invitation)
            found = next(
                (
                    key
                    for key, (_deadline, consumed) in self._invitations.items()
                    if not consumed and candidate and hmac.compare_digest(key, candidate)
                ),
                None,
            )
            if found is None:
                raise ServiceError(401, "The invitation is invalid or expired.")
            if len(self._clients) >= MAX_CLIENTS:
                revoked = next(
                    (key for key, row in self._clients.items() if row["status"] == "revoked"), None
                )
                if revoked is None:
                    raise ServiceError(
                        409, "The connection list is full. Revoke an unused connection."
                    )
                self._store.delete(revoked)
                del self._clients[revoked]
            credential = secrets.token_urlsafe(32)
            now = self.wall_clock()
            client = {
                "id": "agent-" + uuid.uuid4().hex,
                "name": name.strip(),
                "credential_hash": _hash(credential),
                "status": "approved",
                "matching_code": secrets.token_hex(3).upper(),
                "created_at": now,
                "approved_at": now,
                "revoked_at": None,
                "last_connected_at": None,
            }
            self._store.save(client)
            self._clients[client["id"]] = client
            # Retain consumed codes until expiry so a collision cannot make one valid again.
            self._invitations[found] = (self._invitations[found][0], True)
            return {
                "client_id": client["id"],
                "credential": credential,
                "matching_code": client["matching_code"],
                "status": "approved",
            }

    def _client(self, client_id):
        if not isinstance(client_id, str) or client_id not in self._clients:
            raise ServiceError(404, "The agent connection does not exist.")
        return self._clients[client_id]

    def revoke(self, human, client_id):
        with self.lock:
            self._require_human(human)
            client = dict(self._client(client_id), status="revoked", revoked_at=self.wall_clock())
            self._store.save(client)
            self._clients[client_id] = client
            for key, session in list(self._sessions.items()):
                if session.principal.client_id == client_id:
                    self._remove_session(key, "connection_revoked")
            return self._snapshot(human)

    def _credential(self, credential):
        candidate = _hash(credential)
        client = next(
            (
                row
                for row in self._clients.values()
                if candidate and hmac.compare_digest(candidate, row["credential_hash"])
            ),
            None,
        )
        if client is None:
            raise ServiceError(401, "The agent credential is invalid or revoked.")
        return client

    def credential_status(self, credential):
        """Read only this credential's approval, without connecting or renewing it."""
        with self.lock:
            self._poll()
            client = self._credential(credential)
            return {key: client[key] for key in ("id", "name", "status", "matching_code")}

    def connect(self, credential):
        with self.lock:
            self._poll()
            client = self._credential(credential)
            if client["status"] == "revoked":
                raise ServiceError(401, "The agent credential is invalid or revoked.")
            if client["status"] != "approved":
                raise ServiceError(403, "Use a new invitation to connect this agent.")
            return self._new_session(client)

    def grant(self, human, session_id):
        with self.lock:
            self._require_human(human)
            session = self._sessions.get(session_id) if isinstance(session_id, str) else None
            if session is None or session.principal.kind != "agent":
                raise ServiceError(409, "Connect the approved agent before granting control.")
            if self._operator is not session.principal:
                self._change(session.principal, "agent_granted")
            return self._snapshot(human)

    def take_human(self, human):
        with self.lock:
            self._require_human(human)
            if self._operator is not human:
                self._change(human, "human_takeover")
            return self._snapshot(human)

    def acknowledge_warning(self, human):
        with self.lock:
            self._require_human(human)
            self._warning = None

    def _human_check(self, expected_epoch=None):
        self._poll()
        if expected_epoch is not None and expected_epoch != self._epoch():
            raise ServiceError(409, "Control changed. Read the current operator state.")
        if self._operator is not None and self._operator is not self._human:
            raise ServiceError(403, "An agent holds control. Take control in the Console first.")

    @contextmanager
    def human_guard(self, expected_epoch=None):
        """Allow trusted Console writes unless an agent currently holds control."""
        with self.lock:
            self._human_check(expected_epoch)
            yield

    def _check(self, principal, expected_epoch):
        if principal is self._human:
            self._poll()
        else:
            self._session(principal)
        if not isinstance(expected_epoch, str) or expected_epoch != self._epoch():
            raise ServiceError(409, "Control changed. Read the current operator state.")
        if self._operator is not principal:
            raise ServiceError(403, "Only the current operator can start this action.")

    def check(self, principal, expected_epoch):
        with self.lock:
            self._check(principal, expected_epoch)

    @contextmanager
    def guard(self, principal, expected_epoch):
        """Check a short simulated step atomically with ownership changes.

        Do not hold this context while waiting for I/O or operation completion.
        Stop must use its independent path and must not acquire this guard.
        """
        with self.lock:
            self._check(principal, expected_epoch)
            yield

    def release(self, principal, expected_epoch):
        with self.lock:
            if principal is self._human:
                self._require_human(principal)
            else:
                self._session(principal)
            if not isinstance(expected_epoch, str) or expected_epoch != self._epoch():
                raise ServiceError(409, "Control changed. Read the current operator state.")
            if principal is not self._human and self._operator is not principal:
                raise ServiceError(403, "Only the current operator can release its control.")
            if self._operator is not None:
                self._change(None, "operator_released")
            return self._snapshot(principal)

    def heartbeat(self, principal):
        with self.lock:
            session = self._session(principal)
            now = self.clock()
            session.seen, session.seen_at = now, self.wall_clock()
            session.deadline = now + SESSION_TIMEOUT_S
            return self._snapshot(principal)

    def disconnect(self, principal):
        with self.lock:
            self._session(principal)
            self._remove_session(principal.id, "connection_closed")
            return self._snapshot(None)

    @staticmethod
    def _identity(principal):
        return {
            "id": principal.id,
            "kind": principal.kind,
            "client_id": principal.client_id,
            "name": principal.name,
        }

    def _snapshot(self, principal):
        management = principal is self._human
        now = self.clock()
        sessions = [
            {
                **self._identity(session.principal),
                "last_seen_age_s": max(0, now - session.seen),
                "last_seen_at": session.seen_at,
                "expires_in_s": max(0, session.deadline - now),
            }
            for session in self._sessions.values()
            if management or session.principal is principal
        ]
        connected = {session.principal.client_id for session in self._sessions.values()}
        clients = [
            {
                **{key: value for key, value in client.items() if key != "credential_hash"},
                "connected": client["id"] in connected,
            }
            for client in self._clients.values()
            if management or (principal is not None and client["id"] == principal.client_id)
        ]
        return {
            "phase": 4,
            "boot_id": self.boot_id,
            "control_epoch": self._epoch(),
            "operator": self._identity(self._operator) if self._operator else None,
            "management_available": management,
            "session": self._identity(principal) if principal and not management else None,
            "principal": self._identity(principal) if principal else None,
            "clients": clients,
            "sessions": sessions,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "timeout_s": SESSION_TIMEOUT_S,
            "commands_enabled": False,
            "furnace_control_enabled": False,
            "last_change": {"reason": self._reason, "at": self._changed_at},
            "watchdog": {
                "policy": "warn_only",
                "shutdown_enabled": False,
                "timeout_s": SESSION_TIMEOUT_S,
                "pending": None,
                "active": dict(self._warning) if self._warning else None,
            },
        }

    def snapshot(self, principal=None):
        with self.lock:
            self._poll()
            if principal is not None and principal is not self._human:
                self._session(principal)
            return self._snapshot(principal)

    def restart(self):
        """Simulate a Console restart without losing approved client records."""
        with self.lock:
            self._available()
            self.boot_id = uuid.uuid4().hex
            self._human = Principal("human", uuid.uuid4().hex, "human", "Human")
            self._version = 0
            self._operator = None
            self._sessions.clear()
            self._stop_senders.clear()
            self._tokens.clear()
            self._invitations.clear()
            self._changed_at = self.wall_clock()
            self._reason = "console_restarted"
            self._warning = None
            return self._snapshot(None)

    def close(self):
        with self.lock:
            if self._closed:
                return
            if self._operator is not None:
                self._change(None, "console_closed")
            self._sessions.clear()
            self._tokens.clear()
            self._invitations.clear()
            self._store.close()
            self._closed = True
