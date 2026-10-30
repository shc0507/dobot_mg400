"""AI-808/AI-518P furnace and MFC drivers with verified register writes.

Create a Furnace explicitly to connect and leave it in STOP/COOLING. Connect
the MFC separately. Constructors open devices; module imports do not.
FurnaceConfig supplies serial settings and finite temperature/ramp caps.
Setters retain the source's clamping behavior. Before RUN and HOLD, check
live values and supported active program banks against the configured caps.
"""

import math
import queue
import threading
import time
from dataclasses import replace

import minimalmodbus
import serial

from .config import FurnaceConfig, resolve_slave_address, serial_port

# Serial protocol and polling settings for the furnace controller.
FLOW_BAUDRATE = 9600
FLOW_TIMEOUT_SECONDS = 0.2
FLOW_RESPONSE_ATTEMPTS = 4
# CAP's SetGas waits 80 ms after each ASCII write before another bus command.
FLOW_WRITE_SETTLE_SECONDS = 0.08
FLOW_VALVE_SLAVE_ADDRESS = 4
GAS_CHANNEL_COUNT = 3
GAS_UNIT_IDS = (7, 8, 9)
POLL_INTERVAL_SECONDS = 1.0
RUN_TO_HOLD_DELAY_SECONDS = 0.15

# Verified zero-based register addresses
SV_REGISTER = 0x00
DPT_REGISTER = 0x0C
AMAN_REGISTER = 0x18
SRUN_REGISTER = 0x1B
ACT_REGISTER = 0x22
AUX_OUTPUT_TYPE_REGISTER = 0x24
P2_REGISTER = 0x25
I2_REGISTER = 0x26
D2_REGISTER = 0x27
CTI2_REGISTER = 0x28
EVENT_OUTPUT_STATUS_REGISTER = 0x30
SPR_REGISTER = 0x2A
PV_REGISTER = 74
MODEL_ID_REGISTER = 0x15
PNO_REGISTER = 0x2B
PAF_REGISTER = 0x2D
PROGRAM_STEP_REGISTER = 0x2E
AI_518P_MODEL_ID = 5187
AI_808_MODEL_ID = 8080
# Per Furnac_Communication_Protocol.pdf pp. 4-5, program memory begins
# at 0x50 and alternates SP1, t1, SP2, t2, ... through at most 0xB4.
PROGRAM_DATA_FIRST_REGISTER = 0x50
PROGRAM_SP1_REGISTER = 0x50
PROGRAM_T1_REGISTER = 0x51
PROGRAM_SP2_REGISTER = 0x52
PROGRAM_T2_REGISTER = 0x53
MAX_PROGRAM_TIME_VALUE = 32000
MAX_PROGRAM_TIME_MINUTES = 3200.0
AUTOMATIC_MODE = 1
PROGRAM_DATA_LAST_REGISTER = 0xB4
MAX_PROGRAM_SETPOINTS = (PROGRAM_DATA_LAST_REGISTER - PROGRAM_DATA_FIRST_REGISTER) // 2 + 1


# Configure Modbus RTU serial communication and confirm the expected device responds.
def connect(port=None, slave_address=None):
    """Create and verify a connection to the furnace controller."""
    port = serial_port(port, "MG400_FURNACE_SERIAL")
    slave_address = resolve_slave_address(slave_address)
    print(
        f"Attempting to initialize furnace on {port}, slave {slave_address}...",
        flush=True,
    )
    controller = minimalmodbus.Instrument(port, slave_address)
    controller.mode = minimalmodbus.MODE_RTU

    controller.serial.baudrate = 9600
    controller.serial.bytesize = 8
    controller.serial.parity = serial.PARITY_NONE
    controller.serial.stopbits = 1
    controller.serial.timeout = 1.0

    controller.clear_buffers_before_each_transaction = True

    # Verify that the controller responds.
    address = read_register(controller, 0x16)

    if address != slave_address:
        controller.serial.close()
        raise RuntimeError(
            f"Expected slave address {slave_address}, but the controller returned {address}"
        )

    print(f"Connected to {port}, slave {slave_address}.")
    return controller


# Safely close an open serial connection; passing None is allowed.
def disconnect(controller):
    """Close the controller's serial connection."""
    if controller is not None and controller.serial.is_open:
        controller.serial.close()
        print("Serial connection closed.")


# ---------------------------------------------------------------------------
# Basic Modbus functions
# ---------------------------------------------------------------------------


# Use Modbus function 3 for all signed holding-register reads.
def read_register(controller, register):
    """Read one signed holding register."""
    return controller.read_register(
        registeraddress=register,
        functioncode=3,
        signed=True,
    )


