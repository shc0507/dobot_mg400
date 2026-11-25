"""Check the Token Station stream with fake HTTP responses and test credentials."""

import copy
import http.client
import io
import json
import threading

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect import chat_completions_stream, chat_model
from orchestrator.agent_connect.chat_model import DECISION_SCHEMA, OpenAIInterpreter
from orchestrator.agent_connect.model_profiles import TOKEN_STATION_BASE_URL


def decision(**changes):
    return {
        "kind": "command",
        "message": "Move Top to 2 rotations.",
        "action": "stage.position",
        "params_json": '{"motor":"top","position":2}',
    } | changes


def chunk(content=None, *, finish=None, delta=None, index=0, **changes):
    return {
        "id": "test-completion",
        "choices": [
            {
                "index": index,
                "delta": {"content": content} if delta is None else delta,
                "finish_reason": finish,
            }
        ],
    } | changes


def stream(*chunks, done=True):
    events = ["data: " + json.dumps(item, ensure_ascii=False) for item in chunks]
    if done:
        events.append("data: [DONE]")
    return ("\n\n".join(events) + "\n\n").encode()


def profile(**changes):
    return {
        "id": "gateway-test",
        "name": "Gateway test",
        "route": "token_station",
        "provider": "openai",
        "model": "openai/gpt-6.1-sol",
        "api_key": "test-gateway-key",
    } | changes


def install_http(monkeypatch, raw, failure=None):
    calls, reads, closed = [], [], []

    class Reply:
        def __init__(self):
            self.body = io.BytesIO(raw)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def read1(self, amount):
            reads.append(amount)
            if failure:
                raise failure
            return self.body.read(amount)

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Reply()

    monkeypatch.setattr(chat_model.urllib.request, "build_opener", lambda *args: Opener())
    return calls, reads, closed


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_gateway_uses_chat_completions_with_namespaced_model(monkeypatch, provider):
    text = json.dumps(decision())
    calls, reads, closed = install_http(
        monkeypatch,
        stream(chunk(text[:30]), chunk(text[30:]), chunk(finish="stop")),
    )
    model = OpenAIInterpreter(api_key="previous-test-key")
    model.configure_profile(profile(provider=provider))
    context = {"user_message": "Move Top to 2 rotations.", "project_knowledge": "Lab context."}
    original = copy.deepcopy(context)
    result = model.interpret(context)
    assert result["params"] == {"motor": "top", "position": 2}
    assert context == original
    assert len(calls) == 1 and reads == [4096] and closed == [True]
    request, timeout = calls[0]
    assert request.method == "POST" and timeout == chat_model.TIMEOUT_S
    assert request.full_url == TOKEN_STATION_BASE_URL + "/chat/completions"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer test-gateway-key"
    assert headers["accept"] == "text/event-stream"
    assert "x-api-key" not in headers and "x-goog-api-key" not in headers
    payload = json.loads(request.data)
    assert set(payload) == {"model", "messages", "stream", "stream_options"}
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["model"] == "openai/gpt-6.1-sol" and payload["stream"] is True
    instructions, user = payload["messages"]
    assert instructions["role"] == "system" and user["role"] == "user"
    assert "Lab context." in instructions["content"]
    assert chat_model.INSTRUCTIONS in instructions["content"]
    assert json.loads(instructions["content"].split("exact JSON schema:\n")[1]) == DECISION_SCHEMA
    assert json.loads(user["content"]) == {"user_message": context["user_message"]}
    assert "test-gateway-key" not in request.full_url + request.data.decode()
    assert model.status()["route"] == "token_station"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_gateway_parses_sse_fields_and_ignores_private_reasoning(monkeypatch, newline):
    expected = decision(message="Move Top to 2 rotations. 移动。")
    content = json.dumps(expected, ensure_ascii=False)
    private = json.dumps(decision(action="must-never-escape"))
    first = json.dumps(chunk(delta={"role": "assistant", "content": None, "reasoning": private}))
    raw = (
        (
            "\ufeff: comment\nid: ignored\nretry: 999999\nevent: message\ndata: "
            + first[: first.index('"choices"')]
            + "\ndata: "
            + first[first.index('"choices"') :]
            + "\n\n"
            + stream(
                chunk(delta={"reasoning_content": private, "thinking": private}),
                chunk(content, finish="stop"),
                {"choices": [], "usage": {"total_tokens": 1}},
            ).decode()
        )
        .replace("\n", newline)
        .encode()
    )
    install_http(monkeypatch, raw)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    assert model.interpret({})["action"] == "stage.position"


