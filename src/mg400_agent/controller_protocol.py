"""Parse controller replies without opening connections or sending commands."""

import math
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
    expected_echo = re.sub(r"\s+", "", command)
    if expected_echo.startswith("CalcUser("):
        # Some MG400 firmware puts CalcUser values after the empty payload braces.
        match = re.fullmatch(
            r"(-?\d+),\{([^{};]*)\},((?:[^{}();]*,)?)\s*(CalcUser\s*\([^;]*\));",
            response,
            re.DOTALL,
        )
        if not match or re.sub(r"\s+", "", match[4]) != expected_echo:
            raise RuntimeError(f"Unexpected controller response: {response}")
        if int(match[1]) != 0:
            raise ControllerCommandError(int(match[1]), command, response)
        inside, outside = match[2].strip(), match[3].strip()
        payload = outside[:-1].strip() if outside else inside
        values = payload.split(",")
        number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
        if (
            (inside and outside)
            or len(values) != 4
            or not all(
                re.fullmatch(number, value.strip()) and math.isfinite(float(value))
                for value in values
            )
        ):
            raise RuntimeError(f"Invalid CalcUser coordinates: {response}")
        return payload
    match = re.fullmatch(r"(-?\d+),\{(.*)\},(.*);", response, re.DOTALL)
    if not match or re.sub(r"\s+", "", match[3]) != expected_echo:
        raise RuntimeError(f"Unexpected controller response: {response}")
    if int(match[1]) != 0:
        raise ControllerCommandError(int(match[1]), command, response)
    return match[2].strip()
