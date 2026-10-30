"""Instrument contract methods mixed into the stage's serial controller.

Lease authorization precedes the controller lock. Snapshots taken under the
controller lock deliberately do not read the lease; HTTP adapters add it later.
"""

import time
import uuid

from labkit.describe import command, describe, parameter
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.lease import Lease

from .protocol import MAX_SPEED, MAX_STEPS, MICROSTEP_MODES, MOTOR_IDS, ControllerError, LinkError


class StageContract:
    def _init_contract(self):
        self.events = EventLog()
        self.lease = Lease(clock=self.clock, on_expire=self.on_lease_expired)
        self._state_seq = 0
        self._last_command = None
        self._pending_commands = {}
        self._contract_fault = None
        self._contract_stopping = 0

    def describe(self):
        motor = parameter("string", enum=list(MOTOR_IDS))
        direction = parameter("string", enum=["cw", "ccw"])
        speed = parameter("integer", minimum=1, maximum=MAX_SPEED, description="STEP pulses/s")
        hold_id = parameter("string", pattern="^[A-Za-z0-9_-]{16,64}$")

        def spec(properties, *, any_client=False):
            return command(
                parameter(
                    "object",
                    properties=properties,
                    required=list(properties),
                    additionalProperties=False,
                ),
                any_client=any_client,
            )

        return describe(
            service="stage",
            version="0.1.0",
            demo=self.demo,
            zones=[],
            lease_seconds=self.lease.seconds,
            on_lease_expiry="stop_all",
            commands={
                "move": spec(
                    dict(
                        motor=motor,
                        direction=direction,
                        speed=speed,
                        steps=parameter("integer", minimum=1, maximum=MAX_STEPS),
                    )
                ),
                "hold": spec(
                    dict(
                        motor=motor,
                        direction=direction,
                        speed=speed,
                        hold_id=hold_id,
                        sampled_at=parameter("number"),
                    )
                ),
                "keepalive": spec(dict(motor=motor, hold_id=hold_id)),
                "release_hold": spec(dict(motor=motor, hold_id=hold_id)),
                "stop_all": spec({}, any_client=True),
                "set_microstep": spec(
                    dict(microstep=parameter("integer", enum=list(MICROSTEP_MODES)))
                ),
            },
        )

    def panel_snapshot(self):
        self.lease.poll()
        lease = self.lease.snapshot()
        return {**self.snapshot(), "lease": lease}

    def manual(self, operation, *args):
        """Exclude new manual actuation atomically with lease acquisition."""
        with self.lease.control("stage-panel", require_holder=False):
            if self.lease.snapshot()["holder"] is not None:
                raise ControllerError("Stage is controlled by automation")
            with self._lock:
                if self._contract_fault is not None:
                    raise ControllerError(self._contract_fault)
                if self._contract_stopping:
                    raise ControllerError("Stop confirmation is in progress.")
                return getattr(self, operation)(*args)

    def _contract_state(self):
        self._state_seq += 1
        return {
            "service": "stage",
            "seq": self._state_seq,
            "ts_unix": time.time(),
            "busy": any(motor.mode != "idle" for motor in self._motors.values()),
            "locked": bool(self._error or self._contract_fault or self._contract_stopping),
            "fault": (
                {"code": "device_link_lost", "message": self._error}
                if self._error
                else {"code": "stop_unconfirmed", "message": self._contract_fault}
                if self._contract_fault
                else {"code": "stop_pending", "message": "Stop confirmation is in progress."}
                if self._contract_stopping
                else None
            ),
            "last_command": dict(self._last_command) if self._last_command else None,
        }

    def _finish_command(self, entry, result, message):
        self._last_command = {key: entry[key] for key in ("id", "action", "client")} | {
            "result": result,
            "message": message,
            "finished_at": time.time(),
        }
        self._event(f"Automation {entry['action']} {result}: {message}")

    def _complete_motion_commands(self, result="ok"):
        for command_id, entry in list(self._pending_commands.items()):
            if self._motors[entry["motor"]].mode == "idle":
                del self._pending_commands[command_id]
                self._finish_command(entry, result, "Motor is idle.")

    def _fail_motion_commands(self, message):
        for entry in self._pending_commands.values():
            self._finish_command(entry, "failed", message)
        self._pending_commands.clear()

    def _lock_unconfirmed_stop(self):
        self._contract_fault = "Motor stop is unconfirmed. Check the stage before reconnecting."
        self._fail_motion_commands(self._contract_fault)

    def _stop_after_disconnect(self):
        # Only used after this stop owned a live connection. A normal disconnect
        # may supply its final idle feedback, but a lost or moving board cannot.
        if self._error is None and all(motor.mode == "idle" for motor in self._motors.values()):
            return "confirmed"
        self._lock_unconfirmed_stop()
        return "unconfirmed"

    def command(self, action, args, client):
        specs = self.describe()["commands"]
        if action not in specs or not isinstance(args, dict):
            raise ValueError("Unknown stage command or invalid arguments.")
        expected = set(specs[action]["params"]["required"])
        if set(args) != expected:
            raise ValueError("Command arguments must match the discovery schema.")
        entry = {"id": uuid.uuid4().hex, "action": action, "client": client}
        with self._lock:
            if (
                self._error is not None
                or self._contract_fault is not None
                or self._contract_stopping
            ) and action != "stop_all":
                raise ServiceError(409, "The service is locked. Only stop is allowed.")
            try:
                if action == "set_microstep":
                    self.set_microstep(args["microstep"])
                elif action == "keepalive":
                    self.keep_hold_alive(**args)
                else:
                    getattr(self, action)(**args)
            except (ValueError, ControllerError) as exc:
                self._finish_command(
                    entry, "failed" if isinstance(exc, LinkError) else "rejected", str(exc)
                )
                if isinstance(exc, ControllerError):
                    raise ServiceError(
                        503 if isinstance(exc, LinkError) else 409, str(exc)
                    ) from exc
                raise
            if action == "move":
                self._pending_commands[entry["id"]] = {**entry, "motor": args["motor"]}
            else:
                self._finish_command(entry, "ok", "Command acknowledged.")
        return entry["id"]

    def stop(self, client, wait_s):
        # This path deliberately bypasses the lease, including failed expiry.
        deadline = time.monotonic() + wait_s
        with self._lock:
            if self._transport is None:
                if any(motor.mode != "idle" for motor in self._motors.values()):
                    self._lock_unconfirmed_stop()
                return "unconfirmed"
            self._contract_stopping += 1
        try:
            with self._lock:
                if self._transport is None:
                    return self._stop_after_disconnect()
                try:
                    state = self.stop_all()
                except ControllerError:
                    return "unconfirmed"
            while any(motor["mode"] != "idle" for motor in state["motors"]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._lock:
                        self._lock_unconfirmed_stop()
                    return "pending"
                # Leave the device lock available to other stop requests while waiting.
                time.sleep(min(0.02, remaining))
                with self._lock:
                    if self._transport is None:
                        return self._stop_after_disconnect()
                    try:
                        self._read_status(command_result="stopped")
                    except ControllerError as exc:
                        self._lose_connection(exc)
                        return "unconfirmed"
                    state = self.snapshot()
            return "confirmed"
        finally:
            with self._lock:
                self._contract_stopping -= 1

    def on_lease_expired(self):
        outcome = self.stop("lease-expiry", 0)
        with self._lock:
            self._event(f"Automation lease expired · stop {outcome}")