def invalid_streams():
    text = json.dumps(decision())
    good = chunk(text, finish="stop")
    yield "missing-done", stream(good, done=False)
    yield "missing-stop", stream(chunk(text))
    yield "no-content", stream(chunk(finish="stop"))
    yield "no-final-separator", stream(good).rstrip() + b"\n"
    yield "post-stop-choice", stream(good, chunk())
    yield "different-response-id", stream(chunk(text), chunk(finish="stop", id="other"))
    yield "invalid-utf8", b"data: \xff\n\n" + stream(good)
    yield "oversized", b":" + b"x" * chat_model.MAX_RESPONSE_BYTES
    yield "json-body", json.dumps(good).encode()
    yield "event-error", b"event: error\ndata: {}\n\n" + stream(good)
    yield "empty-event-error", b"event: error\n\n" + stream(good)
    yield "error-after-stop", stream(good, {"error": {"message": "test-gateway-key"}})
    yield "empty-error-object", stream({"error": {}, "choices": good["choices"]})
    yield "second-choice", stream(good | {"choices": good["choices"] * 2})
    yield "nonzero-choice", stream(chunk(text, finish="stop", index=1))
    yield "bool-choice", stream(chunk(text, finish="stop", index=False))
    yield "duplicate-json-field", b'data: {"choices":[],"choices":[]}\n\ndata: [DONE]\n\n'
    for reason in ["length", "content_filter", "tool_calls", "function_call"]:
        yield reason, stream(chunk(text, finish=reason))
    for field in ["refusal", "tool_calls", "function_call", "audio"]:
        yield field, stream(chunk(text), chunk(delta={field: "private"}, finish="stop"))
    for delta in [{"content": []}, {"role": "user", "content": text}]:
        yield "invalid-delta", stream(chunk(delta=delta, finish="stop"))


@pytest.mark.parametrize(
    "raw", [value for _, value in invalid_streams()], ids=[name for name, _ in invalid_streams()]
)
def test_invalid_gateway_stream_never_returns_a_decision(monkeypatch, raw):
    calls, _, closed = install_http(monkeypatch, raw)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    with pytest.raises(ServiceError) as error:
        model.interpret({})
    assert error.value.status == 502 and len(calls) == 1 and closed == [True]
    assert "test-gateway-key" not in str(error.value)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "{",
        json.dumps(decision()) + json.dumps(decision()),
        json.dumps(decision(extra=True)),
        json.dumps(decision(kind="answer")),
        json.dumps(decision(params_json='{"x":1,"x":2}')),
        json.dumps(decision(params_json='{"x":NaN}')),
        json.dumps(decision(params_json='{"x":1e999}')),
        json.dumps(decision(kind="stop", action="stop", params_json='{"target":"unknown"}')),
    ],
)
def test_gateway_uses_common_strict_decision_validation(monkeypatch, text):
    install_http(monkeypatch, stream(chunk(text, finish="stop")))
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    with pytest.raises(ServiceError, match="one valid decision"):
        model.interpret({})


@pytest.mark.parametrize("kind", ["answer", "clarify", "stop"])
def test_gateway_returns_non_motion_decisions(monkeypatch, kind):
    value = decision(
        kind=kind,
        action="stop" if kind == "stop" else "",
        params_json='{"target":"all_motion"}' if kind == "stop" else "{}",
    )
    install_http(monkeypatch, stream(chunk(json.dumps(value), finish="stop")))
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    assert model.interpret({})["kind"] == kind


def test_gateway_read_failure_does_not_return_partial_content(monkeypatch):
    raw = stream(chunk(json.dumps(decision()), finish="stop"))
    calls, _, closed = install_http(monkeypatch, raw, http.client.IncompleteRead(raw))
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    with pytest.raises(ServiceError, match="No command was sent"):
        model.interpret({})
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize("part_size", [1, 7, 4096])
def test_stream_finishes_at_done_without_reading_eof(part_size):
    raw = stream(chunk(json.dumps(decision(message="移动。"), ensure_ascii=False), finish="stop"))

    class PersistentReply:
        def __init__(self):
            self.body = io.BytesIO(raw)

        def read1(self, amount):
            value = self.body.read(min(part_size, amount))
            assert value, "The stream must not wait for EOF after DONE."
            return value

    response = chat_completions_stream.read_stream(
        PersistentReply(), loads=chat_model._loads, max_bytes=len(raw), timeout_s=45
    )
    assert chat_model._decision(response, route="token_station")["message"] == "移动。"


def test_stream_deadline_rejects_data_after_the_deadline(monkeypatch):
    times = iter([0, 0, 46])
    monkeypatch.setattr(chat_completions_stream.time, "monotonic", lambda: next(times))
    raw = stream(chunk(json.dumps(decision()), finish="stop"))
    calls, _, closed = install_http(monkeypatch, raw)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile())
    with pytest.raises(ServiceError, match="No command was sent"):
        model.interpret({})
    assert len(calls) == 1 and closed == [True]


def test_legacy_config_does_not_reuse_gateway_key():
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile(base_url="https://api.openai.com/v1"))
    assert model.configure("", "next-model")["configured"] is False
    assert model.status()["route"] == "direct"


def test_route_change_during_request_keeps_gateway_snapshot(monkeypatch):
    started, finish = threading.Event(), threading.Event()
    results, calls, errors = [], [], []

    def request(payload, **kwargs):
        calls.append((payload, kwargs))
        started.set()
        assert finish.wait(2)
        return {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(decision())},
                }
            ]
        }

    monkeypatch.setattr(chat_model, "_request", request)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile(provider="anthropic"))

    def run():
        try:
            results.append(model.interpret({}))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(1)
        model.configure("new-test-key", "native-test-model")
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert results[0]["action"] == "stage.position"
    payload, config = calls[0]
    assert payload["model"] == "openai/gpt-6.1-sol" and payload["stream"] is True
    assert config["route"] == "token_station" and config["api_key"] == "test-gateway-key"
    assert config["base_url"] == TOKEN_STATION_BASE_URL
