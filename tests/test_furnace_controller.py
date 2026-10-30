import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from furnace import controller as driver
from furnace.config import FurnaceConfig
from furnace.controller import Furnace, MFCFlowController


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Block serial constructors unless a test explicitly installs a fake."""

    def forbidden(*args, **kwargs):
        raise AssertionError("A test attempted to open a real serial port")

    for name in (
        "MG400_FURNACE_SERIAL",
        "MG400_FURNACE_SLAVE",
        "MG400_MFC_SERIAL",
        "MG400_FURNACE_MAX_C",
        "MG400_FURNACE_MAX_RAMP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(driver.minimalmodbus, "Instrument", forbidden)
    monkeypatch.setattr(driver.serial, "Serial", forbidden)
    monkeypatch.setattr(driver.time, "sleep", lambda delay: None)


class FakeSerial:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []
        self.is_open = True
        self.timeout = 1.0

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, packet):
        self.writes.append(packet)

    def flush(self):
        pass

    def read_until(self, delimiter):
        return self.responses.pop(0)

    def read(self, size):
        return self.responses.pop(0)

    def close(self):
        self.is_open = False


class MFCFlowControllerTests(unittest.TestCase):
    def test_gas_three_mode_and_setpoint_writes_allow_controller_to_settle(self):
        events = []

        class RecordingSerial(FakeSerial):
            def write(self, packet):
                events.append(("write", packet))
                super().write(packet)

            def flush(self):
                events.append(("flush",))

        controller = MFCFlowController(serial_connection=RecordingSerial([]))
        with patch(
            "furnace.controller.time.sleep",
            side_effect=lambda delay: events.append(("wait", delay)),
        ):
            controller.set_valve_override(9, False)
            controller.set_flow_percent_unconfirmed(9, 20)

        self.assertEqual(
            events,
            [
                ("write", b"\x0209SVC\r"),
                ("flush",),
                ("wait", 0.08),
                ("write", b"\x0209SVN\r"),
                ("flush",),
                ("wait", 0.08),
                ("write", b"\x0209SFR$7D0\r"),
                ("flush",),
                ("wait", 0.08),
            ],
        )

    def test_company_gas_solenoid_packet_uses_fixed_channel_and_crc(self):
        expected = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([expected])
        controller = MFCFlowController(serial_connection=serial)

        result = controller.set_gas_solenoid(0, True)

        self.assertEqual(serial.writes, [expected])
        self.assertTrue(result["open"])
        self.assertTrue(result["verified"])

    def test_solenoid_command_can_be_sent_when_device_does_not_echo(self):
        expected = bytes.fromhex("04 05 00 02 00 00 6C 5F")
        serial = FakeSerial([b""])
        controller = MFCFlowController(serial_connection=serial)

        result = controller.set_gas_solenoid(2, False)

        self.assertEqual(serial.writes, [expected])
        self.assertFalse(result["verified"])

    def test_open_valve_controls_mfc_override_before_external_solenoid(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([solenoid])
        controller = MFCFlowController(serial_connection=serial)

        result = controller.set_gas_valve(0, True)

        self.assertEqual(serial.writes, [solenoid, b"\x0207SVO\r"])
        self.assertTrue(result["open"])
        self.assertEqual(result["mfc_valve"]["command"], "SVO")

    def test_flow_target_restores_normal_mfc_valve_after_manual_open(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([b"", solenoid, b""])
        controller = MFCFlowController(serial_connection=serial)

        controller.set_gas_valve(0, True)
        controller.set_flow_percent_unconfirmed(7, 60)

        self.assertEqual(
            serial.writes,
            [
                solenoid,
                b"\x0207SVO\r",
                b"\x0207SVN\r",
                b"\x0207SFR$1770\r",
            ],
        )

    def test_flow_target_restores_normal_mfc_valve_after_manual_close(self):
        open_packet = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        close_packet = bytes.fromhex("04 05 00 00 00 00 CD 9F")
        serial = FakeSerial([open_packet, close_packet])
        controller = MFCFlowController(serial_connection=serial)

        controller.set_gas_valve(0, True)
        controller.set_gas_valve(0, False)
        controller.set_flow_percent_unconfirmed(7, 60)

        self.assertEqual(
            serial.writes,
            [
                open_packet,
                b"\x0207SVO\r",
                close_packet,
                b"\x0207SVC\r",
                b"\x0207SVN\r",
                b"\x0207SFR$1770\r",
            ],
        )

    def test_sets_percent_with_manual_defined_framing(self):
        serial = FakeSerial(
            [
                b"OK\r",
            ]
        )
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.set_flow_percent_unconfirmed(7, 25), 25)
        self.assertEqual(
            serial.writes,
            [
                b"\x0207SFR$9C4\r",
            ],
        )

    def test_rejects_percentage_above_100_before_opening(self):
        serial = FakeSerial([])
        controller = MFCFlowController(serial_connection=serial)

        with self.assertRaisesRegex(ValueError, "Flow percentage"):
            controller.set_flow_percent_unconfirmed(7, 100.01)
        self.assertEqual(serial.writes, [])

    def test_stop_uses_cap_zero_setpoint_packet(self):
        serial = FakeSerial([b"OK\r"])
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.stop_flow(7), 0.0)
        self.assertEqual(
            serial.writes,
            [
                b"\x0207SFR$0\r",
            ],
        )

    def test_fixed_gas_channels_are_cap_addresses_07_to_09(self):
        self.assertEqual(
            [MFCFlowController.gas_unit_id(channel) for channel in range(3)],
            [7, 8, 9],
        )

    def test_fixed_gas_status_reports_only_live_percentage(self):
        serial = FakeSerial([b"N1388\r"])
        controller = MFCFlowController(serial_connection=serial)

        status = controller.gas_channel_status(0)

        self.assertEqual(serial.writes, [b"\x0207RFR\r"])
        self.assertEqual(status["actual_percent"], 50.0)
        self.assertEqual(
            set(status),
            {
                "channel_index",
                "channel_number",
                "unit_id",
                "gas_name",
                "actual_percent",
                "controller_status",
                "available",
                "error",
                "write_only",
            },
        )

    def test_furnace_sets_fixed_channel_flow_then_opens_its_solenoid(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([solenoid, b"OK\r"])
        flow = MFCFlowController(serial_connection=serial)
        furnace = Furnace.__new__(Furnace)
        furnace.flow_controller = flow

        self.assertEqual(furnace.set_gas_flow(0, 80), 80)
        self.assertEqual(
            serial.writes,
            [
                b"\x0207SFR$1F40\r",
                solenoid,
            ],
        )

    def test_furnace_sets_full_scale_as_100_percent_without_mfc_reply(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([solenoid, b"OK\r"])
        flow = MFCFlowController(serial_connection=serial)
        furnace = Furnace.__new__(Furnace)
        furnace.flow_controller = flow

        self.assertEqual(furnace.set_gas_flow(0, 100), 100)
        self.assertEqual(
            serial.writes,
            [
                b"\x0207SFR$2710\r",
                solenoid,
            ],
        )

    def test_alarm_status_still_allows_a_flow_reading(self):
        serial = FakeSerial([b"A1388\r"])
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.read_flow_percent(7), 50.0)
        self.assertEqual(controller._last_status[7], "A")

    def test_ascii_read_retries_a_silent_first_attempt(self):
        serial = FakeSerial([b"", b"N1388\r"])
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.read_flow_percent(7), 50.0)
        self.assertEqual(serial.writes, [b"\x0207RFR\r", b"\x0207RFR\r"])

    def test_rejects_invalid_percent_and_unit_without_serial_io(self):
        serial = FakeSerial([])
        controller = MFCFlowController(serial_connection=serial)

        with self.assertRaises(ValueError):
            controller.set_flow_percent(0, 10)
        with self.assertRaises(ValueError):
            controller.set_flow_percent(1, 100.01)
        with self.assertRaises(ValueError):
            controller.set_flow_percent_unconfirmed(7, 100.01)
        self.assertEqual(serial.writes, [])

    def test_unconfirmed_percent_setpoint_uses_percent_register_scale(self):
        serial = FakeSerial([])
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.set_flow_percent_unconfirmed(7, 37.5), 37.5)
        self.assertEqual(serial.writes, [b"\x0207SFR$EA6\r"])

    def test_furnace_init_does_not_auto_connect_flow_controller(self):
        with (
            patch("furnace.controller.connect", return_value=object()) as connect_mock,
            patch.object(Furnace, "disable_internal_program"),
            patch.object(Furnace, "_detect_program_setpoints", return_value=()),
            patch.object(Furnace, "set_stop"),
            patch.object(Furnace, "connect_flow_controller") as flow_connect,
        ):
            furnace = Furnace(
                port="/dev/serial/by-id/furnace-test",
                slave_address=1,
                flow_port="/dev/serial/by-id/mfc-test",
            )

        connect_mock.assert_called_once_with(port="/dev/serial/by-id/furnace-test", slave_address=1)
        flow_connect.assert_not_called()
        self.assertIsNotNone(furnace.controller)


class FakeInstrument:
    """In-memory signed holding registers with source protocol assertions."""

    def __init__(self, values=None):
        self.values = {
            0x16: 1,
            driver.MODEL_ID_REGISTER: driver.AI_808_MODEL_ID,
            driver.PNO_REGISTER: 0,
            driver.DPT_REGISTER: 1,
            driver.PV_REGISTER: 200,
            driver.SV_REGISTER: 250,
            driver.SPR_REGISTER: 100,
            driver.SRUN_REGISTER: 0,
            driver.PAF_REGISTER: 0xA6,
        }
        self.values.update(values or {})
        self.events = []
        self.failed_reads = set()
        self.reject_writes = set()
        self.on_write = None
        self.serial = SimpleNamespace(is_open=True, close=self.close)

    def close(self):
        self.serial.is_open = False
        self.events.append(("close",))

    def read_register(self, *, registeraddress, functioncode, signed):
        assert functioncode == 3 and signed is True
        self.events.append(("read", registeraddress))
        if registeraddress in self.failed_reads:
            raise OSError("fake register unavailable")
        return self.values.get(registeraddress, 0)

    def write_register(self, *, registeraddress, value, number_of_decimals, functioncode, signed):
        assert number_of_decimals == 0 and functioncode == 6 and signed is True
        self.events.append(("write", registeraddress, value))
        if registeraddress not in self.reject_writes:
            self.values[registeraddress] = value
        if self.on_write is not None:
            self.on_write(registeraddress, value)

    @property
    def writes(self):
        return [event[1:] for event in self.events if event[0] == "write"]


@pytest.fixture
def make_furnace(monkeypatch):
    def make(*, config=None, values=None):
        instrument = FakeInstrument(values)
        monkeypatch.setattr(driver.minimalmodbus, "Instrument", lambda *args: instrument)
        furnace = Furnace(config=config or FurnaceConfig(port="/dev/serial/by-id/fake-furnace"))
        instrument.events.clear()
        return furnace, instrument

    return make


def test_defaults_need_explicit_ports_and_imports_are_inert():
    code = """
