"""Absolute positioning delegates target execution to a calibrated Arduino."""

from dataclasses import replace

import pytest
from test_stage_encoder_calibration import calibration_frame, calibration_reply
from test_stage_encoder_coordinates import COORDINATES
from test_stage_encoder_limits import axis
from test_stage_encoder_passive_switches import passive_reply
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    POSITION_LIMIT,
    CommandRejected,
    ControllerError,
    LinkError,
)

HEADER = b"positioning=calibrated_absolute_v1\n"


def absolute_reply(*args, **kwargs):
    return [HEADER, *passive_reply(*args, **kwargs)]


@pytest.fixture
def positioned(stage_rig):
    controller, transport, clock = stage_rig
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    clock.advance(1.1)
    controller.poll()
    transport.commands.clear()
    return stage_rig


def test_capability_advertises_positioning_only_after_calibration(stage_rig):
    controller, _, clock = stage_rig
    assert controller.snapshot()["absolute_positioning"]
    assert not axis(controller)["positioning_ready"]
    controller.calibrate("bottom", 0.25)
    assert not axis(controller)["positioning_ready"]
    clock.advance(10)
    controller.poll()
    assert axis(controller)["calibration"] == "done"
    assert not axis(controller)["positioning_ready"]  # Ordinary completion cooldown applies.
    clock.advance(1.1)
    controller.poll()
    assert axis(controller)["positioning_ready"]
    assert not axis(controller, "top")["positioning_ready"]


@pytest.mark.parametrize(
    "bad",
    [
        b"positioning=calibrated_absolute_v2\n",
        b"positioning calibrated_absolute_v1\n",
        b"positioning=unknown\n",
    ],
)
def test_positioning_header_rejects_unknown_versions(switch_serial, bad):
    transport, _, _ = switch_serial([bad, *passive_reply()])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "reply",
    [
        [HEADER, *absolute_reply()],
        [COORDINATES, HEADER, *passive_reply()],
        [passive_reply()[0], HEADER, *passive_reply()[1:]],
    ],
)
def test_positioning_header_is_first_and_unique(switch_serial, reply):
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "missing",
    [
        b"calibration=",
        b"coordinates=",
        b"homing=",
        b"switches=",
        b"limits=",
        b"telemetry=",
        b"motion=",
    ],
)
def test_absolute_positioning_requires_current_calibration_dependencies(switch_serial, missing):
    reply = [line for line in absolute_reply() if not line.startswith(missing)]
    if missing == b"calibration=":
        reply = [line.replace(b" calibration=none", b"") for line in reply]
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


def test_legacy_automatic_contact_zeroing_cannot_claim_new_absolute_protocol(switch_serial):
    transport, _, _ = switch_serial([HEADER, *calibration_reply()])
    with pytest.raises(LinkError, match="calibration-only"):
        transport.status()


@pytest.mark.parametrize("first_new", [False, True])
def test_positioning_capability_is_pinned_until_reconnect(switch_serial, first_new):
    before, after = (
        (absolute_reply, passive_reply) if first_new else (passive_reply, absolute_reply)
    )
    transport, port, _ = switch_serial(before())
    transport.status()
    port.lines.extend(after())
    with pytest.raises(LinkError, match="positioning capability changed"):
        transport.status()


def test_legacy_firmware_disables_absolute_action_with_upgrade_message(switch_serial):
    transport, port, clock = switch_serial(passive_reply("done", lower=0, upper=4096, bounds=3))
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    state = controller.connect("DEMO")
    assert not state["absolute_positioning"] and not axis(controller)["positioning_ready"]
    port.writes.clear()
    port.lines.extend(passive_reply("done", lower=0, upper=4096, bounds=3))
    with pytest.raises(ControllerError, match="absolute positioning support"):
        controller.move_to_position("bottom", 0.5, 0.25)
    assert port.writes == [b"status\n"]


