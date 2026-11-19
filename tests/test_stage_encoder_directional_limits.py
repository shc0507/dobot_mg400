"""V2 names lower=CW and upper=CCW without changing encoder count direction."""

import pytest
from test_stage_encoder_limits import axis
from test_stage_encoder_switches import HEADERS, limit_frame, switch_reply, switch_rows
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    POSITION_LIMIT,
    CommandRejected,
    ControllerError,
    LinkError,
    parse_limit_telemetry,
)

V2_HEADERS = [b"switches=analog_pair_v2\n", b"limits=directional_v2\n", *HEADERS[2:]]


@pytest.fixture(name="stage_rig")
def legacy_coordinate_rig(stage_rig):
    """Retain v2 CW-positive coverage after the default demo moves to CCW-positive."""
    controller, transport, _ = stage_rig
    transport.positive_direction = "cw"
    transport.lower_home_zero = True
    transport.calibration_only_homing = False
    for state in transport.motors.values():
        state.positive_direction = "cw"
        state.lower_limit, state.upper_limit = POSITION_LIMIT, -POSITION_LIMIT
    controller.poll()
    return stage_rig


def directional_rows(*, lower=1_000_000_000, upper=-1_000_000_000, **kwargs):
    return switch_rows(lower=lower, upper=upper, **kwargs)


def directional_reply(**kwargs):
    return [*V2_HEADERS, *directional_rows(**kwargs)]


def directional_frame(seq, motor="bottom", *, lower=1_000_000_000, upper=-1_000_000_000, **kwargs):
    return limit_frame(seq, motor, lower=lower, upper=upper, **kwargs)