import builtins
import socket
import threading
import minimalmodbus
import serial

def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted I/O or started a thread")

minimalmodbus.Instrument = forbidden
serial.Serial = forbidden
socket.socket = forbidden
threading.Thread.start = forbidden
builtins.open = forbidden
import furnace
import furnace.config
import furnace.controller
assert furnace.FurnaceConfig().port is None
assert furnace.FurnaceConfig().flow_port is None
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, text=True)
    config = FurnaceConfig.from_env()
    assert config == FurnaceConfig()
    assert (config.slave_address, config.max_c, config.max_ramp) == (1, 1000, 100)
    with pytest.raises(FrozenInstanceError):
        config.max_c = 1001
    for operation in (Furnace, driver.connect, MFCFlowController):
        with pytest.raises(ValueError, match="SERIAL"):
            operation()


def test_environment_settings_and_explicit_port_precedence(monkeypatch):
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/serial/by-id/env-furnace")
    monkeypatch.setenv("MG400_FURNACE_SLAVE", "3")
    monkeypatch.setenv("MG400_MFC_SERIAL", "/dev/serial/by-id/env-mfc")
    monkeypatch.setenv("MG400_FURNACE_MAX_C", "600.5")
    monkeypatch.setenv("MG400_FURNACE_MAX_RAMP", "12.5")
    config = FurnaceConfig.from_env()
    assert config == FurnaceConfig(
        "/dev/serial/by-id/env-furnace", 3, "/dev/serial/by-id/env-mfc", 600.5, 12.5
    )
    explicit = FurnaceConfig.from_env(port="/dev/furnace", slave_address=7, flow_port="/dev/mfc")
    assert (explicit.port, explicit.slave_address, explicit.flow_port) == (
        "/dev/furnace",
        7,
        "/dev/mfc",
    )
    monkeypatch.setenv("MG400_FURNACE_MAX_C", "500")
    assert FurnaceConfig.from_env().max_c == 500
    assert config.max_c == 600.5


