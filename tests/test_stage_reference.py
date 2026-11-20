"""Coordinate reference identity and dispatch races use a simulated board only."""

from dataclasses import replace

import pytest

from labkit.errors import ServiceError
from stage_controller.controller import MotorController


def prepare(rig):
    controller, transport, clock = rig
    controller.calibrate("bottom", 0.25)
    clock.advance(10)
    controller.poll()
    clock.advance(1.1)
    controller.poll()
    controller.lease.acquire("agent-test")
    transport.commands.clear()
    state = controller.snapshot()
    return dict(
        motor="bottom",
        position=0.8,
        speed=0.2,
        expected_instance=state["instance"],
        expected_reference_revision=state["reference_revision"],
    )


def test_reference_and_instance_stay_stable_during_normal_motion_and_stop(stage_rig):
    args = prepare(stage_rig)
    controller, _, clock = stage_rig
    controller.command("move_to_position", args, "agent-test")
    for _ in range(3):
        clock.advance(0.1)
        controller.poll()
        state = controller.snapshot()
        assert state["reference_revision"] == args["expected_reference_revision"]
        assert state["instance"] == args["expected_instance"]
    controller.stop("observer", 0)
    assert controller.snapshot()["reference_revision"] == args["expected_reference_revision"]
    assert controller.events.instance == args["expected_instance"]


@pytest.mark.parametrize("action", ["reset_position", "clear_upper_limit", "clear_lower_limit"])
def test_reference_change_refuses_an_old_target_without_motion(stage_rig, action):
    args = prepare(stage_rig)
    controller, transport, _ = stage_rig
    getattr(controller, action)("bottom")
    assert controller.snapshot()["reference_revision"] > args["expected_reference_revision"]
    transport.commands.clear()
    with pytest.raises(ServiceError, match="reference changed"):
        controller.command("move_to_position", args, "agent-test")
    assert transport.commands == []


def test_reference_change_in_fresh_telemetry_is_checked_before_serial_motion(
    stage_rig, monkeypatch
):
    args = prepare(stage_rig)
    controller, transport, _ = stage_rig
    original = transport.status

    def changed_reference():
        statuses = original()
        statuses["bottom"] = replace(statuses["bottom"], upper_limit=2.0)
        return statuses

    monkeypatch.setattr(transport, "status", changed_reference)
    with pytest.raises(ServiceError, match="reference changed"):
        controller.command("move_to_position", args, "agent-test")
    assert transport.commands == []
    assert controller._command_reference is None


def test_reconnect_changes_reference_but_not_service_instance(stage_rig):
    controller, _, _ = stage_rig
    before = controller.snapshot()
    controller.disconnect()
    assert not controller.snapshot()["reference_valid"]
    controller.connect("DEMO")
    after = controller.snapshot()
    assert after["instance"] == before["instance"]
    assert after["reference_revision"] > before["reference_revision"]
    other = MotorController(demo=True)
    try:
        assert other.snapshot()["instance"] != after["instance"]
    finally:
        other.close()


def test_status_motor_order_does_not_change_the_reference(stage_rig, monkeypatch):
    args = prepare(stage_rig)
    controller, transport, _ = stage_rig
    original = transport.status
    monkeypatch.setattr(transport, "status", lambda: dict(reversed(list(original().items()))))
    controller.command("move_to_position", args, "agent-test")
    assert controller.snapshot()["reference_revision"] == args["expected_reference_revision"]
    assert any(command.startswith("goto ") for command in transport.commands)


def test_expected_reference_requires_both_fields_and_stop_never_needs_them(stage_rig):
    args = prepare(stage_rig)
    controller, _, _ = stage_rig
    args.pop("expected_instance")
    with pytest.raises(ValueError, match="both"):
        controller.command("move_to_position", args, "agent-test")
    assert controller.stop("observer", 0) == "confirmed"


def test_recalibration_invalidates_reference_until_completion(stage_rig):
    args = prepare(stage_rig)
    controller, _, clock = stage_rig
    controller.calibrate("bottom", 0.25)
    during = controller.snapshot()
    assert during["reference_revision"] > args["expected_reference_revision"]
    assert not during["motors"][0]["reference_valid"]
    clock.advance(10)
    controller.poll()
    assert controller.snapshot()["reference_revision"] > during["reference_revision"]
