"""Reduce explicit single-device requests without hiding that device's fields."""

import copy
import re


def model_context(context):
    """Keep all context when the request can refer to another device or old context."""
    text = context.get("user_message", "").strip().lower()
    scope = None
    # An exact allowlist keeps aliases, comparisons and follow-up requests complete.
    number = r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
    queries = {
        "stage": r"what (?:is|are) (?:the )?(?:current )?stage (?:position|coordinates)[?.]?",
        "robot": r"what is (?:the )?(?:current )?robot (?:position|pose)[?.]?",
    }
    moves = {
        "stage": rf"move (?:the )?(?:x|y|insert) stage to {number} rotations?[.!]?",
        "robot": rf"move (?:the )?robot (?:up|down) by {number} mm at {number}% speed[.!]?",
    }
    for device in queries:
        if re.fullmatch(queries[device], text) or re.fullmatch(moves[device], text):
            scope = device
    value = copy.deepcopy(context)
    if scope:
        devices = value.get("state", {}).get("devices", {})
        value["state"]["devices"] = {key: row for key, row in devices.items() if key == scope}
        value["commands"] = [row for row in value["commands"] if row.get("device") == scope]
    value["context_scope"] = {
        "devices": [scope] if scope else "all",
        "note": "Omitted devices are not evidence. Ask for a new request if they are needed. "
        "Conversation history is historical. Use only the current state for current readings.",
    }
    return value