@pytest.mark.parametrize(
    "setting,maximum",
    [
        ("MG400_FURNACE_MAX_C", 1000),
        ("MG400_FURNACE_MAX_RAMP", 100),
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "", "bad", "1001"])
def test_invalid_caps_fail_before_opening(monkeypatch, setting, maximum, value):
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/fake")
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=setting):
        Furnace()
    field = "max_c" if maximum == 1000 else "max_ramp"
    for invalid in (value, maximum + 0.01, True):
        with pytest.raises(ValueError, match=setting):
            FurnaceConfig(**{field: invalid})


@pytest.mark.parametrize("value", ["0", "248", "1.5", "nan", "inf", "", "bad"])
def test_invalid_slave_rejected_before_opening(monkeypatch, value):
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/fake")
    monkeypatch.setenv("MG400_FURNACE_SLAVE", value)
    with pytest.raises(ValueError, match="SLAVE"):
        driver.connect()
    with pytest.raises(ValueError, match="SLAVE"):
        Furnace()


@pytest.mark.parametrize("port", ["", "   ", "\x00invalid"])
def test_explicit_invalid_ports_do_not_fall_back_to_environment(monkeypatch, port):
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/fake-furnace")
    monkeypatch.setenv("MG400_MFC_SERIAL", "/dev/fake-mfc")
    for operation in (Furnace, driver.connect, MFCFlowController):
        with pytest.raises(ValueError, match="serial port"):
            operation(port=port)


