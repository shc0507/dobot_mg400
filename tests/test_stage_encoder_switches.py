"""Home contacts and learned bounds share the autonomous UART stream."""

import pytest
from stage_helpers import Clock
from test_stage_encoder_limits import axis, limits_reply
from test_stage_encoder_limits import legacy_limits_rig as legacy_limits_rig
from test_stage_encoder_telemetry import IDLE_STATUS, all_frames, frame
from test_stage_protocol import FakeSerial

from stage_controller import protocol
from stage_controller.controller import MotorController
from stage_controller.protocol import (
    POSITION_LIMIT,
    CommandRejected,
    ControllerError,
    LinkError,
    MotorStatus,
    SerialTransport,
    motion_limit_error,
    parse_limit_telemetry,
)

MOTORS = ("bottom", "top", "insert")
HEADERS = [
    b"switches=analog_pair_v1\n",
    b"limits=soft_v1\n",
    b"telemetry=positions_v1\n",
    b"motion=closed_loop_v1\n",
    b"microstep=8\n",
]


def switch_rows(*, lower=-1_000_000_000, upper=1_000_000_000, bounds=0, mask=0, raw=1023):
    suffix = (
        f" lower={lower} upper={upper} bounds={bounds} switch={mask} switch_raw={raw}\n"
    ).encode()
    return [line.rstrip() + suffix for line in IDLE_STATUS]


def switch_reply(**kwargs):
    return [*HEADERS, *switch_rows(**kwargs)]


def limit_frame(
    seq,
    motor="bottom",
    *,
    uptime=100000,
    lower=-1_000_000_000,
    upper=1_000_000_000,
    bounds=0,
    mask=0,
    raw=1023,
):
    return (
        f"LIM {seq} {uptime} {motor} lower={lower} upper={upper} "
        f"bounds={bounds} switch={mask} raw={raw}\n"
    ).encode()


def limit_frames(seq=4, **kwargs):
    return [limit_frame(seq + i, motor, **kwargs) for i, motor in enumerate(MOTORS)]


@pytest.fixture
def switch_serial(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock)
    monkeypatch.setattr(protocol.time, "sleep", lambda _: None)

    def create(lines=None):
        port = FakeSerial(switch_reply() if lines is None else lines)
        monkeypatch.setattr(protocol.serial, "Serial", lambda **_kwargs: port)
        return SerialTransport("COM7"), port, clock

    return create


@pytest.fixture
def switching(switch_serial):
    transport, port, clock = switch_serial()
    transport.status()
    port.lines.extend([*all_frames(), *limit_frames()])
    transport.poll_status()
    port.writes.clear()
    return transport, port, clock


@pytest.mark.parametrize(
    "lower,upper,bounds",
    [
        (-1_000_000_000, -2048, 2),
        (1024, 1_000_000_000, 1),
        (0, 0, 3),
        (-1_000_000_000, 0, 2),
        (0, 1_000_000_000, 1),
    ],
)
def test_physical_switch_side_is_independent_of_coordinate_sign(
    switch_serial, lower, upper, bounds
):
    transport, port, _ = switch_serial(switch_reply(lower=lower, upper=upper, bounds=bounds))
    state = transport.status()["bottom"]
    assert transport.home_switches and transport.travel_limits and transport.telemetry_streaming
    assert state.lower_limit == lower / 4096
    assert state.upper_limit == upper / 4096
    port.lines.extend([*all_frames(), *limit_frames(lower=lower, upper=upper, bounds=bounds)])
    state = transport.poll_status()["bottom"]
    assert state.lower_limit_set == bool(bounds & 1)
    assert state.upper_limit_set == bool(bounds & 2)
    assert state.switch_mask == 0 and state.switch_raw == 1023
    assert state.limit_telemetry_age_ms == 0


