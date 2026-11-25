"""Normalize numeric provider usage without including response text or credentials."""

import math


def _tokens(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _sum_fields(usage, first, *optional):
    values = [_tokens(usage.get(first))] + [_tokens(usage.get(key, 0)) for key in optional]
    return sum(values) if all(value is not None for value in values) else None


def usage_report(profile, response, elapsed_ms, status):
    """Use null for unavailable usage. Never estimate provider billing."""
    response = response if isinstance(response, dict) else {}
    provider, route = profile["provider"], profile["route"]
    usage = response.get("usageMetadata" if provider == "gemini" and route == "direct" else "usage")
    usage = usage if isinstance(usage, dict) else {}
    total = _tokens(usage.get("total_tokens"))
    cost = None
    if route == "token_station":
        input_tokens = _tokens(usage.get("prompt_tokens"))
        output_tokens = _tokens(usage.get("completion_tokens"))
        # Only an explicitly denominated value can be displayed as US dollars.
        value = usage.get("cost_usd")
        if type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value):
            cost = value
    elif provider == "anthropic":
        input_tokens = _sum_fields(
            usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
        )
        output_tokens = _tokens(usage.get("output_tokens"))
    elif provider == "gemini":
        input_tokens = _sum_fields(usage, "promptTokenCount", "toolUsePromptTokenCount")
        output_tokens = _sum_fields(usage, "candidatesTokenCount", "thoughtsTokenCount")
        total = _tokens(usage.get("totalTokenCount"))
    else:
        input_tokens = _tokens(usage.get("input_tokens"))
        output_tokens = _tokens(usage.get("output_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    return {
        "provider": provider,
        "model": profile["model"],
        "route": route,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "cost_usd": cost,
        "elapsed_ms": max(0, round(elapsed_ms)),
        "status": status,
    }


def notify(callback, *args):
    """Keep optional reporting failures separate from model decisions."""
    if callback is not None:
        try:
            callback(*args)
        except Exception:
            pass
