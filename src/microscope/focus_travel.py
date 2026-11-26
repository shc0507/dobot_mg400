"""Persistent manual focus travel cap and per-lens autofocus search ranges."""

import json
import os
import tempfile
from pathlib import Path

from labkit.errors import ServiceError

DEFAULT_FOCUS_TRAVEL = 20000
MAX_FOCUS_TRAVEL = 2**31 - 1
DEFAULT_FIVE_X_HALF_RANGE = 5000
MAX_HALF_RANGE = 1_000_000
AUTOFOCUS_RANGE_FIELDS = (
    "five_x_half_range_pulses",
    "ten_x_half_range_pulses",
    "twenty_x_half_range_pulses",
    "fifty_x_half_range_pulses",
)
DEFAULT_AUTOFOCUS_RANGES = dict(
    zip(AUTOFOCUS_RANGE_FIELDS, (DEFAULT_FIVE_X_HALF_RANGE, 5000, 5000, 3000))
)


def validate_travel(value):
    if type(value) is not int or not 1 <= value <= MAX_FOCUS_TRAVEL:
        raise ValueError(f"Focus travel must be an integer from 1 to {MAX_FOCUS_TRAVEL} pulses")
    return value


class FocusTravelMixin:
    def _autofocus_range_values(self):
        return {
            name: getattr(self, "_" + name.removesuffix("_pulses"))
            for name in AUTOFOCUS_RANGE_FIELDS
        }

    def _apply_autofocus_ranges(self, values):
        for name, value in values.items():
            setattr(self, "_" + name.removesuffix("_pulses"), value)

    def _load_autofocus_settings(self):
        path = self._turret_store.root.parent / "autofocus-settings.json"
        try:
            with path.open("rb") as source:
                encoded = source.read(513)
        except FileNotFoundError:
            self._apply_autofocus_ranges(DEFAULT_AUTOFOCUS_RANGES)
            return
        if len(encoded) > 512:
            raise ValueError("Stored autofocus settings are too large")
        values = DEFAULT_AUTOFOCUS_RANGES | self._validate_autofocus_settings(json.loads(encoded))
        self._apply_autofocus_ranges(values)

    @staticmethod
    def _validate_autofocus_settings(args):
        if not isinstance(args, dict) or not args or set(args) - set(AUTOFOCUS_RANGE_FIELDS):
            raise ValueError("Provide at least one known lens autofocus half-range")
        for name, value in args.items():
            if type(value) is not int or not 4 <= value <= MAX_HALF_RANGE:
                raise ValueError(f"{name} must be an integer from 4 to {MAX_HALF_RANGE} pulses")
        return dict(args)

    def autofocus_settings(self):
        with self._lock:
            return {
                **self._autofocus_range_values(),
                "minimum_half_range_pulses": 4,
                "maximum_half_range_pulses": MAX_HALF_RANGE,
            }

    def set_autofocus_settings(self, args, client):
        patch = self._validate_autofocus_settings(args)
        with self._lease.control(client):
            with self._lock:
                if self._job is not None or self._turret_session is not None or self._stopping:
                    raise ServiceError(
                        409, "Stop microscope work before changing autofocus settings"
                    )
                values = self._autofocus_range_values() | patch
                parent = self._turret_store.root.parent
                parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                descriptor, temporary = tempfile.mkstemp(prefix=".autofocus-", dir=parent)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        json.dump(values, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, parent / "autofocus-settings.json")
                finally:
                    Path(temporary).unlink(missing_ok=True)
                self._apply_autofocus_ranges(values)
                self._seq += 1
        return self.autofocus_settings()

    def _load_focus_travel(self):
        path = self._turret_store.root.parent / "focus-travel-limit.json"
        try:
            with path.open("rb") as source:
                encoded = source.read(513)
        except FileNotFoundError:
            self._focus_travel_limit = DEFAULT_FOCUS_TRAVEL
            return
        if len(encoded) > 512:
            raise ValueError("Stored focus travel setting is too large")
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != {"pulses"}:
            raise ValueError("Stored focus travel setting must contain pulses")
        self._focus_travel_limit = validate_travel(value["pulses"])

    def focus_travel_limit(self):
        with self._lock:
            return {"focus_travel_limit_pulses": self._focus_travel_limit}

    def set_focus_travel_limit(self, args, client):
        if not isinstance(args, dict) or set(args) != {"pulses"}:
            raise ValueError("Focus travel setting requires pulses")
        pulses = validate_travel(args["pulses"])
        with self._lease.control(client):
            with self._lock:
                if self._job is not None or self._turret_session is not None or self._stopping:
                    raise ServiceError(409, "Stop microscope work before changing the travel cap")
                parent = self._turret_store.root.parent
                parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                descriptor, temporary = tempfile.mkstemp(prefix=".focus-travel-", dir=parent)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        json.dump({"pulses": pulses}, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, parent / "focus-travel-limit.json")
                finally:
                    Path(temporary).unlink(missing_ok=True)
                self._focus_travel_limit = pulses
                self._seq += 1
        return self.focus_travel_limit()

    def _reserve_focus_travel(self, job, steps):
        self._check_job(job)
        if type(steps) is not int:
            raise ValueError("Focus steps must be an integer")
        with self._lock:
            limit = getattr(job, "focus_travel_limit", self._focus_travel_limit)
            used = getattr(job, "focus_travel_used", 0)
            if used + abs(steps) > limit:
                raise ValueError(
                    f"Focus travel limit reached: {used} of {limit} pulses used; "
                    f"the next move needs {abs(steps)} pulses"
                )
            job.focus_travel_used = used + abs(steps)
            self._visual_focus.update(
                travel_used_pulses=job.focus_travel_used, travel_limit_pulses=limit
            )
            self._seq += 1
