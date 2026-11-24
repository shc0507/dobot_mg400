"""Model request formats and response extraction. No device commands."""

import json
from urllib.parse import quote

ANTHROPIC_VERSION = "2026-10-21"


def request_parts(provider, model, base_url, api_key, *, route="direct"):
    """Return the endpoint and headers for an already validated profile."""
    headers = {"Content-Type": "application/json"}
    if route == "token_station":
        path = "/chat/completions"
        headers["Authorization"] = "Bearer " + api_key
        headers["Accept"] = "text/event-stream"
    elif route != "direct":
        raise ValueError("Unknown model route.")
    elif provider == "openai":
        path = "/responses"
        headers["Authorization"] = "Bearer " + api_key
    elif provider == "anthropic":
        path = "/messages"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    elif provider == "gemini":
        path = "/models/" + quote(model, safe="") + ":generateContent"
        headers["x-goog-api-key"] = api_key
    else:
        raise ValueError("Unknown model provider.")
    return base_url + path, headers


def build_payload(provider, model, instructions, content, schema, *, route="direct"):
    if route == "token_station":
        return {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": instructions
                    + "\nReturn one JSON object that matches this exact JSON schema:\n"
                    + json.dumps(schema, allow_nan=False, separators=(",", ":")),
                },
                {"role": "user", "content": content},
            ],
            "stream": True,
        }
    if route != "direct":
        raise ValueError("Unknown model route.")
    if provider == "openai":
        return {
            "model": model,
            "store": False,
            "max_output_tokens": 4096,
            "input": [
                {"role": "developer", "content": instructions},
                {"role": "user", "content": content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "console_decision",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
    if provider == "anthropic":
        return {
            "model": model,
            "max_tokens": 4096,
            "system": instructions,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
    if provider == "gemini":
        return {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": content}]}],
            "generationConfig": {
                "candidateCount": 1,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
    raise ValueError("Unknown model provider.")


def _openai_text(response):
    if response.get("status") != "completed" or response.get("error"):
        raise ValueError("Incomplete model response.")
    output = response.get("output")
    if not isinstance(output, list):
        raise ValueError("Missing model output.")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Invalid model item.")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ValueError("Unexpected model item.")
        if item.get("status") not in (None, "completed"):
            raise ValueError("Incomplete model message.")
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("Missing model content.")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise ValueError("Model refusal or unexpected content.")
            texts.append(part.get("text"))
    return texts


def _anthropic_text(response):
    if (
        response.get("type") != "message"
        or response.get("role") != "assistant"
        or response.get("stop_reason") != "end_turn"
    ):
        raise ValueError("Incomplete model response.")
    content = response.get("content")
    if not isinstance(content, list):
        raise ValueError("Missing model content.")
    texts = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("Invalid model content.")
        if part.get("type") in {"thinking", "redacted_thinking"}:
            continue
        if part.get("type") != "text":
            raise ValueError("Model refusal or unexpected content.")
        texts.append(part.get("text"))
    return texts


def _gemini_text(response):
    feedback = response.get("promptFeedback", {})
    if not isinstance(feedback, dict) or feedback.get("blockReason"):
        raise ValueError("The model blocked the request.")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("Expected one model candidate.")
    candidate = candidates[0]
    if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
        raise ValueError("Incomplete model response.")
    ratings = candidate.get("safetyRatings", [])
    if not isinstance(ratings, list) or any(
        not isinstance(rating, dict) or rating.get("blocked") for rating in ratings
    ):
        raise ValueError("The model blocked the response.")
    content = candidate.get("content")
    if not isinstance(content, dict) or content.get("role") != "model":
        raise ValueError("Missing model content.")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise ValueError("Missing model parts.")
    texts = []
    for part in parts:
        if not isinstance(part, dict) or set(part) - {"text", "thought", "thoughtSignature"}:
            raise ValueError("Unexpected model content.")
        if not isinstance(part.get("text"), str):
            raise ValueError("Missing model text.")
        if "thought" in part and not isinstance(part["thought"], bool):
            raise ValueError("Invalid model thought flag.")
        if not part.get("thought", False):
            texts.append(part["text"])
    return texts


def _chat_completions_text(response):
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Expected one model choice.")
    choice = choices[0]
    if (
        not isinstance(choice, dict)
        or type(choice.get("index")) is not int
        or choice["index"] != 0
        or choice.get("finish_reason") != "stop"
    ):
        raise ValueError("Incomplete model response.")
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.get("role") != "assistant"
        or any(
            message.get(field) is not None for field in ("refusal", "tool_calls", "function_call")
        )
    ):
        raise ValueError("Model refusal or unexpected content.")
    return [message.get("content")]


def extract_text(provider, response, *, route="direct"):
    if not isinstance(response, dict) or response.get("error"):
        raise ValueError("Invalid model response.")
    if route == "token_station":
        texts = _chat_completions_text(response)
    elif route == "direct":
        readers = {"openai": _openai_text, "anthropic": _anthropic_text, "gemini": _gemini_text}
        texts = readers[provider](response)
    else:
        raise ValueError("Unknown model route.")
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise ValueError("Expected one model decision.")
    return texts[0]