def test_connect_uses_argument_and_signed_register_verification(monkeypatch):
    instrument = FakeInstrument({0x16: 7})
    calls = []

    def create(port, slave_address):
        calls.append((port, slave_address))
        return instrument

    monkeypatch.setattr(driver.minimalmodbus, "Instrument", create)
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/env")
    monkeypatch.setenv("MG400_FURNACE_SLAVE", "5")
    assert driver.connect("/dev/explicit", 7) is instrument
    assert calls == [("/dev/explicit", 7)]
    assert instrument.mode == driver.minimalmodbus.MODE_RTU
    assert instrument.clear_buffers_before_each_transaction is True
    assert (instrument.serial.baudrate, instrument.serial.timeout) == (9600, 1.0)
    assert instrument.events == [("read", 0x16)]
    instrument.values[0x16] = 5
    driver.connect()
    assert calls[-1] == ("/dev/env", 5)
    instrument.values[0x16] = 8
    with pytest.raises(RuntimeError, match="Expected slave address"):
        driver.connect("/dev/mismatch", 7)
    assert instrument.serial.is_open is False


def test_write_register_requires_exact_readback():
    instrument = FakeInstrument({driver.SV_REGISTER: -50})
    assert driver.read_register(instrument, driver.SV_REGISTER) == -50
    assert driver.write_register(instrument, driver.SV_REGISTER, -25) == -25
    assert instrument.events[-2:] == [("write", driver.SV_REGISTER, -25), ("read", 0)]
    instrument.reject_writes.add(driver.SV_REGISTER)
    with pytest.raises(RuntimeError, match="verification failed"):
        driver.write_register(instrument, driver.SV_REGISTER, 400)