@pytest.mark.parametrize(
    "change",
    [
        {"seq": 4294967296},
        {"uptime": 4294967296},
        {"lower": -1_000_000_001},
        {"upper": 1_000_000_001},
        {"lower": 1, "upper": 0, "bounds": 3},
        {"bounds": 4},
        {"lower": 0, "bounds": 0},
        {"upper": 0, "bounds": 0},
        {"mask": 4},
        {"raw": 1024},
        {"mask": -1},
        {"raw": -1},
    ],
)
def test_limit_frame_rejects_out_of_range_fields(change):
    values = {"seq": 1, **change}
    with pytest.raises(LinkError):
        parse_limit_telemetry(limit_frame(**values).decode().strip())


@pytest.mark.parametrize(
    "line",
    [
        "LIM nonsense",
        "LIM 1 0 bottom lower=0 upper=1 bounds=3 switch=0",
        "LIM 1 0 other lower=0 upper=1 bounds=3 switch=0 raw=1",
        "LIM 1 0 bottom lower=0.5 upper=1 bounds=3 switch=0 raw=1",
        "LIM 1 0 bottom lower=0 upper=1 bounds=3 switch=0 raw=1 extra=0",
    ],
)
def test_malformed_limit_frame_is_not_silently_ignored(switching, line):
    transport, port, _ = switching
    port.lines.append((line + "\n").encode())
    with pytest.raises(LinkError):
        transport.poll_status()


@pytest.mark.parametrize(
    "headers",
    [
        HEADERS[1:],
        [HEADERS[0], *HEADERS],
        [b"switches=analog_pair_v2\n", *HEADERS[1:]],
        [b"switches analog_pair_v1\n", *HEADERS[1:]],
        [HEADERS[1], HEADERS[0], *HEADERS[2:]],
        [HEADERS[0], *HEADERS[2:]],
        [*HEADERS[:2], *HEADERS[3:]],
        [*HEADERS[:3], HEADERS[4]],
        HEADERS[:4],
    ],
)
def test_switch_capability_dependencies_and_header_order_are_strict(switch_serial, headers):
    transport, _, _ = switch_serial([*headers, *switch_rows()])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "suffix",
    [
        b"",
        b" switch=4 switch_raw=0",
        b" switch=0 switch_raw=1024",
        b" switch=-1 switch_raw=0",
        b" switch=0",
        b" switch=0 switch_raw=nan",
    ],
)
def test_switch_status_fields_are_required_and_validated(switch_serial, suffix):
    rows = [row.split(b" switch=")[0] + suffix + b"\n" for row in switch_rows()]
    transport, _, _ = switch_serial([*HEADERS, *rows])
    with pytest.raises(LinkError):
        transport.status()


def test_legacy_limit_firmware_remains_supported_without_switch_stream(switch_serial):
    transport, port, _ = switch_serial()
    transport.status()
    port.lines.extend(limits_reply(streaming=True))
    states = transport.status()
    assert not transport.home_switches
    assert transport.travel_limits
    assert all(
        state.switch_mask is None and state.limit_telemetry_age_ms is None
        for state in states.values()
    )
    port.lines.extend(all_frames())
    transport.poll_status()


def test_idle_limit_changes_arrive_without_writes_and_preserve_position(switching):
    transport, port, _ = switching
    port.lines.extend(
        [frame(7, counts=-2048), limit_frame(8, upper=-2048, bounds=2, mask=2, raw=510)]
    )
    state = transport.poll_status()["bottom"]
    assert state.position == -0.5
    assert state.upper_limit == -0.5 and state.upper_limit_set
    assert state.switch_mask == 2 and state.switch_raw == 510
    assert port.writes == []


def test_limit_frame_fragment_survives_poll_then_command_ack(switching):
    transport, port, _ = switching
    message = limit_frame(7, lower=1024, bounds=1, mask=1, raw=10)
    port.lines.append(message[:42])
    assert transport.poll_status()["bottom"].lower_limit_set is False
    port.lines.extend([message[42:70], message[70:], b"OK: bottom speed\n"])
    transport.command("speed 0.125")
    state = transport.poll_status()["bottom"]
    assert state.lower_limit == 0.25 and state.switch_mask == 1
    assert state.speed == 0.125
    assert port.writes == [b"speed 0.125\n"]


