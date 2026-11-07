"""Vacuum suction cup on the Dobot mini vacuum source, driven by MG400 base DOs.

The vacuum source runs in Auto mode. Its Vacuum input sucks and its Pressure
input blows. Never set both high: the vacuum source does not specify that state.
"""

# Base I/O block. The owner wired Pressure to DO1 and Vacuum to DO2.
PRESSURE_DO = 1
VACUUM_DO = 2

# Release blows for this time, then stops. Tune it on the bench.
BLOW_SECONDS = 0.3

# Grip switch input on the base I/O block. None until the vacuum switch is installed.
# Then set it to the DI number (the plan proposes DI1), and decode_state reports `held`.
GRIP_DI = None

ACTIONS = ("suck", "release", "off")

# Each step is (DO index, status) or ("wait", seconds). The order keeps both outputs
# from being high at the same time.
STEPS = {
    "suck": ((PRESSURE_DO, 0), (VACUUM_DO, 1)),
    "release": ((VACUUM_DO, 0), (PRESSURE_DO, 1), ("wait", BLOW_SECONDS), (PRESSURE_DO, 0)),
    "off": ((VACUUM_DO, 0), (PRESSURE_DO, 0)),
}


def command_text(index, status):
    """Return the immediate dashboard command that sets one base DO."""
    if type(index) is not int or not 1 <= index <= 16 or status not in (0, 1):
        raise ValueError("DO index must be 1..16 and status 0 or 1")
    return f"DOExecute({index},{status})"


def bit(field, number):
    """Return the level of DO or DI number from a 30004 DigitalOutputs or DigitalInputs field."""
    return bool(field >> (number - 1) & 1)


def decode_state(outputs, inputs):
    """Return the measured vacuum state from the 30004 DO and DI fields."""
    if outputs is None:
        return {"suck": None, "blow": None, "held": None}
    return {
        "suck": bit(outputs, VACUUM_DO),
        "blow": bit(outputs, PRESSURE_DO),
        "held": None if GRIP_DI is None or inputs is None else bit(inputs, GRIP_DI),
    }
