"""Held-direction pause/retry telemetry is advisory, not a replacement fault."""

import pytest
from test_stage_encoder_absolute_position import absolute_reply
from test_stage_encoder_calibration import calibration_frame
from test_stage_encoder_coordinates import ccw_frame, ccw_reply
from test_stage_encoder_limits import axis
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame
from test_stage_hold import FIRST, start

from stage_controller.controller import MotorController
from stage_controller.protocol import DemoTransport, LinkError

HEADER = b"warnings=held_direction_v1\n"


def warning_reply(*args, warning="none", count=0, **kwargs):
    return [
        HEADER,
        *[
            line.rstrip() + f" warning={warning} warnings={count}\n".encode()
            if b"pos=" in line
            else line
            for line in absolute_reply(*args, **kwargs)
        ],
    ]


def warning_frame(seq, motor="bottom", *, warning="none", count=0, **kwargs):
    return calibration_frame(seq, motor, **kwargs).rstrip() + (
        f" warning={warning} warnings={count}\n".encode()
    )


@pytest.fixture
def warned_stream(switch_serial):
    transport, port, clock = switch_serial(warning_reply())
    transport.status()
    port.lines.extend(
        [*all_frames(), warning_frame(4), warning_frame(5, "top"), warning_frame(6, "insert")]
    )
    transport.poll_status()
    port.writes.clear()
    return transport, port, clock


@pytest.mark.parametrize(
    "warning,count,recommended",
    [
        ("none", 0, False),
        ("direction_pause", 1, False),
        ("none", 5, False),
        ("direction_pause", 6, True),
        ("none", 65535, True),
    ],
)
def test_warning_status_and_lim_keep_fields_and_derive_six_incident_advisory(
    switch_serial, warning, count, recommended
):
    transport, port, _ = switch_serial(warning_reply(warning=warning, count=count))
    state = transport.status()["bottom"]
    assert transport.hold_direction_warnings
    assert state.warning == warning and state.direction_warning_count == count
    assert state.recalibration_recommended == recommended
    port.lines.extend(
        [
            *all_frames(),
            warning_frame(
                4, warning=warning, count=count, counts=2048, lower=0, upper=4096, bounds=3
            ),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.position == 0.5 and state.lower_limit == 0 and state.upper_limit == 1
    assert state.warning == warning and state.direction_warning_count == count


@pytest.mark.parametrize(
    "bad", [b"warnings=held_direction_v2\n", b"warnings held_direction_v1\n", b"warnings=invalid\n"]
)
def test_unknown_warning_capability_is_rejected(switch_serial, bad):
    transport, _, _ = switch_serial([bad, *warning_reply()[1:]])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "reply", [[HEADER, *warning_reply()], [absolute_reply()[0], *warning_reply()]]
)
def test_warning_header_must_be_first_and_unique(switch_serial, reply):
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError):
        transport.status()


def test_warning_capability_is_orthogonal_to_positioning_and_calibration(switch_serial):
    reply = [
        HEADER,
        *[
            line.rstrip() + b" warning=none warnings=0\n" if b"pos=" in line else line
            for line in ccw_reply()
        ],
    ]
    transport, port, _ = switch_serial(reply)
    transport.status()
    assert transport.hold_direction_warnings and not transport.absolute_positioning
    assert not transport.auto_calibration
    port.lines.extend([*all_frames(), ccw_frame(4).rstrip() + b" warning=none warnings=6\n"])
    assert transport.poll_status()["bottom"].recalibration_recommended


@pytest.mark.parametrize("new_first", [False, True])
def test_warning_capability_cannot_change_without_reconnect(switch_serial, new_first):
    before, after = (
        (warning_reply, absolute_reply) if new_first else (absolute_reply, warning_reply)
    )
    transport, port, _ = switch_serial(before())
    transport.status()
    port.lines.extend(after())
    with pytest.raises(LinkError, match="direction-warning capability changed"):
        transport.status()


@pytest.mark.parametrize("new_firmware", [False, True])
@pytest.mark.parametrize("early", [False, True])
def test_warning_field_presence_matches_capability_even_before_first_header(
    switch_serial, new_firmware, early
):
    reply = warning_reply() if new_firmware else absolute_reply()
    wrong = calibration_frame(1) if new_firmware else warning_frame(1)
    transport, port, _ = switch_serial([wrong, *reply] if early else reply)
    if early:
        with pytest.raises(LinkError, match="direction-warning capability"):
            transport.status()
    else:
        transport.status()
        port.lines.append(wrong)
        with pytest.raises(LinkError, match="direction-warning capability"):
            transport.poll_status()


