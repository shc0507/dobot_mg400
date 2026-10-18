"""Read the robot address from an explicit TOML configuration file."""

import tomllib
from pathlib import Path


def load_robot_host(path: str | Path) -> str:
    """Return the configured host without opening a network connection."""
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    try:
        host = data["robot"]["host"]
    except (KeyError, TypeError) as error:
        raise ValueError("Robot configuration must contain [robot].host") from error
    if not isinstance(host, str) or not host or any(character.isspace() for character in host):
        raise ValueError("Robot host must be a nonempty string without whitespace")
    return host
