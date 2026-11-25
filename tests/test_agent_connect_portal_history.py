"""Restart recovery reads results and never replays equipment commands."""

import copy

import pytest
from test_agent_connect_chat import Control, Model, Reader, run
from test_agent_connect_model_profiles import FakeProtector, add_profile

from labkit.errors import ServiceError
from orchestrator.agent_connect.chat import ChatAgent
from orchestrator.agent_connect.chat_model import OpenAIInterpreter
from orchestrator.agent_connect.chat_payload import model_context
from orchestrator.agent_connect.model_profiles import ModelProfileStore
from orchestrator.agent_connect.portal_history import PortalHistory


def history(path):
    return PortalHistory(path, FakeProtector())


def test_history_is_encrypted_bounded_and_preserves_independent_writes(tmp_path):
    path = tmp_path / "history.db"
    first, second = history(path), history(path)
    for index in range(63):
        (first if index % 2 else second).put("message", str(index), {"text": f"private-{index}"})
    rows = history(path).rows("message")
    assert len(rows) == 60 and rows[0]["text"] == "private-3"
    assert b"private-62" not in path.read_bytes()
    assert first.put("request", "one", {"message": "first"}, once=True) is None
    assert second.put("request", "one", {"message": "second"}, once=True) == {"message": "first"}


def test_restart_restores_conversation_usage_and_deduplicates_requests(tmp_path):
    path = tmp_path / "history.db"
    first = ChatAgent(Control(), Reader(), Model(), history=history(path))
    run(first)
    first._usage(first.current, {"provider": "openai", "input_tokens": 20, "cost_usd": None})
    first.close()
    control, model = Control(), Model()
    second = ChatAgent(control, Reader(), model, history=history(path))
    try:
        assert second.status()["messages"] == first.status()["messages"]
        assert second.status()["usage"][0]["input_tokens"] == 20
        second.submit("live", "Move robot Z up by 1.25 mm at 1%", "request-1")
        assert not model.calls and not control.sent and not second.current
        with pytest.raises(ServiceError, match="another instruction"):
            second.submit("live", "Move elsewhere", "request-1")
        assert second.status("mock")["messages"] == []
    finally:
        second.close()


def test_restart_reconciles_only_by_original_request_id(tmp_path):
    path = tmp_path / "history.db"
    store = history(path)
    store.put(
        "request",
        "old-request",
        {
            "request_id": "old-request",
            "message": "Move the robot",
            "dispatched": True,
            "command": {"request_id": "old-request", "action": "robot.jog", "status": "submitting"},
        },
    )
    control, model = Control(), Model()
    agent = ChatAgent(control, Reader(), model, history=history(path))
    try:
        assert agent.status()["recovery_pending"] == ["old-request"]
        assert not control.lookups and not control.sent and not model.calls
        result = agent.reconcile("live")
        assert control.lookups == ["commands/request/old-request"]
        assert not result["recovery_pending"]
        assert result["activity"][-1]["result"]["status"] == "succeeded"
        assert not control.sent and not control.stopped and not model.calls
    finally:
        agent.close()


def test_missing_or_mismatched_recovery_result_stays_unknown(tmp_path):
    store = history(tmp_path / "history.db")
    store.put(
        "request",
        "old",
        {
            "request_id": "old",
            "message": "Move",
            "dispatched": True,
            "command": {"request_id": "old", "status": "unknown"},
        },
    )
    control = Control()
    control.read_commands = lambda *args: {"request_id": "different", "status": "succeeded"}
    agent = ChatAgent(control, Reader(), Model(), history=store)
    try:
        assert agent.reconcile("live")["recovery_pending"] == ["old"]
        assert not control.sent and not control.stopped
    finally:
        agent.close()


def test_unwritable_history_blocks_new_commands_but_never_stop(tmp_path):
    store = history(tmp_path / "history.db")
    control = Control()
    agent = ChatAgent(control, Reader(), Model(), history=store)
    store.available = False
    with pytest.raises(ServiceError, match="history"):
        agent.submit("live", "Move the robot", "move-id")
    agent.submit("live", "stop robot", "stop-id")
    assert control.stopped and not control.sent
    agent.close()


def test_corrupt_history_is_not_replaced(tmp_path):
    path = tmp_path / "history.db"
    path.write_bytes(b"unreadable-original")
    store = history(path)
    assert not store.available and path.read_bytes() == b"unreadable-original"


def test_cancellation_during_journal_write_prevents_dispatch(tmp_path):
    store = history(tmp_path / "history.db")
    agent = ChatAgent(Control(), Reader(), Model(), history=store)
    original = store.put

    def put(kind, key, value, **kwargs):
        result = original(kind, key, value, **kwargs)
        if kind == "request" and value.get("dispatched"):
            agent.current.cancelled.set()
        return result

    store.put = put
    run(agent)
    assert not agent.control.sent
    agent.close()