@pytest.mark.parametrize("reply", [[HEADER, *absolute_reply()], warning_reply()[1:]])
def test_status_warning_fields_require_matching_header(switch_serial, reply):
    transport, _, _ = switch_serial(reply)
    with pytest.raises(LinkError, match="direction-warning capability"):
        transport.status()


@pytest.mark.parametrize(
    "warning,count",
    [("invalid", 1), ("pause", 1), ("none", -1), ("none", 65536), ("none", "NaN"), ("none", "1.5")],
)
def test_malformed_warning_frame_is_rejected(warned_stream, warning, count):
    transport, port, _ = warned_stream
    port.lines.append(warning_frame(7, warning=warning, count=count))
    with pytest.raises(LinkError):
        transport.poll_status()


def test_legacy_warning_defaults_do_not_claim_advisory(switch_serial):
    transport, _, _ = switch_serial(absolute_reply())
    state = transport.status()["bottom"]
    assert not transport.hold_direction_warnings
    assert state.warning == "none" and state.direction_warning_count == 0
    assert not state.recalibration_recommended


def test_warning_fragment_and_command_ack_keep_atomic_position_and_bounds(warned_stream):
    transport, port, _ = warned_stream
    message = warning_frame(
        7,
        warning="direction_pause",
        count=6,
        counts=1000,
        lower=0,
        upper=4096,
        bounds=3,
        phase="done",
    )
    port.lines.append(message[:-10])
    assert transport.poll_status()["bottom"].direction_warning_count == 0
    port.lines.extend([message[-10:], b"OK: bottom keepalive\n"])
    transport.command("keepalive")
    state = transport.poll_status()["bottom"]
    assert state.warning == "direction_pause" and state.recalibration_recommended
    assert state.position == 1000 / 4096 and state.upper_limit == 1 and state.calibration == "done"


def test_status_wins_over_older_warning_frame_and_duplicate_cannot_revive_pause(warned_stream):
    transport, port, _ = warned_stream
    port.lines.extend(
        [warning_frame(7, warning="direction_pause", count=6), *warning_reply(count=6)]
    )
    assert transport.status()["bottom"].warning == "none"
    port.lines.append(warning_frame(7, warning="direction_pause", count=6))
    assert transport.poll_status()["bottom"].warning == "none"


def test_warning_stream_still_requires_fresh_lim_despite_tel_updates(warned_stream):
    transport, port, clock = warned_stream
    clock.advance(1.001)
    port.lines.extend(all_frames(7))
    with pytest.raises(LinkError, match="limit/switch telemetry stopped"):
        transport.poll_status()


def resume_demo(controller, clock):
    for _ in range(3):
        clock.advance(0.04)
        controller.poll()


def test_minor_warning_freezes_position_keeps_lease_and_resumes_with_fresh_ramp(stage_rig):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -1, 1
    state.lower_limit_set = state.upper_limit_set = True
    start(controller, clock)
    clock.advance(0.25)
    controller.poll()
    before = state.position
    transport.inject_hold_direction_warning("bottom", 40)
    controller.poll()
    assert axis(controller)["warning"] == "direction_pause"
    assert state.mode == "continuous" and state.fault == "none" and state.measured_speed == 0
    assert state.position == before and state.lower_limit == -1 and state.upper_limit == 1
    controller.keep_hold_alive("bottom", FIRST)
    assert "bottom" in controller._holds
    resume_demo(controller, clock)
    assert state.warning == "none" and state.mode == "continuous" and state.position == before
    clock.advance(0.1)
    controller.poll()
    assert state.measured_speed == pytest.approx(-0.025)
    assert state.position == pytest.approx(before - 0.00125)
    assert state.direction_warning_count == 1
    assert transport.commands.count("hold") == 1 and "calibrate" not in transport.commands


