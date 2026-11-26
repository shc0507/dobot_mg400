"""Production read-only Agent Connect definitions. Runtime data never enters the bundle."""

import copy
import json

from .contract import DIALECT, digest

PREFIX = "/lab/api/agent"
GROUPS = ("devices", "samples", "operations")
QUALITY = ("valid", "stale", "unavailable", "uncalibrated", "unknown")


def bundle():
    """Return the complete version 1 read-only definition bundle."""
    scalar = {"type": ["number", "null"]}
    quality = {"enum": list(QUALITY)}
    nullable_text = {"type": ["string", "null"]}
    nullable_bool = {"type": ["boolean", "null"]}
    axis = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "integer", "null"]},
            "position_rotations": scalar,
            "unit": {"const": "rotation"},
            "reference_valid": nullable_bool,
            "reference_revision": {"type": ["integer", "null"]},
            "quality": quality,
            "telemetry_age_s": scalar,
            "encoder_age_s": scalar,
        },
    }
    location = {
        "type": "object",
        "required": ["holder"],
        "properties": {
            "holder": {"enum": ["unknown", "tray_slot", "robot", "stage_insert"]},
            "tray_id": nullable_text,
            "slot_id": nullable_text,
            "container_id": nullable_text,
            "inside_furnace": nullable_bool,
        },
    }
    sample = {
        "type": "object",
        "required": ["chip_id", "location", "basis", "revision"],
        "properties": {
            "chip_id": {"type": "string"},
            "location": location,
            "basis": {"enum": ["unknown", "observed", "inferred", "reported"]},
            "revision": {"type": "integer"},
            "updated_at": scalar,
            "last_observation": {"type": ["object", "null"]},
            "last_known_location": {"type": ["object", "null"]},
            "last_run": {"type": ["object", "null"]},
            "last_step": {"type": ["object", "null"]},
            "pending_operation": {"type": ["object", "null"]},
            "conflict": {"type": ["object", "string", "null"]},
            "passport_url": {"type": "string"},
        },
    }
    sample_page = {
        "type": "object",
        "required": ["items", "count", "total", "complete", "selection"],
        "properties": {
            "items": {"type": "array", "items": sample},
            "count": {"type": "integer", "minimum": 0},
            "total": {"type": "integer", "minimum": 0},
            "complete": {"type": "boolean"},
            "selection": {"enum": ["active", "ids", "all"]},
            "next_offset": {"type": ["integer", "null"]},
            "revision": {"type": "integer"},
        },
    }
    device = {
        "type": "object",
        "required": ["quality", "sampled_at", "age_s", "instance", "values"],
        "properties": {
            "quality": quality,
            "sampled_at": scalar,
            "age_s": scalar,
            "instance": {"type": ["string", "null"]},
            "state_at": scalar,
            "observed_at": scalar,
            "frame_age_s": scalar,
            "cached_state": {
                "type": "object",
                "required": ["values", "complete", "omitted", "omitted_count", "units"],
                "properties": {
                    "values": {"type": "object"},
                    "complete": {"type": "boolean"},
                    "omitted_count": {"type": "integer", "minimum": 0},
                    "omitted": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "required": ["path", "reason"],
                            "properties": {
                                "path": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                    "units": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
            "data_gaps": {"type": "array", "items": {"type": "string"}},
            "values": {
                "type": "object",
                "properties": {
                    "connected": nullable_bool,
                    "busy": nullable_bool,
                    "pv_c": scalar,
                    "sp_c": scalar,
                    "temperature_c": scalar,
                    "joints_deg": {"type": ["array", "null"], "items": scalar},
                    "pose_mm_deg": {"type": ["array", "null"], "items": scalar},
                    "feedback_fresh": nullable_bool,
                    "axes": {"type": "array", "items": axis},
                    "reference_revision": {"type": ["integer", "null"]},
                    "stage_reference": {
                        "type": ["object", "null"],
                        "required": [
                            "reference_revision",
                            "reference_source",
                            "stop_epoch",
                            "limits_digest",
                        ],
                        "properties": {
                            "reference_revision": {"type": ["integer", "null"], "minimum": 0},
                            "reference_source": {
                                "enum": ["reference_revision", "limits_port_calibration"]
                            },
                            "stop_epoch": {"type": "integer", "minimum": 0},
                            "limits_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        },
                        "additionalProperties": False,
                    },
                    "fault": {"type": ["object", "string", "null"]},
                    "last_command": {"type": ["object", "null"]},
                    "robot_error": nullable_bool,
                    "alarms": {"type": ["array", "object", "null"]},
                },
            },
        },
    }
    point = {"type": "object", "additionalProperties": scalar}
    device["properties"]["observations"] = {
        "type": "object",
        "required": ["available", "quality"],
        "properties": {
            "available": {"type": "boolean"},
            "quality": quality,
            **{
                name: nullable_text
                for name in (
                    "reason",
                    "instance",
                    "job_id",
                    "action",
                    "status",
                    "source_timestamp",
                    "timestamp_basis",
                    "coordinate_system",
                    "source",
                    "result_source",
                )
            },
            "camera": {"type": ["string", "integer", "null"]},
            **{
                name: scalar
                for name in (
                    "observed_at",
                    "cache_age_s",
                    "frame_age_s",
                    "finished_at",
                    "source_observed_at",
                    "read_at",
                    "request_elapsed_s",
                    "capture_timestamp",
                    "width",
                    "height",
                )
            },
            "frame_sequence": {"type": ["integer", "null"]},
            "temperature_unit": {"const": "celsius"},
            "calibrated": nullable_bool,
            "source_identity_known": {"type": "boolean"},
            "complete": {"type": "boolean"},
            "count": {"type": "integer", "minimum": 0},
            "total": {"type": "integer", "minimum": 0},
            "stats": {
                "type": "object",
                "properties": {
                    **{
                        name: scalar for name in ("mean_celsius", "stddev_celsius", "range_celsius")
                    },
                    **{name: point for name in ("min", "max", "center")},
                },
            },
            "regions": {"type": "object", "additionalProperties": point},
            "items": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"enum": ["detections", "labels", "markers", "slots"]},
                        "center_px": {"type": "array", "items": scalar},
                        "center_xy_mm": {"type": "array", "items": scalar},
                        "robot_mm": point,
                        "text": nullable_text,
                        "label": nullable_text,
                        "confidence": scalar,
                        "text_truncated": {"type": "boolean"},
                    },
                },
            },
        },
    }
    axis["properties"].update(
        {
            "mode": nullable_text,
            "remaining_rotations": scalar,
            "lower_limit_rotations": scalar,
            "upper_limit_rotations": scalar,
            "motion_ready": nullable_bool,
            "fault": nullable_text,
        }
    )
    properties = device["properties"]["values"]["properties"]
    for name in ("locked", "enabled", "stop_unconfirmed", "focus_zero_known", "model_loaded"):
        properties[name] = nullable_bool
    for name in (
        "robot_mode",
        "speed_factor",
        "ramp_c_per_min",
        "objective_id",
        "objective_offset_steps",
        "focus_position_pulses",
        "focus_travel_limit_pulses",
        "frames",
        "fps",
    ):
        properties[name] = scalar
    for name in ("run_state", "control_mode", "state", "focus_reference"):
        properties[name] = nullable_text
    for name in ("flow_pct", "focus_limits_pulses"):
        properties[name] = {"type": "array", "items": scalar}
    for name in (
        "vacuum",
        "sequence",
        "recording",
        "last_result",
        "cameras",
        "turret_profile",
        "visual_focus",
        "autofocus_run",
        "autofocus_settings",
        "motor_power",
    ):
        properties[name] = {"type": ["object", "null"]}
    properties["calibrated_cameras"] = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": DIALECT,
        "contract": "agent-connect",
        "version": "1.1.0",
        "phase": 7,
        "actions": {},
        "semantics": {
            "quality": list(QUALITY),
            "sampled_at": (
                "Source sample time in Unix seconds when available. "
                "It need not be sensor capture time. "
                "Null means unknown. "
                "Stage monotonic assembly time is excluded."
            ),
            "observed_at": "Console cache update time in Unix seconds.",
            "generated_at": "Frame assembly time in Unix seconds, not source capture time.",
            "age_s": "Seconds since the Console observed the source state, not sensor age.",
            "state_at": "Service state assembly time in Unix seconds, not sensor capture time.",
            "sensor_age": (
                "frame_age_s, telemetry_age_s and encoder_age_s include cache age. "
                "Null means unknown."
            ),
            "location_basis": (
                "Observed uses sensor evidence. Inferred uses completed motion. "
                "Reported uses an operator record."
            ),
            "unknown": "Missing reference, source capture time, or measurement age remains null.",
            "observations": (
                "Shared thermal statistics and existing vision results. No images or capture jobs."
            ),
            "vision_freshness": (
                "Result completion time is not frame capture time. "
                "Historical inference has unknown current-scene quality. "
                "An absent source instance does not prevent reading a cached result. "
                "Its process continuity remains unknown."
            ),
            "cached_state": (
                "Bounded source fields from the existing Console cache. "
                "Values retain the source names and types. No additional device poll occurs. "
                "Omissions include private fields, locations, images, matrices, command output, "
                "nonfinite numbers, and fields beyond size limits. "
                "Check complete and omitted_count."
            ),
            "cached_state_units": (
                "The units map uses JSON Pointer paths within cached_state.values. "
                "Only verified legacy names and explicit unit suffixes receive units. "
                "A missing unit is unknown or not applicable. No unit is inferred from a value."
            ),
            "data_gaps": (
                "Names of absent source metadata. These are not inferred from broadcast time."
            ),
            "events": "Ordered within boot_id by event_seq. Replay keeps original contract_id.",
            "selection": "active or ids; count is returned rows, total is matching rows.",
            "complete": (
                "True when no later page remains. Read previous pages for the complete selection."
            ),
            "manual_reports": "Disabled until operator authority is available.",
            "units": {
                "pv_c": "degC",
                "position_rotations": "rotation",
                "joints_deg": "degree",
                "pose_mm_deg": "mm,mm,mm,degree",
            },
        },
        "schemas": {
            "samples": {"$schema": DIALECT, **sample_page},
            "subscription": {
                "$schema": DIALECT,
                "type": "object",
                "properties": {
                    "groups": {"type": "array", "items": {"enum": list(GROUPS)}},
                    "rate_hz": {"enum": [1, 2, 4], "default": 2},
                    "selection": {"enum": ["active", "ids"]},
                    "chip_ids": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
            },
            "resync": {
                "$schema": DIALECT,
                "type": "object",
                "required": ["type", "reason", "boot_id", "contract_id", "event_seq"],
                "properties": {
                    "type": {"const": "resync"},
                    "reason": {"enum": ["boot_changed", "cursor_ahead", "history_evicted"]},
                    "boot_id": {"type": "string"},
                    "contract_id": {"type": "string"},
                    "event_seq": {"type": "integer"},
                    "after": {"type": "integer"},
                },
            },
            "state": {
                "$schema": DIALECT,
                "type": "object",
                "required": ["type", "boot_id", "contract_id", "seq", "generated_at"],
                "properties": {
                    "type": {"const": "state"},
                    "boot_id": {"type": "string"},
                    "contract_id": {"type": "string"},
                    "seq": {"type": "integer"},
                    "generated_at": {"type": "number"},
                    "devices": {"type": "object", "additionalProperties": device},
                    "samples": sample_page,
                    "operations": {"type": "object"},
                },
            },
            "event": {
                "$schema": DIALECT,
                "type": "object",
                "required": ["type", "boot_id", "contract_id", "event_seq", "kind", "data"],
                "properties": {
                    "type": {"const": "event"},
                    "boot_id": {"type": "string"},
                    "contract_id": {"type": "string"},
                    "event_seq": {"type": "integer"},
                    "kind": {"type": "string"},
                    "data": {"type": "object"},
                },
            },
        },
    }


def immutable(value=None):
    result = copy.deepcopy(bundle() if value is None else value)
    if len(json.dumps(result).encode()) > 65536:
        raise ValueError("The contract exceeds the byte limit.")
    return digest(result), result
