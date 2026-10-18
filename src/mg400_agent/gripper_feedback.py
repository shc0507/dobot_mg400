"""Validate measured gripper positions received through WebRTC or a shared file."""

import json
import math
import time
from pathlib import Path
from uuid import UUID


class FeedbackState:
    """Keep the latest valid sample and reject delayed or repeated samples."""

    def __init__(self):
        self._latest = None
        self._received = None
        self._session = None
        self._sequence = -1
        self._retired_sessions = set()

    def receive(self, payload):
        """Return whether this sample is valid. Every rejection clears freshness."""
        self._latest = self._received = None
        if not isinstance(payload, dict) or not isinstance(payload.get("session"), str):
            return False
        try:
            session = str(UUID(payload["session"]))
        except ValueError:
            return False
        sequence = payload.get("seq")
        if type(sequence) is not int or sequence < 0:
            return False
        if session in self._retired_sessions:
            return False
        if session == self._session and sequence <= self._sequence:
            return False
        sent_at = payload.get("sent_at")
        if type(sent_at) not in (int, float):
            return False
        try:
            sent_at = float(sent_at)
        except OverflowError:
            return False
        if not math.isfinite(sent_at) or abs(time.time() - sent_at) > 2.0:
            return False
        for key, lower, upper in (("init", 1, 1), ("state", 0, 2), ("position", 0, 1000)):
            value = payload.get(key)
            if type(value) is not int or not lower <= value <= upper:
                return False
        if session != self._session:
            if self._session is not None:
                self._retired_sessions.add(self._session)
            self._session = session
        self._sequence = sequence
        self._latest = {
            key: payload[key] for key in ("session", "seq", "sent_at", "init", "state", "position")
        }
        self._received = time.monotonic()
        return True

    def get(self, now=None):
        """Return a sample copy only while its monotonic age is at most 0.75 seconds."""
        if self._latest is None:
            return None
        if now is None:
            now = time.monotonic()
        if not 0 <= now - self._received <= 0.75:
            return None
        return self._latest.copy()


class FileFeedback:
    """Read new samples without making an unchanged file fresh again."""

    def __init__(self, path, feedback):
        self.path = Path(path)
        self.feedback = feedback
        self._previous = None

    def poll(self):
        try:
            content = self.path.read_text()
            if content == self._previous:
                return
            self._previous = content
            payload = json.loads(content)
        except (OSError, ValueError):
            self._previous = None
            self.feedback.receive(None)
            return
        self.feedback.receive(payload)
