from concurrent.futures import ThreadPoolExecutor

import pytest

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    MICROSTEP_MODES,
    CommandRejected,
    ControllerError,
    LinkError,
)


@pytest.mark.parametrize("divisor", [1, 2, 4, 8, 16])
def test_global_mode_roundtrip_preserves_motor_settings_and_pulse_counts(stage_rig, divisor):
    controller, transport, clock = stage_rig
    controller.move("top", "ccw", 50, 50)
    clock.advance(1)
    controller.poll()
    controller.set_microstep(2 if divisor == 1 else 1)
    before = controller.snapshot()["motors"]
    selected = transport.selected
    transport.commands.clear()

    result = controller.set_microstep(divisor)

    assert result["microstep"] == divisor
    assert transport.microstep == divisor
    assert transport.commands == [f"microstep {divisor}"]
    assert transport.selected == selected
    assert result["motors"] == before
    assert result["motors"][1]["position"] == -50


def test_reapplying_current_mode_is_a_noop(stage_rig):
    controller, transport, _ = stage_rig
    assert controller.set_microstep(1)["microstep"] == 1
    assert not transport.commands


@pytest.mark.parametrize("motor", ["bottom", "top", "insert"])
@pytest.mark.parametrize("held", [False, True])
def test_mode_change_is_blocked_when_any_motor_is_moving(stage_rig, motor, held):
    controller, transport, clock = stage_rig
    if held:
        controller.hold(motor, "cw", 50, "test-microstep-hold", clock())
    else:
        controller.move(motor, "cw", 50, 500)
    transport.commands.clear()

    with pytest.raises(ControllerError, match="Stop all motors"):
        controller.set_microstep(16)
    assert not transport.commands
    assert controller.snapshot()["microstep"] == 1
    # The simulated device also enforces the firmware's global idle requirement.
    with pytest.raises(CommandRejected, match="All motors must be idle"):
        transport.command("microstep 16")
    assert transport.microstep == 1


@pytest.mark.parametrize("divisor", [0, 3, 32, 256, -1, True, 2.0, "16", None, []])
def test_invalid_mode_never_reaches_serial(stage_rig, divisor):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError):
        controller.set_microstep(divisor)
    assert not transport.commands


def test_old_firmware_has_unknown_mode_and_explicit_upgrade_message(stage_rig):
    controller, transport, _ = stage_rig
    transport.microstep = None
    controller.poll()
    assert controller.snapshot()["microstep"] is None
    with pytest.raises(ControllerError, match="updated three-motor-demo"):
        controller.set_microstep(8)
    assert not transport.commands
    assert controller.snapshot()["connected"]


def test_rejected_mode_does_not_claim_success_or_disconnect(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig

    def rejected(_command):
        raise CommandRejected("Device rejected mode")

    monkeypatch.setattr(transport, "command", rejected)
    with pytest.raises(CommandRejected, match="rejected mode"):
        controller.set_microstep(8)
    assert controller.snapshot()["microstep"] == 1
    assert controller.snapshot()["connected"]


def test_mode_must_match_board_readback(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    original = transport.command

    def ignores_mode(command):
        if not command.startswith("microstep "):
            original(command)

    monkeypatch.setattr(transport, "command", ignores_mode)
    with pytest.raises(LinkError, match="did not confirm"):
        controller.set_microstep(8)
    assert not controller.snapshot()["connected"]
    assert controller.snapshot()["microstep"] is None
    assert transport.closed


def test_connect_and_poll_read_existing_mode_without_overwriting_it(stage_rig):
    controller, transport, _ = stage_rig
    controller.disconnect()
    transport.microstep = 16
    transport.commands.clear()
    assert controller.connect("DEMO")["microstep"] == 16
    assert not transport.commands
    transport.microstep = 4
    controller.poll()
    assert controller.snapshot()["microstep"] == 4


@pytest.mark.parametrize("divisor", [1, 2, 4, 8, 16])
def test_step_and_speed_values_remain_pulse_counts_at_each_resolution(stage_rig, divisor):
    controller, transport, clock = stage_rig
    controller.set_microstep(divisor)
    transport.commands.clear()
    controller.move("bottom", "cw", 50, 50)
    clock.advance(1)
    controller.poll()
    assert transport.commands == ["motor bottom", "dir cw", "speed 50", "steps 50"]
    assert controller.snapshot()["motors"][0]["position"] == 50
    assert controller.snapshot()["microstep"] == divisor


def test_mode_change_and_move_are_serialized_across_clients(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        mode = pool.submit(controller.set_microstep, 16)
        move = pool.submit(controller.move, "top", "cw", 50, 500)
        move.result()
        try:
            mode.result()
        except ControllerError:
            assert transport.commands == ["motor top", "dir cw", "speed 50", "steps 500"]
        else:
            assert transport.commands == [
                "microstep 16",
                "motor top",
                "dir cw",
                "speed 50",
                "steps 500",
            ]


def test_disconnected_state_does_not_claim_a_mode():
    controller = MotorController(demo=True)
    assert controller.snapshot()["microstep"] is None
    assert [item["value"] for item in controller.snapshot()["microstep_modes"]] == list(
        MICROSTEP_MODES
    )
    with pytest.raises(ControllerError, match="Connect"):
        controller.set_microstep(16)