def test_serial_sends_absolute_target_without_pc_direction_or_relative_distance(switch_serial):
    reply = absolute_reply("done", position=0.5, lower=0, upper=4096, bounds=3)
    transport, port, clock = switch_serial(reply)
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [
            *all_frames(counts=2048),
            calibration_frame(4, counts=2048, phase="done", lower=0, upper=4096, bounds=3),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
        ]
    )
    controller.poll()
    port.writes.clear()
    port.lines.extend(
        [
            *reply,
            b"OK: bottom motor\n",
            b"OK: bottom speed\n",
            calibration_frame(7, counts=2048, phase="done", lower=0, upper=4096, bounds=3),
            b"OK: bottom goto\n",
        ]
    )
    result = controller.move_to_position("bottom", 0, 0.25)
    assert port.writes == [b"status\n", b"motor bottom\n", b"speed 0.25\n", b"goto 0\n"]
    assert result["motors"][0]["remaining"] == -0.5
    assert result["motors"][0]["mode"] == "moving"
    assert not result["motors"][0]["positioning_ready"]
    port.lines.extend(
        [
            frame(8, direction="cw", mode="idle"),
            calibration_frame(9, phase="done", lower=0, upper=4096, bounds=3),
        ]
    )
    controller.poll()
    assert axis(controller)["position"] == 0 and axis(controller)["calibration"] == "done"
    assert axis(controller)["cooldown"] == 1


@pytest.mark.parametrize("target,direction", [(0, "cw"), (0.1, "cw"), (0.75, "ccw"), (1, "ccw")])
def test_demo_moves_to_absolute_fraction_or_endpoint_and_keeps_calibration(
    positioned, target, direction
):
    controller, transport, clock = positioned
    result = controller.move_to_position("bottom", target, 0.25)
    assert transport.commands == ["motor bottom", "speed 0.25", f"goto {target:g}"]
    assert axis(controller)["direction"] == direction
    assert result["motors"][0]["calibration"] == "done"
    clock.advance(5)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(target, abs=1 / 4096)
    assert axis(controller)["mode"] == "idle" and axis(controller)["calibration"] == "done"
    assert axis(controller)["lower_limit"] == 0 and axis(controller)["upper_limit"] == 1


@pytest.mark.parametrize("target", [0.5, 0.5 + 1 / 4096])
def test_already_reached_target_is_idle_without_a_new_cooldown(positioned, target):
    controller, transport, _ = positioned
    before = axis(controller)["position"]
    controller.move_to_position("bottom", target, 0.25)
    after = axis(controller)
    assert after["position"] == before and after["mode"] == "idle"
    assert after["remaining"] == 0 and after["cooldown"] == 0
    assert after["positioning_ready"] and after["calibration"] == "done"
    assert not transport.moves


@pytest.mark.parametrize(
    "start,target,mask,direction",
    [
        (-0.25, 0.25, 1, "ccw"),
        (1.25, 0.75, 2, "cw"),
    ],
)
def test_absolute_target_inside_range_recovers_inward_from_outside(
    positioned, start, target, mask, direction
):
    controller, transport, clock = positioned
    state = transport.motors["bottom"]
    state.position, state.switch_mask = start, mask
    controller.move_to_position("bottom", target, 0.25)
    assert axis(controller)["direction"] == direction
    clock.advance(4)
    controller.poll()
    assert axis(controller)["position"] == target and axis(controller)["calibration"] == "done"


@pytest.mark.parametrize(
    "target", [-1, -1e-12, None, True, "0.5", float("nan"), float("inf"), POSITION_LIMIT + 1]
)
def test_invalid_absolute_value_never_sends_commands(positioned, target):
    controller, transport, _ = positioned
    with pytest.raises(ValueError):
        controller.move_to_position("bottom", target, 0.25)
    assert transport.commands == []


@pytest.mark.parametrize("target", [1.00000001, 1.5, 100])
def test_target_outside_saved_range_is_rejected_even_if_rounding_would_fit(positioned, target):
    controller, transport, _ = positioned
    with pytest.raises(ControllerError, match="calibrated range"):
        controller.move_to_position("bottom", target, 0.25)
    assert transport.commands == []


