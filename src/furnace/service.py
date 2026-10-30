"""Furnace monitoring, bounded recipes, and durable client-loss recovery.

Construction opens nothing. Call start() to attach and recover, then optionally
run the poll thread. Tests and embedding can use start(background=False) and
poll_once(). Factories receive validated configuration and must return the real
driver interface. All output changes use one operation lock. A controlling
client must hold lease before a non-stop command. Stop bypasses lease failures.

Gas map and idle gas settings are JSON objects keyed by channel 0, 1, or 2.
MG400_FURNACE_GAS_MAP maps channels to operator-supplied labels; it has no default.
MG400_FURNACE_IDLE_GAS maps channels to percent (unspecified channels are off).
The orphan grace setting is MG400_FURNACE_ORPHAN_GRACE_MIN, default 15 minutes.
Signal handlers belong to the application: SIGTERM must call close().
"""

import copy
import json
import math
import os
import threading
import time
from functools import wraps

from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.lease import Lease

from . import controller as protocol
from .config import FurnaceConfig, serial_port
from .controller import GAS_UNIT_IDS, Furnace, MFCFlowController
from .state import StateError, StateStore

FRAME_METADATA_FIELDS = (
    "temperature_sample_time",
    "temperature_c",
    "target_c",
    "ramp_rate_c_per_min",
    "furnace_state",
    "sequence_running",
    "sequence_step",
    "sequence_phase",
    "gas_1_target_percent",
    "gas_2_target_percent",
    "gas_3_target_percent",
)


def _controlled(method):
    @wraps(method)
    def call(self, *args, client=None, **kwargs):
        if client is None:
            raise ServiceError(409, "Acquire the control lease before sending a command.")
        with self.lease.control(client):
            with self._operation_lock:
                if self._locked:
                    raise ServiceError(409, "Acknowledge the furnace fault before control.")
                if self._closed:
                    raise ServiceError(409, "The service is closed.")
                result = method(self, *args, **kwargs)
        self._notify_pending()
        return self.status() if result is None else result

    return call


def _json_setting(value, name):
    if value is None:
        value = os.environ.get(name)
    if isinstance(value, str):

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError(f"Duplicate {name} key: {key}")
                result[key] = item
            return result

        value = json.loads(value, object_pairs_hook=unique)
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


