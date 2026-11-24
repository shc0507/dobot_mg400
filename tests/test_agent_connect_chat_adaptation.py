"""The adapted local agent uses reviewed guides and rejects changed action semantics."""

import copy
import hashlib
from pathlib import Path

import pytest
from test_agent_connect_chat import catalog, run
from test_agent_connect_chat import rig as rig

from labkit.errors import ServiceError
from orchestrator.agent_connect.chat_adaptation import REVIEWED_DOCUMENTS, ConsoleAdaptation
from orchestrator.agent_connect.command_store import canonical


def resign(value):
    definition = {
        "version": 1,
        "actions": [
            {k: v for k, v in a.items() if k not in {"available", "reason"}}
            for a in value["actions"]
        ],
    }
    value["contract_id"] = "sha256:" + hashlib.sha256(canonical(definition).encode()).hexdigest()


def test_reviewed_guides_become_separate_operating_context(rig):
    agent, _, _, model = rig
    run(agent)
    context = model.calls[0]
    assert "Stage positions use measured shaft rotations" in context["project_knowledge"]
    assert "robot flange poses" in context["project_knowledge"]
    assert "Project knowledge" not in str(context["state"])
    assert agent.status()["adaptation"]["status"] == "ready"
    assert len(agent.status()["adaptation"]["documents"]) == 3


@pytest.mark.parametrize("change", ["new_action", "revision", "unit", "defaults"])
def test_changed_action_meaning_requires_adaptation_before_model_or_motion(rig, change):
    agent, control, _, model = rig
    original = copy.deepcopy(control.catalog)
    action = control.catalog["actions"][0]
    if change == "new_action":
        action["id"] = "new.motion"
    elif change == "revision":
        action["revision"] += 1
    elif change == "unit":
        action["params"]["properties"]["step"]["description"] = "radians"
    else:
        action["defaults"]["speed"] = 90
    resign(control.catalog)
    run(agent)
    assert not control.sent and not model.calls
    assert agent.status()["adaptation"]["status"] == "update_required"
    control.catalog = original
    run(agent, request_id="reviewed-again")
    assert agent.status()["adaptation"]["status"] == "ready"


def test_availability_and_labels_can_change_without_adapting_coordinates():
    definition = catalog()
    definition["actions"][0].update(label="Jog arm", available=False, reason="Disconnected")
    assert ConsoleAdaptation().context(definition)


def test_changed_local_guide_needs_review_and_stop_remains_available(rig, tmp_path):
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "docs/agent-connect"
    target.mkdir(parents=True)
    for name in REVIEWED_DOCUMENTS:
        (target / name).write_text((root / "docs/agent-connect" / name).read_text(encoding="utf-8"))
    with (target / "project-knowledge.md").open("a") as output:
        output.write("\nChanged coordinate meaning.\n")
    adaptation = ConsoleAdaptation(tmp_path)
    with pytest.raises(ServiceError, match="integration guides changed"):
        adaptation.context(catalog())
    agent, control, _, model = rig
    agent.adaptation = adaptation
    run(agent)
    assert not model.calls and not control.sent
    agent.submit("live", "Stop stage", "independent-stop")
    assert control.stopped[0][1]["target"] == "stage"


def test_missing_guides_do_not_crash_portal_or_invent_context(tmp_path):
    adapter = ConsoleAdaptation(tmp_path)
    assert adapter.public()["status"] == "update_required"
    assert adapter.public()["project_knowledge"]["digest"] is None
    assert adapter.knowledge == ""
