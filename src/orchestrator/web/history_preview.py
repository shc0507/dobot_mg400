"""Bounded, in-memory device history for the offline Console preview."""

import copy
import threading
from collections import deque


class PreviewHistory:
    """Implement the Console history reads without the host's POSIX file store."""

    def __init__(self):
        self.lock = threading.RLock()
        self.events = deque(maxlen=1000)
        self.states = deque(maxlen=1000)
        self.sequence = 0

    def _append(self, rows, value):
        self.sequence += 1
        rows.append(copy.deepcopy(dict(value, id=self.sequence)))

    def add_device_events(self, rows):
        with self.lock:
            for device, seq, ts, level, message, fields in rows:
                if any(row["device"] == device and row["seq"] == seq for row in self.events):
                    continue
                self._append(
                    self.events,
                    dict(
                        device=device, seq=seq, ts=ts, level=level, message=message, fields=fields
                    ),
                )

    def add_device_state(self, device, ts, phase, row, state):
        with self.lock:
            self._append(self.states, dict(device=device, ts=ts, phase=phase, row=row, state=state))

    def _rows(self, rows, *, device=None, since=0, start=None, end=None, limit=500):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000.")
        with self.lock:
            selected = [
                row
                for row in rows
                if row["id"] > since
                and (device is None or row["device"] == device)
                and (start is None or row["ts"] >= start)
                and (end is None or row["ts"] <= end)
            ]
            selected.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
            return copy.deepcopy(selected[:limit])

    def device_events(self, **query):
        return self._rows(self.events, **query)

    def device_states(self, **query):
        return self._rows(self.states, **query)

    def prune_devices(self, before):
        with self.lock:
            for rows in (self.events, self.states):
                retained = [row for row in rows if row["ts"] >= before]
                rows.clear()
                rows.extend(retained)