def test_constructor_stops_and_never_opens_mfc(monkeypatch):
    instrument = FakeInstrument(
        {driver.MODEL_ID_REGISTER: driver.AI_518P_MODEL_ID, driver.PNO_REGISTER: 2}
    )
    calls = []

    def create(port, slave):
        calls.append((port, slave))
        return instrument

    monkeypatch.setattr(driver.minimalmodbus, "Instrument", create)
    monkeypatch.setenv("MG400_FURNACE_SERIAL", "/dev/env-furnace")
    monkeypatch.setenv("MG400_MFC_SERIAL", "/dev/env-mfc")
    furnace = Furnace(port="/dev/explicit-furnace", flow_port="/dev/explicit-mfc")
    assert calls == [("/dev/explicit-furnace", 1)]
    assert furnace.flow_port == "/dev/explicit-mfc"
    assert furnace.flow_controller is None
    assert furnace.program_mode_disabled is True
    assert instrument.writes == [
        (driver.SRUN_REGISTER, 1),
        (driver.PNO_REGISTER, 0),
        (driver.SRUN_REGISTER, 1),
    ]
    instrument.events.clear()
    furnace.close()
    assert instrument.events == [
        ("write", driver.SRUN_REGISTER, 1),
        ("read", driver.SRUN_REGISTER),
        ("close",),
    ]


def test_constructor_closes_on_failed_stop(monkeypatch):
    instrument = FakeInstrument()
    instrument.reject_writes.add(driver.SRUN_REGISTER)
    monkeypatch.setattr(driver.minimalmodbus, "Instrument", lambda *args: instrument)
    with pytest.raises(RuntimeError, match="verification failed"):
        Furnace(port="/dev/fake")
    assert not instrument.serial.is_open


def test_mfc_port_and_inactive_modem_lines_are_set_before_open(monkeypatch):
    opened = []

    class Port(FakeSerial):
        def __init__(self, **kwargs):
            super().__init__([])
            assert kwargs["port"] is None
            assert kwargs["baudrate"] == 9600
            assert kwargs["timeout"] == kwargs["write_timeout"] == 0.2

        def open(self):
            opened.append((self.port, self.dtr, self.rts))

    monkeypatch.setattr(driver.serial, "Serial", Port)
    monkeypatch.setenv("MG400_MFC_SERIAL", "/dev/env-mfc")
    explicit = MFCFlowController(port="/dev/explicit-mfc")
    env = MFCFlowController()
    assert opened == [("/dev/explicit-mfc", False, False), ("/dev/env-mfc", False, False)]
    assert explicit.port == "/dev/explicit-mfc"
    assert env.port == "/dev/env-mfc"
    explicit.close()
    env.close()


def test_furnace_flow_port_is_used_and_an_open_port_cannot_be_ignored(make_furnace, monkeypatch):
    furnace, _ = make_furnace(config=FurnaceConfig(port="/dev/furnace", flow_port="/dev/mfc"))
    calls = []

    class Controller:
        def __init__(self, port):
            calls.append(port)
            self.port = port

        def close(self, *, stop_output):
            calls.append(("close", stop_output))

    monkeypatch.setattr(driver, "MFCFlowController", Controller)
    first = furnace.connect_flow_controller()
    assert calls == ["/dev/mfc"]
    assert furnace.connect_flow_controller("/dev/mfc") is first
    with pytest.raises(RuntimeError, match="Disconnect"):
        furnace.connect_flow_controller("/dev/other-mfc")
    furnace.disconnect_flow_controller()
    furnace.connect_flow_controller("/dev/other-mfc")
    assert calls == ["/dev/mfc", ("close", True), "/dev/other-mfc"]
    assert furnace.flow_port == "/dev/other-mfc"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "bad"])
def test_nonfinite_setter_inputs_fail_before_any_io(make_furnace, value):
    furnace, instrument = make_furnace()
    for operation, args in (
        (furnace.set_temperature, (value,)),
        (furnace.set_ramp_rate, (value,)),
        (furnace.change_temperature, (value, 1)),
        (furnace.change_temperature, (1, value)),
    ):
        with pytest.raises(ValueError):
            operation(*args)
    assert instrument.events == []


