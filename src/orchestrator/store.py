"""Private SQLite run records and files. Never follow data-directory symlinks."""

import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

DEFAULT_RUNS = Path("/var/lib/mg400/runs")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("chip_id must be a nonempty string.")
    return value


class RunStore:
    def __init__(self, directory=DEFAULT_RUNS, *, readonly=False):
        self.directory = Path(directory).absolute()
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise ValueError("Run paths must not contain symbolic links.")
        self.owner = (
            (1000, 1000)
            if self.directory.is_relative_to("/var/lib/mg400")
            else (os.geteuid(), os.getegid())
        )
        if not readonly:
            try:
                self.directory.mkdir(mode=0o750)
            except FileExistsError:
                pass
            else:
                self.directory.chmod(0o750)
        self._check(self.directory, directory=True)
        self.path = self.directory / "runs.db"
        if not readonly:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._check(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                self._check(sidecar)
        self._lock = threading.RLock()
        uri = self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, recipe_json TEXT NOT NULL,
                    chip_id TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
                    skill TEXT NOT NULL, params_json TEXT NOT NULL, chip_id TEXT,
                    status TEXT NOT NULL, started_at REAL, finished_at REAL,
                    result_json TEXT, error TEXT, resolution TEXT,
                    PRIMARY KEY(run_id, position)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    step INTEGER NOT NULL, chip_id TEXT, path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    step INTEGER NOT NULL, chip_id TEXT NOT NULL, data_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    chip_id TEXT, step INTEGER, kind TEXT NOT NULL,
                    data_json TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)

    def _check(self, path, *, directory=False):
        info = path.lstat()
        valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        allowed_modes = {0o700, 0o750} if directory else {0o600, 0o640}
        if (
            not valid_kind
            or (info.st_uid, info.st_gid) != self.owner
            or stat.S_IMODE(info.st_mode) not in allowed_modes
        ):
            raise ValueError(f"Run data has unsafe type, ownership or permissions: {path}")

    def create(self, recipe):
        run_id = uuid.uuid4().hex
        chip_id = recipe.params.get("chip_id")
        if chip_id is not None:
            identifier(chip_id)
        payload = encoded(asdict(recipe))
        folder = self.directory / run_id
        folder.mkdir(mode=0o750)
        now = time.time()
        try:
            folder.chmod(0o750)
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, recipe.name, payload, chip_id, "created", now, now, ""),
                )
                self._db.executemany(
                    "INSERT INTO steps(run_id,position,skill,params_json,chip_id,status) "
                    "VALUES(?,?,?,?,?,?)",
                    [
                        (
                            run_id,
                            index,
                            step.skill,
                            encoded(step.params),
                            step.params.get("chip_id", chip_id),
                            "pending",
                        )
                        for index, step in enumerate(recipe.steps)
                    ],
                )
        except BaseException:
            folder.rmdir()
            raise
        return run_id

    def event(self, run_id, kind, data, *, chip_id=None, step=None):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events(run_id,chip_id,step,kind,data_json,created_at) "
                "SELECT id,COALESCE(?,chip_id),?,?,?,? FROM runs WHERE id=?",
                (chip_id, step, kind, encoded(data), time.time(), run_id),
            )

    def status(self, run_id, status, detail=""):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE runs SET status=?,detail=?,updated_at=? WHERE id=?",
                (status, detail, time.time(), run_id),
            )
            self.event(run_id, "status", {"status": status, "detail": detail})

    def step(self, run_id, position, status, *, result=None, error=None, resolution=None):
        with self._lock, self._db:
            if status == "running":
                self._db.execute(
                    "UPDATE steps SET status=?,started_at=? WHERE run_id=? AND position=?",
                    (status, time.time(), run_id, position),
                )
            else:
                self._db.execute(
                    "UPDATE steps SET status=?,finished_at=?,result_json=?,error=?,resolution=? "
                    "WHERE run_id=? AND position=?",
                    (status, time.time(), encoded(result), error, resolution, run_id, position),
                )

    def sample(self, run_id, step, chip_id, data):
        identifier(chip_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO samples(run_id,step,chip_id,data_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, step, chip_id, encoded(data), time.time()),
            )

    def artifact(self, run_id, step, name, data, *, chip_id=None):
        if chip_id is not None:
            identifier(chip_id)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise ValueError("Artifact names must be simple filenames.")
        if not isinstance(data, bytes):
            raise ValueError("Artifact content must be bytes.")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid run identity.")
        with self._lock:
            if not self._db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise ValueError("Unknown run.")
            folder = self.directory / run_id
            self._check(folder, directory=True)
            artifact_id = uuid.uuid4().hex
            path = folder / f"{artifact_id}-{name}"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                with self._db:
                    self._db.execute(
                        "INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                        (
                            artifact_id,
                            run_id,
                            step,
                            chip_id,
                            str(path.relative_to(self.directory)),
                            time.time(),
                        ),
                    )
            except BaseException:
                path.unlink()
                raise
            return artifact_id

    def runs(self):
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    "SELECT id,name,chip_id,status,created_at,updated_at,detail "
                    "FROM runs ORDER BY created_at DESC"
                )
            ]

    def get(self, run_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown run.")
            result = dict(row)
            for table in ("steps", "samples", "artifacts", "events"):
                result[table] = [
                    dict(item)
                    for item in self._db.execute(
                        f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
                    )
                ]
            return result

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
