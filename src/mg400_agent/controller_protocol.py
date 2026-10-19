"""Parse controller replies without opening connections or sending commands."""

import re


class ControllerCommandError(RuntimeError):
    """A complete controller reply rejected the command and matched its echo."""

    def __init__(self, code, command, response):
        super().__init__(response)
        self.code = code
        self.command = command
        self.response = response


def parse_controller_response(packet: bytes | bytearray, command: str) -> str:
    """Return the payload only if the reply confirms success and echoes the command."""
    response = packet.decode("ascii").strip()
    match = re.fullmatch(r"(-?\d+),\{(.*)\},(.*);", response, re.DOTALL)
    if not match or re.sub(r"\s+", "", match[3]) != re.sub(r"\s+", "", command):
        raise RuntimeError(f"Unexpected controller response: {response}")
    if int(match[1]) != 0:
        raise ControllerCommandError(int(match[1]), command, response)
    return match[2].strip()
