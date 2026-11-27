"""Stage decisions retain the references observed before model inference."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_agent_connect_actions import PAGE, StageConsole, named_profile
from test_agent_connect_chat import rig as rig
from test_agent_connect_chat import run

from labkit.errors import ServiceError
from orchestrator.agent_connect.action_backend import create_backend
from orchestrator.agent_connect.actions import SPECS
from orchestrator.agent_connect.chat_preconditions import protected_action
from orchestrator.agent_connect.command_store import canonical


def definition(action):
    item = {
        key: copy.deepcopy(SPECS[action][key])
        for key in ("id", "label", "description", "params", "defaults", "device", "revision")
    }
    value = {"version": 1, "actions": [item]}
    identity = "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()
    return {**value, "contract_id": identity, "actions": [{**item, "available": True}]}


@pytest.fixture
def stage_rig(rig):
    agent, control, reader, model = rig
    control.catalog = definition("stage.position")
    control.state["command_contract_id"] = control.catalog["contract_id"]
    reference = {
        "instance": "stage-process-a",
        "reference_revision": 3,
        "reference_source": "reference_revision",
        "stop_epoch": 2,
        "limits_digest": "a" * 64,
    }
    choices = {
        "stage_context": {
            "version": 1,
            "available": True,
            "reason": "",
            "preconditions": {
                "stage_reference": copy.deepcopy(reference),
                "profile_id": "profile-a",
                "profile_digest": "b" * 64,
            },
        }
    }
    read_commands = control.read_commands
    control.read_commands = lambda source, action: (
        copy.deepcopy(choices) if action == "commands/choices" else read_commands(source, action)
    )
    read = reader.read

    def state(source):
        value = read(source)
        value["state"]["devices"]["stage"] = {
            "quality": "valid",
            "values": {"stage_reference": copy.deepcopy(reference)},
        }
        return value

    reader.read = state
    model.decision.update(
        action="stage.position", params={"motor": "top", "position": 1}, message="Move X to 1."
    )
    yield agent, control, reader, model, choices, reference


def test_stage_command_sends_original_reference_and_profile(stage_rig):
    agent, control, _, _, choices, _ = stage_rig
    expected = copy.deepcopy(choices["stage_context"]["preconditions"])
    run(agent, "Move X to 1 rotation")
    assert control.sent[0][2]["preconditions"] == expected
    assert agent.current.stage_preconditions == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance", "stage-process-b"),
        ("reference_revision", 4),
        ("stop_epoch", 3),
        ("limits_digest", "c" * 64),
        ("profile_id", "profile-b"),
        ("profile_digest", "c" * 64),
    ],
)
def test_changed_context_during_inference_sends_no_command(stage_rig, field, value):
    agent, control, _, model, choices, _ = stage_rig
    model.gate.clear()
    agent.submit("live", "Move X to 1 rotation", "changed-context")
    assert model.called.wait(1)
    preconditions = choices["stage_context"]["preconditions"]
    (preconditions if field.startswith("profile_") else preconditions["stage_reference"])[field] = (
        value
    )
    model.gate.set()
    assert agent.current.done.wait(3)
    assert not control.sent
    assert "changed" in agent.status()["messages"][-1]["text"]


@pytest.mark.parametrize(
    "field,value", [("instance", "stage-process-old"), ("reference_revision", 2)]
)
def test_retained_frame_must_match_choices_even_when_choices_stay_constant(stage_rig, field, value):
    agent, control, _, _, _, frame_reference = stage_rig
    frame_reference[field] = value
    result = run(agent, "Move X to 1 rotation")
    assert not control.sent
    assert "changed" in result["messages"][-1]["text"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"version": 2},
        {"version": True},
        {"version": 1, "available": False, "preconditions": None},
    ],
)
def test_missing_or_unsupported_context_blocks_stage_only(stage_rig, bad):
    agent, control, _, _, choices, _ = stage_rig
    choices["stage_context"] = bad
    result = run(agent, "Move X to 1 rotation")
    assert not control.sent
    assert "stage decision context" in result["messages"][-1]["text"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance", ""),
        ("instance", " "),
        ("instance", True),
        ("instance", None),
        ("stop_epoch", True),
        ("limits_digest", "bad"),
        ("reference_revision", -1),
        ("reference_source", "unknown"),
    ],
)
def test_invalid_reference_metadata_is_not_forwarded(stage_rig, field, value):
    agent, control, _, _, choices, frame_reference = stage_rig
    choices["stage_context"]["preconditions"]["stage_reference"][field] = value
    frame_reference[field] = value
    run(agent, "Move X to 1 rotation")
    assert not control.sent


def test_legacy_context_without_explicit_instance_is_not_forwarded(stage_rig):
    agent, control, _, _, choices, frame_reference = stage_rig
    choices["stage_context"]["preconditions"]["stage_reference"].pop("instance")
    frame_reference.pop("instance")
    result = run(agent, "Move X to 1 rotation")
    assert not control.sent
    assert "stage decision context" in result["messages"][-1]["text"]


def test_model_context_cannot_replace_saved_preconditions(stage_rig):
    agent, control, _, model, choices, _ = stage_rig
    expected = copy.deepcopy(choices["stage_context"]["preconditions"])

    def interpret(context):
        context["choices"]["stage_context"]["preconditions"]["profile_id"] = "model-generated"
        return model.decision

    model.interpret = interpret
    run(agent, "Move X to 1 rotation")
    assert control.sent[0][2]["preconditions"] == expected


def test_empty_profile_and_legacy_reference_remain_supported(stage_rig):
    agent, control, _, _, choices, frame_reference = stage_rig
    frame_reference.update(
        instance=None, reference_revision=None, reference_source="limits_port_calibration"
    )
    values = choices["stage_context"]["preconditions"]
    values.update(stage_reference=copy.deepcopy(frame_reference), profile_id=None)
    run(agent, "Move X to 1 rotation")
    assert control.sent[0][2]["preconditions"] == values


@pytest.mark.parametrize("action", ["robot.jog", "stage.calibrate", "stage.clear_fault"])
def test_unavailable_stage_context_preserves_other_commands(stage_rig, action):
    agent, control, _, model, choices, _ = stage_rig
    control.catalog = definition(action)
    control.state["command_contract_id"] = control.catalog["contract_id"]
    model.decision.update(action=action, params=copy.deepcopy(SPECS[action]["defaults"]))
    choices.clear()
    run(agent)
    assert control.sent[0][2]["preconditions"] == {}


def test_question_does_not_require_stage_context(stage_rig):
    agent, control, _, model, choices, _ = stage_rig
    choices.clear()
    model.decision.update(kind="answer", action="", params={}, message="The stage uses rotations.")
    result = run(agent, "What units does the stage use?")
    assert not control.sent and result["messages"][-1]["kind"] == "answer"


def test_protected_scope_matches_existing_stage_reference_support():
    for name, spec in SPECS.items():
        assert protected_action(name) == (
            spec["device"] == "stage" and spec["route"] in {"transfer", "profile"}
        )


@pytest.mark.parametrize("change", ["profile", "reference", "instance"])
def test_server_rejects_change_after_portal_final_check_before_taking_control(
    stage_rig, tmp_path, change
):
    agent, control, _, model, choices, frame_reference = stage_rig
    console = StageConsole()
    service = SimpleNamespace(
        demo=True,
        temporary=SimpleNamespace(name=str(tmp_path)),
        console=console,
        dashboard_configured=lambda device: device == "stage",
        devices=SimpleNamespace(
            get=lambda device: {
                "phase": "ready",
                "age_s": 0.1,
                "state": copy.deepcopy(console.state),
            }
        ),
    )
    backend = create_backend(service)
    try:
        original = named_profile(backend)
        other = copy.deepcopy(original)
        other["positions"]["Pickup"]["position_rotations"]["x"] = 5
        other = backend.transfer.store.save(other)
        choices.clear()
        choices.update(backend.choices(PAGE))
        frame_reference.clear()
        frame_reference.update(choices["stage_context"]["preconditions"]["stage_reference"])
        control.catalog = definition("transfer.position")
        control.state["command_contract_id"] = control.catalog["contract_id"]
        model.decision.update(action="transfer.position", params={"name": "Pickup"})
        attempts = []

        def submit(source, action, body, *, before_dispatch=None):
            attempts.append(copy.deepcopy(body))
            if change == "profile":
                backend.transfer.store.select(other["id"])
            elif change == "instance":
                console.state["instance"] = "stage-process-b"
            else:
                console.state["reference_revision"] += 1
            try:
                backend.prepare(body["action"], body["params"], body["preconditions"], PAGE)
            except ValueError as error:
                raise ServiceError(409, str(error)) from None
            pytest.fail("Changed context must be rejected before a device lease is taken.")

        control.command = submit
        result = run(agent, "Move to Pickup")
        assert len(attempts) == 1
        assert attempts[0]["preconditions"]["profile_id"] == original["id"]
        assert "no longer current" in result["messages"][-1]["text"]
        assert console.takes == 0 and console.moves == [] and console.stops == 0
    finally:
        backend.close()
