"""Autonomous encoder telemetry shares one serial stream with commands/status."""

import pytest
from stage_helpers import Clock
from test_stage_protocol import CLOSED_STATUS, FakeSerial, closed_reply

from stage_controller import protocol
from stage_controller.controller import MotorController
from stage_controller.protocol import LinkError, SerialTransport

MOTORS = ("bottom", "top", "insert")
CHANNELS = {"bottom": 0, "top": 2, "insert": 1}
HEADERS = [b"telemetry=positions_v1\n", b"motion=closed_loop_v1\n", b"microstep=8\n"]
IDLE_STATUS = [
    line.replace(b"ccw moving", b"cw idle").replace(
        b"encoder=offline fault=encoder", b"encoder=ok fault=none"
    )
    for line in CLOSED_STATUS
]


def frame(
    seq,
    motor="bottom",
    *,
    uptime=100000,
    direction="cw",
    mode="idle",
    counts=0,
    remaining=0,
    speed=0.0,
    encoder="ok",
    fault="none",
    raw=0,
    channel=None,
    magnet=32,
    age=0,
):
    channel = CHANNELS[motor] if channel is None else channel
    return (
        f"TEL {seq} {uptime} {motor} {direction} {mode} counts={counts} "
        f"remaining={remaining} speed={speed:.6f} encoder={encoder} fault={fault} "
        f"raw={raw} channel={channel} magnet={magnet} age={age}\n"
    ).encode()


def all_frames(seq=1, **kwargs):
    return [frame(seq + i, motor, **kwargs) for i, motor in enumerate(MOTORS)]


@pytest.fixture
def streamed(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock)
    monkeypatch.setattr(protocol.time, "sleep", lambda _delay: None)
    port = FakeSerial([*HEADERS, *IDLE_STATUS])
    monkeypatch.setattr(protocol.serial, "Serial", lambda **_kwargs: port)
    transport = SerialTransport("COM7")
    transport.status()
    port.writes.clear()
    return transport, port, clock


def test_streaming_header_enables_passive_idle_position_updates(streamed):
    transport, port, _ = streamed
    assert transport.telemetry_streaming
    port.lines.extend(all_frames(counts=2048, raw=2048))
    states = transport.poll_status()
    assert port.writes == []
    assert all(state.position == 0.5 for state in states.values())
    assert all(state.mode == "idle" for state in states.values())
    assert all(state.encoder_raw == 2048 for state in states.values())
    assert {motor: state.encoder_channel for motor, state in states.items()} == CHANNELS
    assert all(state.magnet_status == 32 for state in states.values())
    assert all(state.telemetry_age_ms == 0 for state in states.values())


