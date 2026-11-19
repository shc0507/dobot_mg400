"""Encoder stage controller with the Console lease and instrument contract.

The Arduino measures rotations and owns the closed loop. Legacy pulse geometry
is retained as configuration data only; it cannot generate encoder motion.
"""

import os
import time

from serial.tools import list_ports

from .config import DEFAULT_CONFIG, load_geometry
from .contract import StageContract
from .encoder import MotorController as EncoderController
from .protocol import (
    ACTIVE_CALIBRATION,
    TELEMETRY_TIMEOUT_SECONDS,
    ControllerError,
    DemoTransport,
    SerialTransport,
)


class MotorController(StageContract, EncoderController):
    def __init__(
        self,
        *,
        demo=False,
        serial=None,
        transport_factory=None,
        clock=time.monotonic,
        config=None,
        frame_source=None,
    ):
        self.serial = None if demo else serial or None
        factory = transport_factory or (
            (lambda _port: DemoTransport(clock)) if demo else SerialTransport
        )
        super().__init__(demo=demo, transport_factory=factory, clock=clock)
        self._init_contract()
        self._geometry = None
        self._configuration_error = None
        # Do not reinterpret old steps/mm, named poses or camera references as turns.
        if config is not None or not demo:
            try:
                self._geometry = load_geometry(config or DEFAULT_CONFIG)
            except (OSError, ValueError, UnicodeError) as exc:
                self._configuration_error = f"Legacy stage configuration unavailable: {exc}"

    def ports(self):
        if self.demo:
            return super().ports()
        ports = sorted(list_ports.comports())
        if self.serial is None:
            return [{"device": port.device, "description": port.description} for port in ports]
        target = os.path.realpath(self.serial)
        return [
            {"device": self.serial, "description": port.description}
            for port in ports
            if os.path.realpath(port.device) == target
        ]

    def _event(self, message, level="info"):
        super()._event(message, level)
        self.events.add(level, message, kind="activity")

    @staticmethod
    def _motor_busy(state):
        return (
            state.mode != "idle"
            or state.calibration in ACTIVE_CALIBRATION
            or state.warning == "direction_pause"
        )

    def _feedback_fresh(self, motor):
        state = self._motors[motor]
        elapsed = max(0, (self.clock() - self._observed_at) * 1000)
        ages = [state.telemetry_age_ms]
        if self._home_switches:
            ages.append(state.limit_telemetry_age_ms)
        return (
            self._transport is not None
            and self._closed_loop
            and all(
                age is not None and age + elapsed <= TELEMETRY_TIMEOUT_SECONDS * 1000
                for age in ages
            )
        )

    def _read_status(self, *, fresh=True, command_result="ok"):
        super()._read_status(fresh=fresh)
        self._complete_motion_commands(command_result)

    def poll(self):
        # Expiry can call stop. Keep the lease lock outside the device lock.
        self.lease.poll()
        super().poll()

    def _lose_connection(self, exc):
        self._stop_epoch += 1
        super()._lose_connection(exc)
        self._lock_unconfirmed_stop()

    def connect(self, port, *, client=None):
        if client is not None:
            with self.lease.control(client):
                return self.connect(port)
        with self._lock:
            if self._contract_stopping:
                raise ControllerError("Stop confirmation is in progress. Wait before connecting.")
            if port not in {item["device"] for item in self.ports()}:
                raise ValueError("That port is no longer available. Refresh the port list.")
            super().connect(port)
            if not any(self._motor_busy(state) for state in self._motors.values()):
                self._contract_fault = None
            self._stop_epoch += 1
            return self.snapshot()

    def disconnect(self):
        with self._lock:
            self._stop_epoch += 1
            super().disconnect()
            self._fail_motion_commands("Stage disconnected.")
            if any(self._motor_busy(state) for state in self._motors.values()):
                self._lock_unconfirmed_stop()
            return self.snapshot()

    def _require_motion_ready(self, motor, speed):
        if self._contract_fault or self._contract_stopping:
            raise ControllerError("The service is locked. Only stop is allowed.")
        super()._require_motion_ready(motor, speed)
        if any(self._motor_busy(state) for name, state in self._motors.items() if name != motor):
            raise ControllerError("Stop the other stage motor before starting this motor.")
        if any(self.clock() < deadline for deadline in self._cooldowns.values()):
            raise ControllerError("Wait for the stage cooldown to finish.")

    def stop_all(self):
        with self._lock:
            self._stop_epoch += 1
            pending, self._pending_commands = self._pending_commands, {}
            try:
                result = super().stop_all()
            except ControllerError:
                for entry in pending.values():
                    self._finish_command(entry, "failed", "Motor stop could not be confirmed.")
                raise
            if any(self._motor_busy(state) for state in self._motors.values()):
                self._pending_commands.update(pending)
            else:
                for entry in pending.values():
                    self._finish_command(entry, "stopped", "Stopped by request.")
            return result

    def snapshot(self):
        with self._lock:
            state = super().snapshot()
            other_busy = any(self._motor_busy(item) for item in self._motors.values())
            cooldown = any(self.clock() < deadline for deadline in self._cooldowns.values())
            for motor in state["motors"]:
                ready = motor["available"] and not other_busy and not cooldown
                motor.update(
                    position_rotations=motor["position"] if self._closed_loop else None,
                    remaining_rotations=motor["remaining"] if self._closed_loop else None,
                    lower_limit_rotations=motor["lower_limit"]
                    if motor["lower_limit_set"]
                    else None,
                    upper_limit_rotations=motor["upper_limit"]
                    if motor["upper_limit_set"]
                    else None,
                    motion_ready=ready,
                    available=ready,
                    positioning_ready=motor["positioning_ready"] and ready,
                    telemetry_fresh=self._feedback_fresh(motor["id"]),
                    position_tolerance_rotations=motor["tolerance"],
                    coordinate_unit="rotations",
                )
            return {
                **state,
                **self._contract_state(),
                "coordinate_unit": "rotations",
                "configuration_error": self._configuration_error,
                "legacy_geometry_active": False,
            }

    def preview_reference(self, motor):
        raise ControllerError("Camera references are inactive. Use encoder calibration.")

    def teach_reference(self, motor, confirm, *, client=None):
        raise ControllerError("Camera references are inactive. Use encoder calibration.")
