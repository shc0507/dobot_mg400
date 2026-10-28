from concurrent.futures import ThreadPoolExecutor

import pytest
from stage_helpers import Clock, RecordingTransport, serial_port

from stage_controller.controller import (
    GRIPPER_CONVERTER,
    MotorController,
    is_gripper_converter,
)
from stage_controller.protocol import CommandRejected, ControllerError, LinkError


def motor(controller, name="bottom"):
    return next(item for item in controller.snapshot()["motors"] if item["id"] == name)


def test_move_uses_correct_motor_speed_direction_and_step_count(stage_rig):
    controller, transport, clock = stage_rig
    controller.move("top", "ccw", 400, 100)
    assert transport.commands == ["motor top", "dir ccw", "speed 400", "steps 100"]
    clock.advance(1)
    controller.poll()
    assert motor(controller, "top")["position"] == -100
    assert motor(controller)["position"] == 0


def test_busy_and_post_completion_cooldown_cannot_be_bypassed(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 10, 100)
    clock.advance(2)  # More than one second after click, but still moving.
    with pytest.raises(ControllerError, match="still moving"):
        controller.move("bottom", "ccw", 10, 100)
    clock.advance(8)
    controller.poll()
    assert motor(controller)["cooldown"] == 1
    clock.advance(0.999)
    with pytest.raises(ControllerError, match="cooldown"):
        controller.move("bottom", "cw", 10, 100)
    clock.advance(0.002)
    controller.move("bottom", "ccw", 10, 100)
    assert motor(controller)["mode"] == "moving"


def test_tiny_move_finishing_before_first_poll_still_cools_down(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 1000, 1)
    clock.advance(2)
    controller.poll()
    assert not motor(controller)["available"]
    assert motor(controller)["cooldown"] == 1


def test_other_motors_move_independently_and_transactions_do_not_interleave(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(controller.move, name, "cw", 200, 200)
            for name in ("bottom", "top", "insert")
        ]
        for future in futures:
            future.result()
    for start in range(0, 12, 4):
        group = transport.commands[start : start + 4]
        assert group[0].startswith("motor ")
        assert group[1:] == ["dir cw", "speed 200", "steps 200"]
    assert all(item["mode"] == "moving" for item in controller.snapshot()["motors"])


def test_simultaneous_clicks_do_not_queue_two_moves(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(controller.move, "bottom", "cw", 200, 200) for _ in range(2)]
    assert sum(future.exception() is None for future in futures) == 1
    assert transport.commands.count("steps 200") == 1


def test_stop_interrupts_all_motors_and_preserves_cooldown(stage_rig):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 1, 1000)
    controller.move("top", "ccw", 1, 1000)
    result = controller.stop_all()
    assert transport.commands[-1] == "stop all"
    assert all(item["mode"] == "idle" for item in result["motors"])
    assert motor(controller)["cooldown"] == 1
    assert motor(controller, "top")["cooldown"] == 1
    assert motor(controller, "insert")["available"]


def test_disconnect_stops_and_reconnect_cannot_skip_cooldown(stage_rig):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 200, 200)
    controller.disconnect()
    assert transport.closed
    assert transport.commands[-1] == "stop all"
    assert not controller.snapshot()["connected"]
    controller.connect("DEMO")
    with pytest.raises(ControllerError, match="cooldown"):
        controller.move("bottom", "cw", 200, 200)


def test_board_rejection_aborts_transaction_before_steps(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    original = transport.command

    def reject(command):
        if command.startswith("speed"):
            raise CommandRejected("ERROR: rejected speed")
        original(command)

    monkeypatch.setattr(transport, "command", reject)
    with pytest.raises(CommandRejected):
        controller.move("top", "cw", 100, 100)
    assert not any(command.startswith("steps") for command in transport.commands)
    assert controller.snapshot()["connected"]


def test_link_loss_disables_controls_and_never_retries_move(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 1, 100)

    def failed_status():
        raise LinkError("USB unplugged")

    monkeypatch.setattr(transport, "status", failed_status)
    controller.poll()
    snapshot = controller.snapshot()
    assert not snapshot["connected"]
    assert all(not item["available"] for item in snapshot["motors"])
    assert "stop could not be confirmed" in snapshot["error"]
    assert transport.commands.count("steps 100") == 1
    assert transport.closed


@pytest.mark.parametrize(
    "speed,steps",
    [
        (0, 1),
        (1001, 1),
        (True, 1),
        (1.5, 1),
        ("200", 1),
        (1, 0),
        (1, 100001),
        (1, False),
        (1, None),
        (1, 2.5),
    ],
)
def test_invalid_values_never_write_to_serial(stage_rig, speed, steps):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError):
        controller.move("bottom", "cw", speed, steps)
    assert not transport.commands


def test_requires_connection_and_a_current_port():
    controller = MotorController(demo=True)
    with pytest.raises(ControllerError, match="Connect"):
        controller.move("bottom", "cw", 200, 200)
    with pytest.raises(ValueError, match="no longer available"):
        controller.connect("COM999")


