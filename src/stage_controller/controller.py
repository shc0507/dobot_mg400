"""Thread-safe controller, independent of Flask and the browser.

One lock owns the serial stream. It covers the entire select/direction/speed/
steps or hold transaction so separate motors or browser tabs cannot mix commands.
"""

import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone

from serial.tools import list_ports

from .protocol import (
    BAUD_RATE,
    COOLDOWN_SECONDS,
    DEFAULT_SPEED,
    DEFAULT_STEPS,
    HOLD_TIMEOUT_SECONDS,
    MAX_SPEED,
    MAX_STEPS,
    MICROSTEP_MODES,
    MOTOR_IDS,
    CommandRejected,
    ControllerError,
    DemoTransport,
    LinkError,
    MotorStatus,
    SerialTransport,
)

# The PGEA gripper's FTDI RS-485 converter on the same host (see scripts/gripper_feedback.py).
# Never list or open it: Arduino commands on that bus could move the gripper.
GRIPPER_CONVERTER = (0x0403, 0x6001, "BG02Q5TN")


def _gripper_devices(ports):
    # A /dev/serial/by-id link and its /dev/ttyUSBn name are one device.
    return {
        os.path.realpath(port.device)
        for port in ports
        if (port.vid, port.pid, port.serial_number) == GRIPPER_CONVERTER
    }


def is_gripper_converter(path):
    """Check whether a device path is the gripper converter.

    A /dev/serial/by-id name contains the serial number, also while the converter is unplugged.
    """
    if GRIPPER_CONVERTER[2] in os.path.basename(path):
        return True
    return os.path.realpath(path) in _gripper_devices(list_ports.comports())