# Write with Modbus function 6, then read back to catch rejected commands.
def write_register(controller, register, value):
    """Write and verify one holding register."""
    controller.write_register(
        registeraddress=register,
        value=value,
        number_of_decimals=0,
        functioncode=6,
        signed=True,
    )

    confirmed = read_register(controller, register)

    if confirmed != value:
        raise RuntimeError(
            f"Register {register} verification failed: wrote {value}, read back {confirmed}"
        )

    return confirmed


class MFCFlowController:
    """RS-485 ASCII driver for the MFCW mass-flow controllers.

    The MFCW manual defines packets as STX, two hexadecimal ID characters,
    a command and optional ``$`` data, followed by CR. Reverse engineering
    the installed CAP application confirmed that this enclosure's three gas
    MFCs have permanent ASCII addresses 07, 08, and 09.
    """

    def __init__(
        self,
        port=None,
        baudrate=FLOW_BAUDRATE,
        timeout=FLOW_TIMEOUT_SECONDS,
        serial_connection=None,
    ):
        self.port = serial_port(port, "MG400_MFC_SERIAL", required=serial_connection is None)
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self._io_lock = threading.RLock()
        self._controlled_unit_ids = set()
        self._controlled_solenoid_channels = set()
        # MFC valve overrides are separate from the enclosure's external
        # solenoids.  Keep track of either SVO or SVC so a later setpoint
        # command can return the MFC to normal closed-loop regulation (SVN).
        self._valve_override_units = set()
        self._last_status = {}
        self.serial = serial_connection
        if self.serial is None:
            # Match System.IO.Ports.SerialPort defaults used by CAP: open the
            # port with both modem-control lines inactive. PySerial otherwise
            # asserts DTR/RTS while opening, which can hold some RS-485
            # adapters in the wrong direction.
            self.serial = serial.Serial(
                port=None,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = self.port
            self.serial.open()

    @property
    def connected(self):
        """Return whether the RS-485 serial connection is open."""
        return bool(self.serial is not None and self.serial.is_open)

    @staticmethod
    def _unit_id(value):
        if isinstance(value, bool):
            raise ValueError("Flow-controller unit ID must be an integer from 1 to 255")
        try:
            numeric_id = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Flow-controller unit ID must be an integer from 1 to 255") from exc
        if not math.isfinite(numeric_id) or not numeric_id.is_integer():
            raise ValueError("Flow-controller unit ID must be an integer from 1 to 255")
        unit_id = int(numeric_id)
        if not 1 <= unit_id <= 255:
            raise ValueError("Flow-controller unit ID must be an integer from 1 to 255")
        return unit_id

    @staticmethod
    def _finite_number(value, name, minimum=None, maximum=None):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} must be finite")
        if minimum is not None and number < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"{name} must be at most {maximum}")
        return number

    def _exchange(self, unit_id, command, data=None):
        """Send one manual-defined ASCII packet and return its response body."""
        unit_id = self._unit_id(unit_id)
        if not self.connected:
            raise RuntimeError("Flow controller is not connected")
        if not isinstance(command, str) or len(command) not in (3, 4):
            raise ValueError("Invalid MFC command")
        payload = f"\x02{unit_id:02X}{command}"
        if data is not None:
            payload += f"${data}"
        packet = (payload + "\r").encode("ascii")

        with self._io_lock:
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            response = b""
            for _ in range(FLOW_RESPONSE_ATTEMPTS):
                self.serial.write(packet)
                self.serial.flush()
                response = self.serial.read_until(b"\r")
                if response.endswith(b"\r"):
                    break

        try:
            body = response[:-1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"MFC unit {unit_id} returned a non-ASCII response") from exc
        if body == "NG":
            raise RuntimeError(f"MFC unit {unit_id} rejected {command} (NG response)")
        return body

    def _send_without_response(self, unit_id, command, data=None):
        """Transmit one idempotent CAP ASCII command without awaiting a reply."""
        unit_id = self._unit_id(unit_id)
        if not self.connected:
            raise RuntimeError("Flow controller is not connected")
        if not isinstance(command, str) or len(command) not in (3, 4):
            raise ValueError("Invalid MFC command")
        payload = f"\x02{unit_id:02X}{command}"
        if data is not None:
            payload += f"${data}"
        packet = (payload + "\r").encode("ascii")
        with self._io_lock:
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            self.serial.write(packet)
            self.serial.flush()
            # flush() drains host output; it does not establish that the MFC
            # has processed the command. Keep the shared bus locked while it
            # settles, especially between SVN and the following SFR command.
            time.sleep(FLOW_WRITE_SETTLE_SECONDS)
        return packet

    @staticmethod
    def _modbus_crc(data):
        """Return the Modbus RTU CRC16 in low-byte, high-byte wire order."""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return bytes((crc & 0xFF, (crc >> 8) & 0xFF))

    @staticmethod
    def _gas_channel(value):
        if isinstance(value, bool):
            raise ValueError("Gas channel must be 0, 1, or 2")
        try:
            channel = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Gas channel must be 0, 1, or 2") from exc
        if str(value).strip() != str(channel) or not 0 <= channel < GAS_CHANNEL_COUNT:
            raise ValueError("Gas channel must be 0, 1, or 2")
        return channel

    @classmethod
    def gas_unit_id(cls, channel):
        """Return the installed CAP application's fixed MFC address."""
        return GAS_UNIT_IDS[cls._gas_channel(channel)]

    def set_gas_solenoid(self, channel, opened):
        """Open or close one fixed gas solenoid using the company packet."""
        channel = self._gas_channel(channel)
        if not isinstance(opened, bool):
            raise ValueError("opened must be true or false")
        if not self.connected:
            raise RuntimeError("Flow controller is not connected")
        value = b"\xff\x00" if opened else b"\x00\x00"
        body = bytes((FLOW_VALVE_SLAVE_ADDRESS, 0x05, 0x00, channel)) + value
        packet = body + self._modbus_crc(body)
        with self._io_lock:
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            self.serial.write(packet)
            self.serial.flush()
            response = self.serial.read(len(packet))
        self._controlled_solenoid_channels.add(channel)
        if response and response != packet:
            if len(response) < len(packet):
                raise RuntimeError(
                    f"Gas channel {channel + 1} returned an incomplete valve response"
                )
            if self._modbus_crc(response[:-2]) != response[-2:]:
                raise RuntimeError(f"Gas channel {channel + 1} returned a bad Modbus CRC")
            raise RuntimeError(f"Gas channel {channel + 1} did not echo the requested valve state")
        return {
            "channel_index": channel,
            "channel_number": channel + 1,
            "open": opened,
            "verified": response == packet,
            "packet_hex": packet.hex(" ").upper(),
        }

    def set_valve_override(self, unit_id, opened):
        """Fully open/close an MFC valve using CAP's SVO/SVC commands.

        The enclosure has two valves in series: the MFC's proportional valve
        and a separate Modbus solenoid.  The old CAP application controls the
        MFC valve with SVO/SVC; sending only the Modbus coil command therefore
        produces an audible click without any gas flow.  The MFC installation
        is write-only in this enclosure, so transmit the command without
        waiting for the optional ASCII acknowledgement.
        """
        unit_id = self._unit_id(unit_id)
        if not self.connected:
            raise RuntimeError("Flow controller is not connected")
        command = "SVO" if opened else "SVC"
        self._send_without_response(unit_id, command)
        self._controlled_unit_ids.add(unit_id)
        self._valve_override_units.add(unit_id)
        return {"unit_id": unit_id, "open": bool(opened), "command": command}

    def set_gas_valve(self, channel, opened):
        """Operate both the MFC valve and its external gas solenoid.

        The external relay is sent first so a failure on the optional MFC
        command cannot prevent the enclosure solenoid from actuating.  On
        close, the same relay-first ordering removes pressure before forcing
        the MFC closed.  This is the single operation used by the web button;
        ``set_gas_solenoid`` remains available for low-level relay-only calls.
        """
        channel = self._gas_channel(channel)
        if not isinstance(opened, bool):
            raise ValueError("opened must be true or false")
        unit_id = self.gas_unit_id(channel)
        if opened:
            # Keep the relay operation independent: the MFC ASCII side can be
            # silent or unavailable while the Modbus solenoid remains usable.
            solenoid = self.set_gas_solenoid(channel, True)
            try:
                override = self.set_valve_override(unit_id, True)
            except Exception:
                # Do not hide the fact that only the external relay opened;
                # callers receive the original error and can close the relay.
                raise
        else:
            # Always issue SVC even if the relay reports a malformed or
            # missing echo; the external coil may still have changed state.
            solenoid_error = None
            try:
                solenoid = self.set_gas_solenoid(channel, False)
            except Exception as exc:
                solenoid_error = exc
                solenoid = {
                    "channel_index": channel,
                    "channel_number": channel + 1,
                    "open": False,
                    "verified": False,
                }
            try:
                override = self.set_valve_override(unit_id, False)
            except Exception:
                if solenoid_error is not None:
                    raise solenoid_error
                raise
            if solenoid_error is not None:
                raise solenoid_error
        solenoid.update({"mfc_valve": override})
        return solenoid

    def _restore_normal_valve(self, unit_id):
        """Leave a manual SVO override before applying a flow setpoint."""
        unit_id = self._unit_id(unit_id)
        if unit_id in self._valve_override_units:
            self._send_without_response(unit_id, "SVN")
            self._valve_override_units.discard(unit_id)

    def _write_command(self, unit_id, command, data=None):
        # The source permits a silent acknowledgement. _exchange still rejects NG.
        self._exchange(unit_id, command, data)

    def _read_command(self, unit_id, command):
        response = self._exchange(unit_id, command)
        if not response:
            raise RuntimeError(f"MFC unit {unit_id} returned an empty response")
        status, data = response[0], response[1:]
        if status not in ("N", "A"):
            status_names = {
                "A": "power or flow alarm",
                "E": "sensor electronics error",
                "X": "alarm and electronics error",
            }
            problem = status_names.get(status, f"unknown status {status!r}")
            raise RuntimeError(f"MFC unit {unit_id} reports {problem}")
        self._last_status[self._unit_id(unit_id)] = status
        return data

    def set_digital_mode(self, unit_id):
        """Select the documented digital RS-485 setpoint source."""
        self._write_command(unit_id, "SCM", "1")

    def read_gas_name(self, unit_id):
        """Read the gas name stored by a controller."""
        return self._read_command(unit_id, "RGN").strip()

    def read_flow_percent(self, unit_id):
        """Read actual flow as a percentage of the configured full scale."""
        raw = self._read_command(unit_id, "RFR")
        try:
            return int(raw, 16) / 100.0
        except ValueError as exc:
            raise RuntimeError(f"MFC unit {unit_id} returned invalid flow data {raw!r}") from exc

    def set_flow_percent(self, unit_id, percent):
        """Safely select digital control and set 0.00 to 100.00 percent flow."""
        unit_id = self._unit_id(unit_id)
        percent = self._finite_number(percent, "Flow percentage", minimum=0, maximum=100)
        raw = round(percent * 100)
        self._controlled_unit_ids.add(unit_id)
        self._valve_override_units.discard(unit_id)

        # Close before changing the setpoint source so an existing analog input
        # cannot briefly command unexpected flow. SVN restores regulation.
        self._write_command(unit_id, "SVC")
        try:
            self.set_digital_mode(unit_id)
            self._write_command(unit_id, "SFR", f"{raw:04X}")
            if raw:
                self._write_command(unit_id, "SVN")
        except Exception:
            try:
                self._write_command(unit_id, "SVC")
            except Exception:
                pass
            raise
        return percent

    def set_flow_percent_unconfirmed(self, unit_id, percent):
        """Set a 0–100 percent setpoint without requiring an MFC reply."""
        unit_id = self._unit_id(unit_id)
        percent = self._finite_number(percent, "Flow percentage", minimum=0, maximum=100)
        raw = round(percent * 100)
        self._controlled_unit_ids.add(unit_id)
        self._restore_normal_valve(unit_id)
        self._send_without_response(unit_id, "SFR", f"{raw:X}")
        return percent

    def stop_flow_unconfirmed(self, unit_id):
        """Send CAP's zero setpoint without requiring an acknowledgement."""
        unit_id = self._unit_id(unit_id)
        self._controlled_unit_ids.add(unit_id)
        self._restore_normal_valve(unit_id)
        self._send_without_response(unit_id, "SFR", "0")
        return 0.0

    def gas_channel_status(self, channel):
        """Read one of the three fixed gas channels without discovery."""
        channel = self._gas_channel(channel)
        unit_id = self.gas_unit_id(channel)
        percent = self.read_flow_percent(unit_id)
        return {
            "channel_index": channel,
            "channel_number": channel + 1,
            "unit_id": unit_id,
            "gas_name": f"Gas {channel + 1}",
            "actual_percent": percent,
            "controller_status": self._last_status.get(unit_id, "N"),
            "available": True,
            "error": None,
            "write_only": False,
        }

    def set_water_flow(self, unit_id, percent):
        """Set a channel only when its stored identity explicitly says water."""
        name = self.read_gas_name(unit_id)
        normalized = "".join(character for character in name.upper() if character.isalnum())
        if "WATER" not in normalized and "H2O" not in normalized:
            raise ValueError(
                f"MFC unit {self._unit_id(unit_id)} identifies as {name!r}, not WATER/H2O; "
                "no water command was sent"
            )
        return self.set_flow_percent(unit_id, percent)

    def stop_flow(self, unit_id):
        """Command a zero setpoint using CAP's normal SFR command."""
        unit_id = self._unit_id(unit_id)
        self._controlled_unit_ids.add(unit_id)
        self._restore_normal_valve(unit_id)
        self._write_command(unit_id, "SFR", "0")
        return 0.0

    def channel_status(self, unit_id):
        """Read identity and actual flow percentage."""
        unit_id = self._unit_id(unit_id)
        gas_name = self.read_gas_name(unit_id)
        percent = self.read_flow_percent(unit_id)
        return {
            "unit_id": unit_id,
            "gas_name": gas_name,
            "actual_percent": percent,
            "controller_status": self._last_status.get(unit_id, "N"),
        }

    def close(self, stop_output=True):
        """Optionally close every valve controlled during this session."""
        if self.serial is None:
            return
        try:
            if stop_output and self.connected:
                for channel in sorted(self._controlled_solenoid_channels):
                    try:
                        self.set_gas_valve(channel, False)
                    except Exception:
                        pass
                for unit_id in sorted(self._controlled_unit_ids):
                    try:
                        self.stop_flow_unconfirmed(unit_id)
                    except Exception:
                        pass
        finally:
            if self.connected:
                self.serial.close()
            self.serial = None


