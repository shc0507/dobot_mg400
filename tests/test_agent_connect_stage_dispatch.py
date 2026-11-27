"""Forward the prepared stage reference through the final native dispatch boundary."""

import copy

import pytest
from test_agent_connect_actions import PAGE, Context, run
from test_agent_connect_actions import rig as rig
from test_agent_connect_live_actions import live as live

from orchestrator.agent_connect.action_backend import _GuardedTransfer


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("stage.coordinates", {"target": {"x": 1, "y": 2}}),
        ("stage.position", {"motor": "top", "position": 1}),
        ("stage.jog", {"motor": "top", "direction": "ccw"}),
        (
            "stage.step",
            {"motor": "top", "direction": "ccw", "rotations": 0.2, "speed": 0.1},
        ),
    ],
)
def test_stage_actions_forward_the_prepared_native_reference(rig, action, params):
    backend, console = rig
    result = run(backend, action, params)
    assert result["status"] == "succeeded"
    assert console.moves
    assert all(move["expected_instance"] == "stage-process-a" for move in console.moves)
    assert all(move["expected_reference_revision"] == 3 for move in console.moves)


@pytest.mark.parametrize(
    "changed",
    [{"instance": "stage-process-b"}, {"reference_revision": 4}],
)
def test_native_guard_rejects_reference_change_after_last_agent_check(rig, changed):
    backend, console = rig
    original = console.command
    requests = []

    def dispatch(device, page, action, params):
        requests.append(copy.deepcopy(params))
        console.state.update(changed)
        if (
            params.get("expected_instance") != console.state["instance"]
            or params.get("expected_reference_revision") != console.state["reference_revision"]
        ):
            return {"outcome": "refused", "error": "The stage process or reference changed."}
        return original(device, page, action, params)

    console.command = dispatch
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "failed"
    assert console.moves == []
    assert len(requests) == 1
    assert requests[0]["expected_instance"] == "stage-process-a"
    assert requests[0]["expected_reference_revision"] == 3


@pytest.mark.parametrize("command_id", [None, "", " ", 3])
@pytest.mark.parametrize("identified", [False, True])
def test_unidentified_stage_acceptance_remains_unknown_after_reaching_the_target(
    live, command_id, identified
):
    backend, console, _ = live
    if identified:
        console.state.update(instance="stage-a", reference_revision=3)
    command = console.command

    def unidentified(*args):
        result = command(*args)
        return {**result, "command_id": command_id}

    console.command = unidentified
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "unknown"
    assert "no command completion ID" in result["error"]
    assert console.state["motors"][0]["position_rotations"] == 2
    assert len(console.moves) == 1
    assert console.stops == 1


def test_guarded_transfer_cannot_replace_original_plan_reference(rig):
    backend, console = rig
    context = Context()
    plan = backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    backend._take("stage", PAGE, context)
    adapter = _GuardedTransfer(backend, plan, context)
    result = adapter.position(
        PAGE,
        motor="top",
        position=1,
        speed=0.25,
        stop_epoch=console.state["stop_epoch"],
        expected_instance="replacement-process",
        expected_reference_revision=9,
    )
    assert result["outcome"] == "accepted"
    assert console.moves[0]["expected_instance"] == "stage-process-a"
    assert console.moves[0]["expected_reference_revision"] == 3


def test_legacy_stage_dispatch_omits_unknown_native_reference(live):
    backend, console, _ = live
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "succeeded"
    assert len(console.moves) == 2
    assert all("expected_instance" not in move for move in console.moves)
    assert all("expected_reference_revision" not in move for move in console.moves)


def test_legacy_plan_does_not_adopt_reference_fields_from_the_caller(live):
    backend, console, _ = live
    context = Context()
    plan = backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    backend._take("stage", PAGE, context)
    adapter = _GuardedTransfer(backend, plan, context)
    result = adapter.position(
        PAGE,
        motor="top",
        position=1,
        speed=0.25,
        stop_epoch=console.state["stop_epoch"],
        expected_instance="unverified-process",
        expected_reference_revision=9,
    )
    assert result["outcome"] == "accepted"
    assert "expected_instance" not in console.moves[0]
    assert "expected_reference_revision" not in console.moves[0]
