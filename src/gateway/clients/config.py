"""Explicit loopback settings for clients with no operator credentials."""

import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from labkit.auth import loopback_name, valid_token


def loopback_url(value, *, path, setting):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname is not None
            and loopback_name(parsed.hostname)
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.path == path
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in value)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f"{setting} must be an HTTP loopback URL with a port and path {path}.")
    return value


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    gateway_token: str = field(repr=False)
    model_id: str
    model_url: str
    model_key: str = field(repr=False)
    timeout: float

    @classmethod
    def from_env(cls, env):
        """Read only these example settings, never downstream service credentials."""
        token = env.get("MG400_GATEWAY_TOKEN", "")
        if not isinstance(token, str) or len(token) < 24 or not valid_token(token):
            raise ValueError("MG400_GATEWAY_TOKEN requires at least 24 printable ASCII characters.")
        model = env.get("QUMUS_DEFAULT_MODEL_ID", "").strip()
        if not model or len(model) > 200 or any(ord(char) < 32 for char in model):
            raise ValueError("QUMUS_DEFAULT_MODEL_ID must name the model served at QUMUS_API_BASE.")
        key = env.get("QUMUS_API_KEY", "unused")
        if not key or not isinstance(key, str) or not valid_token(key) or key == token:
            raise ValueError("QUMUS_API_KEY must be a separate model credential or unused.")
        try:
            timeout = float(env.get("QUMUS_LLM_TIMEOUT", "60"))
        except (TypeError, ValueError):
            timeout = float("nan")
        if not math.isfinite(timeout) or not 1 <= timeout <= 300:
            raise ValueError("QUMUS_LLM_TIMEOUT must be from 1 through 300 seconds.")
        if env.get("QUMUS_LLM_RETRIES", "0") != "0":
            raise ValueError("QUMUS_LLM_RETRIES must be 0 for this example.")
        return cls(
            loopback_url(
                env.get("MG400_GATEWAY_URL", "http://127.0.0.1:8773/mcp"),
                path="/mcp",
                setting="MG400_GATEWAY_URL",
            ),
            token,
            model,
            loopback_url(
                env.get("QUMUS_API_BASE", "http://127.0.0.1:8000/v1"),
                path="/v1",
                setting="QUMUS_API_BASE",
            ),
            key,
            timeout,
        )