def test_stop_dispatch_precedes_any_history_write(tmp_path):
    store = history(tmp_path / "history.db")
    agent = ChatAgent(Control(), Reader(), Model(), history=store)
    original = store.put

    def put(*args, **kwargs):
        assert agent.control.stopped
        return original(*args, **kwargs)

    store.put = put
    agent.submit("live", "stop robot", "stop-with-disk")
    assert agent.control.stopped[0][1]["target"] == "robot"
    agent.close()


def test_cancel_event_and_usage_callback_reach_the_interpreter(tmp_path):
    class Cancellable(Model):
        def interpret(self, context, *, cancelled=None, on_usage=None):
            self.called.set()
            assert cancelled.wait(2)
            on_usage(
                {
                    "provider": "openai",
                    "model": "test",
                    "input_tokens": None,
                    "output_tokens": None,
                    "status": "cancelled",
                    "elapsed_ms": 50,
                }
            )
            raise ServiceError(409, "Model request cancelled.")

    model, control = Cancellable(), Control()
    agent = ChatAgent(control, Reader(), model, history=history(tmp_path / "history.db"))
    agent.submit("live", "Move the robot", "cancel-id")
    assert model.called.wait(2)
    agent.cancel("live")
    assert agent.current.done.wait(2)
    assert agent.status()["usage"][-1]["status"] == "cancelled"
    assert not control.sent
    agent.close()


def test_external_profile_deactivation_clears_cached_model(tmp_path):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    identifier = add_profile(store)["profiles"][0]["id"]
    store.activate(identifier)
    model = OpenAIInterpreter(api_key="")
    agent = ChatAgent(Control(), Reader(), model, profiles=store)
    assert agent.status()["model"]["configured"]
    ModelProfileStore(path, FakeProtector()).activate(None)
    assert not agent.status()["model"]["configured"]
    agent.close()


def test_app_initialization_defers_restore_until_server_can_listen(tmp_path):
    from test_agent_connect_portal_continuity import Protector, Source, pair

    from orchestrator.agent_connect.control_portal import PortalControl
    from orchestrator.agent_connect.portal import create_portal_app
    from orchestrator.agent_connect.portal_store import PortalPairingStore

    path, source = tmp_path / "pairing.json", Source()
    first = PortalControl(source, pairing_store=PortalPairingStore(path, Protector()))
    pair(first, source)
    first.close()
    source.calls.clear()
    app = create_portal_app(
        control_transport=source,
        interpreter=Model(),
        pairing_store=PortalPairingStore(path, Protector()),
    )
    try:
        assert not source.calls
        assert app.extensions["agent_portal_control"].continuity.thread is None
    finally:
        app.extensions["agent_portal_chat"].close()
        app.extensions["agent_portal_control"].close()
        source.authority.close()


def test_failed_app_initialization_releases_pairing_owner(tmp_path, monkeypatch):
    from test_agent_connect_portal_continuity import Protector, Source

    from orchestrator.agent_connect import chat_routes
    from orchestrator.agent_connect.portal import create_portal_app
    from orchestrator.agent_connect.portal_store import PortalPairingStore

    source, path = Source(), tmp_path / "pairing.json"
    store = PortalPairingStore(path, Protector())

    def fail(*args, **kwargs):
        raise ValueError("Initialization failed")

    monkeypatch.setattr(chat_routes, "register_chat", fail)
    with pytest.raises(ValueError, match="Initialization failed"):
        create_portal_app(control_transport=source, interpreter=Model(), pairing_store=store)
    second = PortalPairingStore(path, Protector())
    try:
        assert second.status()["storage_available"]
        assert not source.calls
    finally:
        second.close()
        source.authority.close()


@pytest.mark.parametrize(
    "message,scope",
    [
        ("Move the X stage to 2 rotations", "stage"),
        ("What is the robot position?", "robot"),
        ("Move that stage again", None),
        ("Move the robot and stage", None),
        ("Where is the chip on the stage?", None),
        ("What are all current readings?", None),
        ("How does the stage position compare with MG400?", None),
    ],
)
def test_context_scope_keeps_rules_qualities_and_full_context_when_unclear(message, scope):
    context = {
        "user_message": message,
        "state": {
            "devices": {
                "stage": {"quality": "valid", "values": {"fault": "limit"}},
                "robot": {"quality": "unknown"},
            },
            "operations": {"active": "test"},
        },
        "commands": [
            {"id": "stage.position", "device": "stage"},
            {"id": "robot.jog", "device": "robot"},
        ],
        "project_knowledge": "All operating rules",
        "units": {"stage": "rotation"},
        "choices": {"limits": [0, 2]},
        "conversation": [],
    }
    original = copy.deepcopy(context)
    payload = model_context(context)
    assert context == original
    assert set(payload["state"]["devices"]) == ({scope} if scope else {"stage", "robot"})
    for key in ("project_knowledge", "units", "choices", "conversation"):
        assert payload[key] == context[key]
    assert payload["state"]["operations"] == context["state"]["operations"]
