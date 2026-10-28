import pytest

from stage_controller.protocol import CommandRejected, ControllerError, DemoTransport

FIRST = "hold-session-first-0001"
SECOND = "hold-session-second-0002"


def start(controller, clock, motor="bottom", direction="cw", speed=50, hold_id=FIRST):
    return controller.hold(motor, direction, speed, hold_id, clock())


def status(controller, motor="bottom"):
    return next(item for item in controller.snapshot()["motors"] if item["id"] == motor)


def test_hold_runs_at_speed_until_release_without_step_limit(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    assert transport.commands == ["motor bottom", "dir cw", "speed 50", "hold"]
    for _ in range(20):
        clock.advance(0.5)
        controller.keep_hold_alive("bottom", FIRST)
        controller.poll()
    assert status(controller)["mode"] == "continuous"
    assert status(controller)["position"] == 500
    controller.release_hold("bottom", FIRST)
    assert transport.commands[-2:] == ["motor bottom", "stop"]
    assert status(controller)["mode"] == "idle"
    assert status(controller)["cooldown"] == 1
    clock.advance(5)
    controller.poll()
    assert status(controller)["position"] == 500


def test_release_only_stops_its_motor_after_other_motor_was_selected(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    start(controller, clock, "top", "ccw", 100, SECOND)
    clock.advance(0.5)
    controller.keep_hold_alive("top", SECOND)
    controller.release_hold("bottom", FIRST)
    assert transport.commands[-2:] == ["motor bottom", "stop"]
    assert status(controller)["mode"] == "idle"
    assert status(controller, "top")["mode"] == "continuous"
    assert status(controller, "top")["position"] == -50


def test_release_before_start_prevents_late_motion(stage_rig):
    controller, transport, clock = stage_rig
    sampled_at = clock()
    controller.release_hold("bottom", FIRST)
    with pytest.raises(ControllerError, match="ended"):
        start(controller, clock)
    assert transport.commands == []
    clock.advance(11)
    with pytest.raises(ControllerError, match="expired"):
        controller.hold("bottom", "cw", 50, FIRST, sampled_at)
    assert transport.commands == []


def test_late_release_and_heartbeat_cannot_stop_or_renew_new_hold(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    controller.release_hold("bottom", FIRST)
    clock.advance(1.1)
    start(controller, clock, hold_id=SECOND)
    transport.commands.clear()
    controller.release_hold("bottom", FIRST)
    with pytest.raises(ControllerError, match="ended"):
        controller.keep_hold_alive("bottom", FIRST)
    assert transport.commands == []
    assert status(controller)["mode"] == "continuous"


def test_missing_heartbeat_stops_and_cannot_restart_with_old_identifier(stage_rig):
    controller, transport, clock = stage_rig
    start(controller, clock)
    clock.advance(1.1)
    controller.poll()
    assert status(controller)["mode"] == "idle"
    assert status(controller)["position"] == 50
    assert status(controller)["cooldown"] == 1
    transport.commands.clear()
    with pytest.raises(ControllerError, match="ended"):
        controller.keep_hold_alive("bottom", FIRST)
    clock.advance(1.1)
    with pytest.raises(ControllerError, match="ended"):
        start(controller, clock)
    assert transport.commands == []


def test_late_heartbeat_stops_even_without_background_poll(stage_rig):
    controller, _, clock = stage_rig
    start(controller, clock)
    clock.advance(1.1)
    with pytest.raises(ControllerError, match="timed out"):
        controller.keep_hold_alive("bottom", FIRST)
    assert status(controller)["mode"] == "idle"


def test_firmware_watchdog_is_independent_of_controller_polling(stage_rig):
    _, _, clock = stage_rig
    transport = DemoTransport(clock)
    transport.command("dir ccw")
    transport.command("speed 50")
    transport.command("hold")
    clock.advance(8)
    with pytest.raises(CommandRejected, match="ended"):
        transport.command("keepalive")
    state = transport.status()["bottom"]
    assert state.mode == "idle"
    assert state.position == -50


@pytest.mark.parametrize("finish", ["stop_all", "disconnect"])
def test_stop_all_and_disconnect_cancel_holds(stage_rig, finish):
    controller, transport, clock = stage_rig
    start(controller, clock)
    start(controller, clock, "top", hold_id=SECOND)
    getattr(controller, finish)()
    assert all(item.mode == "idle" for item in transport.motors.values())
    with pytest.raises(ControllerError):
        controller.keep_hold_alive("bottom", FIRST)
    controller.release_hold("bottom", FIRST)  # Repeated releases are harmless.


def test_hold_respects_busy_state_and_cooldown(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 50, 50)
    with pytest.raises(ControllerError, match="still moving"):
        start(controller, clock)
    clock.advance(1)
    controller.poll()
    with pytest.raises(ControllerError, match="cooldown"):
        start(controller, clock)
    clock.advance(1)
    start(controller, clock)
    with pytest.raises(ControllerError, match="still moving"):
        controller.move("bottom", "cw", 50, 50)


def test_old_firmware_does_not_fall_back_to_unbounded_rotation(stage_rig, monkeypatch):
    controller, transport, clock = stage_rig
    original = transport.command

    def old_firmware(command):
        if command == "hold":
            raise CommandRejected("Unknown command")
        original(command)

    monkeypatch.setattr(transport, "command", old_firmware)
    with pytest.raises(ControllerError, match="updated three-motor-demo"):
        start(controller, clock)
    assert "start" not in transport.commands
    assert status(controller)["mode"] == "idle"


@pytest.mark.parametrize("speed", [0, 1001, True, 1.5, "50", None])
def test_invalid_hold_speed_never_sends_commands(stage_rig, speed):
    controller, transport, clock = stage_rig
    with pytest.raises(ValueError):
        start(controller, clock, speed=speed)
    assert transport.commands == []
