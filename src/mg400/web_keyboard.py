"""Keyboard input rules for the shared panel core."""

KEYS = frozenset("WASDQERFZX")
# Z and X run 1 suction action per press: the action ends by itself, not on key release.
VACUUM_KEYS = {"Z": "suck", "X": "release"}
KEY_LEASE_SECONDS = 0.4
KEYBOARD_JOIN_SECONDS = 12.0
MAX_SEQUENCE = 2**53
# Client events that turn keys off, with the reason shown to the operator.
DISARM_REASONS = {
    "user": None,
    "focus": "window lost focus.",
    "edit": "target editing.",
    "speed": "speed editing. Click Keyboard to apply the speed.",
    "modifier": "shortcut modifier is active.",
}


def sequence_number(value):
    if type(value) is not int or not 0 <= value <= MAX_SEQUENCE:
        raise ValueError("Key sequence must be an integer from 0 to 2^53")
    return value


def held_keys(value):
    if (
        not isinstance(value, list)
        or len(value) > len(KEYS)
        or any(not isinstance(key, str) or key not in KEYS for key in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("Held keys must be a list of unique keyboard keys")
    return frozenset(value)


def speed_percent(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("Keyboard speed must be an integer from 1 to 100")
    return value
