"""In-memory serial transports for furnace tests, demo mode, and rehearsals.

Pass FurnaceSerial directly to minimalmodbus.Instrument. Pass MFCSerial as
MFCFlowController's serial_connection. These objects never open a device,
start a thread, or bind a port. A closed transport can reopen in memory.

The furnace implements the driver's signed FC3/FC6 registers and its
two-target Pno=2 program in ramp/minute mode. Each time-register tick is
6 seconds. STEP=2 keeps its target until an explicit HOLD or STOP. Other
program modes, jumps, and terminal-step actions are not modeled.

Temperature follows a first-order response, bounded by SPr in degrees per
minute during RUN/HOLD. HOLD pauses the program timer but maintains SV.
STOP cools toward ambient without the active ramp limit. The clock and
time_scale control elapsed simulation time. No background polling is needed.
This model is deterministic, not a calibrated model of the lab furnace.

MFC readback is instantaneous. The external solenoid and the MFC override
both affect flow. Gas names are simulation labels. Missing replies can be
modeled without undoing accepted commands, which preserves unknown outcomes.
"""

import math
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import controller as p


def _finite(value: float, name: str, *, positive: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return value


def _frame(body: bytes) -> bytes:
    return body + p.MFCFlowController._modbus_crc(body)


def _exception(address: int, function: int, code: int) -> bytes:
    return _frame(bytes((address, function | 0x80, code)))


class _Serial:
    """The serial-like interface used by minimalmodbus and the MFC driver."""

    def __init__(self, port: str):
        self.port = port
        self.baudrate = 9600
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.timeout = 0.2
        self.write_timeout = 0.2
        self.dtr = False
        self.rts = False
        self.is_open = True
        self.respond = True
        self._received = bytearray()
        self.requests = deque(maxlen=1024)

    @property
    def in_waiting(self) -> int:
        return len(self._received)

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self._received.clear()

    def _check_open(self) -> None:
        if not self.is_open:
            raise OSError("Simulated serial transport is closed")

    def reset_input_buffer(self) -> None:
        self._check_open()
        self._received.clear()

    def reset_output_buffer(self) -> None:
        self._check_open()

    def flush(self) -> None:
        self._check_open()

    def write(self, data: bytes) -> int:
        self._check_open()
        packet = bytes(data)
        self.requests.append(packet)
        response = self._respond(packet)
        if self.respond:
            self._received.extend(response)
        return len(packet)

    def read(self, size: int = 1) -> bytes:
        self._check_open()
        if size < 0:
            raise ValueError("Read size must not be negative")
        data = bytes(self._received[:size])
        del self._received[:size]
        return data

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        self._check_open()
        end = self._received.find(expected)
        count = len(self._received) if end < 0 else end + len(expected)
        return self.read(count if size is None else min(count, size))

    def _respond(self, packet: bytes) -> bytes:
        raise NotImplementedError


class FurnaceSerial(_Serial):
    """Simulate the AI-808 register subset used by Furnace.

    clock returns monotonic seconds. time_scale multiplies elapsed seconds.
    time_constant_s controls the first-order response in simulation seconds.
    registers returns a copy for inspection. Change device state through
    protocol writes, so readback and driver safety checks still apply.
    """

    def __init__(
        self,
        *,
        slave_address: int = 1,
        ambient_c: float = 25,
        initial_c: float | None = None,
        dpt: int = 1,
        time_constant_s: float = 30,
        time_scale: float = 1,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__("sim://furnace")
        if isinstance(slave_address, bool) or not isinstance(slave_address, int):
            raise ValueError("Slave address must be an integer from 1 to 247")
        if not 1 <= slave_address <= 247:
            raise ValueError("Slave address must be an integer from 1 to 247")
        if not isinstance(dpt, int) or isinstance(dpt, bool) or dpt < 0:
            raise ValueError("dPt must describe 0 to 4 decimal places")
        places = (dpt & 0x7F) + (1 if dpt >= 128 else 0)
        if dpt > 132 or places > 4:
            raise ValueError("dPt must describe 0 to 4 decimal places")
        self._scale = 10**places
        self._ambient = _finite(ambient_c, "Ambient temperature")
        self._temperature = _finite(
            ambient_c if initial_c is None else initial_c, "Initial temperature"
        )
        for value in (self._ambient, self._temperature):
            if not -32768 <= round(value * self._scale) <= 32767:
                raise ValueError("Temperature does not fit a signed register at this dPt")
        self._tau = _finite(time_constant_s, "Time constant", positive=True)
        self._time_scale = _finite(time_scale, "Time scale", positive=True)
        self._clock = clock
        self._last_time = _finite(clock(), "Clock")
        self._step_elapsed = 0.0
        self._address = slave_address
        self._registers = {
            p.SV_REGISTER: round(self._ambient * self._scale),
            p.DPT_REGISTER: dpt,
            p.AMAN_REGISTER: p.AUTOMATIC_MODE,
            p.SRUN_REGISTER: 1,
            p.ACT_REGISTER: 0,
            p.AUX_OUTPUT_TYPE_REGISTER: 0,
            p.P2_REGISTER: 0,
            p.I2_REGISTER: 0,
            p.D2_REGISTER: 0,
            p.CTI2_REGISTER: 0,
            p.EVENT_OUTPUT_STATUS_REGISTER: 0,
            p.SPR_REGISTER: min(10 * self._scale, 32767),
            p.PV_REGISTER: round(self._temperature * self._scale),
            p.MODEL_ID_REGISTER: p.AI_808_MODEL_ID,
            0x16: slave_address,
            p.PNO_REGISTER: 0,
            p.PAF_REGISTER: 0,
            p.PROGRAM_STEP_REGISTER: 1,
        }
        for register in range(p.PROGRAM_DATA_FIRST_REGISTER, p.PROGRAM_DATA_LAST_REGISTER + 1):
            self._registers[register] = (
                round(self._ambient * self._scale) if register % 2 == 0 else 1
            )

    @property
    def registers(self) -> dict[int, int]:
        self._update()
        return self._registers.copy()

    @property
    def temperature_c(self) -> float:
        self._update()
        return self._temperature

    @property
    def step_elapsed_s(self) -> float:
        self._update()
        return self._step_elapsed

    def _evolve(self, seconds: float, *, heating: bool) -> None:
        target = self._registers[p.SV_REGISTER] / self._scale if heating else self._ambient
        difference = target - self._temperature
        distance = abs(difference)
        if heating:
            rate = self._registers[p.SPR_REGISTER] / self._scale / 60
            if rate <= 0:
                return
            linear_seconds = max(0.0, (distance - rate * self._tau) / rate)
            elapsed_linear = min(seconds, linear_seconds)
            distance -= rate * elapsed_linear
            seconds -= elapsed_linear
        distance *= math.exp(-seconds / self._tau)
        self._temperature = target - math.copysign(distance, difference)

    def _update(self) -> None:
        now = _finite(self._clock(), "Clock")
        if now < self._last_time:
            raise ValueError("Simulation clock must not move backwards")
        seconds = (now - self._last_time) * self._time_scale
        if not math.isfinite(seconds):
            raise ValueError("Elapsed simulation time must be finite")
        self._last_time = now
        state = self._registers[p.SRUN_REGISTER]
        if state == 0 and self._registers[p.PNO_REGISTER] == 2:
            if self._registers[p.PROGRAM_STEP_REGISTER] == 1:
                remaining = max(
                    0.0, self._registers[p.PROGRAM_T1_REGISTER] * 6 - self._step_elapsed
                )
                segment = min(seconds, remaining)
                self._evolve(segment, heating=True)
                self._step_elapsed += segment
                seconds -= segment
                if segment >= remaining or math.isclose(
                    segment, remaining, rel_tol=0, abs_tol=1e-9
                ):
                    self._registers[p.PROGRAM_STEP_REGISTER] = 2
                    self._registers[p.SV_REGISTER] = self._registers[p.PROGRAM_SP2_REGISTER]
                    self._step_elapsed = 0
            self._step_elapsed += seconds
        self._evolve(seconds, heating=state in (0, 2))
        self._registers[p.PV_REGISTER] = round(self._temperature * self._scale)

    def _write_register(self, register: int, value: int) -> int | None:
        """Return a Modbus exception code, or None after accepting a write."""
        if register in (p.SV_REGISTER, p.SPR_REGISTER):
            if register == p.SPR_REGISTER and value <= 0:
                return 3
        elif register == p.SRUN_REGISTER:
            if value not in (0, 1, 2):
                return 3
            previous = self._registers[p.SRUN_REGISTER]
            if value == 2 and previous != 0:
                return None  # Echo the request, but readback still reports STOP/HOLD.
            if value == 0:
                if self._registers[p.PNO_REGISTER] == 2:
                    if self._registers[p.PAF_REGISTER] & 0x06:
                        return 3
                    step = self._registers[p.PROGRAM_STEP_REGISTER]
                    self._registers[p.SV_REGISTER] = self._registers[
                        p.PROGRAM_SP1_REGISTER if step == 1 else p.PROGRAM_SP2_REGISTER
                    ]
            elif value == 1:
                self._step_elapsed = 0
        elif register == p.PNO_REGISTER:
            if value not in (0, 2):
                return 3
        elif register == p.PROGRAM_STEP_REGISTER:
            if value not in (1, 2):
                return 3
            self._step_elapsed = 0
        elif register == p.PAF_REGISTER:
            if not 0 <= value <= 255:
                return 3
        elif register == p.AMAN_REGISTER:
            if value != p.AUTOMATIC_MODE:
                return 3
        elif p.PROGRAM_DATA_FIRST_REGISTER <= register <= p.PROGRAM_DATA_LAST_REGISTER:
            if register % 2 and not 1 <= value <= p.MAX_PROGRAM_TIME_VALUE:
                return 3
        else:
            return 2
        self._registers[register] = value
        return None

    def _respond(self, packet: bytes) -> bytes:
        self._update()
        if len(packet) < 4 or _frame(packet[:-2]) != packet or packet[0] != self._address:
            return b""
        address, function = packet[:2]
        if function not in (3, 6):
            return _exception(address, function, 1)
        if len(packet) != 8:
            return _exception(address, function, 3)
        register = int.from_bytes(packet[2:4], "big")
        if function == 3:
            count = int.from_bytes(packet[4:6], "big")
            if not 1 <= count <= 125:
                return _exception(address, function, 3)
            if any(index not in self._registers for index in range(register, register + count)):
                return _exception(address, function, 2)
            data = b"".join(
                self._registers[index].to_bytes(2, "big", signed=True)
                for index in range(register, register + count)
            )
            return _frame(bytes((address, function, len(data))) + data)
        error = self._write_register(register, int.from_bytes(packet[4:6], "big", signed=True))
        return _exception(address, function, error) if error else packet


@dataclass
class _Channel:
    name: str
    setpoint: int = 0
    override: str = "normal"
    solenoid: bool = False

    @property
    def flow(self) -> int:
        if not self.solenoid or self.override == "closed":
            return 0
        return 10000 if self.override == "open" else self.setpoint


class MFCSerial(_Serial):
    """Simulate CAP units 7/8/9 and the three coils on valve unit 4.

    reply_ascii=False simulates a write-only CAP link. echo_solenoids=False
    suppresses the optional Modbus echo. Both still apply valid commands.
    respond=False suppresses every reply on either transport.
    """

    def __init__(
        self,
        *,
        gas_names: Mapping[int, str] | None = None,
        reply_ascii: bool = True,
        echo_solenoids: bool = True,
    ):
        super().__init__("sim://mfc")
        names = {unit: f"SIM GAS {index + 1}" for index, unit in enumerate(p.GAS_UNIT_IDS)}
        if gas_names is not None:
            if any(unit not in names for unit in gas_names):
                raise ValueError("MFC units must be 7, 8, or 9")
            names.update(gas_names)
        if any(
            not isinstance(name, str) or not name or not all(32 <= ord(c) <= 126 for c in name)
            for name in names.values()
        ):
            raise ValueError("Simulated gas names must contain printable ASCII")
        self._channels = {unit: _Channel(name) for unit, name in names.items()}
        self.reply_ascii = reply_ascii
        self.echo_solenoids = echo_solenoids

    def channel_state(self, unit: int) -> dict:
        """Return a snapshot. The setpoint is separate from the effective flow."""
        channel = self._channels[unit]
        return {
            "unit_id": unit,
            "gas_name": channel.name,
            "target_percent": channel.setpoint / 100,
            "actual_percent": channel.flow / 100,
            "override": channel.override,
            "solenoid_open": channel.solenoid,
        }

    def _ascii(self, packet: bytes) -> bytes:
        match = re.fullmatch(rb"\x02([0-9A-Fa-f]{2})([A-Z]{3})(?:\$([0-9A-Fa-f]+))?\r", packet)
        if match is None:
            return b"NG\r"
        address, command, data = match.groups()
        unit = int(address, 16)
        if unit not in self._channels:
            return b""
        channel = self._channels[unit]
        if command == b"SFR" and data is not None:
            value = int(data, 16)
            if value > 10000:
                return b"NG\r"
            channel.setpoint = value
        elif command == b"SCM" and data == b"1":
            pass  # The simulated enclosure starts in digital mode, as the driver expects.
        elif command in (b"SVC", b"SVO", b"SVN") and data is None:
            channel.override = {b"SVC": "closed", b"SVO": "open", b"SVN": "normal"}[command]
        elif command == b"RFR" and data is None:
            return f"N{channel.flow:X}\r".encode("ascii")
        elif command == b"RGN" and data is None:
            return b"N" + channel.name.encode("ascii") + b"\r"
        else:
            return b"NG\r"
        return b"OK\r"

    def _respond(self, packet: bytes) -> bytes:
        if packet.startswith(b"\x02"):
            response = self._ascii(packet)
            return response if self.reply_ascii else b""
        if len(packet) < 4 or _frame(packet[:-2]) != packet or packet[0] != 4:
            return b""
        function = packet[1]
        if function != 5:
            return _exception(4, function, 1)
        if len(packet) != 8:
            return _exception(4, function, 3)
        channel = int.from_bytes(packet[2:4], "big")
        if channel not in range(3):
            return _exception(4, function, 2)
        if packet[4:6] not in (b"\xff\x00", b"\x00\x00"):
            return _exception(4, function, 3)
        self._channels[p.GAS_UNIT_IDS[channel]].solenoid = packet[4:6] == b"\xff\x00"
        return packet if self.echo_solenoids else b""
