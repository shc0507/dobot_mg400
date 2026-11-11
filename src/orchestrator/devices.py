"""One reader per lab device. Every console and labctl state read uses this cache.

Each reader thread reads `api/state` every 250 ms, `describe` at start and when
`state.instance` changes, and the event replay every 1 s. The request load is
fixed, whatever the number of pages and agents.
"""

import copy
import threading
import time
import uuid

from .clients import SERVICES

STATE_S = 0.25
EVENTS_S = 1.0
STALE_S = 3
# State history: a row at each phase or normalized change, else at most one per SAMPLE_S.
SAMPLE_S = 10.0
KEEP_S = 30 * 24 * 3600
PRUNE_S = 3600.0
VOLATILE = ("ts_unix", "seq")


def _flag(value):
    return value if type(value) is bool else None


def normalize(name, state, description):
    """Return the common device row. The robot state uses its own field names."""
    lease = state.get("lease")
    holder = lease.get("holder") if isinstance(lease, dict) else None
    demo = state.get("demo", description.get("demo"))
    row = {
        "connected": _flag(state.get("connected")),
        "busy": _flag(state.get("busy")),
        "locked": _flag(state.get("locked")),
        "fault": None
        if "fault" in state and state["fault"] is None
        else "Service reports a fault or unknown fault state.",
        "holder": holder,
        "demo": _flag(demo),
    }
    if name == "robot":
        flags = [state.get("syncing"), state.get("feedback_fresh")]
        row["connected"] = all(flags) if all(type(flag) is bool for flag in flags) else None
        row["holder"] = state.get("controller_id")
        # An arm without feedback has no error status: that is not a fault.
        error = state.get("robot_error")
        row["fault"] = (
            "Robot error or stop confirmation is unknown."
            if error is True
            or (error is not False and row["connected"] is not False)
            or state.get("alarms")
            or state.get("stop_unconfirmed") is not False
            else None
        )
    return row


class Devices:
    """The device cache. Readers start at the first read and stop at close."""

    def __init__(self, service, *, clock=time.monotonic, on_events=None):
        self.service, self.clock = service, clock
        # on_events(name, adapter) reads the event replay with the reader's adapter.
        self.on_events = on_events
        self.lock = threading.Lock()
        self.closing = threading.Event()
        self.threads = []
        self.entries = {name: self._entry("loading") for name in SERVICES}
        self.reads = dict.fromkeys(SERVICES, 0)
        self.changed = threading.Condition(self.lock)
        self.wakes = {name: threading.Event() for name in SERVICES}
        self.recorded = {}
        self.pruned_at = None

    @staticmethod
    def _entry(phase, **values):
        return {
            "phase": phase,
            "state": None,
            "describe": None,
            "observed": None,
            "updated_at": None,
            **values,
        }

    def start(self):
        with self.lock:
            if self.threads or self.closing.is_set():
                return
            for name in SERVICES:
                worker = threading.Thread(
                    target=self._run, args=(name,), name="device-" + name, daemon=True
                )
                self.threads.append(worker)
                worker.start()

    def close(self):
        self.closing.set()
        for wake in self.wakes.values():
            wake.set()
        with self.lock:
            self.changed.notify_all()
            workers = list(self.threads)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(5)

    def _store(self, name, entry):
        with self.lock:
            self.entries[name] = entry
            self.reads[name] += 1
            self.changed.notify_all()
        self._record(name, entry)

    def _record(self, name, entry):
        """Write the state history row of one read when it is due. Only this reader
        thread writes self.recorded[name]."""
        store = getattr(self.service, "store", None)
        if store is None:
            return
        state = entry["state"]
        row = normalize(name, state, entry["describe"]) if entry["phase"] == "ready" else {}
        key = (entry["phase"], row)
        values = {k: v for k, v in state.items() if k not in VOLATILE} if state else None
        last = self.recorded.get(name)
        now = time.time()
        if last and last[0] == key and (last[1] == values or now - last[2] < SAMPLE_S):
            return
        try:
            store.add_device_state(name, now, entry["phase"], row, state)
            with self.lock:
                prune = self.pruned_at is None or now - self.pruned_at >= PRUNE_S
                if prune:
                    self.pruned_at = now
            if prune:
                store.prune_devices(now - KEEP_S)
        except Exception:
            return  # The store is closed or unavailable; the next read tries again.
        self.recorded[name] = (key, values, now)

    def _run(self, name):
        adapter = description = None
        events_at = None
        while not self.closing.is_set():
            try:
                if not self.service.dashboard_configured(name):
                    self._close(adapter)
                    adapter = description = None
                    self._store(name, self._entry("unconfigured"))
                else:
                    if adapter is None:
                        # The console profile also reads the demo event replay.
                        adapter = self.service.console_client(
                            name, "console-cache-" + uuid.uuid4().hex
                        )
                    if adapter is None:
                        # Demo: the owned demo service starts at Take control.
                        self._store(name, self._entry("offline"))
                    else:
                        state = adapter.state()
                        if not isinstance(state, dict):
                            raise ValueError("Invalid service state.")
                        if description is None or state.get("instance") != description[0]:
                            value = adapter.describe()
                            if not isinstance(value, dict):
                                raise ValueError("Invalid service description.")
                            description = (state.get("instance"), value)
                        self._store(
                            name,
                            self._entry(
                                "ready",
                                state=state,
                                describe=description[1],
                                observed=self.clock(),
                                updated_at=time.time(),
                            ),
                        )
                        now = self.clock()
                        if self.on_events and (events_at is None or now - events_at >= EVENTS_S):
                            events_at = now
                            self.on_events(name, adapter)
            except Exception:
                self._close(adapter)
                adapter = description = None
                self._store(name, self._entry("unavailable"))
            self.wakes[name].wait(STATE_S)
            self.wakes[name].clear()
        self._close(adapter)

    @staticmethod
    def _close(adapter):
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    def get(self, name):
        """Return the cached row of one device: phase, age_s, state, describe and the
        normalized fields. A ready row older than STALE_S has the phase "stale"."""
        with self.lock:
            entry = copy.deepcopy(self.entries[name])
        observed = entry.pop("observed")
        entry["age_s"] = None if observed is None else max(0.0, self.clock() - observed)
        if entry["phase"] == "ready":
            if entry["age_s"] > STALE_S:
                entry["phase"] = "stale"
            entry.update(normalize(name, entry["state"], entry["describe"]))
        else:
            entry.update(dict.fromkeys(("connected", "busy", "locked", "fault", "holder", "demo")))
        return entry

    def wait(self, name, timeout=1.0):
        """Return the row; wait up to timeout for the first read after start."""
        self.start()
        with self.changed:
            self.changed.wait_for(lambda: self.reads[name] or self.closing.is_set(), timeout)
        return self.get(name)

    def fresh(self, name, timeout=1.0):
        """Return a row from a read that started after this call."""
        self.start()
        deadline = time.monotonic() + timeout
        with self.changed:
            # The read in progress can predate this call, so wait for the one after it.
            target = self.reads[name] + 2
            while self.reads[name] < target and not self.closing.is_set():
                self.wakes[name].set()
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.changed.wait(left)
        return self.get(name)
