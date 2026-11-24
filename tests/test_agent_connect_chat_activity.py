"""Chat activity reports dispatch and results without claiming unverified success."""

import threading
from datetime import datetime

import pytest
from test_agent_connect_chat import rig as rig
from test_agent_connect_chat import run

from labkit.errors import ServiceError
from orchestrator.agent_connect.chat import ChatJob


def phases(status):
    return [event["phase"] for event in status["activity"]]


def test_activity_is_visible_while_model_is_waiting(rig):
    agent, control, _, model = rig
    model.gate.clear()
    try:
        agent.submit("live", "Move robot Z up by 1.25 mm at 1%", "model-wait")
        assert model.called.wait(2)
        status = agent.status("live")
        assert status["busy"]
        assert phases(status) == ["reading", "interpreting"]
        assert not control.sent
        assert {event["request_id"] for event in status["activity"]} == {"model-wait"}
        assert len({event["id"] for event in status["activity"]}) == 2
        for event in status["activity"]:
            assert datetime.fromisoformat(event["at"]).utcoffset().total_seconds() == 0
            assert event["text"]
    finally:
        model.gate.set()
    assert agent.current.done.wait(2)


def test_sending_activity_exposes_exact_request_while_transport_is_waiting(rig, monkeypatch):
    agent, control, _, model = rig
    entered, release = threading.Event(), threading.Event()
    command = control.command

    def waiting_command(*args, **kwargs):
        result = command(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(control, "command", waiting_command)
    try:
        agent.submit("live", "Move robot Z up by 1.25 mm at 1%", "transport-wait")
        assert entered.wait(2)
        status = agent.status("live")
        assert status["busy"]
        assert phases(status) == ["reading", "interpreting", "validating", "sending"]
        sent = status["activity"][-1]
        assert sent["command"] == control.sent[0][2]
        assert sent["command"] == {
            "request_id": "transport-wait",
            "boot_id": "command-boot",
            "control_epoch": "epoch-1",
            "contract_id": control.catalog["contract_id"],
            "action": "robot.jog",
            "params": model.decision["params"],
            "preconditions": {},
        }
        assert "result" not in sent
        assert status["active"]["status"] == "submitting"
    finally:
        release.set()
    assert agent.current.done.wait(2)
    final = agent.status("live")
    assert phases(final)[-1] == "completed"
    assert final["activity"][-1]["result"] == {**control.sent[0][2], "status": "succeeded"}
    assert not final["busy"]


def test_missing_operator_grant_never_reports_sending(rig):
    agent, control, _, _ = rig
    control.state["operator"] = None
    result = run(agent)
    assert phases(result) == ["reading", "interpreting", "validating", "failed"]
    assert "Grant operator control" in result["activity"][-1]["text"]
    assert not control.sent


def test_cancel_during_inference_has_no_late_sending_event(rig):
    agent, control, _, model = rig
    model.gate.clear()
    try:
        agent.submit("live", "Move robot Z up by 1.25 mm at 1%", "cancel-wait")
        assert model.called.wait(2)
        pending = agent.cancel("live")
        assert phases(pending)[-1] == "cancelling"
        assert "sending" not in phases(pending)
    finally:
        model.gate.set()
    assert agent.current.done.wait(2)
    final = agent.status("live")
    assert phases(final)[-1] == "cancelled"
    assert "sending" not in phases(final)
    assert not control.sent


def test_cancel_at_dispatch_boundary_has_no_sending_event(rig):
    agent, control, _, _ = rig
    control.before_send = lambda: agent.invalidate("live")
    final = run(agent)
    assert phases(final) == ["reading", "interpreting", "validating", "cancelled"]
    assert not control.sent


@pytest.mark.parametrize("stage", ["submission", "polling"])
def test_unconfirmed_outcome_never_reports_completed(rig, monkeypatch, stage):
    agent, control, _, _ = rig
    read_commands, command = control.read_commands, control.command

    def unavailable_result(source, action):
        if action.startswith("commands/request/"):
            raise ServiceError(503, "Response unavailable.")
        return read_commands(source, action)

    def accepted_command(*args, **kwargs):
        return {**command(*args, **kwargs), "status": "running"}

    monkeypatch.setattr(control, "read_commands", unavailable_result)
    if stage == "submission":
        control.failure = 503
    else:
        monkeypatch.setattr(control, "command", accepted_command)
    final = run(agent)
    assert phases(final)[-1] == "failed"
    assert "completed" not in phases(final)
    assert len(control.sent) == 1
    result = final["activity"][-1]["result"]
    assert result["status"] == "unknown"
    assert result["request_id"] == "request-1"
    assert result["action"] == "robot.jog"
    assert "unconfirmed" in final["activity"][-1]["text"]


def test_executing_activity_is_visible_before_terminal_result(rig, monkeypatch):
    agent, control, _, _ = rig
    entered, release = threading.Event(), threading.Event()
    command, read_commands = control.command, control.read_commands

    def accepted_command(*args, **kwargs):
        return {**command(*args, **kwargs), "status": "running"}

    def delayed_result(source, action):
        if action.startswith("commands/request/"):
            entered.set()
            assert release.wait(3)
        return read_commands(source, action)

    monkeypatch.setattr(control, "command", accepted_command)
    monkeypatch.setattr(control, "read_commands", delayed_result)
    try:
        agent.submit("live", "Move robot Z up by 1.25 mm at 1%", "execution-wait")
        assert entered.wait(2)
        pending = agent.status("live")
        assert pending["busy"]
        assert phases(pending)[-1] == "executing"
        assert pending["activity"][-1]["result"]["status"] == "running"
        assert "completed" not in phases(pending)
    finally:
        release.set()
    assert agent.current.done.wait(2)
    final = agent.status("live")
    assert phases(final)[-2:] == ["executing", "completed"]
    assert final["activity"][-1]["result"]["status"] == "succeeded"


@pytest.mark.parametrize("kind", ["answer", "clarify"])
def test_non_command_completion_does_not_invent_a_command(rig, kind):
    agent, control, _, model = rig
    model.decision = {"kind": kind, "message": "Which axis?", "action": "", "params": {}}
    final = run(agent)
    assert phases(final) == ["reading", "interpreting", "completed"]
    assert "No command sent" in final["activity"][-1]["text"]
    assert all("command" not in event and "result" not in event for event in final["activity"])
    assert not control.sent


def test_activity_is_source_scoped_bounded_and_copied(rig):
    agent, _, _, _ = rig
    payload = {"params": {"step": 1.25}}
    for index in range(125):
        job = ChatJob("live", f"live-{index}", "Test")
        agent._activity(job, "sending", "Test event", command=payload)
    agent._activity(ChatJob("mock", "mock-1", "Test"), "reading", "Mock event")
    payload["params"]["step"] = 999
    live, mock = agent.status("live"), agent.status("mock")
    assert len(live["activity"]) == 120
    assert live["activity"][0]["request_id"] == "live-5"
    assert live["activity"][-1]["request_id"] == "live-124"
    assert all(event["command"]["params"]["step"] == 1.25 for event in live["activity"])
    assert len(mock["activity"]) == 1
    assert mock["activity"][0]["request_id"] == "mock-1"
    live["activity"][-1]["command"]["params"]["step"] = 888
    assert agent.status("live")["activity"][-1]["command"]["params"]["step"] == 1.25


def test_direct_stop_reports_its_request_without_model_or_operator_grant(rig):
    agent, control, reader, model = rig
    model.configured = False
    control.state["operator"] = None
    final = agent.submit("live", "Stop robot", "direct-stop")
    assert phases(final) == ["sending", "completed"]
    expected = {"request_id": "direct-stop", "target": "robot"}
    assert final["activity"][0]["command"] == expected
    assert control.stopped == [("live", expected)]
    assert final["activity"][-1]["result"] == {**expected, "status": "confirmed"}
    assert not model.calls and reader.calls == 0 and not control.sent
    assert not final["busy"]