def test_configured_caps_clamp_setters_and_preserve_program_order(make_furnace):
    config = FurnaceConfig(port="/dev/fake", max_c=60, max_ramp=5)
    furnace, instrument = make_furnace(config=config)
    assert furnace.set_ramp_rate(100) == 5
    assert instrument.writes == [(driver.SPR_REGISTER, 50)]
    instrument.events.clear()
    assert furnace.set_temperature(900) == 60
    assert instrument.writes == [
        (driver.SRUN_REGISTER, 1),
        (driver.AMAN_REGISTER, 1),
        (driver.PAF_REGISTER, 0xA0),
        (driver.PNO_REGISTER, 2),
        (driver.PROGRAM_SP1_REGISTER, 600),
        (driver.PROGRAM_T1_REGISTER, 80),
        (driver.PROGRAM_SP2_REGISTER, 600),
        (driver.PROGRAM_T2_REGISTER, 32000),
        (driver.PROGRAM_STEP_REGISTER, 1),
        (driver.SV_REGISTER, 600),
        (driver.SRUN_REGISTER, 0),
    ]
    for index, event in enumerate(instrument.events):
        if event[0] == "write":
            assert instrument.events[index + 1] == ("read", event[1])
    assert furnace.pending_hold_target == 60
    assert furnace.controller_program_step == 1
    assert furnace._program_bank_supported is True


def test_fractional_caps_never_round_above_limit(make_furnace):
    config = FurnaceConfig(port="/dev/fake", max_c=60.06, max_ramp=5.06)
    furnace, instrument = make_furnace(config=config)
    furnace.set_ramp_rate(100)
    furnace.set_temperature(1000)
    assert instrument.values[driver.SPR_REGISTER] == 50
    assert instrument.values[driver.SV_REGISTER] == 600
    for register in (driver.PROGRAM_SP1_REGISTER, driver.PROGRAM_SP2_REGISTER):
        assert instrument.values[register] / 10 <= config.max_c


def test_ramp_below_resolution_does_not_program_zero(make_furnace):
    furnace, instrument = make_furnace(config=FurnaceConfig(port="/dev/fake", max_ramp=0.05))
    with pytest.raises(ValueError, match="resolution"):
        furnace.set_ramp_rate(100)
    assert instrument.writes == []


@pytest.mark.parametrize("rate", [0, -1, 1001])
def test_temperature_refuses_invalid_or_over_cap_existing_ramp(make_furnace, rate):
    furnace, instrument = make_furnace(values={driver.SPR_REGISTER: rate})
    with pytest.raises((ValueError, RuntimeError)):
        furnace.set_temperature(50)
    assert instrument.writes == []


def test_relative_temperature_uses_configured_caps(make_furnace):
    furnace, instrument = make_furnace(config=FurnaceConfig(port="/dev/fake", max_c=40, max_ramp=2))
    assert furnace.change_temperature(500, 100) == 40
    assert instrument.values[driver.SPR_REGISTER] == 20
    assert instrument.values[driver.SV_REGISTER] == 400


@pytest.mark.parametrize("method", ["start", "set_hold"])
@pytest.mark.parametrize(
    "register,value",
    [
        (driver.SV_REGISTER, 501),
        (driver.SPR_REGISTER, 101),
    ],
)
def test_run_and_hold_refuse_live_values_above_configured_caps(
    make_furnace, method, register, value
):
    furnace, instrument = make_furnace(
        config=FurnaceConfig(port="/dev/fake", max_c=50, max_ramp=10)
    )
    instrument.values[register] = value
    with pytest.raises((ValueError, RuntimeError), match="at most"):
        getattr(furnace, method)()
    assert (driver.SRUN_REGISTER, 0) not in instrument.writes
    if method == "set_hold":
        assert instrument.writes == [(driver.SRUN_REGISTER, 1)]


@pytest.mark.parametrize("method", ["start", "set_hold"])
@pytest.mark.parametrize("model", [driver.AI_518P_MODEL_ID, driver.AI_808_MODEL_ID])
def test_run_checks_active_program_targets_when_bank_is_supported(make_furnace, method, model):
    furnace, instrument = make_furnace(
        values={
            driver.MODEL_ID_REGISTER: model,
            driver.PNO_REGISTER: 2,
            driver.PROGRAM_SP1_REGISTER: 250,
            driver.PROGRAM_SP2_REGISTER: 250,
        }
    )
    instrument.values[driver.PNO_REGISTER] = 2
    instrument.values[driver.PROGRAM_SP2_REGISTER] = 10001
    with pytest.raises((ValueError, RuntimeError), match="Active program temperature"):
        getattr(furnace, method)()
    assert (driver.SRUN_REGISTER, 0) not in instrument.writes


