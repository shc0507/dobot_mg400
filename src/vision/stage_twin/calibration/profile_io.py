# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/calibration/profile_io.py
"""Bounded JSON input for calibration profiles and visualizer preferences."""

import json

MAX_PROFILE_BYTES = 1024 * 1024


def read_profile_json(payload):
    """Parse bounded JSON, rejecting JavaScript's nonfinite constants."""
    if len(payload) > MAX_PROFILE_BYTES:
        raise ValueError("The calibration profile exceeds the 1 MiB limit.")

    def invalid_constant(value):
        raise ValueError(f"Profile numbers must be finite; found {value}.")

    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Calibration profile contains a duplicate field: {key}.")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload, parse_constant=invalid_constant, object_pairs_hook=unique_fields
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Choose a readable calibration profile JSON file.") from exc
    if not isinstance(value, dict):
        raise ValueError("A calibration profile must be a JSON object.")
    return value
