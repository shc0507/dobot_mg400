"""Ordinary home contacts interlock motion; only calibration learns a range."""

import pytest
from test_stage_encoder_calibration import calibration_frame, calibration_reply
from test_stage_encoder_coordinates import ccw_frame, ccw_reply
from test_stage_encoder_limits import axis
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame

from stage_controller.controller import MotorController
from stage_controller.protocol import POSITION_LIMIT, ControllerError, LinkError

OLD_HEADER = b"homing=lower_zero_v1\n"
NEW_HEADER = b"homing=calibration_only_v1\n"


def passive_reply(*args, **kwargs):
    return [
        NEW_HEADER if line == OLD_HEADER else line for line in calibration_reply(*args, **kwargs)
    ]


@pytest.mark.parametrize("legacy", [False, True])
def test_both_homing_modes_keep_counted_position_authority_and_calibration(switch_serial, legacy):
    reply = calibration_reply if legacy else passive_reply
    transport, port, clock = switch_serial(reply(position=0.25))
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    state = controller.connect("DEMO")
    assert state["lower_home_zero"] == legacy
    assert state["calibration_only_homing"] == (not legacy)
    assert state["auto_calibration"]
    port.lines.extend(
        [
            *all_frames(counts=4096),
            calibration_frame(4, counts=2048),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
        ]
    )
    controller.poll()
    assert axis(controller)["position"] == 0.5  # Counted LIM, not TEL's older origin.


def test_new_homing_mode_does_not_require_auto_calibration_capability(switch_serial):
    reply = [NEW_HEADER if line == OLD_HEADER else line for line in ccw_reply()]
    transport, port, _ = switch_serial(reply)
    transport.status()
    assert transport.calibration_only_homing and not transport.lower_home_zero
    assert not transport.auto_calibration
    port.lines.extend([*all_frames(counts=0), ccw_frame(4, counts=2048)])
    assert transport.poll_status()["bottom"].position == 0.5


@pytest.mark.parametrize("new_first", [False, True])
def test_changing_homing_policy_on_connected_board_requires_reconnect(switch_serial, new_first):
    before, after = (
        (passive_reply, calibration_reply) if new_first else (calibration_reply, passive_reply)
    )
    transport, port, _ = switch_serial(before())
    transport.status()
    port.lines.extend(after())
    with pytest.raises(LinkError, match="capability changed"):
        transport.status()


@pytest.mark.parametrize("header", [b"homing=calibration_only_v2\n", b"homing=calibration_only\n"])
def test_unknown_passive_homing_version_is_rejected(switch_serial, header):
    reply = [header if line == NEW_HEADER else line for line in passive_reply()]
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


def test_duplicate_mixed_homing_headers_are_rejected(switch_serial):
    reply = []
    for line in passive_reply():
        reply.append(line)
        if line == NEW_HEADER:
            reply.append(OLD_HEADER)
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


def test_early_counted_lim_works_with_new_homing_policy(switch_serial):
    early = calibration_frame(1, counts=-500, mask=1, raw=250)
    transport, port, _ = switch_serial([early[:45], early[45:], *passive_reply()])
    state = transport.status()["bottom"]
    assert state.position == 0  # Fresh status wins over an earlier packet.
    port.lines.extend([*all_frames(2), calibration_frame(5, counts=-500, mask=1, raw=250)])
    state = transport.poll_status()["bottom"]
    assert state.position == -500 / 4096
    assert state.lower_limit == -POSITION_LIMIT and state.upper_limit == POSITION_LIMIT
    assert not state.lower_limit_set and not state.upper_limit_set


@pytest.mark.parametrize("early", [False, True])
def test_new_homing_policy_still_requires_counts_in_lim(switch_serial, early):
    wrong = calibration_frame(1).replace(b" counts=0", b"")
    transport, port, _ = switch_serial([wrong, *passive_reply()] if early else passive_reply())
    if early:
        with pytest.raises(LinkError, match="homing capability"):
            transport.status()
    else:
        transport.status()
        port.lines.append(wrong)
        with pytest.raises(LinkError, match="homing capability"):
            transport.poll_status()


@pytest.mark.parametrize("saved", [False, True])
def test_idle_hand_motion_and_switch_edges_keep_reported_coordinate_and_saved_range(
    switch_serial, saved
):
    limits = {"lower": -1024, "upper": 4096, "bounds": 3} if saved else {}
    transport, port, clock = switch_serial(passive_reply(**limits))
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.writes.clear()
    for offset, counts, mask, raw in [(0, -500, 1, 250), (6, 5000, 2, 0), (12, 100, 0, 1023)]:
        port.lines.extend(
            [
                *all_frames(1 + offset, counts=0),
                calibration_frame(4 + offset, counts=counts, mask=mask, raw=raw, **limits),
                calibration_frame(5 + offset, "top"),
                calibration_frame(6 + offset, "insert"),
            ]
        )
        controller.poll()
        after = axis(controller)
        assert after["position"] == counts / 4096
        assert after["lower_limit"] == (-0.25 if saved else -POSITION_LIMIT)
        assert after["upper_limit"] == (1 if saved else POSITION_LIMIT)
        assert after["lower_limit_set"] == after["upper_limit_set"] == saved
        assert after["calibration"] == "none"
    assert port.writes == []
    assert not any(
        "travel limit updated" in event["message"] for event in controller.snapshot()["events"]
    )