def test_duplicate_reads_are_not_distinct_stable_samples(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    clock.advance(0.11)
    for _ in range(10):
        controller.poll()
    assert axis(controller)["warning"] == "direction_pause"
    clock.advance(0.01)
    controller.poll()
    clock.advance(0.01)
    controller.poll()
    assert axis(controller)["warning"] == "none"


@pytest.mark.parametrize(
    "excursion,warning,fault,count",
    [
        (32, "none", "none", 0),
        (33, "direction_pause", "none", 1),
        (63, "direction_pause", "none", 1),
        (64, "none", "direction", 1),
    ],
)
def test_minor_and_hard_thresholds_count_each_incident_once(
    stage_rig, excursion, warning, fault, count
):
    controller, transport, clock = stage_rig
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", excursion)
    controller.poll()
    state = transport.motors["bottom"]
    assert state.warning == warning and state.fault == fault
    assert state.direction_warning_count == count


def test_pause_escalation_does_not_count_twice(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    transport.inject_hold_direction_warning("bottom", 64)
    controller.poll()
    state = transport.motors["bottom"]
    assert state.fault == "direction" and state.warning == "none" and state.mode == "idle"
    assert state.direction_warning_count == 1


@pytest.mark.parametrize(
    "excursion,warning,fault",
    [
        (102, "none", "none"),
        (103, "direction_pause", "none"),
        (204, "direction_pause", "none"),
        (205, "none", "direction"),
    ],
)
def test_coarse_motor_uses_exact_pulse_scaled_threshold(stage_rig, excursion, warning, fault):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].steps_per_revolution = 10
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", excursion)
    state = transport.motors["bottom"]
    assert state.warning == warning and state.fault == fault


def test_unstable_pause_faults_after_half_second_without_a_second_incident(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    for _ in range(5):
        clock.advance(0.1)
        transport.inject_hold_direction_warning("bottom", 40)
    clock.advance(0.01)
    controller.poll()
    state = transport.motors["bottom"]
    assert state.fault == "direction" and state.mode == "idle"
    assert state.direction_warning_count == 1


def test_third_incident_in_sliding_window_faults_and_counts_the_third(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    for incident in range(3):
        transport.inject_hold_direction_warning("bottom", 40)
        if incident < 2:
            resume_demo(controller, clock)
            controller.keep_hold_alive("bottom", FIRST)
    controller.poll()
    state = transport.motors["bottom"]
    assert state.direction_warning_count == 3 and state.fault == "direction"
    assert state.mode == "idle" and state.warning == "none"


def test_frequency_guard_uses_true_sliding_window_across_separate_holds(stage_rig):
    controller, transport, clock = stage_rig
    beginning = clock()
    for index, elapsed in enumerate((0, 8, 11, 17)):
        clock.advance(beginning + elapsed - clock())
        hold_id = f"sliding-warning-{index:04d}"
        controller.hold("bottom", "ccw", 0.25, hold_id, clock())
        transport.inject_hold_direction_warning("bottom", 40)
        if index < 3:
            assert transport.motors["bottom"].fault == "none"
            resume_demo(controller, clock)
            controller.release_hold("bottom", hold_id)
    controller.poll()
    state = transport.motors["bottom"]
    # At17s the first incident is old, but incidents at8s,11s,17s share a10s window.
    assert state.direction_warning_count == 4 and state.fault == "direction"


@pytest.mark.parametrize("stop", ["release", "stop_all", "watchdog", "contact", "fault"])
def test_cancellation_during_pause_does_not_retry_or_erase_history(stage_rig, stop):
    controller, transport, clock = stage_rig
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    state = transport.motors["bottom"]
    before = state.position
    if stop == "release":
        controller.release_hold("bottom", FIRST)
    elif stop == "stop_all":
        controller.stop_all()
    elif stop == "watchdog":
        clock.advance(1.1)
    elif stop == "contact":
        state.switch_mask = 1
    else:
        state.encoder, state.fault = "offline", "encoder"
    controller.poll()
    assert state.mode == "idle" and state.warning == "none" and state.direction_warning_count == 1
    assert state.position == before and "bottom" not in controller._holds
    clock.advance(5)
    controller.poll()
    assert state.position == before and state.mode == "idle"


def test_six_episodes_across_holds_recommend_calibration_without_disabling_motion(stage_rig):
    controller, transport, clock = stage_rig
    for incident in range(6):
        hold = f"warning-episode-{incident:04d}"
        controller.hold("bottom", "ccw", 0.25, hold, clock())
        transport.inject_hold_direction_warning("bottom", 40)
        resume_demo(controller, clock)
        controller.release_hold("bottom", hold)
        clock.advance(10.1)
        controller.poll()
        assert axis(controller)["recalibration_recommended"] == (incident >= 5)
    assert axis(controller)["available"] and axis(controller)["fault"] == "none"
    controller.move("bottom", "ccw", 0.25, 0.1)
    assert transport.commands[-1] == "rev 0.1" and "calibrate" not in transport.commands


@pytest.mark.parametrize(
    "action",
    [
        "stop_all",
        "reset_position",
        "set_limit",
        "clear_lower_limit",
        "clear_upper_limit",
        "clear_fault",
    ],
)
def test_stop_and_manual_controls_retain_warning_history(stage_rig, action):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.position, state.direction_warning_count = 0.5, 6
    if action == "clear_fault":
        state.fault = "stall"
    getattr(controller, action)(*(() if action == "stop_all" else ("bottom",)))
    assert axis(controller)["direction_warning_count"] == 6
    assert axis(controller)["recalibration_recommended"]


def test_only_successful_explicit_calibration_clears_history_and_frequency(stage_rig):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.direction_warning_count = 6
    transport._direction_incidents["bottom"].extend([clock(), clock()])
    controller.calibrate("bottom", 0.25)
    controller.stop_all()
    assert state.direction_warning_count == 6
    clock.advance(1.1)
    controller.calibrate("bottom", 0.25)
    state.encoder, state.fault = "offline", "encoder"
    controller.poll()
    assert state.calibration == "failed" and state.direction_warning_count == 6
    state.encoder = "ok"
    controller.clear_fault("bottom")
    clock.advance(1.1)
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    assert state.calibration == "done" and state.direction_warning_count == 0
    assert not state.recalibration_recommended and not transport._direction_incidents["bottom"]


def test_warning_counter_saturates_and_board_restart_starts_new_history(stage_rig):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].direction_warning_count = 65535
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    assert transport.motors["bottom"].direction_warning_count == 65535
    assert DemoTransport(clock).motors["bottom"].direction_warning_count == 0


def test_events_log_episode_pause_resume_and_advisory_once(stage_rig):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].direction_warning_count = 5
    controller.poll()
    start(controller, clock)
    transport.inject_hold_direction_warning("bottom", 40)
    controller.poll()
    events = list(controller.snapshot()["events"])
    for _ in range(5):
        controller.poll()
    assert controller.snapshot()["events"] == events
    resume_demo(controller, clock)
    messages = [event["message"] for event in controller.snapshot()["events"]]
    assert sum("total 6" in text for text in messages) == 1
    assert sum("hold paused" in text for text in messages) == 1
    assert sum("held motion resumed" in text for text in messages) == 1
    assert sum("Auto calibrate is recommended" in text for text in messages) == 1


def test_reconnecting_to_existing_history_does_not_relog_old_incidents(switch_serial):
    transport, port, clock = switch_serial(warning_reply(count=6))
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [
            *all_frames(),
            warning_frame(4, count=6),
            warning_frame(5, "top", count=6),
            warning_frame(6, "insert", count=6),
            warning_frame(4, count=1),
        ]
    )
    controller.poll()
    assert axis(controller)["direction_warning_count"] == 6
    assert axis(controller)["recalibration_recommended"]
    assert not any("recorded" in event["message"] for event in controller.snapshot()["events"])


