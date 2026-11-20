"""Cached adapter checks against the device snapshot field shapes."""

import json
from types import SimpleNamespace

import pytest

from orchestrator.agent_connect.protocol import bundle
from orchestrator.agent_connect.state import CacheStateProvider


def provider(states, phase="ready", age=0.5):
    rows = {
        name: {"state": state, "phase": phase, "age_s": age, "updated_at": 1700000001}
        for name, state in states.items()
    }
    return CacheStateProvider(SimpleNamespace(entries=rows, get=rows.__getitem__))


def test_supplement_cache_schema_and_instance_mismatch():
    from orchestrator.agent_connect.observations import thermal_summary, vision_summary

    state = provider({"thermal": {"instance": "t"}, "vision": {"instance": "v"}})
    summaries = {
        "thermal": thermal_summary({"instance": "t", "sequence": 4, "stats": {}}),
        "vision": vision_summary(
            {
                "instance": "v",
                "job_id": "2",
                "available": True,
                "status": "ok",
                "output": {"labels": [{"text": "chip-1"}]},
            }
        ),
    }
    state.observations_provider = lambda: summaries
    first = state()["devices"]
    assert first["thermal"]["observations"]["available"]
    assert first["vision"]["observations"]["quality"] == "unknown"
    summaries["thermal"]["instance"] = "previous-process"
    second = state()["devices"]
    assert second["thermal"]["observations"]["quality"] == "unavailable"
    assert summaries["thermal"]["available"]  # A projection cannot alter the shared cache.
    schema = bundle()["schemas"]["state"]["properties"]["devices"]["additionalProperties"]
    jsonschema = pytest.importorskip("jsonschema")
    for row in (*first.values(), *second.values()):
        jsonschema.Draft202012Validator(schema).validate(row)


def test_actual_device_shapes_and_schema():
    states = {
        "robot": {
            "instance": "r",
            "connected": True,
            "busy": False,
            "joints": [1, 2, 3, 4],
            "actual": [5, 6, 7, 8],
            "feedback_fresh": True,
            "enabled": True,
            "robot_mode": 5,
            "speed_factor": 10,
            "vacuum": {"suck": True, "held": None},
            "stop_unconfirmed": False,
        },
        "stage": {
            "instance": "s",
            "sampled_at": 123.4,
            "ts_unix": 1700000000,
            "reference_revision": 3,
            "motors": [
                {
                    "id": "insert",
                    "position_rotations": 2,
                    "reference_valid": True,
                    "telemetry_fresh": True,
                    "telemetry_age_ms": 100,
                    "encoder_age_ms": 30,
                    "mode": "idle",
                    "remaining_rotations": 0,
                    "lower_limit_rotations": 0,
                    "upper_limit_rotations": 20,
                    "motion_ready": True,
                    "fault": "none",
                }
            ],
        },
        "furnace": {
            "instance": "f",
            "pv_c": 42,
            "target_c": 100,
            "sample_time": 1700000000,
            "ramp_c_per_min": 5,
            "run_state": "RUN",
            "flow_pct": [3, None],
            "sequence": {"phase": "heating", "current_index": 0, "steps": ["large omitted"]},
        },
        "microscope": {
            "instance": "m",
            "objective_id": 2,
            "focus_position_pulses": None,
            "focus_zero_known": False,
            "focus_limits_pulses": [-200, 300],
            "cameras": {"microscope": True, "side": False},
            "recording": {
                "id": "rec",
                "status": "recording",
                "frame_count": 5,
                "segments": ["large omitted"],
            },
        },
        "vision": {
            "instance": "v",
            "model_loaded": True,
            "calibrated_cameras": ["top"],
            "last_result": {"job_id": "j", "count": 2, "finished_at": 1700000000},
        },
        "thermal": {
            "instance": "t",
            "frame_age_s": 2,
            "frames": 10,
            "fps": 9.5,
            "state": "streaming",
        },
    }
    frame = provider(states)()
    frame.update(type="state", boot_id="b", contract_id="c", seq=1, generated_at=1700000002)
    devices = frame["devices"]
    assert devices["stage"]["sampled_at"] is None
    axis = devices["stage"]["values"]["axes"][0]
    assert axis["reference_revision"] == 3
    assert axis["telemetry_age_s"] == 0.6
    assert axis["encoder_age_s"] == 0.53
    assert devices["furnace"]["values"]["sp_c"] == 100
    assert devices["furnace"]["sampled_at"] == 1700000000
    assert "steps" not in devices["furnace"]["values"]["sequence"]
    assert devices["microscope"]["values"]["focus_zero_known"] is False
    assert devices["microscope"]["values"]["recording"]["frame_count"] == 5
    assert "segments" not in devices["microscope"]["values"]["recording"]
    assert devices["thermal"]["frame_age_s"] == 2.5
    assert "temperature_c" not in devices["thermal"]["values"]
    assert devices["vision"]["values"]["last_result"]["count"] == 2
    pytest.importorskip("jsonschema").validate(frame, bundle()["schemas"]["state"])


def test_stale_axes_unavailable_robot_and_unknown_sensor_times():
    stage = {"motors": [{"id": "insert", "reference_valid": True, "telemetry_fresh": True}]}
    result = provider({"stage": stage}, phase="stale")()["devices"]["stage"]
    assert result["values"]["axes"][0]["quality"] == "stale"
    assert result["values"]["axes"][0]["encoder_age_s"] is None
    robot = provider({"robot": {"feedback_fresh": False}}, phase="unavailable")()["devices"][
        "robot"
    ]
    assert robot["quality"] == "unavailable"
    assert robot["sampled_at"] is None
    assert robot["frame_age_s"] is None


def test_command_output_is_omitted_and_message_is_bounded():
    state = {
        "last_command": {
            "id": "j",
            "action": "detect",
            "result": "ok",
            "output": {"pixels": "x" * 300000},
            "message": "x" * 10000,
        }
    }
    frame = provider({"vision": state})()
    command = frame["devices"]["vision"]["values"]["last_command"]
    assert "output" not in command
    assert len(command["message"]) == 512
    assert len(json.dumps(frame)) < 2000
