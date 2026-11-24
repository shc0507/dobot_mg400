"""Language decisions use current Console authority and never retry movement."""

import copy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from labkit.errors import ServiceError
from orchestrator.agent_connect.actions import SPECS
from orchestrator.agent_connect.chat import ChatAgent, checked_catalog
from orchestrator.agent_connect.chat_model import OpenAIInterpreter
from orchestrator.agent_connect.chat_routes import register_chat
from orchestrator.agent_connect.command_store import canonical
from orchestrator.agent_connect.portal import create_portal_app


def catalog():
    spec = SPECS["robot.jog"]
    action = {
        key: copy.deepcopy(spec[key])
        for key in ("id", "label", "description", "params", "defaults", "device", "revision")
    }
    definition = {"version": 1, "actions": [action]}
    identity = "sha256:" + hashlib.sha256(canonical(definition).encode()).hexdigest()
    return {
        "version": 1,
        "contract_id": identity,
        "actions": [{**action, "available": True, "reason": ""}],
    }


class Control:
    def __init__(self):
        self.lock = threading.RLock()
        self.catalog = catalog()
        self.state = {
            "connected": True,
            "boot_id": "command-boot",
            "control_epoch": "epoch-1",
            "command_contract_id": self.catalog["contract_id"],
            "principal": {"id": "session"},
            "operator": {"id": "session"},
        }
        self.sent, self.stopped, self.lookups = [], [], []
        self.failure = None
        self.before_send = lambda: None

    def status(self, source):
        return copy.deepcopy(self.state)

    def read_commands(self, source, action):
        if action == "commands/catalog":
            return copy.deepcopy(self.catalog)
        if action == "commands/choices":
            return {}
        self.lookups.append(action)
        return {"request_id": action.split("/")[-1], "status": "succeeded", "action": "robot.jog"}

    def command(self, source, action, data, *, before_dispatch=None):
        if (self.state.get("operator") or {}).get("id") != "session":
            raise ServiceError(403, "Grant operator control from the Console first.")
        if self.failure and self.failure < 500:
            raise ServiceError(self.failure, "The Console refused the request.")
        self.before_send()
        if before_dispatch:
            before_dispatch()
        self.sent.append((source, action, copy.deepcopy(data)))
        if self.failure:
            raise ServiceError(self.failure, "Connection response unavailable.")
        return {**data, "status": "succeeded"}

    action = command

    def stop(self, source, data):
        self.stopped.append((source, data))
        return {**data, "status": "confirmed"}


class Reader:
    def __init__(self):
        self.contract = "read-contract"
        self.calls = 0

    def read(self, source):
        self.calls += 1
        return {
            "discovery": {
                "boot_id": "different-read-boot",
                "control": {"command_boot_id": "command-boot"},
            },
            "contract_id": self.contract,
            "units": {"pose_mm_deg": "mm,mm,mm,degree"},
            "state": {"mode": source, "devices": {"robot": {"quality": "valid"}}},
        }


class Model:
    def __init__(self):
        self.decision = {
            "kind": "command",
            "message": "Move robot Z up by 1.25 mm at 1% speed.",
            "action": "robot.jog",
            "params": {"axis": "z", "direction": 1, "step": 1.25, "speed": 1},
        }
        self.called = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.configured = True
        self.calls = []

    def status(self):
        return {"configured": self.configured, "model": "test-model"}

    def configure(self, api_key, model):
        self.configured = bool(api_key)
        return self.status()

    def interpret(self, context):
        self.calls.append(context)
        self.called.set()
        assert self.gate.wait(3)
        return copy.deepcopy(self.decision)


@pytest.fixture
def rig():
    control, reader, model = Control(), Reader(), Model()
    agent = ChatAgent(control, reader, model)
    yield agent, control, reader, model
    model.gate.set()
    agent.close()
    if agent.current:
        assert agent.current.done.wait(3)


def run(agent, message="Move robot Z up by 1.25 mm at 1%", request_id="request-1"):
    agent.submit("live", message, request_id)
    assert agent.current.done.wait(3)
    return agent.status("live")


