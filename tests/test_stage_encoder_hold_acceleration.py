"""Held startup ramps smoothly; release/watchdog/limits still stop immediately."""

import pytest
from test_stage_encoder_limits import axis

from stage_controller.protocol import CommandRejected, ControllerError, DemoTransport

HOLD = "ramp-profile-hold-01"


@pytest.mark.parametrize("direction,sign", [("ccw", 1), ("cw", -1)])
def test_hold_ramps_from_zero_to_target_and_heartbeats_do_not_restart_it(
    stage_rig, direction, sign
):
    controller, transport, clock = stage_rig
    controller.hold("bottom", direction, 0.3, HOLD, clock())
    assert axis(controller)["speed"] == 0.3
    assert axis(controller)["measured_speed"] == 0
    # At .25 rotations/s², a .3 rotations/s target takes 1.2 seconds to reach.
    expected = {
        1: (0.0078125, 0.0625),
        2: (0.03125, 0.125),
        4: (0.125, 0.25),
        8: (0.42, 0.3),
        10: (0.57, 0.3),
    }
    for tick in range(1, 11):
        clock.advance(0.25)
        controller.keep_hold_alive("bottom", HOLD)
        controller.poll()
        if tick in expected:
            position, speed = expected[tick]
            assert axis(controller)["position"] == pytest.approx(sign * position)
            assert axis(controller)["measured_speed"] == pytest.approx(sign * speed)
            assert axis(controller)["mode"] == "continuous"
    assert transport.commands.count("hold") == 1
    assert transport.commands.count("keepalive") == 10


@pytest.mark.parametrize(
    "acceleration,position,speed",
    [
        (0.125, 0.015625, 0.0625),
        (0.25, 0.03125, 0.125),
        (1.0, 0.09375, 0.25),
    ],
)
def test_hold_uses_existing_acceleration_and_never_exceeds_target(
    stage_rig, acceleration, position, speed
):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].acceleration = acceleration
    controller.hold("bottom", "ccw", 0.25, HOLD, clock())
    clock.advance(0.5)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(position)
    assert axis(controller)["measured_speed"] == pytest.approx(speed)
    assert axis(controller)["speed"] == 0.25


@pytest.mark.parametrize("stop", ["release", "stop_all", "disconnect"])
def test_stop_during_startup_has_no_deceleration_tail_or_restart(stage_rig, stop):
    controller, transport, clock = stage_rig
    controller.hold("bottom", "ccw", 0.3, HOLD, clock())
    clock.advance(0.25)
    if stop == "release":
        controller.release_hold("bottom", HOLD)
    else:
        getattr(controller, stop)()
    stopped = transport.motors["bottom"].position
    assert stopped == pytest.approx(0.0078125)
    assert transport.motors["bottom"].measured_speed == 0
    assert transport.motors["bottom"].mode == "idle"
    clock.advance(10)
    assert transport.status()["bottom"].position == stopped
    with pytest.raises(ControllerError):
        controller.keep_hold_alive("bottom", HOLD)


def test_watchdog_caps_ramp_at_its_deadline_without_controller_poll(stage_rig):
    _, _, clock = stage_rig
    transport = DemoTransport(clock)
    transport.command("dir ccw")
    transport.command("speed 0.3")
    transport.command("hold")
    clock.advance(20)
    state = transport.status()["bottom"]
    # The watchdog expired after one second, before the end of the 1.2-second ramp.
    assert state.position == 0.125
    assert state.mode == "idle" and state.measured_speed == 0
    with pytest.raises(CommandRejected, match="ended"):
        transport.command("keepalive")


def test_delayed_poll_and_frequent_polls_produce_the_same_held_trajectory(stage_rig):
    _, _, clock = stage_rig
    frequent, delayed = DemoTransport(clock), DemoTransport(clock)
    for transport in (frequent, delayed):
        transport.command("dir ccw")
        transport.command("speed 0.3")
        transport.command("hold")
    for _ in range(16):
        clock.advance(0.05)
        frequent.status()
    assert frequent.status()["bottom"].position == pytest.approx(0.08)
    assert delayed.status()["bottom"].position == pytest.approx(0.08)
    assert frequent.status()["bottom"].measured_speed == delayed.status()["bottom"].measured_speed


def test_new_hold_restarts_from_zero_after_release_and_cooldown(stage_rig):
    controller, _, clock = stage_rig
    controller.hold("bottom", "ccw", 0.3, HOLD, clock())
    clock.advance(0.5)
    controller.release_hold("bottom", HOLD)
    start = axis(controller)["position"]
    clock.advance(1.1)
    controller.hold("bottom", "ccw", 0.3, "ramp-profile-hold-02", clock())
    assert axis(controller)["measured_speed"] == 0
    clock.advance(0.25)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(start + 0.0078125)
    assert axis(controller)["measured_speed"] == 0.0625


def test_rejected_second_axis_start_does_not_restart_first_axis_ramp(stage_rig):
    controller, _, clock = stage_rig
    controller.hold("bottom", "ccw", 0.3, HOLD, clock())
    clock.advance(0.5)
    controller.keep_hold_alive("bottom", HOLD)
    with pytest.raises(ControllerError, match="other stage motor"):
        controller.hold("top", "cw", 0.3, "ramp-second-axis-01", clock())
    clock.advance(0.25)
    controller.poll()
    assert axis(controller)["measured_speed"] == 0.1875
    assert axis(controller, "top")["measured_speed"] == 0
    assert axis(controller)["position"] == 0.0703125
    assert axis(controller, "top")["position"] == 0


@pytest.mark.parametrize("direction,limit,side", [("cw", -0.01, "lower"), ("ccw", 0.01, "upper")])
def test_nearby_limit_stops_during_acceleration_without_finishing_ramp(
    stage_rig, direction, limit, side
):
    controller, transport, clock = stage_rig
    state = transport.motors["bottom"]
    setattr(state, f"{side}_limit", limit)
    setattr(state, f"{side}_limit_set", True)
    controller.hold("bottom", direction, 0.3, HOLD, clock())
    clock.advance(0.4)
    controller.poll()
    assert axis(controller)["position"] == limit
    assert axis(controller)["measured_speed"] == 0 and axis(controller)["mode"] == "idle"
    assert axis(controller)["cooldown"] == 1
    with pytest.raises(ControllerError, match="ended"):
        controller.keep_hold_alive("bottom", HOLD)


def test_finite_moves_keep_the_existing_demo_motion_profile(stage_rig):
    controller, transport, clock = stage_rig
    transport.motors["bottom"].acceleration = 0.01
    controller.move("bottom", "ccw", 0.3, 0.5)
    clock.advance(0.25)
    controller.poll()
    assert axis(controller)["position"] == pytest.approx(0.075)
    assert axis(controller)["measured_speed"] == 0.3
    assert axis(controller)["remaining"] == pytest.approx(0.425)
