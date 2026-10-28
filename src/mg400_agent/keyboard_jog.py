"""Single-key jogging with checked feedback and explicit stop confirmation."""

import math
import threading
import time

from . import joint_control
from .joint_control import TargetRejectedError
from .joint_limits import (
    MARGIN_DEG,
    limit_distances,
    validate_joint_segment,
    validate_joints,
    validate_measured_joints,
)

SEGMENT_DISTANCE_MM = 50.0
SEGMENT_ANGLE_DEG = 10.0
KEYS = frozenset("WASDRFQE")
# A continuous jog moves on after the guard trips. At 40% speed a W hold passed the
# 1.05-degree J3-J2 margin by 1.9 degrees and raised controller alarm 72. Stopping
# distance grows linearly with SpeedFactor, which scales velocity and acceleration.
# A continuous hold therefore stops early when it closes on a bound inside a
# speed-scaled band. Holds that move away from or along a bound are not limited.
CONTINUOUS_MARGIN_DEG_PER_PERCENT = 0.08
# A distance must shrink by more than feedback noise to count as closing on a bound.
APPROACH_EPSILON_DEG = 0.02
RADIAL_USER = 8
MOVEJOG_COMMANDS = {
    "W": f"MoveJog(X+,CoordType=0,User={RADIAL_USER},Tool=0)",
    "S": f"MoveJog(X-,CoordType=0,User={RADIAL_USER},Tool=0)",
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
    session and its parent control. Preparation sets the selected speed without motion.
    """

    def __init__(self, control, *, speed_percent=1):
        if type(speed_percent) is not int or not 1 <= speed_percent <= 100:
            raise ValueError("Keyboard speed must be an integer from 1 to 100")
        self.control = control
        self._speed_percent = speed_percent
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
        self._radial_j1 = None
        self._approach = {}

    @property
    def speed_percent(self):
        return self._speed_percent

    @property
    def continuous_margin(self):
        """Distance in degrees inside which a continuous hold may not close on a bound."""
        return MARGIN_DEG + CONTINUOUS_MARGIN_DEG_PER_PERCENT * self._speed_percent

    def _check_approach(self, joints):
        """Reject a continuous hold that closes on a bound inside the speed-scaled band.

        Each bound keeps the largest distance seen in this hold. A slow hold that
        closes by less than the noise per sample still counts once the total does.
        """
        band = self.continuous_margin
        for name, side, value, limit, distance in limit_distances(joints):
            farthest = self._approach.get((name, side))
            if farthest is None or distance > farthest:
                self._approach[(name, side)] = distance
            elif distance < band and distance < farthest - APPROACH_EPSILON_DEG:
                raise ValueError(
                    f"{name}={value:g} degrees is closing on its reference limit {limit:g} "
                    f"inside the {band:g}-degree stop band for {self.speed_percent}% speed"
                )

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
            self._radial_j1 = None
            self._approach = {}
            return active

    def _stop(self, active):
        try:
            result = active.stop_after_command(
                jog=self._continuous,
                jog_speed_percent=self.speed_percent,
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
        """Verify enabled idle state and set the selected speed without motion."""
        active = self._start("preparing")
        try:
            validate_measured_joints(active._fresh({5})[0].joints_deg)
            _, joints = active._preflight({5})
            validate_measured_joints(joints)
            pose = self._read_measured_pose(active, {5})
            if self._revoked.is_set() or active.stopped:
                raise RuntimeError("Keyboard preparation cancelled")
            active._guarded_command(
                f"SpeedFactor({self.speed_percent})", modes={5}, send_modes={5}, expected=joints
            )
            acknowledged_at = time.monotonic()
            receipts = set()
            while len(receipts) < 3:
                if self._revoked.is_set() or active.stopped:
                    raise RuntimeError("Keyboard preparation cancelled")
                sample, received = active._fresh({5})
                if received > acknowledged_at:
                    if sample.speed_scaling_raw != self.speed_percent:
                        raise RuntimeError(
                            "Keyboard speed feedback does not confirm "
                            f"SpeedFactor({self.speed_percent})"
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
        if released.is_set() or self._closed:
            self._revoke()
        if self._revoked.is_set() or active.stopped:
            raise RuntimeError("Keyboard hold cancelled")

    def _check_send(self, active, released):
        self._check_hold(active, released)
        sample, _ = active._fresh({5})
        if sample.speed_scaling_raw != self.speed_percent:
            raise RuntimeError("Keyboard jogging speed changed")
        self._check_radial_direction(sample.joints_deg)
        if self._continuous:
            try:
                validate_joints(sample.joints_deg)
            except ValueError as error:
                raise TargetRejectedError(str(error)) from error

    def _watch(self, active, released, finished):
        while not finished.wait(0.01):
            try:
                self._check_hold(active, released)
                continuous = self._continuous
                sample = active._fresh({5, 7, 11})[0]
                # Promotion can occur during this read. Accept its first jog
                # frame, but do not apply inner limits to an older retreat frame.
                if sample.mode == 11 and not self._continuous:
                    raise RuntimeError("Unexpected jog state during finite keyboard movement")
                validate_measured_joints(sample.joints_deg)
                if sample.speed_scaling_raw != self.speed_percent:
                    raise RuntimeError("Keyboard jogging speed changed")
                self._check_radial_direction(sample.joints_deg)
                if continuous:
                    try:
                        validate_joints(sample.joints_deg)
                        self._check_approach(sample.joints_deg)
                    except ValueError as error:
                        self.limit_reason = str(error)
                        self._release_gate = released
                        self._revoke()
                        return
            except Exception as error:
                if not self._revoked.is_set():
                    self._revoke(error=error)
                return

    def _check_radial_direction(self, joints):
        if self._radial_j1 is not None and abs(joints[0] - self._radial_j1) > 0.1:
            raise RuntimeError("J1 changed during radial keyboard jogging")

    def _prepare_radial_frame(self, active, joints):
        """Align the reserved user frame while idle; never change the active User."""

        def read_frame(index):
            self._check_hold(active, self._released)
            payload = active._query(f"CalcUser({index},1,{{0,0,0,0}})")
            values = active._four_values(tuple(float(value) for value in payload.split(",")))
            self._check_hold(active, self._released)
            active._fresh({5})
            return values

        def matches(actual, expected):
            return all(abs(a - b) <= 0.00001 for a, b in zip(actual, expected, strict=True))

        if not matches(read_frame(0), (0.0, 0.0, 0.0, 0.0)):
            raise RuntimeError("Radial keyboard jogging requires an unchanged User 0")
        target = (0.0, 0.0, 0.0, joints[0])
        frame = read_frame(RADIAL_USER)
        if not matches(frame, target):
            values = ",".join(f"{value:.6f}" for value in target)
            active._guarded_command(
                f"SetUser({RADIAL_USER},{{{values}}})",
                modes={5},
                send_modes={5},
                expected=joints,
            )
            frame = read_frame(RADIAL_USER)
        if not matches(frame, target):
            raise RuntimeError("Radial keyboard User 8 readback does not match J1")
        self._radial_j1 = joints[0]

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
        recovery_distance = 2.0 if key in "WSRF" else 1.0
        recovery_target = None
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
                if key not in MOVEJOG_COMMANDS or distance <= recovery_distance:
                    break
                # Use a short checked retreat before resuming continuous jogging.
                recovery_target = target_pose, target_joints, distance
                distance /= 2
            except TargetRejectedError:
                if recovery_target is not None:
                    target_pose, target_joints, distance = recovery_target
                    break
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
        """Jog until release or a guard, with finite inward moves at the margin."""
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
            while True:
                self._check_hold(active, released)
                sample, _ = active._fresh({5})
                if sample.speed_scaling_raw != self.speed_percent:
                    raise RuntimeError("Keyboard jogging speed changed")
                if self._continuous:
                    if key in "WS":
                        self._prepare_radial_frame(active, sample.joints_deg)
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
                self._step(active, key, initial_pose)
                self._check_hold(active, released)
                initial_pose = self._measurement if key not in "AD" else None
                if key in MOVEJOG_COMMANDS:
                    try:
                        validate_joints(active._fresh({5})[0].joints_deg)
                    except ValueError:
                        continue
                    with self._lock:
                        if (
                            self._active is active
                            and self._stop_thread is None
                            and not self._closed
                            and not (self._revoked.is_set() or released.is_set() or active.stopped)
                        ):
                            self._continuous = True
                            read_pose = False
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
