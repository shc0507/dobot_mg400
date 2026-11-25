"""Check model cancellation and usage with fake providers and local socket pairs."""

import io
import json
import socket
import threading
from time import monotonic

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect import chat_model, chat_request_runtime
from orchestrator.agent_connect.chat_completions_stream import read_stream
from orchestrator.agent_connect.chat_model import OpenAIInterpreter
from orchestrator.agent_connect.chat_model_usage import usage_report
from orchestrator.agent_connect.chat_request_runtime import ModelRequestCancelled, RequestControl


def envelope():
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "kind": "answer",
                                "message": "Ready.",
                                "action": "",
                                "params_json": "{}",
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 17, "output_tokens": 4, "total_tokens": 21},
    }


def profile(provider="openai", route="direct"):
    return {"provider": provider, "route": route, "model": "test-model", "api_key": "test-secret"}


@pytest.mark.parametrize(
    "provider,route,usage,expected",
    [
        (
            "openai",
            "direct",
            {"input_tokens": 17, "output_tokens": 4, "total_tokens": 21},
            (17, 4, 21),
        ),
        (
            "anthropic",
            "direct",
            {
                "input_tokens": 3,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 2,
                "output_tokens": 4,
            },
            (12, 4, 16),
        ),
        (
            "gemini",
            "direct",
            {
                "promptTokenCount": 9,
                "cachedContentTokenCount": 5,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 4,
                "totalTokenCount": 16,
            },
            (9, 7, 16),
        ),
        (
            "gemini",
            "token_station",
            {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16},
            (9, 7, 16),
        ),
        ("openai", "direct", {}, (None, None, None)),
        ("anthropic", "direct", {"input_tokens": 3, "output_tokens": 4}, (3, 4, 7)),
        (
            "openai",
            "direct",
            {"input_tokens": True, "output_tokens": -4, "total_tokens": "secret"},
            (None, None, None),
        ),
        (
            "gemini",
            "direct",
            {"promptTokenCount": 4, "candidatesTokenCount": 3, "thoughtsTokenCount": "secret"},
            (4, None, None),
        ),
    ],
)
def test_provider_usage_is_numeric_and_separate(provider, route, usage, expected):
    field = "usageMetadata" if provider == "gemini" and route == "direct" else "usage"
    report = usage_report(profile(provider, route), {field: usage}, 35.4, "completed")
    assert (
        tuple(report[key] for key in ("input_tokens", "output_tokens", "total_tokens")) == expected
    )
    assert report["elapsed_ms"] == 35 and report["cost_usd"] is None
    assert report["provider"] == provider and report["route"] == route
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.025, 0.025),
        (0, 0),
        (-1, None),
        (True, None),
        ("secret", None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_gateway_cost_requires_explicit_usd_numeric_value(value, expected):
    response = {"usage": {"cost_usd": value, "cost": 22, "secret": "private"}}
    report = usage_report(profile(route="token_station"), response, 1, "completed")
    assert report["cost_usd"] == expected
    assert usage_report(profile(), response, 1, "completed")["cost_usd"] is None


def test_report_does_not_change_decision_or_send_usage_to_the_model():
    reports, payloads, activity = [], [], []

    def transport(payload, *, api_key):
        payloads.append(payload)
        return envelope()

    model = OpenAIInterpreter(transport, api_key="test-secret")
    result = model.interpret(
        {}, on_usage=reports.append, on_activity=lambda *item: activity.append(item)
    )
    assert set(result) == {"kind", "message", "action", "params"}
    assert len(reports) == 1 and reports[0]["status"] == "completed"
    assert reports[0]["total_tokens"] == 21 and reports[0]["elapsed_ms"] >= 0
    assert len(activity) == 2
    assert "test-secret" not in json.dumps([result, reports, payloads, activity])
    assert all(
        key not in json.dumps(payloads) for key in ("elapsed_ms", "total_tokens", "cost_usd")
    )


def test_invalid_decision_keeps_usage_but_never_returns_an_action():
    value, reports = envelope(), []
    value["output"][0]["content"][0]["text"] = '{"kind":"command"}'
    model = OpenAIInterpreter(lambda *args, **kwargs: value, api_key="test-secret")
    with pytest.raises(ServiceError, match="one valid decision"):
        model.interpret({}, on_usage=reports.append)
    assert reports[0]["status"] == "failed" and reports[0]["total_tokens"] == 21


def test_reporting_errors_do_not_change_the_valid_decision():
    def fail(*args):
        raise ValueError("private callback error")

    model = OpenAIInterpreter(lambda *args, **kwargs: envelope(), api_key="test-secret")
    assert model.interpret({}, on_usage=fail, on_activity=fail)["kind"] == "answer"


def test_cancelled_request_does_not_call_transport():
    cancelled, calls = threading.Event(), []
    cancelled.set()
    model = OpenAIInterpreter(lambda *args, **kwargs: calls.append(args), api_key="test-secret")
    with pytest.raises(ModelRequestCancelled):
        model.interpret({}, cancelled=cancelled)
    assert calls == []


def test_injected_transport_gets_supported_cancel_event_and_late_result_is_ignored():
    cancelled, entered, release = threading.Event(), threading.Event(), threading.Event()
    reports, decisions, errors = [], [], []

    def transport(payload, *, api_key, cancelled):
        assert cancelled is cancel_event
        entered.set()
        assert release.wait(2)
        return envelope()

    cancel_event = cancelled
    model = OpenAIInterpreter(transport, api_key="test-secret")

    def run():
        try:
            decisions.append(model.interpret({}, cancelled=cancelled, on_usage=reports.append))
        except ModelRequestCancelled as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        cancelled.set()
        thread.join(1)
        assert not thread.is_alive() and len(errors) == 1 and decisions == []
        assert len(reports) == 1 and reports[0]["status"] == "cancelled"
        assert reports[0]["total_tokens"] is None
    finally:
        release.set()
        thread.join(2)
    assert decisions == [] and len(reports) == 1


def test_stream_keeps_final_usage_without_including_it_in_decision():
    text = envelope()["output"][0]["content"][0]["text"]
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
    ]
    raw = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    result = read_stream(
        io.BytesIO(raw.encode()), loads=chat_model._loads, max_bytes=4096, timeout_s=1
    )
    assert result["usage"] == chunks[-1]["usage"]
    assert set(chat_model._decision(result, route="token_station")) == {
        "kind",
        "message",
        "action",
        "params",
    }


