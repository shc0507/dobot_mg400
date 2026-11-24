"""Keep a bounded Stop audit without replaying device requests."""

import json
import os
import sqlite3

from .authority_store import private_path

HISTORY_LIMIT = 200
SCHEMA_VERSION = "1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StopStore:
    """Only the Stop audit worker opens or calls this store."""

    def __init__(self, path=None):
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
        self.connection = sqlite3.connect(str(self.path) if self.path else ":memory:", timeout=0.1)
        try:
            with self.connection:
                tables = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables and not {"stop_meta", "stops"} <= tables:
                    raise ValueError("The Stop history has an unsupported format.")
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS stop_meta (key TEXT PRIMARY KEY, value TEXT)"
                )
                version = self.connection.execute(
                    "SELECT value FROM stop_meta WHERE key='version'"
                ).fetchone()
                if (tables and version is None) or (version and version[0] != SCHEMA_VERSION):
                    raise ValueError("The Stop history requires a supported release.")
                self.connection.execute(
                    "INSERT OR IGNORE INTO stop_meta VALUES ('version', ?)", (SCHEMA_VERSION,)
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS stops (id TEXT PRIMARY KEY, revision INTEGER "
                    "NOT NULL, updated_at REAL NOT NULL, record_json TEXT NOT NULL)"
                )
        except BaseException:
            self.connection.close()
            raise

    def load(self):
        records = []
        for identifier, revision, encoded in self.connection.execute(
            "SELECT id, revision, record_json FROM stops ORDER BY updated_at DESC LIMIT ?",
            (HISTORY_LIMIT,),
        ):
            record = json.loads(encoded)
            if (
                not isinstance(record, dict)
                or record.get("id") != identifier
                or record.get("revision") != revision
                or not isinstance(record.get("deliveries"), list)
                or len(encoded) > 131072
            ):
                raise ValueError("A saved Stop record is invalid.")
            canonical(record)
            records.append(record)
        return records

    def save(self, record):
        with self.connection:
            self.connection.execute(
                "INSERT INTO stops VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision, updated_at=excluded.updated_at, "
                "record_json=excluded.record_json WHERE stops.revision <= excluded.revision",
                (record["id"], record["revision"], record["updated_at"], canonical(record)),
            )
            self.connection.executemany(
                "DELETE FROM stops WHERE id=? AND id!=?",
                [(identifier, record["id"]) for identifier in record.get("previous_ids", [])],
            )
            self.connection.execute(
                "DELETE FROM stops WHERE id NOT IN "
                "(SELECT id FROM stops ORDER BY updated_at DESC LIMIT ?)",
                (HISTORY_LIMIT,),
            )

    def close(self):
        self.connection.close()
