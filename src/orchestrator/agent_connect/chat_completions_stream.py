"""Read a bounded Chat Completions stream before returning a complete response."""

import time


def _lines(chunks):
    line, first, skip_lf = bytearray(), True, False
    for raw in chunks:
        for char in raw:
            if skip_lf and char == 10:
                skip_lf = False
                continue
            skip_lf = False
            if char in (10, 13):
                yield line.decode("utf-8-sig" if first else "utf-8")
                line.clear()
                first, skip_lf = False, char == 13
            else:
                line.append(char)
    if line:
        raise ValueError("Incomplete model stream.")


def _events(chunks):
    data, event = [], ""
    for line in _lines(chunks):
        if not line:
            if event not in {"", "message"}:
                raise ValueError("Unexpected model event.")
            if data:
                yield "\n".join(data)
            data, event = [], ""
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
    if data or event:
        raise ValueError("Incomplete model event.")


def _parse_stream(chunks, *, loads):
    """Use only content from choice 0. Require stop and the final DONE event."""
    content, finished, response_id = [], False, None
    for data in _events(chunks):
        if data == "[DONE]":
            if not finished or not any(content):
                raise ValueError("Incomplete model response.")
            return {
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "".join(content)},
                    }
                ]
            }
        chunk = loads(data)
        if not isinstance(chunk, dict) or chunk.get("error") is not None:
            raise ValueError("Invalid model stream chunk.")
        if "id" in chunk:
            if not isinstance(chunk["id"], str) or not chunk["id"]:
                raise ValueError("Invalid model response ID.")
            if response_id is not None and response_id != chunk["id"]:
                raise ValueError("Multiple model responses.")
            response_id = chunk["id"]
        choices = chunk.get("choices")
        if finished and choices == [] and isinstance(chunk.get("usage"), dict):
            continue
        if finished or not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one unfinished model choice.")
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
        ):
            raise ValueError("Unexpected model choice.")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise ValueError("Invalid model content.")
        if any(
            delta.get(field) is not None
            for field in ("refusal", "tool_calls", "function_call", "audio")
        ):
            raise ValueError("Model refusal or unexpected content.")
        value = delta.get("content")
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("Invalid model text.")
            content.append(value)
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason != "stop":
                raise ValueError("Incomplete model response.")
            finished = True
    raise ValueError("Incomplete model stream.")


def read_stream(response, *, loads, max_bytes, timeout_s):
    """Stop reading at DONE. Bound the bytes and check the deadline between reads."""
    deadline = time.monotonic() + timeout_s

    def chunks():
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("The model stream exceeded its deadline.")
            raw = response.read1(min(4096, max_bytes - size + 1))
            if time.monotonic() >= deadline:
                raise TimeoutError("The model stream exceeded its deadline.")
            if not raw:
                return
            size += len(raw)
            if size > max_bytes:
                raise ValueError("The model stream is too large.")
            yield raw

    return _parse_stream(chunks(), loads=loads)
