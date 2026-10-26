"""Keep current keyboard input separate from finite motion tasks."""

import asyncio
import threading
import time

KEYS = frozenset("WASDQERFZX")


class GripperUnavailableError(RuntimeError):
    """The gripper cannot start this step. No command was sent."""


class KeyboardController:
    """Dispatch one hold at a time and reconsider input after it finishes.

    run_hold must return only after motion and stop verification finish.
    Each press can start one hold. A completed hold needs a new press.
    Call methods on the event loop, except revoke(), which also permits worker threads.
    """

    def __init__(self, run_hold, release_hold, can_start, on_start):
        self._run_hold = run_hold
        self._release_hold = release_hold
        self._can_start = can_start
        self._on_start = on_start
        self._enabled = False
        self._revoked = threading.Event()
        self._revoked.set()
        self._generation = 0
        self._in_flight = False
        self._pending = set()
        self.held_keys = set()
        self.active_key = None
        self.released = None
        self.task = None

    def set_enabled(self, enabled):
        self._generation += 1
        self._enabled = enabled
        self.held_keys.clear()
        self._pending.clear()
        self.active_key = None
        if enabled:
            self._revoked.clear()
        else:
            self.revoke()

    def release(self):
        """Cancel the active hold without discarding a newer press."""
        if self.released is not None:
            self.released.set()
        self._release_hold()

    def revoke(self):
        """Block new holds immediately, including before event-loop cleanup."""
        self._revoked.set()
        self.release()

    def key_event(self, key, pressed, *, repeat=False):
        if key not in KEYS:
            return
        if not pressed:
            self.held_keys.discard(key)
            self._pending.discard(key)
            if key == self.active_key:
                self.release()
                self.active_key = None
            self._reconcile()
            return
        if not self._enabled or self._revoked.is_set() or repeat or key in self.held_keys:
            return
        self.held_keys.add(key)
        self._pending.add(key)
        if len(self.held_keys) != 1:
            self.release()
        self._reconcile()

    def _reconcile(self):
        if not self._enabled or self._revoked.is_set():
            return
        if self._in_flight:
            return
        if len(self.held_keys) != 1:
            return
        key = next(iter(self.held_keys))
        if key not in self._pending or not self._can_start():
            return
        if not self._enabled or self._revoked.is_set():
            return
        self._pending.remove(key)
        self.active_key = key
        self.released = threading.Event()
        generation = self._generation
        self._in_flight = True
        self.task = asyncio.ensure_future(self._run(key, self.released, generation))
        self.task.add_done_callback(self._complete)
        self._on_start(self.task)

    async def _run(self, key, released, generation):
        if self._revoked.is_set() or generation != self._generation:
            released.set()
            return
        await self._run_hold(key, released)

    def _complete(self, task):
        failed = task.cancelled() or task.exception() is not None
        if task is not self.task:
            return
        # Process failure before input events can start the next hold.
        self._in_flight = False
        if failed:
            self.set_enabled(False)
            return
        self._reconcile()


async def run_keyboard_hold(
    session, key, released, *, pose_measurement=None, pose_task=None, gripper=None
):
    """Wait for an arm hold to stop or for the current gripper step to finish."""
    if key in "ZX":
        expired = await _jog_gripper(session.control, gripper, key, released)
        return None, expired
    initial_pose = None
    if key not in "AD":
        if session.reusable_pose(pose_measurement):
            initial_pose = pose_measurement
        elif pose_task:
            initial_pose = await pose_task
    pose = await asyncio.to_thread(session.run, key, released, initial_pose=initial_pose)
    return pose, False


async def _jog_gripper(control, gripper, key, released):
    if gripper is None or gripper.closed:
        raise GripperUnavailableError("Gripper control is unavailable.")
    deadline = time.monotonic() + 5.0
    while not released.is_set() and time.monotonic() < deadline:
        control.read_state()
        if gripper.closed:
            raise GripperUnavailableError("Gripper control is unavailable.")
        if gripper.failed:
            raise RuntimeError(gripper.report["result"])
        if gripper.busy:
            raise RuntimeError("Wait for the current gripper command to finish.")
        position = gripper.measured_position()
        if position is None:
            raise GripperUnavailableError(
                gripper.feedback.diagnostic() or "Waiting for fresh gripper position feedback."
            )
        target = max(0, min(1000, position + (25 if key == "Z" else -25)))
        if target == position:
            return
        if released.is_set():
            return
        action = gripper.submit_position(target)
        if action is None:
            if (
                not gripper.failed
                and not gripper.busy
                and gripper.report.get("command_published") is False
            ):
                raise GripperUnavailableError(gripper.report["result"])
            raise RuntimeError(gripper.report["result"])
        completed = await action
        if gripper.failed:
            raise RuntimeError(gripper.report["result"])
        if completed is False:
            raise GripperUnavailableError(gripper.report["result"])
        if gripper.report.get("contact_observed"):
            return
    return not released.is_set()
