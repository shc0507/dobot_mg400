"""Private SQLite run records and files. Never follow data-directory symlinks."""

import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

DEFAULT_RUNS = Path("/var/lib/mg400/runs")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


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
        directory_info = self._check(self.directory, directory=True)
        self._directory_identity = directory_info.st_dev, directory_info.st_ino
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
                -- Device history. A restarted service reuses seq, never (seq, ts).
                CREATE TABLE IF NOT EXISTS device_events (
                    id INTEGER PRIMARY KEY, device TEXT NOT NULL, seq INTEGER NOT NULL,
                    ts REAL, level TEXT NOT NULL, message TEXT NOT NULL,
                    UNIQUE(device, seq, ts)
                );
                CREATE TABLE IF NOT EXISTS device_states (
                    id INTEGER PRIMARY KEY, device TEXT NOT NULL, ts REAL NOT NULL,
                    phase TEXT NOT NULL, row_json TEXT NOT NULL, state_json TEXT
                );
                CREATE INDEX IF NOT EXISTS device_events_ts ON device_events(ts);
                CREATE INDEX IF NOT EXISTS device_states_ts ON device_states(device, ts);
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
        return info

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

    @staticmethod
    def _page(limit, offset=0):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("Read limit must be an integer from 1 to 1000.")
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError("Read offset must be an integer from 0 to 1000000.")
        return -1 if limit is None else limit, offset

    def runs(self, *, limit=None, offset=0, chip_id=None):
        page = self._page(limit, offset)
        where, parameters = "", []
        if chip_id is not None:
            identifier(chip_id)
            where = "WHERE runs.chip_id=? OR " + " OR ".join(
                f"EXISTS(SELECT 1 FROM {table} WHERE {table}.run_id=runs.id AND {table}.chip_id=?)"
                for table in ("steps", "samples", "artifacts", "events")
            )
            parameters = [chip_id] * 5
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    "SELECT id,name,chip_id,status,created_at,updated_at,detail "
                    f"FROM runs {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (*parameters, *page),
                )
            ]

    def get(self, run_id, *, item_limit=None, chip_id=None):
        limit, _ = self._page(item_limit)
        if chip_id is not None:
            identifier(chip_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown run.")
            result = dict(row)
            for table in ("steps", "samples", "artifacts", "events"):
                where = "run_id=?" + (" AND chip_id=?" if chip_id is not None else "")
                parameters = (run_id, chip_id) if chip_id is not None else (run_id,)
                result[table] = [
                    dict(item)
                    for item in self._db.execute(
                        f"SELECT * FROM {table} WHERE {where} ORDER BY rowid LIMIT ?",
                        (*parameters, -1 if item_limit is None else limit + 1),
                    )
                ]
                if item_limit is not None:
                    result.setdefault("truncated", {})[table] = len(result[table]) > item_limit
                    result[table] = result[table][:item_limit]
            return result

    @staticmethod
    def _run_identity(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid run identity.")

    @staticmethod
    def _chip_relation():
        return "runs.chip_id=? OR " + " OR ".join(
            f"EXISTS(SELECT 1 FROM {table} WHERE {table}.run_id=runs.id AND {table}.chip_id=?)"
            for table in ("steps", "samples", "artifacts", "events")
        )

    @staticmethod
    def _artifact_metadata(row):
        result = dict(row)
        run_id, artifact_id, path = result["run_id"], result["id"], result.pop("path")
        RunStore._run_identity(run_id)
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise ValueError("Invalid artifact identity.")
        prefix = f"{run_id}/{artifact_id}-"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError("Invalid stored artifact path.")
        name = path[len(prefix) :]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise ValueError("Invalid stored artifact name.")
        result["name"] = name
        return result

    def chip_history(self, chip_id, *, kind="runs", run_id=None, limit=100, offset=0):
        """Read one exact chip's records, filtering before bounded pagination.

        Each table uses its recorded chip identity. A run summary is included
        if that chip occurs in the run or one of its records; summaries omit
        the complete recipe and other chips' records. Artifact paths are never
        returned. next_offset is None only at the end of this query.
        """
        identifier(chip_id)
        if kind not in ("runs", "steps", "samples", "artifacts", "events"):
            raise ValueError("Unknown chip history kind.")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Read limit must be an integer from 1 to 1000.")
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1 - limit:
            raise ValueError("Read offset must be a nonnegative SQLite integer.")
        if run_id is not None:
            self._run_identity(run_id)
        relation = self._chip_relation()
        with self._lock:
            if (
                run_id is not None
                and not self._db.execute(
                    f"SELECT 1 FROM runs WHERE id=? AND ({relation})", (run_id, *([chip_id] * 5))
                ).fetchone()
            ):
                raise ValueError("Unknown chip run.")
            if kind == "runs":
                select = "SELECT id,name,status,created_at,updated_at,detail FROM runs"
                where, parameters = f"({relation})", [chip_id] * 5
                order = "created_at DESC,id DESC"
                run_column = "id"
            else:
                select = f"SELECT * FROM {kind}"
                where, parameters = "chip_id=?", [chip_id]
                order = "run_id,position" if kind == "steps" else "created_at,id"
                run_column = "run_id"
            if run_id is not None:
                where += f" AND {run_column}=?"
                parameters.append(run_id)
            rows = [
                dict(row)
                for row in self._db.execute(
                    f"{select} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                    (*parameters, limit + 1, offset),
                )
            ]
        more = len(rows) > limit
        items = rows[:limit]
        if kind == "artifacts":
            items = [self._artifact_metadata(row) for row in items]
        return {"items": items, "next_offset": offset + limit if more else None}

    @staticmethod
    def _inode(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _file_signature(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _artifact_file_check(self, info, *, directory=False):
        valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        modes = (0o700, 0o750) if directory else (0o600, 0o640)
        if (
            not valid_kind
            or (info.st_uid, info.st_gid) != self.owner
            or stat.S_IMODE(info.st_mode) not in modes
            or (not directory and info.st_nlink != 1)
        ):
            raise ValueError("Artifact data has unsafe type, ownership, links or permissions.")

    def artifact_bytes(self, artifact_id, *, chip_id, max_bytes=MAX_ARTIFACT_BYTES):
        """Read a chip-associated artifact by opaque ID, without accepting a path.

        Data directories and the final regular file are opened through anchored
        descriptors. The final O_PATH handle rejects special files before a
        readable handle is opened. Reads are capped at 16 MiB, and replacement
        or modification during the read fails instead of returning mixed data.
        """
        identifier(chip_id)
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise ValueError("Invalid artifact identity.")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact read limit must be an integer from 1 byte to 16 MiB.")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM artifacts WHERE id=? AND chip_id=?", (artifact_id, chip_id)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown chip artifact.")
            result = self._artifact_metadata(row)
        try:
            with ExitStack() as stack:
                links = []

                def open_handle(path, flags, *, parent=None):
                    fd = os.open(path, flags | os.O_CLOEXEC, dir_fd=parent)
                    stack.callback(os.close, fd)
                    return fd

                directory_flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                directory = open_handle("/", directory_flags)
                for part in self.directory.parts[1:]:
                    if part in (".", ".."):
                        raise ValueError("Run paths must be normalized.")
                    child = open_handle(part, directory_flags, parent=directory)
                    links.append((directory, part, child))
                    directory = child
                root_info = os.fstat(directory)
                self._artifact_file_check(root_info, directory=True)
                if self._inode(root_info) != self._directory_identity:
                    raise ValueError("Run directory was replaced.")
                run = open_handle(result["run_id"], directory_flags, parent=directory)
                links.append((directory, result["run_id"], run))
                self._artifact_file_check(os.fstat(run), directory=True)
                filename = f"{artifact_id}-{result['name']}"
                handle = open_handle(filename, os.O_PATH | os.O_NOFOLLOW, parent=run)
                links.append((run, filename, handle))
                before = os.fstat(handle)
                self._artifact_file_check(before)
                if before.st_size > max_bytes:
                    raise ValueError("Artifact exceeds the read limit.")
                descriptor = open_handle(f"/proc/self/fd/{handle}", os.O_RDONLY | os.O_NONBLOCK)

                def verify():
                    for parent, name, child in links:
                        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        opened = os.fstat(child)
                        if self._inode(current) != self._inode(opened):
                            raise ValueError("Artifact path was replaced.")
                    self._artifact_file_check(os.fstat(directory), directory=True)
                    self._artifact_file_check(os.fstat(run), directory=True)
                    after = os.fstat(descriptor)
                    self._artifact_file_check(after)
                    if self._file_signature(before) != self._file_signature(after):
                        raise ValueError("Artifact changed during its read.")

                verify()
                chunks, size = [], 0
                while True:
                    chunk = os.read(descriptor, min(65536, max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Artifact exceeds the read limit.")
                    chunks.append(chunk)
                verify()
                if size != before.st_size:
                    raise ValueError("Artifact changed during its read.")
                return {**result, "data": b"".join(chunks)}
        except OSError as error:
            raise ValueError("Cannot read artifact safely.") from error

    def add_device_events(self, rows):
        """Insert (device, seq, ts, level, message) rows; skip rows already stored."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO device_events(device,seq,ts,level,message) "
                "VALUES(?,?,?,?,?)",
                rows,
            )

    def add_device_state(self, device, ts, phase, row, state):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO device_states(device,ts,phase,row_json,state_json) VALUES(?,?,?,?,?)",
                (device, ts, phase, encoded(row), None if state is None else encoded(state)),
            )

    def _device_rows(self, table, columns, device, since, start, end, limit):
        self._page(limit)
        clauses, parameters = ["id>?"], [since]
        for clause, value in (("device=?", device), ("ts>=?", start), ("ts<=?", end)):
            if value is not None:
                clauses.append(clause)
                parameters.append(value)
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (*parameters, limit),
                )
            ]

    def device_events(self, *, device=None, since=0, start=None, end=None, limit=500):
        """Return stored device events, newest first. id is the cursor for since."""
        return self._device_rows(
            "device_events", "id,device,seq,ts,level,message", device, since, start, end, limit
        )

    def device_states(self, *, device=None, start=None, end=None, limit=500):
        """Return stored device state rows, newest first."""
        rows = self._device_rows(
            "device_states", "id,device,ts,phase,row_json,state_json", device, 0, start, end, limit
        )
        for row in rows:
            row["row"] = json.loads(row.pop("row_json"))
            state = row.pop("state_json")
            row["state"] = None if state is None else json.loads(state)
        return rows

    def prune_devices(self, before):
        """Delete device history older than the time before."""
        with self._lock, self._db:
            for table in ("device_events", "device_states"):
                self._db.execute(f"DELETE FROM {table} WHERE ts<?", (before,))

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
