"""Check native provider adapters with fake HTTP responses and no paid calls."""

import copy
import http.client
import io
import json
import threading
import urllib.error
from urllib.parse import urlsplit

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect import chat_model
from orchestrator.agent_connect.chat_model import DECISION_SCHEMA, OpenAIInterpreter
from orchestrator.agent_connect.chat_providers import ANTHROPIC_VERSION
from orchestrator.agent_connect.model_profiles import DEFAULT_BASE_URLS

PROVIDERS = tuple(DEFAULT_BASE_URLS)


def decision(**changes):
    return {
        "kind": "command",
        "message": "Move Top to 2 rotations.",
        "action": "stage.position",
        "params_json": '{"motor":"top","position":2}',
    } | changes


def envelope(provider, text=None):
    text = json.dumps(decision()) if text is None else text
    if provider == "openai":
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        }
    if provider == "anthropic":
        return {
            "type": "message",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        }
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": text}],
                },
            }
        ]
    }


def profile(provider="openai", **changes):
    return {
        "id": "profile-one",
        "name": "Lab model",
        "provider": provider,
        "model": "test-model",
        "base_url": DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["openai"]),
        "api_key": "test-provider-secret",
    } | changes


def install_http(monkeypatch, *, response=None, raw=None, failure=None):
    calls, reads, handlers, closed = [], [], [], []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def read(self, amount):
            reads.append(amount)
            return raw if raw is not None else json.dumps(response).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if failure is not None:
                raise failure
            return Reply()

    def build_opener(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(chat_model.urllib.request, "build_opener", build_opener)
    return calls, reads, handlers, closed


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("custom_endpoint", [False, True])
def test_native_requests_use_provider_wire_format(monkeypatch, provider, custom_endpoint):
    calls, reads, handlers, closed = install_http(monkeypatch, response=envelope(provider))
    config = profile(provider)
    if custom_endpoint:
        config["base_url"] = "http://127.0.0.1:9100/provider/v9/"
    model = OpenAIInterpreter(api_key="previous-key")
    model.configure_profile(config)
    context = {
        "user_message": "Move Top to 2 rotations.",
        "state": {"note": "Untrusted data"},
        "project_knowledge": "Reviewed operating context.",
    }
    original = copy.deepcopy(context)
    assert model.interpret(context)["params"] == {"motor": "top", "position": 2}
    assert context == original
    assert len(calls) == 1 and reads == [chat_model.MAX_RESPONSE_BYTES + 1] and closed == [True]
    request, timeout = calls[0]
    assert request.method == "POST" and timeout == chat_model.TIMEOUT_S
    assert urlsplit(request.full_url).query == ""
    assert any(isinstance(handler, chat_model._NoRedirect) for handler in handlers)
    assert next(handler for handler in handlers if hasattr(handler, "proxies")).proxies == {}
    payload = json.loads(request.data)
    headers = {key.lower(): value for key, value in request.header_items()}
    base = config["base_url"].rstrip("/")
    if provider == "openai":
        assert request.full_url == base + "/responses"
        assert headers["authorization"] == "Bearer test-provider-secret"
        assert payload["store"] is False and payload["max_output_tokens"] == 4096
        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": "console_decision",
            "strict": True,
            "schema": DECISION_SCHEMA,
        }
        assert payload["input"][0]["role"] == "developer"
        instructions, content = payload["input"][0]["content"], payload["input"][1]["content"]
    elif provider == "anthropic":
        assert request.full_url == base + "/messages"
        assert headers["x-api-key"] == "test-provider-secret"
        assert headers["anthropic-version"] == ANTHROPIC_VERSION
        assert "authorization" not in headers
        assert payload["max_tokens"] == 4096
        assert payload["output_config"]["format"] == {
            "type": "json_schema",
            "schema": DECISION_SCHEMA,
        }
        assert payload["messages"][0]["role"] == "user"
        instructions, content = payload["system"], payload["messages"][0]["content"]
    else:
        assert request.full_url == base + "/models/test-model:generateContent"
        assert headers["x-goog-api-key"] == "test-provider-secret"
        assert "authorization" not in headers
        assert payload["generationConfig"] == {
            "candidateCount": 1,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "responseJsonSchema": DECISION_SCHEMA,
        }
        assert payload["contents"][0]["role"] == "user"
        instructions = payload["systemInstruction"]["parts"][0]["text"]
        content = payload["contents"][0]["parts"][0]["text"]
    assert "Reviewed operating context." in instructions
    assert "Untrusted data" not in instructions
    assert json.loads(content) == {
        key: value for key, value in context.items() if key != "project_knowledge"
    }
    assert "tools" not in payload
    assert "test-provider-secret" not in request.data.decode() + request.full_url
    assert "previous-key" not in str(headers)


