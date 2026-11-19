"""Automatic lower-home zeroing streams position and rebased bounds coherently."""

import re

import pytest
from test_stage_encoder_directional_limits import (
    V2_HEADERS,
    directional_frame,
    directional_reply,
    directional_rows,
)
from test_stage_encoder_directional_limits import legacy_coordinate_rig as legacy_coordinate_rig
from test_stage_encoder_limits import axis
from test_stage_encoder_switches import HEADERS, switch_reply
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame

from stage_controller.controller import MotorController
from stage_controller.protocol import ControllerError, LinkError

HOME_HEADER = b"homing=lower_zero_v1\n"


def homing_reply(*, position=0, **kwargs):
    rows = [
        re.sub(rb"pos=-?\d+(?:\.\d+)?", f"pos={position:.6f}".encode(), row)
        for row in directional_rows(**kwargs)
    ]
    return [HOME_HEADER, *V2_HEADERS, *rows]


def homing_frame(seq, motor="bottom", *, counts=0, **kwargs):
    return directional_frame(seq, motor, **kwargs).rstrip() + f" counts={counts}\n".encode()


@pytest.fixture
def homed(switch_serial):
    transport, port, clock = switch_serial(homing_reply(position=0.5, upper=-4096, bounds=2))
    transport.status()
    port.lines.extend(
        [
            *all_frames(counts=2048),
            homing_frame(4, counts=2048, upper=-4096, bounds=2),
            homing_frame(5, "top", counts=2048, upper=-4096, bounds=2),
            homing_frame(6, "insert", counts=2048, upper=-4096, bounds=2),
        ]
    )
    transport.poll_status()
    port.writes.clear()
    return transport, port, clock


@pytest.mark.parametrize(
    "headers,rows",
    [
        ([b"homing=lower_zero_v2\n", *V2_HEADERS], directional_rows()),
        ([HOME_HEADER, HOME_HEADER, *V2_HEADERS], directional_rows()),
        ([V2_HEADERS[0], HOME_HEADER, *V2_HEADERS[1:]], directional_rows()),
        ([HOME_HEADER, *HEADERS], switch_reply()[len(HEADERS) :]),
        ([HOME_HEADER, *V2_HEADERS[1:]], directional_rows()),
    ],
)
def test_homing_capability_must_be_first_unique_and_require_directional_switches(
    switch_serial, headers, rows
):
    transport, _, _ = switch_serial([*headers, *rows])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("new_firmware", [False, True])
def test_counts_field_must_match_homing_capability_even_before_initial_header(
    switch_serial, early, new_firmware
):
    make_reply = homing_reply if new_firmware else directional_reply
    wrong_frame = directional_frame(1) if new_firmware else homing_frame(1)
    if early:
        transport, _, _ = switch_serial([wrong_frame, *make_reply()])
        with pytest.raises(LinkError, match="homing capability"):
            transport.status()
    else:
        transport, port, _ = switch_serial(make_reply())
        transport.status()
        port.lines.append(wrong_frame)
        with pytest.raises(LinkError, match="homing capability"):
            transport.poll_status()


@pytest.mark.parametrize("counts", [-1_000_000_001, 1_000_000_001, "0.5", "nan"])
def test_invalid_counted_lim_is_rejected(homed, counts):
    transport, port, _ = homed
    port.lines.append(homing_frame(7, counts=counts))
    with pytest.raises(LinkError):
        transport.poll_status()


@pytest.mark.parametrize("counts", [-1_000_000_000, -2048, 0, 2048, 1_000_000_000])
def test_counted_lim_before_initial_header_parses_signed_positions(switch_serial, counts):
    early = homing_frame(1, counts=counts)
    transport, port, _ = switch_serial([early[:45], early[45:], *homing_reply()])
    state = transport.status()["bottom"]
    assert transport.lower_home_zero
    assert state.position == 0  # Newer coherent status row supersedes the early frame.
    port.lines.extend([*all_frames(2), homing_frame(5, counts=counts)])
    assert transport.poll_status()["bottom"].position == counts / 4096