@pytest.mark.parametrize("speed", [None, True, "0.25", float("nan"), float("inf"), 0, 0.601])
def test_invalid_absolute_speed_never_sends_commands(positioned, speed):
    controller, transport, _ = positioned
    with pytest.raises(ValueError):
        controller.move_to_position("bottom", 0.75, speed)
    assert transport.commands == []


@pytest.mark.parametrize(
    "change",
    [
        {"calibration": "none"},
        {"calibration": "cancelled"},
        {"calibration": "failed"},
        {"calibration": "lower"},
        {"calibration": "upper"},
        {"calibration": "center"},
        {"lower_limit_set": False},
        {"upper_limit_set": False},
        {"lower_limit": None, "lower_limit_set": False},
        {"upper_limit": float("nan")},
        {"lower_limit": -0.25},
        {"upper_limit": 0},
        {"lower_limit": 2},
        {"mode": "moving"},
        {"encoder": "offline"},
        {"fault": "stall"},
    ],
)
def test_readiness_requires_completed_calibration_valid_bounds_and_healthy_idle(positioned, change):
    controller, transport, _ = positioned
    state = transport.motors["bottom"]
    for field, value in change.items():
        setattr(state, field, value)
    controller.poll()
    assert not axis(controller)["positioning_ready"]
    with pytest.raises(ControllerError):
        controller.move_to_position("bottom", 0.5, 0.25)
    assert transport.commands == []


@pytest.mark.parametrize("field", ["telemetry_age_ms", "limit_telemetry_age_ms"])
def test_stale_feedback_disables_absolute_positioning(positioned, monkeypatch, field):
    controller, transport, _ = positioned
    original = transport.status

    def stale_status():
        states = original()
        states["bottom"] = replace(states["bottom"], **{field: 1001})
        return states

    monkeypatch.setattr(transport, "status", stale_status)
    controller.poll()
    assert not axis(controller)["positioning_ready"]
    with pytest.raises(ControllerError, match="fresh"):
        controller.move_to_position("bottom", 0.75, 0.25)
    assert transport.commands == []


@pytest.mark.parametrize("mask,target", [(1, 0), (2, 1)])
def test_pressed_contact_rejects_absolute_motion_into_it(positioned, mask, target):
    controller, transport, _ = positioned
    transport.motors["bottom"].switch_mask = mask
    with pytest.raises(ControllerError, match="home switch is pressed"):
        controller.move_to_position("bottom", target, 0.25)
    assert transport.commands == []


def test_trusted_fault_clear_keeps_completed_calibration(positioned):
    controller, transport, _ = positioned
    state = transport.motors["bottom"]
    state.fault = "stall"
    controller.clear_fault("bottom")
    assert axis(controller)["positioning_ready"] and axis(controller)["calibration"] == "done"
    transport.commands.clear()
    controller.move_to_position("bottom", 0.75, 0.25)
    assert transport.commands[-1] == "goto 0.75"


def test_untrusted_recovery_requires_clearing_bounds_and_new_calibration(positioned):
    controller, transport, _ = positioned
    state = transport.motors["bottom"]
    state.encoder, state.fault = "untrusted", "tracking"
    with pytest.raises(ControllerError, match="Clear both travel limits"):
        controller.clear_fault("bottom")
    controller.clear_lower_limit("bottom")
    controller.clear_upper_limit("bottom")
    controller.clear_fault("bottom")
    assert not axis(controller)["positioning_ready"] and axis(controller)["calibration"] == "none"
    with pytest.raises(ControllerError, match="Auto calibrate"):
        controller.move_to_position("bottom", 0, 0.25)


