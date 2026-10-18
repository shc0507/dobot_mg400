"""Optional, bounded natural-language planning over validated robot skills."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Protocol


class ToolExecutor(Protocol):
    def tool_schemas(self) -> list[dict]: ...

    def execute(self, name: str, arguments: dict) -> dict: ...

    def stop(self) -> dict: ...


_INSTRUCTIONS = """You operate a robot only through the provided tools.
Use the tools' documented arguments, units, and named targets. Never invent robot state,
coordinates, or successful execution. Tool results are the source of verified
state. If the task is ambiguous, ask for clarification without moving the robot.
Read the current state before planning a new motion task. Only offer actions
supported by the available tools. Distinguish absolute angles (move to) from
relative angles (move by); ask if the requested direction or amount is unclear.
Issue at most one tool call per response. Do not retry failed actions. Describe
what was actually verified, and distinguish it from anything still unverified.
"""


def run_agent(
    task: str,
    executor: ToolExecutor,
    *,
    model: str,
    client: Any = None,
    max_steps: int = 12,
    history: list[Any] | None = None,
    request_runner: Callable[[Callable[[], Any]], Any] | None = None,
    api_mode: str = "responses",
) -> str:
    """Run a Responses or Chat Completions loop; any failure stops the executor.

    ``max_steps`` caps model requests, including requests returning final text.
    Returned prose is informational; consult ``executor.state()`` for robot state.
    A supplied client must expose the selected OpenAI-compatible API interface.
    Optional history is committed after a successful turn, retaining tool feedback.
    request_runner may keep a viewer responsive while an API request is pending;
    tool execution always stays on the calling thread.
    """
    try:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a nonempty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly specified")
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if api_mode not in {"responses", "chat_completions"}:
            raise ValueError("api_mode must be responses or chat_completions")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    "Install the OpenAI Python package to use agent mode."
                ) from error
            options = {
                "timeout": 120.0 if api_mode == "chat_completions" else 30.0,
                "max_retries": 0,
            }
            if api_mode == "chat_completions" and os.environ.get("OPENAI_BASE_URL"):
                # The SDK requires a nonempty value even for keyless lab servers.
                # A supplied key is always used; auth failures never trigger fallback.
                options["api_key"] = os.environ.get("OPENAI_API_KEY") or "not-required"
            client = OpenAI(**options)

        messages = list(history) if history is not None else []
        messages.append({"role": "user", "content": task})
        schemas = executor.tool_schemas()

        def request():
            if api_mode == "chat_completions":
                return client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": _INSTRUCTIONS}, *messages],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                key: tool[key] for key in ("name", "description", "parameters")
                            },
                        }
                        for tool in schemas
                    ],
                    parallel_tool_calls=False,
                )
            return client.responses.create(
                model=model,
                instructions=_INSTRUCTIONS,
                input=list(messages),
                tools=schemas,
                parallel_tool_calls=False,
                store=False,
                include=["reasoning.encrypted_content"],
            )

        for _ in range(max_steps):
            response = request() if request_runner is None else request_runner(request)
            if api_mode == "chat_completions":
                if len(response.choices) != 1:
                    raise RuntimeError("Expected one Chat Completions choice")
                choice = response.choices[0]
                if choice.finish_reason not in {"stop", "tool_calls"}:
                    raise RuntimeError(
                        f"Model response did not complete successfully: {choice.finish_reason}"
                    )
                message = choice.message
                calls = message.tool_calls or []
                output = [{"role": "assistant", "content": message.content}]
                if calls:
                    if any(call.type != "function" for call in calls):
                        raise RuntimeError("Only function tool calls are supported")
                    output[0]["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ]
                elif choice.finish_reason == "tool_calls":
                    raise RuntimeError("Model reported tool_calls but supplied no tool")
                final_text = message.content
            else:
                if response.status != "completed" or response.error is not None:
                    raise RuntimeError(
                        f"Model response did not complete successfully: {response.status}"
                    )
                calls = [item for item in response.output if item.type == "function_call"]
                output = response.output
                final_text = response.output_text
            if len(calls) > 1:
                raise RuntimeError("Multiple tool calls in one response are forbidden")
            if not calls:
                if not isinstance(final_text, str) or not final_text.strip():
                    raise RuntimeError("Model returned neither a tool call nor final text")
                if history is not None:
                    history[:] = [*messages, *output]
                return final_text

            call = calls[0]
            function = call.function if api_mode == "chat_completions" else call
            call_id = call.id if api_mode == "chat_completions" else call.call_id
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Tool call must have a nonempty id")
            arguments = json.loads(function.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            result = executor.execute(function.name, arguments)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError(f"Skill {function.name!r} failed: {result!r}")
            # Replay every output item, including encrypted reasoning, unchanged.
            messages.extend(output)
            encoded_result = json.dumps(result, allow_nan=False)
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": encoded_result,
                }
                if api_mode == "responses"
                else {"role": "tool", "tool_call_id": call_id, "content": encoded_result}
            )
        raise RuntimeError(f"Agent exceeded its limit of {max_steps} model requests")
    except BaseException as error:
        try:
            stop_result = executor.stop()
            if not isinstance(stop_result, dict) or stop_result.get("ok") is not True:
                raise RuntimeError(f"Stop could not be confirmed: {stop_result!r}")
        except Exception as stop_error:
            raise RuntimeError(
                f"Agent failed ({error}); stopping also failed ({stop_error})"
            ) from error
        raise