class FurnaceService:
    """Serialize driver access and retain the source's ramp/HOLD timer rules."""

    def __init__(
        self,
        poll_interval=1.0,
        target_tolerance_c=1.0,
        port=None,
        slave_address=None,
        flow_port=None,
        *,
        config=None,
        gas_map=None,
        idle_gas=None,
        orphan_grace_min=None,
        state_store=None,
        furnace_factory=None,
        flow_factory=None,
        clock=time.monotonic,
        wall_clock=time.time,
        lease_seconds=3,
    ):
        self.config = config or FurnaceConfig.from_env(
            port=port, slave_address=slave_address, flow_port=flow_port
        )
        self.port = self.config.port if port is None else serial_port(port, "MG400_FURNACE_SERIAL")
        self.slave_address = (
            self.config.slave_address
            if slave_address is None
            else protocol.resolve_slave_address(slave_address)
        )
        self.flow_port = (
            self.config.flow_port
            if flow_port is None
            else serial_port(flow_port, "MG400_MFC_SERIAL")
        )
        self.poll_interval = self._number(poll_interval, "Poll interval", exclusive_minimum=0)
        self.target_tolerance_c = self._number(target_tolerance_c, "Target tolerance", minimum=0)
        self.clock, self.wall_clock = clock, wall_clock
        self.orphan_grace_min = self._number(
            os.environ.get("MG400_FURNACE_ORPHAN_GRACE_MIN", "15")
            if orphan_grace_min is None
            else orphan_grace_min,
            "Orphan grace",
            minimum=0,
        )
        self.gas_map = self._gas_map(_json_setting(gas_map, "MG400_FURNACE_GAS_MAP"))
        self.idle_gas = self._gas_plan(
            _json_setting(idle_gas, "MG400_FURNACE_IDLE_GAS") or {}, complete=True
        )
        self.store = state_store if state_store is not None else StateStore()
        self._furnace_factory = furnace_factory or (lambda **kw: Furnace.attach(**kw))
        self._flow_factory = flow_factory or MFCFlowController
        self._operation_lock = threading.RLock()
        self._state_lock = self._io_lock = self._flow_io_lock = self._operation_lock
        self._furnace = self._flow_controller = None
        self._stop_event = threading.Event()
        self._thread = None
        self._closed = False
        self._locked = False
        self._fault = self._error = self._flow_error = self._persistence_error = None
        self._output_stopped = True
        self._steps = []
        self._sequence_running = False
        self._sequence_index = None
        self._sequence_phase = "idle"
        self._sequence_started_at = self._hold_started_at = self._hold_started_wall = None
        self._orphan_started_wall = None
        self._orphan_started_at = None
        self._active_target = self._active_ramp = None
        self._applied_signature = None
        self._end_gas = None
        self._end_gas_until_c = None
        self._gas_targets = {}
        self._last_persist_at = self.clock()
        self._notifications = []
        self.on_sequence_end = None
        self.events = EventLog(clock=wall_clock)
        self.lease = Lease(lease_seconds, clock, on_expire=self.on_lease_expired)
        self._snapshot = dict(
            sample_time=None,
            pv=None,
            sv=None,
            stored_sv=None,
            ramp=None,
            run_state_code=None,
            run_state="DISCONNECTED",
            auxiliary_output=None,
        )
        self._flow_channels = {
            ("gas", unit): dict(
                channel_index=channel,
                channel_number=channel + 1,
                unit_id=unit,
                gas_name=self.gas_map.get(channel, f"Gas {channel + 1}"),
                actual_percent=None,
                controller_status=None,
                target_percent=None,
                available=False,
                error=None,
                write_only=True,
                setpoint_verified=False,
            )
            for channel, unit in enumerate(GAS_UNIT_IDS)
        }
        self._gas_valves = {
            channel: dict(
                channel_index=channel, channel_number=channel + 1, open=None, verified=False
            )
            for channel in range(3)
        }

    @property
    def connected(self):
        return self._furnace is not None and self._furnace.connected

    @property
    def furnace(self):
        return self._furnace

    @property
    def flow_connected(self):
        return bool(self._flow_controller is not None and self._flow_controller.connected)

    @property
    def flow_controller(self):
        return self._flow_controller

    def start(self, *, background=True):
        """Attach without output writes, recover, then start optional polling."""
        with self._operation_lock:
            if self._closed:
                raise RuntimeError("The service is closed")
            self._connect()
            if background and self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, name="furnace-monitor", daemon=True
                )
                self._thread.start()
        return self.status()

    def _connect(self):
        if self._furnace is not None:
            if not self.connected:
                raise RuntimeError(
                    "Furnace connection was lost; restart and recover before control"
                )
            return
        self._furnace = self._furnace_factory(
            port=self.port, slave_address=self.slave_address, config=self.config
        )
        try:
            self._snapshot = self._read_snapshot(track=False)
            self._recover()
        except Exception as error:
            self._fail(error, idle=True)

    @_controlled
    def connect(self, port=None):
        if port is not None and port != self.port:
            raise ValueError("Set the furnace port before starting the service")
        self._connect()

    @_controlled
    def disconnect(self, stop_output=True):
        if not stop_output and (self._sequence_running or not self._output_stopped):
            raise ValueError("Stop active output before disconnecting the furnace")
        self._sequence_running = False
        self._sequence_phase = "stopped"
        self._hold_started_at = self._hold_started_wall = None
        self._orphan_started_at = self._orphan_started_wall = None
        errors = self._stop_outputs()
        if errors:
            self._fail(RuntimeError("; ".join(str(error) for error in errors)))
            return
        self._persist_or_fail()
        if self._furnace is not None:
            self._furnace.close(stop_output=False)
            self._furnace = None
        self._snapshot.update(sv=None, run_state_code=None, run_state="DISCONNECTED")

    def _ensure_flow_controller(self):
        if self._flow_controller is None:
            self.flow_port = serial_port(self.flow_port, "MG400_MFC_SERIAL")
            self._flow_controller = self._flow_factory(port=self.flow_port)
        if not self.flow_connected:
            raise RuntimeError("Flow controller connection was lost")
        return self._flow_controller

    @_controlled
    def connect_flow_controller(self, port=None):
        if port is not None:
            if self._flow_controller is not None and port != self.flow_port:
                raise ValueError("Disconnect the flow controller before changing ports")
            self.flow_port = serial_port(port, "MG400_MFC_SERIAL")
        self._ensure_flow_controller()
        self._persist_or_fail()
        return self.flow_status()

    def disconnect_flow_controller(self, stop_output=True):
        if not stop_output:
            raise ValueError("Flow output must stop before disconnect")
        with self._operation_lock:
            try:
                self._flow_off()
                self._persist()
            except Exception as error:
                self._fail(error)
            if self._flow_controller is not None:
                self._flow_controller.close(stop_output=False)
                self._flow_controller = None
        return self.flow_status()

    @staticmethod
    def _gas_map(value):
        result = {}
        for key, label in (value or {}).items():
            channel = MFCFlowController._gas_channel(key)
            if channel in result or not isinstance(label, str) or not label.strip():
                raise ValueError("Gas map needs distinct channels and nonempty labels")
            result[channel] = label.strip()
        return result

    def _require_gas_map(self, channels):
        if not self.gas_map or any(channel not in self.gas_map for channel in channels):
            raise ValueError("Set MG400_FURNACE_GAS_MAP for each requested gas channel")

    def _gas_plan(self, value, *, complete=False, require_map=False):
        if not isinstance(value, dict):
            raise ValueError("Gas plan must be a channel-to-percent object")
        result = {}
        for key, percent in value.items():
            channel = MFCFlowController._gas_channel(key)
            if channel in result:
                raise ValueError("Duplicate gas channel")
            result[channel] = self._number(percent, "Flow percentage", minimum=0, maximum=100)
        if require_map:
            self._require_gas_map(
                [key for key, value in result.items() if value > 0] if complete else result
            )
        self._require_gas_map([key for key, value in result.items() if value > 0]) if any(
            result.values()
        ) else None
        if complete:
            result = {channel: result.get(channel, 0.0) for channel in range(3)}
        return result

    def _read_flow(self, channel):
        unit = MFCFlowController.gas_unit_id(channel)
        snapshot = self._flow_channels[("gas", unit)]
        try:
            current = self._flow_controller.gas_channel_status(channel)
        except RuntimeError as error:
            if str(error) != f"MFC unit {unit} returned an empty response":
                raise
            snapshot.update(
                actual_percent=None,
                available=False,
                readback="unverified",
                error=None,
                write_only=True,
                setpoint_verified=False,
            )
        else:
            self._number(current["actual_percent"], "Flow readback", minimum=0, maximum=100)
            snapshot.update(current)
            snapshot.update(
                gas_name=self.gas_map.get(channel, snapshot["gas_name"]),
                readback="verified",
                setpoint_verified=False,
            )
        return snapshot

    def _zero_gas(self, channel):
        """Attempt zero and both closes without restoring a previous flow via SVN."""
        controller = self._ensure_flow_controller()
        unit = controller.gas_unit_id(channel)
        errors = []
        try:
            controller._send_without_response(unit, "SFR", "0")
            controller._controlled_unit_ids.add(unit)
        except Exception as error:
            errors.append(error)
        try:
            self._gas_valves[channel] = controller.set_gas_valve(channel, False)
        except Exception as error:
            self._gas_valves[channel].update(open=None, verified=False)
            errors.append(error)
        snapshot = self._flow_channels[("gas", unit)]
        snapshot.update(
            target_percent=0.0,
            actual_percent=None,
            available=False,
            setpoint_verified=False,
            readback="unverified",
        )
        self._gas_targets[channel] = 0.0
        if errors:
            raise RuntimeError("; ".join(str(error) for error in errors))
        self._read_flow(channel)

    def _set_gas_flow(self, channel, percent):
        if percent == 0:
            self._zero_gas(channel)
            return
        controller = self._ensure_flow_controller()
        unit = controller.gas_unit_id(channel)
        if unit in controller._valve_override_units:
            # A prior zero write may have failed while SVC still succeeded.
            # Prime the new explicit target before SVN can restore regulation.
            controller._send_without_response(unit, "SFR", f"{round(percent * 100):X}")
            controller._controlled_unit_ids.add(unit)
            controller._restore_normal_valve(unit)
        else:
            controller.set_flow_percent_unconfirmed(unit, percent)
        self._gas_valves[channel] = controller.set_gas_solenoid(channel, True)
        self._gas_targets[channel] = percent
        self._flow_channels[("gas", unit)].update(target_percent=percent, setpoint_verified=False)
        self._read_flow(channel)

    def _apply_gas(self, plan):
        if plan is None:
            return
        for channel, percent in plan.items():
            self._set_gas_flow(channel, percent)

    @_controlled
    def set_gas_flow(self, channel, flow_percent):
        channel = MFCFlowController._gas_channel(channel)
        percent = self._number(flow_percent, "Flow percentage", minimum=0, maximum=100)
        self._require_gas_map([channel])
        self._transition(lambda: self._set_gas_flow(channel, percent))
        return self.flow_status()

    @_controlled
    def set_gas_valve(self, channel, opened):
        channel = MFCFlowController._gas_channel(channel)
        if not isinstance(opened, bool):
            raise ValueError("opened must be true or false")
        self._require_gas_map([channel])
        self._transition(lambda: self._set_gas_flow(channel, 100.0 if opened else 0.0))
        return self.flow_status()

    @_controlled
    def inspect_gas_flow(self, channel):
        channel = MFCFlowController._gas_channel(channel)
        self._ensure_flow_controller()
        try:
            self._read_flow(channel)
        except Exception as error:
            self._fail(error)
            raise
        return self.flow_status()

    def stop_gas_flow(self, channel):
        channel = MFCFlowController._gas_channel(channel)
        with self._operation_lock:
            try:
                self._zero_gas(channel)
                self._persist()
            except Exception as error:
                self._fail(error)
        return self.flow_status()

    def _assert_flow_channel_kind(self, kind, unit_id):
        unit_id = MFCFlowController._unit_id(unit_id)
        other = "water" if kind == "gas" else "gas"
        if (other, unit_id) in self._flow_channels:
            raise ValueError(f"MFC unit {unit_id} is already assigned as {other}")
        return unit_id

    @_controlled
    def set_water_flow(self, unit_id, flow_percent):
        unit = self._assert_flow_channel_kind("water", unit_id)
        percent = self._number(flow_percent, "Flow percentage", minimum=0, maximum=100)
        controller = self._ensure_flow_controller()

        # The source driver checks WATER/H2O identity before the first write.
        def apply():
            target = controller.set_water_flow(unit, percent)
            self._flow_channels[("water", unit)] = dict(
                unit_id=unit,
                target_percent=target,
                actual_percent=None,
                available=False,
                error=None,
                setpoint_verified=False,
            )

        self._transition(apply)
        return self.flow_status()

    def stop_water_flow(self, unit_id):
        unit = self._assert_flow_channel_kind("water", unit_id)
        with self._operation_lock:
            try:
                self._ensure_flow_controller()._send_without_response(unit, "SFR", "0")
            except Exception as error:
                self._fail(error)
        return self.flow_status()

    def _flow_off(self):
        if self._flow_controller is None and self.flow_port is None:
            return
        errors = []
        for channel in range(3):
            try:
                self._zero_gas(channel)
            except Exception as error:
                errors.append(error)
        if self._flow_controller is not None:
            for kind, unit in list(self._flow_channels):
                if kind != "water":
                    continue
                try:
                    self._flow_controller._send_without_response(unit, "SFR", "0")
                except Exception as error:
                    errors.append(error)
        if errors:
            raise RuntimeError("; ".join(str(error) for error in errors))

    def stop_all_flow(self):
        with self._operation_lock:
            try:
                self._flow_off()
                self._persist()
            except Exception as error:
                self._fail(error)
        return self.flow_status()

    def flow_status(self):
        with self._operation_lock:
            return copy.deepcopy(
                dict(
                    connected=self.flow_connected,
                    port=self.flow_port,
                    error=str(self._flow_error or self._error)
                    if self._flow_error or self._error
                    else None,
                    locked=self._locked,
                    fault=self._fault,
                    gas_channels=[self._flow_channels[("gas", unit)] for unit in GAS_UNIT_IDS],
                    water_channels=[
                        value for (kind, _), value in self._flow_channels.items() if kind == "water"
                    ],
                    gas_valves=list(self._gas_valves.values()),
                )
            )

    def _validate_steps(self, steps):
        if not isinstance(steps, list) or not steps:
            raise ValueError("Sequence must contain at least one step")
        result = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                raise ValueError(f"Step {index} must be an object")
            item = dict(
                temperature_c=self._number(
                    step.get("temperature_c"),
                    f"step {index} temperature",
                    minimum=0,
                    maximum=self.config.max_c,
                ),
                ramp_rate_c_per_min=self._number(
                    step.get("ramp_rate_c_per_min"),
                    f"step {index} ramp rate",
                    exclusive_minimum=0,
                    maximum=self.config.max_ramp,
                ),
                hold_minutes=self._number(
                    step.get("hold_minutes", 0), f"step {index} hold time", minimum=0
                ),
            )
            if "gas" in step:
                item["gas"] = self._gas_plan(step["gas"], require_map=True)
            result.append(item)
        return result

    def _validate_recipe(self, steps, end_gas, end_gas_until_c):
        steps = self._validate_steps(steps)
        if any("gas" in step for step in steps) and (end_gas is None or end_gas_until_c is None):
            raise ValueError("A sequence with gas requires end_gas and end_gas_until_c")
        if (end_gas is None) != (end_gas_until_c is None):
            raise ValueError("Supply both end_gas and end_gas_until_c")
        if end_gas is not None:
            end_gas = self._gas_plan(end_gas, complete=True, require_map=True)
            end_gas_until_c = self._number(
                end_gas_until_c, "End gas temperature", minimum=0, maximum=self.config.max_c
            )
        return steps, end_gas, end_gas_until_c

    @_controlled
    def set_sequence(self, steps, *, end_gas=None, end_gas_until_c=None):
        validated, gas, until = self._validate_recipe(steps, end_gas, end_gas_until_c)
        if self._sequence_phase == "cooling":
            raise ValueError("Stop the cooling gas plan before replacing the sequence")
        self._steps, self._end_gas, self._end_gas_until_c = validated, gas, until
        if self._sequence_running and self._sequence_index >= len(validated):
            self._sequence_index = len(validated) - 1
        if self._sequence_running:
            self._apply_current_step()
        # Future steps and hold duration can change without a current setter.
        self._persist_or_fail()

    @_controlled
    def start_sequence(self, steps=None, *, end_gas=None, end_gas_until_c=None):
        if self._sequence_running or self._sequence_phase == "cooling":
            raise RuntimeError("A sequence is already running")
        recipe = self._validate_recipe(
            self._steps if steps is None else steps,
            self._end_gas if steps is None and end_gas is None else end_gas,
            self._end_gas_until_c if steps is None and end_gas_until_c is None else end_gas_until_c,
        )
        self._connect()
        self._check_unlocked()
        self._steps, self._end_gas, self._end_gas_until_c = recipe
        self._sequence_running = True
        self._sequence_index = 0
        self._sequence_phase = "ramping"
        self._hold_started_at = self._hold_started_wall = None
        self._orphan_started_at = self._orphan_started_wall = None
        self._sequence_started_at = self.wall_clock()
        self._applied_signature = None
        self._apply_current_step()

    @_controlled
    def apply_setpoint(self, temperature_c, ramp_rate_c_per_min):
        target = self._number(temperature_c, "Temperature", minimum=0, maximum=self.config.max_c)
        ramp = self._number(
            ramp_rate_c_per_min, "Ramp rate", exclusive_minimum=0, maximum=self.config.max_ramp
        )
        self._connect()
        self._check_unlocked()
        self._sequence_running = False
        self._sequence_phase = "manual"
        self._hold_started_at = self._hold_started_wall = None
        self._orphan_started_at = self._orphan_started_wall = None
        self._active_target, self._active_ramp = target, ramp

        def apply():
            self._furnace.set_ramp_rate(ramp)
            self._furnace.set_temperature(target)
            self._output_stopped = False

        self._transition(apply)
        self._snapshot = self._read_snapshot(track=False)

    def _signature(self):
        step = self._steps[self._sequence_index]
        return (
            self._sequence_index,
            step["temperature_c"],
            step["ramp_rate_c_per_min"],
            step.get("gas"),
        )

    def _apply_current_step(self):
        if not self._sequence_running:
            return
        signature = self._signature()
        if signature == self._applied_signature:
            return
        step = self._steps[self._sequence_index]
        thermal_changed = (
            self._applied_signature is None or signature[:3] != self._applied_signature[:3]
        )
        self._active_target = step["temperature_c"]
        self._active_ramp = step["ramp_rate_c_per_min"]
        if thermal_changed:
            self._sequence_phase = "ramping"
            self._hold_started_at = self._hold_started_wall = None

        def apply():
            self._apply_gas(step.get("gas"))
            if thermal_changed:
                self._furnace.set_ramp_rate(self._active_ramp)
                self._furnace.set_temperature(self._active_target)
            self._output_stopped = False
            self._applied_signature = copy.deepcopy(signature)

        self._transition(apply)
        self._snapshot = self._read_snapshot(track=False)

    def _advance_sequence(self, snapshot):
        if not self._sequence_running:
            return
        self._apply_current_step()
        step = self._steps[self._sequence_index]
        if self._sequence_phase == "holding":
            if self.clock() - self._hold_started_at < step["hold_minutes"] * 60:
                return
            if self._sequence_index + 1 < len(self._steps):
                self._sequence_index += 1
                self._apply_current_step()
            else:
                self._sequence_running = False
                self._sequence_phase = "cooling" if self._end_gas is not None else "complete"
                self._hold_started_at = self._hold_started_wall = None

                def finish():
                    self._furnace.set_stop()
                    self._mark_stopped()
                    self._apply_gas(self._end_gas)

                self._transition(finish)
                self._notifications.append("complete")
        elif (
            abs(snapshot["pv"] - step["temperature_c"]) <= self.target_tolerance_c
            and snapshot["run_state_code"] == 2
        ):
            self._hold_started_at, self._hold_started_wall = self.clock(), self.wall_clock()
            self._sequence_phase = "holding"
            self._persist_or_fail()

    def _transition(self, operation):
        phase = self._sequence_phase
        try:
            # A crash between intent and completion must never replay outputs.
            self._persist(phase="applying")
            operation()
            self._sequence_phase = phase
            self._persist()
        except Exception as error:
            self._fail(error)
            raise

    def _persist_or_fail(self):
        try:
            self._persist()
        except Exception as error:
            self._fail(error)
            raise

    def _identity(self):
        return dict(
            port=self.port,
            slave_address=self.slave_address,
            flow_port=self.flow_port,
            gas_map={str(key): value for key, value in self.gas_map.items()},
        )

    def _persist(self, *, phase=None):
        elapsed = (
            0 if self._hold_started_at is None else max(0, self.clock() - self._hold_started_at)
        )
        value = dict(
            version=1,
            identity=self._identity(),
            saved_at=self.wall_clock(),
            steps=self._steps,
            index=self._sequence_index,
            phase=phase or self._sequence_phase,
            running=self._sequence_running,
            sequence_started_at=self._sequence_started_at,
            hold_started_at=self._hold_started_wall,
            elapsed_s=elapsed,
            target_c=self._active_target,
            ramp_c_per_min=self._active_ramp,
            gas_plan=self._gas_targets,
            end_gas=self._end_gas,
            end_gas_until_c=self._end_gas_until_c,
            orphan_time=self._orphan_started_wall,
            locked=self._locked,
            fault=self._fault,
        )
        self.store.save(value)
        self._last_persist_at = self.clock()
        self._persistence_error = None

    def _recover(self):
        saved = self.store.load()
        state = self._snapshot["run_state_code"]
        if saved is not None and (
            saved.get("locked") or saved.get("phase") in ("fault", "applying")
        ):
            raise StateError("Saved state requires acknowledgement")
        if state == 1:
            # STOP never resumes a saved cooling plan or issues gas commands.
            self._sequence_phase = "idle"
            self._persist()
            return
        if state not in (0, 2) or not isinstance(saved, dict):
            raise StateError("Running furnace has no matching recovery state")
        required = {
            "version",
            "identity",
            "saved_at",
            "steps",
            "index",
            "phase",
            "running",
            "sequence_started_at",
            "hold_started_at",
            "elapsed_s",
            "target_c",
            "ramp_c_per_min",
            "gas_plan",
            "end_gas",
            "end_gas_until_c",
            "orphan_time",
            "locked",
            "fault",
        }
        if (
            not required <= saved.keys()
            or saved["version"] != 1
            or saved["identity"] != self._identity()
        ):
            raise StateError("Recovery state is incomplete or belongs to another configuration")
        if saved["locked"] is not False or saved["fault"] is not None:
            raise StateError("Saved fault requires acknowledgement")
        phase = saved["phase"]
        if phase not in ("ramping", "holding", "manual"):
            raise StateError("Saved state does not describe active heating")
        target = self._number(
            saved["target_c"], "Saved target", minimum=0, maximum=self.config.max_c
        )
        ramp = self._number(
            saved["ramp_c_per_min"], "Saved ramp", exclusive_minimum=0, maximum=self.config.max_ramp
        )
        if abs(self._snapshot["stored_sv"] - target) > 2:
            raise StateError("Controller target differs from saved target")
        self._furnace._check_run_limits()
        # The source program writes both targets identically. Check the active
        # bank without guessing register semantics for unsupported models.
        if self._furnace._program_bank_supported:
            _, _, scale = self._furnace.get_temperature_format()
            count = protocol.read_register(self._furnace.controller, protocol.PNO_REGISTER)
            for index in range(count):
                actual = (
                    protocol.read_register(
                        self._furnace.controller, protocol.PROGRAM_DATA_FIRST_REGISTER + 2 * index
                    )
                    / scale
                )
                if abs(actual - target) > 2:
                    raise StateError("Active program target differs from saved target")
        now = self.wall_clock()
        saved_at = self._number(saved["saved_at"], "Saved time", minimum=0)
        if saved_at > now:
            raise StateError("Recovery state has a future timestamp")
        elapsed = self._number(saved["elapsed_s"], "Saved elapsed time", minimum=0)
        hold_wall = saved["hold_started_at"]
        orphan_wall = saved["orphan_time"]
        for name, value in (
            ("Hold", hold_wall),
            ("Orphan", orphan_wall),
            ("Sequence", saved["sequence_started_at"]),
        ):
            if value is not None and self._number(value, name + " time", minimum=0) > saved_at:
                raise StateError(f"{name} time is after the state timestamp")
        if phase != "holding" and (hold_wall is not None or elapsed != 0):
            raise StateError("Only a holding phase can have a hold timer")
        if phase == "holding" and hold_wall is not None and elapsed > saved_at - hold_wall + 1:
            raise StateError("Saved hold duration exceeds its wall-clock interval")
        if phase != "manual" and orphan_wall is not None:
            raise StateError("A bounded sequence cannot have a manual orphan timer")
        if phase == "manual":
            if saved["running"] is not False or hold_wall is not None:
                raise StateError("Invalid manual recovery state")
            steps = self._validate_steps(saved["steps"]) if saved["steps"] else []
            end_gas, until = None, None
            index = saved["index"]
        else:
            if saved["running"] is not True:
                raise StateError("Invalid sequence recovery state")
            steps, end_gas, until = self._validate_recipe(
                saved["steps"], saved["end_gas"], saved["end_gas_until_c"]
            )
            index = saved["index"]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(steps):
                raise StateError("Invalid saved sequence index")
            if (
                steps[index]["temperature_c"] != target
                or steps[index]["ramp_rate_c_per_min"] != ramp
            ):
                raise StateError("Saved active step differs from its target")
            if phase == "holding" and (hold_wall is None or state != 2):
                raise StateError("Saved hold does not match controller HOLD")
        gas = self._gas_plan(saved["gas_plan"])
        self._steps, self._end_gas, self._end_gas_until_c = steps, end_gas, until
        self._sequence_index, self._sequence_phase = index, phase
        self._sequence_running = phase != "manual"
        self._sequence_started_at = saved["sequence_started_at"]
        self._active_target, self._active_ramp = target, ramp
        self._gas_targets = gas
        for channel, percent in gas.items():
            self._flow_channels[("gas", GAS_UNIT_IDS[channel])]["target_percent"] = percent
        if phase == "holding":
            self._hold_started_wall = hold_wall
            self._hold_started_at = self.clock() - max(elapsed + now - saved_at, now - hold_wall)
        if phase == "manual":
            self._orphan_started_wall = now if orphan_wall is None else orphan_wall
            self._orphan_started_at = self.clock() - (now - self._orphan_started_wall)
        if self._sequence_running:
            self._applied_signature = copy.deepcopy(self._signature())
        if state == 0:
            self._furnace.pending_hold_target = target
        self._output_stopped = False
        self.events.add("info", "Resumed furnace state", phase=phase, target_c=target)
        self._persist()

    def _check_unlocked(self):
        if self._locked:
            raise ServiceError(409, "Furnace recovery requires acknowledgement")

    def _fail(self, error, *, idle=False):
        self._locked = True
        self._error = error
        self._fault = dict(code="unknown_outcome", message=str(error))
        self._sequence_running = False
        self._sequence_phase = "fault"
        self._hold_started_at = self._hold_started_wall = None
        self.events.add("error", "Furnace locked; acknowledgement required", error=str(error))
        try:
            self._persist()
        except Exception as persistence_error:
            self._persistence_error = str(persistence_error)
            self.events.add("error", "Cannot persist furnace fault", error=str(persistence_error))
            try:
                self.store.invalidate_resumable()
            except Exception as invalidation_error:
                self.events.add(
                    "error", "Cannot invalidate stale recovery state", error=str(invalidation_error)
                )
        # Never remove a durable fault or retry a failed output automatically.
        errors = self._stop_outputs(idle=idle)
        if errors:
            self._fault["message"] += "; stop: " + "; ".join(str(item) for item in errors)
        try:
            self._persist()
        except Exception as persistence_error:
            self._persistence_error = str(persistence_error)

    def _stop_outputs(self, *, gas=True, idle=False):
        errors = []
        if self._furnace is None and self.port is not None:
            try:
                self._furnace = self._furnace_factory(
                    port=self.port, slave_address=self.slave_address, config=self.config
                )
            except Exception as error:
                errors.append(error)
        if self._furnace is not None:
            try:
                self._furnace.set_stop()
                self._mark_stopped()
            except Exception as error:
                errors.append(error)
        if gas:
            gas_stopped = False
            try:
                self._flow_off()
                gas_stopped = True
            except Exception as error:
                errors.append(error)
            if idle and gas_stopped and any(self.idle_gas.values()):
                try:
                    self._apply_gas(self.idle_gas)
                except Exception as error:
                    errors.append(error)
                    # A partial idle plan must not leave its earlier channels on.
                    try:
                        self._flow_off()
                    except Exception as cleanup_error:
                        errors.append(cleanup_error)
        return errors

    def stop_furnace(self):
        return self.stop_sequence()

    def stop_sequence(self, stop_furnace=True):
        if not stop_furnace:
            raise ValueError("Stopping sequence tracking must also stop output")
        with self._operation_lock:
            was_running = self._sequence_running
            self._sequence_running = False
            self._sequence_phase = "fault" if self._locked else "stopped"
            self._hold_started_at = self._hold_started_wall = None
            self._orphan_started_at = self._orphan_started_wall = None
            errors = self._stop_outputs()
            if errors:
                self._fail(RuntimeError("; ".join(str(error) for error in errors)))
            else:
                try:
                    self._persist()
                except Exception as error:
                    self._fail(error)
            if was_running:
                self._notifications.append("stopped")
        self._notify_pending()
        return self.status()

    def acknowledge(self, *, client):
        with self.lease.control(client):
            with self._operation_lock:
                if not self.connected or self._furnace.get_run_state()[0] != 1:
                    raise ServiceError(409, "Verify controller STOP before acknowledgement")
                self._locked = False
                self._fault = self._error = None
                self._sequence_phase = "idle"
                self._sequence_running = False
                try:
                    self._persist()
                except Exception as error:
                    self._fail(error)
                    raise
                self.events.add("info", "Furnace fault acknowledged", client=client)
        return self.status()

    def on_lease_expired(self):
        with self._operation_lock:
            if (
                self._sequence_phase == "manual"
                and not self._output_stopped
                and self._orphan_started_at is None
            ):
                self._orphan_started_at, self._orphan_started_wall = self.clock(), self.wall_clock()
                try:
                    self._persist()
                except Exception as error:
                    # Keep lease acquisition available for explicit fault acknowledgement.
                    self._fail(error)
                self.events.add("warning", "Open-ended furnace setpoint lost its client")

    def poll_once(self):
        try:
            self.lease.poll()
        except Exception as error:
            with self._operation_lock:
                self._fail(error)
            return self.status()
        lease = self.lease.snapshot()
        with self._operation_lock:
            if self._closed or not self.connected or self._locked:
                return self._status_locked(lease)
            try:
                if self._sequence_phase == "manual" and not self._output_stopped:
                    if lease["holder"] is None and self._orphan_started_at is None:
                        self._orphan_started_at, self._orphan_started_wall = (
                            self.clock(),
                            self.wall_clock(),
                        )
                        self._persist()
                    elif lease["holder"] is not None and self._orphan_started_at is not None:
                        self._orphan_started_at = self._orphan_started_wall = None
                        self._persist()
                    if (
                        self._orphan_started_at is not None
                        and self.clock() - self._orphan_started_at >= self.orphan_grace_min * 60
                    ):
                        self._sequence_phase = "stopped"

                        def grace_stop():
                            errors = self._stop_outputs(idle=True)
                            if errors:
                                raise RuntimeError("; ".join(str(error) for error in errors))

                        self._transition(grace_stop)
                self._snapshot = self._read_snapshot(track=True)
                if self._sequence_running and self._snapshot["run_state_code"] == 1:
                    raise RuntimeError("Controller stopped before the planned sequence end")
                self._advance_sequence(self._snapshot)
                if (
                    self._sequence_phase == "cooling"
                    and self._snapshot["pv"] < self._end_gas_until_c
                ):
                    self._sequence_phase = "complete"
                    self._transition(lambda: self._apply_gas(self.idle_gas))
                if self._sequence_phase == "holding" and self.clock() - self._last_persist_at >= 60:
                    self._persist()
            except Exception as error:
                if not self._locked:
                    self._fail(error)
        self._notify_pending()
        return self.status()

    def _loop(self):
        while not self._stop_event.wait(self.poll_interval):
            self.poll_once()

    def close(self):
        """SIGTERM policy: stop heat and gas independently, then close transports."""
        self._stop_event.set()
        lease = self.lease.snapshot()
        # Publish the terminal gate before stop releases its lock or callbacks.
        with self._operation_lock:
            if self._closed:
                return self._status_locked(lease)
            self._closed = True
        self.stop_sequence()
        with self._operation_lock:
            for device in (self._furnace, self._flow_controller):
                if device is not None:
                    try:
                        device.close(stop_output=False)
                    except Exception as error:
                        self._error = error
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(2, self.poll_interval + 1))
        return self.status()

    def _read_snapshot(self, *, track=True):
        values = self._furnace.get_current_values()
        pv = self._number(values["PV"], "Controller PV")
        if track:
            if (
                self._furnace.pending_hold_target is not None
                and self._furnace.get_program_step() == 2
            ):
                self._transition(
                    lambda: self._furnace.update_temperature_change(
                        current_pv=pv, tolerance_c=self.target_tolerance_c
                    )
                )
        state, name = self._furnace.get_run_state()
        output_mode, output_name = self._furnace.get_output_mode()
        try:
            auxiliary = self._furnace.get_auxiliary_output_status()
        except Exception as error:
            auxiliary = {"error": str(error)}
        self._output_stopped = state == 1
        stored_sv = self._number(values["SV"], "Controller SV")
        return dict(
            sample_time=self.wall_clock(),
            pv=pv,
            sv=None if state == 1 else stored_sv,
            stored_sv=stored_sv,
            ramp=self._number(values["Ramp"], "Controller ramp"),
            run_state_code=state,
            run_state=name,
            output_mode_code=output_mode,
            output_mode=output_name,
            auxiliary_output=auxiliary,
        )

    def _mark_stopped(self):
        self._output_stopped = True
        self._snapshot.update(sv=None, run_state_code=1, run_state="STOP/COOLING")

    def _notify_pending(self):
        with self._operation_lock:
            notifications, self._notifications = self._notifications, []
        if self.on_sequence_end is not None:
            for reason in notifications:
                try:
                    self.on_sequence_end(reason)
                except Exception as error:
                    self.events.add("error", "Sequence callback failed", error=str(error))

    def status(self):
        lease = self.lease.snapshot()
        with self._operation_lock:
            return self._status_locked(lease)

    def _status_locked(self, lease):
        remaining = None
        if self._sequence_phase == "holding" and self._hold_started_at is not None:
            remaining = max(
                0,
                self._steps[self._sequence_index]["hold_minutes"] * 60
                - (self.clock() - self._hold_started_at),
            )
        return copy.deepcopy(
            dict(
                connected=self.connected,
                locked=self._locked,
                fault=self._fault,
                error=str(self._error) if self._error else None,
                persistence_error=self._persistence_error,
                lease=lease,
                orphan_time=self._orphan_started_wall,
                instrument_model=getattr(self._furnace, "instrument_model", None),
                internal_program_steps=getattr(self._furnace, "program_steps_before_disable", None),
                program_mode_disabled=getattr(self._furnace, "program_mode_disabled", False),
                program_steps_before_disable=getattr(
                    self._furnace, "program_steps_before_disable", None
                ),
                program_setpoint_count=len(
                    getattr(self._furnace, "program_setpoint_registers", ())
                ),
                controller_program_step=getattr(self._furnace, "controller_program_step", None),
                waiting_for_program_step_2=getattr(self._furnace, "pending_hold_target", None)
                is not None,
                control_mode=self._control_mode(),
                flow=self.flow_status(),
                **self._snapshot,
                sequence=dict(
                    running=self._sequence_running,
                    steps=self._steps,
                    current_index=self._sequence_index,
                    phase=self._sequence_phase,
                    hold_remaining_seconds=remaining,
                    started_at=self._sequence_started_at,
                    end_gas=self._end_gas,
                    end_gas_until_c=self._end_gas_until_c,
                ),
            )
        )

    def _control_mode(self):
        if not self.connected:
            return "DISCONNECTED"
        return {0: "RUN", 1: "STOPPED", 2: "HOLD"}.get(self._snapshot["run_state_code"], "UNKNOWN")

    def frame_metadata(self):
        """Return the source recording metadata keys and commanded percentages."""
        status = self.status()
        sequence = status["sequence"]
        targets = {f"gas_{channel + 1}_target_percent": None for channel in range(3)}
        for channel in status["flow"]["gas_channels"]:
            targets[f"gas_{channel['channel_index'] + 1}_target_percent"] = channel.get(
                "target_percent"
            )
        return dict(
            temperature_sample_time=status["sample_time"],
            temperature_c=status["pv"],
            target_c=status["sv"],
            ramp_rate_c_per_min=status["ramp"],
            furnace_state=status["run_state"],
            sequence_running=sequence["running"],
            sequence_step=None
            if sequence["current_index"] is None
            else sequence["current_index"] + 1,
            sequence_phase=sequence["phase"],
            **targets,
        )

    @staticmethod
    def _number(value, name, minimum=None, exclusive_minimum=None, maximum=None):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"{name} must be a number") from error
        if isinstance(value, bool) or not math.isfinite(number):
            raise ValueError(f"{name} must be finite")
        if minimum is not None and number < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        if exclusive_minimum is not None and number <= exclusive_minimum:
            raise ValueError(f"{name} must be greater than {exclusive_minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"{name} must be at most {maximum}")
        return number
