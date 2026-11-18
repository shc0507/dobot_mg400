"""Measured Insert pulse scales for fixed eighth-step transfer movements."""

import hashlib
import json
import math
from statistics import median

MICROSTEP = 8
# 400 full steps/revolution, 5 mm screw lead, and a direct motor coupling.
NOMINAL_PULSES_PER_MM = 400 * MICROSTEP / 5
MIN_SCALE = NOMINAL_PULSES_PER_MM * 0.75
MAX_SCALE = NOMINAL_PULSES_PER_MM * 1.25


def valid_scale(value):
    return type(value) in (int, float) and math.isfinite(value) and MIN_SCALE <= value <= MAX_SCALE


class MotionScaleStore:
    """Reuse atomic profile storage, but keep motor measurements out of profiles."""

    def __init__(self, profiles):
        self.profiles = profiles
        self.path = profiles.root / "motion-scale.json"

    @staticmethod
    def key(source, port, direction):
        binding = {
            "camera": source["camera"],
            "device_id": source["device_id"],
            "profile_id": source["profile_id"],
            "port": port,
            "microstep": MICROSTEP,
            "direction": direction,
        }
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    def _load(self):
        try:
            with self.path.open("rb") as stream:
                data = json.loads(stream.read(32769))
            if data.get("schema_version") == 1 and isinstance(data.get("scales"), dict):
                return data["scales"]
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def get(self, key):
        with self.profiles.lock:
            value = self._load().get(key)
        return float(value) if valid_scale(value) else None

    def save(self, key, value):
        if not valid_scale(value):
            raise ValueError("Measured Insert pulse scale does not match the motor and screw.")
        with self.profiles.lock:
            scales = self._load()
            scales.pop(key, None)
            scales[key] = float(value)
            scales = dict(list(scales.items())[-64:])
            self.profiles._write(self.path, {"schema_version": 1, "scales": scales})


class PulseScale:
    """Use motor geometry first and confirm measured corrections over useful travel."""

    def __init__(self, cached=None):
        self.cached = cached if valid_scale(cached) else None
        self.value = NOMINAL_PULSES_PER_MM
        self.verified = False
        self.samples = []

    @property
    def source(self):
        return "measured" if len(self.samples) >= 2 else "verified" if self.verified else "nominal"

    def observe(self, pulses, displacement):
        if displacement < 1.5:
            return False
        value = pulses / displacement
        if not valid_scale(value):
            raise ValueError(
                "Measured Insert travel does not match 640 pulses/mm at 1/8 step. "
                "Check the screw, coupling and Position Finder calibration."
            )
        self.samples.append(value)
        self.samples = self.samples[-5:]
        self.verified = True
        if len(self.samples) == 1:
            self.value = (
                self.cached if self.cached and abs(self.cached / value - 1) <= 0.25 else value
            )
        else:
            self.value = median(self.samples)
        return len(self.samples) >= 2 and max(self.samples) / min(self.samples) <= 1.25


def movement(error, scale):
    """Choose a pulse batch and speed from distance; never change stepping mode."""
    remaining = abs(error)
    if not scale.verified:
        distance, speed = min(2.0, remaining * 0.75), 400
    elif remaining > 10:
        distance, speed = min(10.0, remaining * 0.75), 1000
    elif remaining > 2:
        distance, speed = remaining * 0.65, 500
    else:
        distance, speed = remaining * 0.65, 200
    pulses = max(1, round(distance * scale.value))
    return pulses, speed


def move_seconds(pulses, speed, acceleration):
    """Bound completion using the firmware's symmetric acceleration and deceleration."""
    acceleration = acceleration if type(acceleration) in (int, float) and acceleration > 0 else 50
    ramp_distance = speed * speed / acceleration
    duration = (
        2 * math.sqrt(pulses / acceleration)
        if pulses <= ramp_distance
        else pulses / speed + speed / acceleration
    )
    # Include the stage cooldown, serial feedback and scheduling margin.
    return max(10.0, duration + 5.0)
