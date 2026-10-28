"""Keyboard input rules for the shared panel core.

The frozen Isaac panel does not import this module.
"""

import time

from .web_gripper import GripperNotSentError

KEYS = frozenset("WASDQERFZX")
GRIPPER_KEYS = frozenset("ZX")
KEY_LEASE_SECONDS = 0.4
KEYBOARD_JOIN_SECONDS = 12.0
GRIPPER_STEP = 25
GRIPPER_HOLD_SECONDS = 5.0
MAX_SEQUENCE = 2**53
# Client events that turn keys off, with the reason shown to the operator.
DISARM_REASONS = {
    "user": None,
    "focus": "window lost focus.",
    "edit": "target editing.",
    "speed": "speed editing. Click Keyboard to apply the speed.",
    "modifier": "shortcut modifier is active.",
}


class GripperUnavailableError(RuntimeError):
    """The gripper cannot start this step. No command was sent."""


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


def run_gripper_steps(
    gripper, read_state, key, released, *, set_active, hold_seconds=GRIPPER_HOLD_SECONDS
):
    """Step the gripper until release, contact, end of travel, or the hold limit.

    Blocking. Call on a keyboard worker without the panel lock. Each step waits for
    arrival or contact. Return "stopped", "contact" or "expired".
    """
    if gripper is None or gripper.closed:
        raise GripperUnavailableError("Gripper control is unavailable.")
    deadline = time.monotonic() + hold_seconds
    while not released.is_set() and time.monotonic() < deadline:
        read_state()
        if gripper.closed:
            raise GripperUnavailableError("Gripper control is unavailable.")
        if gripper.failed:
            raise RuntimeError(gripper.snapshot()["message"])
        position, reason = gripper.measure()
        if position is None:
            raise GripperUnavailableError(reason or "Waiting for fresh gripper position feedback.")
        target = max(0, min(1000, position + (GRIPPER_STEP if key == "Z" else -GRIPPER_STEP)))
        if target == position or released.is_set():
            return "stopped"
        set_active(True)
        try:
            gripper.move(target)
        except GripperNotSentError as error:
            if released.is_set():
                return "stopped"
            raise GripperUnavailableError(str(error)) from error
        finally:
            set_active(False)
        if gripper.last_outcome == "contact":
            return "contact"
    return "stopped" if released.is_set() else "expired"
