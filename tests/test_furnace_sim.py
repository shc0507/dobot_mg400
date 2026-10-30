"""Drive the real furnace/MFC code through memory-only serial protocols."""

import math
import socket
import subprocess
import sys
from types import SimpleNamespace

import minimalmodbus
import pytest
import serial

from furnace import Furnace, FurnaceConfig, MFCFlowController
from furnace import controller as p
from furnace.sim import FurnaceSerial, MFCSerial

RealInstrument = minimalmodbus.Instrument


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, clock):
    def forbidden(*args, **kwargs):
        raise AssertionError("A simulator test attempted real serial or network I/O")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(p, "time", SimpleNamespace(time=clock, sleep=clock.advance))
    monkeypatch.setattr(
        minimalmodbus, "time", SimpleNamespace(monotonic=clock, sleep=lambda seconds: None)
    )
    monkeypatch.setattr(minimalmodbus, "_latest_read_times", {})
    monkeypatch.setattr(minimalmodbus, "_serialports", {})
    for name in (
        "MG400_FURNACE_SERIAL",
        "MG400_FURNACE_SLAVE",
        "MG400_MFC_SERIAL",
        "MG400_FURNACE_MAX_C",
        "MG400_FURNACE_MAX_RAMP",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_lab(monkeypatch, clock):
    def make(*, config=None, **kwargs):
        transport = FurnaceSerial(clock=clock, **kwargs)
        mfc = MFCSerial()

        def connect(port, slave_address):
            assert port == "sim://furnace"
            return RealInstrument(transport, slave_address)

        def mfc_serial(**settings):
            assert settings["port"] is None
            assert settings["baudrate"] == 9600
            return mfc

        monkeypatch.setattr(p.minimalmodbus, "Instrument", connect)
        monkeypatch.setattr(p.serial, "Serial", mfc_serial)
        furnace = Furnace(
            config=config or FurnaceConfig(port="sim://furnace", flow_port="sim://mfc")
        )
        return SimpleNamespace(furnace=furnace, serial=transport, mfc=mfc, clock=clock)

    return make


def frame(body):
    # Check the simulator's driver-derived CRC against minimalmodbus's implementation.
    return body + minimalmodbus._calculate_crc(body)


def write(instrument, register, value):
    return p.write_register(instrument, register, value)


def test_import_and_construction_have_no_external_side_effects():
    script = """
import builtins
import pathlib
import socket
import threading
import serial

def forbidden(*args, **kwargs):
    raise AssertionError("Simulator import or construction attempted external I/O")

serial.Serial = forbidden
socket.socket = forbidden
threading.Thread.start = forbidden
pathlib.Path.write_text = forbidden
pathlib.Path.write_bytes = forbidden
builtins.open = forbidden
from furnace.sim import FurnaceSerial, MFCSerial
assert FurnaceSerial().registers[0x1B] == 1
assert all(MFCSerial().channel_state(unit)["actual_percent"] == 0 for unit in (7, 8, 9))
"""
    subprocess.run(
        [sys.executable, "-B", "-c", script], check=True, capture_output=True, timeout=10
    )


def test_minimalmodbus_uses_real_crc_frames_and_signed_readback(clock):
    transport = FurnaceSerial(clock=clock)
    instrument = RealInstrument(transport, 1)
    assert p.read_register(instrument, 0x16) == 1
    assert transport.requests[-1] == frame(bytes.fromhex("01 03 00 16 00 01"))
    assert write(instrument, p.SV_REGISTER, -123) == -123
    assert list(transport.requests)[-2:] == [
        frame(bytes.fromhex("01 06 00 00 FF 85")),
        frame(bytes.fromhex("01 03 00 00 00 01")),
    ]
    write(instrument, p.PROGRAM_SP1_REGISTER, 400)
    write(instrument, p.PROGRAM_T1_REGISTER, 15)
    assert instrument.read_registers(p.PROGRAM_SP1_REGISTER, 2) == [400, 15]


def test_driver_constructor_stops_without_connecting_gas(make_lab):
    lab = make_lab(initial_c=100)
    assert lab.furnace.connected
    assert lab.furnace.flow_controller is None
    assert lab.furnace.get_run_state() == (1, "STOP/COOLING")
    assert lab.furnace.instrument_model == p.AI_808_MODEL_ID
    assert not lab.mfc.requests
    assert lab.furnace.get_output_mode() == (1, "AUTO")
    assert lab.furnace.get_auxiliary_output_status()["event_output_status"] == 0
    assert all(lab.mfc.channel_state(unit)["actual_percent"] == 0 for unit in (7, 8, 9))


def test_stop_cools_toward_ambient_without_active_ramp_limit(make_lab):
    lab = make_lab(initial_c=100, time_constant_s=30)
    lab.furnace.set_ramp_rate(1)
    lab.clock.advance(30)
    assert lab.serial.temperature_c == pytest.approx(25 + 75 / math.e)
    assert float(lab.furnace.get_current_values()["PV"]) == pytest.approx(52.6)
    assert lab.serial.step_elapsed_s == 0


def test_cold_stopped_furnace_stays_cold_and_does_not_start_a_program(make_lab):
    lab = make_lab()
    lab.clock.advance(86400)
    assert lab.serial.temperature_c == 25
    assert lab.furnace.get_run_state()[0] == 1
    assert lab.serial.registers[p.PNO_REGISTER] == 0


def test_first_order_response_near_target_uses_time_constant(make_lab):
    lab = make_lab(time_constant_s=30)
    write(lab.furnace.controller, p.SV_REGISTER, 300)
    lab.furnace.start()
    lab.clock.advance(30)
    assert lab.serial.temperature_c == pytest.approx(30 - 5 / math.e)


def test_run_temperature_is_limited_by_ramp_rate(make_lab):
    lab = make_lab()
    lab.furnace.set_ramp_rate(10)
    lab.furnace.set_temperature(100)
    lab.clock.advance(60)
    assert lab.serial.temperature_c == pytest.approx(35)
    assert lab.furnace.get_program_step() == 1
    assert lab.serial.step_elapsed_s == 60
    assert lab.furnace.get_current_values()["Ramp"].strip() == "10.0"


def test_hold_pauses_program_but_continues_regulating_temperature(make_lab):
    lab = make_lab()
    lab.furnace.set_temperature(100)
    lab.clock.advance(60)
    before = lab.serial.temperature_c
    assert lab.furnace.set_hold() == 2
    paused = lab.serial.step_elapsed_s
    lab.clock.advance(1200)
    assert lab.serial.temperature_c > before
    assert lab.serial.temperature_c == pytest.approx(100, abs=0.001)
    assert lab.serial.step_elapsed_s == paused
    assert lab.furnace.get_program_step() == 1
    assert lab.furnace.get_run_state() == (2, "HOLD")
    lab.furnace.start()
    lab.clock.advance(450 - paused)
    assert lab.furnace.get_program_step() == 2


def test_source_two_target_program_enters_hold_only_when_driver_requests_it(make_lab):
    lab = make_lab()
    lab.furnace.set_ramp_rate(30)
    lab.furnace.set_temperature(40)
    assert lab.serial.registers[p.PROGRAM_T1_REGISTER] == 5
    assert lab.serial.registers[p.PROGRAM_T2_REGISTER] == p.MAX_PROGRAM_TIME_VALUE
    assert lab.furnace.update_temperature_change() is False
    lab.clock.advance(29.9)
    assert lab.furnace.get_program_step() == 1
    lab.clock.advance(0.1)
    assert lab.furnace.get_program_step() == 2
    assert lab.furnace.get_run_state()[0] == 0
    assert lab.serial.registers[p.SV_REGISTER] == 400
    assert lab.furnace.update_temperature_change() is True
    assert lab.furnace.get_run_state()[0] == 2
    assert lab.furnace.pending_hold_target is None
    lab.clock.advance(600)
    assert lab.serial.temperature_c == pytest.approx(40, abs=0.01)
    lab.furnace.set_stop()
    lab.clock.advance(300)
    assert lab.serial.temperature_c == pytest.approx(25, abs=0.01)


def test_program_step_transition_changes_to_second_target(clock):
    transport = FurnaceSerial(clock=clock)
    instrument = RealInstrument(transport, 1)
    for register, value in (
        (p.PNO_REGISTER, 2),
        (p.PROGRAM_SP1_REGISTER, 300),
        (p.PROGRAM_T1_REGISTER, 2),
        (p.PROGRAM_SP2_REGISTER, 400),
        (p.PROGRAM_T2_REGISTER, 32000),
        (p.SRUN_REGISTER, 0),
    ):
        write(instrument, register, value)
    clock.advance(12)
    assert p.read_register(instrument, p.PROGRAM_STEP_REGISTER) == 2
    assert p.read_register(instrument, p.SV_REGISTER) == 400
    clock.advance(60)
    assert transport.temperature_c > 30


def test_large_and_small_clock_advances_give_the_same_result():
    clocks = [Clock(), Clock()]
    models = [FurnaceSerial(clock=clock) for clock in clocks]
    for model in models:
        instrument = RealInstrument(model, 1)
        for register, value in (
            (p.PNO_REGISTER, 2),
            (p.PROGRAM_SP1_REGISTER, 700),
            (p.PROGRAM_SP2_REGISTER, 500),
            (p.PROGRAM_T1_REGISTER, 60),
            (p.SRUN_REGISTER, 0),
        ):
            write(instrument, register, value)
    clocks[0].advance(600)
    for _ in range(600):
        clocks[1].advance(1)
        models[1].registers
    assert models[0].temperature_c == pytest.approx(models[1].temperature_c, abs=1e-10)
    assert models[0].registers == models[1].registers
    assert models[0].step_elapsed_s == models[1].step_elapsed_s == 240


def test_time_scale_accelerates_temperature_and_program_without_sleeping(make_lab):
    lab = make_lab(time_scale=60)
    lab.furnace.set_temperature(35)
    lab.clock.advance(0.5)
    assert lab.furnace.get_program_step() == 1
    assert lab.serial.temperature_c == 30
    lab.clock.advance(0.5)
    assert lab.furnace.get_program_step() == 2
    assert lab.serial.temperature_c == pytest.approx(35 - 5 / math.e)


def test_driver_caps_apply_to_simulated_registers(make_lab):
    lab = make_lab(
        config=FurnaceConfig(port="sim://furnace", flow_port="sim://mfc", max_c=50, max_ramp=5)
    )
    assert lab.furnace.set_ramp_rate(100) == 5
    assert lab.furnace.set_temperature(1000) == 50
    for register in (p.SV_REGISTER, p.PROGRAM_SP1_REGISTER, p.PROGRAM_SP2_REGISTER):
        assert lab.serial.registers[register] == 500
    assert lab.serial.registers[p.SPR_REGISTER] == 50
    lab.clock.advance(600)
    assert lab.serial.temperature_c <= 50


@pytest.mark.parametrize("dpt,scale", [(0, 1), (1, 10), (128, 10), (129, 100)])
def test_driver_decodes_dpt_scaling_against_wire_registers(make_lab, dpt, scale):
    lab = make_lab(dpt=dpt)
    assert lab.furnace.get_temperature_format() == (dpt, round(math.log10(scale)), scale)
    lab.furnace.set_ramp_rate(2)
    lab.furnace.set_temperature(30)
    assert lab.serial.registers[p.SV_REGISTER] == 30 * scale
    assert lab.serial.registers[p.SPR_REGISTER] == 2 * scale


def test_hold_without_run_is_not_confirmed(make_lab):
    lab = make_lab()
    with pytest.raises(RuntimeError, match="verification failed"):
        write(lab.furnace.controller, p.SRUN_REGISTER, 2)
    assert lab.furnace.get_run_state()[0] == 1
    assert lab.furnace.set_hold() == 2


def test_hold_restores_setpoint_after_run_loads_program_target(make_lab):
    lab = make_lab()
    lab.furnace.set_temperature(40)
    write(lab.furnace.controller, p.SV_REGISTER, 350)
    assert lab.furnace.set_hold() == 2
    assert lab.serial.registers[p.SV_REGISTER] == 350
    assert lab.serial.registers[p.PROGRAM_SP1_REGISTER] == 400


def test_rejected_hold_causes_verified_stop_through_real_driver(make_lab, monkeypatch):
    lab = make_lab()
    lab.furnace.set_temperature(40)
    original_write = lab.serial._write_register

    def reject_hold(register, value):
        if (register, value) == (p.SRUN_REGISTER, 2):
            return None
        return original_write(register, value)

    monkeypatch.setattr(lab.serial, "_write_register", reject_hold)
    with pytest.raises(RuntimeError, match="Heating has been stopped"):
        lab.furnace.set_hold()
    assert lab.serial.registers[p.SRUN_REGISTER] == 1
    assert lab.furnace.pending_hold_target is None


def test_missing_response_can_follow_an_applied_stop(make_lab):
    lab = make_lab()
    lab.furnace.set_temperature(40)
    lab.serial.respond = False
    with pytest.raises(minimalmodbus.NoResponseError):
        lab.furnace.set_stop()
    assert lab.serial.registers[p.SRUN_REGISTER] == 1
    lab.serial.respond = True
    assert lab.furnace.get_run_state()[0] == 1


def test_close_without_stop_preserves_device_state_and_reopen_is_only_memory(make_lab):
    lab = make_lab()
    lab.furnace.set_temperature(100)
    lab.furnace.close(stop_output=False)
    assert not lab.serial.is_open
    lab.clock.advance(60)
    assert lab.serial.temperature_c == 35
    lab.serial.open()
    instrument = RealInstrument(lab.serial, 1)
    assert p.read_register(instrument, p.SRUN_REGISTER) == 0
    write(instrument, p.SRUN_REGISTER, 1)


def test_paf_options_are_preserved_by_driver_programming(make_lab):
    lab = make_lab()
    write(lab.furnace.controller, p.PAF_REGISTER, 0xA6)
    lab.furnace.set_temperature(40)
    assert lab.serial.registers[p.PAF_REGISTER] == 0xA0


@pytest.mark.parametrize(
    "register,value",
    [
        (p.PNO_REGISTER, 1),
        (p.PROGRAM_STEP_REGISTER, 3),
        (p.PROGRAM_T1_REGISTER, -1),
        (p.SPR_REGISTER, 0),
        (p.AMAN_REGISTER, 0),
        (p.SRUN_REGISTER, 3),
    ],
)
def test_unsupported_writes_fail_without_changing_registers(clock, register, value):
    transport = FurnaceSerial(clock=clock)
    instrument = RealInstrument(transport, 1)
    before = transport.registers
    with pytest.raises(minimalmodbus.IllegalRequestError):
        write(instrument, register, value)
    assert transport.registers == before


def test_unsupported_program_mode_does_not_start_heat(clock):
    transport = FurnaceSerial(clock=clock)
    instrument = RealInstrument(transport, 1)
    write(instrument, p.PNO_REGISTER, 2)
    write(instrument, p.PAF_REGISTER, 6)
    with pytest.raises(minimalmodbus.IllegalRequestError):
        write(instrument, p.SRUN_REGISTER, 0)
    assert transport.registers[p.SRUN_REGISTER] == 1


def test_bad_crc_wrong_address_and_unsupported_operations_do_not_mutate_device(clock):
    transport = FurnaceSerial(clock=clock)
    before = transport.registers
    valid = frame(bytes.fromhex("01 06 00 00 01 90"))
    transport.write(valid[:-1] + bytes((valid[-1] ^ 1,)))
    assert transport.read(8) == b""
    transport.write(frame(bytes.fromhex("02 06 00 00 01 90")))
    assert transport.read(8) == b""
    instrument = RealInstrument(transport, 1)
    with pytest.raises(minimalmodbus.IllegalRequestError):
        instrument.write_registers(p.SV_REGISTER, [400, 401])
    with pytest.raises(minimalmodbus.IllegalRequestError):
        write(instrument, p.PV_REGISTER, 400)
    with pytest.raises(minimalmodbus.IllegalRequestError):
        p.read_register(instrument, 0xFFFF)
    assert transport.registers == before


@pytest.mark.parametrize(
    "options",
    [
        {"time_scale": 0},
        {"time_scale": -1},
        {"time_scale": float("nan")},
        {"time_constant_s": 0},
        {"time_constant_s": float("inf")},
        {"ambient_c": float("nan")},
        {"initial_c": float("inf")},
        {"slave_address": 0},
        {"slave_address": 248},
        {"slave_address": True},
        {"dpt": -1},
        {"dpt": 5},
        {"dpt": 132},
        {"dpt": True},
        {"initial_c": 4000},
        {"clock": lambda: float("nan")},
    ],
)
def test_invalid_simulation_settings_fail_before_external_io(options):
    with pytest.raises(ValueError):
        FurnaceSerial(**options)


def test_clock_must_stay_finite_and_monotonic(clock):
    model = FurnaceSerial(clock=clock)
    clock.advance(1)
    model.registers
    clock.now = 0
    with pytest.raises(ValueError, match="backwards"):
        model.registers
    clock.now = float("nan")
    with pytest.raises(ValueError, match="finite"):
        model.registers


def test_instances_and_inspection_copies_do_not_share_state(clock):
    first = FurnaceSerial(clock=clock)
    second = FurnaceSerial(clock=clock)
    write(RealInstrument(first, 1), p.SV_REGISTER, 400)
    values = first.registers
    values[p.SV_REGISTER] = 900
    assert first.registers[p.SV_REGISTER] == 400
    assert second.registers[p.SV_REGISTER] == 250


@pytest.mark.parametrize("channel,percent", [(0, 25), (1, 50), (2, 100)])
def test_furnace_controls_fixed_gas_channel_and_reads_actual_flow(make_lab, channel, percent):
    lab = make_lab()
    flow = lab.furnace.connect_flow_controller()
    assert lab.furnace.set_gas_flow(channel, percent) == percent
    unit = 7 + channel
    assert lab.furnace.get_gas_flow(channel)["actual_percent"] == percent
    state = lab.mfc.channel_state(unit)
    assert state["target_percent"] == percent
    assert state["solenoid_open"]
    assert flow.read_gas_name(unit) == f"SIM GAS {channel + 1}"
    assert list(lab.mfc.requests)[:2] == [
        f"\x02{unit:02X}SFR${round(percent * 100):X}\r".encode(),
        frame(bytes((4, 5, 0, channel, 255, 0))),
    ]
    lab.furnace.stop_gas_flow(channel)
    assert flow.read_flow_percent(unit) == 0
    assert lab.mfc.channel_state(unit)["target_percent"] == 0


def test_mfc_setpoint_override_and_external_solenoid_are_independent():
    transport = MFCSerial()
    flow = MFCFlowController(serial_connection=transport)
    assert flow.set_flow_percent(7, 37.5) == 37.5
    assert transport.channel_state(7)["target_percent"] == 37.5
    assert flow.read_flow_percent(7) == 0
    flow.set_gas_solenoid(0, True)
    assert flow.read_flow_percent(7) == 37.5
    flow.set_gas_valve(0, True)
    assert flow.read_flow_percent(7) == 100
    flow.set_flow_percent_unconfirmed(7, 60)
    assert transport.channel_state(7)["override"] == "normal"
    assert flow.read_flow_percent(7) == 60
    flow.set_gas_valve(0, False)
    flow.set_flow_percent_unconfirmed(7, 20)
    assert transport.channel_state(7)["override"] == "normal"
    assert flow.read_flow_percent(7) == 0


def test_write_only_mfc_and_optional_solenoid_echo_keep_unverified_commands():
    transport = MFCSerial(reply_ascii=False, echo_solenoids=False)
    flow = MFCFlowController(serial_connection=transport)
    flow.set_flow_percent_unconfirmed(9, 60)
    result = flow.set_gas_solenoid(2, True)
    assert result["verified"] is False
    assert transport.channel_state(9)["actual_percent"] == 60
    with pytest.raises(RuntimeError, match="empty response"):
        flow.read_flow_percent(9)
    assert list(transport.requests)[-4:] == [b"\x0209RFR\r"] * 4
    flow.close()
    assert not transport.is_open
    assert transport.channel_state(9)["actual_percent"] == 0
    assert transport.channel_state(9)["target_percent"] == 0


def test_driver_close_stops_all_controlled_flows_and_closes_both_transports(make_lab):
    lab = make_lab()
    lab.furnace.connect_flow_controller()
    lab.furnace.set_temperature(50)
    for channel in range(3):
        lab.furnace.set_gas_flow(channel, 20 * (channel + 1))
    lab.furnace.close()
    assert not lab.serial.is_open
    assert not lab.mfc.is_open
    assert lab.serial.registers[p.SRUN_REGISTER] == 1
    for unit in (7, 8, 9):
        state = lab.mfc.channel_state(unit)
        assert state["actual_percent"] == state["target_percent"] == 0
        assert not state["solenoid_open"]


def test_water_guard_uses_simulated_identity_before_setting_flow():
    gas = MFCFlowController(serial_connection=MFCSerial())
    with pytest.raises(ValueError, match="no water command"):
        gas.set_water_flow(7, 30)
    assert list(gas.serial.requests) == [b"\x0207RGN\r"]
    water = MFCFlowController(serial_connection=MFCSerial(gas_names={7: "WATER"}))
    water.set_gas_solenoid(0, True)
    assert water.set_water_flow(7, 30) == 30
    assert water.channel_status(7)["actual_percent"] == 30


@pytest.mark.parametrize(
    "packet",
    [
        b"\x0207SFR$2711\r",
        b"\x0207SFR$-1\r",
        b"\x0207SFR\r",
        b"\x0207SCM$2\r",
        b"\x0207SVO$1\r",
        b"\x0207XXX\r",
        b"\x0207RFR$1\r",
        b"\x0207SFR$100\n",
    ],
)
def test_invalid_ascii_packets_do_not_change_flow(packet):
    transport = MFCSerial()
    before = transport.channel_state(7)
    transport.write(packet)
    assert transport.read_until(b"\r") == b"NG\r"
    assert transport.channel_state(7) == before


def test_unknown_mfc_unit_and_bad_solenoid_packets_have_no_effect():
    transport = MFCSerial()
    transport.write(b"\x020ASFR$100\r")
    assert transport.read_until(b"\r") == b""
    packet = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
    transport.write(packet[:-1] + b"\x00")
    assert transport.read(8) == b""
    for body, exception in (
        (bytes.fromhex("04 05 00 03 FF 00"), 2),
        (bytes.fromhex("04 05 00 00 00 01"), 3),
    ):
        transport.write(frame(body))
        assert transport.read(8) == frame(bytes((4, 0x85, exception)))
    assert all(not transport.channel_state(unit)["solenoid_open"] for unit in (7, 8, 9))
    transport.write(packet)
    assert transport.read(8) == packet
    assert transport.channel_state(7)["solenoid_open"]


def test_serial_buffer_reads_reset_close_and_reopen_are_in_memory():
    transport = MFCSerial()
    assert transport.write(b"\x0207RGN\r") == 7
    assert transport.in_waiting == len(b"NSIM GAS 1\r")
    assert transport.read(2) == b"NS"
    assert transport.read_until(b"\r") == b"IM GAS 1\r"
    assert transport.read(1) == b""
    transport.write(b"\x0207RGN\r")
    transport.reset_input_buffer()
    assert transport.in_waiting == 0
    transport.close()
    for operation in (lambda: transport.write(b"\x0207RFR\r"), transport.read, transport.flush):
        with pytest.raises(OSError, match="closed"):
            operation()
    transport.open()
    transport.write(b"\x0207RFR\r")
    assert transport.read_until(b"\r") == b"N0\r"


@pytest.mark.parametrize("names", [{10: "SIM"}, {7: ""}, {8: "bad\rname"}, {9: "气"}])
def test_invalid_simulated_gas_identity_is_rejected(names):
    with pytest.raises(ValueError):
        MFCSerial(gas_names=names)