@pytest.mark.parametrize("phase", ["headers", "body", "stream"])
def test_cancel_shuts_down_stalled_http_socket(monkeypatch, phase):
    client, server = socket.socketpair()
    cancelled, sent, peer_closed = threading.Event(), threading.Event(), threading.Event()
    reports, errors, results = [], [], []

    def connect(*args, **kwargs):
        client.settimeout(2)
        return client

    monkeypatch.setattr(socket, "create_connection", connect)

    def peer():
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += server.recv(65536)
            if phase != "headers":
                server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n")
            sent.set()
            while server.recv(65536):
                pass
            peer_closed.set()
        except OSError:
            peer_closed.set()
        finally:
            server.close()

    peer_thread = threading.Thread(target=peer, daemon=True)
    peer_thread.start()
    model = OpenAIInterpreter(api_key="test-secret")
    config = profile(route="token_station" if phase == "stream" else "direct")
    model.configure_profile(config | {"base_url": "http://127.0.0.1:9999/v1"})

    def run():
        try:
            results.append(model.interpret({}, cancelled=cancelled, on_usage=reports.append))
        except ServiceError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert sent.wait(1)
        start = monotonic()
        cancelled.set()
        thread.join(1)
        assert not thread.is_alive() and monotonic() - start < 1
        assert peer_closed.wait(1)
        assert results == [] and len(errors) == 1
        assert isinstance(errors[0], ModelRequestCancelled)
        assert reports[0]["status"] == "cancelled" and reports[0]["total_tokens"] is None
    finally:
        cancelled.set()
        client.close()
        server.close()
        thread.join(2)
        peer_thread.join(2)


def test_deadline_bounds_an_uninterruptible_transport():
    release = threading.Event()
    control = RequestControl(None, 0.08)
    start = monotonic()
    try:
        with pytest.raises(TimeoutError):
            control.run(lambda: release.wait(2))
        assert monotonic() - start < 1
    finally:
        release.set()


def test_cancel_shuts_down_a_stalled_tls_handshake(monkeypatch):
    client, server = socket.socketpair()
    cancelled, errors = threading.Event(), []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: client)
    control = RequestControl(cancelled, 2)
    connection = chat_request_runtime._HTTPSConnection("fake.example", control=control)

    def run():
        try:
            control.run(connection.connect)
        except ModelRequestCancelled as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        server.settimeout(1)
        assert server.recv(65536)  # The TLS ClientHello starts the handshake.
        cancelled.set()
        thread.join(1)
        assert not thread.is_alive() and len(errors) == 1
        assert server.recv(65536) == b""
    finally:
        cancelled.set()
        client.close()
        server.close()
        thread.join(2)


def test_connection_created_after_cancel_is_closed_before_request_data(monkeypatch):
    cancelled, entered, release = threading.Event(), threading.Event(), threading.Event()
    client, server = socket.socketpair()

    def connect(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return client

    monkeypatch.setattr(socket, "create_connection", connect)
    control = RequestControl(cancelled, 2)
    connection = chat_request_runtime._HTTPConnection("fake.example", control=control)
    errors = []

    def run():
        try:
            control.run(lambda: connection.request("POST", "/", body=b"private-request"))
        except ModelRequestCancelled as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        cancelled.set()
        thread.join(1)
        assert not thread.is_alive() and len(errors) == 1
        release.set()
        server.settimeout(1)
        assert server.recv(65536) == b""
    finally:
        release.set()
        client.close()
        server.close()
        thread.join(2)
