"""Ordered sample evidence. Callers must authorize manual record changes."""

import copy
import json
import math
import time
import uuid
from datetime import datetime
from urllib.parse import quote

HOLDERS = {"unknown", "tray_slot", "robot", "stage_insert"}
KINDS = {
    "accepted",
    "rejected",
    "completed",
    "observation",
    "reported",
    "unknown",
    "entry",
    "partial",
    "conflict",
}


def _time(value):
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid evidence time.")
    if not math.isfinite(value):
        raise ValueError("Evidence time must be finite.")
    return float(value)


def location(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("Sample location must be an object.")
    for key in ("tray_id", "slot_id", "container_id"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not 1 <= len(item) <= 256):
            raise ValueError("Location IDs must contain 1 to 256 characters.")
    holder = value.get("holder", "unknown")
    if holder not in HOLDERS:
        raise ValueError("Unknown sample holder.")
    return {
        "holder": holder,
        "tray_id": value.get("tray_id"),
        "slot_id": value.get("slot_id"),
        "container_id": value.get("container_id"),
        "inside_furnace": None,
    }


def new_sample(chip_id):
    return {
        "chip_id": chip_id,
        "location": location(),
        "basis": "unknown",
        "last_observation": None,
        "last_known_location": None,
        "last_run": None,
        "last_step": None,
        "pending_operation": None,
        "revision": 0,
        "updated_at": None,
        "conflict": None,
        "passport_url": "/lab/chip?id=" + quote(chip_id, safe=""),
    }


def reduce_sample(previous, event):
    """Return new state without mutating inputs. Persistence assigns event order."""
    state = copy.deepcopy(previous or new_sample(event["chip_id"]))
    kind = event["kind"]
    if kind not in KINDS:
        raise ValueError("Unknown chip event kind.")
    now = event["recorded_at"]
    observed = event.get("observation")
    captured = _time(observed.get("observed_at")) if observed else None
    barrier = state.get("_movement_at")

    def invalidate():
        if state["location"]["holder"] != "unknown":
            state["last_known_location"] = {
                "location": copy.deepcopy(state["location"]),
                "basis": state["basis"],
                "updated_at": state["updated_at"],
            }
        state["location"] = location()
        state["basis"] = "unknown"

    if observed:
        previous_time = _time((state.get("last_observation") or {}).get("observed_at"))
        if previous_time is None or (captured is not None and captured >= previous_time):
            state["last_observation"] = copy.deepcopy(observed)
    if kind == "accepted":
        state["_pending_before"] = {
            "location": copy.deepcopy(state["location"]),
            "basis": state["basis"],
            "movement_at": state.get("_movement_at"),
            "revision": state["revision"] + 1,
        }
        state["pending_operation"] = {
            "id": event.get("operation_id", event["event_id"]),
            "destination": event.get("destination"),
            "action": event.get("action"),
            "may_start": event.get("may_start", True),
        }
        if event.get("may_start", True):
            invalidate()
            state["_movement_at"] = _time(now)
    elif kind == "rejected":
        # Only an acceptance that cannot start motion preserves the prior holder.
        pending = state["pending_operation"]
        if pending and event.get("operation_id") == pending["id"]:
            before = state.get("_pending_before", {})
            if (
                event.get("no_motion_started", False)
                and before.get("revision") == state["revision"]
            ):
                state["location"] = before["location"]
                state["basis"] = before["basis"]
                state["_movement_at"] = before["movement_at"]
            elif not event.get("no_motion_started", False):
                invalidate()
            state["pending_operation"] = None
    elif kind in {"partial", "unknown", "conflict"}:
        if event.get("scope") == "entry":
            state["location"]["inside_furnace"] = None
        else:
            invalidate()
        if event.get("scope") == "entry":
            state["_entry_at"] = max(state.get("_entry_at") or 0, _time(now))
        else:
            state["_movement_at"] = max(barrier or 0, _time(now))
        if kind == "conflict":
            state["conflict"] = event.get("reason", "Conflicting sample evidence.")
    elif kind == "entry":
        if event.get("inside_furnace") is not None and type(event["inside_furnace"]) is not bool:
            raise ValueError("Furnace entry must be true, false or null.")
        # Entry evidence has its own time barrier; holder evidence is independent.
        captured_entry = _time(event.get("observed_at"))
        entry_barrier = max(state.get("_entry_at") or 0, barrier or 0)
        valid = all(
            event.get(key) is True for key in ("mapping_valid", "reference_valid", "aligned")
        )
        if not valid:
            state["location"]["inside_furnace"] = None
            state["_entry_at"] = max(entry_barrier, _time(now))
        elif captured_entry is not None and captured_entry >= entry_barrier:
            state["location"]["inside_furnace"] = (
                event.get("inside_furnace")
                if state["location"]["holder"] == "stage_insert"
                else None
            )
            state["_entry_at"] = captured_entry
    else:
        pending = state["pending_operation"]
        if kind == "completed":
            if not pending or event.get("operation_id") != pending["id"]:
                raise ValueError("Completion does not match the pending operation.")
        identity_conflict = (
            observed and observed.get("chip_id", state["chip_id"]) != state["chip_id"]
        )
        stale = (
            kind == "observation"
            and barrier is not None
            and (captured is None or captured < barrier)
        )
        same_time_conflict = (
            kind == "observation"
            and captured is not None
            and captured == barrier
            and event.get("location")
            and state["location"]["holder"] != "unknown"
            and location(event["location"]) != {**state["location"], "inside_furnace": None}
        )
        if identity_conflict or event.get("conflict") or same_time_conflict:
            invalidate()
            state["conflict"] = "Conflicting sample identity or location."
            state["_movement_at"] = max(
                barrier or 0, captured if captured is not None else _time(now)
            )
        elif not stale:
            target = event.get("location")
            basis = "unknown"
            if kind == "reported":
                if not event.get("actor"):
                    raise ValueError("A manual report requires an actor.")
                basis = "reported"
                state["reported_by"] = copy.deepcopy(event["actor"])
            elif observed and captured is not None and (barrier is None or captured >= barrier):
                exact = (
                    observed.get("identity_source") == "exact_id"
                    and observed.get("chip_id") == state["chip_id"]
                )
                continuity = (
                    observed.get("identity_source") == "assigned_slot_continuity"
                    and observed.get("source_assignment") == state["chip_id"]
                    and observed.get("source_empty") is True
                    and observed.get("target_present") is True
                )
                if exact or continuity:
                    basis = "observed"
            if (
                basis == "unknown"
                and kind == "completed"
                and event.get("workflow_supports_location")
            ):
                basis = "inferred"
            if target and basis != "unknown":
                invalidate()
                state["location"] = location(target)
                state["basis"] = basis
                state["conflict"] = None
            elif kind != "observation":
                invalidate()
            if kind in {"completed", "reported"}:
                evidence_time = captured if basis == "observed" else _time(now)
                state["_movement_at"] = max(barrier or 0, evidence_time)
                state["pending_operation"] = None
            elif captured is not None:
                state["_movement_at"] = max(captured, barrier or captured)
    if event.get("actor"):
        state["updated_by"] = copy.deepcopy(event["actor"])
    state["revision"] += 1
    state["updated_at"] = now
    return state


class SampleTracker:
    """Persist event and current state in one SQLite transaction."""

    def __init__(self, store):
        self.store = store

    @classmethod
    def memory(cls):
        """Build a portable store for demo state and reducer checks."""
        import sqlite3
        import threading
        from types import SimpleNamespace

        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, chip_id TEXT, status TEXT);
            CREATE TABLE steps (run_id TEXT, position INTEGER, skill TEXT,
                chip_id TEXT, status TEXT, started_at REAL);
            CREATE TABLE samples (chip_id TEXT);
            CREATE TABLE artifacts (chip_id TEXT);
            CREATE TABLE events (chip_id TEXT);
        """)
        tracker = cls(SimpleNamespace(_db=db, _lock=threading.RLock()))
        tracker.initialize()
        tracker._owns_store = True
        return tracker

    def close(self):
        """Close only the SQLite connection created by memory()."""
        if getattr(self, "_owns_store", False):
            self.store._db.close()
            self._owns_store = False

    def initialize(self):
        with self.store._lock, self.store._db:
            self.store._db.executescript("""
                CREATE TABLE IF NOT EXISTS chip_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    chip_id TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chip_events_chip ON chip_events(chip_id, sequence);
                CREATE TABLE IF NOT EXISTS sample_current (
                    chip_id TEXT PRIMARY KEY, state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sample_revision (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO sample_revision VALUES(1,0);
            """)

            tables = {
                row[0]
                for row in self.store._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("runs", "steps", "samples", "artifacts", "events"):
                if table in tables:
                    self.store._db.execute(
                        f"CREATE INDEX IF NOT EXISTS sample_{table}_chip ON {table}(chip_id)"
                    )
            if "steps" in tables:
                self.store._db.execute(
                    "CREATE INDEX IF NOT EXISTS sample_steps_started "
                    "ON steps(chip_id,started_at DESC,run_id DESC,position DESC)"
                )

    def _read(self, chip_id):
        if not self.store._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sample_current'"
        ).fetchone():
            return new_sample(chip_id)
        row = self.store._db.execute(
            "SELECT state_json FROM sample_current WHERE chip_id=?", (chip_id,)
        ).fetchone()
        return json.loads(row[0]) if row else new_sample(chip_id)

    def _save(self, state):
        self.store._db.execute(
            "INSERT OR REPLACE INTO sample_current VALUES(?,?)",
            (state["chip_id"], json.dumps(state, allow_nan=False)),
        )
        self.store._db.execute("UPDATE sample_revision SET revision=revision+1 WHERE singleton=1")

    def record_event(self, chip_id, kind, *, event_id=None, **payload):
        from orchestrator.store import identifier

        if getattr(self.store, "readonly", False):
            raise ValueError("Sample store is read-only.")
        identifier(chip_id)
        if len(chip_id) > 256:
            raise ValueError("Chip ID exceeds 256 characters.")
        event_id = event_id or uuid.uuid4().hex
        identifier(event_id)
        if len(event_id) > 256:
            raise ValueError("Event ID exceeds 256 characters.")
        actor = payload.get("actor")
        if actor is not None:
            identity = actor.get("id") if isinstance(actor, dict) else actor
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
                raise ValueError("An actor requires a nonempty identity.")
        if payload.get("location") is not None:
            location(payload["location"])
        if kind not in KINDS:
            raise ValueError("Unknown chip event kind.")
        with self.store._lock, self.store._db:
            self.store._db.execute("BEGIN IMMEDIATE")
            old = self.store._db.execute(
                "SELECT data_json FROM chip_events WHERE event_id=?", (event_id,)
            ).fetchone()
            event = {**payload, "chip_id": chip_id, "kind": kind, "event_id": event_id}
            if old:
                previous = json.loads(old[0])
                event.setdefault("recorded_at", previous["recorded_at"])
                if event != previous:
                    raise ValueError("Event ID already has different content.")
                return self._public(self._read(chip_id))
            event.setdefault("recorded_at", time.time())
            _time(event["recorded_at"])
            state = reduce_sample(self._read(chip_id), event)
            self.store._db.execute(
                "INSERT INTO chip_events(event_id,chip_id,kind,data_json,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (
                    event_id,
                    chip_id,
                    kind,
                    json.dumps(event, allow_nan=False),
                    _time(event["recorded_at"]),
                ),
            )
            self._save(state)
            return self._public(state)

    def manual_report(self, chip_id, *, actor, location=None, unknown=False, event_id=None):
        if not actor:
            raise ValueError("A manual report requires an actor.")
        if not unknown and location is None:
            raise ValueError("A manual report requires a location.")
        return self.record_event(
            chip_id,
            "unknown" if unknown else "reported",
            actor=actor,
            location=location,
            event_id=event_id,
        )

    @staticmethod
    def _public(state):
        return {
            key: copy.deepcopy(value) for key, value in state.items() if not key.startswith("_")
        }

    def _history_state(self, chip_id, tables):
        state = self._read(chip_id)
        if {"runs", "steps"} <= tables:
            step = self.store._db.execute(
                "SELECT s.*, r.status AS run_status FROM steps s JOIN runs r "
                "ON r.id=s.run_id WHERE s.chip_id=? AND s.started_at IS NOT NULL "
                "AND s.status != 'pending' ORDER BY s.started_at DESC,"
                "s.run_id DESC,s.position DESC LIMIT 1",
                (chip_id,),
            ).fetchone()
            if step:
                state["last_run"] = {"id": step["run_id"], "status": step["run_status"]}
                state["last_step"] = {
                    "run_id": step["run_id"],
                    "index": step["position"],
                    "name": step["skill"],
                    "status": step["status"],
                }
        return state

    @staticmethod
    def _selection_sql(selection, tables):
        if selection == "all":
            sources = [
                table
                for table in (
                    "runs",
                    "steps",
                    "samples",
                    "artifacts",
                    "events",
                    "chip_events",
                    "sample_current",
                )
                if table in tables
            ]
            queries = [
                f"SELECT chip_id FROM {table} WHERE chip_id IS NOT NULL AND trim(chip_id) != ''"
                for table in sources
            ]
        else:
            queries = []
            if "sample_current" in tables:
                queries.append(
                    "SELECT chip_id FROM sample_current "
                    "WHERE json_type(state_json, '$.pending_operation')='object'"
                )
            if {"runs", "steps"} <= tables:
                queries.append(
                    "SELECT DISTINCT s.chip_id FROM steps s JOIN runs r ON r.id=s.run_id "
                    "WHERE r.status IN ('running','paused') AND s.chip_id IS NOT NULL "
                    "AND trim(s.chip_id) != '' AND s.started_at IS NOT NULL "
                    "AND s.status != 'pending' AND s.rowid=(SELECT latest.rowid FROM steps latest "
                    "WHERE latest.chip_id=s.chip_id AND latest.started_at IS NOT NULL "
                    "AND latest.status != 'pending' ORDER BY latest.started_at DESC,"
                    "latest.run_id DESC,latest.position DESC LIMIT 1)"
                )
        return " UNION ".join(queries) if queries else "SELECT NULL AS chip_id WHERE 0"

    def samples(self, selection="active", chip_ids=(), limit=100, offset=0, expected_revision=None):
        """Read one bounded page from one SQLite snapshot.

        Pass the first page's revision for later pages to reject changed selections.
        Exact-ID selection reads only those IDs and includes unknown placeholders.
        """
        from orchestrator.store import identifier

        if selection not in {"active", "all", "ids"}:
            raise ValueError("Unknown sample selection.")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 1000000
        ):
            raise ValueError("Invalid sample page.")
        if not isinstance(chip_ids, (list, tuple)) or len(chip_ids) > 50:
            raise ValueError("Select at most 50 chip IDs.")
        for chip_id in chip_ids:
            identifier(chip_id)
            if len(chip_id) > 256:
                raise ValueError("Chip ID exceeds 256 characters.")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("Invalid sample revision.")
        with self.store._lock:
            db = self.store._db
            db.execute("SAVEPOINT sample_snapshot")
            try:
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                revision = (
                    db.execute("SELECT revision FROM sample_revision WHERE singleton=1").fetchone()[
                        0
                    ]
                    if "sample_revision" in tables
                    else 0
                )
                if expected_revision is not None and expected_revision != revision:
                    raise ValueError("Sample revision changed. Restart pagination.")
                if selection == "ids":
                    requested = list(dict.fromkeys(chip_ids))
                    total = len(requested)
                    selected = requested[offset : offset + limit]
                else:
                    query = self._selection_sql(selection, tables)
                    total = db.execute(f"SELECT count(*) FROM ({query})").fetchone()[0]
                    selected = [
                        row[0]
                        for row in db.execute(
                            f"SELECT chip_id FROM ({query}) ORDER BY chip_id LIMIT ? OFFSET ?",
                            (limit, offset),
                        )
                    ]
                items = [self._public(self._history_state(chip_id, tables)) for chip_id in selected]
                complete = offset + len(items) >= total
                return {
                    "revision": revision,
                    "selection": selection,
                    "items": items,
                    "count": len(items),
                    "total": total,
                    "complete": complete,
                    "next_offset": None if complete else offset + len(items),
                }
            finally:
                db.execute("RELEASE SAVEPOINT sample_snapshot")
