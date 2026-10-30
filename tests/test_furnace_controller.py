import pathlib
import threading
import unittest
from unittest.mock import patch

from furnace import FRAME_METADATA_FIELDS, Furnace, FurnaceService, MFCFlowController


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
        with patch("furnace.time.sleep", side_effect=lambda delay: events.append(("wait", delay))):
            controller.set_valve_override(9, False)
            controller.set_flow_percent_unconfirmed(9, 20)

        self.assertEqual(events, [
            ("write", b"\x0209SVC\r"), ("flush",), ("wait", 0.08),
            ("write", b"\x0209SVN\r"), ("flush",), ("wait", 0.08),
            ("write", b"\x0209SFR$7D0\r"), ("flush",), ("wait", 0.08),
        ])

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

        self.assertEqual(serial.writes, [
            solenoid,
            b"\x0207SVO\r",
            b"\x0207SVN\r",
            b"\x0207SFR$1770\r",
        ])

    def test_flow_target_restores_normal_mfc_valve_after_manual_close(self):
        open_packet = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        close_packet = bytes.fromhex("04 05 00 00 00 00 CD 9F")
        serial = FakeSerial([open_packet, close_packet])
        controller = MFCFlowController(serial_connection=serial)

        controller.set_gas_valve(0, True)
        controller.set_gas_valve(0, False)
        controller.set_flow_percent_unconfirmed(7, 60)

        self.assertEqual(serial.writes, [
            open_packet,
            b"\x0207SVO\r",
            close_packet,
            b"\x0207SVC\r",
            b"\x0207SVN\r",
            b"\x0207SFR$1770\r",
        ])

    def test_sets_percent_with_manual_defined_framing(self):
        serial = FakeSerial([
            b"OK\r",
        ])
        controller = MFCFlowController(serial_connection=serial)

        self.assertEqual(controller.set_flow_percent_unconfirmed(7, 25), 25)
        self.assertEqual(serial.writes, [
            b"\x0207SFR$9C4\r",
        ])

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
        self.assertEqual(serial.writes, [
            b"\x0207SFR$0\r",
        ])

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
        self.assertEqual(set(status), {
            "channel_index", "channel_number", "unit_id", "gas_name",
            "actual_percent", "controller_status", "available", "error",
            "write_only",
        })

    def test_furnace_sets_fixed_channel_flow_then_opens_its_solenoid(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([solenoid, b"OK\r"])
        flow = MFCFlowController(serial_connection=serial)
        furnace = Furnace.__new__(Furnace)
        furnace.flow_controller = flow

        self.assertEqual(furnace.set_gas_flow(0, 80), 80)
        self.assertEqual(serial.writes, [
            b"\x0207SFR$1F40\r",
            solenoid,
        ])

    def test_furnace_sets_full_scale_as_100_percent_without_mfc_reply(self):
        solenoid = bytes.fromhex("04 05 00 00 FF 00 8C 6F")
        serial = FakeSerial([solenoid, b"OK\r"])
        flow = MFCFlowController(serial_connection=serial)
        furnace = Furnace.__new__(Furnace)
        furnace.flow_controller = flow

        self.assertEqual(furnace.set_gas_flow(0, 100), 100)
        self.assertEqual(serial.writes, [
            b"\x0207SFR$2710\r",
            solenoid,
        ])

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
        with patch("furnace.connect", return_value=object()) as connect_mock, \
             patch.object(Furnace, "disable_internal_program"), \
             patch.object(Furnace, "_detect_program_setpoints", return_value=()), \
             patch.object(Furnace, "set_stop"), \
             patch.object(Furnace, "connect_flow_controller") as flow_connect:
            furnace = Furnace(port="COM14", slave_address=1, flow_port="COM13")

        connect_mock.assert_called_once_with(port="COM14", slave_address=1)
        flow_connect.assert_not_called()
        self.assertIsNotNone(furnace.controller)

