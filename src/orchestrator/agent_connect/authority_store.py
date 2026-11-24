"""Persist approved clients without persisting sessions or operator grants."""

import base64
import os
import sqlite3
import stat
from pathlib import Path

from labkit.errors import ServiceError

SCHEMA_VERSION = "1"


def private_path(path):
    """Reject links before creating or opening authority data."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Authority paths must not contain symbolic links.")
    return path


class AuthorityStore:
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
        self.connection = sqlite3.connect(
            str(self.path) if self.path is not None else ":memory:",
            check_same_thread=False,
            timeout=3,
        )
        self.connection.row_factory = sqlite3.Row
        try:
            self._initialize()
            self._remove_legacy_owner_code()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        with self.connection:
            existed = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='agent_connect_authority_meta'"
            ).fetchone()
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS agent_connect_authority_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self.connection.execute(
                "SELECT value FROM agent_connect_authority_meta WHERE key = 'version'"
            ).fetchone()
            if (existed and row is None) or (row is not None and row["value"] != SCHEMA_VERSION):
                raise ServiceError(503, "The saved authority format requires a supported release.")
            self.connection.execute(
                "INSERT OR IGNORE INTO agent_connect_authority_meta VALUES ('version', ?)",
                (SCHEMA_VERSION,),
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS agent_connect_clients ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, credential_hash TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'revoked')), "
                "matching_code TEXT NOT NULL, created_at REAL NOT NULL, "
                "approved_at REAL, revoked_at REAL)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS agent_connect_client_connections ("
                "client_id TEXT PRIMARY KEY, last_connected_at REAL)"
            )

    def _remove_legacy_owner_code(self):
        """Remove only a generated bootstrap file beside a legacy authority database."""
        if self.path is None:
            return
        legacy = self.connection.execute(
            "SELECT 1 FROM agent_connect_authority_meta WHERE key='activated'"
        ).fetchone()
        if legacy is None:
            return
        path = self.path.with_name("owner-code")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size not in {44, 45}:
                return
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                opened = os.fstat(handle.fileno())
                data = handle.read(46)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return
            token = data.removesuffix(b"\r\n").removesuffix(b"\n")
            decoded = base64.b64decode(token + b"=", altchars=b"-_", validate=True)
            if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != token:
                return
            current = path.lstat()
            keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
            if all(getattr(info, key) == getattr(current, key) for key in keys):
                path.unlink()
        except (OSError, ValueError):
            # An unrelated, changed, or inaccessible file is not ours to remove.
            return

    def clients(self):
        return {
            row["id"]: dict(row)
            for row in self.connection.execute(
                "SELECT clients.*, connections.last_connected_at "
                "FROM agent_connect_clients clients "
                "LEFT JOIN agent_connect_client_connections connections "
                "ON clients.id=connections.client_id "
                "ORDER BY clients.rowid"
            )
        }

    def save(self, client):
        with self.connection:
            self.connection.execute(
                "INSERT INTO agent_connect_clients "
                "(id, name, credential_hash, status, matching_code, "
                "created_at, approved_at, revoked_at) "
                "VALUES "
                "(:id, :name, :credential_hash, :status, :matching_code, "
                ":created_at, :approved_at, :revoked_at) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                "approved_at=excluded.approved_at, revoked_at=excluded.revoked_at",
                client,
            )
            self.connection.execute(
                "INSERT INTO agent_connect_client_connections (client_id, last_connected_at) "
                "VALUES (:id, :last_connected_at) ON CONFLICT(client_id) DO UPDATE "
                "SET last_connected_at=excluded.last_connected_at",
                client,
            )

    def delete(self, client_id):
        with self.connection:
            self.connection.execute("DELETE FROM agent_connect_clients WHERE id = ?", (client_id,))
            self.connection.execute(
                "DELETE FROM agent_connect_client_connections WHERE client_id = ?", (client_id,)
            )

    def close(self):
        self.connection.close()