def test_port_list_never_offers_the_gripper_converter(stage_ports, tmp_path):
    tty = tmp_path / "ttyUSB0"
    tty.touch()
    link = tmp_path / "usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0"
    link.symlink_to(tty)
    stage_ports.extend(
        [
            serial_port(str(tty), *GRIPPER_CONVERTER, description="FT232R USB UART"),
            # A link entry is the same device, even without its USB identity.
            serial_port(str(link), description="FT232R USB UART"),
            serial_port("/dev/ttyUSB1", 0x0403, 0x6001, "A10KXYZ1", "Another FTDI adapter"),
            serial_port("/dev/ttyACM0", 0x2341, 0x0043, "ARDUINO1", "Arduino Uno"),
        ]
    )
    opened = []
    controller = MotorController(transport_factory=opened.append)
    assert controller.ports() == [
        {"device": "/dev/ttyACM0", "description": "Arduino Uno"},
        {"device": "/dev/ttyUSB1", "description": "Another FTDI adapter"},
    ]
    for port in (str(tty), str(link)):
        with pytest.raises(ValueError, match="no longer available"):
            controller.connect(port)
    assert opened == []


@pytest.mark.parametrize("port", ["/dev/ttyACM1", "/dev/sda", "../ttyACM0", "", None, 0, []])
def test_connect_opens_only_a_listed_port(stage_ports, port):
    stage_ports.append(serial_port("/dev/ttyACM0", description="Arduino Uno"))
    opened = []

    def open_port(device):
        opened.append(device)
        return RecordingTransport(Clock())

    controller = MotorController(transport_factory=open_port)
    with pytest.raises(ValueError, match="no longer available"):
        controller.connect(port)
    assert opened == []
    controller.connect("/dev/ttyACM0")
    assert opened == ["/dev/ttyACM0"]
    # A connected panel still refuses an unlisted port before anything else.
    with pytest.raises(ValueError):
        controller.connect(port)
    controller.close()


def test_demo_connects_only_the_demo_port():
    controller = MotorController(demo=True)
    with pytest.raises(ValueError):
        controller.connect("/dev/ttyACM0")
    assert controller.connect("DEMO")["connected"]
    controller.close()


def test_configured_serial_is_the_only_port_offered(stage_usb, tmp_path):
    opened = []

    def open_port(device):
        opened.append(device)
        return RecordingTransport(Clock())

    controller = MotorController(serial=stage_usb["arduino"], transport_factory=open_port)
    # The by-id link resolves to its ttyACM device, and the link is the device offered.
    assert controller.ports() == [{"device": stage_usb["arduino"], "description": "Arduino Uno"}]
    for port in (str(tmp_path / "ttyACM0"), str(tmp_path / "ttyACM1"), stage_usb["other"]):
        with pytest.raises(ValueError, match="no longer available"):
            controller.connect(port)
    assert opened == []
    controller.connect(stage_usb["arduino"])
    assert opened == [stage_usb["arduino"]]
    controller.close()


def test_configured_serial_follows_its_link_after_a_reconnect(stage_usb, stage_ports, tmp_path):
    controller = MotorController(serial=stage_usb["arduino"])
    # The Arduino comes back as ttyACM2, and udev moves its by-id link.
    stage_ports[:] = [port for port in stage_ports if port.device != str(tmp_path / "ttyACM0")]
    (tmp_path / "ttyACM2").touch()
    stage_ports.append(serial_port(str(tmp_path / "ttyACM2"), description="Arduino Uno"))
    link = tmp_path / "by-id" / stage_usb["arduino"].rsplit("/", 1)[1]
    link.unlink()
    link.symlink_to(tmp_path / "ttyACM2")
    assert controller.ports() == [{"device": stage_usb["arduino"], "description": "Arduino Uno"}]


def test_absent_configured_serial_offers_no_port(stage_usb, stage_ports, tmp_path):
    missing = str(tmp_path / "by-id" / "usb-Arduino_Nano_ARDUINO9-if00")
    assert MotorController(serial=missing).ports() == []
    # Unplugged: pyserial no longer lists the device that the link names.
    controller = MotorController(serial=stage_usb["arduino"])
    stage_ports[:] = [port for port in stage_ports if port.device != str(tmp_path / "ttyACM0")]
    assert controller.ports() == []
    with pytest.raises(ValueError, match="no longer available"):
        controller.connect(stage_usb["arduino"])


def test_configured_serial_never_offers_the_gripper_converter(stage_usb, tmp_path):
    alias = tmp_path / "stage-arduino"
    alias.symlink_to(tmp_path / "ttyUSB0")
    for serial in (stage_usb["gripper"], str(alias), str(tmp_path / "ttyUSB0")):
        opened = []
        controller = MotorController(serial=serial, transport_factory=opened.append)
        assert controller.ports() == []
        with pytest.raises(ValueError, match="no longer available"):
            controller.connect(serial)
        assert opened == []


def test_gripper_converter_is_found_by_usb_identity_or_by_id_name(stage_usb, tmp_path):
    alias = tmp_path / "stage-arduino"
    alias.symlink_to(tmp_path / "ttyUSB0")
    assert is_gripper_converter(stage_usb["gripper"])
    assert is_gripper_converter(str(alias))
    assert is_gripper_converter(str(tmp_path / "ttyUSB0"))
    # The by-id name identifies the converter also while it is unplugged.
    assert is_gripper_converter("/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0")
    assert not is_gripper_converter(stage_usb["arduino"])
    assert not is_gripper_converter(str(tmp_path / "ttyACM1"))


def test_demo_ignores_the_configured_serial(stage_usb):
    controller = MotorController(demo=True, serial=stage_usb["arduino"])
    assert controller.serial is None
    assert [port["device"] for port in controller.ports()] == ["DEMO"]
    with pytest.raises(ValueError):
        controller.connect(stage_usb["arduino"])