def test_tel_and_lim_share_one_sequence_and_duplicates_do_not_refresh(switching):
    transport, port, clock = switching
    clock.advance(0.5)
    port.lines.extend([frame(8, counts=4096), limit_frame(7, mask=2), limit_frame(8, mask=2)])
    state = transport.poll_status()["bottom"]
    assert state.position == 1 and state.switch_mask == 0
    assert state.limit_telemetry_age_ms == 500
    port.lines.extend([limit_frame(9, mask=2), frame(8, counts=8192)])
    state = transport.poll_status()["bottom"]
    assert state.position == 1 and state.switch_mask == 2
    assert state.limit_telemetry_age_ms == 0


def test_shared_sequence_wraps_between_both_frame_types(switch_serial):
    transport, port, _ = switch_serial()
    transport.status()
    port.lines.extend(
        [
            frame(0xFFFFFFFD),
            limit_frame(0xFFFFFFFE, mask=1),
            frame(0xFFFFFFFF, "top"),
            limit_frame(0, "top", mask=2),
            frame(1, "insert"),
            limit_frame(2, "insert", mask=3),
        ]
    )
    states = transport.poll_status()
    assert [states[motor].switch_mask for motor in MOTORS] == [1, 2, 3]
    assert all(
        state.telemetry_age_ms == state.limit_telemetry_age_ms == 0 for state in states.values()
    )


def test_status_rows_win_over_older_limit_frames_without_later_reversion(switching):
    transport, port, _ = switching
    port.lines.extend(
        [limit_frame(7, upper=4096, bounds=2, mask=2), *switch_reply(mask=2, raw=510)]
    )
    state = transport.status()["bottom"]
    assert state.upper_limit_set is False and state.switch_mask == 2
    port.lines.extend(all_frames(8, counts=0))
    state = transport.poll_status()["bottom"]
    assert state.upper_limit == POSITION_LIMIT and state.upper_limit_set is False
    assert state.switch_mask == 2 and state.switch_raw == 510


def test_limit_frame_after_its_status_row_wins(switching):
    transport, port, _ = switching
    rows = switch_rows()
    port.lines.extend(
        [*HEADERS, rows[0], limit_frame(7, upper=0, bounds=2, mask=2, raw=510), *rows[1:]]
    )
    state = transport.status()["bottom"]
    assert state.upper_limit == 0 and state.upper_limit_set
    assert state.switch_mask == 2 and state.switch_raw == 510


def test_pre_command_limit_and_idle_frames_cannot_finish_a_new_move(switching):
    transport, port, _ = switching
    port.lines.extend([frame(7), limit_frame(8), b"OK: bottom rev\n"])
    transport.command("rev 0.1")
    port.lines.append(limit_frame(9))
    state = transport.poll_status()["bottom"]
    assert state.mode == "moving" and state.remaining == 0.1


def test_missing_first_limit_frame_gates_motion_then_disconnects(switch_serial):
    transport, port, clock = switch_serial()
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(all_frames())
    controller.poll()
    assert controller.snapshot()["home_switches"]
    assert not axis(controller)["available"]
    port.lines.extend(switch_reply())
    with pytest.raises(ControllerError, match="limit/switch telemetry"):
        controller.move("bottom", "cw", 0.25, 0.1)
    clock.advance(1.001)
    port.lines.extend(all_frames(4))
    controller.poll()
    assert not controller.snapshot()["connected"]
    assert "limit/switch telemetry stopped" in controller.snapshot()["error"]
    assert b"stop all\n" in port.writes


def test_a_missing_axis_limit_stream_expires_despite_fresh_position(switching):
    transport, port, clock = switching
    clock.advance(1.001)
    port.lines.extend([*all_frames(7), limit_frame(10), limit_frame(11, "top")])
    with pytest.raises(LinkError, match="Insert limit/switch telemetry stopped"):
        transport.poll_status()


