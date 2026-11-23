from concurrent.futures import ThreadPoolExecutor

import pytest

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    MICROSTEP_MODES,
    REQUIRED_MICROSTEP,
    CommandRejected,
    ControllerError,
    LinkError,
)


def test_all_motors_start_in_fixed_eighth_step_mode(stage_rig):
    controller, transport, _ = stage_rig
    snapshot = controller.snapshot()
    assert REQUIRED_MICROSTEP == 8
    assert list(MICROSTEP_MODES) == [8]
    assert snapshot["microstep"] == transport.microstep == 8
    assert [item["value"] for item in snapshot["microstep_modes"]] == [8]
    assert all(item["max_speed"] == 0.3 for item in snapshot["motors"])
    assert all(item["tolerance"] == pytest.approx(4 / 4096) for item in snapshot["motors"])
    assert not transport.commands


def test_reapplying_fixed_mode_is_a_noop_and_preserves_measured_position(stage_rig):
    controller, transport, clock = stage_rig
    controller.move("top", "ccw", 0.25, 0.25)
    clock.advance(1)
    controller.poll()
    before = controller.snapshot()["motors"]
    transport.commands.clear()
    result = controller.set_microstep(8)
    assert result["microstep"] == 8
    assert result["motors"] == before
    assert result["motors"][1]["position"] == 0.25
    assert not transport.commands


@pytest.mark.parametrize("motor", ["bottom", "top", "insert"])
@pytest.mark.parametrize("held", [False, True])
def test_mode_configuration_is_blocked_when_any_motor_is_moving(stage_rig, motor, held):
    controller, transport, clock = stage_rig
    if held:
        controller.hold(motor, "cw", 0.25, "test-microstep-hold", clock())
    else:
        controller.move(motor, "cw", 0.25, 2.5)
    transport.commands.clear()
    with pytest.raises(ControllerError, match="Stop all motors"):
        controller.set_microstep(8)
    assert not transport.commands
    assert controller.snapshot()["microstep"] == 8
    with pytest.raises(CommandRejected, match="All motors must be idle"):
        transport.command("microstep 8")
    assert transport.microstep == 8


@pytest.mark.parametrize("divisor", [0, 1, 2, 3, 4, 16, 32, 256, -1, True, 8.0, "8", None, []])
def test_every_mode_except_integer_eight_is_rejected_without_serial_write(stage_rig, divisor):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError):
        controller.set_microstep(divisor)
    assert not transport.commands
    assert controller.snapshot()["microstep"] == 8


@pytest.mark.parametrize("divisor", [1, 2, 4, 16])
def test_simulated_firmware_rejects_modes_other_than_eighth_step(stage_rig, divisor):
    _, transport, _ = stage_rig
    with pytest.raises(CommandRejected):
        transport.command(f"microstep {divisor}")
    assert transport.microstep == 8


def test_old_firmware_has_unknown_mode_and_explicit_upgrade_message(stage_rig):
    controller, transport, _ = stage_rig
    transport.microstep = None
    controller.poll()
    assert controller.snapshot()["microstep"] is None
    with pytest.raises(ControllerError, match="updated stage-controller-v3"):
        controller.set_microstep(8)
    assert not transport.commands
    assert controller.snapshot()["connected"]


def test_rejected_mode_does_not_claim_success_or_disconnect(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    transport.microstep = 1  # Different firmware must explicitly confirm the required mode.

    def rejected(_command):
        raise CommandRejected("Device rejected mode")

    monkeypatch.setattr(transport, "command", rejected)
    with pytest.raises(CommandRejected, match="rejected mode"):
        controller.set_microstep(8)
    assert controller.snapshot()["microstep"] == 1
    assert controller.snapshot()["connected"]
    assert not any(item["available"] for item in controller.snapshot()["motors"])


def test_mode_must_match_board_readback(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    transport.microstep = 1
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


def test_connect_and_poll_report_incompatible_mode_without_silently_reconfiguring(stage_rig):
    controller, transport, _ = stage_rig
    controller.disconnect()
    transport.microstep = 16
    transport.commands.clear()
    assert controller.connect("DEMO")["microstep"] == 16
    assert not transport.commands
    assert not any(item["available"] for item in controller.snapshot()["motors"])
    transport.microstep = 4
    controller.poll()
    assert controller.snapshot()["microstep"] == 4
    assert not any(item["available"] for item in controller.snapshot()["motors"])


@pytest.mark.parametrize("reported_mode", [None, 1, 2, 4, 16])
@pytest.mark.parametrize("held", [False, True])
def test_incompatible_reported_mode_blocks_motion_but_allows_stop(stage_rig, reported_mode, held):
    controller, transport, clock = stage_rig
    transport.microstep = reported_mode
    controller.poll()
    assert not any(item["available"] for item in controller.snapshot()["motors"])
    with pytest.raises(ControllerError):
        if held:
            controller.hold("top", "cw", 0.25, "wrong-mode-hold-001", clock())
        else:
            controller.move("top", "cw", 0.25, 0.5)
    assert not transport.commands
    assert controller.stop_all()["connected"]
    assert transport.commands == ["stop all"]


def test_fractional_rotations_are_shaft_rotations_at_fixed_eighth_step(stage_rig):
    controller, transport, clock = stage_rig
    controller.move("bottom", "cw", 0.25, 0.25)
    clock.advance(1)
    controller.poll()
    assert transport.commands == ["motor bottom", "dir cw", "speed 0.25", "rev 0.25"]
    assert controller.snapshot()["motors"][0]["position"] == -0.25
    assert controller.snapshot()["microstep"] == 8


def test_mode_check_and_move_are_serialized_across_clients(stage_rig):
    controller, transport, _ = stage_rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        mode = pool.submit(controller.set_microstep, 8)
        move = pool.submit(controller.move, "top", "cw", 0.25, 2.5)
        move.result()
        try:
            mode.result()
        except ControllerError:
            pass  # The move won the lock, so the idle-only mode check was rejected.
    assert transport.commands == ["motor top", "dir cw", "speed 0.25", "rev 2.5"]
    assert controller.snapshot()["microstep"] == 8


def test_disconnected_state_does_not_claim_a_mode():
    controller = MotorController(demo=True)
    assert controller.snapshot()["microstep"] is None
    assert [item["value"] for item in controller.snapshot()["microstep_modes"]] == [8]
    with pytest.raises(ControllerError, match="Connect"):
        controller.set_microstep(8)


@pytest.mark.parametrize("motor", ["bottom", "top", "insert"])
@pytest.mark.parametrize("held", [False, True])
def test_rotation_speed_ceiling_is_enforced_for_every_fixed_mode_axis(stage_rig, motor, held):
    controller, transport, clock = stage_rig
    assert controller.snapshot()["motors"][0]["max_speed"] == 0.3
    with pytest.raises((ValueError, ControllerError)):
        if held:
            controller.hold(motor, "cw", 0.301, "speed-bound-hold-001", clock())
        else:
            controller.move(motor, "cw", 0.301, 0.5)
    assert transport.commands == []


def test_held_speed_is_shaft_rotations_per_second_at_fixed_eighth_step(stage_rig):
    controller, _, clock = stage_rig
    controller.hold("top", "ccw", 0.1, "microstep-units-hold", clock())
    clock.advance(0.5)
    controller.release_hold("top", "microstep-units-hold")
    assert controller.snapshot()["motors"][1]["position"] == pytest.approx(0.03)
