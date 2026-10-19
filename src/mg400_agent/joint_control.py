"""Joint moves driven by explicit UI actions and an existing feedback reader.

Construction is inert. Methods block and belong on a worker thread, not Kit's UI
thread. A stopped instance never sends another enable or movement command.
"""

import json
import math
import socket
import threading
import time
from dataclasses import dataclass

from .config import finite_number
from .controller_protocol import ControllerCommandError, parse_controller_response
from .feedback import Feedback
from .joint_limits import validate_joint_segment, validate_joints


class TargetRejectedError(ValueError):
    """The target was rejected before its movement command was sent."""


@dataclass(frozen=True)
class PoseMeasurement:
    """One GetPose reply and the feedback that brackets its query."""

    control: "JointControl"
    pose: tuple[float, float, float, float]
    requested_at: float
    before: Feedback
    after: Feedback


class JointControl:
    def __init__(self, host, feedback_snapshot, *, parent=None, command_timeout=2.0):
        self.host = host
        self.feedback_snapshot = feedback_snapshot
        self._parent = parent
        self._command_timeout = command_timeout
        self._send_guard = None
        self._operation_lock = parent._operation_lock if parent else threading.Lock()
        self._dashboard_lock = parent._dashboard_lock if parent else threading.Lock()
        self._send_lock = parent._send_lock if parent else threading.Lock()
        self._stop_lock = threading.RLock()
        self._stopped = threading.Event()
        self._disable_requested = False
        self._disable_result = self._disable_error = None
        self._pending = None
        self._stop_result = None
        self._stop_uncertain = False

    @property
    def busy(self):
        return self._operation_lock.locked()

    @property
    def stopped(self):
        return self._stopped.is_set() or (self._parent is not None and self._parent.stopped)

    def cancel(self, *, disabling=False):
        """Immediately latch cancellation from the UI, without waiting or networking.

        Call disable() or stop() on a worker next to verify the requested state.
        """
        if disabling:
            self._disable_requested = True
        self._stopped.set()
        pending = self._pending
        if (
            pending is not None
            and pending["sent"]
            and not (pending["done"].is_set() and pending["ok"])
        ):
            self._stop_uncertain = True
            if self._parent is not None:
                self._parent._stop_uncertain = True

    @staticmethod
    def _near(first, second, tolerance=0.1):
        return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))

    def _fresh(self, allowed_modes):
        sample, received = self.feedback_snapshot()
        age = time.monotonic() - received
        if not math.isfinite(age) or not 0 <= age <= 0.25:
            raise RuntimeError("Robot feedback is absent or stale")
        if sample.error_status or sample.mode not in allowed_modes:
            raise RuntimeError(
                f"Unexpected robot state: mode={sample.mode}, error={sample.error_status}"
            )
        return sample, received

    def read_state(self) -> Feedback:
        """Read fresh idle feedback without sending any controller command."""
        return self._fresh({4, 5})[0]

    @staticmethod
    def _four_values(values):
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            raise ValueError("Expected four finite pose values in mm/degrees")
        try:
            return tuple(finite_number(value) for value in values)
        except OverflowError as error:
            raise ValueError("Expected four finite pose values in mm/degrees") from error

    def _read_pose_measurement(self, modes):
        requested_at = time.monotonic()
        before, _ = self._fresh(modes)
        values = tuple(float(value) for value in self._command("GetPose(0,0)").split(","))
        pose = self._four_values(values)
        after, _ = self._fresh(modes)
        return PoseMeasurement(self, pose, requested_at, before, after)

    def _read_pose(self, modes):
        return self._read_pose_measurement(modes).pose

    def read_pose(self):
        """Read measured XYZ in mm and R in degrees for User=0, Tool=0."""
        return self._read_pose({4, 5, 7})

    def read_pose_measurement(self):
        """Read a pose with its query time and fresh feedback before and after."""
        return self._read_pose_measurement({4, 5, 7})

    def _check_rejected_target_state(self, before, received):
        while True:
            after, updated = self._fresh({5})
            if self.stopped or not self._near(before.joints_deg, after.joints_deg):
                raise RuntimeError("Robot state changed during target validation")
            if updated > received:
                return
            time.sleep(0.01)

    def solve_pose(self, target):
        """Check a Cartesian target without sending a movement command."""
        target = self._four_values(target)
        before, received = self._fresh({5})
        values = ",".join(f"{value:.6f}" for value in target)
        # With six arguments, the controller chooses the nearest joint solution.
        try:
            payload = self._command(f"InverseSolution({values},0,0)")
        except ControllerCommandError as error:
            if error.code != -1:
                raise
            self._check_rejected_target_state(before, received)
            raise TargetRejectedError(
                f"Controller rejected the target (InverseSolution code {error.code}). "
                "This target was not sent."
            ) from error
        # Invalid reply data remains a fault, not a rejected target.
        solution = self._four_values(tuple(float(value) for value in payload.split(",")))
        try:
            return validate_joints(solution)
        except ValueError as error:
            self._check_rejected_target_state(before, received)
            raise TargetRejectedError(
                f"Target exceeds the configured joint range: {error}"
            ) from error

    def _command(self, text, port=29999, *, pending=None, stopping=False):
        """Send once; a reply must have a successful code and the matching echo."""
        if port != 29999:
            return self._exchange(text, port, pending=pending, stopping=stopping)
        # Hold one dashboard connection through its reply and close. The motion
        # port uses a separate connection so its pending ACK cannot block a stop.
        if not self._dashboard_lock.acquire(timeout=self._command_timeout):
            raise TimeoutError("Dashboard connection is busy")
        try:
            return self._exchange(text, port, pending=pending, stopping=stopping)
        finally:
            self._dashboard_lock.release()

    def _exchange(self, text, port, *, pending=None, stopping=False):
        deadline = time.monotonic() + self._command_timeout
        with socket.create_connection(
            (self.host, port), timeout=self._command_timeout
        ) as connection:
            with self._send_lock:
                if pending is not None and self._send_guard is not None:
                    self._send_guard()
                if pending is not None and self.stopped and not stopping:
                    raise RuntimeError("Control stopped before command was sent")
                if pending is not None:
                    sample, _ = self._fresh(pending["send_modes"])
                    if not self._near(sample.joints_deg, pending["expected"]):
                        raise RuntimeError("Robot moved before command was sent")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Controller connection timed out")
                connection.settimeout(remaining)
                if pending is not None:
                    if self.stopped:
                        raise RuntimeError("Control stopped before command was sent")
                    # Even a partial failed send makes command acceptance uncertain.
                    pending["sent"] = True
                connection.sendall(text.encode("ascii"))
            packet = bytearray()
            while b";" not in packet:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Controller acknowledgement timed out: {text}")
                connection.settimeout(remaining)
                part = connection.recv(4096 - len(packet))
                if not part:
                    raise ConnectionError("Controller disconnected before acknowledgement")
                packet.extend(part)
                if len(packet) >= 4096:
                    raise ValueError("Oversized controller response")
        return parse_controller_response(packet, text)

    def _dashboard_state(self):
        mode = int(self._command("RobotMode()"))
        errors = json.loads(self._command("GetErrorID()"))
        if not isinstance(errors, list) or not errors or any(item != [] for item in errors):
            raise RuntimeError(f"Controller alarms require inspection: {errors}")
        joints = tuple(float(value) for value in self._command("GetAngle()").split(","))
        if len(joints) not in (4, 6) or not all(map(math.isfinite, joints)):
            raise ValueError("Invalid actual joint angles")
        return mode, joints[:4]

    def _wait_stable(self, target=None, *, modes, final_modes, timeout=5.0, stopping=False):
        deadline = time.monotonic() + timeout
        previous_received = None
        anchor = stable_since = None
        count = 0
        while time.monotonic() < deadline:
            if self.stopped and not stopping:
                raise RuntimeError("Control has been stopped; reopen the panel")
            sample, received = self._fresh(modes)
            if received != previous_received:
                previous_received = received
                ready = sample.mode in final_modes and (
                    target is None or self._near(sample.joints_deg, target)
                )
                if not ready:
                    anchor = stable_since = None
                    count = 0
                elif anchor is None or not self._near(sample.joints_deg, anchor, 0.025):
                    anchor, stable_since, count = sample.joints_deg, received, 1
                else:
                    count += 1
                    if count >= 3 and received - stable_since >= 0.4:
                        return sample
            time.sleep(0.01)
        raise TimeoutError("Robot did not reach a fresh, verified stationary state")

    def _preflight(self, modes):
        self._fresh(modes)
        mode, joints = self._dashboard_state()
        sample, _ = self._fresh(modes)
        if mode != sample.mode or not self._near(joints, sample.joints_deg):
            raise RuntimeError("Dashboard and passive feedback disagree")
        self._wait_stable(joints, modes={mode}, final_modes={mode})
        return mode, joints

    def _guarded_command(self, text, *, modes, send_modes, expected, port=29999):
        self._fresh(send_modes)
        pending = {
            "done": threading.Event(),
            "sent": False,
            "ok": False,
            "value": None,
            "send_modes": send_modes,
            "expected": expected,
        }
        with self._send_lock:
            if self._send_guard is not None:
                self._send_guard()
            if self.stopped:
                raise RuntimeError("Control has been stopped; reopen the panel")
            self._pending = pending
            if self._parent is not None:
                self._parent._pending = pending

        def send():
            try:
                pending["value"] = self._command(text, port, pending=pending)
                pending["ok"] = True
            except Exception as error:
                pending["value"] = error
            finally:
                # cancel() intentionally takes no lock. It can race with the final
                # send check; preserve uncertainty before publishing the ACK result.
                if self.stopped and pending["sent"]:
                    self._stop_uncertain = True
                    if self._parent is not None:
                        self._parent._stop_uncertain = True
                pending["done"].set()

        threading.Thread(target=send, name="MG400-command", daemon=True).start()
        while not pending["done"].wait(0.01):
            if self.stopped:
                raise RuntimeError("Control stopped while waiting for acknowledgement")
            self._fresh(modes)
        if not pending["ok"]:
            raise pending["value"]
        if self.stopped:
            raise RuntimeError("Control has been stopped; reopen the panel")
        self._fresh(modes)

    def _begin(self):
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another robot operation is in progress")
        if self.stopped:
            self._operation_lock.release()
            raise RuntimeError("Control has been stopped; reopen the panel")

    def _failed(self, error, *, disabling=False):
        with self._stop_lock:
            # The explicit disable owns cancellation of the previous operation.
            if self._disable_requested and not disabling:
                return
            result = self.stop()
        if not result["ok"]:
            raise RuntimeError(
                f"{error}; physical stop not confirmed: {result['error']}; "
                "use the physical emergency stop"
            ) from error

    def enable(self) -> Feedback:
        """Explicitly enable using the existing payload settings, without moving."""
        self._begin()
        try:
            mode, joints = self._preflight({4, 5})
            if mode == 4:
                self._guarded_command(
                    "EnableRobot()", modes={4, 5}, send_modes={4}, expected=joints
                )
            sample = self._wait_stable(joints, modes={4, 5}, final_modes={5})
            actual_mode, actual = self._dashboard_state()
            sample = self._fresh({5})[0]
            if (
                actual_mode != 5
                or not self._near(actual, joints)
                or not self._near(sample.joints_deg, joints)
            ):
                raise RuntimeError("Enabled idle state was not confirmed")
            return sample
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self._operation_lock.release()

    def move(self, target) -> Feedback:
        """Move J1-J4 to absolute angles in degrees within the validated joint limits."""
        target = validate_joints(target)
        sample = self.read_state()
        if sample.mode != 5:
            raise RuntimeError("Robot must be enabled and idle before movement")
        validate_joint_segment(sample.joints_deg, target)
        self._begin()
        try:
            _, start = self._preflight({5})
            validate_joint_segment(start, target)
            values = ",".join(f"{value:.6f}" for value in target)
            self._guarded_command(
                f"JointMovJ({values},SpeedJ=5,AccJ=5)",
                modes={5, 7},
                send_modes={5},
                expected=start,
                port=30003,
            )
            sample = self._wait_stable(target, modes={5, 7}, final_modes={5}, timeout=120.0)
            mode, actual = self._dashboard_state()
            sample = self._fresh({5})[0]
            if (
                mode != 5
                or not self._near(actual, target)
                or not self._near(sample.joints_deg, target)
            ):
                raise RuntimeError("Final dashboard position does not verify arrival")
            return sample
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self._operation_lock.release()

    def _wait_linear(self, target, joints, timeout=120.0):
        deadline = time.monotonic() + timeout
        previous_received = None
        anchor = stable_since = None
        count = 0
        while time.monotonic() < deadline:
            if self.stopped:
                raise RuntimeError("Control has been stopped; reopen the panel")
            pose = self._read_pose({5, 7})
            sample, received = self._fresh({5, 7})
            if received != previous_received:
                previous_received = received
                ready = (
                    sample.mode == 5
                    and self._near(pose, target)
                    and self._near(sample.joints_deg, joints)
                )
                if not ready:
                    anchor = stable_since = None
                    count = 0
                elif anchor is None or not self._near(sample.joints_deg, anchor, 0.025):
                    anchor, stable_since, count = sample.joints_deg, received, 1
                else:
                    count += 1
                    if count >= 3 and received - stable_since >= 0.4:
                        return sample
            time.sleep(0.05)
        raise TimeoutError("Robot did not reach a fresh, verified Cartesian target")

    def move_linear(self, target) -> Feedback:
        """Move to absolute User=0/Tool=0 XYZ/R and verify measured arrival.

        The controller solves the endpoint and plans the Cartesian line.
        R is flange yaw, not the J4 joint angle.
        """
        target = self._four_values(target)
        if self.read_state().mode != 5:
            raise RuntimeError("Robot must be enabled and idle before movement")
        self._begin()
        try:
            _, start = self._preflight({5})
            start_pose = self._read_pose({5})
            values = ",".join(f"{value:.6f}" for value in target)
            target_joints = self.solve_pose(target)
            if not self._near(self._read_pose({5}), start_pose):
                raise RuntimeError("Robot pose changed before Cartesian movement")
            self._guarded_command(
                f"MovL({values},User=0,Tool=0,SpeedL=1,AccL=1)",
                modes={5, 7},
                send_modes={5},
                expected=start,
                port=30003,
            )
            self._wait_linear(target, target_joints)
            mode, actual = self._dashboard_state()
            pose = self._read_pose({5})
            sample = self._fresh({5})[0]
            if (
                mode != 5
                or not self._near(actual, target_joints)
                or not self._near(sample.joints_deg, target_joints)
                or not self._near(pose, target)
            ):
                raise RuntimeError("Final dashboard position does not verify Cartesian arrival")
            return sample
        except TargetRejectedError:
            raise
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self._operation_lock.release()

    def disable(self) -> Feedback:
        """Cancel this instance and verify that the robot is disabled and stationary.

        Create a new instance only after this succeeds and the previous operation exits.
        """
        self.cancel(disabling=True)
        with self._send_lock:
            # Record uncertainty if a send raced with the immediate cancellation.
            self.cancel(disabling=True)
        with self._stop_lock:
            if self._disable_error is not None:
                raise self._disable_error
            if self._disable_result is not None:
                return self._disable_result
            try:
                self._command("DisableRobot()", stopping=True)
                if self._stop_uncertain:
                    raise RuntimeError(
                        "A command acknowledgement was unresolved or failed during disable. "
                        "Command ordering remains unknown."
                    )
                stationary = self._wait_stable(
                    modes={4, 5, 7}, final_modes={4}, timeout=5.0, stopping=True
                )
                mode, actual = self._dashboard_state()
                sample = self._fresh({4})[0]
                if (
                    mode != 4
                    or not self._near(actual, stationary.joints_deg)
                    or not self._near(sample.joints_deg, stationary.joints_deg)
                ):
                    raise RuntimeError("Disabled stationary state was not confirmed")
                self._disable_result = sample
                return sample
            except Exception as error:
                try:
                    self._failed(error, disabling=True)
                except Exception as failure:
                    error = failure
                self._disable_error = error
                raise error

    def stop(self):
        """Permanently cancel this instance, request a controlled stop, verify rest.

        An unresolved mutation ACK makes ordering across connections unknown;
        even a later successful ACK cannot establish a confirmed stop.
        """
        self.cancel()
        with self._send_lock:
            pending = self._pending
            uncertain = (
                pending is not None
                and pending["sent"]
                and not (pending["done"].is_set() and pending["ok"])
            )
            if uncertain:
                self._stop_uncertain = True
        with self._stop_lock:
            if self._stop_result is not None:
                return self._stop_result
            try:
                self._command("ResetRobot()", stopping=True)
                if self._stop_uncertain:
                    raise RuntimeError(
                        "Mutation acknowledgement was unresolved or failed when stopping; "
                        "command ordering remains unknown"
                    )
                self._wait_stable(modes={4, 5, 7}, final_modes={4, 5}, timeout=3, stopping=True)
                self._stop_result = {
                    "ok": True,
                    "stationary_verified": True,
                    "physical_stop_requested": True,
                }
            except Exception as error:
                self._stop_result = {
                    "ok": False,
                    "stationary_verified": False,
                    "physical_stop_requested": True,
                    "error": str(error),
                }
            return self._stop_result
