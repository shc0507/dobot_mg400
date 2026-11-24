"""Model parsing and transport checks without a paid API or device connection."""

import copy
import io
import json
import threading
import urllib.error

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect import chat_model
from orchestrator.agent_connect.chat_model import API_URL, DEFAULT_MODEL, OpenAIInterpreter


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def response(kind="command", action="stage.position", params=None, **changes):
    decision = {
        "kind": kind,
        "message": "Move Top to 2 rotations.",
        "action": action,
        "params_json": json.dumps({"motor": "top", "position": 2} if params is None else params),
    }
    decision.update(changes)
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(decision)}],
            }
        ],
    }


def interpreter(value):
    calls = []

    def transport(payload, *, api_key):
        calls.append((copy.deepcopy(payload), api_key))
        return value

    return OpenAIInterpreter(transport, api_key="secret-test-key"), calls


def test_missing_key_never_calls_transport():
    calls = []
    model = OpenAIInterpreter(lambda *args, **kwargs: calls.append(args))
    assert model.status() == {"configured": False, "model": DEFAULT_MODEL}
    with pytest.raises(ServiceError, match="API key") as error:
        model.interpret({"user_message": "Move Top to 2 rotations."})
    assert error.value.status == 409 and calls == []


def test_key_stays_in_memory_and_blank_config_keeps_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")
    calls = []

    def transport(payload, *, api_key):
        calls.append(api_key)
        return response("answer", "", {}, message="The stage reading is unavailable.")

    model = OpenAIInterpreter(transport)
    assert model.configure("", "example-model") == {"configured": True, "model": "example-model"}
    model.interpret({"user_message": "Where is the stage?"})
    assert calls == ["env-secret"]
    assert "env-secret" not in json.dumps(model.status())
    assert OpenAIInterpreter(transport, api_key="").status()["configured"] is False


@pytest.mark.parametrize(
    "key,model_name", [(None, "x"), ("key\n", "x"), ("key", ""), ("key", "a/b")]
)
def test_invalid_config_preserves_previous_values(key, model_name):
    model, _ = interpreter(response())
    with pytest.raises(ServiceError):
        model.configure(key, model_name)
    assert model.status() == {"configured": True, "model": DEFAULT_MODEL}


def test_structured_request_keeps_context_and_returns_typed_parameters():
    model, calls = interpreter(response())
    context = {
        "user_message": "Move X to 2 rotations.",
        "conversation": [],
        "state": {"stage": {"x": 1.5, "unit": "rotation"}},
        "catalog": {"actions": [{"id": "stage.position", "available": True}]},
    }
    assert model.interpret(context) == {
        "kind": "command",
        "message": "Move Top to 2 rotations.",
        "action": "stage.position",
        "params": {"motor": "top", "position": 2},
    }
    payload, key = calls[0]
    assert len(calls) == 1 and key == "secret-test-key"
    assert payload["model"] == DEFAULT_MODEL and payload["store"] is False
    assert payload["input"][0]["role"] == "developer"
    assert json.loads(payload["input"][1]["content"]) == context
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["type"] == "json_schema"
    assert "secret-test-key" not in json.dumps(payload)
    assert "tools" not in payload


@pytest.mark.parametrize("kind", ["answer", "clarify"])
def test_answers_and_clarifications_have_no_action(kind):
    model, _ = interpreter(response(kind, "", {}, message="Which stage axis do you mean?"))
    assert model.interpret({}) == {
        "kind": kind,
        "message": "Which stage axis do you mean?",
        "action": "",
        "params": {},
    }


def test_stop_is_returned_for_the_executor_without_sending_it():
    model, calls = interpreter(response("stop", "stop", {"target": "robot"}))
    assert model.interpret({})["params"] == {"target": "robot"}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"status": "incomplete", "output": response()["output"]},
        {"status": "completed", "output": []},
        {"status": "completed", "output": response()["output"] * 2},
        {"status": "completed", "output": [{"type": "function_call"}]},
        {"status": "completed", "output": [None]},
        response(params_json="[]"),
        response(params_json='{"position": NaN}'),
        response(params_json='{"position": 1e999}'),
        response(params_json='{"position": 1, "position": 2}'),
        response(params_json='{"position":'),
        response(kind="other"),
        response(message=""),
        response(action=""),
        response(kind="answer"),
        response(kind="clarify", action="", params={"position": 2}),
        response(kind="stop", action="other", params={"target": "robot"}),
        response(kind="stop", action="stop", params={}),
        response(unexpected=True),
    ],
)
def test_incomplete_malformed_or_multiple_decisions_never_escape(value):
    model, calls = interpreter(value)
    with pytest.raises(ServiceError, match="one valid decision") as error:
        model.interpret({})
    assert error.value.status == 502 and len(calls) == 1


def test_refusal_is_not_an_executable_decision():
    value = response()
    value["output"][0]["content"] = [{"type": "refusal", "refusal": "No."}]
    model, _ = interpreter(value)
    with pytest.raises(ServiceError, match="one valid decision"):
        model.interpret({})


def test_reasoning_item_is_not_mistaken_for_an_extra_decision():
    value = response()
    value["output"].insert(0, {"type": "reasoning", "summary": []})
    model, _ = interpreter(value)
    assert model.interpret({})["kind"] == "command"


@pytest.mark.parametrize("context", [None, [], {"bad": float("nan")}, {"bad": "x" * 262144}])
def test_invalid_or_oversized_context_does_not_call_openai(context):
    model, calls = interpreter(response())
    with pytest.raises(ServiceError, match="context"):
        model.interpret(context)
    assert calls == []


def test_fixed_transport_has_no_retry_and_hides_http_error_body(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            raise urllib.error.HTTPError(
                API_URL, 401, "secret-test-key", {}, io.BytesIO(b"secret-test-key")
            )

    monkeypatch.setattr(chat_model.urllib.request, "build_opener", lambda *args: Opener())
    model = OpenAIInterpreter(api_key="secret-test-key")
    with pytest.raises(ServiceError, match="rejected the API key") as error:
        model.interpret({})
    assert "secret-test-key" not in str(error.value)
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == API_URL and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer secret-test-key"
    assert timeout == chat_model.TIMEOUT_S


def test_fixed_transport_caps_response_size(monkeypatch):
    reads = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, amount):
            reads.append(amount)
            return b"x" * amount

    class Opener:
        def open(self, request, timeout):
            return Reply()

    monkeypatch.setattr(chat_model.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ServiceError, match="too large"):
        OpenAIInterpreter(api_key="secret-test-key").interpret({})
    assert reads == [chat_model.MAX_RESPONSE_BYTES + 1]


def test_config_does_not_wait_for_a_model_request():
    started, finish = threading.Event(), threading.Event()
    observed, errors = [], []

    def transport(payload, *, api_key):
        observed.append((payload["model"], api_key))
        started.set()
        assert finish.wait(2)
        return response()

    model = OpenAIInterpreter(transport, api_key="first-key")

    def run():
        try:
            model.interpret({})
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(1)
        assert model.configure("second-key", "another-model")["model"] == "another-model"
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert observed == [(DEFAULT_MODEL, "first-key")]
