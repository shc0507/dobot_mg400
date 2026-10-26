import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.agent import run_agent


def response(*output, text="", status="completed", error=None):
    return SimpleNamespace(output=list(output), output_text=text, status=status, error=error)


def call(name="move_to", arguments='{"target": "home"}', call_id="call_1"):
    return SimpleNamespace(type="function_call", name=name, arguments=arguments, call_id=call_id)


@pytest.fixture
def executor():
    return Mock(
        execute=Mock(return_value={"ok": True, "position": "home"}),
        tool_schemas=Mock(return_value=[{"type": "function", "name": "move_to"}]),
        stop=Mock(return_value={"ok": True}),
    )


def client_for(*responses):
    return SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=responses)))


def test_replays_all_outputs_and_tool_result_without_server_storage(executor):
    reasoning = SimpleNamespace(type="reasoning", encrypted_content="opaque")
    tool_call = call()
    client = client_for(response(reasoning, tool_call), response(text="Reached home."))

    assert run_agent("Go home", executor, model="test-model", client=client) == "Reached home."
    executor.execute.assert_called_once_with("move_to", {"target": "home"})
    executor.stop.assert_not_called()
    first, second = [request.kwargs for request in client.responses.create.call_args_list]
    assert first["input"] == [{"role": "user", "content": "Go home"}]
    assert first["model"] == "test-model"
    assert first["tools"] == executor.tool_schemas.return_value
    assert first["parallel_tool_calls"] is False
    assert first["store"] is False
    assert first["include"] == ["reasoning.encrypted_content"]
    assert second["input"][1:3] == [reasoning, tool_call]
    result = second["input"][3]
    assert result["type"] == "function_call_output"
    assert result["call_id"] == "call_1"
    assert json.loads(result["output"]) == executor.execute.return_value


def test_informational_reply_does_not_execute_actions(executor):
    client = client_for(response(text="Which named position should I use?"))
    assert run_agent("Move somewhere", executor, model="test", client=client).startswith("Which")
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


def test_default_client_bounds_api_timeout_and_disables_retries(executor, monkeypatch):
    client = client_for(response(text="Ready."))
    factory = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    assert run_agent("Explain available skills", executor, model="test") == "Ready."
    factory.assert_called_once_with(timeout=30.0, max_retries=0)