@pytest.mark.parametrize(
    "lower,upper,bounds",
    [
        (1_000_000_000, -1_000_000_000, 0),
        (4096, -2048, 3),
        (-2048, -4096, 3),
        (4096, 2048, 3),
        (0, 0, 3),
        (-2048, -1_000_000_000, 1),
        (1_000_000_000, 2048, 2),
    ],
)
def test_v2_status_and_lim_use_named_direction_not_numeric_sign(
    switch_serial, lower, upper, bounds
):
    transport, port, _ = switch_serial(directional_reply(lower=lower, upper=upper, bounds=bounds))
    state = transport.status()["bottom"]
    assert transport.lower_limit_direction == state.lower_limit_direction == "cw"
    assert state.lower_limit == lower / 4096 and state.upper_limit == upper / 4096
    port.lines.extend(
        [
            *all_frames(),
            directional_frame(4, lower=lower, upper=upper, bounds=bounds, mask=1, raw=250),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.lower_limit_set == bool(bounds & 1)
    assert state.upper_limit_set == bool(bounds & 2)
    assert state.switch_mask == 1 and state.switch_raw == 250
    assert state.lower_limit_direction == "cw"


@pytest.mark.parametrize(
    "values",
    [
        {"lower": -1_000_000_000, "upper": 1_000_000_000},
        {"lower": -1, "upper": 1, "bounds": 3},
        {"lower": 0, "bounds": 0},
        {"upper": 0, "bounds": 0},
        {"lower": 1_000_000_001, "bounds": 1},
        {"upper": -1_000_000_001, "bounds": 2},
    ],
)
def test_v2_rejects_inverted_bounds_and_wrong_disabled_sentinels(switch_serial, values):
    transport, _, _ = switch_serial(directional_reply(**values))
    with pytest.raises(LinkError):
        transport.status()
    with pytest.raises(LinkError):
        parse_limit_telemetry(
            directional_frame(1, **values).decode().strip(), lower_limit_direction="cw"
        )


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("zero", [False, True])
def test_early_lim_before_first_capability_waits_for_version_including_all_zero_bounds(
    switch_serial, version, zero
):
    kwargs = {"lower": 0, "upper": 0, "bounds": 3} if zero else {}
    make_frame = directional_frame if version == "v2" else limit_frame
    make_reply = directional_reply if version == "v2" else switch_reply
    early = make_frame(1, mask=1, raw=250, **kwargs)
    transport, port, _ = switch_serial([early[:22], early[22:], *make_reply(**kwargs)])
    state = transport.status()["bottom"]
    assert state.lower_limit_direction == ("cw" if version == "v2" else "ccw")
    assert state.limit_telemetry_age_ms == 0
    # A newer status row wins over the older frame's switch mask.
    assert state.switch_mask == 0
    port.lines.extend([*all_frames(2), make_frame(5, mask=1, raw=250, **kwargs)])
    state = transport.poll_status()["bottom"]
    assert state.switch_mask == 1


def test_v2_lim_after_first_row_merges_with_that_rows_version(switch_serial):
    rows = directional_rows()
    transport, _, _ = switch_serial(
        [
            *V2_HEADERS,
            rows[0],
            directional_frame(1, lower=0, bounds=1, mask=1, raw=250),
            *rows[1:],
        ]
    )
    state = transport.status()["bottom"]
    assert state.lower_limit_direction == "cw"
    assert state.lower_limit == 0 and state.lower_limit_set
    assert state.switch_mask == 1


def test_every_early_lim_is_validated_even_if_a_later_frame_replaces_it(switch_serial):
    transport, _, _ = switch_serial(
        [
            limit_frame(1),
            directional_frame(2),
            *directional_reply(),
        ]
    )
    with pytest.raises(LinkError, match="ordering or defaults"):
        transport.status()


@pytest.mark.parametrize("v2_switch", [False, True])
def test_switch_and_limit_capability_versions_must_match(switch_serial, v2_switch):
    headers = [V2_HEADERS[0], *HEADERS[1:]] if v2_switch else [HEADERS[0], *V2_HEADERS[1:]]
    rows = switch_rows() if v2_switch else directional_rows()
    transport, _, _ = switch_serial([*headers, *rows])
    with pytest.raises(LinkError, match="conventions do not match"):
        transport.status()


@pytest.mark.parametrize("first_v2", [False, True])
def test_established_limit_version_cannot_change_without_reconnecting(switch_serial, first_v2):
    first, second = (
        (directional_reply, switch_reply) if first_v2 else (switch_reply, directional_reply)
    )
    transport, port, _ = switch_serial(first())
    transport.status()
    port.lines.extend(second())
    with pytest.raises(LinkError, match="convention changed"):
        transport.status()


def test_known_v2_rejects_legacy_lim_immediately(switch_serial):
    transport, port, _ = switch_serial(directional_reply())
    transport.status()
    port.lines.append(limit_frame(1))
    with pytest.raises(LinkError, match="ordering or defaults"):
        transport.poll_status()


def test_v2_status_after_reset_wins_over_pre_command_lim(switch_serial):
    transport, port, _ = switch_serial(directional_reply(lower=2048, bounds=1, mask=1, raw=250))
    transport.status()
    port.lines.extend(
        [
            directional_frame(1, lower=2048, bounds=1, mask=1, raw=250),
            b"OK: bottom reset\n",
        ]
    )
    transport.command("reset")
    port.lines.extend(directional_reply(mask=1, raw=250))
    state = transport.status()["bottom"]
    assert state.lower_limit == POSITION_LIMIT and state.upper_limit == -POSITION_LIMIT
    assert not state.lower_limit_set and not state.upper_limit_set
    assert state.switch_mask == 1
    port.lines.extend(all_frames(2))
    assert not transport.poll_status()["bottom"].lower_limit_set


@pytest.mark.parametrize("version,direction", [("v1", "ccw"), ("v2", "cw")])
def test_controller_exposes_negotiated_direction(switch_serial, version, direction):
    transport, _, clock = switch_serial(directional_reply() if version == "v2" else switch_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    state = controller.connect("DEMO")
    assert state["lower_limit_direction"] == direction
    assert all(motor["lower_limit_direction"] == direction for motor in state["motors"])


def test_demo_defaults_and_manual_set_follow_v2_direction(stage_rig):
    controller, transport, _ = stage_rig
    assert controller.snapshot()["lower_limit_direction"] == "cw"
    assert axis(controller)["lower_limit"] == POSITION_LIMIT
    assert axis(controller)["upper_limit"] == -POSITION_LIMIT
    state = transport.motors["bottom"]
    state.position = 0.5
    controller.set_limit("bottom")
    assert axis(controller)["lower_limit"] == 0.5 and axis(controller)["lower_limit_set"]
    assert not axis(controller)["upper_limit_set"]
    state.position = -0.25
    controller.set_limit("bottom")
    assert axis(controller)["upper_limit"] == -0.25 and axis(controller)["upper_limit_set"]
    assert axis(controller)["lower_limit"] == 0.5
    assert transport.commands == ["motor bottom", "setlimit"] * 2


def test_reset_preserves_fault_and_contact_but_clears_v2_limits(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = 0.5
    state.lower_limit, state.upper_limit = 0.5, -0.5
    state.lower_limit_set = state.upper_limit_set = True
    state.fault, state.switch_mask = "stall", 1
    controller.reset_position("bottom")
    after = axis(controller)
    assert after["position"] == 0 and after["fault"] == "stall" and after["switch_mask"] == 1
    assert after["lower_limit"] == POSITION_LIMIT and after["upper_limit"] == -POSITION_LIMIT
    assert not after["lower_limit_set"] and not after["upper_limit_set"]
    controller.clear_fault("bottom")
    with pytest.raises(ControllerError, match="lower home switch is pressed"):
        controller.move("bottom", "cw", 0.25, 0.1)


@pytest.mark.parametrize(
    "side,mask,direction,default",
    [
        ("lower", 1, "cw", POSITION_LIMIT),
        ("upper", 2, "ccw", -POSITION_LIMIT),
    ],
)
def test_v2_clear_is_idempotent_and_cannot_bypass_pressed_switch(
    stage_rig, side, mask, direction, default
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = 0.5, -0.5
    state.lower_limit_set = state.upper_limit_set = True
    state.switch_mask = mask
    action = getattr(controller, f"clear_{side}_limit")
    action("bottom")
    action("bottom")
    assert axis(controller)[f"{side}_limit"] == default
    assert not axis(controller)[f"{side}_limit_set"]
    opposite = "upper" if side == "lower" else "lower"
    assert axis(controller)[f"{opposite}_limit_set"]
    with pytest.raises(ControllerError, match=f"{side} home switch is pressed"):
        controller.move("bottom", direction, 0.25, 0.1)


@pytest.mark.parametrize(
    "mask,direction,side",
    [
        (1, "cw", "lower"),
        (2, "ccw", "upper"),
        (3, "cw", "lower"),
        (3, "ccw", "upper"),
    ],
)
@pytest.mark.parametrize("held", [False, True])
def test_v2_pressed_switch_blocks_its_physical_direction_even_without_bounds(
    stage_rig, mask, direction, side, held
):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].switch_mask = mask
    with pytest.raises(ControllerError, match=f"{side} home switch is pressed"):
        if held:
            controller.hold("bottom", direction, 0.25, "v2-held-switch-01", clock())
        else:
            controller.move("bottom", direction, 0.25, 0.1)
    assert not transport.commands


@pytest.mark.parametrize("mask,direction", [(1, "ccw"), (2, "cw")])
def test_v2_motion_away_from_contact_preserves_encoder_sign(stage_rig, mask, direction):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].switch_mask = mask
    controller.move("bottom", direction, 0.25, 0.1)
    clock.advance(1)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(0.1 if direction == "cw" else -0.1)


@pytest.mark.parametrize(
    "lower,upper,position,direction,side",
    [
        (0.5, -0.25, 0.4, "cw", "lower"),
        (0.5, -0.25, -0.2, "ccw", "upper"),
        (-0.5, -1, -0.6, "cw", "lower"),
        (1, 0.5, 0.6, "ccw", "upper"),
    ],
)
def test_v2_finite_bounds_reject_outward_target_using_named_direction(
    stage_rig, lower, upper, position, direction, side
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit, state.position = lower, upper, position
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ControllerError, match=f"beyond the {side} travel limit"):
        controller.move("bottom", direction, 0.25, 0.2)
    assert not transport.commands


@pytest.mark.parametrize(
    "position,direction,target",
    [
        (1, "ccw", 0.9),
        (-1, "cw", -0.9),
        (0.4, "cw", 0.5),
        (-0.15, "ccw", -0.25),
    ],
)
def test_v2_exact_limit_and_partial_inward_recovery_are_allowed(
    stage_rig, position, direction, target
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit, state.position = 0.5, -0.25, position
    state.lower_limit_set = state.upper_limit_set = True
    controller.move("bottom", direction, 0.25, 0.1)
    clock.advance(1)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(target)


@pytest.mark.parametrize("direction,position,side", [("cw", 0.1, "lower"), ("ccw", -0.1, "upper")])
def test_v2_held_motion_stops_at_correct_named_bound(stage_rig, direction, position, side):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = 0.1, -0.1
    state.lower_limit_set = state.upper_limit_set = True
    controller.hold("bottom", direction, 0.25, "v2-held-limit-001", clock())
    clock.advance(0.95)
    controller.poll()
    assert axis(controller)["position"] == position
    assert axis(controller)["mode"] == "idle" and axis(controller)["cooldown"] == 1
    with pytest.raises(ControllerError, match="ended"):
        controller.keep_hold_alive("bottom", "v2-held-limit-001")
    clock.advance(1)
    with pytest.raises(ControllerError, match=f"{side.title()} travel limit reached"):
        controller.hold("bottom", direction, 0.25, "v2-held-limit-002", clock())


@pytest.mark.parametrize("command", ["rev 0.1", "hold"])
def test_direct_demo_commands_obey_reversed_contact_definition(stage_rig, command):
    _, transport, _ = stage_rig
    transport.motors["bottom"].switch_mask = 1
    with pytest.raises(CommandRejected, match="lower home switch is pressed"):
        transport.command(command)


def test_unsupported_limit_capability_exposes_no_direction(stage_rig):
    controller, transport, _ = stage_rig
    transport.travel_limits = False
    controller.poll()
    assert controller.snapshot()["lower_limit_direction"] is None
