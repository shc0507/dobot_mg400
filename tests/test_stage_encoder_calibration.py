"""Arduino-owned switch-to-switch calibration and autonomous demo parity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_stage_encoder_coordinates import COORDINATES, ccw_frame, ccw_reply
from test_stage_encoder_limits import axis
from test_stage_encoder_switches import switch_serial as switch_serial
from test_stage_encoder_telemetry import all_frames, frame

from stage_controller.controller import MotorController
from stage_controller.protocol import (
    CommandRejected,
    ControllerError,
    LinkError,
)

HEADER = b"calibration=switch_midpoint_v1\n"
PHASES = ("none", "lower", "upper", "center", "done", "cancelled", "failed")


def calibration_reply(phase="none", **kwargs):
    return [
        HEADER,
        *[
            line.rstrip() + f" calibration={phase}\n".encode() if b"pos=" in line else line
            for line in ccw_reply(**kwargs)
        ],
    ]


def calibration_frame(seq, motor="bottom", *, phase="none", **kwargs):
    return ccw_frame(seq, motor, **kwargs).rstrip() + f" calibration={phase}\n".encode()


@pytest.fixture
def calibrated_stream(switch_serial):
    transport, port, clock = switch_serial(calibration_reply())
    transport.status()
    port.lines.extend(
        [
            *all_frames(),
            calibration_frame(4),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
        ]
    )
    transport.poll_status()
    port.writes.clear()
    return transport, port, clock


@pytest.mark.parametrize("phase", PHASES)
def test_calibration_phase_is_part_of_status_and_atomic_limit_stream(switch_serial, phase):
    transport, port, _ = switch_serial(calibration_reply(phase))
    assert transport.status()["bottom"].calibration == phase
    assert transport.auto_calibration
    port.lines.extend(
        [
            *all_frames(),
            calibration_frame(4, phase=phase, counts=2048, lower=0, upper=4096, bounds=3),
        ]
    )
    state = transport.poll_status()["bottom"]
    assert state.calibration == phase
    assert state.position == 0.5 and state.lower_limit == 0 and state.upper_limit == 1


@pytest.mark.parametrize(
    "headers",
    [
        [b"calibration=switch_midpoint_v2\n"],
        [HEADER, HEADER],
        [COORDINATES, HEADER],
        [b"calibration switch_midpoint_v1\n"],
    ],
)
def test_calibration_header_must_be_first_unique_and_known(switch_serial, headers):
    transport, _, _ = switch_serial([*headers, *calibration_reply()[1:]])
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize(
    "missing", [b"coordinates=", b"homing=", b"switches=", b"limits=", b"telemetry=", b"motion="]
)
def test_calibration_requires_current_closed_loop_switch_homing_capabilities(
    switch_serial, missing
):
    rows = [line for line in calibration_reply() if not line.startswith(missing)]
    transport, _, _ = switch_serial(rows)
    with pytest.raises(LinkError):
        transport.status()


@pytest.mark.parametrize("new_firmware", [False, True])
@pytest.mark.parametrize("early", [False, True])
def test_calibration_lim_field_presence_must_match_capability(switch_serial, new_firmware, early):
    reply = calibration_reply() if new_firmware else ccw_reply()
    wrong = ccw_frame(1) if new_firmware else calibration_frame(1)
    transport, port, _ = switch_serial([wrong, *reply] if early else reply)
    if early:
        with pytest.raises(LinkError, match="calibration capability"):
            transport.status()
    else:
        transport.status()
        port.lines.append(wrong)
        with pytest.raises(LinkError, match="calibration capability"):
            transport.poll_status()


@pytest.mark.parametrize(
    "rows",
    [
        [HEADER, *ccw_reply()],
        calibration_reply()[1:],
    ],
)
def test_calibration_status_fields_require_matching_capability(switch_serial, rows):
    transport, _, _ = switch_serial(rows)
    with pytest.raises(LinkError, match="calibration capability"):
        transport.status()


@pytest.mark.parametrize("phase", ["bad", "LOWER", "lower extra=1", "", "0"])
def test_unknown_calibration_phase_cannot_be_ignored(calibrated_stream, phase):
    transport, port, _ = calibrated_stream
    port.lines.append(calibration_frame(7, phase=phase))
    with pytest.raises(LinkError):
        transport.poll_status()


def test_fragmented_calibration_frame_survives_command_interleave(calibrated_stream):
    transport, port, _ = calibrated_stream
    message = calibration_frame(7, phase="upper", counts=0, lower=0, bounds=1)
    port.lines.append(message[:-8])
    assert transport.poll_status()["bottom"].calibration == "none"
    port.lines.extend([message[-8:], b"OK: bottom speed\n"])
    transport.command("speed 0.5")
    state = transport.poll_status()["bottom"]
    assert state.calibration == "upper" and state.lower_limit_set and state.position == 0
    assert state.speed == 0.5


def test_fresh_status_wins_over_older_calibration_frame(calibrated_stream):
    transport, port, _ = calibrated_stream
    port.lines.extend(
        [
            calibration_frame(7, phase="upper", lower=0, bounds=1),
            *calibration_reply("done", lower=0, upper=4096, bounds=3, position=0.5),
        ]
    )
    state = transport.status()["bottom"]
    assert state.calibration == "done" and state.position == 0.5 and state.upper_limit_set
    port.lines.extend(all_frames(8))
    assert transport.poll_status()["bottom"].calibration == "done"


def test_pre_command_none_cannot_clear_an_acknowledged_calibration(calibrated_stream):
    transport, port, _ = calibrated_stream
    port.lines.extend([calibration_frame(7), b"OK: bottom calibrate\n"])
    transport.command("calibrate")
    port.lines.append(frame(8, mode="idle"))
    state = transport.poll_status()["bottom"]
    assert state.calibration == "lower"


def test_legacy_coordinate_firmware_has_no_calibration_action(switch_serial):
    transport, port, clock = switch_serial(ccw_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    assert not controller.connect("DEMO")["auto_calibration"]
    assert all(state.calibration == "none" for state in transport._states.values())
    port.writes.clear()
    port.lines.extend(ccw_reply())
    with pytest.raises(ControllerError, match="automatic switch calibration"):
        controller.calibrate("bottom", 0.25)
    assert port.writes == [b"status\n"]


@pytest.mark.parametrize("new_first", [False, True])
def test_calibration_capability_is_pinned(switch_serial, new_first):
    first, second = (calibration_reply, ccw_reply) if new_first else (ccw_reply, calibration_reply)
    transport, port, _ = switch_serial(first())
    transport.status()
    port.lines.extend(second())
    with pytest.raises(LinkError, match="calibration capability changed"):
        transport.status()


def test_demo_visits_lower_upper_center_in_order_and_cools_down_only_after_done(stage_rig):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.direction = 0.5, "ccw"
    state.lower_limit, state.upper_limit = 0.4, 0.6
    state.lower_limit_set = state.upper_limit_set = True
    controller.calibrate("bottom", 0.5)
    assert not state.lower_limit_set and not state.upper_limit_set
    clock.advance(1)
    controller.poll()
    assert axis(controller)["calibration"] == "lower"
    assert axis(controller)["direction"] == "cw" and axis(controller)["measured_speed"] == -0.25
    clock.advance(1.5)
    controller.poll()
    assert axis(controller)["calibration"] == "upper"
    assert axis(controller)["position"] == axis(controller)["lower_limit"] == 0
    assert axis(controller)["switch_mask"] == 1 and axis(controller)["cooldown"] == 0
    clock.advance(4.5)
    controller.poll()
    assert axis(controller)["calibration"] == "center"
    assert axis(controller)["upper_limit"] == 1 and axis(controller)["position"] == 1
    assert axis(controller)["cooldown"] == 0
    clock.advance(2)
    controller.poll()
    assert axis(controller)["calibration"] == "done"
    assert axis(controller)["position"] == 0.5 and axis(controller)["direction"] == "ccw"
    assert axis(controller)["cooldown"] == 1
    assert transport.commands == ["motor bottom", "speed 0.5", "calibrate"]


@pytest.mark.parametrize("action", ["stop_all", "disconnect"])
@pytest.mark.parametrize(
    "elapsed,phase,upper_set", [(1, "lower", False), (3, "upper", False), (7.5, "center", True)]
)
def test_cancel_each_phase_preserves_measured_bounds_and_cannot_resume(
    stage_rig, action, elapsed, phase, upper_set
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.direction = 0.5, "ccw"
    controller.calibrate("bottom", 0.25)
    clock.advance(elapsed)
    controller.poll()
    assert axis(controller)["calibration"] == phase
    getattr(controller, action)()
    assert state.calibration == "cancelled" and state.mode == "idle"
    assert state.direction == "ccw" and state.speed == 0.25
    assert state.lower_limit_set == (phase != "lower")
    assert state.upper_limit_set == upper_set
    stopped = state.position
    clock.advance(100)
    transport.status()
    assert state.position == stopped and state.calibration == "cancelled"
    assert not transport.calibrations


@pytest.mark.parametrize(
    "elapsed,lower_set,upper_set", [(1, False, False), (3, True, False), (7.5, True, True)]
)
def test_encoder_fault_fails_each_phase_without_erasing_measured_bounds(
    stage_rig, elapsed, lower_set, upper_set
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.direction = 0.5, "ccw"
    controller.calibrate("bottom", 0.25)
    clock.advance(elapsed)
    controller.poll()
    state.encoder, state.fault = "offline", "encoder"
    controller.poll()
    assert state.calibration == "failed" and state.mode == "idle" and state.direction == "ccw"
    assert state.lower_limit_set == lower_set and state.upper_limit_set == upper_set
    assert not axis(controller)["available"]


@pytest.mark.parametrize("start,lower_set", [(0.5, False), (0, True)])
def test_each_switch_search_has_its_own_timeout(stage_rig, start, lower_set):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    state.position, state.direction = start, "ccw"
    controller.calibrate("bottom", 0.001)
    clock.advance(121)
    controller.poll()
    assert state.calibration == "failed" and state.fault == "timeout"
    assert state.mode == "idle" and state.direction == "ccw"
    assert state.lower_limit_set == lower_set and not state.upper_limit_set


@pytest.mark.parametrize(
    "speed", [None, True, "0.25", float("nan"), float("inf"), 0, 0.0009, 0.501, 5.1]
)
def test_invalid_calibration_speed_preserves_bounds_and_never_commands(stage_rig, speed):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    with pytest.raises(ValueError):
        controller.calibrate("bottom", speed)
    assert transport.commands == []
    assert state.lower_limit == -0.25 and state.upper_limit == 0.5


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("encoder", "offline"),
        ("encoder", "untrusted"),
        ("fault", "stall"),
        ("mode", "moving"),
        ("switch_mask", 3),
    ],
)
def test_rejected_calibration_start_preserves_previous_limits(stage_rig, attribute, value):
    controller, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = -0.25, 0.5
    state.lower_limit_set = state.upper_limit_set = True
    setattr(state, attribute, value)
    with pytest.raises(ControllerError):
        controller.calibrate("bottom", 0.25)
    assert transport.commands == []
    assert state.lower_limit_set and state.upper_limit_set
    assert state.lower_limit == -0.25 and state.upper_limit == 0.5


@pytest.mark.parametrize("phase", ["lower", "upper", "center"])
def test_phase_remains_busy_when_adjacent_tel_says_idle(switch_serial, phase):
    transport, port, clock = switch_serial(calibration_reply())
    controller = MotorController(demo=True, transport_factory=lambda _: transport, clock=clock)
    controller.connect("DEMO")
    port.lines.extend(
        [
            *all_frames(),
            calibration_frame(4, phase=phase),
            calibration_frame(5, "top"),
            calibration_frame(6, "insert"),
            frame(7, mode="idle"),
        ]
    )
    controller.poll()
    assert axis(controller)["mode"] == "idle" and not axis(controller)["available"]
    assert axis(controller)["cooldown"] == 0
    for action in (
        lambda: controller.move("bottom", "ccw", 0.25, 0.1),
        lambda: controller.reset_position("bottom"),
        lambda: controller.set_limit("bottom"),
        lambda: controller.clear_upper_limit("bottom"),
        lambda: controller.clear_fault("bottom"),
        lambda: controller.calibrate("bottom", 0.25),
    ):
        port.lines.extend(calibration_reply(phase))
        with pytest.raises(ControllerError):
            action()
    assert all(command == b"status\n" for command in port.writes)


def test_calibration_endpoints_survive_manual_coordinate_reset(stage_rig):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].position = 0.25
    controller.reset_position("bottom")
    controller.calibrate("bottom", 0.25)
    clock.advance(20)
    controller.poll()
    after = axis(controller)
    assert after["calibration"] == "done" and after["position"] == 0.5
    assert after["lower_limit"] == 0 and after["upper_limit"] == 1
    assert after["encoder_raw"] == 2048


def test_midpoint_uses_nearest_encoder_count_with_half_ties_up(stage_rig):
    controller, transport, clock = stage_rig
    transport._calibration_endpoints["bottom"] = (0, -1025 / 4096)
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    assert axis(controller)["upper_limit"] == 1025 / 4096
    assert axis(controller)["position"] == 513 / 4096


@pytest.mark.parametrize(
    "action", ["reset_position", "set_limit", "clear_upper_limit", "clear_lower_limit"]
)
def test_manual_position_or_limit_change_invalidates_previous_done_state(stage_rig, action):
    controller, _, clock = stage_rig
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    assert axis(controller)["calibration"] == "done"
    getattr(controller, action)("bottom")
    assert axis(controller)["calibration"] == "none"


def test_simultaneous_calibrations_start_only_one_axis(stage_rig):
    controller, transport, clock = stage_rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(controller.calibrate, motor, 0.25) for motor in ("bottom", "top")]
    assert sum(future.exception() is None for future in futures) == 1
    assert len(transport.commands) == 3
    assert transport.commands[0].startswith("motor ")
    assert transport.commands[1:] == ["speed 0.25", "calibrate"]
    clock.advance(10)
    controller.poll()
    assert sum(m["calibration"] == "done" for m in controller.snapshot()["motors"]) == 1
    assert not transport.calibrations


def test_demo_bad_direct_start_is_rejected_before_clearing_limits(stage_rig):
    _, transport, _ = stage_rig
    state = transport.motors["bottom"]
    state.lower_limit, state.upper_limit = 0, 1
    state.lower_limit_set = state.upper_limit_set = True
    state.encoder = "magnet"
    with pytest.raises(CommandRejected):
        transport.command("calibrate")
    assert state.lower_limit == 0 and state.upper_limit == 1 and state.calibration == "none"


@pytest.mark.parametrize("field", ["telemetry_age_ms", "limit_telemetry_age_ms"])
@pytest.mark.parametrize("age", [None, 1001])
def test_calibration_waits_for_fresh_encoder_and_limit_streams(stage_rig, monkeypatch, field, age):
    controller, transport, _ = stage_rig
    original = transport.status

    def stale_status():
        states = original()
        states["bottom"] = replace(states["bottom"], **{field: age})
        return states

    monkeypatch.setattr(transport, "status", stale_status)
    with pytest.raises(ControllerError, match="fresh"):
        controller.calibrate("bottom", 0.25)
    assert transport.commands == []


def test_upper_search_timeout_starts_after_lower_search_finishes(stage_rig):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].position = 0.06
    controller.calibrate("bottom", 0.001)
    clock.advance(121)
    controller.poll()
    assert axis(controller)["calibration"] == "upper" and axis(controller)["fault"] == "none"
    clock.advance(60)
    controller.poll()
    assert axis(controller)["calibration"] == "failed" and axis(controller)["fault"] == "timeout"
    assert axis(controller)["lower_limit_set"] and not axis(controller)["upper_limit_set"]
