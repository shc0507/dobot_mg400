"""Polled stage sequences. The controller serial lock owns every field here."""

import math

from .config import DEFAULT_CONFIG, demo_geometry, load_geometry
from .protocol import DEFAULT_SPEED, MAX_STEPS, MOTOR_IDS, ControllerError, LinkError


class StageAutomation:
    def _init_automation(self, config):
        self._geometry = None
        self._configuration_error = None
        try:
            self._geometry = (
                demo_geometry()
                if self.demo and config is None
                else load_geometry(config or DEFAULT_CONFIG)
            )
        except (OSError, ValueError, UnicodeError) as exc:
            self._configuration_error = f"Stage configuration unavailable: {exc}"
        self._references = {}
        self._automation = None
        self._sequence_stops = []
        self._automation_error = None
        self._automation_client = None

    def _axis(self, motor):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        if self._geometry is None:
            raise ControllerError(self._configuration_error)
        return self._geometry.motors[motor]

    def _ready(self, motor):
        state = self._motors[motor]
        return (
            self._transport is not None
            and self._feedback_fresh()
            and self._geometry is not None
            and state.homed is True
            and state.soft is True
            and self._microstep is not None
            and self._references.get(motor) == self._microstep
        )

    def _feedback_fresh(self):
        return (
            self._transport is not None
            and self._last_feedback_at is not None
            and 0 <= self.clock() - self._last_feedback_at <= 2
        )

    def _require_reference(self, motor):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        if not self._ready(motor):
            raise ControllerError("position unknown, home first")

    def _require_no_sequence(self):
        if self._contract_fault or self._contract_stopping:
            raise ControllerError("The service is locked. Only stop is allowed.")
        if self._automation is not None or self._sequence_stops:
            raise ControllerError(
                "A stage sequence is in progress. Stop it before another command."
            )

    def _idle_axis(self, motor):
        if self._motors[motor].mode != "idle":
            raise ControllerError(f"{motor.title()} is still moving.")
        if self.clock() < self._cooldowns[motor]:
            raise ControllerError(f"Wait for {motor}'s 1 second cooldown to finish.")

    def _end_sequence(self, result, message):
        operation, self._automation = self._automation, None
        self._finish_sequence(operation, result, message)

    def _finish_sequence(self, operation, result, message):
        if operation is None:
            return
        if result == "failed":
            self._automation_error = message
        entry = operation.get("entry")
        if entry:
            self._finish_command(entry, result, message)
        self._event(
            f"Stage {operation['action']} {result}: {message}",
            "error" if result == "failed" else "info",
        )

    def _cancel_sequence(self):
        # Remove continuation authority before writing stop, but retain the audit
        # entry until fresh feedback confirms stopping or the link is lost.
        operation, self._automation = self._automation, None
        if operation is not None:
            self._sequence_stops.append(operation)

    def _complete_sequence_stops(self, failure=None):
        for operation in list(self._sequence_stops):
            if failure or all(self._motors[motor].mode == "idle" for motor in operation["axes"]):
                self._sequence_stops.remove(operation)
                self._finish_sequence(
                    operation,
                    "failed" if failure else "stopped",
                    failure or "Stop confirmed. Remaining sequence legs cancelled.",
                )

    def _observe_references(self, previous_mode):
        if previous_mode != self._microstep:
            self._references.clear()
        for motor, state in self._motors.items():
            if state.homed is not True or state.soft is not True:
                self._references.pop(motor, None)
        errors = getattr(self._transport, "home_errors", ())
        while errors:
            motor, reason = errors.popleft()
            self._references.pop(motor, None)
            message = f"{motor}: home failed: {reason}"
            self._event(message, "error")
            if self._automation and motor in self._automation["axes"]:
                self._automation["failure"] = message

    def _new_sequence(self, action, axes, **fields):
        self._automation_error = None
        self._automation = dict(
            action=action,
            axes=tuple(axes),
            owner=self._automation_client,
            microstep=self._microstep,
            epoch=self.lease.epoch,
            motor=None,
            pose=None,
            phase="waiting",
            **fields,
        )
        return self._automation

    def home(self, motor):
        with self._lock:
            axis = self._axis(motor)
            self._require_connection()
            self._require_no_sequence()
            try:
                self._read_status()
                self._idle_axis(motor)
                if self._motors[motor].homed is None:
                    raise ControllerError("Homing is unavailable with this firmware.")
                bounds = axis.bounds(self._microstep)
                self._transport.command(f"motor {motor}")
                # Finish draining any prior home failure before starting this home.
                self._observe_references(self._microstep)
                operation = self._new_sequence("home", (motor,), bounds=bounds)
                operation.update(motor=motor, phase="homing", deadline=self.clock() + 125)
                self._references.pop(motor, None)
                self._transport.command("home")
                self._motors[motor].homed = self._motors[motor].soft = False
                self._motors[motor].mode = "moving"
                self._active.add(motor)
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            except ControllerError:
                self._end_sequence("failed", "Homing was rejected.")
                raise
            return self.snapshot()

    def awake(self, motor, seconds):
        if (
            motor not in MOTOR_IDS
            or type(seconds) not in (int, float)
            or not 0 <= seconds <= 60
            or not math.isfinite(seconds)
        ):
            raise ValueError("Choose a motor and an awake interval from 0 to 60 seconds.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if self._motors[motor].awake is None:
                    raise ControllerError("Keep-awake is unavailable with this firmware.")
                self._transport.command(f"motor {motor}")
                self._transport.command(f"awake {int(seconds * 1000)}")
                self._read_status()
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def goto(self, motor, name):
        axis = self._axis(motor)
        if not isinstance(name, str) or name not in axis.positions:
            raise ValueError("Unknown stage named position.")
        return self._start_targets("goto", [(motor, axis.positions[name])])

    def pose(self, name):
        if self._geometry is None:
            raise ControllerError(self._configuration_error)
        if not isinstance(name, str) or name not in self._geometry.poses:
            raise ValueError("Unknown stage pose.")
        return self._start_targets("pose", self._geometry.poses[name], name)

    def _start_targets(self, action, targets, pose=None):
        with self._lock:
            self._require_connection()
            self._require_no_sequence()
            try:
                self._read_status()
                legs = []
                for motor, mm in targets:
                    self._require_reference(motor)
                    self._idle_axis(motor)
                    target = self._axis(motor).target(mm, self._microstep)
                    if abs(target - self._motors[motor].position) > MAX_STEPS:
                        raise ValueError("Stage move exceeds the maximum pulse count.")
                    legs.append((motor, target))
                operation = self._new_sequence(action, [motor for motor, _ in targets], legs=legs)
                operation["pose"] = pose
                self._next_leg()
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            except ControllerError as exc:
                self._end_sequence("failed", str(exc))
                raise
            return self.snapshot()

    def _next_leg(self):
        operation = self._automation
        if not operation["legs"]:
            self._end_sequence("ok", "All requested positions confirmed.")
            return
        motor, target = operation["legs"][0]
        self._require_reference(motor)
        if self.clock() < self._cooldowns[motor]:
            operation["phase"] = "cooldown"
            return
        self._idle_axis(motor)
        delta = target - self._motors[motor].position
        if not delta:
            operation["legs"].pop(0)
            self._next_leg()
            return
        if abs(delta) > MAX_STEPS:
            raise ControllerError("Stage move exceeds the maximum pulse count.")
        operation.update(
            motor=motor,
            target=target,
            phase="moving",
            deadline=self.clock() + 2 * abs(delta) / DEFAULT_SPEED + 20,
        )
        for command in (
            f"motor {motor}",
            f"dir {'cw' if delta > 0 else 'ccw'}",
            f"speed {DEFAULT_SPEED}",
            f"steps {abs(delta)}",
        ):
            self._transport.command(command)
        self._active.add(motor)
        self._motors[motor].mode = "moving"

    def _advance_sequence(self, holder):
        operation = self._automation
        if operation is None:
            return
        try:
            if holder != operation["owner"] or self.lease.epoch != operation["epoch"]:
                raise ControllerError("Stage sequence lost its original control lease.")
            if operation["microstep"] != self._microstep:
                raise ControllerError("Stage microstep changed during the sequence.")
            if operation.get("failure"):
                raise ControllerError(operation["failure"])
            if self._contract_fault:
                raise ControllerError(self._contract_fault)
            motor = operation["motor"]
            if operation["phase"] in ("waiting", "cooldown"):
                self._next_leg()
                return
            if self.clock() >= operation["deadline"]:
                raise ControllerError("Stage sequence timed out. Motion was not retried.")
            state = self._motors[motor]
            if state.mode != "idle":
                return
            if operation["action"] == "home":
                if state.homed is not True or state.position != 0:
                    raise ControllerError("Home ended without a confirmed reference.")
                low, high = operation["bounds"]
                self._transport.command(f"motor {motor}")
                self._transport.command(f"soft {low} {high}")
                self._read_status()
                state = self._motors[motor]
                if (
                    operation.get("failure")
                    or state.mode != "idle"
                    or state.homed is not True
                    or state.soft is not True
                    or state.position != 0
                    or self._microstep != operation["microstep"]
                ):
                    raise ControllerError("Arduino did not confirm homing and soft limits.")
                self._references[motor] = self._microstep
                self._end_sequence("ok", "Home and soft limits confirmed.")
            else:
                self._require_reference(motor)
                if state.position != operation["target"]:
                    raise ControllerError("Stage stopped before the requested position.")
                operation["legs"].pop(0)
                operation["phase"] = "cooldown"
                # The next poll rechecks leased authority and fresh feedback.
        except ControllerError as exc:
            self._end_sequence("failed", str(exc))
            self._transport.command("stop all")
            self._read_status(command_result="stopped")
            if any(state.mode != "idle" for state in self._motors.values()):
                self._lock_unconfirmed_stop()

    def _automation_snapshot(self):
        operation = self._automation
        return dict(
            configured=self._geometry is not None,
            error=self._configuration_error or self._automation_error,
            active={key: operation[key] for key in ("action", "motor", "pose", "phase")}
            if operation
            else None,
            poses=sorted(self._geometry.poses) if self._geometry else [],
        )