def test_clear_instruction_dispatches_without_confirmation_using_current_envelope(rig):
    agent, control, reader, model = rig
    result = run(agent)
    assert len(control.sent) == 1 and reader.calls == 2
    source, route, body = control.sent[0]
    assert source == "live" and route == "commands"
    assert body == {
        "request_id": "request-1",
        "boot_id": "command-boot",
        "control_epoch": "epoch-1",
        "contract_id": control.catalog["contract_id"],
        "action": "robot.jog",
        "params": model.decision["params"],
        "preconditions": {},
    }
    assert result["messages"][-1]["command"]["status"] == "succeeded"
    assert not result["busy"]


@pytest.mark.parametrize("kind", ["answer", "clarify"])
def test_questions_and_clarifications_do_not_dispatch(rig, kind):
    agent, control, _, model = rig
    model.decision = {"kind": kind, "message": "Which axis?", "action": "", "params": {}}
    result = run(agent, "Move a little")
    assert result["messages"][-1]["kind"] == kind
    assert not control.sent


@pytest.mark.parametrize("changes", [{"action": "shell.exec"}, {"params": {"step": -1}}])
def test_invalid_model_action_does_not_dispatch(rig, changes):
    agent, control, _, model = rig
    model.decision.update(changes)
    result = run(agent)
    assert result["messages"][-1]["kind"] == "error" and not control.sent


@pytest.mark.parametrize("change", ["boot_id", "control_epoch", "principal", "contract"])
def test_authority_or_definition_change_during_inference_discards_action(rig, change):
    agent, control, reader, model = rig
    model.gate.clear()
    agent.submit("live", "Move Z", "request-1")
    assert model.called.wait(2)
    if change == "contract":
        reader.contract = "new-read-contract"
    else:
        control.state[change] = {"id": "other"} if change == "principal" else "changed"
    model.gate.set()
    assert agent.current.done.wait(3)
    assert not control.sent
    assert agent.status()["messages"][-1]["kind"] == "error"


def test_stop_during_inference_needs_no_key_or_grant_and_prevents_late_move(rig):
    agent, control, _, model = rig
    model.gate.clear()
    agent.submit("live", "Move Z", "request-1")
    assert model.called.wait(2)
    model.configured = False
    control.state["operator"] = None
    agent.submit("live", "stop", "stop-1")
    assert control.stopped == [("live", {"request_id": "stop-1", "target": "all_motion"})]
    model.gate.set()
    assert agent.current.done.wait(3)
    assert not control.sent


def test_cancellation_checked_again_at_transport_dispatch(rig):
    agent, control, _, _ = rig
    control.before_send = lambda: agent.invalidate("live")
    run(agent)
    assert not control.sent


def test_duplicate_instruction_id_does_not_call_model_or_move_again(rig):
    agent, control, _, model = rig
    run(agent)
    agent.submit("live", "Move robot Z up by 1.25 mm at 1%", "request-1")
    assert len(model.calls) == len(control.sent) == 1
    with pytest.raises(ServiceError, match="another instruction"):
        agent.submit("live", "Move Y", "request-1")


def test_missing_grant_preserves_refusal_without_lookup_or_retry(rig):
    agent, control, _, _ = rig
    control.state["operator"] = None
    result = run(agent)
    assert "Grant operator" in result["messages"][-1]["text"]
    assert not control.sent and not control.lookups


def test_uncertain_submission_looks_up_same_id_without_resubmitting(rig):
    agent, control, _, _ = rig
    control.failure = 503
    result = run(agent)
    assert len(control.sent) == 1
    assert control.lookups == ["commands/request/request-1"]
    assert result["messages"][-1]["command"]["status"] == "succeeded"


def test_context_does_not_contain_connection_credentials(rig):
    agent, _, _, model = rig
    run(agent)
    assert set(model.calls[0]) == {
        "source",
        "user_message",
        "conversation",
        "state",
        "units",
        "read_contract_id",
        "commands",
        "choices",
        "project_knowledge",
    }


def test_lost_submission_and_lookup_keep_unknown_request_visible(rig, monkeypatch):
    agent, control, _, _ = rig
    control.failure = 503
    original = control.read_commands

    def read(source, action):
        if action.startswith("commands/request/"):
            raise ServiceError(503, "Disconnected")
        return original(source, action)

    monkeypatch.setattr(control, "read_commands", read)
    result = run(agent)
    record = result["messages"][-1]["command"]
    assert record["request_id"] == "request-1" and record["status"] == "unknown"
    assert "unconfirmed" in record["error"] and len(control.sent) == 1