def test_status_and_newer_lim_remain_coherent_under_new_policy(switch_serial):
    reply = passive_reply(position=0.25, lower=-1024, upper=4096, bounds=3)
    transport, _, _ = switch_serial(
        [
            *reply[:-2],
            calibration_frame(1, counts=-500, lower=-1024, upper=4096, bounds=3, mask=1, raw=250),
            frame(2, counts=0),
            *reply[-2:],
        ]
    )
    state = transport.status()["bottom"]
    assert state.position == -500 / 4096 and state.lower_limit == -0.25 and state.upper_limit == 1


@pytest.mark.parametrize("mask,direction,side", [(1, "cw", "lower"), (2, "ccw", "upper")])
def test_normal_idle_contact_keeps_manual_bounds_and_only_blocks_its_direction(
    stage_rig, mask, direction, side
):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position, state.lower_limit, state.upper_limit = 0.25, -0.25, 1
    state.lower_limit_set = state.upper_limit_set = True
    state.switch_mask = mask
    controller.poll()
    assert state.position == 0.25 and state.lower_limit == -0.25 and state.upper_limit == 1
    with pytest.raises(ControllerError, match=f"{side} home switch is pressed"):
        controller.move("bottom", direction, 0.25, 0.1)
    opposite = "ccw" if direction == "cw" else "cw"
    controller.move("bottom", opposite, 0.25, 0.1)
    assert transport.commands[-3:] == [f"dir {opposite}", "speed 0.25", "rev 0.1"]


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("mask,direction", [(1, "cw"), (2, "ccw")])
def test_contact_stops_normal_motion_without_zeroing_or_learning_or_restarting(
    stage_rig, held, mask, direction
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -1, 1
    state.lower_limit_set = state.upper_limit_set = True
    if held:
        controller.hold("bottom", direction, 0.25, "passive-switch-hold", clock())
    else:
        controller.move("bottom", direction, 0.25, 0.5)
    clock.advance(0.25)
    controller.poll()
    position = state.position
    assert position != 0
    state.switch_mask = mask
    clock.advance(0.1)
    controller.poll()
    assert state.position == position and state.mode == "idle"
    assert state.lower_limit == -1 and state.upper_limit == 1
    assert state.lower_limit_set and state.upper_limit_set and state.calibration == "none"
    assert axis(controller)["cooldown"] == 1
    state.switch_mask = 0
    clock.advance(10)
    controller.poll()
    assert state.position == position and state.mode == "idle"


@pytest.mark.parametrize("saved", [False, True])
def test_demo_hand_motion_at_idle_does_not_zero_or_capture_any_bounds(stage_rig, saved):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    if saved:
        state.lower_limit, state.upper_limit = -0.25, 1
        state.lower_limit_set = state.upper_limit_set = True
    before = (state.lower_limit, state.upper_limit, state.lower_limit_set, state.upper_limit_set)
    for position, mask in [(-0.4, 1), (1.4, 2), (0.5, 0)]:
        state.position, state.switch_mask = position, mask
        controller.poll()
        assert state.position == position
        assert (
            state.lower_limit,
            state.upper_limit,
            state.lower_limit_set,
            state.upper_limit_set,
        ) == before


def test_explicit_calibration_retains_lower_zero_upper_capture_and_midpoint(stage_rig):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.lower_limit, state.upper_limit = 0.5, -0.25, 2
    state.lower_limit_set = state.upper_limit_set = True
    response = controller.calibrate("bottom", 0.5)
    assert response["calibration_only_homing"] and not response["lower_home_zero"]
    assert response["auto_calibration"]
    clock.advance(2.5)
    controller.poll()
    assert state.calibration == "upper" and state.position == state.lower_limit == 0
    assert state.lower_limit_set and not state.upper_limit_set
    clock.advance(6.5)
    controller.poll()
    assert state.calibration == "done" and state.position == 0.5
    assert state.lower_limit == 0 and state.upper_limit == 1
    assert state.lower_limit_set and state.upper_limit_set


def test_new_serial_capability_accepts_calibration_and_keeps_phase_atomic(switch_serial):
    transport, port, clock = switch_serial(passive_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [
            *all_frames(),
            calibration_frame(4),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
        ]
    )
    controller.poll()
    port.writes.clear()
    port.lines.extend(
        [*passive_reply(), b"OK: bottom motor\n", b"OK: bottom speed\n", b"OK: bottom calibrate\n"]
    )
    controller.calibrate("bottom", 0.25)
    assert port.writes == [b"status\n", b"motor bottom\n", b"speed 0.25\n", b"calibrate\n"]
    port.writes.clear()
    port.lines.extend(
        [
            calibration_frame(7, phase="done", counts=2048, lower=0, upper=4096, bounds=3),
            frame(8, counts=-500),
        ]
    )
    controller.poll()
    assert axis(controller)["calibration"] == "done" and axis(controller)["position"] == 0.5
    assert axis(controller)["lower_limit"] == 0 and axis(controller)["upper_limit"] == 1
    assert port.writes == []
