"""Single-key jogging with checked feedback and explicit stop confirmation."""

import math
import threading
import time

from . import joint_control
from .joint_control import TargetRejectedError
from .joint_limits import validate_joint_segment, validate_joints, validate_measured_joints

SEGMENT_DISTANCE_MM = 50.0
SEGMENT_ANGLE_DEG = 10.0
KEYS = frozenset("WASDRFQE")
MOVEJOG_COMMANDS = {
    "A": "MoveJog(J1+)",
    "D": "MoveJog(J1-)",
    "Q": "MoveJog(J4+)",
    "E": "MoveJog(J4-)",
    "R": "MoveJog(Z+,CoordType=0,User=0,Tool=0)",
    "F": "MoveJog(Z-,CoordType=0,User=0,Tool=0)",
}


class KeyboardJogSession:
    """Prepare once, then run one cancellable hold at a time on worker threads.

    release() and close() are nonblocking UI calls. A normal release preserves
    readiness after the stop is verified. Any uncertain stop locks both this
    session and its parent control. Preparation sets SpeedFactor(1) without motion.
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
        self.measured_pose = None
        self._measurement = None
        self.fault = None
        self.limit_reason = None
        self._continuous = False

    @property
    def busy(self):
        with self._lock:
            return self._active is not None

    @property
    def ready(self):
        with self._lock:
            return (
                self._prepared
                and self._active is None
                and not self._closed
                and self.fault is None
                and not self.control.stopped
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
                raise RuntimeError("Release the current key before another hold")
            active = self.control.begin_operation(
                command_timeout=0.5,
                send_guard=(lambda: self._check_send(active, released))
                if phase == "running"
                else None,
            )
            self._active, self._phase = active, phase
            self._revoked = threading.Event()
            self._released = released
            self._stop_thread = self._stop_result = None
            self.limit_reason = None
            self._continuous = False
            return active

    def _stop(self, active):
        try:
            result = active.stop_after_command(
                jog=self._continuous,
                on_error=lambda error: self._revoke(error=error),
            )
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        with self._lock:
            self._stop_result = result

    def _revoke(self, *, error=None, physical_release=False):
        with self._lock:
            if self._active is None:
                if physical_release and self._release_gate is not None:
                    self._release_gate.set()
                return
            if physical_release and self._released is not None:
                self._released.set()
            if error is not None and self.fault is None:
                self.fault = str(error)
                self.control.cancel()
            self._revoked.set()
            if error is not None or self._phase == "preparing" or self.control.stopped:
                self._active.cancel()
            if self._phase == "preparing":
                self._prepared = False
            elif self._stop_thread is None:
                self._stop_thread = threading.Thread(
                    target=self._stop,
                    args=(self._active,),
                    name="MG400-keyboard-stop",
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

    def _read_measured_pose(self, active, modes):
        measurement = active._read_pose_measurement(modes)
        self._measurement = measurement
        self.measured_pose = (measurement.pose, measurement.requested_at)
        return measurement.pose

    def _finish(self, active, *, read_pose=False):
        # Do not publish readiness while a send or stop thread can still act.
        if not active.wait_for_command():
            self._revoke(error=RuntimeError("Keyboard command did not finish after cancellation"))
        while True:
            with self._lock:
                stop_thread = self._stop_thread
            if stop_thread is not None:
                stop_thread.join()
                if not self._stop_result["ok"]:
                    self._revoke(
                        error=RuntimeError(
                            f"Keyboard stop unconfirmed: {self._stop_result.get('error', '')}"
                        )
                    )
            if read_pose and self.fault is None and not self.control.stopped:
                try:
                    self._pose = self._read_measured_pose(active, {5})
                except joint_control.DashboardQueryCancelledError as error:
                    if self._revoked.is_set() and self._stop_thread is not stop_thread:
                        # A release interrupted this read. Verify its stop, then
                        # get a new measured pose instead of returning the old one.
                        continue
                    self._revoke(error=error)
                except joint_control.PreparationRejectedError as error:
                    self.limit_reason = str(error)
                    self._release_gate = self._released
                except Exception as error:
                    self._revoke(error=error)
            read_pose = False
            with self._lock:
                # A release can start a stop while the final pose read is pending.
                if self._stop_thread is not stop_thread:
                    continue
                active.finish_operation()
                self._active = self._phase = None
                return

    def prepare(self):
        """Verify enabled idle state and set the low jogging speed without motion."""
        active = self._start("preparing")
        try:
            validate_measured_joints(active._fresh({5})[0].joints_deg)
            _, joints = active._preflight({5})
            validate_measured_joints(joints)
            pose = self._read_measured_pose(active, {5})
            if self._revoked.is_set() or active.stopped:
                raise RuntimeError("Keyboard preparation cancelled")
            active._guarded_command("SpeedFactor(1)", modes={5}, send_modes={5}, expected=joints)
            acknowledged_at = time.monotonic()
            receipts = set()
            while len(receipts) < 3:
                if self._revoked.is_set() or active.stopped:
                    raise RuntimeError("Keyboard preparation cancelled")
                sample, received = active._fresh({5})
                if received > acknowledged_at:
                    if sample.speed_scaling_raw != 1:
                        raise RuntimeError(
                            "Keyboard speed feedback does not confirm SpeedFactor(1)"
                        )
                    receipts.add(received)
                if time.monotonic() - acknowledged_at > 0.5:
                    raise TimeoutError("Keyboard speed feedback was not confirmed")
                time.sleep(0.005)
            with self._lock:
                if self._revoked.is_set() or active.stopped or self._closed:
                    raise RuntimeError("Keyboard preparation cancelled")
                self._pose = pose
                self._prepared = True
            return pose
        except joint_control.PreparationRejectedError:
            self._prepared = False
            raise
        except Exception as error:
            if not self._revoked.is_set():
                self._revoke(error=error)
            raise
        finally:
            self._finish(active)

    def _check_hold(self, active, released):
        if released.is_set():
            self._revoke()
        if self._revoked.is_set() or active.stopped:
            raise RuntimeError("Keyboard hold cancelled")

    def _check_send(self, active, released):
        self._check_hold(active, released)
        if self._continuous:
            sample, _ = active._fresh({5})
            if sample.speed_scaling_raw != 1:
                raise RuntimeError("Keyboard jogging speed changed")
            try:
                validate_joints(sample.joints_deg)
            except ValueError as error:
                raise TargetRejectedError(str(error)) from error

    def _watch(self, active, released, finished):
        while not finished.wait(0.01):
            try:
                self._check_hold(active, released)
                sample = active._fresh({5, 7, 11} if self._continuous else {5, 7})[0]
                validate_measured_joints(sample.joints_deg)
                if self._continuous:
                    if sample.speed_scaling_raw != 1:
                        raise RuntimeError("Keyboard jogging speed changed")
                    try:
                        validate_joints(sample.joints_deg)
                    except ValueError as error:
                        self.limit_reason = str(error)
                        self._release_gate = released
                        self._revoke()
                        return
            except Exception as error:
                if not self._revoked.is_set():
                    self._revoke(error=error)
                return

    def reusable_pose(self, measurement, *, active=None):
        """Check a measured pose against its source, request time, and idle feedback."""
        from .joint_control import PoseMeasurement

        if not isinstance(measurement, PoseMeasurement):
            return False
        if measurement.control is not self.control and measurement.control is not active:
            return False
        sample, _ = (active or self.control)._fresh({5})
        return (
            0 <= time.monotonic() - measurement.requested_at <= 0.25
            and measurement.before.mode == measurement.after.mode == 5
            and not measurement.before.error_status
            and not measurement.after.error_status
            and self.control._near(
                measurement.before.joints_deg, measurement.after.joints_deg, 0.025
            )
            and self.control._near(measurement.before.joints_deg, sample.joints_deg, 0.025)
            and self.control._near(measurement.after.joints_deg, sample.joints_deg, 0.025)
        )

    def _step(self, active, key, initial_pose):
        sample, _ = active._fresh({5})
        if key in "AD":
            pose = None
        elif self.reusable_pose(initial_pose, active=active):
            pose = initial_pose.pose
            self._measurement = initial_pose
            self.measured_pose = (pose, initial_pose.requested_at)
        else:
            pose = self._read_measured_pose(active, {5})
        joints = validate_measured_joints(sample.joints_deg)
        distance = SEGMENT_DISTANCE_MM if key in "WSRF" else SEGMENT_ANGLE_DEG
        minimum = 0.1
        while True:
            self._check_hold(active, self._released)
            try:
                if key in "AD":
                    target_joints = list(joints)
                    target_joints[0] += distance if key == "A" else -distance
                    target_pose = None
                else:
                    target_pose = list(pose)
                    signed_distance = distance if key in "WRQ" else -distance
                    if key in "WS":
                        angle = math.radians(joints[0])
                        target_pose[0] += signed_distance * math.cos(angle)
                        target_pose[1] += signed_distance * math.sin(angle)
                    elif key in "RF":
                        target_pose[2] += signed_distance
                    else:
                        target_pose[3] += signed_distance
                    target_pose = tuple(target_pose)
                    target_joints = active.solve_pose(target_pose)
                try:
                    target_joints = validate_joints(target_joints)
                    validate_joint_segment(joints, target_joints)
                except ValueError as error:
                    raise TargetRejectedError(str(error)) from error
                break
            except TargetRejectedError:
                # Only rejected targets can shorten the hold. No movement has
                # been sent. Query failures do not shorten the target.
                if distance <= minimum:
                    raise
                distance = max(minimum, distance / 2)
        if target_pose is None:
            values = ",".join(f"{value:.6f}" for value in target_joints)
            command = f"JointMovJ({values},SpeedJ=5,AccJ=20)"
        else:
            values = ",".join(f"{value:.6f}" for value in target_pose)
            command = f"MovL({values},User=0,Tool=0,SpeedL=2,AccL=20)"
        self._check_hold(active, self._released)
        active._guarded_command(
            command,
            modes={5, 7},
            send_modes={5},
            expected=joints,
            port=30003,
        )
        origin = joints if target_pose is None else pose
        destination = target_joints if target_pose is None else target_pose
        # Require another feedback receipt after the successful acknowledgement.
        _, before = active._fresh({5, 7})
        while True:
            self._check_hold(active, self._released)
            actual_pose = (
                self._read_measured_pose(active, {5, 7}) if target_pose is not None else None
            )
            sample, received = active._fresh({5, 7})
            validate_measured_joints(sample.joints_deg)
            measured = sample.joints_deg if target_pose is None else actual_pose
            # Keep the target tolerance, but a microstep needs actual progress
            # along its commanded direction, measured from the same source.
            progress = (
                sum(
                    (actual - start) * (end - start)
                    for actual, start, end in zip(measured, origin, destination, strict=True)
                )
                / distance
            )
            if (
                received > before
                and sample.mode == 5
                and (distance >= 0.2 or progress >= distance / 2)
                and active._near(sample.joints_deg, target_joints)
                and (target_pose is None or active._near(actual_pose, target_pose))
            ):
                if actual_pose is not None:
                    self._pose = actual_pose
                return
            time.sleep(0.01)

    def run(self, key, released, initial_pose=None):
        """Jog until release or a guard. Keep radial motion on checked trajectories."""
        if key not in KEYS:
            raise ValueError("Expected one keyboard movement key")
        active = self._start("running", released)
        if key in MOVEJOG_COMMANDS:
            try:
                validate_joints(active._fresh({5})[0].joints_deg)
            except ValueError:
                # Preserve finite inward moves from the target margin band.
                pass
            except Exception as error:
                self._revoke(error=error)
            else:
                self._continuous = True
        finished = threading.Event()
        watcher = threading.Thread(
            target=self._watch,
            args=(active, released, finished),
            name="MG400-keyboard-watch",
            daemon=True,
        )
        watcher.start()
        read_pose = not self._continuous
        try:
            self._check_hold(active, released)
            if self._continuous:
                sample, _ = active._fresh({5})
                if sample.speed_scaling_raw != 1:
                    raise RuntimeError("Keyboard jogging speed changed")
                active._guarded_command(
                    MOVEJOG_COMMANDS[key],
                    modes={5, 7, 11},
                    send_modes={5},
                    expected=sample.joints_deg,
                    port=30003,
                )
                while True:
                    self._check_hold(active, released)
                    time.sleep(0.005)
            else:
                while True:
                    self._step(active, key, initial_pose)
                    self._check_hold(active, released)
                    initial_pose = self._measurement if key not in "AD" else None
        except (TargetRejectedError, joint_control.PreparationRejectedError) as error:
            if isinstance(error, joint_control.PreparationRejectedError):
                read_pose = False
            try:
                self._check_hold(active, released)
                active._fresh({5})
            except Exception as state_error:
                if not self._revoked.is_set():
                    self._revoke(error=state_error)
            else:
                with self._lock:
                    if not self._revoked.is_set() and not active.stopped:
                        self.limit_reason = str(error)
                        self._release_gate = released
        except Exception as error:
            if not self._revoked.is_set():
                self._revoke(error=error)
        finally:
            finished.set()
            watcher.join()
            self._finish(active, read_pose=read_pose)
        if self.fault is not None:
            raise RuntimeError(self.fault)
        if self.control.stopped:
            raise RuntimeError("Parent control stopped during keyboard movement")
        # The panel refreshes Cartesian coordinates after the hold. A dashboard
        # query here would delay the next key after stop confirmation.
        return None if self._continuous else self._pose
