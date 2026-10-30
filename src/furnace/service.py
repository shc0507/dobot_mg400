"""Standalone furnace control, monitoring, and sequence library."""

import math
import queue
import threading
import time
import minimalmodbus
import serial


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


class FurnaceService:
    """Thread-safe monitoring and editable sequences around one Furnace object."""

    def __init__(self, poll_interval=1.0, target_tolerance_c=1.0,
                 port=PORT, slave_address=SLAVE_ADDRESS, flow_port=FLOW_PORT):
        self.poll_interval = poll_interval
        self.target_tolerance_c = target_tolerance_c
        # The web app and runtime should always use the configured COM ports
        # defined in this module rather than accepting per-request overrides.
        self.port = PORT
        self.slave_address = slave_address
        self.flow_port = FLOW_PORT
        self._furnace = None
        self._flow_controller = None
        self._output_stopped = False
        # Furnace methods perform serial I/O, so only one thread may call them at a time.
        self._io_lock = threading.Lock()
        self._flow_io_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._error = None
        self._flow_error = None
        self._flow_channels = {
            ("gas", unit_id): {
                "channel_index": channel,
                "channel_number": channel + 1,
                "unit_id": unit_id,
                "gas_name": f"Gas {channel + 1}",
                "actual_percent": None,
                "controller_status": None,
                "target_percent": None,
                "available": False,
                "error": "Not connected",
                "write_only": True,
            }
            for channel, unit_id in enumerate(GAS_UNIT_IDS)
        }
        self._gas_valves = {
            channel: {
                "channel_index": channel,
                "channel_number": channel + 1,
                "open": None,
                "verified": None,
            }
            for channel in range(GAS_CHANNEL_COUNT)
        }
        self._snapshot = {
            "sample_time": None,
            "pv": None,
            "sv": None,
            "stored_sv": None,
            "ramp": None,
            "run_state_code": None,
            "run_state": "DISCONNECTED",
            "auxiliary_output": None,
        }
        self._steps = []
        self._sequence_running = False
        self._sequence_index = None
        self._sequence_phase = "idle"
        self._hold_started_at = None
        self._sequence_started_at = None
        self._applied_signature = None
        self.on_sequence_end = None
        self._thread = threading.Thread(
            target=self._loop, name="furnace-monitor", daemon=True
        )
        self._thread.start()

    @property
    def connected(self):
        """Return whether this service owns a connected Furnace object."""
        return self._furnace is not None and self._furnace.connected

    @property
    def furnace(self):
        """Expose the managed Furnace instance for advanced library callers."""
        return self._furnace

    @property
    def flow_connected(self):
        """Return whether the enclosure's RS-485 connection is open."""
        return bool(
            self._flow_controller is not None
            and self._flow_controller.connected
        )

    @property
    def flow_controller(self):
        """Expose the managed MFC flow driver for advanced callers."""
        return self._flow_controller

    def connect(self, port=None):
        """Create the Furnace object using the fixed controller port from this module."""
        resolved_port = PORT
        self.port = resolved_port
        with self._io_lock:
            if self._furnace is None:
                self._furnace = Furnace(
                    port=resolved_port,
                    slave_address=self.slave_address,
                )
                # Furnace initialization deliberately leaves output in STOP/COOLING.
                self._output_stopped = True
        with self._state_lock:
            self._error = None
        return True

    def disconnect(self, stop_output=False):
        """End sequence state, optionally stop heating, and close the serial link."""
        self.stop_sequence(stop_furnace=stop_output)
        with self._io_lock:
            furnace, self._furnace = self._furnace, None
            if furnace is not None:
                # Closing a port is not a controller command; all commands use Furnace methods.
                furnace.close(stop_output=False)
        with self._state_lock:
            self._snapshot["sv"] = None
            self._snapshot["run_state_code"] = None
            self._snapshot["run_state"] = "DISCONNECTED"

    def connect_flow_controller(self, port=None):
        """Open the fixed enclosure flow port from this module."""
        requested_port = FLOW_PORT
        self.flow_port = requested_port
        if not requested_port:
            raise ValueError("Flow-controller serial port is required")
        with self._flow_io_lock:
            if self._flow_controller is not None:
                if self._flow_controller.port != requested_port:
                    raise RuntimeError(
                        "Disconnect the current flow controller before changing ports"
                    )
                return self.flow_status()
            controller = MFCFlowController(port=requested_port)
            self._flow_controller = controller
            self.flow_port = requested_port
            snapshots = {}
            for channel, unit_id in enumerate(GAS_UNIT_IDS):
                snapshot = {
                    "channel_index": channel,
                    "channel_number": channel + 1,
                    "unit_id": unit_id,
                    "gas_name": f"Gas {channel + 1}",
                    "actual_percent": None,
                    "controller_status": None,
                    "target_percent": None,
                    "available": False,
                    "error": None,
                    "write_only": True,
                }
                snapshots[("gas", unit_id)] = snapshot
        with self._state_lock:
            self._flow_channels.update(snapshots)
            # An open serial port is a successful connection. Silent MFCs are
            # reported per channel and retried by the monitor thread.
            self._flow_error = None
        return self.flow_status()

    def disconnect_flow_controller(self, stop_output=True):
        """Stop every used channel and close the enclosure serial port."""
        with self._flow_io_lock:
            controller, self._flow_controller = self._flow_controller, None
            if controller is not None:
                controller.close(stop_output=stop_output)
        return self.flow_status()

    def _ensure_flow_controller(self):
        if not self.flow_connected:
            self.connect_flow_controller()
        return self._flow_controller

    def _assert_flow_channel_kind(self, kind, unit_id):
        unit_id = MFCFlowController._unit_id(unit_id)
        other_kind = "water" if kind == "gas" else "gas"
        with self._state_lock:
            if (other_kind, unit_id) in self._flow_channels:
                raise ValueError(
                    f"MFC unit {unit_id} is already assigned as {other_kind}"
                )
        return unit_id

    def set_gas_flow(self, channel, flow_percent):
        """Set one fixed gas path to a percentage of its full-scale flow."""
        channel = MFCFlowController._gas_channel(channel)
        unit_id = MFCFlowController.gas_unit_id(channel)
        percent = MFCFlowController._finite_number(
            flow_percent, "Flow percentage", minimum=0, maximum=100
        )
        controller = self._ensure_flow_controller()
        with self._state_lock:
            previous = dict(self._flow_channels[("gas", unit_id)])
        with self._flow_io_lock:
            if percent == 0:
                valve = controller.set_gas_solenoid(channel, False)
                target = controller.set_flow_percent_unconfirmed(unit_id, percent)
            else:
                target = controller.set_flow_percent_unconfirmed(unit_id, percent)
                valve = controller.set_gas_solenoid(channel, True)
        snapshot = previous
        snapshot.update({
            "kind": "gas",
            "target_percent": target,
            "setpoint_verified": False,
        })
        with self._state_lock:
            self._flow_channels[("gas", unit_id)] = snapshot
            self._gas_valves[channel] = dict(valve)
            self._flow_error = None
        return self.flow_status()

    def inspect_gas_flow(self, channel):
        """Read one gas MFC without changing its mode, valve, or setpoint."""
        channel = MFCFlowController._gas_channel(channel)
        unit_id = MFCFlowController.gas_unit_id(channel)
        controller = self._ensure_flow_controller()
        with self._state_lock:
            previous = dict(self._flow_channels[("gas", unit_id)])
        with self._flow_io_lock:
            try:
                snapshot = controller.gas_channel_status(channel)
            except RuntimeError as exc:
                # A silent write-only MFC cannot confirm flow, but that does
                # not mean the port or previously sent setpoint failed.
                snapshot = previous
                snapshot.update({"available": False, "actual_percent": None,
                         "error": str(exc)})
        snapshot.update({"kind": "gas", "target_percent": previous.get("target_percent"),
                         "setpoint_verified": previous.get("setpoint_verified", False)})
        with self._state_lock:
            self._flow_channels[("gas", snapshot["unit_id"])] = snapshot
            self._flow_error = None
        return self.flow_status()

    def set_gas_valve(self, channel, opened):
        """Request a 100% full-scale setpoint on open, or close both valves."""
        channel = MFCFlowController._gas_channel(channel)
        if not isinstance(opened, bool):
            raise ValueError("opened must be true or false")
        if opened:
            return self.set_gas_flow(channel, 100.0)
        controller = self._ensure_flow_controller()
        with self._flow_io_lock:
            result = controller.set_gas_valve(channel, opened)
        with self._state_lock:
            self._gas_valves[result["channel_index"]] = dict(result)
            self._flow_error = None
        return self.flow_status()

    def stop_gas_flow(self, channel):
        """Close both valves, then zero the MFC setpoint."""
        channel = MFCFlowController._gas_channel(channel)
        unit_id = MFCFlowController.gas_unit_id(channel)
        controller = self._ensure_flow_controller()
        with self._flow_io_lock:
            valve = controller.set_gas_valve(channel, False)
            controller.stop_flow_unconfirmed(unit_id)
        with self._state_lock:
            snapshot = dict(self._flow_channels[("gas", unit_id)])
        snapshot.update({
            "kind": "gas",
            "target_percent": 0.0,
            "actual_percent": 0.0,
            "setpoint_verified": False,
        })
        with self._state_lock:
            self._flow_channels[("gas", unit_id)] = snapshot
            self._gas_valves[channel] = dict(valve)
            self._flow_error = None
        return self.flow_status()

    def set_water_flow(self, unit_id, percent):
        """Set a user-identified MFCW-compatible water channel by percentage."""
        unit_id = self._assert_flow_channel_kind("water", unit_id)
        controller = self._ensure_flow_controller()
        with self._flow_io_lock:
            target = controller.set_water_flow(unit_id, percent)
            snapshot = controller.channel_status(unit_id)
        snapshot.update({"kind": "water", "target_percent": target})
        with self._state_lock:
            self._flow_channels[("water", snapshot["unit_id"])] = snapshot
            self._flow_error = None
        return self.flow_status()

    def stop_water_flow(self, unit_id):
        """Zero a user-identified MFCW-compatible water channel."""
        unit_id = self._assert_flow_channel_kind("water", unit_id)
        controller = self._ensure_flow_controller()
        with self._flow_io_lock:
            controller.stop_flow(unit_id)
            snapshot = controller.channel_status(unit_id)
        snapshot.update({"kind": "water", "target_percent": 0.0})
        with self._state_lock:
            self._flow_channels[("water", snapshot["unit_id"])] = snapshot
            self._flow_error = None
        return self.flow_status()

    def stop_all_flow(self):
        """Zero and close all channels used through this service."""
        if not self.flow_connected:
            return self.flow_status()
        with self._state_lock:
            channels = list(self._flow_channels.items())
        errors = []
        with self._flow_io_lock:
            for (kind, unit_id), snapshot in channels:
                if kind == "gas":
                    continue
                try:
                    self._flow_controller.stop_flow_unconfirmed(unit_id)
                    snapshot = dict(snapshot)
                    if kind == "gas":
                        snapshot["target_percent"] = 0.0
                    else:
                        snapshot["target_percent"] = 0.0
                    snapshot["actual_percent"] = 0.0
                    with self._state_lock:
                        self._flow_channels[(kind, unit_id)] = snapshot
                except Exception as exc:
                    errors.append(f"unit {unit_id}: {exc}")
            for channel in range(GAS_CHANNEL_COUNT):
                try:
                    result = self._flow_controller.set_gas_valve(
                        channel, False
                    )
                    with self._state_lock:
                        self._gas_valves[channel] = dict(result)
                        gas = dict(self._flow_channels[("gas", GAS_UNIT_IDS[channel])])
                        gas.update({
                            "target_percent": 0.0,
                            "actual_percent": 0.0,
                        })
                        self._flow_channels[("gas", GAS_UNIT_IDS[channel])] = gas
                except Exception as exc:
                    errors.append(f"gas channel {channel + 1}: {exc}")
            # Only zero MFC setpoints that this process actually changed.
            # Closing the known-working external solenoids must not depend on
            # the currently silent ASCII readback path.
            for unit_id in sorted(self._flow_controller._controlled_unit_ids):
                try:
                    self._flow_controller.stop_flow(unit_id)
                    with self._state_lock:
                        gas = dict(self._flow_channels[("gas", unit_id)])
                        gas["target_percent"] = 0.0
                        self._flow_channels[("gas", unit_id)] = gas
                except Exception as exc:
                    errors.append(f"MFC unit {unit_id}: {exc}")
        if errors:
            error = RuntimeError("; ".join(errors))
            with self._state_lock:
                self._flow_error = error
            raise error
        return self.flow_status()

    def flow_status(self):
        """Return JSON-friendly connection and per-channel flow state."""
        with self._state_lock:
            gas = [
                dict(value)
                for (kind, _), value in self._flow_channels.items()
                if kind == "gas"
            ]
            water = [
                dict(value)
                for (kind, _), value in self._flow_channels.items()
                if kind == "water"
            ]
            gas.sort(key=lambda item: item["unit_id"])
            water.sort(key=lambda item: item["unit_id"])
            warnings = [
                f"Gas {item['channel_number']}: {item['error']}"
                for item in gas
                if item.get("error")
            ]
            return {
                "connected": self.flow_connected,
                "port": self.flow_port,
                "error": str(self._flow_error) if self._flow_error else None,
                "warning": "; ".join(warnings) if warnings else None,
                "write_only": self.flow_connected and any(
                    item.get("write_only", False) for item in gas
                ),
                "gas_channels": gas,
                "water_channels": water,
                "gas_valves": [
                    dict(self._gas_valves[channel])
                    for channel in range(GAS_CHANNEL_COUNT)
                ],
                "gas_unit_ids": list(GAS_UNIT_IDS),
            }

    def close(self):
        """Stop monitoring and safely close heat and flow outputs."""
        self._stop_event.set()
        self.disconnect_flow_controller(stop_output=True)
        self.disconnect(stop_output=True)
        self._thread.join(timeout=2.0)

    def apply_setpoint(self, temperature_c, ramp_rate_c_per_min):
        """Apply one manual ramp and target through Furnace's public setters."""
        temperature = self._number(temperature_c, "temperature", minimum=0)
        ramp = self._number(
            ramp_rate_c_per_min, "ramp rate", exclusive_minimum=0
        )
        self.stop_sequence(stop_furnace=False)
        self.connect()
        with self._io_lock:
            self._furnace.set_ramp_rate(ramp)
            # set_temperature() starts RUN; the monitor holds at target step 2.
            self._furnace.set_temperature(temperature)
            self._output_stopped = False
        # Start cooler circulation immediately; the monitor will retry if
        # COM5 is temporarily unavailable.
        return self.status()

    def stop_furnace(self):
        """Stop heating through Furnace without closing its serial connection."""
        self.stop_sequence(stop_furnace=False)
        self.connect()
        with self._io_lock:
            self._furnace.set_stop()
            self._output_stopped = True
        self._mark_stopped()
        return self.status()

    def set_sequence(self, steps):
        """Validate and store a sequence, including edits made during a run."""
        validated = self._validate_steps(steps)
        with self._state_lock:
            self._steps = validated
            if self._sequence_running and self._sequence_index >= len(validated):
                self._sequence_index = len(validated) - 1
                self._hold_started_at = None
                self._applied_signature = None
                self._sequence_phase = "ramping"
        return self.status()

    def start_sequence(self, steps=None):
        """Start target step 1, then HOLD when target step 2 begins."""
        if steps is not None:
            self.set_sequence(steps)
        with self._state_lock:
            if not self._steps:
                raise ValueError("Add at least one sequence step")
            if self._sequence_running:
                raise RuntimeError("A sequence is already running")
        self.connect()
        with self._state_lock:
            self._sequence_running = True
            self._sequence_index = 0
            self._sequence_phase = "ramping"
            self._hold_started_at = None
            self._sequence_started_at = time.time()
            self._applied_signature = None
        # Applying the first target starts the controller's two-target program.
        self._apply_current_step()
        with self._io_lock:
            self._output_stopped = False
        return self.status()

    def stop_sequence(self, stop_furnace=True):
        """End sequence tracking and optionally stop physical furnace output."""
        with self._state_lock:
            was_running = self._sequence_running
            self._sequence_running = False
            self._sequence_phase = "stopped" if was_running else "idle"
            self._hold_started_at = None
            self._applied_signature = None
        if stop_furnace and self._furnace is not None:
            with self._io_lock:
                self._furnace.set_stop()
                self._output_stopped = True
            self._mark_stopped()
        if was_running:
            self._notify_end("stopped")
        return self.status()
    def status(self):
        """Return one consistent dictionary of hardware and sequence state."""
        with self._state_lock:
            hold_remaining = None
            if (
                self._sequence_running
                and self._sequence_phase == "holding"
                and self._hold_started_at is not None
            ):
                hold_seconds = self._steps[self._sequence_index]["hold_minutes"] * 60
                hold_remaining = max(
                    0, hold_seconds - (time.monotonic() - self._hold_started_at)
                )
            return {
                "connected": self.connected,
                "error": str(self._error) if self._error else None,
                "instrument_model": getattr(
                    self._furnace, "instrument_model", None
                ),
                "internal_program_steps": (
                    0
                    if getattr(self._furnace, "program_mode_disabled", False)
                    else getattr(self._furnace, "program_steps_before_disable", None)
                ),
                "program_mode_disabled": getattr(
                    self._furnace, "program_mode_disabled", False
                ),
                "program_steps_before_disable": getattr(
                    self._furnace, "program_steps_before_disable", None
                ),
                "program_setpoint_count": len(
                    getattr(self._furnace, "program_setpoint_registers", ())
                ),
                "controller_program_step": getattr(
                    self._furnace, "controller_program_step", None
                ),
                "waiting_for_program_step_2": (
                    getattr(self._furnace, "pending_hold_target", None) is not None
                ),
                "control_mode": self._control_mode(),
                "flow": self.flow_status(),
                **self._snapshot,
                "sequence": {
                    "running": self._sequence_running,
                    "steps": [dict(step) for step in self._steps],
                    "current_index": self._sequence_index,
                    "phase": self._sequence_phase,
                    "hold_remaining_seconds": hold_remaining,
                    "started_at": self._sequence_started_at,
                },
            }

    def _control_mode(self):
        """Describe how the library is currently controlling furnace output."""
        if not self.connected:
            return "DISCONNECTED"
        state = self._snapshot.get("run_state_code")
        if state == 1:
            return "STOPPED"

        if state == 2:
            return "HOLD"
        if state == 0:
            return "RUN"
        return "UNKNOWN"

    def frame_metadata(self):
        """Return furnace, sequence, and commanded gas values saved beside a video frame."""
        status = self.status()
        sequence = status["sequence"]
        gas_channels = status.get("flow", {}).get("gas_channels", [])
        flow_targets = {
            f"gas_{channel_index + 1}_target_percent": None
            for channel_index in range(3)
        }
        for channel in gas_channels:
            channel_index = channel.get("channel_index")
            if channel_index is not None and 0 <= channel_index < 3:
                flow_targets[f"gas_{channel_index + 1}_target_percent"] = channel.get(
                    "target_percent"
                )
        return {
            "temperature_sample_time": status["sample_time"],
            "temperature_c": status["pv"],
            "target_c": status["sv"],
            "ramp_rate_c_per_min": status["ramp"],
            "furnace_state": status["run_state"],
            "sequence_running": sequence["running"],
            "sequence_step": (
                None
                if sequence["current_index"] is None
                else sequence["current_index"] + 1
            ),
            "sequence_phase": sequence["phase"],
            **flow_targets,
        }

    def _loop(self):
        """Poll values and advance sequence timers until service shutdown."""
        while not self._stop_event.wait(self.poll_interval):
            if self._furnace is not None:
                try:
                    snapshot = self._read_snapshot()
                    with self._state_lock:
                        self._snapshot = snapshot
                        self._error = None
                    self._advance_sequence(snapshot)
                except Exception as exc:
                    with self._state_lock:
                        self._error = exc
            if self._flow_controller is not None:
                try:
                    self._poll_flow_channels()
                    with self._state_lock:
                        self._flow_error = None
                except Exception as exc:
                    with self._state_lock:
                        self._flow_error = exc

    def _poll_flow_channels(self):
        """Refresh fixed gas readings and any explicitly used extra channel."""
        with self._state_lock:
            channels = list(self._flow_channels.items())
        if not channels:
            return
        updates = {}
        with self._flow_io_lock:
            controller = self._flow_controller
            if controller is None or not controller.connected:
                return
            for key, previous in channels:
                if previous.get("write_only"):
                    continue
                try:
                    if key[0] == "gas":
                        current = controller.gas_channel_status(
                            previous["channel_index"]
                        )
                        current["target_percent"] = previous.get("target_percent")
                    else:
                        percent = controller.read_flow_percent(key[1])
                        current = dict(previous)
                        current["actual_percent"] = percent
                        current["available"] = True
                        current["error"] = None
                except Exception as exc:
                    current = dict(previous)
                    current["available"] = False
                    current["error"] = str(exc)
                updates[key] = current
        with self._state_lock:
            self._flow_channels.update(updates)

    def _read_snapshot(self):
        """Read values exclusively through Furnace's getter methods."""
        with self._io_lock:
            values = self._furnace.get_current_values()
            pv = float(values["PV"])
            # A pending ramp remains in RUN until the controller enters step 2.
            # This is non-blocking and may issue HOLD during this monitor poll.
            self._furnace.update_temperature_change(
                current_pv=pv, tolerance_c=self.target_tolerance_c
            )
            state, state_name = self._furnace.get_run_state()
            output_mode, output_mode_name = self._furnace.get_output_mode()
            try:
                auxiliary = self._furnace.get_auxiliary_output_status()
            except Exception as exc:
                auxiliary = {"error": str(exc)}
            self._output_stopped = state == 1
        stored_sv = float(values["SV"])
        ramp = float(values["Ramp"])
        return {
            "sample_time": float(values.get("time", time.time())),
            "pv": pv,
            "sv": None if state == 1 else stored_sv,
            "stored_sv": stored_sv,
            "ramp": ramp,
            "run_state_code": state,
            "run_state": state_name,
            "auxiliary_output": auxiliary,
        }

    def _advance_sequence(self, snapshot):
        """Advance timers without sending commands while the current step holds."""
        with self._state_lock:
            if not self._sequence_running:
                return

        # This is a no-op unless the active step target or ramp actually changed.
        self._apply_current_step()

        completed = False
        with self._state_lock:
            if not self._sequence_running:
                return
            step = self._steps[self._sequence_index]

            if self._sequence_phase == "holding":
                # Holding is purely a timer. No Furnace setter is called here.
                held = time.monotonic() - self._hold_started_at
                if held < step["hold_minutes"] * 60:
                    return
                self._sequence_index += 1
                self._hold_started_at = None
                self._applied_signature = None
                if self._sequence_index >= len(self._steps):
                    self._sequence_index = len(self._steps) - 1
                    self._sequence_running = False
                    self._sequence_phase = "complete"
                    completed = True
                else:
                    self._sequence_phase = "ramping"
                    return
            else:
                at_target = (
                    abs(snapshot["pv"] - step["temperature_c"])
                    <= self.target_tolerance_c
                )
                # Do not start Python's hold timer until the controller has
                # reached SP2, the requested PV, and confirmed HOLD.
                if not at_target or snapshot["run_state_code"] != 2:
                    return
                self._hold_started_at = time.monotonic()
                self._sequence_phase = "holding"
                return

        if completed:
            with self._io_lock:
                self._furnace.set_stop()
                self._output_stopped = True
            self._mark_stopped()
            self._notify_end("complete")
    def _apply_current_step(self):
        """Set ramp and temperature once when the active step changes."""
        with self._state_lock:
            if not self._sequence_running:
                return
            step = dict(self._steps[self._sequence_index])
            signature = (
                self._sequence_index,
                step["temperature_c"],
                step["ramp_rate_c_per_min"],
            )
            if signature == self._applied_signature:
                return
        with self._io_lock:
            self._furnace.set_ramp_rate(step["ramp_rate_c_per_min"])
            self._furnace.set_temperature(step["temperature_c"])
        with self._state_lock:
            self._applied_signature = signature
            self._hold_started_at = None
            self._sequence_phase = "ramping"

    def _mark_stopped(self):
        """Immediately expose STOP with no active setpoint."""
        with self._state_lock:
            current_sv = self._snapshot.get("sv")
            if current_sv is not None:
                self._snapshot["stored_sv"] = current_sv
            self._snapshot["sv"] = None
            self._snapshot["run_state_code"] = 1
            self._snapshot["run_state"] = "STOP/COOLING"

    def _notify_end(self, reason):
        """Notify the application after sequence state and locks are settled."""
        callback = self.on_sequence_end
        if callback is not None:
            try:
                callback(reason)
            except Exception as exc:
                with self._state_lock:
                    self._error = exc

    @classmethod
    def _validate_steps(cls, steps):
        """Validate incoming sequence JSON and normalize all numeric fields."""
        if not isinstance(steps, list) or not steps:
            raise ValueError("Sequence must contain at least one step")
        result = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                raise ValueError(f"Step {index} must be an object")
            result.append(
                {
                    "temperature_c": cls._number(
                        step.get("temperature_c"),
                        f"step {index} temperature",
                        minimum=0,
                    ),
                    "ramp_rate_c_per_min": cls._number(
                        step.get("ramp_rate_c_per_min"),
                        f"step {index} ramp rate",
                        exclusive_minimum=0,
                    ),
                    "hold_minutes": cls._number(
                        step.get("hold_minutes", 0),
                        f"step {index} hold time",
                        minimum=0,
                    ),
                }
            )
        return result

    @staticmethod
    def _number(value, name, minimum=None, exclusive_minimum=None):
        """Parse one finite number and enforce optional lower bounds."""
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} must be finite")
        if minimum is not None and number < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        if exclusive_minimum is not None and number <= exclusive_minimum:
            raise ValueError(f"{name} must be greater than {exclusive_minimum}")
        return number