@pytest.mark.parametrize(
    "action", ["reset_position", "set_limit", "clear_lower_limit", "clear_upper_limit"]
)
def test_manual_position_or_bound_edit_requires_recalibration(positioned, action):
    controller, _, _ = positioned
    getattr(controller, action)("bottom")
    assert not axis(controller)["positioning_ready"]
    with pytest.raises(ControllerError, match="Auto calibrate"):
        controller.move_to_position("bottom", 0.5, 0.25)


def test_ordinary_moves_and_contact_stop_preserve_absolute_calibration(positioned):
    controller, transport, clock = positioned
    controller.move("bottom", "ccw", 0.25, 0.1)
    clock.advance(1)
    controller.poll()
    clock.advance(1.1)
    controller.poll()
    assert axis(controller)["positioning_ready"]
    controller.move_to_position("bottom", 1, 0.25)
    transport.motors["bottom"].switch_mask = 2
    controller.poll()
    assert axis(controller)["calibration"] == "done"
    assert axis(controller)["lower_limit_set"] and axis(controller)["upper_limit_set"]


def test_direct_demo_goto_requires_calibration_and_preserves_range(positioned):
    _, transport, _ = positioned
    state = transport.motors["bottom"]
    for command in ("goto -0.1", "goto 1.1"):
        with pytest.raises(CommandRejected):
            transport.command(command)
    state.calibration = "none"
    with pytest.raises(CommandRejected, match="Auto calibrate"):
        transport.command("goto 0.5")
    assert state.lower_limit == 0 and state.upper_limit == 1 and not transport.moves


@pytest.mark.parametrize(
    "outcome", ["complete", "stop", "stop_all", "disconnect", "fault", "offline", "contact"]
)
def test_absolute_motion_restores_configured_direction_on_every_exit(positioned, outcome):
    controller, transport, clock = positioned
    state = transport.motors["bottom"]
    state.direction = "cw"
    controller.move_to_position("bottom", 0.75, 0.25)
    assert state.direction == "ccw"
    clock.advance(0.25)
    controller.poll()
    if outcome == "complete":
        clock.advance(2)
    elif outcome == "stop":
        transport.command("stop")
    elif outcome in ("stop_all", "disconnect"):
        getattr(controller, outcome)()
    elif outcome == "fault":
        state.fault = "stall"
    elif outcome == "offline":
        state.encoder = "offline"
    else:
        state.switch_mask = 2
    controller.poll()
    assert state.direction == "cw" and state.mode == "idle"
    assert state.calibration == "done" and state.lower_limit_set and state.upper_limit_set
    assert not transport._absolute_directions
    stopped = state.position
    clock.advance(5)
    transport.status()
    assert state.position == stopped


@pytest.mark.parametrize("position,mask", [(0, 1), (1, 2)])
def test_reached_endpoint_noop_is_allowed_with_its_contact_pressed(positioned, position, mask):
    controller, transport, _ = positioned
    state = transport.motors["bottom"]
    state.position, state.switch_mask, state.direction = position, mask, "ccw"
    controller.move_to_position("bottom", position, 0.25)
    assert state.position == position and state.direction == "ccw"
    assert state.mode == "idle" and state.calibration == "done"
    assert axis(controller)["cooldown"] == 0


def test_serial_noop_confirmation_keeps_readiness_without_direction_command(switch_serial):
    reply = absolute_reply("done", position=0.5, lower=0, upper=4096, bounds=3)
    transport, port, clock = switch_serial(reply)
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [
            *all_frames(counts=2048),
            calibration_frame(4, counts=2048, phase="done", lower=0, upper=4096, bounds=3),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
        ]
    )
    controller.poll()
    port.writes.clear()
    port.lines.extend([*reply, b"OK: bottom motor\n", b"OK: bottom speed\n", b"OK: bottom goto\n"])
    controller.move_to_position("bottom", 0.5, 0.25)
    assert port.writes == [b"status\n", b"motor bottom\n", b"speed 0.25\n", b"goto 0.5\n"]
    assert axis(controller)["mode"] == "idle" and axis(controller)["cooldown"] == 0
    assert axis(controller)["positioning_ready"]