def test_old_tel_then_rebased_lim_updates_position_and_upper_together_without_writes(homed):
    transport, port, _ = homed
    port.lines.append(frame(7, counts=2048, raw=2048))
    before = transport.poll_status()["bottom"]
    assert before.position == 0.5 and before.upper_limit == -1
    port.lines.append(homing_frame(8, counts=0, lower=0, upper=-6144, bounds=3, mask=1, raw=250))
    after = transport.poll_status()["bottom"]
    assert after.position == after.lower_limit == 0
    assert after.upper_limit == -1.5 and after.lower_limit_set and after.upper_limit_set
    assert after.switch_mask == 1
    assert port.writes == []


def test_new_tel_position_waits_for_matching_coordinate_snapshot(homed):
    transport, port, _ = homed
    port.lines.append(frame(7, counts=0, raw=2048, mode="idle", remaining=0, speed=0))
    state = transport.poll_status()["bottom"]
    # TEL still supplies raw/health/motion feedback, but cannot mix a new origin
    # with the old saved upper coordinate while its matching LIM is in flight.
    assert state.encoder_raw == 2048
    assert state.position == 0.5 and state.upper_limit == -1
    port.lines.append(homing_frame(8, counts=0, lower=0, upper=-6144, bounds=3, mask=1))
    state = transport.poll_status()["bottom"]
    assert state.position == state.lower_limit == 0 and state.upper_limit == -1.5


