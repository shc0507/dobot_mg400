"""Instrument contract for measured shaft rotations.

Lease authorization precedes the device lock. A stop epoch cancels delayed
sequence commands without allowing another process to own the serial stream.
"""

import math
import time
import uuid

from labkit.describe import command, describe, parameter
from labkit.errors import ServiceError
from labkit.events import EventLog

from .lease import StageLease
from .protocol import (
    ENCODER_COUNTS_PER_ROTATION,
    MAX_ROTATIONS,
    MAX_SPEED,
    MICROSTEP_MODES,
    MIN_ROTATIONS,
    MIN_SPEED,
    MOTOR_IDS,
    POSITION_LIMIT,
    ControllerError,
    LinkError,
)


class StageContract:
    def _init_contract(self):
        self.events = EventLog()
        self.lease = StageLease(clock=self.clock, on_expire=self.on_lease_expired)
        self._state_seq = 0
        self._last_command = None
        self._pending_commands = {}
        self._contract_fault = None
        self._contract_stopping = 0
        self._stop_epoch = 0

    def describe(self):
        motor = parameter("string", enum=list(MOTOR_IDS))
        direction = parameter("string", enum=["cw", "ccw"])
        speed = parameter(
            "number",
            minimum=MIN_SPEED,
            maximum=MAX_SPEED,
            description="Motor shaft rotations/s; limited by device max_speed",
        )
        hold_id = parameter("string", pattern="^[A-Za-z0-9_-]{16,64}$")
        epoch = parameter("integer", minimum=0)

        def spec(properties, *, any_client=False, recovery=False):
            return command(
                parameter(
                    "object",
                    properties=properties,
                    required=list(properties),
                    additionalProperties=False,
                ),
                any_client=any_client,
                recovery=recovery,
            )

        move = dict(
            motor=motor,
            direction=direction,
            speed=speed,
            rotations=parameter("number", minimum=MIN_ROTATIONS, maximum=MAX_ROTATIONS),
        )
        absolute = dict(
            motor=motor,
            position=parameter("number", minimum=0, maximum=POSITION_LIMIT),
            speed=speed,
        )
        stepping = dict(microstep=parameter("integer", enum=list(MICROSTEP_MODES)))
        commands = {
            "connect": spec(dict(port=parameter("string")), recovery=True),
            "move": spec(move),
            "move_guarded": spec({**move, "stop_epoch": epoch}),
            "move_to_position": spec(absolute),
            "move_to_position_guarded": spec({**absolute, "stop_epoch": epoch}),
            "calibrate": spec(dict(motor=motor, speed=speed)),
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
            "set_microstep": spec(stepping),
            "set_microstep_guarded": spec({**stepping, "stop_epoch": epoch}),
        }
        for action in (
            "reset_position",
            "set_limit",
            "clear_upper_limit",
            "clear_lower_limit",
            "clear_fault",
        ):
            commands[action] = spec(dict(motor=motor))
        return describe(
            service="stage",
            version="0.2.0",
            demo=self.demo,
            zones=[],
            lease_seconds=self.lease.seconds,
            on_lease_expiry="stop_all",
            commands=commands,
        )

    def _contract_state(self):
        self._state_seq += 1
        return {
            "service": "stage",
            "seq": self._state_seq,
            "ts_unix": time.time(),
            "busy": any(self._motor_busy(motor) for motor in self._motors.values()),
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
            "stop_epoch": self._stop_epoch + self.lease.epoch,
        }

    def _finish_command(self, entry, result, message, *, log=True):
        self._last_command = {key: entry[key] for key in ("id", "action", "client")} | {
            "result": result,
            "message": message,
            "finished_at": time.time(),
        }
        if log:
            self._event(f"Stage {entry['action']} {result}: {message}")

    def _complete_motion_commands(self, result="ok"):
        for command_id, entry in list(self._pending_commands.items()):
            state = self._motors[entry["motor"]]
            if self._motor_busy(state) or not self._feedback_fresh(entry["motor"]):
                continue
            del self._pending_commands[command_id]
            healthy = state.encoder == "ok" and state.fault == "none"
            reached = healthy and (
                state.calibration == "done"
                if entry["action"] == "calibrate"
                else math.isclose(
                    state.position,
                    entry["target"],
                    rel_tol=0,
                    abs_tol=max(state.tolerance, 1 / ENCODER_COUNTS_PER_ROTATION),
                )
            )
            self._finish_command(
                entry,
                "failed" if result == "ok" and not reached else result,
                "Encoder position confirmed."
                if reached
                else "Motor stopped without reaching the requested position.",
            )

    def _fail_motion_commands(self, message):
        for entry in self._pending_commands.values():
            self._finish_command(entry, "failed", message)
        self._pending_commands.clear()

    def _lock_unconfirmed_stop(self):
        self._contract_fault = "Motor stop is unconfirmed. Check the stage before reconnecting."
        self._fail_motion_commands(self._contract_fault)

    def _stop_after_disconnect(self):
        if self._error is None and not any(
            self._motor_busy(motor) for motor in self._motors.values()
        ):
            return "confirmed"
        self._lock_unconfirmed_stop()
        return "unconfirmed"

    def command(self, action, args, client):
        specs = self.describe()["commands"]
        if action not in specs or not isinstance(args, dict):
            raise ValueError("Unknown stage command or invalid arguments.")
        if set(args) != set(specs[action]["params"]["required"]):
            raise ValueError("Command arguments must match the discovery schema.")
        entry = {"id": uuid.uuid4().hex, "action": action, "client": client}
        if action == "connect":
            try:
                self.connect(**args, client=client)
            except (ValueError, ControllerError) as exc:
                with self._lock:
                    self._finish_command(
                        entry, "failed" if isinstance(exc, LinkError) else "rejected", str(exc)
                    )
                if isinstance(exc, ControllerError):
                    raise ServiceError(
                        503 if isinstance(exc, LinkError) else 409, str(exc)
                    ) from exc
                raise
            with self._lock:
                self._finish_command(entry, "ok", "connect confirmed.")
            return entry["id"]
        with self.lease.control(client, require_holder=not specs[action]["any_client"]), self._lock:
            if (
                self._error or self._contract_fault or self._contract_stopping
            ) and action != "stop_all":
                raise ServiceError(409, "The service is locked. Only stop is allowed.")
            try:
                guarded = action.endswith("_guarded")
                operation = action.removesuffix("_guarded")
                if guarded:
                    epoch = args["stop_epoch"]
                    if type(epoch) is not int or epoch < 0:
                        raise ValueError("The stop epoch must be a nonnegative integer.")
                    if epoch != self._stop_epoch + self.lease.epoch:
                        raise ControllerError("Stage motion was cancelled or control changed.")
                    args = {key: value for key, value in args.items() if key != "stop_epoch"}
                elif operation not in ("keepalive", "release_hold", "stop_all"):
                    self._stop_epoch += 1
                if operation == "set_microstep":
                    self.set_microstep(args["microstep"])
                elif operation == "keepalive":
                    self.keep_hold_alive(**args)
                else:
                    getattr(self, operation)(**args)
            except (ValueError, ControllerError) as exc:
                if isinstance(exc, LinkError) and self._transport is not None:
                    self._lose_connection(exc)
                self._finish_command(
                    entry, "failed" if isinstance(exc, LinkError) else "rejected", str(exc)
                )
                if isinstance(exc, ControllerError):
                    raise ServiceError(
                        503 if isinstance(exc, LinkError) else 409, str(exc)
                    ) from exc
                raise
            if operation in ("move", "move_to_position", "calibrate"):
                state = self._motors[args["motor"]]
                target = args.get("position", state.position + state.remaining)
                self._pending_commands[entry["id"]] = {
                    **entry,
                    "motor": args["motor"],
                    "target": target,
                }
                self._last_command = {
                    **entry,
                    "result": "running",
                    "message": "Command acknowledged.",
                }
                self._complete_motion_commands()
            else:
                self._finish_command(
                    entry, "ok", "Command acknowledged.", log=operation != "keepalive"
                )
        return entry["id"]

    def stop(self, client, wait_s):
        # This path deliberately bypasses the lease, including failed expiry.
        deadline = time.monotonic() + wait_s
        with self._lock:
            self._stop_epoch += 1
            if self._transport is None:
                if any(self._motor_busy(motor) for motor in self._motors.values()):
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
            while state["busy"]:
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
            self._event(f"Automation lease expired Â· stop {outcome}")