def test_profile_switch_replaces_secrets_endpoint_and_status():
    seen = []

    def transport(payload, *, api_key):
        seen.append(api_key)
        return envelope("anthropic")

    model = OpenAIInterpreter(transport, api_key="old-key")
    selected = profile("anthropic", api_key="new-key", base_url="https://proxy.example/v1/")
    assert model.configure_profile(selected) == {
        "configured": True,
        "provider": "anthropic",
        "route": "direct",
        "profile_id": "profile-one",
        "name": "Lab model",
        "model": "test-model",
        "base_url": "https://proxy.example/v1",
    }
    selected["api_key"] = "mutated-key"
    model.interpret({})
    assert seen == ["new-key"]
    assert all(
        key not in json.dumps(model.status()) for key in ("old-key", "new-key", "mutated-key")
    )
    assert model.configure_profile(None) == {
        "configured": False,
        "provider": "",
        "route": "direct",
        "profile_id": "",
        "name": "",
        "model": "",
        "base_url": "",
    }
    with pytest.raises(ServiceError, match="API key"):
        model.interpret({})
    assert seen == ["new-key"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_blank_profile_key_never_reuses_previous_key(provider):
    model = OpenAIInterpreter(api_key="old-key")
    model.configure_profile(profile(provider, api_key=""))
    assert model.status()["configured"] is False
    with pytest.raises(ServiceError, match="API key"):
        model.interpret({})


@pytest.mark.parametrize(
    "provider,endpoint",
    [
        ("anthropic", DEFAULT_BASE_URLS["anthropic"]),
        ("openai", "https://proxy.example/v1"),
    ],
)
def test_legacy_config_does_not_send_profile_key_to_another_provider_or_endpoint(
    provider, endpoint
):
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile(provider, base_url=endpoint))
    assert model.configure("", "next-model")["configured"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"model": "../model"},
        {"base_url": "http://remote.example/v1"},
        {"base_url": "https://user:secret@example.com/v1"},
        {"base_url": "https://example.com/?key=secret"},
        {"base_url": "https://example.com/#secret"},
        {"api_key": "secret\n"},
        {"name": ["bad"]},
    ],
)
def test_bad_profile_preserves_active_configuration(changes):
    model = OpenAIInterpreter(api_key="old-key")
    before = model.status()
    with pytest.raises(ServiceError) as error:
        model.configure_profile(profile(**changes))
    assert model.status() == before
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "{",
        '{"kind":"answer","kind":"command"}',
        json.dumps(decision(kind=[])),
        json.dumps(decision(message="")),
        json.dumps(decision(extra=True)),
        json.dumps(decision(action="")),
        json.dumps(decision(kind="answer")),
        json.dumps(decision(params_json="[]")),
        json.dumps(decision(params_json='{"x":NaN}')),
        json.dumps(decision(params_json='{"x":1e999}')),
        json.dumps(decision(params_json='{"x":1,"x":2}')),
        json.dumps(decision(kind="stop", action="stop", params_json='{"target":"unknown"}')),
        json.dumps(decision(kind="stop", action="stop", params_json='{"target":[]}')),
    ],
)
def test_all_providers_share_strict_decision_validation(provider, text):
    model = OpenAIInterpreter(lambda payload, **kwargs: envelope(provider, text), api_key="")
    model.configure_profile(profile(provider))
    with pytest.raises(ServiceError, match="one valid decision"):
        model.interpret({})


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("kind", ["answer", "clarify", "stop"])
def test_all_providers_return_non_motion_decisions(provider, kind):
    expected = decision(
        kind=kind,
        action="stop" if kind == "stop" else "",
        params_json='{"target":"all_motion"}' if kind == "stop" else "{}",
    )
    model = OpenAIInterpreter(
        lambda payload, **kwargs: envelope(provider, json.dumps(expected)), api_key=""
    )
    model.configure_profile(profile(provider))
    result = model.interpret({})
    assert result["kind"] == kind and result["action"] == expected["action"]


