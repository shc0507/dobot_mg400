"""CCW-positive coordinates change reported signs, never physical directions."""

import pytest
from test_stage_encoder_directional_limits import directional_reply
from test_stage_encoder_limits import axis, limits_reply
from test_stage_encoder_lower_home import HOME_HEADER, homing_frame, homing_reply
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame
from test_stage_protocol import STATUS

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    POSITION_LIMIT,
    CommandRejected,
    ControllerError,
    LinkError,
)

COORDINATES = b"coordinates=ccw_positive_v1\n"


def ccw_reply(*, lower=-1_000_000_000, upper=1_000_000_000, **kwargs):
    return [COORDINATES, *homing_reply(lower=lower, upper=upper, **kwargs)]


def ccw_frame(seq, motor="bottom", *, lower=-1_000_000_000, upper=1_000_000_000, **kwargs):
    return homing_frame(seq, motor, lower=lower, upper=upper, **kwargs)


@pytest.mark.parametrize(
    "position,lower,upper,bounds",
    [
        (0, -1_000_000_000, 1_000_000_000, 0),
        (0, 0, 4096, 3),
        (-0.5, -4096, -2048, 3),
        (0.5, 2048, 4096, 3),
        (0, 0, 0, 3),
    ],
)
def test_status_and_lim_report_ccw_positive_and_correct_numeric_bound_order(
    switch_serial, position, lower, upper, bounds
):
    transport, port, _ = switch_serial(
        ccw_reply(position=position, lower=lower, upper=upper, bounds=bounds)
    )
    state = transport.status()["bottom"]
    assert transport.positive_direction == state.positive_direction == "ccw"
    assert state.lower_limit_direction == "cw"
    assert state.position == position
    assert state.lower_limit == lower / 4096 and state.upper_limit == upper / 4096
    port.lines.extend(
        [
            *all_frames(direction="ccw", counts=1024, remaining=2048, speed=0.125),
            ccw_frame(4, counts=1024, lower=lower, upper=upper, bounds=bounds),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.position == 0.25 and state.remaining == 0.5 and state.measured_speed == 0.125


@pytest.mark.parametrize(
    "old_reply,lower_direction",
    [
        (limits_reply, "ccw"),
        (directional_reply, "cw"),
        (homing_reply, "cw"),
    ],
)
def test_missing_coordinate_header_preserves_all_legacy_conventions(
    switch_serial, old_reply, lower_direction
):
    transport, _, clock = switch_serial(old_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    result = controller.connect("DEMO")
    assert result["positive_direction"] == "cw"
    assert result["lower_limit_direction"] == lower_direction
    assert all(state["positive_direction"] == "cw" for state in result["motors"])


@pytest.mark.parametrize(
    "reply",
    [
        [b"coordinates=ccw_positive_v2\n", *homing_reply()],
        [COORDINATES, *ccw_reply()],
        [HOME_HEADER, COORDINATES, *homing_reply()[1:]],
        [b"coordinates ccw_positive_v1\n", *homing_reply()],
        [COORDINATES, *STATUS],
    ],
)
def test_coordinates_header_is_first_unique_known_and_requires_closed_loop(switch_serial, reply):
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("first_ccw", [False, True])
def test_coordinate_convention_is_pinned_until_reconnect(switch_serial, first_ccw):
    first, second = (ccw_reply, homing_reply) if first_ccw else (homing_reply, ccw_reply)
    # Both-zero bounds are legal in either convention: rejection must come from
    # the capability change, not incidental numeric-bound validation.
    transport, port, _ = switch_serial(first(lower=0, upper=0, bounds=3))
    transport.status()
    port.lines.extend(second(lower=0, upper=0, bounds=3))
    with pytest.raises(LinkError, match="coordinate convention changed"):
        transport.status()


@pytest.mark.parametrize("zero", [False, True])
def test_early_lim_waits_for_both_coordinate_and_limit_conventions(switch_serial, zero):
    values = {"lower": 0, "upper": 0, "bounds": 3} if zero else {}
    early = ccw_frame(1, counts=0, **values)
    transport, _, _ = switch_serial([early[:55], early[55:], *ccw_reply(**values)])
    state = transport.status()["bottom"]
    assert state.positive_direction == "ccw" and state.lower_limit_direction == "cw"
    assert state.limit_telemetry_age_ms == 0


def test_early_lim_with_old_directional_defaults_is_rejected_under_new_coordinates(switch_serial):
    transport, _, _ = switch_serial([homing_frame(1), *ccw_reply()])
    with pytest.raises(LinkError, match="ordering or defaults"):
        transport.status()


@pytest.mark.parametrize(
    "lower,upper,bounds",
    [
        (1_000_000_000, -1_000_000_000, 0),
        (2048, -2048, 3),
        (0, 1_000_000_000, 0),
        (-1_000_000_000, 0, 0),
    ],
)
def test_new_coordinates_reject_old_order_or_wrong_disabled_defaults(
    switch_serial, lower, upper, bounds
):
    transport, _, _ = switch_serial(ccw_reply(lower=lower, upper=upper, bounds=bounds))
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("direction,expected", [("cw", -0.1), ("ccw", 0.1)])
def test_serial_optimistic_remaining_uses_negotiated_sign_without_changing_commands(
    switch_serial, direction, expected
):
    transport, port, _ = switch_serial(ccw_reply())
    transport.status()
    port.lines.extend(
        [
            *all_frames(),
            ccw_frame(4),
            b"OK: bottom dir\n",
            b"OK: bottom rev\n",
        ]
    )
    # Old telemetry is flushed before the direction ACK. The accepted command
    # then establishes the physical direction used for optimistic remaining.
    transport.command(f"dir {direction}")
    transport.command("rev 0.1")
    state = transport.poll_status()["bottom"]
    assert state.remaining == expected and state.mode == "moving"


@pytest.mark.parametrize("direction,sign,raw", [("cw", -1, 1024), ("ccw", 1, 3072)])
@pytest.mark.parametrize("motor", ["bottom", "top", "insert"])
def test_demo_sign_flip_preserves_motor_selection_physical_direction_and_raw_angle(
    stage_rig, direction, sign, raw, motor
):
    controller, transport, clock = stage_rig
    controller.move(motor, direction, 0.25, 0.25)
    assert transport.commands == [f"motor {motor}", f"dir {direction}", "speed 0.25", "rev 0.25"]
    assert axis(controller, motor)["remaining"] == sign * 0.25
    clock.advance(0.5)
    controller.poll()
    assert axis(controller, motor)["position"] == sign * 0.125
    assert axis(controller, motor)["measured_speed"] == sign * 0.25
    clock.advance(0.5)
    controller.poll()
    state = axis(controller, motor)
    assert state["position"] == sign * 0.25 and state["remaining"] == 0
    assert state["encoder_raw"] == raw
    assert state["encoder_channel"] == {"bottom": 0, "top": 2, "insert": 1}[motor]


@pytest.mark.parametrize("position", [-0.125, 0.125])
@pytest.mark.parametrize("action", ["reset_position", "clear_fault"])
def test_rezeroing_preserves_physical_raw_angle_in_ccw_positive_coordinates(
    stage_rig, position, action
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = position
    if action == "clear_fault":
        state.encoder, state.fault = "untrusted", "tracking"
    before_raw = transport.status()["bottom"].encoder_raw
    getattr(controller, action)("bottom")
    assert axis(controller)["position"] == 0
    assert axis(controller)["encoder_raw"] == before_raw == round(-position * 4096) % 4096


def test_positive_manual_limit_is_upper_and_negative_is_lower(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = 0.5
    controller.set_limit("bottom")
    assert axis(controller)["upper_limit"] == 0.5 and axis(controller)["upper_limit_set"]
    assert not axis(controller)["lower_limit_set"]
    state.position = -0.25
    controller.set_limit("bottom")
    assert axis(controller)["lower_limit"] == -0.25 and axis(controller)["lower_limit_set"]
    assert axis(controller)["upper_limit"] == 0.5


@pytest.mark.parametrize("position,lower,upper", [(0.25, 0.5, 1), (-0.25, -1, -0.5)])
def test_manual_limit_cannot_cross_opposite_saved_bound(stage_rig, position, lower, upper):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position, state.lower_limit, state.upper_limit = position, lower, upper
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(CommandRejected, match="cross the opposite"):
        controller.set_limit("bottom")


@pytest.mark.parametrize("direction,side,position", [("cw", "lower", -0.2), ("ccw", "upper", 0.4)])
def test_finite_limits_enforce_the_same_physical_direction_with_reversed_signs(
    stage_rig, direction, side, position
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit, state.position = -0.25, 0.5, position
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ControllerError, match=f"beyond the {side} travel limit"):
        controller.move("bottom", direction, 0.25, 0.2)
    assert not transport.commands


@pytest.mark.parametrize(
    "position,direction", [(0.5, "ccw"), (0.6, "ccw"), (-0.25, "cw"), (-0.5, "cw")]
)
def test_outward_hold_at_or_beyond_a_ccw_positive_limit_is_rejected(stage_rig, position, direction):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position = position
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ControllerError, match="limit"):
        controller.hold("bottom", direction, 0.25, "ccw-coord-hold-02", clock())
    assert not transport.commands


@pytest.mark.parametrize("direction,position", [("cw", -0.1), ("ccw", 0.1)])
def test_held_motion_stops_at_reversed_coordinate_bound_without_software_rezero(
    stage_rig, direction, position
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -0.1, 0.1
    state.lower_limit_set = state.upper_limit_set = True
    controller.hold("bottom", direction, 0.25, "ccw-coord-hold-01", clock())
    clock.advance(0.95)
    controller.poll()
    after = axis(controller)
    assert after["mode"] == "idle" and after["position"] == position
    assert after["lower_limit"] == -0.1 and after["upper_limit"] == 0.1


@pytest.mark.parametrize("position,direction,expected", [(-1, "ccw", -0.9), (1, "cw", 0.9)])
def test_partial_inward_recovery_still_works_outside_new_coordinate_bounds(
    stage_rig, position, direction, expected
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit, state.position = -0.25, 0.5, position
    state.lower_limit_set = state.upper_limit_set = True
    controller.move("bottom", direction, 0.25, 0.1)
    clock.advance(1)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(expected)


@pytest.mark.parametrize("mask,direction,side", [(1, "cw", "lower"), (2, "ccw", "upper")])
def test_physical_switch_masks_do_not_flip_with_display_sign(stage_rig, mask, direction, side):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].switch_mask = mask
    with pytest.raises(ControllerError, match=f"{side} home switch is pressed"):
        controller.move("bottom", direction, 0.25, 0.1)
    controller.reset_position("bottom")
    with pytest.raises(ControllerError, match=f"{side} home switch is pressed"):
        controller.move("bottom", direction, 0.25, 0.1)


@pytest.mark.parametrize("direction,sign", [("cw", -1), ("ccw", 1)])
def test_count_range_guards_follow_positive_direction(stage_rig, direction, sign):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].position = sign * POSITION_LIMIT
    with pytest.raises(ControllerError, match="position range"):
        controller.move("bottom", direction, 0.25, 0.1)
    with pytest.raises(ControllerError, match="position range"):
        controller.hold("bottom", direction, 0.25, "ccw-coordinate-range", clock())


def test_ccw_positive_lower_home_preserves_positive_upper_and_coherent_position(switch_serial):
    transport, port, clock = switch_serial(ccw_reply(position=-0.5, upper=4096, bounds=2))
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.writes.clear()
    # The actual lower contact was at public -0.5; rebasing changes saved upper
    # from +1 to +1.5. An earlier TEL may still describe the pre-home position.
    port.lines.extend(
        [
            *all_frames(direction="cw", counts=-2048, speed=-0.25),
            ccw_frame(4, counts=0, lower=0, upper=6144, bounds=3, mask=1, raw=250),
            ccw_frame(5, "top"),
            ccw_frame(6, "insert"),
        ]
    )
    controller.poll()
    assert axis(controller)["position"] == axis(controller)["lower_limit"] == 0
    assert axis(controller)["upper_limit"] == 1.5
    assert port.writes == []
    port.lines.extend(
        [
            *ccw_reply(lower=0, upper=6144, bounds=3, mask=1, raw=250),
            b"OK: bottom motor\n",
            b"OK: bottom dir\n",
            b"OK: bottom speed\n",
            b"OK: bottom rev\n",
        ]
    )
    controller.move("bottom", "ccw", 0.25, 0.1)
    assert axis(controller)["remaining"] == 0.1
    port.lines.extend(
        [
            frame(7, direction="ccw", counts=410, speed=0.25),
            ccw_frame(8, counts=410, lower=0, upper=6144, bounds=3),
        ]
    )
    controller.poll()
    assert axis(controller)["position"] == 410 / 4096
    assert not any(b"reset" in command for command in port.writes)
