import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.agent import run_agent


@pytest.fixture(autouse=True)
def isolated_api_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_MODE"):
        monkeypatch.delenv(name, raising=False)


def call(name="home", arguments="{}", call_id="home_call", call_type="function"):
    return SimpleNamespace(
        id=call_id,
        type=call_type,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def response(*calls, text=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
                message=SimpleNamespace(content=text, tool_calls=list(calls)),
            )
        ]
    )


def client_for(*responses):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=responses)))
    )


@pytest.fixture
def executor():
    return Mock(
        execute=Mock(return_value={"ok": True, "state": {"joints_deg": [0, 0, 0, 0]}}),
        stop=Mock(return_value={"ok": True}),
        tool_schemas=Mock(
            return_value=[
                {
                    "type": "function",
                    "name": "home",
                    "description": "Return to zero joints",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]
        ),
    )


def run(task, executor, client, **kwargs):
    return run_agent(
        task, executor, client=client, model="qwen-test", api_mode="chat_completions", **kwargs
    )


def test_chat_tools_and_feedback_use_chat_wire_format_and_commit_history(executor):
    client = client_for(response(call(), text="Returning home."), response(text="Reached home."))
    history = []

    assert run("Go home", executor, client, history=history) == "Reached home."

    first, second = [item.kwargs for item in client.chat.completions.create.call_args_list]
    assert first["model"] == "qwen-test"
    assert first["parallel_tool_calls"] is False
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][1:] == [{"role": "user", "content": "Go home"}]
    schema = executor.tool_schemas.return_value[0]
    assert first["tools"] == [
        {
            "type": "function",
            "function": {key: schema[key] for key in ("name", "description", "parameters")},
        }
    ]
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "home_call"
    assert json.loads(tool_message["content"]) == executor.execute.return_value
    assistant = second["messages"][-2]
    assert assistant == {
        "role": "assistant",
        "content": "Returning home.",
        "tool_calls": [
            {
                "id": "home_call",
                "type": "function",
                "function": {"name": "home", "arguments": "{}"},
            }
        ],
    }
    assert history == [
        {"role": "user", "content": "Go home"},
        assistant,
        tool_message,
        {"role": "assistant", "content": "Reached home."},
    ]
    executor.execute.assert_called_once_with("home", {})
    executor.stop.assert_not_called()
    earlier = list(history)
    next_client = client_for(response(text="Ready."))
    run("What next?", executor, next_client, history=history)
    assert next_client.chat.completions.create.call_args.kwargs["messages"][1:] == [
        *earlier,
        {"role": "user", "content": "What next?"},
    ]


def test_chat_content_only_reply_does_not_move(executor):
    client = client_for(response(text="Which joint do you mean?"))
    assert run("Move a joint", executor, client) == "Which joint do you mean?"
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


def test_chat_failed_turn_leaves_history_unmodified(executor):
    original = [{"role": "assistant", "content": "Ready."}]
    history = list(original)
    client = client_for(response(call()), ConnectionError("Disconnected"))
    with pytest.raises(ConnectionError, match="Disconnected"):
        run("Go home", executor, client, history=history)
    assert history == original
    executor.execute.assert_called_once()
    executor.stop.assert_called_once()


@pytest.mark.parametrize(
    "reply,error",
    [
        (response(call(), call(call_id="second")), "Multiple tool calls"),
        (response(call(), finish_reason="length"), "did not complete"),
        (response(call(), finish_reason="content_filter"), "did not complete"),
        (response(finish_reason="tool_calls"), "supplied no tool"),
        (response(text="  "), "neither a tool call nor final text"),
        (response(call(call_type="custom")), "Only function tool calls"),
        (SimpleNamespace(choices=[]), "Expected one Chat Completions choice"),
    ],
)
def test_chat_rejects_incomplete_or_ambiguous_response_before_motion(executor, reply, error):
    client = client_for(reply)
    with pytest.raises(RuntimeError, match=error):
        run("Go home", executor, client)
    executor.execute.assert_not_called()
    executor.stop.assert_called_once()
    assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize(
    "tool", [call(arguments="{bad"), call(arguments="[]"), call(call_id=""), call(call_id=None)]
)
def test_chat_rejects_bad_tool_arguments_or_id_before_motion(executor, tool):
    with pytest.raises(ValueError):
        run("Go home", executor, client_for(response(tool)))
    executor.execute.assert_not_called()
    executor.stop.assert_called_once()


def test_chat_default_client_uses_bounded_timeout_and_no_retry(executor, monkeypatch):
    factory = Mock(return_value=client_for(response(text="Ready.")))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    assert (
        run_agent(
            "Explain available tools", executor, model="qwen-test", api_mode="chat_completions"
        )
        == "Ready."
    )
    factory.assert_called_once_with(timeout=120.0, max_retries=0)


@pytest.mark.parametrize("key", [None, "", "test-only-provided-key"])
def test_custom_chat_client_preserves_key_or_supplies_sdk_placeholder(
    executor, monkeypatch, capsys, key
):
    factory = Mock(return_value=client_for(response(text="Ready.")))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    monkeypatch.setenv("OPENAI_BASE_URL", "http://lab.example.invalid/v1")
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)

    assert (
        run_agent(
            "Explain available tools", executor, model="qwen-test", api_mode="chat_completions"
        )
        == "Ready."
    )

    factory.assert_called_once_with(timeout=120.0, max_retries=0, api_key=key or "not-required")
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_custom_chat_auth_failure_does_not_retry_with_placeholder(executor, monkeypatch):
    client = client_for(PermissionError("Authentication rejected"))
    factory = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    monkeypatch.setenv("OPENAI_BASE_URL", "http://lab.example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-provided-key")

    with pytest.raises(PermissionError, match="Authentication rejected"):
        run_agent("Go home", executor, model="qwen-test", api_mode="chat_completions")

    factory.assert_called_once_with(timeout=120.0, max_retries=0, api_key="test-only-provided-key")
    assert client.chat.completions.create.call_count == 1
    executor.execute.assert_not_called()
    executor.stop.assert_called_once()
