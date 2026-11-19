"""Position reset and measured travel limits, including legacy v1 semantics."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_stage_encoder_telemetry import all_frames
from test_stage_protocol import CLOSED_STATUS, FakeSerial, closed_reply

from stage_controller import protocol
from stage_controller.protocol import (
    POSITION_LIMIT,
    CommandRejected,
    ControllerError,
    LinkError,
)


def rows_with_limits(lower=-1_000_000_000, upper=1_000_000_000, bounds=0):
    suffix = f" lower={lower} upper={upper} bounds={bounds}\n".encode()
    return [line.rstrip() + suffix for line in CLOSED_STATUS]


def limits_reply(rows=None, *, streaming=False):
    headers = [b"limits=soft_v1\n"]
    if streaming:
        headers.append(b"telemetry=positions_v1\n")
    return [*headers, *closed_reply(rows if rows is not None else rows_with_limits())]


@pytest.fixture
def serial_limits(monkeypatch):
    def create(lines):
        port = FakeSerial(lines)
        monkeypatch.setattr(protocol.serial, "Serial", lambda **_kwargs: port)
        monkeypatch.setattr(protocol.time, "sleep", lambda _: None)
        return protocol.SerialTransport("COM7"), port

    return create


def axis(controller, name="bottom"):
    return next(state for state in controller.snapshot()["motors"] if state["id"] == name)


@pytest.fixture(name="stage_rig")
def legacy_limits_rig(stage_rig):
    """Keep coverage of installed v1 boards while the default demo uses v2."""
    controller, transport, _ = stage_rig
    transport.lower_limit_direction = "ccw"
    transport.positive_direction = "cw"
    transport.lower_home_zero = False
    transport.calibration_only_homing = False
    for state in transport.motors.values():
        state.lower_limit_direction = "ccw"
        state.positive_direction = "cw"
        state.lower_limit, state.upper_limit = -POSITION_LIMIT, POSITION_LIMIT
    controller.poll()
    return stage_rig


def test_limit_capability_parses_signed_count_bounds_in_rotation_units(serial_limits):
    transport, _ = serial_limits(limits_reply(rows_with_limits(-2048, 4096, 3)))
    statuses = transport.status()
    assert transport.travel_limits
    assert statuses["bottom"].lower_limit == -0.5
    assert statuses["bottom"].upper_limit == 1
    assert statuses["bottom"].lower_limit_set is True
    assert statuses["bottom"].upper_limit_set is True


def test_disabled_limits_are_explicit_default_sentinels(serial_limits):
    transport, _ = serial_limits(limits_reply())
    state = transport.status()["bottom"]
    assert state.lower_limit == -POSITION_LIMIT
    assert state.upper_limit == POSITION_LIMIT
    assert state.lower_limit_set is False and state.upper_limit_set is False


@pytest.mark.parametrize(
    "lower,upper,bounds",
    [
        (-1_000_000_001, 1_000_000_000, 1),
        (-1_000_000_000, 1_000_000_001, 2),
        (10, -10, 3),
        (-4096, 4096, 4),
        (-4096, 4096, 0),
        (-4096, 4096, 1),
        (-4096, 4096, 2),
    ],
)
def test_invalid_limit_ranges_or_flags_are_rejected(serial_limits, lower, upper, bounds):
    transport, _ = serial_limits(limits_reply(rows_with_limits(lower, upper, bounds)))
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("header", [b"limits=soft_v2\n", b"limits=bad\n", b"limits soft_v1\n"])
def test_unknown_limit_capability_is_rejected(serial_limits, header):
    transport, _ = serial_limits([header, *closed_reply(rows_with_limits())])
    with pytest.raises(LinkError):
        transport.status()


def test_duplicate_limit_header_is_rejected(serial_limits):
    transport, _ = serial_limits([b"limits=soft_v1\n", *limits_reply()])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("reply", [limits_reply(CLOSED_STATUS), closed_reply(rows_with_limits())])
def test_capability_and_limit_fields_must_agree(serial_limits, reply):
    transport, _ = serial_limits(reply)
    with pytest.raises(LinkError):
        transport.status()


def test_old_firmware_does_not_retain_previous_limit_capability(serial_limits):
    transport, port = serial_limits(limits_reply())
    transport.status()
    port.lines.extend(closed_reply())
    states = transport.status()
    assert not transport.travel_limits
    assert all(
        state.lower_limit is None and state.lower_limit_set is None for state in states.values()
    )


def test_telemetry_keeps_last_confirmed_limits(serial_limits):
    transport, port = serial_limits(limits_reply(rows_with_limits(-4096, 8192, 3), streaming=True))
    transport.status()
    port.lines.extend(all_frames(counts=2048))
    state = transport.poll_status()["bottom"]
    assert state.position == 0.5
    assert state.lower_limit == -1 and state.upper_limit == 2
    assert state.lower_limit_set and state.upper_limit_set


@pytest.mark.parametrize("verb", ["reset", "setlimit", "clearupper", "clearlower"])
def test_limit_commands_require_matching_selected_motor_ack(serial_limits, verb):
    transport, port = serial_limits([b"OK: top motor\n", f"OK: top {verb}\n".encode()])
    transport.command("motor top")
    transport.command(verb)
    assert port.writes == [b"motor top\n", f"{verb}\n".encode()]


def test_set_limit_uses_sign_and_preserves_the_other_axis_and_bound(stage_rig):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].position = 0.5
    controller.set_limit("bottom")
    assert transport.commands == ["motor bottom", "setlimit"]
    assert axis(controller)["upper_limit"] == 0.5
    transport.motors["bottom"].position = -0.25
    controller.set_limit("bottom")
    state = axis(controller)
    assert state["lower_limit"] == -0.25 and state["upper_limit"] == 0.5
    assert state["lower_limit_set"] and state["upper_limit_set"]
    assert not axis(controller, "top")["lower_limit_set"]
    assert not axis(controller, "top")["upper_limit_set"]


def test_set_limit_at_zero_is_rejected_without_serial_commands(stage_rig):
    controller, transport, _ = stage_rig
    with pytest.raises(ControllerError, match="zero"):
        controller.set_limit("bottom")
    assert not transport.commands
    with pytest.raises(CommandRejected, match="zero"):
        transport.command("setlimit")


@pytest.mark.parametrize("captured,side", [(0.75, "upper"), (-0.25, "lower")])
def test_set_limit_uses_firmware_capture_after_manual_drift(stage_rig, monkeypatch, captured, side):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].position = 0.25
    original = transport.command

    def fresh_capture(command):
        if command == "setlimit":
            transport.motors["bottom"].position = captured
        original(command)

    monkeypatch.setattr(transport, "command", fresh_capture)
    controller.set_limit("bottom")
    assert axis(controller)[f"{side}_limit"] == captured
    assert axis(controller)[f"{side}_limit_set"]
    assert transport.commands == ["motor bottom", "setlimit"]


def test_reset_clears_both_limits_keeps_fault_and_does_not_change_physical_raw_angle(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["top"]
    state.position = 0.25
    state.lower_limit, state.upper_limit = -0.5, 1
    state.lower_limit_set = state.upper_limit_set = True
    state.fault = "stall"
    before_raw = transport.status()["top"].encoder_raw
    result = controller.reset_position("top")
    after = next(item for item in result["motors"] if item["id"] == "top")
    assert transport.commands == ["motor top", "reset"]
    assert after["position"] == 0 and after["mode"] == "idle"
    assert after["lower_limit"] == -POSITION_LIMIT and after["upper_limit"] == POSITION_LIMIT
    assert not after["lower_limit_set"] and not after["upper_limit_set"]
    assert after["fault"] == "stall"
    assert after["encoder_raw"] == before_raw
    assert not after["available"]


@pytest.mark.parametrize(
    "method,verb,side",
    [
        ("clear_upper_limit", "clearupper", "upper"),
        ("clear_lower_limit", "clearlower", "lower"),
    ],
)
def test_clearing_one_bound_is_idempotent_and_allowed_without_encoder(
    stage_rig, method, verb, side
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.encoder, state.fault = "offline", "encoder"
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    opposite = "lower" if side == "upper" else "upper"
    for _ in range(2):
        getattr(controller, method)("bottom")
    after = axis(controller)
    assert after[f"{side}_limit_set"] is False
    assert after[f"{opposite}_limit_set"] is True
    assert after[f"{opposite}_limit"] == (-0.25 if opposite == "lower" else 0.5)
    assert after["encoder"] == "offline" and after["fault"] == "encoder"
    assert transport.commands == ["motor bottom", verb, "motor bottom", verb]


@pytest.mark.parametrize(
    "method", ["reset_position", "set_limit", "clear_upper_limit", "clear_lower_limit"]
)
def test_travel_configuration_requires_idle_motor(stage_rig, method):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 0.25, 0.5)
    transport.commands.clear()
    with pytest.raises(ControllerError, match="Stop"):
        getattr(controller, method)("bottom")
    assert not transport.commands


@pytest.mark.parametrize("method", ["reset_position", "set_limit"])
@pytest.mark.parametrize("health", ["offline", "magnet", "untrusted"])
def test_reset_and_set_require_trusted_encoder(stage_rig, method, health):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].encoder = health
    transport.motors["bottom"].position = 0.5
    with pytest.raises(ControllerError, match="encoder"):
        getattr(controller, method)("bottom")
    assert not transport.commands


@pytest.mark.parametrize(
    "method", ["reset_position", "set_limit", "clear_upper_limit", "clear_lower_limit"]
)
def test_old_firmware_rejects_new_controls_but_preserves_existing_motion(stage_rig, method):
    controller, transport, _ = stage_rig
    transport.travel_limits = False
    with pytest.raises(ControllerError, match="Upload"):
        getattr(controller, method)("bottom")
    assert not transport.commands
    assert not controller.snapshot()["travel_limits"]
    controller.move("bottom", "cw", 0.25, 0.1)
    assert transport.commands[-1] == "rev 0.1"


@pytest.mark.parametrize(
    "method,verb",
    [
        ("reset_position", "reset"),
        ("set_limit", "setlimit"),
        ("clear_upper_limit", "clearupper"),
        ("clear_lower_limit", "clearlower"),
    ],
)
def test_travel_changes_require_actual_readback(stage_rig, monkeypatch, method, verb):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = 0.25
    state.lower_limit, state.upper_limit = -0.5, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    original = transport.command

    def ignore_change(command):
        if command != verb:
            original(command)

    monkeypatch.setattr(transport, "command", ignore_change)
    with pytest.raises(ControllerError, match="confirm"):
        getattr(controller, method)("bottom")
    assert controller.snapshot()["connected"]


def test_lost_tracking_requires_both_bounds_cleared_before_rezeroing_recovery(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = 0.5
    state.lower_limit, state.upper_limit = -1, 1
    state.lower_limit_set = state.upper_limit_set = True
    state.encoder, state.fault = "untrusted", "encoder"
    with pytest.raises(ControllerError, match="Clear both"):
        controller.clear_fault("bottom")
    controller.clear_upper_limit("bottom")
    with pytest.raises(ControllerError, match="Clear both"):
        controller.clear_fault("bottom")
    controller.clear_lower_limit("bottom")
    controller.clear_fault("bottom")
    assert axis(controller)["position"] == 0 and axis(controller)["fault"] == "none"


@pytest.mark.parametrize("direction,rotations", [("cw", 0.501), ("ccw", 0.251)])
def test_move_rejects_target_outside_bounds_without_sending_motion(stage_rig, direction, rotations):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ControllerError, match="limit"):
        controller.move("bottom", direction, 0.25, rotations)
    assert not transport.commands


@pytest.mark.parametrize("sign,direction", [(1, "cw"), (-1, "ccw")])
def test_cleared_soft_limits_do_not_remove_hard_encoder_coordinate_range(
    stage_rig, sign, direction
):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].position = sign * POSITION_LIMIT
    with pytest.raises(ControllerError, match="position range"):
        controller.move("bottom", direction, 0.25, 0.1)
    with pytest.raises(ControllerError, match="position range"):
        controller.hold("bottom", direction, 0.25, "hard-limit-hold-01", clock())
    assert not transport.commands


@pytest.mark.parametrize(
    "position,direction,rotations,end",
    [
        (-1, "cw", 0.1, -0.9),
        (1, "ccw", 0.1, 0.9),
        (-1, "cw", 1, 0),
        (1, "ccw", 1, 0),
        (0, "cw", 0.5, 0.5),
        (0, "ccw", 0.25, -0.25),
    ],
)
def test_finite_motion_accepts_exact_limits_and_strictly_inward_recovery(
    stage_rig, position, direction, rotations, end
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position = position
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    controller.move("bottom", direction, 0.25, rotations)
    clock.advance(rotations / 0.25 + 1)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(end)
    assert axis(controller)["mode"] == "idle"


@pytest.mark.parametrize(
    "position,direction", [(0.5, "cw"), (0.6, "cw"), (-0.25, "ccw"), (-0.5, "ccw")]
)
def test_outward_hold_at_or_beyond_limit_is_rejected(stage_rig, position, direction):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position = position
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ControllerError, match="limit"):
        controller.hold("bottom", direction, 0.25, "bounded-hold-0001", clock())
    assert not transport.commands


@pytest.mark.parametrize("direction,limit", [("cw", 0.1), ("ccw", -0.1)])
def test_held_motion_stops_at_bound_and_starts_cooldown(stage_rig, direction, limit):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -0.1, 0.1
    state.lower_limit_set = state.upper_limit_set = True
    controller.hold("bottom", direction, 0.25, "bounded-hold-0001", clock())
    clock.advance(0.95)
    controller.poll()
    assert axis(controller)["position"] == limit
    assert axis(controller)["mode"] == "idle"
    assert axis(controller)["cooldown"] == 1
    with pytest.raises(ControllerError, match="ended"):
        controller.keep_hold_alive("bottom", "bounded-hold-0001")


def test_reset_does_not_bypass_existing_move_cooldown(stage_rig):
    controller, transport, clock = stage_rig
    controller.move("bottom", "cw", 0.25, 0.1)
    clock.advance(1)
    controller.poll()
    controller.reset_position("bottom")
    assert axis(controller)["cooldown"] == 1
    with pytest.raises(ControllerError, match="cooldown"):
        controller.move("bottom", "cw", 0.25, 0.1)


def test_travel_configuration_transactions_do_not_interleave_across_motors(stage_rig):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].position = 0.5
    transport.motors["top"].position = -0.5
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(controller.set_limit, motor) for motor in ("bottom", "top")]
        for future in futures:
            future.result()
    for i in (0, 2):
        assert transport.commands[i].startswith("motor ")
        assert transport.commands[i + 1] == "setlimit"
    assert axis(controller)["upper_limit_set"]
    assert axis(controller, "top")["lower_limit_set"]
