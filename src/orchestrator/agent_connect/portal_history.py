"""Bounded, encrypted PC chat history. This module cannot send commands."""

import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from labkit.errors import ServiceError

from .credential_protection import WindowsCredentialProtector
from .live import ORIGIN
from .model_profiles import _checked_path

LIMITS = {"message": 60, "activity": 120, "usage": 100, "request": 100}


class PortalHistory:
    """Encrypt each payload with the current Windows account before SQLite writes."""

    def __init__(self, path=None, protector=None):
        self.lock = threading.RLock()
        self.available = False
        self.message = "Local history is unavailable."
        try:
            self.protector = protector or WindowsCredentialProtector()
            if path is None:
                root = os.environ.get("LOCALAPPDATA", "")
                if not root or not Path(root).is_absolute():
                    raise ValueError
                path = Path(root) / "AI Chemistry Console" / "agent-portal" / "history.sqlite3"
            self.path = _checked_path(Path(path).absolute())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS history ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, "
                    "kind TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL, "
                    "UNIQUE(origin,kind,key))"
                )
            self.available = True
            self.message = "Recent live conversation and command references are saved on this PC."
        except Exception:
            self.message = (
                "Secure local history could not be opened. Existing files were not replaced."
            )

    @contextmanager
    def _db(self):
        _checked_path(self.path)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            yield db

    def _encode(self, value):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > 1024 * 1024:
            raise ValueError
        return self.protector.protect(raw)

    def _decode(self, value):
        return json.loads(self.protector.unprotect(value))

    def _error(self):
        self.available = False
        self.message = "Local history could not be saved or read. No command was repeated."
        return ServiceError(503, self.message)

    def status(self):
        return {"available": self.available, "message": self.message}

    def rows(self, kind):
        if kind not in LIMITS:
            raise ValueError("Unknown history kind.")
        with self.lock:
            try:
                with self._db() as db:
                    rows = db.execute(
                        "SELECT payload FROM history WHERE origin=? AND kind=? ORDER BY seq",
                        (ORIGIN, kind),
                    ).fetchall()
                return [self._decode(row[0]) for row in rows]
            except Exception:
                raise self._error() from None

    def put(self, kind, key, value, *, once=False):
        """Return an existing request when once=True; never replace that request."""
        if kind not in LIMITS:
            raise ValueError("Unknown history kind.")
        with self.lock:
            try:
                if not self.available:
                    raise ValueError
                payload = self._encode(value)
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute(
                        "SELECT payload FROM history WHERE origin=? AND kind=? AND key=?",
                        (ORIGIN, kind, key),
                    ).fetchone()
                    if old and once:
                        return self._decode(old[0])
                    db.execute(
                        "INSERT INTO history(origin,kind,key,payload) VALUES(?,?,?,?) "
                        "ON CONFLICT(origin,kind,key) DO UPDATE SET payload=excluded.payload",
                        (ORIGIN, kind, key, payload),
                    )
                    db.execute(
                        "DELETE FROM history WHERE origin=? AND kind=? AND seq NOT IN "
                        "(SELECT seq FROM history WHERE origin=? AND kind=? "
                        "ORDER BY seq DESC LIMIT ?)",
                        (ORIGIN, kind, ORIGIN, kind, LIMITS[kind]),
                    )
                return None
            except Exception:
                raise self._error() from None