@pytest.mark.parametrize("count", [-1, driver.MAX_PROGRAM_SETPOINTS + 1])
def test_run_refuses_unknown_active_bank_size(make_furnace, count):
    furnace, instrument = make_furnace(values={driver.MODEL_ID_REGISTER: driver.AI_518P_MODEL_ID})
    instrument.values[driver.PNO_REGISTER] = count
    with pytest.raises(RuntimeError, match="Pno"):
        furnace.start()
    assert instrument.writes == []


def test_programming_establishes_bank_support_for_later_run(make_furnace):
    furnace, instrument = make_furnace()
    assert furnace._program_bank_supported is False
    furnace.set_temperature(40)
    assert furnace._program_bank_supported is True
    instrument.values[driver.PROGRAM_SP1_REGISTER] = 10001
    instrument.events.clear()
    with pytest.raises(ValueError, match="Active program temperature"):
        furnace.start()
    assert instrument.writes == []


def test_no_bank_model_retains_run_and_hold_without_pno_access(make_furnace):
    furnace, instrument = make_furnace()
    instrument.failed_reads.add(driver.PNO_REGISTER)
    assert furnace.start() == 0
    assert furnace.set_hold() == 2
    assert ("read", driver.PNO_REGISTER) not in instrument.events


def test_hold_preserves_run_delay_hold_restore_and_verification(make_furnace, monkeypatch):
    furnace, instrument = make_furnace()
    monkeypatch.setattr(
        driver.time, "sleep", lambda value: instrument.events.append(("wait", value))
    )

    def load_program_target(register, value):
        if (register, value) == (driver.SRUN_REGISTER, 0):
            instrument.values[driver.SV_REGISTER] = 300

    instrument.on_write = load_program_target
    assert furnace.set_hold() == 2
    assert instrument.values[driver.SV_REGISTER] == 250
    assert instrument.writes == [
        (driver.SRUN_REGISTER, 0),
        (driver.SRUN_REGISTER, 2),
        (driver.SV_REGISTER, 250),
    ]
    run_index = instrument.events.index(("write", driver.SRUN_REGISTER, 0))
    assert instrument.events[run_index:] == [
        ("write", driver.SRUN_REGISTER, 0),
        ("read", driver.SRUN_REGISTER),
        ("wait", 0.15),
        ("write", driver.SRUN_REGISTER, 2),
        ("read", driver.SRUN_REGISTER),
        ("write", driver.SV_REGISTER, 250),
        ("read", driver.SV_REGISTER),
        ("read", driver.SRUN_REGISTER),
    ]


@pytest.mark.parametrize("sv_reads", [(1000, 250), (250, 1000)])
def test_hold_checks_both_preserved_and_live_setpoints(make_furnace, monkeypatch, sv_reads):
    furnace, instrument = make_furnace(config=FurnaceConfig(port="/dev/fake", max_c=50))
    original_read = instrument.read_register
    values = iter(sv_reads)

    def changing_read(**kwargs):
        value = original_read(**kwargs)
        if kwargs["registeraddress"] == driver.SV_REGISTER:
            return next(values)
        return value

    monkeypatch.setattr(instrument, "read_register", changing_read)
    with pytest.raises(RuntimeError, match="at most 50"):
        furnace.set_hold()
    assert instrument.writes == [(driver.SRUN_REGISTER, 1)]


@pytest.mark.parametrize("stop_fails", [False, True])
def test_hold_failure_always_attempts_stop(make_furnace, stop_fails):
    furnace, instrument = make_furnace()

    def reject_hold(register, value):
        if (register, value) == (driver.SRUN_REGISTER, 2):
            instrument.values[driver.SRUN_REGISTER] = 0
        if stop_fails and (register, value) == (driver.SRUN_REGISTER, 1):
            instrument.values[driver.SRUN_REGISTER] = 0

    instrument.on_write = reject_hold
    message = "STOP was also attempted but failed" if stop_fails else "Heating has been stopped"
    with pytest.raises(RuntimeError, match=message):
        furnace.set_hold()
    assert instrument.writes[-1] == (driver.SRUN_REGISTER, 1)
    assert instrument.writes[:2] == [(driver.SRUN_REGISTER, 0), (driver.SRUN_REGISTER, 2)]


