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


def test_unidentified_supplement_remains_available_without_a_continuity_claim():
    state = provider({"thermal": {}, "vision": {}})
    summaries = {
        name: {"instance": None, "available": True, "quality": "valid"}
        for name in ("thermal", "vision")
    }
    state.observations_provider = lambda: summaries
    for row in state()["devices"].values():
        assert row["observations"]["available"]
        assert row["observations"]["quality"] == "unknown"
        assert row["observations"]["reason"] == "source_identity_unavailable"
    assert all(row["quality"] == "valid" for row in summaries.values())


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
            "focus_travel_limit_pulses": 1000,
            "focus_reference": "relative_command",
            "cameras": {"microscope": True, "side": False},
            "turret_profile": {
                "id": "height-profile",
                "ready": True,
                "needs_height_calibration": False,
            },
            "visual_focus": {
                "phase": "idle_released",
                "objective_id": 2,
                "y_px": 410.0,
                "observed_at": 1700000000,
            },
            "autofocus_run": {
                "phase": "failed",
                "objective_id": 2,
                "verified": False,
                "completed": [0, 1],
                "error": "No focus peak",
                "sequence": True,
                "search_half_range_pulses": 1200,
            },
            "autofocus_settings": {
                "five_x_half_range_pulses": 5000,
                "ten_x_half_range_pulses": 800,
                "twenty_x_half_range_pulses": 1200,
                "fifty_x_half_range_pulses": 300,
            },
            "motor_power": {
                "objective": {"enabled": False, "sleeping": True},
                "focus": {"enabled": False, "sleeping": True},
            },
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
    for field in (
        "turret_profile",
        "visual_focus",
        "autofocus_run",
        "autofocus_settings",
        "motor_power",
        "focus_travel_limit_pulses",
        "focus_reference",
    ):
        assert devices["microscope"]["values"][field] == states["microscope"][field]
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
    assert len(json.dumps(frame)) < 3500


def test_cached_sensor_fields_preserve_human_state_and_known_units():
    states = {
        "robot": {
            "payload_kg": 0.25,
            "keyboard_speed_percent": 10,
            "actual": [1, 2, 3, 4],
            "pressure": {"abs_hpa": 985.5, "gauge_hpa": -25.3},
        },
        "furnace": {
            "pv": 42,
            "sv": 100,
            "ramp": 5,
            "stored_sv": 100,
            "auxiliary_output": {"value": 3, "verified": True},
            "flow": {"gas_channels": [{"actual_percent": 8, "target_percent": 10}]},
        },
        "stage": {
            "sampled_at": 123.4,
            "motors": [{"encoder_counts": 12, "telemetry_age_ms": 20, "position_rotations": 3}],
        },
        "microscope": {"objective_offset_limit_steps": 400, "stabilization": True},
        "thermal": {"frame_sequence": 12, "custom_sensor": {"reading": 3.5}},
    }
    rows = provider(states)()["devices"]
    for name, state in states.items():
        cached = rows[name]["cached_state"]
        assert cached["values"] == state
        assert cached["complete"] and cached["omitted_count"] == 0
        assert rows[name]["data_gaps"] == ["source_instance", "sensor_capture_time", "state_time"]
    assert rows["furnace"]["values"]["ramp_c_per_min"] == 5
    assert (
        rows["furnace"]["cached_state"]["units"]["/flow/gas_channels/0/actual_percent"] == "percent"
    )
    assert rows["stage"]["cached_state"]["units"]["/motors/0/position_rotations"] == "rotation"
    assert "/motors/0/encoder_counts" not in rows["stage"]["cached_state"]["units"]
    assert rows["thermal"]["cached_state"]["units"] == {}
    assert rows["robot"]["cached_state"]["units"]["/actual/0"] == "mm"
    assert rows["robot"]["cached_state"]["units"]["/actual/3"] == "degree"
    assert rows["robot"]["cached_state"]["units"]["/pressure/abs_hpa"] == "hPa"
    assert rows["robot"]["cached_state"]["units"]["/pressure/gauge_hpa"] == "hPa"
    assert rows["stage"]["sampled_at"] is None
    assert rows["stage"]["cached_state"]["units"]["/sampled_at"] == "monotonic_s"


def test_cached_fields_bound_bulk_data_and_remove_nested_secrets():
    state = {
        "pv_c": 25,
        "nested": {"session_token": "sensitive", "password": "secret", "apiKey": "private-api-key"},
        "reading_c": None,
        "unlabelled_matrix": [[1, 2]],
        "image": "base64-image",
        "image_url": "https://private",
        "unbounded": {f"sensor_{i}_mm": i for i in range(10000)},
        "nonfinite": float("nan"),
    }
    row = provider({"furnace": state})()["devices"]["furnace"]
    encoded = json.dumps(row, allow_nan=False)
    assert not any(
        value in encoded
        for value in ("sensitive", '"secret"', "private-api-key", "base64-image", "https://private")
    )
    cached = row["cached_state"]
    assert cached["values"]["pv_c"] == 25
    assert cached["units"]["/reading_c"] == "degC"
    assert not cached["complete"]
    assert len(cached["omitted"]) <= 32
    assert len(encoded.encode()) < 32768
    assert {item["reason"] for item in cached["omitted"]} >= {
        "private_or_bulk_data",
        "matrix",
        "location",
        "nonfinite",
    }


def test_cached_state_is_detached_and_does_not_claim_capture_time():
    states = {"furnace": {"sample_time": 1700000000, "flow": {"target_percent": 5}}}
    source = provider(states)
    row = source()["devices"]["furnace"]
    assert row["sampled_at"] == 1700000000
    assert "sensor_capture_time" in row["data_gaps"]
    row["cached_state"]["values"]["flow"]["target_percent"] = 99
    assert source()["devices"]["furnace"]["cached_state"]["values"]["flow"]["target_percent"] == 5