def test_agent_completes_sequential_tools(executor):
    results = [
        {"ok": True, "state": {"joints_deg": [0, 0, 0, 0]}},
        {"ok": True, "state": {"joints_deg": [20, 0, 0, 0]}},
        {"ok": True, "state": {"joints_deg": [0, 0, 0, 0]}},
    ]
    executor.execute.side_effect = results
    client = client_for(
        response(call("get_robot_state", "{}", "state")),
        response(call("move_joint", '{"joint": "J1", "angle_deg": 20}', "move")),
        response(call("home", "{}", "home")),
        response(text="J1 moved to 20 degrees and returned home."),
    )

    answer = run_agent("Move J1 to 20 degrees, then go home", executor, model="test", client=client)

    assert answer == "J1 moved to 20 degrees and returned home."
    assert [request.args for request in executor.execute.call_args_list] == [
        ("get_robot_state", {}),
        ("move_joint", {"joint": "J1", "angle_deg": 20}),
        ("home", {}),
    ]
    tool_outputs = [
        json.loads(item["output"])
        for item in client.responses.create.call_args_list[-1].kwargs["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert tool_outputs == results
    executor.stop.assert_not_called()
    assert client.responses.create.call_count == 4


def test_rejects_multiple_calls_before_executing_any(executor):
    client = client_for(response(call(), call(call_id="call_2")))
    with pytest.raises(RuntimeError, match="Multiple tool calls"):
        run_agent("Move", executor, model="test", client=client)
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


@pytest.mark.parametrize("arguments", ["{broken", "[]", "null", '"home"'])
def test_malformed_arguments_leave_executor_available(executor, arguments):
    client = client_for(response(call(arguments=arguments)))
    with pytest.raises(ValueError):
        run_agent("Move", executor, model="test", client=client)
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


@pytest.mark.parametrize("name", [None, "", 123])
def test_malformed_tool_name_leaves_executor_available(executor, name):
    with pytest.raises(ValueError, match="nonempty name"):
        run_agent("Move", executor, model="test", client=client_for(response(call(name=name))))
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


def test_unknown_tool_is_validated_by_executor_and_never_retried(executor):
    executor.execute.return_value = {"ok": False, "error": "Unknown skill"}
    client = client_for(response(call(name="unknown")))
    with pytest.raises(RuntimeError, match="Unknown skill"):
        run_agent("Move", executor, model="test", client=client)
    executor.execute.assert_called_once_with("unknown", {"target": "home"})
    executor.stop.assert_called_once()
    assert client.responses.create.call_count == 1


def test_executor_exception_stops_before_propagating(executor):
    error = ValueError("Invalid target")
    executor.execute.side_effect = error
    client = client_for(response(call()))
    with pytest.raises(ValueError) as caught:
        run_agent("Move", executor, model="test", client=client)
    assert caught.value is error
    executor.stop.assert_called_once()
    assert client.responses.create.call_count == 1


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", "cancelled"])
def test_noncompleted_response_leaves_executor_available(executor, status):
    client = client_for(response(call(), status=status))
    with pytest.raises(RuntimeError, match="did not complete"):
        run_agent("Move", executor, model="test", client=client)
    executor.execute.assert_not_called()
    executor.stop.assert_not_called()


def test_api_exception_allows_next_turn(executor):
    error = ConnectionError("API unavailable")
    client = client_for(error)
    with pytest.raises(ConnectionError) as caught:
        run_agent("Move", executor, model="test", client=client)
    assert caught.value is error
    executor.stop.assert_not_called()
    assert (
        run_agent("Read state", executor, model="test", client=client_for(response(text="Ready.")))
        == "Ready."
    )


def test_loop_limit_does_not_stop_completed_synchronous_tools(executor):
    client = client_for(response(call()), response(call(call_id="call_2")))
    with pytest.raises(RuntimeError, match="limit of 2"):
        run_agent("Move", executor, model="test", client=client, max_steps=2)
    assert client.responses.create.call_count == 2
    assert executor.execute.call_count == 2
    executor.stop.assert_not_called()


def test_empty_final_response_is_failure(executor):
    client = client_for(response())
    with pytest.raises(RuntimeError, match="neither a tool call nor final text"):
        run_agent("Move", executor, model="test", client=client)
    executor.stop.assert_not_called()


@pytest.mark.parametrize("max_steps", [0, -1, True, 1.5])
def test_invalid_budget_does_not_make_api_request(executor, max_steps):
    client = client_for()
    with pytest.raises(ValueError, match="positive integer"):
        run_agent("Move", executor, model="test", client=client, max_steps=max_steps)
    client.responses.create.assert_not_called()
    executor.stop.assert_not_called()


def test_stop_failure_is_reported_with_original_failure(executor):
    executor.stop.side_effect = RuntimeError("Stop unavailable")
    original = ConnectionError("Device disconnected")
    executor.execute.side_effect = original
    client = client_for(response(call()))
    with pytest.raises(RuntimeError, match="stopping also failed") as caught:
        run_agent("Move", executor, model="test", client=client)
    assert caught.value.__cause__ is original
    assert "Stop unavailable" in str(caught.value)


def test_unsuccessful_stop_result_is_reported_with_original_failure(executor):
    executor.stop.return_value = {"ok": False, "error": "Stop unconfirmed"}
    original = ConnectionError("Device disconnected")
    executor.execute.side_effect = original
    client = client_for(response(call()))
    with pytest.raises(RuntimeError, match="stopping also failed") as caught:
        run_agent("Move", executor, model="test", client=client)
    assert caught.value.__cause__ is original
    assert "Stop unconfirmed" in str(caught.value)


def test_successful_turn_commits_reasoning_tools_and_final_output_to_history(executor):
    reasoning = SimpleNamespace(type="reasoning", encrypted_content="opaque")
    tool_call = call()
    final = SimpleNamespace(type="message", role="assistant", content="Reached home.")
    history = [{"role": "user", "content": "Earlier question"}]
    client = client_for(response(reasoning, tool_call), response(final, text="Reached home."))

    run_agent("Go home", executor, model="test", client=client, history=history)

    assert history[:4] == [
        {"role": "user", "content": "Earlier question"},
        {"role": "user", "content": "Go home"},
        reasoning,
        tool_call,
    ]
    assert json.loads(history[4]["output"]) == executor.execute.return_value
    assert history[-1] is final
    previous_turn = list(history)
    next_client = client_for(response(text="Ready."))
    run_agent("What next?", executor, model="test", client=next_client, history=history)
    assert next_client.responses.create.call_args.kwargs["input"] == [
        *previous_turn,
        {"role": "user", "content": "What next?"},
    ]


def test_api_failure_preserves_completed_tool_history_for_next_turn(executor):
    history = [{"role": "user", "content": "Earlier question"}]
    client = client_for(response(call()), ConnectionError("Lost API connection"))

    with pytest.raises(ConnectionError, match="Lost API connection"):
        run_agent("Go home", executor, model="test", client=client, history=history)

    executor.execute.assert_called_once()
    assert history[0] == {"role": "user", "content": "Earlier question"}
    assert history[1] == {"role": "user", "content": "Go home"}
    assert history[2].type == "function_call"
    assert json.loads(history[3]["output"]) == executor.execute.return_value
    executor.stop.assert_not_called()
    prior = list(history)
    next_client = client_for(response(text="The previous move completed."))
    run_agent("What happened?", executor, model="test", client=next_client, history=history)
    assert next_client.responses.create.call_args.kwargs["input"] == [
        *prior,
        {"role": "user", "content": "What happened?"},
    ]
    executor.execute.assert_called_once()


def test_agent_returns_recoverable_rejection_to_model_for_correction(executor):
    rejection = {"ok": False, "recoverable": True, "error": "Unknown joint: J9"}
    executor.execute.side_effect = [rejection, {"ok": True}]
    client = client_for(
        response(call("move_joint", '{"joint": "J9", "angle_deg": 20}', "bad")),
        response(call("move_joint", '{"joint": "J1", "angle_deg": 20}', "corrected")),
        response(text="J1 moved to 20 degrees."),
    )
    assert (
        run_agent("Move J1 to 20 degrees", executor, model="test", client=client)
        == "J1 moved to 20 degrees."
    )
    rejected = client.responses.create.call_args_list[1].kwargs["input"][-1]
    assert json.loads(rejected["output"]) == rejection
    assert [request.args for request in executor.execute.call_args_list] == [
        ("move_joint", {"joint": "J9", "angle_deg": 20}),
        ("move_joint", {"joint": "J1", "angle_deg": 20}),
    ]
    executor.stop.assert_not_called()
    assert client.responses.create.call_count == 3


@pytest.mark.parametrize(
    "result",
    [None, {}, {"ok": "true"}, {"ok": False}, {"ok": True, "position": float("nan")}],
)
def test_unknown_executor_result_still_stops(executor, result):
    executor.execute.return_value = result
    with pytest.raises((RuntimeError, ValueError)):
        run_agent("Move", executor, model="test", client=client_for(response(call())))
    executor.stop.assert_called_once()


def test_interrupt_during_tool_execution_still_stops(executor):
    executor.execute.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_agent("Move", executor, model="test", client=client_for(response(call())))
    executor.stop.assert_called_once()


def test_request_runner_offloads_only_requests_while_tools_stay_on_calling_thread(executor):
    main_thread = get_ident()
    api_threads = []
    tool_threads = []
    results = iter([response(call()), response(text="Reached home.")])

    def request(**kwargs):
        api_threads.append(get_ident())
        return next(results)

    def execute(*args):
        tool_threads.append(get_ident())
        return {"ok": True}

    executor.execute.side_effect = execute
    client = SimpleNamespace(responses=SimpleNamespace(create=request))
    with ThreadPoolExecutor(max_workers=1) as worker:
        answer = run_agent(
            "Go home",
            executor,
            model="test",
            client=client,
            request_runner=lambda callback: worker.submit(callback).result(timeout=2),
        )

    assert answer == "Reached home."
    assert len(api_threads) == 2
    assert all(thread != main_thread for thread in api_threads)
    assert tool_threads == [main_thread]
