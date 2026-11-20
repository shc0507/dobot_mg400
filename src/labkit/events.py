"""Bounded event replay and matching JSON records in the service journal."""

import json
import logging
import threading
import time
import uuid
from collections import deque


class EventLog:
    def __init__(self, maxlen=1000, *, clock=time.time, logger=None, instance=None):
        if isinstance(maxlen, bool) or not isinstance(maxlen, int) or maxlen < 1:
            raise ValueError("Event capacity must be a positive integer.")
        self._events = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self.instance = uuid.uuid4().hex if instance is None else self._validate_instance(instance)
        self.clock = clock
        self.logger = logger if logger is not None else logging.getLogger("labkit.events")

    @staticmethod
    def _validate_instance(instance):
        if (
            not isinstance(instance, str)
            or not 1 <= len(instance) <= 128
            or not instance.isascii()
            or not instance.isprintable()
        ):
            raise ValueError("Event instance must contain 1 to 128 printable ASCII characters.")
        return instance

    def add(self, level, message, **fields):
        """Record one event and return its sequence number."""
        levels = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
        if level not in levels or not isinstance(message, str):
            raise ValueError("Expected a logging level and a text message.")
        with self._lock:
            event = {
                "instance": self.instance,
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

    def since(self, seq=0, *, instance=None):
        """Return events after seq and an atomic replay cursor, including after eviction."""
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("Event sequence must be a nonnegative integer.")
        if instance is not None:
            self._validate_instance(instance)
        with self._lock:
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            restarted = instance is not None and instance != self.instance
            reason = (
                "instance_changed"
                if restarted
                else "cursor_ahead"
                if seq > self._seq
                else "history_evicted"
                if seq < oldest - 1
                else None
            )
            # Keep the legacy sequence-only selection. A supplied old process ID
            # requests the new process's retained history, even when its counter
            # has already advanced beyond the old cursor.
            after = 0 if restarted else seq
            return {
                "events": json.loads(
                    json.dumps([event for event in self._events if event["seq"] > after])
                ),
                "next": self._seq,
                "instance": self.instance,
                "oldest": oldest,
                "gap": reason is not None,
                "gap_reason": reason,
                "restarted": restarted,
            }
