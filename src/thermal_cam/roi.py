"""Named rectangular regions in native P1 sensor coordinates."""

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_ROI_PATH = Path("/var/lib/mg400/thermal/rois.toml")
COORDINATES = {"x0", "y0", "x1", "y1"}


@dataclass(frozen=True)
class Region:
    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self):
        if any(type(value) is not int for value in asdict(self).values()):
            raise ValueError("Region coordinates must be integers.")
        if not (0 <= self.x0 < self.x1 <= 160 and 0 <= self.y0 < self.y1 <= 120):
            raise ValueError(
                "Region bounds must satisfy 0 <= x0 < x1 <= 160 and 0 <= y0 < y1 <= 120."
            )

    def bounds(self):
        return asdict(self)


def load_regions(path=DEFAULT_ROI_PATH):
    """Read once at service construction. An absent file defines no named regions."""
    try:
        with Path(path).open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read thermal regions from {path}: {exc}") from exc
    if set(data) - {"regions"} or not isinstance(data.get("regions", {}), dict):
        raise ValueError("The thermal region file must contain only a regions table.")
    result = {}
    for name, bounds in data.get("regions", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Region names must contain 1 to 64 ASCII letters, digits, _ or -.")
        if not isinstance(bounds, dict) or set(bounds) != COORDINATES:
            raise ValueError(f"Region {name!r} must define exactly x0, y0, x1 and y1.")
        try:
            result[name] = Region(**bounds)
        except ValueError as exc:
            raise ValueError(f"Invalid region {name!r}: {exc}") from exc
    return result


def select_region(query, regions):
    """Reject ambiguous selectors before the caller obtains a camera frame."""
    pairs = list(query)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("Region query parameters must occur exactly once.")
    if set(values) == {"name"}:
        name = values["name"]
        if name not in regions:
            raise ValueError("Unknown thermal region name.")
        return name, regions[name]
    if set(values) != COORDINATES:
        raise ValueError("Specify either name or all of x0, y0, x1 and y1.")
    if any(not value.isascii() or not value.isdecimal() for value in values.values()):
        raise ValueError("Region coordinates must be nonnegative integers.")
    return None, Region(**{key: int(value) for key, value in values.items()})
