"""Numeric input validation for control and simulation."""

import math


def finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a finite number")
    if not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return float(value)
