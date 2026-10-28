"""Control the gripper through a collector file on the same host."""

import contextlib
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from mg400_agent.gripper_feedback import FeedbackState, FileFeedback


class GripperNotSentError(RuntimeError):
    """The adapter sent no command and did not lock."""


class GripperLockedError(RuntimeError):
    """The command failed after publication, or the adapter is locked."""


class WebGripper:
    """Send explicit commands. Never start the collector or initialize automatically."""

    def __init__(self, feedback_path, *, publication_guard=None):
        self.feedback_path = Path(feedback_path)
        self.publication_guard = publication_guard or contextlib.nullcontext
        self.feedback = FeedbackState()
        self._source = FileFeedback(self.feedback_path, self.feedback)
        self._state_lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._busy = self._locked = self._closed = False
        self._published = False
        self._message = ""
        self._outcome = None

    @property
    def closed(self):
        with self._state_lock:
            return self._closed

    @property
    def failed(self):
        """Whether an uncertain command result locked this adapter."""
        with self._state_lock:
            return self._locked

    @property
    def last_outcome(self):
        """Return "arrived" or "contact" for the last confirmed position command."""
        with self._state_lock:
            return self._outcome

    def poll(self):
        """Read feedback without sending a command."""
        with self._state_lock:
            self._source.poll()

    def _capability_error(self, sample, operation):
        if sample is None:
            return "USB feedback is stale or unavailable. Check the collector."
        control = sample.get("control")
        protocols = (3,) if operation == "initialize" else (2, 3)
        if control is None or control["protocol"] not in protocols:
            return "Update the USB collector and start it with --commands."
        if control["locked"]:
            return control.get("error") or "The collector is locked."
        return None

    def _request_error(self, sample, operation):
        if self._closed:
            return "Gripper control is closed."
        if self._locked:
            return self._message
        error = self._capability_error(sample, operation)
        if error:
            return error
        if operation == "initialize" and sample["init"] == 1:
            return "The gripper is already initialized."
        if sample["init"] == 2:
            return "Gripper initialization is in progress."
        if operation == "position" and sample["init"] != 1:
            return "The gripper is not initialized."
        return None

    def measure(self):
        """Read feedback once. Return the position and the reason when it is unavailable."""
        with self._state_lock:
            self.poll()
            sample = self.feedback.get_status()
            error = self._request_error(sample, "position")
            if error is None:
                return sample["position"], None
            return None, error

    def snapshot(self):
        """Return current measurements and permitted gripper actions."""
        with self._state_lock:
            sample = self.feedback.get_status()
            position_error = self._request_error(sample, "position")
            initialization_error = self._request_error(sample, "initialize")
            collector_locked = bool(sample and sample.get("control", {}).get("locked"))
            return {
                "position": sample["position"] if sample and sample["init"] == 1 else None,
                "available": not self._busy and position_error is None,
                "can_initialize": not self._busy and initialization_error is None,
                "busy": self._busy,
                "locked": self._locked or collector_locked,
                "message": (
                    self._message
                    if self._busy or self._locked
                    else position_error or self._message or "The gripper is ready."
                ),
            }

    def move(self, position):
        """Wait for arrival or object contact at an opening from 0 to 1000."""
        if type(position) is not int or not 0 <= position <= 1000:
            raise ValueError("The target position must be an integer from 0 to 1000.")
        return self._perform("position", position)

    def initialize(self):
        """Wait for explicit initialization. Initialization opens the jaws."""
        return self._perform("initialize")

    def _perform(self, operation, position=None):
        if not self._action_lock.acquire(blocking=False):
            raise RuntimeError("A gripper command is in progress.")
        temporary = None
        try:
            with self._state_lock:
                self._published = False
                self._outcome = None
                self.poll()
                initial = self.feedback.get_status()
                error = self._request_error(initial, operation)
                if error:
                    raise RuntimeError(error)
                self._busy = True
                self._message = (
                    "Initializing gripper. The jaws will open."
                    if operation == "initialize"
                    else "The gripper position command is in progress."
                )
            request_id = str(uuid4())
            deadline = time.monotonic() + (15 if operation == "initialize" else 5)
            request = {
                "id": request_id,
                "session": initial["session"],
                "sent_at": time.time(),
                "deadline_monotonic": deadline,
            }
            if operation == "initialize":
                request["operation"] = operation
            else:
                request["position"] = position
            path = self.feedback_path.with_name(self.feedback_path.name + ".command.json")
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(request) + "\n")
            # The caller's guard must precede the adapter lock, as snapshot calls do.
            with self.publication_guard():
                with self._state_lock:
                    self.poll()
                    current = self.feedback.get_status()
                    error = self._request_error(current, operation)
                    if error or current["session"] != initial["session"]:
                        raise RuntimeError(error or "The USB collector session changed.")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "The gripper command deadline expired before publication."
                        )
                    # A failed replace can leave the publication result unknown.
                    self._published = None
                    temporary.replace(path)
                    self._published = True
            return self._wait(operation, position, initial, request_id, deadline)
        except Exception as error:
            with self._state_lock:
                failure = GripperLockedError
                if self._published is not False:
                    self._locked = True
                    self._message = f"Control is locked. Check the gripper. {error}"
                elif not self._locked:
                    self._message = f"No command sent. {error}"
                    failure = GripperNotSentError
                message = self._message
            raise failure(message) from error
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
            with self._state_lock:
                self._busy = False
            self._action_lock.release()

    def _wait(self, operation, position, initial, request_id, deadline):
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Gripper control closed before the result was confirmed.")
                if time.monotonic() >= deadline:
                    break
                self.poll()
                sample = self.feedback.get_status()
                if sample is None:
                    continue
                if sample["session"] != initial["session"]:
                    raise RuntimeError("The USB collector session changed.")
                error = self._capability_error(sample, operation)
                if error:
                    raise RuntimeError(error)
                if operation == "position" and sample["init"] != 1:
                    raise RuntimeError(self.feedback.diagnostic())
                control = sample["control"]
                if (
                    sample["seq"] <= initial["seq"]
                    or control["command_id"] != request_id
                    or not control["ack"]
                ):
                    continue
                if operation == "initialize":
                    if (
                        control.get("operation") == operation
                        and sample["init"] == 1
                        and sample["state"] == 1
                        and 0 <= sample["position"] <= 1000
                    ):
                        self._message = "Gripper initialization complete."
                        return True
                elif control["target"] == position:
                    if sample["state"] == 1 and abs(sample["position"] - position) <= 5:
                        self._message = "Measured position is within 5 / 1000 of the target."
                        self._outcome = "arrived"
                        return True
                    if sample["state"] == 2:
                        self._message = "The gripper reports object contact before the target."
                        self._outcome = "contact"
                        return True
        raise TimeoutError("The gripper command result is not confirmed.")

    def close(self):
        """Stop waiting and reject new commands. This does not halt the gripper."""
        with self._state_lock:
            self._closed = True
