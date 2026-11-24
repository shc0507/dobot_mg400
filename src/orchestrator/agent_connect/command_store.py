"""Store command identities and outcomes without replaying device writes."""

import json
import os
import sqlite3
import threading
import time

from labkit.errors import ServiceError

from .authority_store import private_path

SCHEMA_VERSION = "1"
ACTIVE = frozenset({"accepted", "running", "cancelling"})
TERMINAL = frozenset({"succeeded", "failed", "rejected", "stopped", "unknown"})


def canonical(value):
    """Reject non-JSON values and use stable object-key order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CommandStore:
    """Keep one record per client request ID, including completed requests."""

    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.path = private_path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_path(self.path)
            for suffix in ("-journal", "-wal", "-shm"):
                private_path(str(self.path) + suffix)
            if not self.path.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                os.close(descriptor)
            self.path.chmod(0o600)
        self.connection = sqlite3.connect(
            str(self.path) if self.path is not None else ":memory:",
            check_same_thread=False,
            timeout=0.1,
        )
        self.connection.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        with self.connection:
            tables = {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables and not {"command_meta", "commands"} <= tables:
                raise ServiceError(503, "The command history has an unsupported format.")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS command_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version = self.connection.execute(
                "SELECT value FROM command_meta WHERE key='version'"
            ).fetchone()
            if (tables and version is None) or (
                version is not None and version[0] != SCHEMA_VERSION
            ):
                raise ServiceError(503, "The command history requires a supported release.")
            self.connection.execute(
                "INSERT OR IGNORE INTO command_meta VALUES ('version', ?)", (SCHEMA_VERSION,)
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS commands ("
                "client_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                "request_json TEXT NOT NULL, operation_json TEXT NOT NULL, "
                "PRIMARY KEY (client_id, request_id))"
            )
            # Validate all history before accepting any new write. Never replace damaged rows.
            records = self.items()
            for record in records:
                if record["status"] in ACTIVE:
                    record.update(
                        status="unknown",
                        updated_at=time.time(),
                        finished_at=time.time(),
                        error="The Console restarted before the operation outcome was recorded.",
                    )
                    self._update(record)

    @staticmethod
    def _decode(row):
        record = json.loads(row["operation_json"])
        body = json.loads(row["request_json"])
        if (
            not isinstance(record, dict)
            or record.get("status") not in ACTIVE | TERMINAL
            or record.get("client_id") != row["client_id"]
            or record.get("request_id") != row["request_id"]
            or not isinstance(record.get("id"), str)
            or type(record.get("dispatch_count")) is not int
            or record["dispatch_count"] < 0
            or not isinstance(body, dict)
            or body.get("request_id") != row["request_id"]
        ):
            raise ValueError("The saved command record is invalid.")
        canonical(record)
        canonical(body)
        return record

    def get(self, client_id, request_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM commands WHERE client_id=? AND request_id=?",
                (client_id, request_id),
            ).fetchone()
            if row is None:
                return None
            return row["request_json"], self._decode(row)

    def items(self, client_id=None):
        with self.lock:
            sql = "SELECT * FROM commands"
            args = ()
            if client_id is not None:
                sql += " WHERE client_id=?"
                args = (client_id,)
            return [
                self._decode(row) for row in self.connection.execute(sql + " ORDER BY rowid", args)
            ]

    def insert(self, body, record):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (record["client_id"], record["request_id"], canonical(body), canonical(record)),
            )

    def _update(self, record):
        cursor = self.connection.execute(
            "UPDATE commands SET operation_json=? WHERE client_id=? AND request_id=?",
            (canonical(record), record["client_id"], record["request_id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("The command history no longer contains this operation.")

    def save(self, record):
        with self.lock, self.connection:
            self._update(record)

    def close(self):
        with self.lock:
            self.connection.close()