# Low-level convenience wrapper around the furnace registers and serial connection.
class Furnace(object):
    def __init__(self, port=None, slave_address=None, flow_port=None, *, config=None):
        """Connect safely and leave the controller in STOP/COOLING."""
        if config is None:
            config = FurnaceConfig.from_env(
                port=port, slave_address=slave_address, flow_port=flow_port
            )
        else:
            config = replace(
                config,
                port=config.port if port is None else port,
                slave_address=config.slave_address if slave_address is None else slave_address,
                flow_port=config.flow_port if flow_port is None else flow_port,
            )
        self.config = config
        self.controller = None
        self.port = serial_port(config.port, "MG400_FURNACE_SERIAL")
        self.slave_address = config.slave_address
        self.flow_port = config.flow_port
        self.commands = queue.Queue()
        self.should_exit = False
        self.instrument_model = None
        self.program_steps_before_disable = None
        self.program_mode_disabled = False
        self.program_setpoint_registers = ()
        self._program_bank_supported = False
        self.pending_hold_target = None
        self.controller_program_step = None
        self.flow_controller = None

        self.controller = connect(port=self.port, slave_address=self.slave_address)
        try:
            self.disable_internal_program()
            self.program_setpoint_registers = self._detect_program_setpoints()
            self._program_bank_supported = self.instrument_model == AI_518P_MODEL_ID or bool(
                self.program_setpoint_registers
            )
            # Merely constructing the library object must never begin heating.
            self.set_stop()
        except Exception:
            disconnect(self.controller)
            self.controller = None
            raise

        # Do not auto-connect the separate flow controller during furnace setup.
        # The caller explicitly connects each device with its separate settings.

    @property
    def connected(self):
        """Return whether the serial connection is currently open."""
        return bool(self.controller is not None and self.controller.serial.is_open)

    def __enter__(self):
        """Allow ``with Furnace() as furnace`` for automatic cleanup."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def connect_flow_controller(self, port=None):
        """Open the enclosure's independent MFCW RS-485 connection."""
        port = serial_port(self.flow_port if port is None else port, "MG400_MFC_SERIAL")
        if self.flow_controller is None:
            self.flow_controller = MFCFlowController(port=port)
            self.flow_port = port
        elif port != self.flow_controller.port:
            raise RuntimeError("Disconnect the flow controller before changing its port")
        return self.flow_controller

    def disconnect_flow_controller(self, stop_output=True):
        """Close controlled flow channels, then release their serial port."""
        controller, self.flow_controller = self.flow_controller, None
        if controller is not None:
            controller.close(stop_output=stop_output)

    def _require_flow_controller(self):
        if self.flow_controller is None or not self.flow_controller.connected:
            raise RuntimeError("Flow controller is not connected")
        return self.flow_controller

    def set_gas_flow(self, channel, flow_percent):
        """Set fixed gas channel 0, 1, or 2 to a 0–100% flow setpoint."""
        controller = self._require_flow_controller()
        channel = controller._gas_channel(channel)
        unit_id = controller.gas_unit_id(channel)
        percent = MFCFlowController._finite_number(
            flow_percent, "Flow percentage", minimum=0, maximum=100
        )
        if percent == 0:
            controller.set_gas_solenoid(channel, False)
            return controller.set_flow_percent_unconfirmed(unit_id, percent)
        target = controller.set_flow_percent_unconfirmed(unit_id, percent)
        controller.set_gas_solenoid(channel, True)
        return target

    def set_gas_valve(self, channel, opened):
        """Operate the MFC valve and fixed solenoid for a gas channel."""
        return self._require_flow_controller().set_gas_valve(channel, opened)

    def open_gas_valve(self, channel):
        """Open the main gas path for zero-based channel 0, 1, or 2."""
        return self.set_gas_valve(channel, True)

    def close_gas_valve(self, channel):
        """Close the main gas path for zero-based channel 0, 1, or 2."""
        return self.set_gas_valve(channel, False)

    def get_gas_flow(self, channel):
        """Return capacity and live flow for fixed gas channel 0, 1, or 2."""
        return self._require_flow_controller().gas_channel_status(channel)

    def stop_gas_flow(self, channel):
        """Close one fixed gas solenoid, then set its MFC target to zero."""
        controller = self._require_flow_controller()
        channel = controller._gas_channel(channel)
        controller.set_gas_valve(channel, False)
        return controller.stop_flow_unconfirmed(controller.gas_unit_id(channel))

    def disable_internal_program(self):
        """Disable the AI-518P stored program and select constant-temperature mode.

        Pno controls how many internal program steps are active. Setting it to
        zero does not erase the stored step values; it prevents those steps from
        changing SV while Python is controlling the setpoint.
        """
        self.instrument_model = read_register(self.controller, MODEL_ID_REGISTER)
        if self.instrument_model != AI_518P_MODEL_ID:
            self.program_steps_before_disable = None
            self.program_mode_disabled = False
            self.program_setpoint_registers = ()
            return False

        current_pno = read_register(self.controller, PNO_REGISTER)
        self.program_steps_before_disable = current_pno
        if current_pno != 0:
            # Stop the internal program before changing its active step count.
            write_register(self.controller, SRUN_REGISTER, 1)
            write_register(self.controller, PNO_REGISTER, 0)

        confirmed_pno = read_register(self.controller, PNO_REGISTER)
        if confirmed_pno != 0:
            raise RuntimeError("Could not disable the controller's internal program (Pno != 0)")
        self.program_mode_disabled = True
        return True

    def _detect_program_setpoints(self):
        """Return only program-setpoint registers documented by Yudian.

        Furnac_Communication_Protocol.pdf defines Pno (0x2B) as the number of
        program steps and the program bank as 0x50 onward, alternating SP and
        time. Codes beyond the active bank may not respond, so Pno also bounds
        every probe and write.
        """
        try:
            step_count = self.program_steps_before_disable
            if step_count is None:
                step_count = read_register(self.controller, PNO_REGISTER)
        except Exception:
            return ()

        if not 1 <= step_count <= MAX_PROGRAM_SETPOINTS:
            return ()

        registers = tuple(PROGRAM_DATA_FIRST_REGISTER + (2 * index) for index in range(step_count))
        try:
            probe_values = (
                read_register(self.controller, registers[0]),
                read_register(self.controller, registers[-1]),
            )
        except Exception:
            return ()

        # The PDF defines 32512-32767 as the invalid/reserved-code response.
        if any(value >= 32512 for value in probe_values):
            return ()

        self.program_steps_before_disable = step_count
        return registers

    def set_automatic_mode(self):
        """Select verified PID automatic output mode (AMAn=1)."""
        return write_register(self.controller, AMAN_REGISTER, AUTOMATIC_MODE)

    def get_output_mode(self):
        """Return the AMAn output-mode code and its display name."""
        mode = read_register(self.controller, AMAN_REGISTER)
        names = {
            0: "MAN",
            1: "AUTO",
            2: "FORCED MAN",
            3: "FORCED AUTO",
        }
        return mode, names.get(mode, f"UNKNOWN({mode})")

    def get_auxiliary_output_status(self):
        """Read the controller's configured auxiliary/cooling output."""
        aux_type = read_register(self.controller, AUX_OUTPUT_TYPE_REGISTER)
        acting = read_register(self.controller, ACT_REGISTER)
        return {
            "type_code": aux_type,
            "type": {0: "SSR", 1: "RELAY", 2: "0-20mA", 3: "4-20mA"}.get(aux_type, "UNKNOWN"),
            "acting_method_code": acting,
            "acting_method": {
                0: "REVERSE",
                1: "DIRECT",
                2: "REVERSE+BURNOUT",
                3: "DIRECT+BURNOUT",
            }.get(acting, "UNKNOWN"),
            "event_output_status": read_register(self.controller, EVENT_OUTPUT_STATUS_REGISTER),
            "secondary_pid": {
                "p": read_register(self.controller, P2_REGISTER),
                "i": read_register(self.controller, I2_REGISTER),
                "d": read_register(self.controller, D2_REGISTER),
                "period": read_register(self.controller, CTI2_REGISTER),
            },
        }

    # Explicit RUN remains available for advanced callers, but is not used by sequences.
    def start(self):
        self._check_run_limits()
        return write_register(self.controller, SRUN_REGISTER, 0)

    def _check_run_limits(self, *, preserved_sv=None):
        """Check live and active program values before an explicit RUN transition."""
        _, _, scale = self.get_temperature_format()
        if preserved_sv is not None:
            MFCFlowController._finite_number(
                preserved_sv / scale,
                "Preserved controller temperature",
                maximum=self.config.max_c,
            )
        MFCFlowController._finite_number(
            read_register(self.controller, SV_REGISTER) / scale,
            "Controller temperature",
            maximum=self.config.max_c,
        )
        MFCFlowController._finite_number(
            read_register(self.controller, SPR_REGISTER) / scale,
            "Controller ramp rate",
            minimum=0,
            maximum=self.config.max_ramp,
        )
        # The source permits controllers without a readable program bank.
        # Check stored targets only after the model, a probe, or programming
        # has established support for these registers.
        if not self._program_bank_supported:
            return
        count = read_register(self.controller, PNO_REGISTER)
        if not isinstance(count, int) or not 0 <= count <= MAX_PROGRAM_SETPOINTS:
            raise RuntimeError(f"Cannot check the active program targets: unexpected Pno {count}")
        for index in range(count):
            value = read_register(self.controller, PROGRAM_DATA_FIRST_REGISTER + 2 * index)
            if value >= 32512:
                raise RuntimeError("Cannot check an active program target: reserved register value")
            MFCFlowController._finite_number(
                value / scale, "Active program temperature", maximum=self.config.max_c
            )

    def set_hold(self):
        """Enter RUN before HOLD, preserving the live setpoint throughout.

        HOLD appears to be a paused RUN state on this controller. Preserve SV,
        enter and verify RUN, briefly allow that transition to settle, request
        and verify HOLD, then restore SV in case RUN loaded an internal program
        value. Any failure after RUN begins sends STOP before being reported.
        """
        preserved_sv = read_register(self.controller, SV_REGISTER)
        try:
            self._check_run_limits(preserved_sv=preserved_sv)
            write_register(self.controller, SRUN_REGISTER, 0)
            time.sleep(RUN_TO_HOLD_DELAY_SECONDS)

            self.controller.write_register(
                registeraddress=SRUN_REGISTER,
                value=2,
                number_of_decimals=0,
                functioncode=6,
                signed=True,
            )
            confirmed = read_register(self.controller, SRUN_REGISTER)
            if confirmed != 2:
                raise RuntimeError(
                    f"HOLD was not accepted after RUN: wrote Srun=2 but read back Srun={confirmed}."
                )

            # RUN may have loaded a stored program target before HOLD took effect.
            write_register(self.controller, SV_REGISTER, preserved_sv)
            final_state = read_register(self.controller, SRUN_REGISTER)
            if final_state != 2:
                raise RuntimeError(
                    "The controller left HOLD while restoring the requested "
                    f"setpoint (Srun={final_state})."
                )
            return final_state
        except Exception as exc:
            try:
                self.set_stop()
            except Exception as stop_exc:
                raise RuntimeError(
                    f"{exc} An automatic STOP was also attempted but failed: {stop_exc}"
                ) from exc
            raise RuntimeError(
                f"{exc} Heating has been stopped; stored program setpoints were not changed."
            ) from exc

    def set_stop(self):
        """Put the controller in STOP mode without closing the serial connection."""
        confirmed = write_register(self.controller, SRUN_REGISTER, 1)
        self.pending_hold_target = None
        self.controller_program_step = None
        print("Furnace controller is STOPPED.")
        print("Heating is disabled; the furnace can cool naturally.")
        return confirmed

    def close(self, stop_output=True):
        """Optionally stop heating/flow, then close both serial connections."""
        try:
            if self.controller is not None and stop_output:
                self.set_stop()
        finally:
            if self.controller is not None:
                disconnect(self.controller)
            self.controller = None
            self.disconnect_flow_controller(stop_output=stop_output)

    def stop(self):
        """Backward-compatible helper that stops heating and disconnects."""
        self.close(stop_output=True)

    # Read temperature and ramp registers and return display-formatted values.
    def get_current_values(self):
        _, decimal_places, scale = self.get_temperature_format()

        raw_pv = read_register(self.controller, PV_REGISTER)
        raw_sv = read_register(self.controller, SV_REGISTER)
        raw_rate = read_register(self.controller, SPR_REGISTER)
        rate = raw_rate / scale

        temperatures = {
            "pv": raw_pv / scale,
            "sv": raw_sv / scale,
            "decimal_places": decimal_places,
        }
        places = temperatures["decimal_places"]

        return {
            "time": time.time(),
            "PV": f"{temperatures['pv']:7.{places}f}",
            "SV": f"{temperatures['sv']:7.{places}f}",
            "Ramp": f"{round(rate, decimal_places):7.{places}f}",
        }

    # Translate the numeric run-state register into a human-readable name.
    def get_run_state(self):
        state = read_register(self.controller, SRUN_REGISTER)

        names = {
            0: "RUN",
            1: "STOP/COOLING",
            2: "HOLD",
        }

        return state, names.get(state, f"UNKNOWN({state})")

    # Run two identical target steps; the monitor enters HOLD at STEP=2.
    def set_temperature(self, temp):
        """Run toward ``temp`` with both SP1 and SP2 set to that target.

        This method calculates t1 in tenths of a minute from the current-PV
        temperature difference / SPr. The controller receives the requested
        target as SP1, SP2, and live SV, then FurnaceService applies HOLD when
        the controller enters step 2.
        """
        temp = MFCFlowController._finite_number(temp, "Temperature")
        if temp > self.config.max_c:
            temp = self.config.max_c
            print(f"Set to max temp {self.config.max_c}")

        target = float(temp)
        _, _, scale = self.get_temperature_format()
        raw_start = read_register(self.controller, PV_REGISTER)
        starting_temperature = raw_start / scale
        raw_temperature = min(round(target * scale), math.floor(self.config.max_c * scale))
        raw_ramp_rate = read_register(self.controller, SPR_REGISTER)
        ramp_rate = raw_ramp_rate / scale
        if ramp_rate <= 0:
            raise RuntimeError(
                "Cannot calculate t1 because the controller ramp rate is not positive"
            )
        MFCFlowController._finite_number(
            ramp_rate, "Controller ramp rate", maximum=self.config.max_ramp
        )

        duration_minutes = abs(target - starting_temperature) / ramp_rate
        if duration_minutes > MAX_PROGRAM_TIME_MINUTES:
            raise ValueError(
                f"The requested ramp requires {duration_minutes:.1f} minutes; "
                f"the controller maximum is {MAX_PROGRAM_TIME_MINUTES:.1f}"
            )
        raw_t1 = max(1, round(duration_minutes * 10))

        # PAF.B=0 selects ramp mode and PAF.C=0 selects minute units. Preserve
        # every unrelated PAF option while clearing just those two bits.
        current_paf = read_register(self.controller, PAF_REGISTER)
        if not 0 <= current_paf <= 255:
            raise RuntimeError(f"Unexpected PAF value: {current_paf}")
        ramp_minutes_paf = current_paf & ~0x06

        self.set_stop()
        self.set_automatic_mode()
        if ramp_minutes_paf != current_paf:
            write_register(self.controller, PAF_REGISTER, ramp_minutes_paf)
        write_register(self.controller, PNO_REGISTER, 2)
        write_register(self.controller, PROGRAM_SP1_REGISTER, raw_temperature)
        write_register(self.controller, PROGRAM_T1_REGISTER, raw_t1)
        write_register(self.controller, PROGRAM_SP2_REGISTER, raw_temperature)
        write_register(self.controller, PROGRAM_T2_REGISTER, MAX_PROGRAM_TIME_VALUE)
        write_register(self.controller, PROGRAM_STEP_REGISTER, 1)
        write_register(self.controller, SV_REGISTER, raw_temperature)
        self._program_bank_supported = True
        write_register(self.controller, SRUN_REGISTER, 0)

        self.pending_hold_target = target
        self.controller_program_step = 1
        return target

    def get_program_step(self):
        """Read the controller's current internal program step number."""
        step = read_register(self.controller, PROGRAM_STEP_REGISTER)
        self.controller_program_step = step
        return step

    def update_temperature_change(self, current_pv=None, tolerance_c=None):
        """Enter HOLD when the two-target program reaches step 2."""
        if self.pending_hold_target is None:
            return False
        if self.get_program_step() != 2:
            return False

        self.set_hold()
        self.pending_hold_target = None
        self.controller_program_step = 2
        return True

    # Validate and write the temperature change rate in controller-scaled units.
    def set_ramp_rate(self, rate):
        rate = MFCFlowController._finite_number(rate, "Ramp rate")
        if rate <= 0:
            raise ValueError("Ramp rate must be greater than zero.")
        if rate > self.config.max_ramp:
            rate = self.config.max_ramp
            print(f"Set to max ramp ({self.config.max_ramp})")

        _, decimal_places, scale = self.get_temperature_format()
        raw_rate = min(round(rate * scale), math.floor(self.config.max_ramp * scale))
        if raw_rate <= 0:
            raise ValueError("Ramp rate is below the controller resolution")
        write_register(
            self.controller,
            SPR_REGISTER,
            raw_rate,
        )
        return rate

    # Adjust the current setpoint by a relative amount and return the new target.
    def change_temperature(self, delta, ramp_rate):
        delta = MFCFlowController._finite_number(delta, "Temperature change")
        ramp_rate = MFCFlowController._finite_number(ramp_rate, "Ramp rate")
        if ramp_rate <= 0:
            raise ValueError("Ramp rate must be greater than zero.")
        current_temp = float(self.get_current_values()["SV"])
        new_temp = MFCFlowController._finite_number(current_temp + delta, "Temperature")
        if new_temp > self.config.max_c:
            new_temp = self.config.max_c
            print(f"Cannot exceed {self.config.max_c} degrees")
        self.set_ramp_rate(ramp_rate)
        self.set_temperature(new_temp)
        return new_temp

    # Decode dPt to determine how raw register values map to decimal temperatures.
    def get_temperature_format(self):
        """
        Return dPt, number of transmitted decimal places, and scaling factor.
        """
        dpt = read_register(self.controller, DPT_REGISTER)
        decimal_places = dpt & 0x7F

        # The protocol says dPt values >=128 require one additional /10.
        if dpt >= 128:
            decimal_places += 1

        if decimal_places > 4:
            raise RuntimeError(f"Unexpected dPt value: {dpt}")

        scale = 10**decimal_places
        return dpt, decimal_places, scale
