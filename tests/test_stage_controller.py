import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from stage_helpers import Clock, RecordingTransport, serial_port

from stage_controller.controller import MotorController
from stage_controller.protocol import CommandRejected, ControllerError, LinkError


def motor(controller, name="bottom"):
    return next(item for item in controller.snapshot()["motors"] if item["id"] == name)


def test_move_uses_correct_motor_speed_direction_and_rotation_count(stage_rig):
    controller, transport, clock = stage_rig
    controller.move("top", "ccw", 0.3, 0.1)
    assert transport.commands == ["motor top", "dir ccw", "speed 0.3", "rev 0.1"]
    clock.advance(1)
    controller.poll()
    assert motor(controller, "top")["position"] == 0.1
    assert motor(controller)["position"] == 0


def test_busy_and_post_completion_cooldown_cannot_be_bypassed(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 0.1, 1)
    clock.advance(2)  # More than one second after click, but still moving.
    with pytest.raises(ControllerError, match="still moving"):
        controller.move("bottom", "ccw", 0.1, 1)
    clock.advance(8)
    controller.poll()
    assert motor(controller)["cooldown"] == 1
    clock.advance(0.999)
    with pytest.raises(ControllerError, match="cooldown"):
        controller.move("bottom", "cw", 0.1, 1)
    clock.advance(0.002)
    controller.move("bottom", "ccw", 0.1, 1)
    assert motor(controller)["mode"] == "moving"


def test_tiny_move_finishing_before_first_poll_still_cools_down(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 0.3, 0.001)
    clock.advance(2)
    controller.poll()
    assert not motor(controller)["available"]
    assert motor(controller)["cooldown"] == 1


def test_only_one_axis_starts_and_transactions_do_not_interleave(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(controller.move, name, "cw", 0.2, 0.2)
            for name in ("bottom", "top", "insert")
        ]
    assert sum(future.exception() is None for future in futures) == 1
    assert len(transport.commands) == 4
    assert transport.commands[0].startswith("motor ")
    assert transport.commands[1:] == ["dir cw", "speed 0.2", "rev 0.2"]
    assert sum(item["mode"] == "moving" for item in controller.snapshot()["motors"]) == 1


def test_simultaneous_clicks_do_not_queue_two_moves(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(controller.move, "bottom", "cw", 0.2, 0.2) for _ in range(2)]
    assert sum(future.exception() is None for future in futures) == 1
    assert transport.commands.count("rev 0.2") == 1


def test_stop_interrupts_all_motors_and_preserves_cooldown(stage_rig):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 0.3, 1000)
    result = controller.stop_all()
    assert transport.commands[-1] == "stop all"
    assert all(item["mode"] == "idle" for item in result["motors"])
    assert motor(controller)["cooldown"] == 1
    assert motor(controller, "top")["cooldown"] == 0
    assert not motor(controller, "insert")["available"]


def test_disconnect_stops_and_reconnect_cannot_skip_cooldown(stage_rig):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 0.2, 0.2)
    controller.disconnect()
    assert transport.closed
    assert transport.commands[-1] == "stop all"
    assert not controller.snapshot()["connected"]
    controller.connect("DEMO")
    with pytest.raises(ControllerError, match="cooldown"):
        controller.move("bottom", "cw", 0.2, 0.2)


def test_board_rejection_aborts_transaction_before_rotation(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    original = transport.command

    def reject(command):
        if command.startswith("speed"):
            raise CommandRejected("ERROR: rejected speed")
        original(command)

    monkeypatch.setattr(transport, "command", reject)
    with pytest.raises(CommandRejected):
        controller.move("top", "cw", 0.1, 0.1)
    assert not any(command.startswith("rev") for command in transport.commands)
    assert controller.snapshot()["connected"]


def test_link_loss_disables_controls_and_never_retries_move(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 0.3, 100)

    def failed_status():
        raise LinkError("USB unplugged")

    monkeypatch.setattr(transport, "status", failed_status)
    controller.poll()
    snapshot = controller.snapshot()
    assert not snapshot["connected"]
    assert all(not item["available"] for item in snapshot["motors"])
    assert "stop could not be confirmed" in snapshot["error"]
    assert transport.commands.count("rev 100") == 1
    assert transport.closed


@pytest.mark.parametrize(
    "speed,rotations",
    [
        (0, 1),
        (5.001, 1),
        (True, 1),
        (float("nan"), 1),
        ("200", 1),
        (1, 0),
        (1, 10001),
        (1, False),
        (1, None),
        (1, float("inf")),
        (float("inf"), 1),
        (1, float("nan")),
        (0.0009, 1),
        (1, 0.0009),
        (1, "0.5"),
        pytest.param(10**400, 1, id="oversized-speed-integer"),
        pytest.param(1, 10**400, id="oversized-rotation-integer"),
    ],
)
def test_invalid_values_never_write_to_serial(stage_rig, speed, rotations):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError):
        controller.move("bottom", "cw", speed, rotations)
    assert not transport.commands


def test_requires_connection_and_a_current_port():
    controller = MotorController(demo=True)
    with pytest.raises(ControllerError, match="Connect"):
        controller.move("bottom", "cw", 0.2, 0.2)
    with pytest.raises(ValueError, match="no longer available"):
        controller.connect("COM999")