def test_step_two_enters_hold_and_clears_pending_target(make_furnace):
    furnace, instrument = make_furnace()
    assert furnace.update_temperature_change() is False
    furnace.set_temperature(50)
    instrument.values[driver.PROGRAM_STEP_REGISTER] = 2
    assert furnace.update_temperature_change() is True
    assert furnace.pending_hold_target is None
    assert furnace.controller_program_step == 2
    assert instrument.values[driver.SRUN_REGISTER] == 2


def test_solenoid_bad_echo_still_closes_mfc_override():
    serial = FakeSerial([b"bad echo"])
    flow = MFCFlowController(serial_connection=serial)
    with pytest.raises(RuntimeError):
        flow.set_gas_valve(0, False)
    assert serial.writes[-1] == b"\x0207SVC\r"


def test_verified_flow_failure_recloses_valve():
    serial = FakeSerial([b"OK\r", b"NG\r", b"OK\r"])
    flow = MFCFlowController(serial_connection=serial)
    with pytest.raises(RuntimeError, match="NG response"):
        flow.set_flow_percent(7, 50)
    assert serial.writes == [b"\x0207SVC\r", b"\x0207SCM$1\r", b"\x0207SVC\r"]


@pytest.mark.parametrize("state", [0, 2])
def test_attach_preserves_running_controller_and_program_without_writes(state):
    instrument = FakeInstrument(
        {
            driver.MODEL_ID_REGISTER: driver.AI_518P_MODEL_ID,
            driver.PNO_REGISTER: 2,
            driver.SRUN_REGISTER: state,
            driver.PROGRAM_SP1_REGISTER: 400,
            driver.PROGRAM_SP2_REGISTER: 400,
        }
    )
    instrument.serial.port = "/dev/fake-attach"
    furnace = Furnace.attach(port=instrument.serial.port, instrument=instrument)
    assert instrument.writes == []
    assert furnace.get_run_state()[0] == state
    assert instrument.values[driver.PNO_REGISTER] == 2
    assert furnace.program_setpoint_registers == (
        driver.PROGRAM_SP1_REGISTER,
        driver.PROGRAM_SP2_REGISTER,
    )
    assert furnace._program_bank_supported is True
    assert furnace.pending_hold_target is None
    assert furnace.flow_controller is None
    assert furnace.program_mode_disabled is False
    furnace.close(stop_output=False)
    assert instrument.writes == []


def test_injected_normal_constructor_still_disables_program_and_stops():
    instrument = FakeInstrument(
        {
            driver.MODEL_ID_REGISTER: driver.AI_518P_MODEL_ID,
            driver.PNO_REGISTER: 2,
        }
    )
    instrument.serial.port = "/dev/fake-normal"
    furnace = Furnace(port=instrument.serial.port, instrument=instrument)
    assert instrument.writes == [
        (driver.SRUN_REGISTER, 1),
        (driver.PNO_REGISTER, 0),
        (driver.SRUN_REGISTER, 1),
    ]
    assert furnace.flow_controller is None


def test_attach_rejects_mismatched_injected_port_before_reading():
    instrument = FakeInstrument()
    instrument.serial.port = "/dev/actual"
    with pytest.raises(ValueError, match="configured serial port"):
        Furnace.attach(port="/dev/other", instrument=instrument)
    assert instrument.events == []


@pytest.mark.parametrize("failed_register", [0x16, driver.MODEL_ID_REGISTER])
def test_attach_closes_transport_after_failed_identity_or_model_read(failed_register):
    instrument = FakeInstrument()
    instrument.serial.port = "/dev/fake-attach"
    instrument.failed_reads.add(failed_register)
    with pytest.raises(OSError, match="fake register unavailable"):
        Furnace.attach(port=instrument.serial.port, instrument=instrument)
    assert instrument.writes == []
    assert not instrument.serial.is_open