def test_signed_counts_remaining_and_measured_speed_keep_rotation_units(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    port.lines.append(
        frame(
            4,
            "top",
            direction="ccw",
            mode="moving",
            counts=-6144,
            remaining=-2048,
            speed=-0.125,
            raw=4095,
            age=12,
        )
    )
    state = transport.poll_status()["top"]
    assert state.position == -1.5
    assert state.remaining == -0.5
    assert state.measured_speed == -0.125
    assert state.direction == "ccw" and state.mode == "moving"
    assert state.encoder_raw == 4095
    assert state.encoder_age_ms == 12
    assert state.telemetry_age_ms == 0
    assert state.speed == 0.1  # Stream speed is measured, not the configured speed limit.


def test_partial_frame_survives_multiple_nonblocking_reads(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    message = frame(4, counts=4096, raw=10)
    port.lines.append(message[:40])
    assert transport.poll_status()["bottom"].position == 0
    port.lines.append(message[40:100])
    assert transport.poll_status()["bottom"].position == 0
    port.lines.append(message[100:])
    assert transport.poll_status()["bottom"].position == 1
    assert port.writes == []


def test_fragment_left_by_poll_is_completed_during_command_ack(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    message = frame(4, counts=2048)
    port.lines.append(message[:35])
    transport.poll_status()
    port.lines.extend([message[35:], b"OK: bottom speed\n"])
    transport.command("speed 0.125")
    state = transport.poll_status()["bottom"]
    assert state.position == 0.5
    assert state.speed == 0.125
    assert port.writes == [b"speed 0.125\n"]


def test_command_ack_interleaving_updates_feedback_and_acknowledged_settings(streamed):
    transport, port, _ = streamed
    port.lines.extend([*all_frames(counts=1024, speed=0.05), b"OK: bottom speed\n"])
    transport.command("speed 0.125")
    state = transport.poll_status()["bottom"]
    assert state.position == 0.25
    assert state.measured_speed == 0.05
    assert state.speed == 0.125
    assert port.writes == [b"speed 0.125\n"]


def test_fresh_status_and_telemetry_can_share_the_same_reply(streamed):
    transport, port, _ = streamed
    port.lines.extend(
        [
            *HEADERS,
            IDLE_STATUS[0],
            frame(1, counts=4096),
            IDLE_STATUS[1],
            frame(2, "top", counts=-2048),
            IDLE_STATUS[2],
            frame(3, "insert", counts=1024),
        ]
    )
    states = transport.status()
    assert states["bottom"].position == 1
    assert states["top"].position == -0.5
    states = transport.poll_status()
    assert states["insert"].position == 0.25
    assert port.writes == [b"status\n"]


def test_idle_frame_before_move_ack_cannot_complete_the_just_started_move(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    port.lines.extend([frame(4), b"OK: bottom rev\n"])
    transport.command("rev 0.5")
    state = transport.poll_status()["bottom"]
    assert state.mode == "moving" and state.remaining == 0.5
    port.lines.append(frame(5, counts=2048))
    assert transport.poll_status()["bottom"].mode == "idle"


def test_held_and_stop_acks_update_cached_modes_without_waiting_for_next_frame(streamed):
    transport, port, _ = streamed
    port.lines.extend([*all_frames(), b"OK: bottom hold\n"])
    transport.command("hold")
    assert transport.poll_status()["bottom"].mode == "continuous"
    port.lines.append(b"OK: bottom stop\n")
    transport.command("stop")
    assert transport.poll_status()["bottom"].mode == "idle"


def test_streamed_fault_is_visible_even_with_no_status_request(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    port.lines.append(frame(4, "top", encoder="untrusted", fault="encoder", magnet=0))
    state = transport.poll_status()["top"]
    assert state.encoder == "untrusted" and state.fault == "encoder"
    assert state.magnet_status == 0
    assert port.writes == []


def test_sequence_and_device_uptime_wrap_are_accepted(streamed):
    transport, port, _ = streamed
    port.lines.extend(
        [
            frame(4294967294, "bottom", uptime=4294967290),
            frame(4294967295, "top", uptime=4294967294),
            frame(0, "insert", uptime=2),
            frame(1, "bottom", uptime=3, counts=4096),
        ]
    )
    states = transport.poll_status()
    assert states["bottom"].position == 1
    assert all(state.telemetry_age_ms == 0 for state in states.values())


def test_duplicate_or_out_of_order_frames_cannot_rewind_position(streamed):
    transport, port, _ = streamed
    port.lines.extend(
        [*all_frames(), frame(4, counts=4096), frame(4, counts=0), frame(3, counts=-4096)]
    )
    assert transport.poll_status()["bottom"].position == 1


def test_duplicate_frames_do_not_renew_stream_freshness(streamed):
    transport, port, clock = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    clock.advance(1.01)
    port.lines.extend([frame(1), frame(2, "top"), frame(3, "insert")])
    with pytest.raises(LinkError):
        transport.poll_status()


def test_unchanged_position_heartbeats_keep_every_axis_fresh(streamed):
    transport, port, clock = streamed
    for seq in range(1, 31, 3):
        clock.advance(0.25)
        port.lines.extend(all_frames(seq, counts=2048))
        states = transport.poll_status()
        assert all(
            state.position == 0.5 and state.telemetry_age_ms == 0 for state in states.values()
        )
    assert port.writes == []


def test_missing_axis_expires_even_while_other_axes_stream(streamed):
    transport, port, clock = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    clock.advance(0.6)
    port.lines.extend([frame(4, "bottom"), frame(5, "top")])
    transport.poll_status()
    clock.advance(0.5)
    port.lines.extend([frame(6, "bottom"), frame(7, "top")])
    with pytest.raises(LinkError):
        transport.poll_status()


def test_partial_frame_never_renews_stale_telemetry(streamed):
    transport, port, clock = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    port.lines.append(frame(4)[:-1])
    clock.advance(1.01)
    with pytest.raises(LinkError):
        transport.poll_status()


def test_first_frame_has_one_second_grace_but_no_fabricated_age(streamed):
    transport, _, clock = streamed
    clock.advance(0.5)
    states = transport.poll_status()
    assert all(state.telemetry_age_ms is None for state in states.values())
    clock.advance(0.51)
    with pytest.raises(LinkError):
        transport.poll_status()


def test_encoder_sample_age_does_not_mistake_a_live_fault_for_serial_loss(streamed):
    transport, port, clock = streamed
    port.lines.extend(all_frames(encoder="offline", fault="encoder", age=4294967295))
    states = transport.poll_status()
    assert all(state.encoder_age_ms == 4294967295 for state in states.values())
    clock.advance(0.5)
    port.lines.extend(all_frames(4, encoder="offline", fault="encoder", age=4294967295))
    states = transport.poll_status()
    assert all(state.telemetry_age_ms == 0 for state in states.values())
    assert port.writes == []


@pytest.mark.parametrize(
    "old,new",
    [
        (b"TEL 1 ", b"TEL -1 "),
        (b"TEL 1 ", b"TEL 4294967296 "),
        (b" 100000 ", b" 4294967296 "),
        (b"counts=0", b"counts=1000000001"),
        (b"counts=0", b"counts=-1000000001"),
        (b"remaining=0", b"remaining=2000000001"),
        (b"speed=0.000000", b"speed=nan"),
        (b"speed=0.000000", b"speed=inf"),
        (b"raw=0", b"raw=4096"),
        (b"channel=0", b"channel=8"),
        (b"magnet=32", b"magnet=256"),
        (b"age=0", b"age=4294967296"),
        (b"encoder=ok", b"encoder=bad"),
        (b"fault=none", b"fault=bad"),
        (b"bottom cw idle", b"bottom invalid idle"),
        (b"bottom cw idle", b"unknown cw idle"),
    ],
)
def test_malformed_or_out_of_range_frame_is_a_link_error(streamed, old, new):
    transport, port, _ = streamed
    port.lines.append(frame(1).replace(old, new))
    with pytest.raises(LinkError):
        transport.poll_status()


def test_legacy_firmware_polling_still_uses_status_requests(streamed):
    transport, port, clock = streamed
    port.lines.extend(closed_reply())
    transport.status()
    assert not transport.telemetry_streaming
    port.writes.clear()
    assert len(transport.poll_status()) == 3
    assert port.writes == []
    clock.advance(0.201)
    port.lines.extend(closed_reply())
    assert len(transport.poll_status()) == 3
    assert port.writes == [b"status\n"]


@pytest.mark.parametrize(
    "header", [b"telemetry=positions_v2\n", b"telemetry=bad\n", b"telemetry=positions_v1 extra\n"]
)
def test_unknown_telemetry_capability_is_rejected(streamed, header):
    transport, port, _ = streamed
    port.lines.extend([header, *HEADERS[1:], *IDLE_STATUS])
    with pytest.raises(LinkError):
        transport.status()


def test_duplicate_telemetry_capability_is_rejected(streamed):
    transport, port, _ = streamed
    port.lines.extend([HEADERS[0], *HEADERS, *IDLE_STATUS])
    with pytest.raises(LinkError):
        transport.status()


def test_oversized_unterminated_frame_is_rejected(streamed):
    transport, port, _ = streamed
    port.lines.append(b"TEL " + b"9" * 4096)
    with pytest.raises(LinkError):
        transport.poll_status()


def test_board_restart_banner_invalidates_cached_position_and_motion(streamed):
    transport, port, _ = streamed
    port.lines.extend(all_frames())
    transport.poll_status()
    port.lines.append(b"Stage closed-loop firmware ready. Serial: 115200 baud, Newline.\n")
    with pytest.raises(LinkError):
        transport.poll_status()


def test_controller_poll_consumes_stream_cache_instead_of_requesting_status(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    cached = transport.status()
    cached["top"].position = 0.5
    calls = []

    def poll_status():
        calls.append("poll")
        return cached

    def no_status():
        pytest.fail("Periodic telemetry polling must not request a full status reply")

    monkeypatch.setattr(transport, "poll_status", poll_status)
    monkeypatch.setattr(transport, "status", no_status)
    controller.poll()
    assert calls == ["poll"]
    assert controller.snapshot()["motors"][1]["position"] == 0.5
    monkeypatch.undo()  # Fixture cleanup intentionally obtains fresh status after Stop.


def test_stream_loss_disconnects_and_attempts_stop(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig

    def expired():
        raise LinkError("Top telemetry expired")

    monkeypatch.setattr(transport, "poll_status", expired)
    controller.poll()
    assert not controller.snapshot()["connected"]
    assert transport.commands == ["stop all"]
    assert transport.closed


def test_connected_stream_does_not_unlock_motion_before_first_axis_frame(streamed):
    transport, port, clock = streamed
    port.lines.extend([*HEADERS, *IDLE_STATUS])
    controller = MotorController(demo=True, transport_factory=lambda _port: transport, clock=clock)
    state = controller.connect("DEMO")
    assert all(not motor["available"] for motor in state["motors"])
    port.lines.extend(all_frames())
    controller.poll()
    assert all(motor["available"] for motor in controller.snapshot()["motors"])
