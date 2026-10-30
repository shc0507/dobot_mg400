"""Bounded event replay and matching JSON records in the service journal."""

import json
import logging
import threading
import time
from collections import deque


class EventLog:
    def __init__(self, maxlen=1000, *, clock=time.time, logger=None):
        if isinstance(maxlen, bool) or not isinstance(maxlen, int) or maxlen < 1:
            raise ValueError("Event capacity must be a positive integer.")
        self._events = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self.clock = clock
        self.logger = logger if logger is not None else logging.getLogger("labkit.events")

    def add(self, level, message, **fields):
        """Record one event and return its sequence number."""
        levels = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
        if level not in levels or not isinstance(message, str):
            raise ValueError("Expected a logging level and a text message.")
        with self._lock:
            event = {
                "seq": self._seq + 1,
                "ts_unix": self.clock(),
                "level": level,
                "message": message,
                "fields": fields,
            }
            line = json.dumps(event, ensure_ascii=True, allow_nan=False, sort_keys=True)
            self._events.append(json.loads(line))
            self._seq += 1
            self.logger.log(levels[level], line)
            return self._seq

    def since(self, seq=0):
        """Return events after seq and an atomic replay cursor, including after eviction."""
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("Event sequence must be a nonnegative integer.")
        with self._lock:
            return {
                "events": json.loads(
                    json.dumps([event for event in self._events if event["seq"] > seq])
                ),
                "next": self._seq,
            }