class MotorController:
    def __init__(self, *, demo=False, serial=None, transport_factory=None, clock=time.monotonic):
        self.demo = demo
        # With a configured device, offer only that device. Demo mode opens no device.
        self.serial = None if demo else serial or None
        self.clock = clock
        self._factory = transport_factory or (
            (lambda _port: DemoTransport(clock)) if demo else SerialTransport
        )
        self._lock = threading.RLock()
        self._transport = None
        self._port = None
        self._error = None
        self._motors = {motor: MotorStatus(motor) for motor in MOTOR_IDS}
        self._microstep = None
        self._active = set()
        self._holds = {}
        self._closed_holds = {}
        self._cooldowns = dict.fromkeys(MOTOR_IDS, 0.0)
        self._events = deque(maxlen=30)
        self._event_id = 0
        self._shutdown = threading.Event()
        self._thread = None

    def ports(self):
        if self.demo:
            return [{"device": "DEMO", "description": "Simulated Arduino · no hardware"}]
        ports = sorted(list_ports.comports())
        gripper = _gripper_devices(ports)
        ports = [port for port in ports if os.path.realpath(port.device) not in gripper]
        if self.serial is None:
            return [{"device": port.device, "description": port.description} for port in ports]
        # Resolve the link at each refresh: after a reconnect it can name another ttyACMn device.
        target = os.path.realpath(self.serial)
        return [
            {"device": self.serial, "description": port.description}
            for port in ports
            if os.path.realpath(port.device) == target
        ][:1]

    def _event(self, message, level="info"):
        self._event_id += 1
        self._events.appendleft(
            {
                "id": self._event_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "level": level,
            }
        )

    def _require_connection(self):
        if self._transport is None:
            raise ControllerError("Connect to an Arduino first.")

    def _read_status(self):
        statuses = self._transport.status()
        self._microstep = self._transport.microstep
        now = self.clock()
        for motor, state in statuses.items():
            if state.mode == "idle":
                self._forget_hold(motor)
            if state.mode != "idle":
                self._active.add(motor)
            elif motor in self._active:
                self._active.remove(motor)
                self._cooldowns[motor] = now + COOLDOWN_SECONDS
                self._event(f"{motor.title()} stopped · 1 second cooldown")
        self._motors = statuses

    def _lose_connection(self, exc):
        # Try to cancel movement even when the response stream is lost.
        # A broken cable cannot deliver this command: never claim it stopped.
        try:
            self._transport.command("stop all")
        except ControllerError:
            pass
        finally:
            for motor in list(self._holds):
                self._forget_hold(motor)
            self._transport.close()
            self._transport = None
            self._port = None
        self._error = f"{exc} Connection lost; motor stop could not be confirmed."
        self._event(self._error, "error")

    def connect(self, port):
        with self._lock:
            # Only a listed port: a remote client must not open arbitrary device paths.
            if not isinstance(port, str) or port not in {item["device"] for item in self.ports()}:
                raise ValueError("That port is no longer available. Refresh the port list.")
            if self._transport is not None:
                raise ControllerError("Disconnect the current board before choosing another port.")
            transport = self._factory(port)
            try:
                statuses = transport.status()
            except ControllerError:
                transport.close()
                raise
            self._transport = transport
            self._port = port
            self._motors = statuses
            self._microstep = transport.microstep
            self._active = {motor for motor, state in statuses.items() if state.mode != "idle"}
            self._error = None
            # Preserve cooldowns across reconnects; reconnecting is not a bypass.
            self._event(f"Connected to {port} · {BAUD_RATE:,} baud")
            return self.snapshot()

    def disconnect(self):
        with self._lock:
            if self._transport is not None:
                try:
                    self._transport.command("stop all")
                    self._read_status()
                except ControllerError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
                self._transport.close()
                self._transport = None
                self._port = None
                self._event("All motors stopped · disconnected")
            return self.snapshot()

    def set_microstep(self, divisor):
        if type(divisor) is not int or divisor not in MICROSTEP_MODES:
            raise ValueError("Choose a microstep mode: 1, 2, 4, 8, or 16.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if self._microstep is None:
                    raise ControllerError(
                        "Upload the updated three-motor-demo sketch to change stepping mode."
                    )
                if any(state.mode != "idle" for state in self._motors.values()):
                    raise ControllerError("Stop all motors before changing stepping mode.")
                if divisor != self._microstep:
                    self._transport.command(f"microstep {divisor}")
                    self._read_status()
                    if self._microstep != divisor:
                        raise LinkError("Arduino did not confirm the requested stepping mode.")
                    self._event(f"All motors · {MICROSTEP_MODES[divisor]}")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def move(self, motor, direction, speed, steps):
        if motor not in MOTOR_IDS or direction not in ("cw", "ccw"):
            raise ValueError("Choose a valid motor and direction (cw or ccw).")
        for value, maximum, name in ((speed, MAX_SPEED, "Speed"), (steps, MAX_STEPS, "Steps")):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be a whole number from 1 to {maximum:,}.")
        with self._lock:
            self._require_connection()
            try:
                self._read_status()
                if self._motors[motor].mode != "idle":
                    raise ControllerError(f"{motor.title()} is still moving.")
                if self.clock() < self._cooldowns[motor]:
                    raise ControllerError(f"Wait for {motor}'s 1 second cooldown to finish.")
                for command in (
                    f"motor {motor}",
                    f"dir {direction}",
                    f"speed {speed}",
                    f"steps {steps}",
                ):
                    self._transport.command(command)
                # Mark active even if a one-step move finishes before the next poll.
                self._active.add(motor)
                self._motors[motor].mode = "moving"
                self._motors[motor].remaining = steps if direction == "cw" else -steps
                self._motors[motor].speed = speed
                self._motors[motor].direction = direction
                self._event(
                    f"{motor.title()} · {direction.upper()} · {steps:,} pulses at {speed:,}/s"
                )
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    @staticmethod
    def _validate_hold(motor, hold_id):
        if motor not in MOTOR_IDS:
            raise ValueError("Choose a valid motor.")
        if not isinstance(hold_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", hold_id):
            raise ValueError("Invalid hold identifier.")

    def _forget_hold(self, motor):
        hold = self._holds.pop(motor, None)
        if hold is not None:
            self._closed_holds[(motor, hold[0])] = self.clock()

    def _stop_hold(self, motor):
        self._transport.command(f"motor {motor}")
        self._transport.command("stop")
        self._forget_hold(motor)

    def hold(self, motor, direction, speed, hold_id, sampled_at):
        self._validate_hold(motor, hold_id)
        if direction not in ("cw", "ccw") or type(speed) is not int or not 1 <= speed <= MAX_SPEED:
            raise ValueError("Choose a direction and a whole speed from 1 to 1,000 pulses/s.")
        with self._lock:
            now = self.clock()
            # Expire delayed start requests. Closed identifiers outlive this window,
            # so a release arriving before its start can never turn into a late move.
            if (
                type(sampled_at) not in (int, float)
                or not math.isfinite(sampled_at)
                or not 0 <= now - sampled_at <= 2.0
            ):
                raise ControllerError("Hold request expired. Release and press again.")
            self._closed_holds = {
                key: ended for key, ended in self._closed_holds.items() if now - ended < 10
            }
            if (motor, hold_id) in self._closed_holds:
                raise ControllerError("Hold has ended. Release and press again.")
            self._require_connection()
            try:
                self._read_status()
                if motor in self._holds and self._holds[motor][0] == hold_id:
                    return self.snapshot()
                if self._motors[motor].mode != "idle":
                    raise ControllerError(f"{motor.title()} is still moving.")
                if self.clock() < self._cooldowns[motor]:
                    raise ControllerError(f"Wait for {motor}'s 1 second cooldown to finish.")
                for command in (f"motor {motor}", f"dir {direction}", f"speed {speed}"):
                    self._transport.command(command)
                try:
                    self._transport.command("hold")
                except CommandRejected as exc:
                    raise ControllerError(
                        "Hold to move was rejected. Upload the updated three-motor-demo sketch."
                    ) from exc
                self._holds[motor] = (hold_id, self.clock() + HOLD_TIMEOUT_SECONDS)
                self._active.add(motor)
                self._motors[motor].mode = "continuous"
                self._motors[motor].remaining = 0
                self._motors[motor].speed = speed
                self._motors[motor].direction = direction
                self._event(f"{motor.title()} · hold {direction.upper()} · {speed:,} pulses/s")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def keep_hold_alive(self, motor, hold_id):
        self._validate_hold(motor, hold_id)
        with self._lock:
            self._require_connection()
            hold = self._holds.get(motor)
            if hold is None or hold[0] != hold_id:
                raise ControllerError("Hold has ended. Release and press again.")
            try:
                if self.clock() >= hold[1]:
                    self._stop_hold(motor)
                    self._read_status()
                    raise ControllerError("Hold timed out. Release and press again.")
                self._transport.command(f"motor {motor}")
                try:
                    self._transport.command("keepalive")
                except CommandRejected:
                    self._stop_hold(motor)
                    self._read_status()
                    raise ControllerError("Hold has ended. Release and press again.") from None
                self._holds[motor] = (hold_id, self.clock() + HOLD_TIMEOUT_SECONDS)
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def release_hold(self, motor, hold_id):
        self._validate_hold(motor, hold_id)
        with self._lock:
            # Record cancellation even when the corresponding start is still in flight.
            self._closed_holds[(motor, hold_id)] = self.clock()
            hold = self._holds.get(motor)
            if hold is not None and hold[0] == hold_id and self._transport is not None:
                try:
                    self._stop_hold(motor)
                    self._read_status()
                except ControllerError as exc:
                    self._lose_connection(exc)
                    raise LinkError(self._error) from exc
            return self.snapshot()

    def stop_all(self):
        with self._lock:
            self._require_connection()
            try:
                self._transport.command("stop all")
                self._read_status()
                self._event("Stop all acknowledged by Arduino")
            except ControllerError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            return self.snapshot()

    def poll(self):
        with self._lock:
            if self._transport is not None:
                try:
                    for motor, (_, deadline) in list(self._holds.items()):
                        if self.clock() >= deadline:
                            self._stop_hold(motor)
                    self._read_status()
                except (LinkError, CommandRejected) as exc:
                    self._lose_connection(exc)

    def snapshot(self):
        with self._lock:
            now = self.clock()
            connected = self._transport is not None
            motors = []
            for motor, status in self._motors.items():
                cooldown = max(0.0, self._cooldowns[motor] - now)
                motors.append(
                    {
                        **asdict(status),
                        "cooldown": round(cooldown, 3),
                        "available": connected and status.mode == "idle" and cooldown == 0,
                    }
                )
            return {
                "sampled_at": now,
                "connected": connected,
                "port": self._port,
                "demo": self.demo,
                "error": self._error,
                "baud_rate": BAUD_RATE,
                "microstep": self._microstep if connected else None,
                "microstep_modes": [
                    {"value": value, "label": label} for value, label in MICROSTEP_MODES.items()
                ],
                "motors": motors,
                "events": list(self._events),
                "defaults": {"speed": DEFAULT_SPEED, "steps": DEFAULT_STEPS},
                "limits": {"speed": MAX_SPEED, "steps": MAX_STEPS},
            }

    def start(self):
        if self._thread is None:

            def run():
                while not self._shutdown.wait(0.2):
                    self.poll()

            self._thread = threading.Thread(target=run, name="arduino-status", daemon=True)
            self._thread.start()

    def close(self):
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            self.disconnect()
        except ControllerError:
            pass
