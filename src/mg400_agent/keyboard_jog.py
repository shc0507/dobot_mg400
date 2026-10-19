"""Bounded keyboard movement. Each hold owns at most one unfinished move."""

import math
import threading
import time

from .joint_control import JointControl
from .joint_limits import validate_joint_segment, validate_joints

HOLD_SECONDS = 5.0
STEP_MM = 5.0
STEP_DEG = 1.0
KEYS = frozenset("WASDRFQE")


class KeyboardJogSession:
    """Prepare once, then run one cancellable hold at a time on worker threads.

    release() and close() are nonblocking UI calls. A normal release preserves
    readiness after the stop is verified. Any uncertain stop locks both this
    session and its parent control. No motion is sent by construction/prepare.
    """

    def __init__(self, control):
        self.control = control
        self._lock = threading.Lock()
        self._active = None
        self._phase = None
        self._prepared = False
        self._closed = False
        self._revoked = threading.Event()
        self._released = None
        self._release_gate = None
        self._stop_thread = None
        self._stop_result = None
        self._pose = None
        self.fault = None
        self.expired = False

    @property
    def busy(self):
        with self._lock:
            return self._active is not None

    @property
    def ready(self):
        with self._lock:
            return (
                self._prepared and self._active is None and not self._closed
                and self.fault is None and not self.control.stopped
            )

    def _start(self, phase, released=None):
        with self._lock:
            if self._closed or self.fault is not None or self.control.stopped:
                raise RuntimeError(self.fault or "Keyboard session is closed or stopped")
            if self._active is not None:
                raise RuntimeError("A keyboard operation is already in progress")
            if phase == "running" and not self._prepared:
                raise RuntimeError("Prepare keyboard control before movement")
            if self._release_gate is not None and not self._release_gate.is_set():
                raise RuntimeError("Release the expired key before another hold")
            active = JointControl(
                self.control.host, self.control.feedback_snapshot,
                parent=self.control, command_timeout=0.5,
            )
            active._begin()
            self._active, self._phase = active, phase
            self._revoked = threading.Event()
            self._released = released
            self._stop_thread = self._stop_result = None
            self.expired = False
            return active

    def _stop(self, active):
        try:
            result = active.stop()
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        with self._lock:
            self._stop_result = result

    def _revoke(self, *, error=None, expired=False, physical_release=False):
        with self._lock:
            if self._active is None:
                if physical_release and self._release_gate is not None:
                    self._release_gate.set()
                return
            if physical_release and self._released is not None:
                self._released.set()
            if expired:
                self.expired = True
                self._release_gate = self._released
            if error is not None and self.fault is None:
                self.fault = str(error)
                self.control.cancel()
            self._revoked.set()
            self._active.cancel()
            if self._phase == "preparing":
                self._prepared = False
            elif self._stop_thread is None:
                self._stop_thread = threading.Thread(
                    target=self._stop, args=(self._active,), name="MG400-keyboard-stop",
                    daemon=True,
                )
                self._stop_thread.start()

    def release(self):
        """Revoke the current input immediately; request a stop on a worker."""
        self._revoke(physical_release=True)

    def close(self):
        """Permanently close this session and revoke its current input."""
        with self._lock:
            self._closed = True
        self.release()

    def _finish(self, active, *, read_pose=False):
        # Do not publish readiness while a send or stop thread can still act.
        pending = active._pending
        if pending is not None and not pending["done"].wait(1.0):
            self._revoke(error=RuntimeError("Keyboard command did not finish after cancellation"))
        with self._lock:
            stop_thread = self._stop_thread
        if stop_thread is not None:
            stop_thread.join()
            if not self._stop_result["ok"]:
                self._revoke(error=RuntimeError(
                    f"Keyboard stop unconfirmed: {self._stop_result.get('error', '')}"
                ))
        if read_pose and self.fault is None and not self.control.stopped:
            try:
                self._pose = active._read_pose({5})
            except Exception as error:
                self._revoke(error=error)
        active._operation_lock.release()
        with self._lock:
            self._active = self._phase = None

    def prepare(self):
        """Verify enabled idle state once and return measured User=0/Tool=0 pose."""
        active = self._start("preparing")
        try:
            active._preflight({5})
            pose = active._read_pose({5})
            with self._lock:
                if self._revoked.is_set() or active.stopped or self._closed:
                    raise RuntimeError("Keyboard preparation cancelled")
                self._pose = pose
                self._prepared = True
            return pose
        except Exception as error:
            if not self._revoked.is_set():
                self._revoke(error=error)
            raise
        finally:
            self._finish(active)

    def _check_hold(self, active, released, deadline):
        if released.is_set():
            self._revoke()
        elif time.monotonic() >= deadline:
            self._revoke(expired=True)
        if self._revoked.is_set() or active.stopped:
            raise RuntimeError("Keyboard hold cancelled")

    def _watch(self, active, released, deadline, finished):
        while not finished.wait(0.01):
            try:
                self._check_hold(active, released, deadline)
                active._fresh({5, 7})
            except Exception as error:
                if not self._revoked.is_set():
                    self._revoke(error=error)
                return

    def _step(self, active, key):
        sample, _ = active._fresh({5})
        pose = active._read_pose({5})
        joints = sample.joints_deg
        if key in "AD":
            target_joints = list(joints)
            target_joints[0] += STEP_DEG if key == "A" else -STEP_DEG
            target_joints = validate_joints(target_joints)
            validate_joint_segment(joints, target_joints)
            values = ",".join(f"{value:.6f}" for value in target_joints)
            command = f"JointMovJ({values},SpeedJ=5,AccJ=20)"
            target_pose = None
        else:
            target_pose = list(pose)
            if key in "WS":
                distance = STEP_MM if key == "W" else -STEP_MM
                angle = math.radians(joints[0])
                target_pose[0] += distance * math.cos(angle)
                target_pose[1] += distance * math.sin(angle)
            elif key in "RF":
                target_pose[2] += STEP_MM if key == "R" else -STEP_MM
            else:
                target_pose[3] += STEP_DEG if key == "Q" else -STEP_DEG
            target_pose = tuple(target_pose)
            values = ",".join(f"{value:.6f}" for value in target_pose)
            solution = tuple(float(value) for value in active._command(
                f"InverseSolution({values},0,0)"
            ).split(","))
            target_joints = validate_joints(solution)
            validate_joint_segment(joints, target_joints)
            command = f"MovL({values},User=0,Tool=0,SpeedL=2,AccL=20)"
        active._send_guard()
        active._guarded_command(
            command, modes={5, 7}, send_modes={5}, expected=joints, port=30003,
        )
        # Require another feedback receipt after the successful acknowledgement.
        _, before = active._fresh({5, 7})
        while True:
            active._send_guard()
            actual_pose = active._read_pose({5, 7})
            sample, received = active._fresh({5, 7})
            if (
                received > before and sample.mode == 5
                and active._near(sample.joints_deg, target_joints)
                and (target_pose is None or active._near(actual_pose, target_pose))
            ):
                self._pose = actual_pose
                return
            time.sleep(0.01)

    def run(self, key, released):
        """Run bounded moves until release or the five-second hold deadline."""
        if key not in KEYS:
            raise ValueError("Expected one keyboard movement key")
        active = self._start("running", released)
        deadline = time.monotonic() + HOLD_SECONDS
        finished = threading.Event()
        active._send_guard = lambda: self._check_hold(active, released, deadline)
        watcher = threading.Thread(
            target=self._watch, args=(active, released, deadline, finished),
            name="MG400-keyboard-watch", daemon=True,
        )
        watcher.start()
        try:
            while True:
                active._send_guard()
                self._step(active, key)
        except Exception as error:
            if not self._revoked.is_set():
                self._revoke(error=error)
        finally:
            finished.set()
            watcher.join()
            self._finish(active, read_pose=True)
        if self.fault is not None:
            raise RuntimeError(self.fault)
        if self.control.stopped:
            raise RuntimeError("Parent control stopped during keyboard movement")
        return self._pose
