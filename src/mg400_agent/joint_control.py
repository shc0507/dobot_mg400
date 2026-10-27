"""Joint moves driven by explicit UI actions and an existing feedback reader.

Construction is inert. Methods block and belong on a worker thread, not Kit's UI
thread. A stopped instance never sends another enable or movement command.
"""

import errno
import json
import math
import select
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from .config import finite_number
from .controller_protocol import ControllerCommandError, parse_controller_response
from .feedback import Feedback
from .joint_limits import validate_joint_segment, validate_joints, validate_measured_joints


class TargetRejectedError(ValueError):
    """The target was rejected before its movement command was sent."""


class PreparationRejectedError(RuntimeError):
    """A precheck rejected this attempt before it sent an enable or movement command."""


class _StationaryTimeoutError(TimeoutError):
    """Fresh feedback did not confirm a stationary state within the allowed time."""


class DashboardQueryCancelledError(ConnectionAbortedError):
    """A stop request cancelled this read-only query."""


@dataclass(frozen=True)
class PoseMeasurement:
    """One GetPose reply and the feedback that brackets its query."""

    control: "JointControl"
    pose: tuple[float, float, float, float]
    requested_at: float
    before: Feedback
    after: Feedback


class JointControl:
    def __init__(
        self, host, feedback_snapshot, *, parent=None, command_timeout=2.0, send_guard=None
    ):
        self.host = host
        self.feedback_snapshot = feedback_snapshot
        self._parent = parent
        self._command_timeout = command_timeout
        self._send_guard = send_guard
        self._operation_lock = parent._operation_lock if parent else threading.Lock()
        self._operation_guard = threading.Lock()
        self._operation_open = False
        self._dashboard_lock = parent._dashboard_lock if parent else threading.Lock()
        self._dashboard_guard = parent._dashboard_guard if parent else threading.Lock()
        self._dashboard = (
            parent._dashboard
            if parent
            else {
                "query": None,
                "stops": 0,
                "generation": 0,
            }
        )
        self._send_lock = parent._send_lock if parent else threading.Lock()
        self._stop_lock = threading.RLock()
        self._stopped = threading.Event()
        self._disable_requested = False
        self._disable_result = self._disable_error = None
        self._pending = None
        self._stop_result = None
        self._stop_uncertain = False
        self._child_stop_error = None
        self._preparing = False
        self._preparation_pending = None
        self._preparation_baseline = None
        self._jog_connection = None
        self._jog_connection_failed = False
        self._jog_connection_lock = threading.Lock()

    @property
    def busy(self):
        return self._operation_lock.locked()

    @property
    def stopped(self):
        return self._stopped.is_set() or (self._parent is not None and self._parent.stopped)

    @property
    def command_pending(self):
        """Whether a registered command worker has not finished."""
        pending = self._pending
        return pending is not None and not pending["done"].is_set()

    def feedback_retention_reason(self):
        """Return why command state requires feedback to remain connected.

        None does not prove rest. Drain caller tasks, verify_stationary(), then
        check again. A concurrent stop or disable invalidates prior verification.
        """
        if self._stop_uncertain:
            return "Command ordering remains unknown."
        if self._disable_error is not None:
            return f"Disable is unconfirmed: {self._disable_error}"
        if self._child_stop_error is not None:
            return f"Operation stop is unconfirmed: {self._child_stop_error}"
        pending = self._pending
        if pending is not None:
            if not pending["done"].is_set():
                return "A controller acknowledgement is pending."
            if pending["sent"] and not pending["ok"]:
                return "A controller acknowledgement failed."
        if self._stop_result is not None and not self._stop_result["ok"]:
            return f"Control stop is unconfirmed: {self._stop_result['error']}"
        if self.stopped and self._stop_result is None and self._disable_result is None:
            return "Cancellation has no confirmed stop or disable result."
        if self.busy:
            return "A robot operation is still in progress."
        return None

    def begin_operation(self, *, command_timeout=0.5, send_guard=None):
        """Claim a child operation without cancelling the reusable parent.

        Use the child for one operation. Call finish_operation() after its workers
        exit. The child shares command exclusion and transport with its parent.
        """
        active = JointControl(
            self.host,
            self.feedback_snapshot,
            parent=self,
            command_timeout=command_timeout,
            send_guard=send_guard,
        )
        active._begin()
        return active

    def wait_for_command(self, timeout=1.0):
        """Wait for the registered command worker. Return False on timeout."""
        pending = self._pending
        return pending is None or pending["done"].wait(timeout)

    def finish_operation(self):
        """Release this operation once, after caller workers have exited.

        An unfinished command keeps the control cancelled and feedback retained.
        Repeated cleanup of a returned child cannot release a sibling's lock.
        """
        with self._operation_guard:
            if not self._operation_open:
                return
            if self.command_pending:
                self.cancel()
                if self._parent is not None:
                    self._parent.cancel()
            self._preparing = False
            self._operation_open = False
            if not self.command_pending:
                self._close_jog_connection()
            self._operation_lock.release()

    def stop_after_command(self, *, on_error=None, jog=False, jog_speed_percent=1):
        """Stop a revoked hold after its bounded acknowledgement wait.

        The caller must revoke input before calling. A wait failure cancels the
        parent immediately. on_error reports that failure before stop verification.
        """
        with self._send_lock:
            pending = self._pending
        if pending is not None and not self.stopped:
            deadline = time.monotonic() + self._command_timeout
            try:
                while not pending["done"].wait(0.01):
                    if self.stopped:
                        break
                    self._fresh({5, 7, 11} if jog else {5, 7})
                    if time.monotonic() >= deadline and not pending["done"].is_set():
                        raise TimeoutError(
                            "Keyboard command acknowledgement did not finish before stopping"
                        )
            except Exception as error:
                self.cancel()
                if self._parent is not None:
                    self._parent.cancel()
                if on_error is not None:
                    on_error(error)
        return self.stop(jog=jog, jog_speed_percent=jog_speed_percent)

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
        values = tuple(float(value) for value in self._query("GetPose(0,0)").split(","))
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
            payload = self._query(f"InverseSolution({values},0,0)")
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

    def _query(self, text):
        before, _ = self._fresh({4, 5, 7})
        if self._preparing and self._preparation_baseline is None:
            self._preparation_baseline = before
        try:
            return self._command(text)
        except DashboardQueryCancelledError:
            raise
        except OSError as error:
            pending = self._pending
            if (
                not self._preparing
                or self.stopped
                or pending is not self._preparation_pending
                or (pending is not None and not (pending["done"].is_set() and pending["ok"]))
            ):
                raise
            baseline = self._preparation_baseline
            after, _ = self._fresh({baseline.mode})
            if (
                baseline.mode not in {4, 5}
                or not self._near(before.joints_deg, baseline.joints_deg)
                or not self._near(after.joints_deg, baseline.joints_deg)
            ):
                raise RuntimeError("Robot state changed during preparation") from error
            if self.stopped:
                raise
            raise PreparationRejectedError(
                f"{text} failed during preparation: {error}. "
                "This attempt sent no enable or movement command. Retry the action."
            ) from error

    def _pose_query_cancelled(self):
        # Keyboard release reads one final pose after its confirmed child stop.
        # An unconfirmed stop must never permit a delayed display poll.
        stop_result = self._stop_result
        return self.stopped and (
            stop_result is None
            or not stop_result["ok"]
            or self._stop_uncertain
            or self._disable_error is not None
            or self._child_stop_error is not None
            or (self._parent is not None and self._parent.stopped)
        )

    def _command(self, text, port=29999, *, pending=None, stopping=False):
        """Send once; a reply must have a successful code and the matching echo."""
        if port != 29999:
            return self._exchange(text, port, pending=pending, stopping=stopping)
        readonly = (
            pending is None
            and not stopping
            and text.partition("(")[0]
            in {
                "GetPose",
                "InverseSolution",
                "RobotMode",
                "GetErrorID",
                "GetAngle",
                "CalcUser",
            }
        )
        pose_query = readonly and text.startswith("GetPose(")
        query = None
        registered_stop = False
        try:
            with self._dashboard_guard:
                generation = self._dashboard["generation"]
                if stopping:
                    self._dashboard["stops"] += 1
                    registered_stop = True
                    self._dashboard["generation"] += 1
                    active = self._dashboard["query"]
                    if active is not None:
                        active["cancelled"].set()
                        connection = active["connection"]
                        if connection is not None:
                            try:
                                connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            try:
                                connection.close()
                            except OSError:
                                pass
                elif readonly and (
                    self._dashboard["stops"] or (pose_query and self._pose_query_cancelled())
                ):
                    raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
            # Resolve before taking the connection lock. DNS cannot delay a stop.
            addresses = (
                socket.getaddrinfo(self.host, port, 0, socket.SOCK_STREAM) if readonly else None
            )
            # Keep the dashboard serial through connection close, including stop.
            if not self._dashboard_lock.acquire(timeout=self._command_timeout):
                raise TimeoutError("Dashboard connection is busy")
            try:
                if readonly:
                    with self._dashboard_guard:
                        if (
                            self._dashboard["stops"]
                            or generation != self._dashboard["generation"]
                            or (pose_query and self._pose_query_cancelled())
                        ):
                            raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
                        query = {
                            "cancelled": threading.Event(),
                            "connection": None,
                            "addresses": addresses,
                        }
                        self._dashboard["query"] = query
                return self._exchange(
                    text,
                    port,
                    pending=pending,
                    stopping=stopping,
                    query=query,
                )
            except OSError as error:
                if query is not None and query["cancelled"].is_set():
                    raise DashboardQueryCancelledError(
                        "Dashboard query cancelled for stop"
                    ) from error
                raise
            finally:
                with self._dashboard_guard:
                    if query is not None:
                        self._dashboard["query"] = None
                self._dashboard_lock.release()
        finally:
            if registered_stop:
                with self._dashboard_guard:
                    self._dashboard["stops"] -= 1

    def _connect(self, port, deadline, query):
        if query is None:
            return socket.create_connection((self.host, port), timeout=self._command_timeout)
        failure = OSError("Controller address has no stream sockets")
        for family, kind, protocol, _, address in query["addresses"]:
            if query["cancelled"].is_set():
                raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
            connection = socket.socket(family, kind, protocol)
            with self._dashboard_guard:
                if query["cancelled"].is_set():
                    connection.close()
                    raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
                query["connection"] = connection
            try:
                connection.setblocking(False)
                code = connection.connect_ex(address)
                if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                    raise OSError(code, "Controller connection failed")
                while code:
                    if query["cancelled"].is_set():
                        raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Controller connection timed out")
                    _, writable, failed = select.select(
                        [],
                        [connection],
                        [connection],
                        min(0.02, remaining),
                    )
                    if writable or failed:
                        code = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if code:
                            raise OSError(code, "Controller connection failed")
                return connection
            except BaseException as error:
                connection.close()
                if query["cancelled"].is_set():
                    raise DashboardQueryCancelledError(
                        "Dashboard query cancelled for stop"
                    ) from error
                if not isinstance(error, OSError):
                    raise
                failure = error
        raise failure

    def _close_jog_connection(self):
        with self._jog_connection_lock:
            connection, self._jog_connection = self._jog_connection, None
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    @contextmanager
    def _command_connection(self, text, port, deadline, query):
        if self._parent is None or port != 30003 or not text.startswith("MoveJog("):
            with self._connect(port, deadline, query) as connection:
                yield connection
            return
        if not self._jog_connection_lock.acquire(timeout=self._command_timeout):
            raise TimeoutError("MoveJog connection is busy")
        try:
            if self._jog_connection_failed:
                raise RuntimeError("MoveJog connection failed; a new command is prohibited")
            if self._jog_connection is None:
                self._jog_connection = self._connect(port, deadline, query)
            yield self._jog_connection
        except Exception:
            self._jog_connection_failed = True
            connection, self._jog_connection = self._jog_connection, None
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            raise
        finally:
            self._jog_connection_lock.release()

    def _exchange(self, text, port, *, pending=None, stopping=False, query=None):
        deadline = time.monotonic() + self._command_timeout
        if query is not None and text.startswith("GetPose(") and self._pose_query_cancelled():
            raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
        with self._command_connection(text, port, deadline, query) as connection:
            with self._send_lock if query is None else nullcontext():
                if query is not None and (
                    query["cancelled"].is_set()
                    or (text.startswith("GetPose(") and self._pose_query_cancelled())
                ):
                    raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
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
            if query is not None and query["cancelled"].is_set():
                raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
            if text.startswith("MoveJog(") and packet.count(b";") != 1:
                raise RuntimeError("MoveJog received multiple controller responses")
            value = parse_controller_response(packet, text)
        if query is not None and query["cancelled"].is_set():
            raise DashboardQueryCancelledError("Dashboard query cancelled for stop")
        return value

    def _dashboard_state(self):
        mode = int(self._query("RobotMode()"))
        if self._preparing:
            sample = self._fresh({4, 5})[0]
            if mode != sample.mode:
                raise RuntimeError(
                    "Dashboard and passive feedback disagree: "
                    f"dashboard_mode={mode}, passive_mode={sample.mode}"
                )
        errors = json.loads(self._query("GetErrorID()"))
        if not isinstance(errors, list) or not errors or any(item != [] for item in errors):
            raise RuntimeError(f"Controller alarms require inspection: {errors}")
        joints = tuple(float(value) for value in self._query("GetAngle()").split(","))
        if len(joints) not in (4, 6) or not all(map(math.isfinite, joints)):
            raise ValueError("Invalid actual joint angles")
        return mode, joints[:4]

    def _wait_stable(
        self,
        target=None,
        *,
        modes,
        final_modes,
        timeout=5.0,
        stopping=False,
        jog_stop_ack=None,
        jog_speed_percent=1,
    ):
        deadline = time.monotonic() + timeout
        jog_stop = jog_stop_ack is not None
        stable_seconds = 0.05 if jog_stop else 0.4
        poll_seconds = 0.005 if jog_stop else 0.01
        previous_received = None
        anchor = stable_since = None
        count = 0
        samples = 0
        minimum = maximum = ()
        longest_stable = 0.0
        while time.monotonic() < deadline:
            if self.stopped and not stopping:
                raise RuntimeError("Control has been stopped; reopen the panel")
            sample, received = self._fresh(modes)
            if jog_stop:
                if getattr(sample, "speed_scaling_raw", None) != jog_speed_percent:
                    raise RuntimeError(
                        f"MoveJog stop requires speed scaling feedback of {jog_speed_percent}"
                    )
                if received <= jog_stop_ack:
                    time.sleep(poll_seconds)
                    continue
            if received != previous_received:
                if (
                    jog_stop
                    and previous_received is not None
                    and not (0 < received - previous_received <= stable_seconds / 3)
                ):
                    anchor = stable_since = None
                    count = 0
                previous_received = received
                samples += 1
                if samples == 1:
                    minimum = maximum = sample.joints_deg
                else:
                    minimum = tuple(map(min, zip(minimum, sample.joints_deg, strict=True)))
                    maximum = tuple(map(max, zip(maximum, sample.joints_deg, strict=True)))
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
                    longest_stable = max(longest_stable, received - stable_since)
                    if count >= 3 and received - stable_since >= stable_seconds:
                        return sample
            time.sleep(poll_seconds)
        span = tuple(round(high - low, 6) for low, high in zip(minimum, maximum, strict=True))
        raise _StationaryTimeoutError(
            "Robot did not reach a fresh, verified stationary state "
            f"(samples={samples}, joint_span_deg={span}, "
            f"longest_stable_s={longest_stable:.3f}, required_stable_s={stable_seconds}, "
            "tolerance_deg=0.025)"
        )

    def _preflight(self, modes):
        self._fresh(modes)
        mode, joints = self._dashboard_state()
        sample, _ = self._fresh(modes)
        if mode != sample.mode or not self._near(joints, sample.joints_deg):
            delta = tuple(
                actual - reference
                for actual, reference in zip(
                    sample.joints_deg,
                    joints,
                    strict=True,
                )
            )
            raise RuntimeError(
                "Dashboard and passive feedback disagree: "
                f"dashboard_mode={mode}, passive_mode={sample.mode}, "
                f"dashboard={joints!r}, passive={sample.joints_deg!r}, "
                f"passive_minus_dashboard={delta!r}, tolerance_deg=0.1"
            )
        stationary = stationary_error = None
        try:
            stationary = self._wait_stable(
                None if mode == 4 else joints,
                modes={mode},
                final_modes={mode},
            )
        except _StationaryTimeoutError as error:
            if mode != 4:
                raise
            stationary_error = error
        if mode == 4:
            current_mode, current_joints = self._dashboard_state()
            sample = self._fresh({4})[0]
            if current_mode != 4 or not self._near(current_joints, sample.joints_deg):
                raise RuntimeError("Disabled stationary state was not confirmed before enable")
            if stationary_error is not None or (
                not self._near(current_joints, stationary.joints_deg)
                or not self._near(sample.joints_deg, stationary.joints_deg)
            ):
                error = stationary_error or RuntimeError(
                    "Disabled stationary state was not confirmed before enable"
                )
                pending = self._pending
                if (
                    not self._preparing
                    or self.stopped
                    or self._stop_uncertain
                    or (self._parent is not None and self._parent._stop_uncertain)
                    or pending is not self._preparation_pending
                    or (pending is not None and not (pending["done"].is_set() and pending["ok"]))
                ):
                    raise error
                raise PreparationRejectedError(
                    "Robot is not verified stationary. "
                    "Wait until the robot is still, then click Enable robot again."
                ) from error
        return mode, stationary.joints_deg

    def _guarded_command(self, text, *, modes, send_modes, expected, port=29999):
        self._fresh(send_modes)
        self._preparing = False
        pending = {
            "command": text,
            "port": port,
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
                if not self._operation_open:
                    self._close_jog_connection()

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
        with self._operation_guard:
            if not self._operation_lock.acquire(blocking=False):
                raise RuntimeError("Another robot operation is in progress")
            if self.stopped:
                self._operation_lock.release()
                raise RuntimeError("Control has been stopped; reopen the panel")
            self._operation_open = True
            self._preparing = True
            self._preparation_pending = self._pending
            self._preparation_baseline = None

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
        stage = "before EnableRobot()"
        try:
            mode, joints = self._preflight({4, 5})
            if mode == 4:
                stage = "EnableRobot() command attempt"
                self._guarded_command(
                    "EnableRobot()", modes={4, 5}, send_modes={4}, expected=joints
                )
            stage = "after EnableRobot()" if mode == 4 else "already enabled idle check"
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
        except PreparationRejectedError:
            raise
        except TimeoutError as error:
            diagnostic = TimeoutError(f"{error}. Failure stage={stage}.")
            self._failed(diagnostic)
            raise diagnostic from error
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self.finish_operation()

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
        except PreparationRejectedError:
            raise
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self.finish_operation()

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
        sample = self.read_state()
        if sample.mode != 5:
            raise RuntimeError("Robot must be enabled and idle before movement")
        validate_measured_joints(sample.joints_deg)
        self._begin()
        try:
            _, start = self._preflight({5})
            validate_measured_joints(start)
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
        except (TargetRejectedError, PreparationRejectedError):
            raise
        except Exception as error:
            self._failed(error)
            raise
        finally:
            self.finish_operation()

    def disable(self) -> Feedback:
        """Cancel this instance and verify the disabled state, without proving rest.

        Create a new instance only after this succeeds and the previous operation exits.
        Use verify_stationary() separately before disconnecting feedback.
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
                # A new disable invalidates any earlier verification of rest.
                self._stop_result = None
                self._command("DisableRobot()", stopping=True)
                acknowledged_at = time.monotonic()
                if self._stop_uncertain:
                    raise RuntimeError(
                        "A command acknowledgement was unresolved or failed during disable. "
                        "Command ordering remains unknown."
                    )
                deadline = acknowledged_at + 5.0
                while time.monotonic() < deadline:
                    sample, received = self._fresh({4, 5, 7, 11})
                    if sample.mode == 4 and received > acknowledged_at:
                        break
                    time.sleep(0.01)
                else:
                    raise TimeoutError("Fresh disabled robot feedback was not confirmed")
                mode, _ = self._dashboard_state()
                sample = self._fresh({4})[0]
                if mode != 4:
                    raise RuntimeError("Disabled robot mode was not confirmed")
                self._disable_result = sample
                return sample
            except Exception as error:
                try:
                    self._failed(error, disabling=True)
                except Exception as failure:
                    error = failure
                self._disable_error = error
                raise error

    def verify_stationary(self) -> Feedback:
        """Verify rest from feedback only. Call on a worker thread.

        This accepts a stopped instance and does not clear command uncertainty.
        """
        return self._wait_stable(modes={4, 5}, final_modes={4, 5}, stopping=True)

    def stop(self, *, jog=False, jog_speed_percent=1):
        """Permanently cancel this instance, request a controlled stop, verify rest.

        An unresolved mutation ACK makes ordering across connections unknown;
        even a later successful ACK cannot establish a confirmed stop.
        """
        normal_jog = jog and not self.stopped and not self._stop_uncertain
        if self._parent is not None and self._parent._stop_uncertain:
            self._stop_uncertain = True
            normal_jog = False
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
            normal_jog = (
                normal_jog
                and not self._stop_uncertain
                and pending is not None
                and pending["sent"]
                and pending["done"].is_set()
                and pending["ok"]
                and pending.get("command", "").startswith("MoveJog(")
                and pending.get("port") == 30003
            )
        with self._stop_lock:
            if self._stop_result is not None:
                return self._stop_result
            if self._parent is not None and self._parent.stopped:
                normal_jog = False
            try:
                if normal_jog:
                    try:
                        self._command("MoveJog()", port=30003, stopping=True)
                    except Exception:
                        self._stop_uncertain = True
                        if self._parent is not None:
                            self._parent._stop_uncertain = True
                        raise
                    jog_stop_ack = time.monotonic()
                else:
                    self._command("ResetRobot()", stopping=True)
                    jog_stop_ack = None
                if self._stop_uncertain:
                    raise RuntimeError(
                        "Mutation acknowledgement was unresolved or failed when stopping; "
                        "command ordering remains unknown"
                    )
                self._wait_stable(
                    modes={4, 5, 7, 11},
                    final_modes={5} if normal_jog else {4, 5},
                    timeout=3,
                    stopping=True,
                    jog_stop_ack=jog_stop_ack,
                    jog_speed_percent=jog_speed_percent,
                )
                self._stop_result = {
                    "ok": True,
                    "stationary_verified": True,
                    "physical_stop_requested": True,
                }
            except Exception as error:
                if normal_jog:
                    try:
                        self._command("ResetRobot()", stopping=True)
                        self._wait_stable(
                            modes={4, 5, 7, 11},
                            final_modes={4, 5},
                            timeout=3,
                            stopping=True,
                        )
                    except Exception as rescue_error:
                        error = RuntimeError(f"{error}; ResetRobot stop failed: {rescue_error}")
                self._stop_result = {
                    "ok": False,
                    "stationary_verified": False,
                    "physical_stop_requested": True,
                    "error": str(error),
                }
            if self._parent is not None and not self._stop_result["ok"]:
                # A child stop must not populate the reusable parent's stop cache.
                self._parent._child_stop_error = self._stop_result["error"]
            return self._stop_result
