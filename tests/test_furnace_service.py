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
    def test_service_open_uses_100_percent_setpoint_for_each_channel(self):
        service = FurnaceService.__new__(FurnaceService)
        for channel in range(3):
            with self.subTest(channel=channel), \
                    patch.object(service, "set_gas_flow", return_value={"sent": True}) as set_flow, \
                    patch.object(service, "_ensure_flow_controller") as connect:
                self.assertEqual(service.set_gas_valve(channel, True), {"sent": True})
                set_flow.assert_called_once_with(channel, 100.0)
                connect.assert_not_called()

    def test_service_open_rejects_non_boolean_before_sending(self):
        service = FurnaceService.__new__(FurnaceService)
        with patch.object(service, "set_gas_flow") as set_flow:
            with self.assertRaises(ValueError):
                service.set_gas_valve(2, "false")
            set_flow.assert_not_called()

    def test_inspection_preserves_target_when_write_only_channel_is_silent(self):
        service = FurnaceService.__new__(FurnaceService)
        service._state_lock = threading.RLock()
        service._flow_io_lock = threading.Lock()
        service._flow_channels = {("gas", 9): {
            "unit_id": 9, "channel_index": 2, "target_percent": 20,
            "write_only": True, "setpoint_verified": False,
        }}
        serial = FakeSerial([b""] * 4)
        controller = MFCFlowController(serial_connection=serial)
        with patch.object(service, "_ensure_flow_controller", return_value=controller), \
                patch.object(service, "flow_status", return_value={}):
            service.inspect_gas_flow(2)
        snapshot = service._flow_channels[("gas", 9)]
        self.assertEqual(snapshot["target_percent"], 20)
        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["setpoint_verified"])
        self.assertIn("empty response", snapshot["error"])
        self.assertEqual(serial.writes, [b"\x0209RFR\r"] * 4)

    def test_service_sets_gas_flow_and_opens_the_valve_in_same_call(self):
        calls = []

        class FakeFlowController:
            connected = True

            def set_gas_solenoid(self, channel, opened):
                calls.append(("gas_solenoid", channel, opened))
                return {"channel_index": channel, "channel_number": channel + 1,
                        "open": opened, "verified": True}

            def set_flow_percent_unconfirmed(self, unit_id, flow_percent):
                calls.append(("sfr", unit_id, flow_percent))
                return flow_percent

            @staticmethod
            def _gas_channel(channel):
                return int(channel)

            @staticmethod
            def gas_unit_id(channel):
                return [7, 8, 9][int(channel)]

        service = FurnaceService.__new__(FurnaceService)
        service.flow_port = "COM5"
        service._flow_controller = FakeFlowController()
        service._flow_channels = {
            ("gas", 7): {"channel_index": 0, "channel_number": 1,
                         "unit_id": 7,
                         "actual_percent": None,
                         "controller_status": None, "target_percent": None,
                         "available": True, "error": None, "write_only": True},
        }
        service._gas_valves = {
            channel: {"channel_index": channel, "channel_number": channel + 1,
                      "open": False, "verified": False}
            for channel in range(3)
        }
        service._state_lock = threading.RLock()
        service._flow_io_lock = threading.Lock()
        service._flow_error = None

        result = service.set_gas_flow(0, 30)

        self.assertEqual(result["gas_channels"][0]["target_percent"], 30)
        self.assertEqual(calls[0], ("sfr", 7, 30))
        self.assertEqual(calls[1][:2], ("gas_solenoid", 0))
        self.assertTrue(calls[1][2])

        calls.clear()
        service.set_gas_flow(0, 0)
        self.assertEqual(calls[0], ("gas_solenoid", 0, False))
        self.assertEqual(calls[1], ("sfr", 7, 0))

    def test_service_rejects_percentage_above_full_scale_before_connecting(self):
        service = FurnaceService.__new__(FurnaceService)
        with self.assertRaisesRegex(ValueError, "Flow percentage"):
            service.set_gas_flow(0, 100.01)

    def test_frame_metadata_includes_gas_setpoints_in_percent(self):
        class DummyFurnace:
            connected = False

        service = FurnaceService.__new__(FurnaceService)
        service._furnace = DummyFurnace()
        service._error = None
        service._flow_error = None
        service._flow_controller = None
        service.flow_port = "COM13"
        service._state_lock = threading.RLock()
        service._steps = []
        service._sequence_running = False
        service._sequence_index = None
        service._sequence_phase = "idle"
        service._hold_started_at = None
        service._sequence_started_at = None
        service._applied_signature = None
        service._snapshot = {
            "sample_time": 123.0,
            "pv": 25.0,
            "sv": 50.0,
            "ramp": 5.0,
            "run_state": "HOLD",
        }
        service._flow_channels = {
            ("gas", 7): {
                "channel_index": 0,
                "channel_number": 1,
                "unit_id": 7,
                "gas_name": "Gas 1",
                "actual_percent": None,
                "controller_status": None,
                "target_percent": 20.0,
                "available": True,
                "error": None,
                "write_only": True,
            },
            ("gas", 8): {
                "channel_index": 1,
                "channel_number": 2,
                "unit_id": 8,
                "gas_name": "Gas 2",
                "actual_percent": None,
                "controller_status": None,
                "target_percent": 50.0,
                "available": True,
                "error": None,
                "write_only": True,
            },
            ("gas", 9): {
                "channel_index": 2,
                "channel_number": 3,
                "unit_id": 9,
                "gas_name": "Gas 3",
                "actual_percent": None,
                "controller_status": None,
                "target_percent": 60.0,
                "available": True,
                "error": None,
                "write_only": True,
            },
        }
        service._gas_valves = {
            0: {"channel_index": 0, "open": True},
            1: {"channel_index": 1, "open": True},
            2: {"channel_index": 2, "open": True},
        }

        metadata = service.frame_metadata()

        self.assertEqual(metadata["gas_1_target_percent"], 20.0)
        self.assertEqual(metadata["gas_2_target_percent"], 50.0)
        self.assertEqual(metadata["gas_3_target_percent"], 60.0)
        self.assertEqual(set(metadata), set(FRAME_METADATA_FIELDS))

    def test_service_connect_uses_hardcoded_furnace_ports(self):
        service = FurnaceService()

        with patch("furnace.Furnace") as furnace_cls, patch(
            "furnace.MFCFlowController"
        ) as flow_cls:
            service.connect(port="COM999")
            service.connect_flow_controller(port="COM998")

        furnace_cls.assert_called_once_with(port="COM14", slave_address=1)
        flow_cls.assert_called_once_with(port="COM13")
        self.assertEqual(service.port, "COM14")
        self.assertEqual(service.flow_port, "COM13")

