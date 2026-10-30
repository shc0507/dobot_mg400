"""Control the robot from the web panel without a simulator dependency."""

import logging
import math
import secrets
import threading
import time
from contextlib import contextmanager

from .config import finite_number
from .controller_alarms import describe_alarms
from .controller_ownership import ControllerOwnership
from .feedback import FeedbackConnection
from .joint_control import JointControl, PreparationRejectedError, TargetRejectedError
from .joint_limits import validate_measured_joints
from .keyboard_jog import KeyboardJogSession
from .web_keyboard import (
    DISARM_REASONS,
    GRIPPER_HOLD_SECONDS,
    GRIPPER_KEYS,
    KEY_LEASE_SECONDS,
    KEYBOARD_JOIN_SECONDS,
    GripperUnavailableError,
    held_keys,
    run_gripper_steps,
    sequence_number,
    speed_percent,
)

LOG = logging.getLogger(__name__)
# Without a configured handler, the panel log stays silent and never writes under the lock.
LOG.addHandler(logging.NullHandler())
_UNSET = object()


class _Logged:
    """An attribute that logs describe(value) when key(value) changes.

    The first assignment sets the initial value and logs nothing. A None description
    logs nothing. Configure a non-blocking handler: assignments happen under the lock.
    """

    def __init__(self, describe, key=None, level=logging.INFO):
        self._describe, self._level = describe, level
        self._key = key or (lambda value: value)

    def __set_name__(self, owner, name):
        self._name = "_logged" + name

    def __get__(self, state, owner=None):
        return self if state is None else getattr(state, self._name)

    def __set__(self, state, value):
        previous = getattr(state, self._name, _UNSET)
        setattr(state, self._name, value)
        if previous is not _UNSET and self._key(previous) != self._key(value):
            text = self._describe(value)
            if text is not None:
                LOG.log(self._level, text)


def _describe_alarms(alarms):
    if not alarms:
        return "Controller alarms cleared."
    return "Controller alarms: " + "; ".join(
        f"{alarm['id']} {alarm['description']}" for alarm in alarms
    )