def test_pause_keeps_controller_hold_identifier_despite_adjacent_idle_tel(switch_serial):
    transport, port, clock = switch_serial(warning_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [*all_frames(), warning_frame(4), warning_frame(5, "top"), warning_frame(6, "insert")]
    )
    controller.poll()
    port.lines.extend(
        [
            *warning_reply(),
            b"OK: bottom motor\n",
            b"OK: bottom dir\n",
            b"OK: bottom speed\n",
            b"OK: bottom hold\n",
        ]
    )
    start(controller, clock)
    port.lines.extend([frame(7, mode="idle"), warning_frame(8, warning="direction_pause", count=1)])
    controller.poll()
    assert "bottom" in controller._holds
    assert not axis(controller)["available"] and axis(controller)["fault"] == "none"
    port.lines.extend([b"OK: bottom motor\n", b"OK: bottom keepalive\n"])
    controller.keep_hold_alive("bottom", FIRST)
    assert port.writes[-2:] == [b"motor bottom\n", b"keepalive\n"]


def test_advisory_does_not_disable_calibrated_positioning_or_auto_calibration(stage_rig):
    controller, transport, clock = stage_rig
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    clock.advance(1.1)
    transport.motors["bottom"].direction_warning_count = 6
    controller.poll()
    assert axis(controller)["positioning_ready"] and axis(controller)["available"]
    assert axis(controller)["recalibration_recommended"]
    controller.calibrate("bottom", 0.25)
    assert axis(controller)["calibration"] == "lower"
