"""Control the robot from the web panel without a simulator dependency."""

import threading
import time
from contextlib import contextmanager

from .config import finite_number
from .controller_ownership import ControllerOwnership
from .feedback import FeedbackConnection
from .joint_control import JointControl, PreparationRejectedError, TargetRejectedError


class RobotPanelState:
    """Keep HTTP reads separate from robot operations and stop verification."""

    mode = "robot"
    LEASE_SECONDS = 3.0
    FEEDBACK_SECONDS = 0.25
    CONNECT_SECONDS = 1.5

    def __init__(
        self,
        host,
        *,
        gripper=None,
        connection_factory=FeedbackConnection,
        control_factory=JointControl,
    ):
        self.robot_host = host
        self._gripper = gripper
        self._connection_factory = connection_factory
        self._control_factory = control_factory
        self._ownership = ControllerOwnership(host)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._reader_stop = threading.Event()
        self._reader = None
        self._sample = None
        self._received = 0.0
        self._pose = [None] * 4
        self._syncing = False
        self._started = 0.0
        self._owner = None
        self._heartbeat = 0.0
        self._busy = False
        self._working = False
        self._job = None
        self._locked = False
        self._fault = False
        self._disable_verified = False
        self._safety_workers = 0
        self._safety_actions = set()
        self._disconnect_requested = False
        self._safety_threads = []
        self._gripper_active = False
        self._closing = False
        self._message = "Start sync to connect to the robot."
        self._grip = self._gripper.snapshot() if self._gripper else {}
        if self._gripper is not None:
            self._gripper.publication_guard = self._gripper_publication_guard
        self._control = self._new_control()
        self._worker = threading.Thread(target=self._work, name="MG400-web-worker", daemon=True)
        self._watchdog = threading.Thread(
            target=self._watch,
            name="MG400-web-watchdog",
            daemon=True,
        )
        self._worker.start()
        self._watchdog.start()

    def _new_control(self):
        return self._control_factory(
            self.robot_host,
            self._feedback_snapshot,
            send_guard=self._guard_send,
        )

    def _feedback_snapshot(self):
        with self._lock:
            if self._sample is None:
                raise RuntimeError("Robot feedback is absent")
            return self._sample, self._received

    def _fresh(self):
        return (
            self._syncing
            and self._sample is not None
            and (0 <= time.monotonic() - self._received <= self.FEEDBACK_SECONDS)
        )

    def snapshot(self):
        with self._lock:
            sample = self._sample
            return {
                "mode": "robot",
                "robot_host": self.robot_host,
                "actual": [
                    *([None] * 4 if self._busy else self._pose),
                    None if self._gripper_active else self._grip.get("position"),
                ],
                "joints": list(sample.joints_deg) if sample else None,
                "enabled": sample.mode in {5, 7} if sample else None,
                "syncing": self._syncing,
                "feedback_fresh": self._fresh(),
                "robot_mode": sample.mode if sample else None,
                "robot_error": sample.error_status if sample else None,
                "message": self._message,
                "busy": self._busy or bool(self._safety_workers),
                "locked": self._locked or bool(self._grip.get("locked")),
                "stop_unconfirmed": self._fault,
                "gripper_available": bool(self._grip.get("available")),
                "gripper_can_initialize": bool(self._grip.get("can_initialize")),
                "gripper_busy": self._gripper_active or bool(self._grip.get("busy")),
                "gripper_message": self._grip.get("message", "Gripper is not configured."),
                "controller_id": self._owner,
            }

    @staticmethod
    def _client(client_id):
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128:
            raise ValueError("A browser client ID is required")

    def heartbeat(self, client_id):
        self._client(client_id)
        with self._lock:
            self._expire_lease()
            if self._owner == client_id:
                self._heartbeat = time.monotonic()
        return self.snapshot()

    def _expire_lease(self):
        if self._owner is not None and time.monotonic() - self._heartbeat > self.LEASE_SECONDS:
            self._owner = None
            if self._syncing and not self._locked:
                if self._sample is None and not self._busy:
                    self._syncing = False
                    self._reader_stop.set()
                    self._message = "The browser control lease expired before feedback connected."
                else:
                    self._request_stop(reason="The browser control lease expired.")

    def _guard_send(self):
        with self._lock:
            if self._closing or self._locked or self._fault or self._control.stopped:
                raise RuntimeError("Robot control is locked. Disable the robot before recovery.")
            if self._owner is None or time.monotonic() - self._heartbeat > self.LEASE_SECONDS:
                raise RuntimeError("The browser control lease expired")
            if not self._fresh():
                raise RuntimeError("Robot feedback is absent or stale")
            if self._sample.error_status or self._sample.mode not in {4, 5, 7}:
                raise RuntimeError("Robot feedback reports an error or an unsupported mode")

    @contextmanager
    def _gripper_publication_guard(self):
        with self._lock:
            self._guard_send()
            if self._sample.mode not in {4, 5}:
                raise RuntimeError("The robot must be idle before a gripper command")
            yield

    def command(self, payload, client_id=None):
        self._client(client_id)
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object")
        action = payload.get("action")
        if not isinstance(action, str) or action not in {
            "start_sync",
            "stop_sync",
            "enable",
            "disable",
            "home",
            "execute",
            "stop",
            "grip",
            "initialize_gripper",
        }:
            raise ValueError("Unknown action")
        fields = {"action"}
        if action == "execute":
            fields.add("targets")
        elif action == "grip":
            fields.add("position")
        if set(payload) != fields:
            raise ValueError("Unexpected command fields")
        targets = self._targets(payload["targets"]) if action == "execute" else None
        if action == "grip":
            targets = (None, self._gripper_target(payload["position"]))
        with self._lock:
            if self._closing:
                raise RuntimeError("The panel is closing")
            self._expire_lease()
            if action in {"stop", "disable"}:
                self._request_stop(disabling=action == "disable")
            else:
                if self._owner is not None and self._owner != client_id:
                    raise PermissionError("Another browser controls the robot")
                if action == "start_sync":
                    self._start_sync()
                    self._owner = client_id
                    self._heartbeat = time.monotonic()
                else:
                    if self._owner != client_id:
                        raise PermissionError("Start sync in this browser before issuing commands")
                    self._heartbeat = time.monotonic()
                    if action == "stop_sync":
                        self._request_stop(
                            disabling=self._fresh() and self._sample.mode == 4,
                            disconnect=True,
                        )
                    else:
                        self._guard_send()
                        if self._busy or self._safety_workers:
                            raise RuntimeError("Another robot operation is in progress")
                        if action in {"home", "execute"}:
                            if self._sample.mode != 5:
                                raise RuntimeError("Enable the robot before movement")
                        if action == "initialize_gripper" and not self._grip.get("can_initialize"):
                            raise RuntimeError("Gripper initialization is unavailable")
                        if action == "initialize_gripper" and self._sample.mode not in {4, 5}:
                            raise RuntimeError(
                                "The robot must be idle before gripper initialization"
                            )
                        if action == "grip" and self._sample.mode not in {4, 5}:
                            raise RuntimeError("The robot must be idle before a gripper command")
                        if targets and targets[1] is not None and not self._grip.get("available"):
                            raise RuntimeError("Gripper control is unavailable")
                        if self._grip.get("locked"):
                            raise RuntimeError("Gripper control is locked")
                        self._busy = True
                        self._pose = [None] * 4
                        self._job = (action, targets)
                        self._message = f"Robot operation started: {action}."
                        self._wake.set()
        return self.snapshot()

    @staticmethod
    def _targets(values):
        if not isinstance(values, dict) or not {"x", "y", "z", "r"} <= set(values):
            raise ValueError("Targets must contain x, y, z, and r")
        if set(values) - {"x", "y", "z", "r", "grip"}:
            raise ValueError("Unknown target field")
        try:
            pose = tuple(finite_number(values[axis]) for axis in ("x", "y", "z", "r"))
            grip = finite_number(values["grip"]) if "grip" in values else None
        except OverflowError as error:
            raise ValueError("Targets must contain finite numbers") from error
        return pose, RobotPanelState._gripper_target(grip) if grip is not None else None

    @staticmethod
    def _gripper_target(value):
        try:
            grip = finite_number(value)
        except OverflowError as error:
            raise ValueError("Gripper target must be an integer from 0 to 1000") from error
        if not grip.is_integer() or not 0 <= grip <= 1000:
            raise ValueError("Gripper target must be an integer from 0 to 1000")
        return int(grip)

    def _start_sync(self):
        if self._syncing:
            return
        if self._reader is not None and self._reader.is_alive():
            raise RuntimeError("Feedback reader is stopping. Retry start sync.")
        self._ownership.acquire()
        self._reader_stop = threading.Event()
        self._sample = None
        self._pose = [None] * 4
        self._syncing = True
        self._started = time.monotonic()
        self._message = "Connecting to robot feedback."
        self._reader = threading.Thread(target=self._read_feedback, daemon=True)
        try:
            self._reader.start()
        except Exception:
            self._syncing = False
            self._reader = None
            self._release_ownership_if_idle()
            raise

    def _release_ownership_if_idle(self):
        """Keep ownership until feedback and all controller work have ended."""
        if (
            self._syncing
            or self._fault
            or self._busy
            or self._working
            or self._job
            or self._safety_workers
            or any(thread.is_alive() for thread in self._safety_threads)
            or (self._reader is not None and self._reader.is_alive())
            or self._control.feedback_retention_reason() is not None
        ):
            return
        self._ownership.release()

    def _read_feedback(self):
        try:
            with self._connection_factory(self.robot_host, timeout_s=1.0) as connection:
                while not self._reader_stop.is_set():
                    sample = connection.read_latest()
                    with self._lock:
                        if self._reader_stop.is_set():
                            return
                        first = self._sample is None
                        self._sample, self._received = sample, time.monotonic()
                        if first and not self._locked:
                            self._message = "Robot feedback is connected."
        except Exception as error:
            with self._lock:
                if not self._reader_stop.is_set():
                    if self._sample is None and not self._busy:
                        self._syncing = False
                        self._message = f"Robot feedback connection failed: {error}"
                    else:
                        self._request_stop(reason=f"Robot feedback failed: {error}")

    def _request_stop(self, *, disabling=False, disconnect=False, reason=None):
        """Latch cancellation before a separate worker sends the stop command."""
        self._ownership.acquire()
        self._locked = True
        self._control.cancel(disabling=disabling)
        self._disconnect_requested |= disconnect
        if disabling in self._safety_actions:
            return
        self._disable_verified = False
        self._message = reason or ("Disabling the robot." if disabling else "Stopping the robot.")
        if self._gripper_active:
            self._message += " The gripper continues until its current command completes."
        self._safety_workers += 1
        self._safety_actions.add(disabling)
        thread = threading.Thread(
            target=self._stop,
            args=(self._control, disabling),
            daemon=True,
        )
        self._safety_threads = [worker for worker in self._safety_threads if worker.is_alive()]
        self._safety_threads.append(thread)
        thread.start()

    def _stop(self, control, disabling):
        try:
            if disabling:
                control.disable()
                control.verify_stationary()
            else:
                result = control.stop()
                if not result["ok"]:
                    raise RuntimeError(result.get("error", "Robot stop was not confirmed"))
            with self._lock:
                if disabling:
                    self._disable_verified = True
                self._message = (
                    "Robot disable and rest are confirmed."
                    if disabling
                    else "Robot stop is confirmed. Disable the robot before recovery."
                )
                if self._gripper_active:
                    self._message += " The gripper command is still active."
        except Exception as error:
            with self._lock:
                self._fault = True
                self._message = f"Robot stop or disable is unconfirmed: {error}"
        finally:
            with self._lock:
                self._safety_workers -= 1
                self._safety_actions.remove(disabling)
                if self._disconnect_requested and not self._safety_workers and not self._fault:
                    self._syncing = False
                    self._reader_stop.set()
                    self._owner = None
                    self._disconnect_requested = False
                self._recover_disabled()

    def _recover_disabled(self):
        if (
            self._disable_verified
            and not self._fault
            and not self._busy
            and not self._working
            and not self._safety_workers
            and not self._closing
        ):
            self._control = self._new_control()
            self._locked = False
            self._disable_verified = False

    def _operate(self, action, targets):
        self._guard_send()
        if action == "enable":
            self._control.enable()
        elif action == "home":
            self._control.move((0.0, 0.0, 0.0, 0.0))
        elif action == "execute":
            pose, grip = targets
            self._control.move_linear(pose)
            if grip is not None:
                self._move_gripper(grip)
        elif action == "grip":
            self._move_gripper(targets[1])
        else:
            self._move_gripper(None)

    def _move_gripper(self, target):
        with self._lock:
            self._guard_send()
            self._gripper_active = True
        try:
            if target is None:
                self._gripper.initialize()
            else:
                self._gripper.move(target)
        finally:
            with self._lock:
                self._gripper_active = False
                self._grip = self._gripper.snapshot()

    def _work(self):
        while not self._closing:
            self._wake.wait(0.1)
            self._wake.clear()
            with self._lock:
                job, self._job = self._job, None
                self._working = True
                poll_pose = (
                    self._fresh()
                    and not self._safety_workers
                    and not self._locked
                    and not self._fault
                    and not self._disconnect_requested
                    and not self._control.stopped
                )
            try:
                if job:
                    self._operate(*job)
                    with self._lock:
                        if not self._locked:
                            self._message = "Robot operation completed."
                elif poll_pose:
                    pose = self._control.read_pose()
                    with self._lock:
                        if not self._busy:
                            self._pose = list(pose)
                if self._gripper is not None and self._syncing:
                    self._gripper.poll()
                    with self._lock:
                        self._grip = self._gripper.snapshot()
            except (TargetRejectedError, PreparationRejectedError) as error:
                with self._lock:
                    if not self._locked:
                        self._message = str(error)
            except Exception as error:
                with self._lock:
                    if not self._locked:
                        self._request_stop(reason=f"Robot operation failed: {error}")
            finally:
                with self._lock:
                    self._working = False
                    if job:
                        self._busy = False
                    self._recover_disabled()

    def _watch(self):
        while not self._closing:
            time.sleep(0.05)
            with self._lock:
                self._release_ownership_if_idle()
                if self._owner is None:
                    continue
                now = time.monotonic()
                if now - self._heartbeat > self.LEASE_SECONDS:
                    self._expire_lease()
                elif self._syncing and not self._locked:
                    expired = now - self._started > self.CONNECT_SECONDS and not self._fresh()
                    failed = self._sample is not None and (
                        self._sample.error_status or self._sample.mode not in {4, 5, 7}
                    )
                    if expired or failed:
                        if self._sample is None and not self._busy:
                            self._syncing = False
                            self._reader_stop.set()
                            self._message = "Robot feedback did not connect. Retry Start sync."
                        else:
                            self._request_stop(
                                reason="Robot feedback is stale or reports an error."
                            )

    def close(self):
        with self._lock:
            if self._closing:
                return
            self._closing = True
            if self._syncing or self._busy:
                self._request_stop(disconnect=True, reason="The panel is closing.")
            self._wake.set()
        for thread in self._safety_threads:
            thread.join(timeout=6)
        self._reader_stop.set()
        self._syncing = False
        self._worker.join(timeout=1)
        self._watchdog.join(timeout=1)
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._gripper is not None:
            self._gripper.close()
        with self._lock:
            self._release_ownership_if_idle()