def test_corrupt_catalog_is_not_used(rig):
    agent, control, _, model = rig
    control.catalog["actions"][0]["params"]["properties"]["step"]["exclusiveMinimum"] = -1
    result = run(agent)
    assert "integrity" in result["messages"][-1]["text"]
    assert not model.calls and not control.sent


def test_catalog_availability_does_not_change_definition_hash():
    value = catalog()
    value["actions"][0].update(available=False, reason="Disconnected")
    assert checked_catalog(value) == value


def test_chat_routes_return_async_status_and_manual_stop_invalidates(rig):
    _, control, reader, model = rig
    app = Flask(__name__)
    agent = register_chat(app, control, lambda path: {}, model=model, reader=reader)

    @app.post("/api/agent-control/stop")
    def native_stop():
        return {"stop": "confirmed"}

    model.gate.clear()
    http = app.test_client()
    response = http.post(
        "/api/agent-chat/message",
        json={"source": "live", "message": "Move Z", "request_id": "http-1"},
    )
    assert response.status_code == 202 and model.called.wait(2)
    assert http.get("/api/agent-chat/status").get_json()["busy"]
    assert http.post("/api/agent-control/stop", json={"source": "live"}).status_code == 200
    model.gate.set()
    assert agent.current.done.wait(3)
    assert not control.sent
    agent.close()


@pytest.mark.parametrize("granted", [True, False])
def test_full_local_chat_routes_use_existing_connection_and_model_protocol(granted):
    definition = catalog()
    sent = []
    state = {
        "phase": 8,
        "commands_enabled": True,
        "physical_commands_enabled": False,
        "furnace_control_enabled": True,
        "equipment_mode": "simulated",
        "boot_id": "command-boot",
        "control_epoch": "epoch-1",
        "command_contract_id": definition["contract_id"],
        "principal": {"id": "paired-agent", "kind": "agent"},
        "operator": {"id": "paired-agent", "kind": "agent"} if granted else None,
    }

    def transport(source, action, data=None, *, token=None):
        assert source == "mock"
        if action == "status":
            return state
        if action == "commands/catalog":
            return definition
        if action == "commands/choices":
            return {}
        if action == "commands":
            assert token == "paired-session"
            sent.append(data)
            return {**data, "status": "accepted"}
        if action.startswith("commands/request/"):
            return {**sent[0], "status": "succeeded"}
        return {}

    def model_api(payload, *, api_key):
        assert api_key == "test-model-secret"
        context = json.loads(payload["input"][-1]["content"])
        assert context["source"] == "mock" and payload["store"] is False
        decision = {
            "kind": "command",
            "message": "Move Z by 1.25 mm at 1%.",
            "action": "robot.jog",
            "params_json": json.dumps({"axis": "z", "direction": 1, "step": 1.25, "speed": 1}),
        }
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(decision)}],
                }
            ],
        }

    app = create_portal_app(
        live_transport=SimpleNamespace(read=lambda path: {}),
        control_transport=transport,
        interpreter=OpenAIInterpreter(model_api, api_key="test-model-secret"),
        chat_reader=Reader(),
    )
    control = app.extensions["agent_portal_control"]
    control.records["mock"]["session_token"] = "paired-session"
    http = app.test_client()
    response = http.post(
        "/api/agent-chat/message",
        json={
            "source": "mock",
            "message": "Move robot Z up by 1.25 mm at 1%.",
            "request_id": "integrated-1",
        },
    )
    assert response.status_code == 202
    agent = app.extensions["agent_portal_chat"]
    assert agent.current.done.wait(3)
    status = http.get("/api/agent-chat/status?source=mock").get_json()
    assert "test-model-secret" not in json.dumps(status)
    assert len(sent) == int(granted)
    if granted:
        assert status["messages"][-1]["command"]["status"] == "succeeded"
        assert sent[0]["params"]["step"] == 1.25
    else:
        assert "Grant operator control" in status["messages"][-1]["text"]
    assert (
        http.post(
            "/api/agent-chat/settings",
            json={"api_key": "secret", "model": "test"},
            headers={"Origin": "https://unrelated.example"},
        ).status_code
        == 403
    )
    agent.close()
    control.close()
