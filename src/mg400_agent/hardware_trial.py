"""One authorized physical trial: J4 +2 degrees, then return to its captured start.

Importing or constructing this module never opens a connection. The context manager
captures state read-only; only the two action tools may enable or move the robot.
"""

import json
import math
import socket
import threading
import time

from .controller_protocol import parse_controller_response
from .feedback import FeedbackConnection
from .joint_limits import validate_joint_segment


class JointTrial:
    """Bounded, sequential executor for the specifically authorized J4 trial."""

    def __init__(self, host: str, source_host: str | None = None):
        self.host = host
        self.source_host = source_host
        self.start = None
        self.initial_mode = None
        self.phase = "unopened"
        self.fault = None
        self.verified_joints_deg = {}
        self.last_verified_checkpoint = None
        self._latest = None
        self._reader_error = None
        self._feedback = None
        self._reader = None
        self._closing = False
        self._send_lock = threading.Lock()
        self._cancelled = False
        self._owned = False
        self._uncertain = False
        self._unsafe_fault = False
        self._stop_attempted = False
        self._stop_result = None
        self._pending_mutation = None

    def __enter__(self):
        if self.phase != "unopened":
            raise RuntimeError("A trial context can only be opened once")
        self._feedback = FeedbackConnection(self.host, self.source_host, timeout_s=0.2)
        try:
            self._feedback.__enter__()
            self._reader = threading.Thread(target=self._read_feedback, daemon=True)
            self._reader.start()
            deadline = time.monotonic() + 2
            while self._latest is None and self._reader_error is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("No initial robot feedback")
                time.sleep(0.01)
            self._wait_stable(final_modes={4, 5}, allowed_modes={4, 5})
            mode, joints = self._dashboard_state()
            sample = self._fresh({4, 5})[2]
            if mode != sample.mode or not self._near(joints, sample.joints_deg, 0.1):
                raise RuntimeError("Dashboard and passive feedback disagree")
            target = (*joints[:3], joints[3] + 2.0)
            validate_joint_segment(joints, target)
            self.start = joints
            self.initial_mode = mode
            self._wait_stable(joints, final_modes={mode}, allowed_modes={mode})
            self.phase = "ready"
            return self
        except BaseException:
            self._close_feedback()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._owned and self.phase != "complete":
                result = self.stop()
                if not result["ok"]:
                    raise RuntimeError(
                        f"Physical stop not confirmed; use the physical emergency stop: {result}"
                    ) from exc_value
        finally:
            self._close_feedback()

    def _close_feedback(self):
        if self._pending_mutation is not None:
            self._pending_mutation[0].join(timeout=4.25)
        self._closing = True
        if self._feedback is not None:
            self._feedback.__exit__(None, None, None)
        if self._reader is not None:
            self._reader.join(timeout=0.5)

    def _read_feedback(self):
        sequence = 0
        try:
            while not self._closing:
                sample = self._feedback.read_latest()
                sequence += 1
                self._latest = (sequence, time.monotonic(), sample)
        except Exception as error:
            if not self._closing:
                self._reader_error = error

    def _command(self, text, port=29999, *, mutation=False, stopping=False):
        """One send, bounded framing and matching ACK; never retry any command."""
        with socket.create_connection(
            (self.host, port),
            timeout=2.0,
            source_address=(self.source_host, 0) if self.source_host else None,
        ) as connection:
            deadline = time.monotonic() + 2.0
            with self._send_lock:
                if mutation and self._cancelled and not stopping:
                    raise RuntimeError("Trial cancelled before command was sent")
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

    @staticmethod
    def _near(first, second, tolerance):
        return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))

    def _fresh(self, allowed_modes=None, *, corridor=True):
        if self._reader_error is not None:
            raise RuntimeError("Passive feedback reader failed") from self._reader_error
        item = self._latest
        if item is None or time.monotonic() - item[1] > 0.25:
            raise RuntimeError("Passive feedback is absent or stale")
        sample = item[2]
        if sample.error_status or (allowed_modes is not None and sample.mode not in allowed_modes):
            raise RuntimeError(
                f"Unexpected physical robot state: mode={sample.mode}, error={sample.error_status}"
            )
        if corridor and self.start is not None:
            joints = sample.joints_deg
            if not self._near(joints[:3], self.start[:3], 0.2) or not (
                self.start[3] - 0.2 <= joints[3] <= self.start[3] + 2.2
            ):
                raise RuntimeError("Actual joints left the authorized J4 trial corridor")
        return item

    def _wait_stable(
        self,
        target=None,
        *,
        final_modes,
        allowed_modes,
        timeout=5.0,
        corridor=True,
    ):
        deadline = time.monotonic() + timeout
        previous_sequence = None
        anchor = None
        stable_since = None
        count = 0
        while time.monotonic() < deadline:
            sequence, received, sample = self._fresh(allowed_modes, corridor=corridor)
            if sequence != previous_sequence:
                previous_sequence = sequence
                ready = sample.mode in final_modes and (
                    target is None or self._near(sample.joints_deg, target, 0.1)
                )
                if not ready:
                    anchor, stable_since, count = None, None, 0
                elif anchor is None or not self._near(sample.joints_deg, anchor, 0.025):
                    anchor, stable_since, count = sample.joints_deg, received, 1
                else:
                    count += 1
                    if count >= 3 and received - stable_since >= 0.4:
                        return sample
            time.sleep(0.01)
        raise TimeoutError("Robot did not reach a fresh, verified stationary state")

    def _guarded_command(self, text, *, port=29999, allowed_modes):
        """Keep supervising feedback while a mutation's ACK is pending."""
        completed = threading.Event()
        outcome = []

        def send():
            try:
                outcome.append((True, self._command(text, port, mutation=True)))
            except BaseException as error:
                outcome.append((False, error))
            finally:
                completed.set()

        self._fresh(allowed_modes)
        self._owned = self._uncertain = True
        worker = threading.Thread(target=send, daemon=True)
        self._pending_mutation = (worker, completed, outcome)
        worker.start()
        while not completed.wait(0.01):
            self._fresh(allowed_modes)
        self._fresh(allowed_modes)
        succeeded, value = outcome[0]
        if not succeeded:
            raise value
        return value

    def _preflight(self, expected):
        self._fresh({4, 5})
        mode, actual = self._dashboard_state()
        sample = self._fresh({4, 5})[2]
        if mode != sample.mode or not self._near(actual, sample.joints_deg, 0.1):
            raise RuntimeError("Dashboard and feedback disagree before movement")
        if not self._near(actual, expected, 0.1):
            raise RuntimeError("Robot moved from the verified trial pose")
        expected_mode = self.initial_mode if self.phase == "ready" and not self._owned else 5
        if mode != expected_mode:
            raise RuntimeError("Unexpected enable state before movement")
        self._wait_stable(expected, final_modes={mode}, allowed_modes={mode})
        return mode

    def _move(self, target, expected):
        mode = self._preflight(expected)
        validate_joint_segment(expected, target)
        if mode == 4:
            # Four-axis protocol (2024-04-19, p. 8): no arguments leave the
            # configured load weight and eccentric parameters unchanged.
            self._guarded_command("EnableRobot()", allowed_modes={4, 5})
            self._wait_stable(expected, final_modes={5}, allowed_modes={4, 5})
            self._preflight(expected)
        values = ",".join(f"{value:.6f}" for value in target)
        self._guarded_command(
            f"JointMovJ({values},SpeedJ=5,AccJ=5)",
            port=30003,
            allowed_modes={5, 7},
        )
        self._wait_stable(target, final_modes={5}, allowed_modes={5, 7}, timeout=20.0)
        mode, actual = self._dashboard_state()
        self._fresh({5})
        if mode != 5 or not self._near(actual, target, 0.1):
            raise RuntimeError("Final dashboard position does not verify arrival")
        self._uncertain = False
        return actual

    def _diagnostics(self):
        return {
            "phase": self.phase,
            "verified_joints_deg": dict(self.verified_joints_deg),
            "last_verified_checkpoint": None
            if self.last_verified_checkpoint is None
            else dict(self.last_verified_checkpoint),
        }

    def state(self):
        sample = self._fresh({4, 5, 7})[2]
        return {
            "mode": "physical_authorized_j4_trial",
            "robot_mode": sample.mode,
            "joints_deg": sample.joints_deg,
            "start_joints_deg": self.start,
            "outbound_joints_deg": None
            if self.start is None
            else (*self.start[:3], self.start[3] + 2.0),
            **self._diagnostics(),
            "stopped": self._cancelled,
            "fault": self.fault,
            "gripper_control_enabled": False,
        }

    def stop(self):
        with self._send_lock:
            self._cancelled = True
        if self._stop_result is not None:
            return self._stop_result
        if not self._owned:
            self._stop_result = {
                "ok": True,
                "session_stopped": True,
                "physical_stop_requested": False,
            }
            return self._stop_result
        try:
            # A stop sent over another TCP connection cannot establish ordering
            # with a mutation whose acceptance is still unknown. Remember the
            # state before ResetRobot, even if an ACK arrives while stopping.
            acceptance_unknown = self._pending_mutation is not None and (
                not self._pending_mutation[1].is_set() or not self._pending_mutation[2][0][0]
            )
            if not self._uncertain and not self._unsafe_fault:
                try:
                    self._fresh({4, 5})
                except Exception:
                    self._unsafe_fault = True
            if self._uncertain or self._unsafe_fault:
                if self._stop_attempted:
                    raise RuntimeError("Previous physical stop attempt was not confirmed")
                self._stop_attempted = True
                self._command("ResetRobot()", mutation=True, stopping=True)
            if self._pending_mutation is not None:
                self._pending_mutation[0].join(timeout=4.25)
            if acceptance_unknown:
                raise RuntimeError(
                    "Mutation acknowledgement was unresolved or failed when stopping; "
                    "command ordering and physical stop remain unconfirmed"
                )
            self._wait_stable(
                final_modes={4, 5}, allowed_modes={4, 5, 7}, timeout=3, corridor=False
            )
            self._uncertain = False
            self._stop_result = {
                "ok": True,
                "session_stopped": True,
                "stationary_verified": True,
                "physical_stop_requested": self._stop_attempted,
            }
        except Exception as error:
            self._stop_result = {
                "ok": False,
                "session_stopped": True,
                "stationary_verified": False,
                "error": str(error),
                "physical_stop_requested": self._stop_attempted,
                "operator_action": "Physical stop is unconfirmed; use the physical emergency stop.",
            }
        return self._stop_result

    def execute(self, name: str, arguments: dict):
        names = {"get_robot_state", "rotate_j4_out", "return_to_start", "stop"}
        if (
            not isinstance(name, str)
            or name not in names
            or not isinstance(arguments, dict)
            or arguments
        ):
            return {
                "ok": False,
                "error": "Only the four preset tools with empty arguments are allowed",
                **self._diagnostics(),
            }
        if name == "stop":
            return self.stop()
        if name != "get_robot_state" and (
            self._cancelled or self.phase != ("ready" if name == "rotate_j4_out" else "out")
        ):
            return {
                "ok": False,
                "error": "Trial phase forbids this action; no motion was sent",
                **self._diagnostics(),
            }
        try:
            if name == "get_robot_state":
                return {"ok": True, "state": self.state()}
            outbound = (*self.start[:3], self.start[3] + 2.0)
            target, expected = (
                (outbound, self.start) if name == "rotate_j4_out" else (self.start, outbound)
            )
            actual = self._move(target, expected)
            self.verified_joints_deg[name] = actual
            self.last_verified_checkpoint = {
                "action": name,
                "joints_deg": actual,
                "robot_mode": 5,
            }
            if name == "rotate_j4_out":
                self.phase = "out"
            else:
                if self.initial_mode == 4:
                    self.phase = "restoring_disabled"
                    self._guarded_command("DisableRobot()", allowed_modes={4, 5})
                    self._wait_stable(self.start, final_modes={4}, allowed_modes={4, 5})
                    mode, actual = self._dashboard_state()
                    if mode != 4 or not self._near(actual, self.start, 0.1):
                        raise RuntimeError("Original disabled state was not restored")
                    self._uncertain = False
                self.phase = "complete"
                self._owned = False
            return {"ok": True, "state": self.state()}
        except Exception as error:
            self.fault = str(error)
            self._unsafe_fault = self._owned
            return {
                "ok": False,
                "error": self.fault,
                "stop": self.stop(),
                **self._diagnostics(),
            }

    def tool_schemas(self):
        descriptions = {
            "get_robot_state": (
                "Read fresh physical MG400 joints in degrees and the authorized trial phase."
            ),
            "rotate_j4_out": (
                "Once only: physically move J4 exactly +2 degrees from the captured start, "
                "preserving J1-J3. Only valid in ready phase. May enable the robot using its "
                "configured payload. Waits for verified arrival."
            ),
            "return_to_start": (
                "Once only after rotate_j4_out: physically return J4 to the captured starting "
                "angle, preserving J1-J3. Waits for verified arrival and restores disabled "
                "state if the robot began disabled. Never use a zero home."
            ),
            "stop": (
                "Cancel this trial and prohibit further motion. If its physical action is "
                "uncertain, request a controlled stop and verify fresh stationary feedback. "
                "This is not a hardware emergency stop."
            ),
        }
        return [
            {
                "type": "function",
                "name": name,
                "description": description,
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            }
            for name, description in descriptions.items()
        ]
