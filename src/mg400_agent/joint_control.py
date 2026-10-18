"""Small joint moves driven by explicit UI actions and an existing feedback reader.

Construction is inert. Methods block and belong on a worker thread, not Kit's UI
thread. A stopped instance never sends another enable or movement command.
"""

import json
import math
import re
import socket
import threading
import time

from .feedback import Feedback
from .joint_limits import validate_joint_segment, validate_joints


class JointControl:
    def __init__(self, host, feedback_snapshot):
        self.host = host
        self.feedback_snapshot = feedback_snapshot
        self._operation_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopped = threading.Event()
        self._pending = None
        self._stop_result = None
        self._stop_uncertain = False

    @property
    def busy(self):
        return self._operation_lock.locked()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def cancel(self):
        """Immediately latch cancellation from the UI, without waiting or networking.

        Call stop() on a worker next to request and verify the physical stop.
        """
        self._stopped.set()
        pending = self._pending
        if pending is not None and pending["sent"] and not (
            pending["done"].is_set() and pending["ok"]
        ):
            self._stop_uncertain = True

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

    def _command(self, text, port=29999, *, pending=None, stopping=False):
        """Send once; a reply must have a successful code and the matching echo."""
        deadline = time.monotonic() + 2.0
        with socket.create_connection((self.host, port), timeout=2.0) as connection:
            with self._send_lock:
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
        response = packet.decode("ascii").strip()
        match = re.fullmatch(r"(-?\d+),\{(.*)\},(.*);", response, re.DOTALL)
        if not match or re.sub(r"\s+", "", match[3]) != re.sub(r"\s+", "", text):
            raise RuntimeError(f"Unexpected controller response: {response}")
        if int(match[1]) != 0:
            raise RuntimeError(response)
        return match[2].strip()

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
            if self.stopped:
                raise RuntimeError("Control has been stopped; reopen the panel")
            self._pending = pending

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

    def _failed(self, error):
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
        """Move absolute J1-J4 in degrees, at most five degrees per axis per action."""
        target = validate_joints(target)
        sample = self.read_state()
        if sample.mode != 5:
            raise RuntimeError("Robot must be enabled and idle before movement")
        validate_joint_segment(sample.joints_deg, target)
        if any(abs(a - b) > 5.0 for a, b in zip(sample.joints_deg, target, strict=True)):
            raise ValueError("Each joint change is limited to 5 degrees per action")
        self._begin()
        try:
            _, start = self._preflight({5})
            validate_joint_segment(start, target)
            if any(abs(a - b) > 5.0 for a, b in zip(start, target, strict=True)):
                raise ValueError("Each joint change is limited to 5 degrees per action")
            values = ",".join(f"{value:.6f}" for value in target)
            self._guarded_command(
                f"JointMovJ({values},SpeedJ=5,AccJ=5)",
                modes={5, 7},
                send_modes={5},
                expected=start,
                port=30003,
            )
            sample = self._wait_stable(target, modes={5, 7}, final_modes={5}, timeout=20.0)
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

    def stop(self):
        """Permanently cancel this instance, request a controlled stop, verify rest.

        An unresolved mutation ACK makes ordering across connections unknown;
        even a later successful ACK cannot establish a confirmed stop.
        """
        self.cancel()
        with self._send_lock:
            pending = self._pending
            uncertain = pending is not None and pending["sent"] and not (
                pending["done"].is_set() and pending["ok"]
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