@pytest.mark.parametrize("rotations", [0.1, 0.5, 1, 12.5, 10000])
@pytest.mark.parametrize("direction,sign", [("cw", -1), ("ccw", 1)])
def test_fractional_and_multiple_turn_moves_keep_units(stage_rig, rotations, direction, sign):
    controller, transport, clock = stage_rig
    controller.move("insert", direction, 0.3, rotations)
    assert transport.commands[-1] == f"rev {rotations:g}"
    clock.advance(rotations / 0.3 + 1)
    controller.poll()
    assert motor(controller, "insert")["position"] == pytest.approx(sign * rotations)
    assert motor(controller, "insert")["remaining"] == 0
    assert motor(controller, "insert")["mode"] == "idle"


@pytest.mark.parametrize("held", [False, True])
def test_legacy_firmware_never_receives_rotation_motion(stage_rig, held):
    controller, transport, clock = stage_rig
    transport.closed_loop = False
    controller.poll()
    assert controller.snapshot()["closed_loop"] is False
    assert not any(item["available"] for item in controller.snapshot()["motors"])
    with pytest.raises(ControllerError, match="stage-controller-v3"):
        if held:
            controller.hold("bottom", "cw", 0.25, "closed-loop-check-01", clock())
        else:
            controller.move("bottom", "cw", 0.25, 0.5)
    assert transport.commands == []


@pytest.mark.parametrize(
    "encoder,fault",
    [
        ("offline", "none"),
        ("magnet", "none"),
        ("untrusted", "none"),
        ("ok", "stall"),
        ("ok", "direction"),
        ("ok", "tracking"),
    ],
)
@pytest.mark.parametrize("held", [False, True])
def test_unhealthy_axis_cannot_start_but_other_axis_remains_available(
    stage_rig, encoder, fault, held
):
    controller, transport, clock = stage_rig
    transport.motors["top"].encoder = encoder
    transport.motors["top"].fault = fault
    controller.poll()
    assert not motor(controller, "top")["available"]
    assert motor(controller, "bottom")["available"]
    with pytest.raises(ControllerError):
        if held:
            controller.hold("top", "cw", 0.25, "unhealthy-axis-0001", clock())
        else:
            controller.move("top", "cw", 0.25, 0.5)
    assert transport.commands == []


def test_fault_clear_is_selected_confirmed_and_does_not_move(stage_rig):
    controller, transport, _ = stage_rig
    transport.motors["top"].fault = "stall"
    controller.poll()
    result = controller.clear_fault("top")
    assert transport.commands == ["motor top", "clear"]
    assert result["motors"][1]["fault"] == "none"
    assert result["motors"][1]["mode"] == "idle"
    assert result["motors"][1]["available"]


def test_fault_clear_cannot_interrupt_a_move(stage_rig):
    controller, transport, _ = stage_rig
    controller.move("top", "cw", 0.25, 1)
    transport.commands.clear()
    with pytest.raises(ControllerError):
        controller.clear_fault("top")
    assert transport.commands == []


def test_fault_clear_requires_healthy_encoder(stage_rig):
    controller, transport, _ = stage_rig
    transport.motors["top"].encoder = "offline"
    transport.motors["top"].fault = "encoder"
    with pytest.raises(ControllerError):
        controller.clear_fault("top")
    assert not motor(controller, "top")["available"]


def test_fault_clear_requires_readback_confirmation(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    transport.motors["top"].fault = "stall"
    original = transport.command

    def ignores_clear(command):
        if command != "clear":
            original(command)

    monkeypatch.setattr(transport, "command", ignores_clear)
    with pytest.raises(ControllerError):
        controller.clear_fault("top")
    assert not motor(controller, "top")["available"]


def test_excessive_single_move_duration_is_rejected_without_commands(stage_rig):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError, match="24 hours"):
        controller.move("bottom", "cw", 0.001, 10000)
    assert transport.commands == []


@pytest.mark.skipif(os.name == "nt", reason="udev symlink checks run on Linux")
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


@pytest.mark.skipif(os.name == "nt", reason="udev symlink checks run on Linux")
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


@pytest.mark.skipif(os.name == "nt", reason="udev symlink checks run on Linux")
def test_absent_configured_serial_offers_no_port(stage_usb, stage_ports, tmp_path):
    missing = str(tmp_path / "by-id" / "usb-Arduino_Nano_ARDUINO9-if00")
    assert MotorController(serial=missing).ports() == []
    # Unplugged: pyserial no longer lists the device that the link names.
    controller = MotorController(serial=stage_usb["arduino"])
    stage_ports[:] = [port for port in stage_ports if port.device != str(tmp_path / "ttyACM0")]
    assert controller.ports() == []
    with pytest.raises(ValueError, match="no longer available"):
        controller.connect(stage_usb["arduino"])


@pytest.mark.skipif(os.name == "nt", reason="udev symlink checks run on Linux")
def test_demo_ignores_the_configured_serial(stage_usb):
    controller = MotorController(demo=True, serial=stage_usb["arduino"])
    assert controller.serial is None
    assert [port["device"] for port in controller.ports()] == ["DEMO"]
    with pytest.raises(ValueError):
        controller.connect(stage_usb["arduino"])