def test_limit_heartbeat_does_not_renew_position_stream(switching):
    transport, port, clock = switching
    clock.advance(1.001)
    port.lines.extend(limit_frames(7))
    with pytest.raises(LinkError, match="position telemetry stopped"):
        transport.poll_status()


def test_unchanged_limit_heartbeats_renew_age(switching):
    transport, port, clock = switching
    clock.advance(0.75)
    port.lines.extend([*all_frames(7), *limit_frames(10)])
    assert transport.poll_status()["bottom"].limit_telemetry_age_ms == 0
    clock.advance(0.75)
    port.lines.extend([*all_frames(13), *limit_frames(16)])
    assert transport.poll_status()["bottom"].limit_telemetry_age_ms == 0


@pytest.mark.parametrize("mask,direction", [(1, "ccw"), (2, "cw"), (3, "cw"), (3, "ccw")])
@pytest.mark.parametrize("held", [False, True])
def test_switch_blocks_moves_with_unset_limits_at_any_coordinate(stage_rig, mask, direction, held):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.switch_mask = -0.5, mask
    with pytest.raises(ControllerError, match="home switch is pressed"):
        if held:
            controller.hold("bottom", direction, 0.25, "pressed_switch_01", clock())
        else:
            controller.move("bottom", direction, 0.25, 0.1)
    assert transport.commands == []


@pytest.mark.parametrize("mask,direction", [(1, "cw"), (2, "ccw")])
def test_move_away_from_contact_is_allowed(stage_rig, mask, direction):
    controller, transport, _ = stage_rig
    transport.motors["bottom"].switch_mask = mask
    controller.move("bottom", direction, 0.25, 0.1)
    assert transport.commands[-1] == "rev 0.1"


@pytest.mark.parametrize("action", ["reset_position", "clear_upper_limit"])
def test_reset_or_clear_does_not_release_contact_or_allow_outward_motion(stage_rig, action):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position = state.upper_limit = 0.5
    state.upper_limit_set = True
    state.switch_mask, state.switch_raw = 2, 510
    getattr(controller, action)("bottom")
    assert not axis(controller)["upper_limit_set"]
    assert axis(controller)["switch_mask"] == 2
    before = len(transport.commands)
    with pytest.raises(ControllerError, match="home switch is pressed"):
        controller.move("bottom", "cw", 0.25, 0.1)
    assert len(transport.commands) == before


def test_contact_guard_precedes_missing_soft_limit_metadata():
    state = MotorStatus("bottom", switch_mask=2)
    assert "upper home switch" in motion_limit_error(state, "cw", 0.1)


@pytest.mark.parametrize("command", ["rev 0.1", "hold"])
def test_demo_wire_commands_cannot_bypass_contact_guard(stage_rig, command):
    _, transport, _ = stage_rig
    transport.motors["bottom"].switch_mask = 2
    with pytest.raises(CommandRejected, match="home switch is pressed"):
        transport.command(command)


def test_switch_events_record_edges_and_changed_bounds_without_frame_noise(stage_rig):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.switch_mask, state.switch_raw = 2, 510
    state.upper_limit, state.upper_limit_set = 0, True
    controller.poll()
    messages = [event["message"] for event in controller.snapshot()["events"]]
    assert sum("upper home switch triggered" in text for text in messages) == 1
    assert sum("upper travel limit updated to 0 rotations" in text for text in messages) == 1
    first = controller.snapshot()["events"]
    for value in (511, 508, 512):
        state.switch_raw = value
        controller.poll()
    assert controller.snapshot()["events"] == first
    state.switch_mask, state.switch_raw = 0, 1023
    controller.poll()
    messages = [event["message"] for event in controller.snapshot()["events"]]
    assert sum("upper home switch released" in text for text in messages) == 1
