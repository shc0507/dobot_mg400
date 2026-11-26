"""Build compact state from the shared cache without device requests."""

import copy
import math

from .stage_reference import stage_reference
from .state_fields import cached_fields


def _age(source_age, cache_age, divisor=1):
    if any(
        type(value) not in (int, float) or not math.isfinite(value)
        for value in (source_age, cache_age)
    ):
        return None
    return max(0, source_age / divisor) + max(0, cache_age)


def _fields(source, names):
    return {name: copy.deepcopy(source[name]) for name in names if name in source}


def _command(command):
    if not isinstance(command, dict):
        return None
    result = _fields(command, ("id", "action", "result", "status", "started_at", "finished_at"))
    if isinstance(command.get("message"), str):
        result["message"] = command["message"][:512]
    return result


def _axis(motor, cache_age, cache_quality, revision):
    valid, fresh = motor.get("reference_valid"), motor.get("telemetry_fresh")
    quality = (
        cache_quality
        if cache_quality != "valid"
        else "uncalibrated"
        if valid is False
        else "stale"
        if fresh is False
        else "valid"
        if valid is True and fresh is True
        else "unknown"
    )
    return {
        **_fields(
            motor,
            (
                "mode",
                "remaining_rotations",
                "lower_limit_rotations",
                "upper_limit_rotations",
                "motion_ready",
                "fault",
            ),
        ),
        "id": motor.get("id"),
        "position_rotations": motor.get("position_rotations"),
        "unit": "rotation",
        "reference_valid": valid,
        "quality": quality,
        "reference_revision": revision,
        "telemetry_age_s": _age(motor.get("telemetry_age_ms"), cache_age, 1000),
        "encoder_age_s": _age(motor.get("encoder_age_ms"), cache_age, 1000),
    }


class CacheStateProvider:
    def __init__(self, devices, *, samples=None, operations=None, observations=None, mode="live"):
        self.devices = devices
        self.samples_provider = samples
        self.operations_provider = operations
        self.observations_provider = observations
        self.mode = mode

    def __call__(self):
        rows = {}
        observations = self.observations_provider() if self.observations_provider else {}
        for name in tuple(self.devices.entries):
            cached = self.devices.get(name)
            state = cached.get("state") or {}
            phase = cached.get("phase")
            quality = (
                "valid" if phase == "ready" else "stale" if phase == "stale" else "unavailable"
            )
            age = cached.get("age_s")
            values = _fields(state, ("connected", "busy", "locked", "fault", "reference_revision"))
            values["last_command"] = _command(state.get("last_command"))
            if name == "robot":
                values.update(
                    _fields(
                        state,
                        (
                            "enabled",
                            "robot_mode",
                            "speed_factor",
                            "vacuum",
                            "stop_unconfirmed",
                            "robot_error",
                            "alarms",
                        ),
                    )
                )
                values.update(
                    joints_deg=copy.deepcopy(state.get("joints")),
                    pose_mm_deg=copy.deepcopy(state.get("actual")),
                    feedback_fresh=state.get("feedback_fresh"),
                )
                if quality == "valid" and state.get("feedback_fresh") is False:
                    quality = "stale"
            elif name == "stage":
                try:
                    values["stage_reference"] = stage_reference(
                        state, allow_legacy=self.mode == "live" and state.get("demo") is False
                    )
                except (AttributeError, TypeError, ValueError):
                    values["stage_reference"] = None
                values["axes"] = [
                    _axis(motor, age, quality, state.get("reference_revision"))
                    for motor in state.get("motors", [])
                ]
            elif name == "furnace":
                source = state.get("furnace") or state.get("snapshot") or state
                values.update(
                    pv_c=source.get("pv_c", source.get("pv")),
                    sp_c=source.get("sp_c", source.get("target_c", source.get("sv"))),
                )
                values.update(
                    _fields(source, ("run_state", "control_mode", "ramp_c_per_min", "flow_pct"))
                )
                if "ramp_c_per_min" not in values:
                    values["ramp_c_per_min"] = source.get("ramp")
                values["sequence"] = _fields(
                    source.get("sequence") or {},
                    ("running", "current_index", "phase", "hold_remaining_seconds"),
                )
            elif name == "microscope":
                values.update(
                    _fields(
                        state,
                        (
                            "objective_id",
                            "objective_offset_steps",
                            "focus_position_pulses",
                            "focus_zero_known",
                            "focus_limits_pulses",
                            "focus_travel_limit_pulses",
                            "focus_reference",
                            "turret_profile",
                            "visual_focus",
                            "autofocus_run",
                            "autofocus_settings",
                            "motor_power",
                            "cameras",
                        ),
                    )
                )
                values["recording"] = _fields(
                    state.get("recording") or {},
                    ("id", "status", "mode", "frame_count", "started_at", "finished_at"),
                )
            elif name == "vision":
                values.update(_fields(state, ("model_loaded", "calibrated_cameras")))
                values["last_result"] = _fields(
                    state.get("last_result") or {},
                    ("instance", "job_id", "camera", "action", "count", "finished_at"),
                )
            elif name == "thermal":
                values.update(_fields(state, ("state", "frames", "fps")))
            rows[name] = {
                "quality": quality,
                # Stage sampled_at is monotonic assembly time, not Unix capture time.
                "sampled_at": None
                if name == "stage"
                else state.get("sampled_at", state.get("sample_time")),
                "state_at": state.get("ts_unix"),
                "observed_at": cached.get("updated_at"),
                "age_s": age,
                "frame_age_s": _age(state.get("frame_age_s"), age),
                "instance": state.get("instance"),
                "values": values,
                "cached_state": cached_fields(state, name),
                "data_gaps": [
                    field
                    for field, missing in (
                        ("source_instance", not state.get("instance")),
                        ("sensor_capture_time", state.get("capture_timestamp") is None),
                        ("state_time", state.get("ts_unix") is None),
                    )
                    if missing
                ],
            }
            if name in observations:
                detail = observations[name]
                if detail.get("instance") != state.get("instance"):
                    detail = {
                        **detail,
                        "available": False,
                        "quality": "unavailable",
                        "reason": "instance_changed",
                    }
                rows[name]["observations"] = detail
        return {
            "mode": self.mode,
            "devices": rows,
            "operations": self.operations_provider()
            if self.operations_provider
            else {"items": [], "count": 0, "complete": True},
        }

    def samples(self, **kwargs):
        if self.samples_provider:
            return self.samples_provider(**kwargs)
        return {
            "revision": 0,
            "selection": kwargs.get("selection", "active"),
            "items": [],
            "count": 0,
            "total": 0,
            "complete": True,
            "next_offset": None,
        }
