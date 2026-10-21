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
        self._diagnostic = None
        self._received = None
        self._session = None
        self._sequence = -1
        self._retired_sessions = set()

    def receive(self, payload):
        """Return whether the sample can update position."""
        previous = self._latest, self._diagnostic, self._received
        self._latest = self._diagnostic = self._received = None
        if not isinstance(payload, dict) or not isinstance(payload.get("session"), str):
            return False
        try:
            session = str(UUID(payload["session"]))
        except ValueError:
            return False
        sequence = payload.get("seq")
        if type(sequence) is not int or sequence < 0:
            return False
        if session in self._retired_sessions or (
            session == self._session and sequence <= self._sequence
        ):
            self._latest, self._diagnostic, self._received = previous
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
        for key, lower, upper in (("init", 0, 2), ("state", 0, 65535), ("position", 0, 65535)):
            value = payload.get(key)
            if type(value) is not int or not lower <= value <= upper:
                return False
        if payload["init"] == 1 and (payload["state"] > 3 or payload["position"] > 1000):
            return False
        control = payload.get("control")
        if control is not None:
            if not isinstance(control, dict) or type(control.get("protocol")) is not int:
                return False
            if control["protocol"] != 1:
                return False
            if any(type(control.get(key)) is not bool for key in ("ack", "locked")):
                return False
            if control.get("io_mode") is not None and (
                type(control["io_mode"]) is not int or control["io_mode"] not in (0, 1)
            ):
                return False
            command_id, target = control.get("command_id"), control.get("target")
            if command_id is not None:
                try:
                    if not isinstance(command_id, str) or str(UUID(command_id)) != command_id:
                        return False
                except ValueError:
                    return False
                if type(target) is not int or not 0 <= target <= 1000:
                    return False
            elif target is not None or control["ack"]:
                return False
            if control.get("error") is not None and not isinstance(control["error"], str):
                return False
        if session != self._session:
            if self._session is not None:
                self._retired_sessions.add(self._session)
            self._session = session
        self._sequence = sequence
        self._received = time.monotonic()
        if payload["init"] != 1:
            self._diagnostic = (
                "The gripper is not initialized."
                if payload["init"] == 0 else "Gripper initialization is in progress."
            )
            return False
        self._latest = {
            key: payload[key] for key in ("session", "seq", "sent_at", "init", "state", "position")
        }
        if control is not None:
            self._latest["control"] = control.copy()
        return True

    def diagnostic(self, now=None):
        """Return the initialization status only while its sample is fresh."""
        if self._diagnostic is None:
            return None
        if now is None:
            now = time.monotonic()
        if not 0 <= now - self._received <= 0.75:
            return None
        return self._diagnostic

    def get(self, now=None):
        """Return a sample copy only while its monotonic age is at most 0.75 seconds."""
        if self._latest is None:
            return None
        if now is None:
            now = time.monotonic()
        if not 0 <= now - self._received <= 0.75:
            return None
        sample = self._latest.copy()
        if "control" in sample:
            sample["control"] = sample["control"].copy()
        return sample


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
