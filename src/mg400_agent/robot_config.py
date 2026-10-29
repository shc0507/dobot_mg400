"""Read the robot address from the environment or an explicit TOML configuration file."""

import os
import tomllib
from pathlib import Path


def load_robot_host(path: str | Path) -> str:
    """Return MG400_ROBOT_HOST, else [robot].host from the file. Open no network connection.

    An empty MG400_ROBOT_HOST counts as unset.
    """
    host = os.environ.get("MG400_ROBOT_HOST")
    if not host:
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
        try:
            host = data["robot"]["host"]
        except (KeyError, TypeError) as error:
            raise ValueError("Robot configuration must contain [robot].host") from error
    if not isinstance(host, str) or not host or any(character.isspace() for character in host):
        raise ValueError("Robot host must be a nonempty string without whitespace")
    return host