def invalid_envelopes():
    for provider in PROVIDERS:
        for field, value in [("error", {"message": "test-provider-secret"})]:
            yield provider, envelope(provider) | {field: value}
        value = envelope(provider)
        if provider == "openai":
            yield provider, value | {"status": "incomplete"}
            value["output"][0]["content"] = [{"type": "refusal", "refusal": "No."}]
        elif provider == "anthropic":
            for reason in ("max_tokens", "refusal", "tool_use", "stop_sequence", None):
                yield provider, value | {"stop_reason": reason}
            yield provider, value | {"content": value["content"] * 2}
            value["content"] = [{"type": "tool_use", "name": "stage.position", "input": {}}]
        else:
            for reason in ("MAX_TOKENS", "SAFETY", "RECITATION", None):
                changed = copy.deepcopy(value)
                changed["candidates"][0]["finishReason"] = reason
                yield provider, changed
            yield provider, value | {"promptFeedback": {"blockReason": "SAFETY"}}
            yield provider, value | {"candidates": value["candidates"] * 2}
            changed = copy.deepcopy(value)
            changed["candidates"][0]["safetyRatings"] = [{"blocked": True}]
            yield provider, changed
            value["candidates"][0]["content"]["parts"] = [{"functionCall": {"name": "stop"}}]
        yield provider, value


@pytest.mark.parametrize("provider,value", list(invalid_envelopes()))
def test_refusals_truncation_multiple_outputs_and_tool_calls_are_rejected(provider, value):
    model = OpenAIInterpreter(lambda payload, **kwargs: value, api_key="")
    model.configure_profile(profile(provider))
    with pytest.raises(ServiceError, match="one valid decision") as error:
        model.interpret({})
    assert "test-provider-secret" not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            "https://example.com",
            401,
            "test-provider-secret",
            {},
            io.BytesIO(b"test-provider-secret"),
        ),
        urllib.error.HTTPError("https://example.com", 429, "test-provider-secret", {}, None),
        urllib.error.URLError("test-provider-secret"),
        TimeoutError("test-provider-secret"),
        http.client.IncompleteRead(b"test-provider-secret"),
    ],
)
def test_transport_errors_are_redacted_without_retries(monkeypatch, provider, failure):
    calls, *_ = install_http(monkeypatch, failure=failure)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile(provider))
    with pytest.raises(ServiceError) as error:
        model.interpret({})
    assert error.value.status == 502 and len(calls) == 1
    assert "test-provider-secret" not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "raw",
    [
        b"x" * (chat_model.MAX_RESPONSE_BYTES + 1),
        b"test-provider-secret",
        b'{"status":"completed","status":"incomplete"}',
    ],
    ids=["oversized", "invalid-json", "duplicate-field"],
)
def test_transport_bounds_and_validates_json(monkeypatch, provider, raw):
    calls, reads, _, closed = install_http(monkeypatch, raw=raw)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile(provider))
    with pytest.raises(ServiceError) as error:
        model.interpret({})
    assert error.value.status == 502 and len(calls) == 1 and closed == [True]
    assert reads == [chat_model.MAX_RESPONSE_BYTES + 1]
    assert "test-provider-secret" not in str(error.value)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_handler_closes_response_without_following(code):
    stream = io.BytesIO(b"test-provider-secret")
    with pytest.raises(ServiceError, match="redirected"):
        chat_model._NoRedirect().redirect_request(
            None, stream, code, "secret", {}, "https://other.example/secret"
        )
    assert stream.closed


def test_profile_switch_during_request_keeps_one_consistent_snapshot(monkeypatch):
    started, finish = threading.Event(), threading.Event()
    calls, errors = [], []

    def request(payload, **kwargs):
        calls.append(kwargs)
        started.set()
        assert finish.wait(2)
        return envelope("anthropic")

    monkeypatch.setattr(chat_model, "_request", request)
    model = OpenAIInterpreter(api_key="")
    model.configure_profile(profile("anthropic"))

    def run():
        try:
            model.interpret({})
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(1)
        model.configure_profile(profile("gemini", api_key="next-key"))
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert calls == [
        {
            "api_key": "test-provider-secret",
            "provider": "anthropic",
            "base_url": DEFAULT_BASE_URLS["anthropic"],
            "model": "test-model",
            "route": "direct",
        }
    ]
