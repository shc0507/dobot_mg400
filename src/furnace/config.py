"""Read furnace settings without opening a device.

Set MG400_FURNACE_SERIAL and MG400_MFC_SERIAL to stable /dev/serial/by-id/
paths. Neither port has a default. Explicit port arguments take precedence.
MG400_FURNACE_SLAVE defaults to 1. MG400_FURNACE_MAX_C and
MG400_FURNACE_MAX_RAMP default to 1000 and 100. Lower caps are permitted.
Caps must be finite and positive, and cannot exceed these defaults.
"""

import math
import os
from dataclasses import dataclass

MAX_TEMP = 1000
MAX_RAMP = 100


def serial_port(value: str | None, setting: str, *, required: bool = True) -> str | None:
    """Resolve one explicit port or environment setting without opening it."""
    if value is None:
        value = os.environ.get(setting) or None
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"Set {setting} or supply a nonempty serial port")
    return value


def resolve_slave_address(value: object = None) -> int:
    """Resolve and validate one explicit slave address or its environment setting."""
    if value is None:
        value = os.environ.get("MG400_FURNACE_SLAVE", "1")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("MG400_FURNACE_SLAVE must be an integer from 1 to 247") from error
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
        raise ValueError("MG400_FURNACE_SLAVE must be an integer from 1 to 247")
    if not 1 <= number <= 247:
        raise ValueError("MG400_FURNACE_SLAVE must be an integer from 1 to 247")
    return int(number)


def _cap(value: object, setting: str, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{setting} must be finite, positive, and at most {maximum}") from error
    if isinstance(value, bool) or not math.isfinite(number) or not 0 < number <= maximum:
        raise ValueError(f"{setting} must be finite, positive, and at most {maximum}")
    return number


@dataclass(frozen=True)
class FurnaceConfig:
    """Validated settings for one furnace and its separate flow controller."""

    port: str | None = None
    slave_address: int = 1
    flow_port: str | None = None
    max_c: float = MAX_TEMP
    max_ramp: float = MAX_RAMP

    def __post_init__(self) -> None:
        for name in ("port", "flow_port"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or "\x00" in value
            ):
                raise ValueError(f"{name} must be a nonempty serial port")
        object.__setattr__(self, "slave_address", resolve_slave_address(self.slave_address))
        object.__setattr__(self, "max_c", _cap(self.max_c, "MG400_FURNACE_MAX_C", MAX_TEMP))
        object.__setattr__(
            self, "max_ramp", _cap(self.max_ramp, "MG400_FURNACE_MAX_RAMP", MAX_RAMP)
        )

    @classmethod
    def from_env(
        cls,
        *,
        port: str | None = None,
        slave_address: int | None = None,
        flow_port: str | None = None,
    ) -> "FurnaceConfig":
        """Read settings now. Explicit arguments override their environment values."""
        return cls(
            port=serial_port(port, "MG400_FURNACE_SERIAL", required=False),
            slave_address=(
                os.environ.get("MG400_FURNACE_SLAVE", "1")
                if slave_address is None
                else slave_address
            ),
            flow_port=serial_port(flow_port, "MG400_MFC_SERIAL", required=False),
            max_c=os.environ.get("MG400_FURNACE_MAX_C", str(MAX_TEMP)),
            max_ramp=os.environ.get("MG400_FURNACE_MAX_RAMP", str(MAX_RAMP)),
        )