class RobotPanelState:
    """Keep HTTP reads separate from robot operations and stop verification."""

    mode = "robot"
    LEASE_SECONDS = 3.0
    FEEDBACK_SECONDS = 0.25
    CONNECT_SECONDS = 1.5
    ALARM_READ_SECONDS = 1.0
    KEY_LEASE_SECONDS = KEY_LEASE_SECONDS
    KEYBOARD_JOIN_SECONDS = KEYBOARD_JOIN_SECONDS
    GRIPPER_HOLD_SECONDS = GRIPPER_HOLD_SECONDS

    # Log every state that explains a lock. The page shows only the latest message.
    _message = _Logged(lambda message: f"Message: {message}")
    _locked = _Logged(
        lambda locked: "Control locked." if locked else "Control unlocked.",
        level=logging.WARNING,
    )
    _fault = _Logged(
        lambda fault: "Stop or disable unconfirmed." if fault else "Unconfirmed stop cleared.",
        level=logging.WARNING,
    )
    _sample = _Logged(
        lambda sample: (
            None if sample is None else (f"Robot mode {sample.mode}, error={sample.error_status}")
        ),
        key=lambda sample: None if sample is None else (sample.mode, sample.error_status),
    )
    _alarms = _Logged(_describe_alarms, level=logging.WARNING)
    _kb_reason = _Logged(lambda reason: None if reason is None else f"Keyboard off: {reason}")
    _grip = _Logged(
        lambda grip: (
            f"Gripper locked: {grip.get('message')}" if grip.get("locked") else "Gripper unlocked."
        ),
        key=lambda grip: bool(grip.get("locked")),
        level=logging.WARNING,
    )

    def __init__(
        self,
        host,
        *,
        gripper=None,
        connection_factory=FeedbackConnection,
        control_factory=JointControl,
        session_factory=KeyboardJogSession,
    ):
        self.robot_host = host
        self._gripper = gripper
        self._connection_factory = connection_factory
        self._control_factory = control_factory
        self._session_factory = session_factory
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
        self._faults = 0
        # Set when a stop or disable failed while the controller reported no alarm.
        self._non_alarm_fault = False
        self._disable_verified = False
        self._safety_workers = 0
        self._safety_actions = set()
        self._disconnect_requested = False
        self._safety_threads = []
        self._gripper_active = False
        self._closing = False
        self._alarms = None
        self._alarms_read = 0.0
        self._clearing = False
        self._message = "Start sync to connect to the robot."
        # Snapshot versions order replies within one process. A restart starts a new instance.
        self._instance = secrets.token_hex(8)
        self._version = 0
        # Keyboard state. _kb_thread is the one preparation or hold worker in flight.
        self._kb_on = False
        self._kb_arming = False
        self._kb_thread = None
        self._kb_kind = None
        self._kb_session = None
        self._kb_generation = 0
        self._kb_speed = 1
        self._kb_reason = None
        self._kb_seq = 0
        self._kb_seq_client = None
        self._kb_held = frozenset()
        self._kb_pending = set()
        self._kb_active = None
        self._kb_released = None
        self._kb_renewed = 0.0
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
            send_guard=self._guard_control_send,
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
            self._version += 1
            moving = self._busy or self._kb_kind in {"prepare", "arm"}
            return {
                "mode": "robot",
                "instance": self._instance,
                "version": self._version,
                "robot_host": self.robot_host,
                "actual": [
                    *([None] * 4 if moving else self._pose),
                    None if self._gripper_active else self._grip.get("position"),
                ],
                "joints": list(sample.joints_deg) if sample else None,
                "enabled": sample.mode in {5, 7} if sample else None,
                "syncing": self._syncing,
                "feedback_fresh": self._fresh(),
                "robot_mode": sample.mode if sample else None,
                "robot_error": sample.error_status if sample else None,
                "alarms": self._alarms,
                "speed_factor": self._speed_factor(sample),
                "message": self._message,
                "busy": self._busy
                or bool(self._safety_workers)
                or self._clearing
                or self._kb_thread is not None,
                "locked": self._locked or bool(self._grip.get("locked")),
                "stop_unconfirmed": self._fault,
                "recoverable": self._recoverable(),
                "gripper_available": bool(self._grip.get("available")),
                "gripper_can_initialize": bool(self._grip.get("can_initialize")),
                "gripper_busy": self._gripper_active or bool(self._grip.get("busy")),
                "gripper_message": self._grip.get("message", "Gripper is not configured."),
                "controller_id": self._owner,
                "keyboard_enabled": self._kb_on,
                "keyboard_arming": self._kb_arming,
                "keyboard_active_key": self._kb_active if self._kb_thread is not None else None,
                "keyboard_speed_percent": self._kb_speed,
                "keyboard_disarm_reason": self._kb_reason,
            }

    @staticmethod
    def _speed_factor(sample):
        value = getattr(sample, "speed_scaling_raw", None)
        # A non-finite raw value would make the JSON response invalid.
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

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

    def connection_lost(self, client_id):
        """Revoke keyboard input at once when the owner's live connection closes.

        The browser lease is unchanged: a page that reconnects in time keeps control.
        """
        self._client(client_id)
        with self._lock:
            if self._owner == client_id:
                self._kb_disarm("browser connection closed.")

    def _expire_lease(self):
        if self._owner is not None and time.monotonic() - self._heartbeat > self.LEASE_SECONDS:
            self._owner = None
            self._kb_disarm("browser control lease expired.")
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

    def _guard_control_send(self):
        """Check every controller mutation, including keyboard child operations."""
        with self._lock:
            if self._kb_kind == "prepare" and not self._kb_arming:
                # A preparation that lost its arming request must not change SpeedFactor.
                raise PreparationRejectedError("Keyboard preparation cancelled.")
            self._guard_send()

    @contextmanager
    def _gripper_publication_guard(self):
        with self._lock:
            self._guard_send()
            if self._sample.mode not in {4, 5}:
                raise RuntimeError("The robot must be idle before a gripper command")
            if (
                self._kb_kind == "gripper"
                and threading.current_thread() is self._kb_thread
                and self._kb_released is not None
                and self._kb_released.is_set()
            ):
                # The key was released after this step began. Publish nothing.
                raise RuntimeError("The keyboard hold ended before the gripper step was sent.")
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
            "keyboard",
            "clear_alarm",
        }:
            raise ValueError("Unknown action")
        fields = {"action"}
        if action == "execute":
            fields.add("targets")
        elif action == "grip":
            fields.add("position")
        elif action == "keyboard":
            if type(payload.get("enabled")) is not bool:
                raise ValueError("Keyboard enabled must be true or false")
            fields |= {"enabled", "seq", "speed" if payload["enabled"] else "reason"}
        if set(payload) != fields:
            raise ValueError("Unexpected command fields")
        targets = self._targets(payload["targets"]) if action == "execute" else None
        if action == "grip":
            targets = (None, self._gripper_target(payload["position"]))
        if action == "keyboard":
            seq = sequence_number(payload["seq"])
            if payload["enabled"]:
                speed = speed_percent(payload["speed"])
            elif not isinstance(payload["reason"], str) or payload["reason"] not in DISARM_REASONS:
                raise ValueError("Unknown keyboard reason")
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
                    if self._owner is None and self._kb_seq_client != client_id:
                        # Each page numbers its keyboard messages from 1.
                        self._kb_seq, self._kb_seq_client = 0, client_id
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
                    elif action == "keyboard":
                        # Arm, disarm and key updates share one order. Ignore stale messages.
                        if seq > self._kb_seq:
                            self._kb_seq = seq
                            if payload["enabled"]:
                                self._kb_arm(speed)
                            else:
                                self._kb_disarm(DISARM_REASONS[payload["reason"]])
                    elif action == "clear_alarm":
                        self._start_alarm_clear()
                    else:
                        self._guard_send()
                        if self._busy or self._safety_workers or self._kb_thread is not None:
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
                        # A button command replaces keyboard control.
                        self._kb_disarm(None)
                        self._busy = True
                        self._pose = [None] * 4
                        self._job = (action, targets)
                        self._message = f"Robot operation started: {action}."
                        self._wake.set()
        return self.snapshot()

    def keys(self, payload, client_id):
        """Apply the held-key set of one browser update. Ignore stale updates."""
        self._client(client_id)
        if not isinstance(payload, dict) or set(payload) != {"seq", "held"}:
            raise ValueError("Key input must contain only seq and held")
        seq = sequence_number(payload["seq"])
        held = held_keys(payload["held"])
        with self._lock:
            if self._closing:
                raise RuntimeError("The panel is closing")
            self._expire_lease()
            if self._owner != client_id:
                raise PermissionError("Start sync in this browser before issuing commands")
            if seq > self._kb_seq:
                self._kb_seq = seq
                self._heartbeat = time.monotonic()
                if self._kb_on:
                    self._kb_input(held)
                else:
                    # Track keys while off, so a key held across arming needs a new press.
                    self._kb_held = held
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
        if self._gripper is not None:
            self._gripper.request_collector()

    def _release_ownership_if_idle(self):
        """Keep ownership until feedback and all controller work have ended."""
        if (
            self._syncing
            or self._fault
            or self._busy
            or self._working
            or self._job
            or self._safety_workers
            or self._clearing
            or self._kb_thread is not None
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
        """Latch cancellation before a separate worker sends the stop command.

        An arm hold stops itself when its input is revoked here, and a preparation
        sends no further command. The worker cancels the parent control after that
        keyboard worker exits, so the stops never overlap.
        """
        self._ownership.acquire()
        self._locked = True
        self._kb_disarm(None)
        keyboard = self._kb_thread
        if keyboard is None or self._kb_kind == "gripper" or not keyboard.is_alive():
            keyboard = None
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
            args=(self._control, disabling, keyboard),
            daemon=True,
        )
        self._safety_threads = [worker for worker in self._safety_threads if worker.is_alive()]
        try:
            thread.start()
        except Exception as error:
            self._safety_workers -= 1
            self._safety_actions.discard(disabling)
            self._control.cancel(disabling=disabling)
            self._record_fault()
            self._message = f"Robot stop could not start: {error} Use the physical emergency stop."
            return
        self._safety_threads.append(thread)

    def _stop(self, control, disabling, keyboard=None):
        try:
            if keyboard is not None:
                # The guard refuses new keyboard sends while the session stops itself.
                if keyboard.ident is not None:
                    keyboard.join(self.KEYBOARD_JOIN_SECONDS)
                control.cancel(disabling=disabling)
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
                if self._fault:
                    # A later success must not hide an earlier unconfirmed verdict.
                    self._message = (
                        "Control stays locked: an earlier stop or disable is unconfirmed. "
                        + self._recovery_hint()
                    )
                if self._gripper_active:
                    self._message += " The gripper command is still active."
        except Exception as error:
            with self._lock:
                self._record_fault()
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

    def _alarm_now(self):
        return self._fresh() and (self._sample.mode == 9 or self._sample.error_status)

    def _record_fault(self):
        self._fault = True
        self._faults += 1
        if not self._alarm_now():
            self._non_alarm_fault = True

    def _recoverable(self):
        """Whether Recover may confirm rest after the alarm behind the fault cleared elsewhere."""
        return (
            self._fault
            and not self._non_alarm_fault
            and not self._clearing
            and self._fresh()
            and not self._alarm_now()
        )

    def _recovery_hint(self):
        if self._non_alarm_fault:
            return "Check the robot, then restart the panel service."
        if self._alarm_now():
            return "Fix the alarm cause, then click Clear alarm."
        return "Check the robot, then click Recover."

    def _recover_disabled(self):
        if (
            self._disable_verified
            and not self._fault
            and not self._busy
            and not self._working
            and not self._safety_workers
            and not self._clearing
            and not self._closing
            and self._kb_thread is None
        ):
            # A verified disable and rest check supersede an earlier keyboard stop verdict.
            self._control = self._new_control()
            self._locked = False
            self._disable_verified = False
            if self._kb_session is not None:
                self._kb_close(self._kb_session)

    def _start_alarm_clear(self):
        if not self._fresh():
            raise RuntimeError("Robot feedback is absent or stale")
        alarm = self._alarm_now()
        if not alarm and not self._recoverable():
            raise RuntimeError("The robot reports no alarm")
        if self._busy or self._safety_workers or self._clearing or self._kb_thread is not None:
            raise RuntimeError("Another robot operation is in progress")
        self._kb_disarm(None)
        thread = threading.Thread(
            target=self._clear_alarm,
            args=(self._control, alarm),
            name="MG400-alarm-clear",
            daemon=True,
        )
        self._clearing = True
        self._message = (
            "Clearing the robot alarm." if alarm else "Confirming robot disable and rest."
        )
        try:
            thread.start()
        except Exception:
            self._clearing = False
            raise
        self._safety_threads.append(thread)

    def _clear_alarm(self, control, clear):
        """Clear the alarm, then confirm a disable and rest on a new control instance.

        In an alarm the controller refuses DisableRobot() and has already halted motion.
        A verified disable and rest after the alarm clears replace the stop that the
        alarm prevented. clear is False when the alarm already cleared elsewhere, for
        example with Collision Resume on the arm. Unknown command ordering, or a stop
        that failed without an alarm, still keeps control locked.
        """
        try:
            alarms = describe_alarms(control.clear_alarms() if clear else ())
            with self._lock:
                self._alarms, self._alarms_read = alarms or None, time.monotonic()
                cleared = self._fresh() and self._sample.mode != 9 and not self._sample.error_status
                if alarms or not cleared:
                    names = "; ".join(
                        f"{alarm['id']} {alarm['description']}"
                        + (f" ({alarm['solution']})" if alarm["solution"] else "")
                        for alarm in alarms
                    )
                    self._message = f"The alarm remains: {names or 'robot still reports an error'}."
                    return
                if control.ordering_unknown or control.command_pending:
                    self._message = (
                        "The alarm is cleared, but command ordering remains unknown. "
                        "Check the robot, then restart the panel service."
                    )
                    return
                if self._non_alarm_fault:
                    self._message = (
                        "The alarm is cleared, but an earlier stop failed without an alarm. "
                        "Check the robot, then restart the panel service."
                    )
                    return
                recovery = self._control = self._new_control()
                faults = self._faults
            recovery.disable()
            recovery.verify_stationary()
            with self._lock:
                # A stop that failed during recovery keeps its unconfirmed verdict.
                if (
                    self._control is recovery
                    and self._faults == faults
                    and not self._safety_workers
                ):
                    self._fault = False
                    self._disable_verified = True
                    self._message = "The alarm is cleared. Robot disable and rest are confirmed."
        except Exception as error:
            with self._lock:
                self._message = f"Alarm recovery failed: {error}"
        finally:
            with self._lock:
                self._clearing = False
                self._recover_disabled()

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
                    and self._kb_thread is None
                )
                alarm = self._fresh() and (self._sample.mode == 9 or self._sample.error_status)
                if self._fresh() and not alarm:
                    self._alarms = None
                read_alarms = (
                    alarm
                    and not self._safety_workers
                    and not self._clearing
                    and self._kb_thread is None
                    and time.monotonic() - self._alarms_read >= self.ALARM_READ_SECONDS
                )
                control = self._control
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
                elif read_alarms:
                    self._read_alarms(control)
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
                    # A key pressed during this poll starts now.
                    self._kb_reconcile()

    def _read_alarms(self, control):
        """Show the alarm IDs. A failed read keeps the last list and stops nothing."""
        try:
            alarms = describe_alarms(control.read_alarms())
        except Exception:
            alarms = None
        with self._lock:
            self._alarms_read = time.monotonic()
            if alarms is not None:
                self._alarms = alarms or None

    def _watch(self):
        while not self._closing:
            time.sleep(0.05)
            try:
                with self._lock:
                    self._watch_once()
            except Exception as error:
                # The watchdog must keep running. Treat its own failure as a reason to stop.
                try:
                    with self._lock:
                        if not self._closing and not self._locked:
                            self._request_stop(reason=f"Panel watchdog failed: {error}")
                except Exception:
                    pass

    def _watch_once(self):
        self._release_ownership_if_idle()
        self._kb_watch()
        if self._owner is None:
            return
        now = time.monotonic()
        if now - self._heartbeat > self.LEASE_SECONDS:
            self._expire_lease()
        elif self._syncing and not self._locked:
            expired = now - self._started > self.CONNECT_SECONDS and not self._fresh()
            failed = self._sample is not None and (
                self._sample.error_status
                or (
                    self._sample.mode not in {4, 5, 7}
                    # Continuous jogging reports mode 11 until its stop is verified.
                    and not (self._sample.mode == 11 and self._kb_kind == "arm")
                )
            )
            if expired or failed:
                if self._sample is None and not self._busy:
                    self._syncing = False
                    self._reader_stop.set()
                    self._message = "Robot feedback did not connect. Retry Start sync."
                else:
                    self._request_stop(reason="Robot feedback is stale or reports an error.")

    # Keyboard control. Call the _kb_* helpers with self._lock held, except the workers.

    def _kb_arm(self, speed):
        if self._kb_on or self._kb_arming:
            if speed != self._kb_speed:
                raise RuntimeError(
                    f"Keyboard is on at {self._kb_speed}%. Turn it off to change the speed."
                )
            return
        self._guard_send()
        if (
            self._busy
            or self._safety_workers
            or self._kb_thread is not None
            or self._gripper_active
        ):
            raise RuntimeError("Another robot operation is in progress")
        if self._sample.mode != 5:
            raise RuntimeError("Enable the robot before keyboard control")
        if self._grip.get("locked"):
            raise RuntimeError("Gripper control is locked")
        try:
            validate_measured_joints(self._sample.joints_deg)
        except ValueError as error:
            raise RuntimeError(f"Keyboard unavailable: {error}") from error
        reason = self._control.feedback_retention_reason()
        if reason is not None:
            raise RuntimeError(f"Keyboard unavailable: {reason}")
        session = self._session_factory(self._control, speed_percent=speed)
        self._kb_generation += 1
        self._kb_session = session
        self._kb_arming = True
        self._kb_speed = speed
        self._kb_reason = None
        self._kb_held = frozenset()
        self._kb_pending.clear()
        self._kb_active = self._kb_released = None
        try:
            self._kb_start("prepare", self._kb_prepare, (session, self._kb_generation))
        except Exception:
            self._kb_arming = False
            self._kb_close(session)
            raise
        self._message = "Checking keyboard control..."

    def _kb_start(self, kind, target, args):
        thread = threading.Thread(
            target=target, args=args, name=f"MG400-keyboard-{kind}", daemon=True
        )
        # Register the worker before it runs; it clears these fields when it finishes.
        self._kb_thread, self._kb_kind = thread, kind
        try:
            thread.start()
        except Exception:
            self._kb_thread = self._kb_kind = None
            raise

    def _kb_preparation_current(self, generation):
        return (
            generation == self._kb_generation
            and self._syncing
            and not self._locked
            and not self._fault
            and not self._closing
        )

    def _kb_prepare(self, session, generation):
        error = None
        started = False
        try:
            # Let a pose query finish before preparation uses the dashboard connection.
            while True:
                with self._lock:
                    if not self._kb_preparation_current(generation):
                        break
                    if not self._working:
                        started = True
                        break
                time.sleep(0.005)
            if started:
                session.prepare()
        except Exception as caught:
            error = caught
        with self._lock:
            self._kb_thread = self._kb_kind = None
            current = generation == self._kb_generation and session is self._kb_session
            if current:
                self._kb_arming = False
            if started and error is None and current and self._kb_may_enable():
                self._kb_on = True
                self._kb_renewed = time.monotonic()
                self._kb_message("Keyboard ready. Hold one key; release requests a stop.")
            else:
                self._kb_close(session)
                if current and isinstance(error, PreparationRejectedError):
                    self._kb_message(
                        f"Keyboard preparation deferred: {error} Click Keyboard to retry."
                    )
                elif (current and error is not None) or session.fault is not None:
                    self._kb_message(f"ERROR: {session.fault or error}")
                elif current:
                    self._kb_reason = "robot state changed during preparation."
                    self._kb_message(f"Keyboard off: {self._kb_reason}")
            self._kb_check_control(session.fault or error)
            self._recover_disabled()
            self._wake.set()

    def _kb_may_enable(self):
        return (
            not self._locked
            and not self._fault
            and not self._closing
            and self._owner is not None
            and self._fresh()
            and not self._sample.error_status
            and self._sample.mode == 5
            and not self._control.stopped
        )

    def _kb_message(self, message):
        if not self._locked and not self._fault and not self._closing:
            self._message = message

    def _kb_close(self, session):
        try:
            session.close()
        except Exception:
            pass
        if session is self._kb_session:
            self._kb_session = None

    def _kb_check_control(self, detail):
        """Turn a keyboard fault in the shared control into a verified stop and a lock."""
        if self._locked or self._closing:
            return
        reason = self._control.feedback_retention_reason()
        if self._control.stopped or reason is not None:
            self._request_stop(
                reason=f"Keyboard control failed: {detail or reason or 'control stopped'}"
            )

    def _kb_disarm(self, reason):
        """Revoke keyboard input without blocking or raising."""
        active = self._kb_on or self._kb_arming
        self._kb_on = self._kb_arming = False
        self._kb_generation += 1
        self._kb_held = frozenset()
        self._kb_pending.clear()
        if self._kb_released is not None:
            self._kb_released.set()
        session = self._kb_session
        if session is not None:
            try:
                if self._kb_kind == "prepare":
                    # Cut a preparation short unless its SpeedFactor acknowledgement is
                    # pending; cancelling then would leave command ordering unknown.
                    if not self._control.command_pending:
                        session.close()
                elif self._kb_thread is not None:
                    session.release()
                else:
                    self._kb_close(session)
            except Exception as error:
                reason = reason or f"keyboard session cleanup failed: {error}"
        self._kb_active = None
        if active and reason is not None:
            self._kb_reason = reason
            self._kb_message(f"Keyboard off: {reason}")

    def _kb_release(self):
        """Release the active hold (or open its release gate) without discarding other input."""
        if self._kb_released is not None:
            self._kb_released.set()
        if self._kb_session is not None and self._kb_kind != "prepare":
            try:
                self._kb_session.release()
            except Exception as error:
                self._kb_disarm(f"keyboard release failed: {error}")

    def _kb_input(self, held):
        released, pressed = self._kb_held - held, held - self._kb_held
        self._kb_held = held
        if held:
            self._kb_renewed = time.monotonic()
        self._kb_pending -= released
        if self._kb_active in released:
            self._kb_active = None
            self._kb_release()
        if pressed and self._kb_on:
            self._kb_pending |= pressed
            # Several held keys stop the active hold and start nothing.
            if len(held) != 1:
                self._kb_release()
        self._kb_reconcile()

    def _kb_can_start(self):
        now = time.monotonic()
        if (
            not self._kb_on
            or self._closing
            or not self._syncing
            or self._owner is None
            or now - self._heartbeat > self.LEASE_SECONDS
            or self._busy
            or self._working
            or self._safety_workers
            or self._locked
            or self._fault
            or self._control.stopped
            or self._gripper_active
            or self._grip.get("locked")
            or not self._fresh()
            or self._sample.error_status
            or self._sample.mode != 5
        ):
            return False
        if now - self._kb_renewed > self.KEY_LEASE_SECONDS:
            self._kb_disarm("key updates stopped.")
            return False
        if self._kb_session is None or not self._kb_session.ready:
            self._kb_disarm("session is not ready. Click Keyboard to retry.")
            return False
        return True

    def _kb_reconcile(self):
        """Start a hold for one newly pressed key when control permits it. Never raises."""
        if not self._kb_on or self._kb_thread is not None or len(self._kb_held) != 1:
            return
        key = next(iter(self._kb_held))
        try:
            if key not in self._kb_pending or not self._kb_can_start():
                return
            self._kb_pending.discard(key)
            self._kb_active = key
            released = self._kb_released = threading.Event()
            kind = "gripper" if key in GRIPPER_KEYS else "arm"
            self._kb_start(
                kind,
                self._kb_hold,
                (self._kb_session, self._control, key, released, self._kb_generation),
            )
        except Exception as error:
            self._kb_disarm(f"keyboard worker could not start: {error}")
            return
        if kind == "arm":
            self._pose = [None] * 4
        self._kb_message("Jogging... Release the key to stop.")

    def _kb_hold(self, session, control, key, released, generation):
        pose = outcome = error = None
        with self._lock:
            # A release or disarm before this point needs no robot command.
            skipped = released.is_set() or generation != self._kb_generation
        if not skipped:
            try:
                if key in GRIPPER_KEYS:
                    outcome = run_gripper_steps(
                        self._gripper,
                        control.read_state,
                        key,
                        released,
                        set_active=self._kb_gripper_active,
                        hold_seconds=self.GRIPPER_HOLD_SECONDS,
                    )
                else:
                    pose = session.run(key, released)
            except Exception as caught:
                error = caught
        with self._lock:
            self._kb_thread = self._kb_kind = None
            current = self._kb_on and session is self._kb_session
            if not current:
                self._kb_close(session)
            if pose is not None and not self._busy:
                self._pose = list(pose)
            if isinstance(error, GripperUnavailableError):
                # After keys turn off, keep the message that says why.
                if self._kb_on:
                    self._kb_message(f"Gripper unavailable: {error} Arm keys remain ready.")
            elif error is not None:
                if self._kb_on:
                    self._kb_disarm(None)
                self._kb_message(f"ERROR: {session.fault or error}")
            elif current:
                if not skipped and key not in GRIPPER_KEYS and session.limit_reason:
                    self._kb_message(
                        f"{session.limit_reason} Release the key and choose another direction."
                    )
                elif outcome == "contact":
                    self._kb_message(
                        "The gripper reports object contact before the target. "
                        "Release the key before pressing again."
                    )
                elif outcome == "expired":
                    self._kb_message("Hold limit reached. Release the key before pressing again.")
                else:
                    self._kb_message("Stopped. Hold a key for the next move.")
            self._kb_check_control(session.fault or error)
            self._recover_disabled()
            self._kb_reconcile()
            self._wake.set()

    def _kb_gripper_active(self, active):
        with self._lock:
            self._gripper_active = active
            if not active:
                self._grip = self._gripper.snapshot()

    def _kb_watch(self):
        """Turn keys off when feedback, control state, or key updates no longer permit them."""
        if not (self._kb_on or self._kb_arming):
            return
        sample = self._sample
        reason = None
        if self._closing or not self._syncing:
            reason = "synchronization stopped."
        elif not self._fresh() or sample.error_status:
            reason = "robot feedback is stale, unavailable, or reports a fault."
        elif self._locked or self._fault or self._control.stopped:
            reason = "robot control is stopped."
        elif self._grip.get("locked"):
            reason = "gripper control is locked."
        else:
            try:
                validate_measured_joints(sample.joints_deg)
            except ValueError:
                reason = "joint feedback is outside the allowed range."
            else:
                if self._kb_on and self._kb_thread is None and sample.mode != 5:
                    reason = "the robot is not enabled and idle."
                elif (
                    self._kb_on
                    and self._kb_held
                    and time.monotonic() - self._kb_renewed > self.KEY_LEASE_SECONDS
                ):
                    reason = "key updates stopped."
        if reason is not None:
            self._kb_disarm(reason)

    def close(self):
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._kb_disarm(None)
            if self._syncing or self._busy or self._kb_thread is not None:
                self._request_stop(disconnect=True, reason="The panel is closing.")
            self._wake.set()
        for thread in self._safety_threads:
            thread.join(timeout=6 + self.KEYBOARD_JOIN_SECONDS + 2)
        with self._lock:
            keyboard = self._kb_thread
        if keyboard is not None and keyboard.ident is not None:
            keyboard.join(timeout=self.KEYBOARD_JOIN_SECONDS)
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