def test_later_tel_never_reverts_a_coherent_homed_position(homed):
    transport, port, _ = homed
    port.lines.extend(
        [
            homing_frame(7, counts=0, lower=0, upper=-6144, bounds=3, mask=1),
            frame(8, counts=2048, raw=2048, encoder="magnet", fault="encoder"),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.position == state.lower_limit == 0 and state.upper_limit == -1.5
    assert state.encoder == "magnet" and state.fault == "encoder"
    assert state.encoder_raw == 2048


def test_old_sequence_lim_cannot_restore_previous_coordinate_origin(homed):
    transport, port, _ = homed
    port.lines.extend(
        [
            homing_frame(8, counts=0, lower=0, upper=-6144, bounds=3, mask=1),
            homing_frame(7, counts=2048, upper=-4096, bounds=2),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.position == state.lower_limit == 0 and state.upper_limit == -1.5


def test_fragmented_rebase_is_not_exposed_until_complete(homed):
    transport, port, _ = homed
    message = homing_frame(7, counts=0, lower=0, upper=-6144, bounds=3, mask=1)
    port.lines.append(message[:-4])
    before = transport.poll_status()["bottom"]
    assert before.position == 0.5 and before.upper_limit == -1
    port.lines.append(message[-4:])
    after = transport.poll_status()["bottom"]
    assert after.position == 0 and after.upper_limit == -1.5


def test_fresh_status_wins_over_older_counted_lim_and_newer_tel_position(homed):
    transport, port, _ = homed
    rows = homing_reply(position=0, lower=0, upper=-6144, bounds=3, mask=1)
    port.lines.extend(
        [
            homing_frame(7, counts=2048, upper=-4096, bounds=2),
            *rows[:-2],
            frame(8, counts=2048),
            *rows[-2:],
        ]
    )
    state = transport.status()["bottom"]
    assert state.position == state.lower_limit == 0 and state.upper_limit == -1.5
    assert transport.poll_status()["bottom"].position == 0


def test_counted_lim_after_status_row_wins_atomically(switch_serial):
    rows = homing_reply(position=0.5, upper=-4096, bounds=2)
    transport, _, _ = switch_serial(
        [
            *rows[:-2],
            homing_frame(1, counts=0, lower=0, upper=-6144, bounds=3, mask=1),
            frame(2, counts=2048),
            *rows[-2:],
        ]
    )
    state = transport.status()["bottom"]
    assert state.position == state.lower_limit == 0 and state.upper_limit == -1.5


def test_stale_lim_cannot_be_kept_alive_by_fresh_tel_positions(homed):
    transport, port, clock = homed
    clock.advance(1.001)
    port.lines.extend(all_frames(7, counts=0))
    with pytest.raises(LinkError, match="limit/switch telemetry stopped"):
        transport.poll_status()


def test_counted_lim_does_not_substitute_for_missing_encoder_telemetry(homed):
    transport, port, clock = homed
    clock.advance(1.001)
    port.lines.extend([homing_frame(7), homing_frame(8, "top"), homing_frame(9, "insert")])
    with pytest.raises(LinkError, match="position telemetry stopped"):
        transport.poll_status()


def test_pre_home_firmware_keeps_tel_driven_position_and_no_auto_zero_claim(switch_serial):
    transport, port, _ = switch_serial(directional_reply())
    transport.status()
    port.lines.extend(all_frames(counts=2048))
    assert not transport.lower_home_zero
    assert transport.poll_status()["bottom"].position == 0.5


@pytest.mark.parametrize("first_new", [False, True])
def test_homing_protocol_cannot_change_on_an_established_connection(switch_serial, first_new):
    before, after = (
        (homing_reply, directional_reply) if first_new else (directional_reply, homing_reply)
    )
    transport, port, _ = switch_serial(before())
    transport.status()
    port.lines.extend(after())
    with pytest.raises(LinkError, match="lower-home capability changed"):
        transport.status()


@pytest.mark.parametrize("saved_upper", [False, True])
def test_controller_auto_zero_rebases_saved_upper_without_host_command_and_allows_inward(
    switch_serial, saved_upper
):
    initial_upper = -4096 if saved_upper else -1_000_000_000
    new_upper = -6144 if saved_upper else -1_000_000_000
    bounds = 3 if saved_upper else 1
    transport, port, clock = switch_serial(
        homing_reply(position=0.5, upper=initial_upper, bounds=2 if saved_upper else 0)
    )
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.writes.clear()
    port.lines.extend(
        [
            *all_frames(counts=2048),
            homing_frame(4, counts=0, lower=0, upper=new_upper, bounds=bounds, mask=1, raw=250),
            homing_frame(5, "top"),
            homing_frame(6, "insert"),
        ]
    )
    controller.poll()
    state = controller.snapshot()
    assert state["lower_home_zero"]
    assert axis(controller)["position"] == axis(controller)["lower_limit"] == 0
    assert axis(controller)["upper_limit"] == new_upper / 4096
    assert axis(controller)["upper_limit_set"] == saved_upper
    assert port.writes == []
    # A fresh read before motion confirms the rebased coordinate system. Moving
    # away from the lower contact is still CCW and decreases encoder position.
    port.lines.extend(
        [
            *homing_reply(lower=0, upper=new_upper, bounds=bounds, mask=1, raw=250),
            b"OK: bottom motor\n",
            b"OK: bottom dir\n",
            b"OK: bottom speed\n",
            b"OK: bottom rev\n",
        ]
    )
    controller.move("bottom", "ccw", 0.25, 0.1)
    assert port.writes == [
        b"status\n",
        b"motor bottom\n",
        b"dir ccw\n",
        b"speed 0.25\n",
        b"rev 0.1\n",
    ]
    assert not any(b"reset" in command for command in port.writes)
    port.lines.extend(
        [
            frame(7, counts=-410, direction="ccw", mode="idle"),
            homing_frame(8, counts=-410, lower=0, upper=new_upper, bounds=bounds),
        ]
    )
    controller.poll()
    assert axis(controller)["position"] == -410 / 4096


def test_outward_move_remains_blocked_at_the_homed_lower_contact(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit, state.position = 0, -1.5, 0
    state.lower_limit_set = state.upper_limit_set = True
    state.switch_mask = 1
    with pytest.raises(ControllerError, match="lower home switch is pressed"):
        controller.move("bottom", "cw", 0.25, 0.1)


def test_reaching_software_lower_bound_does_not_zero_position_or_rebase_upper(stage_rig):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = 0.1, -0.4
    state.lower_limit_set = state.upper_limit_set = True
    controller.hold("bottom", "cw", 0.25, "software-lower-01", clock())
    clock.advance(0.95)
    controller.poll()
    after = axis(controller)
    assert after["position"] == after["lower_limit"] == 0.1
    assert after["upper_limit"] == -0.4 and after["switch_mask"] == 0
    assert after["mode"] == "idle"
