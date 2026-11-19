"""The Motion charts accept a position source without starting legacy vision."""

from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask

from orchestrator.web.workspaces import motion


def test_position_connector_defaults_to_absent_and_hides_invalid_values():
    service = SimpleNamespace()
    assert motion.position_snapshot(service) is None
    service.stage_position_source = SimpleNamespace(
        snapshot=lambda: {
            "available": True,
            "values_rotations": {"x": 12.5, "y": True, "insert": float("nan")},
            "limits_rotations": {
                "x": {"min": 0, "max": 100, "private": "hidden"},
                "y": {"min": 20, "max": 10},
                "insert": {"min": 0, "max": float("inf")},
            },
            "private": "hidden",
        }
    )
    assert motion.position_snapshot(service) == {
        "available": True,
        "values_rotations": {"x": 12.5, "y": None, "insert": None},
        "limits_rotations": {"x": {"min": 0, "max": 100}},
    }


def test_position_connector_failure_does_not_block_motion():
    def failed():
        raise OSError("Position source unavailable")

    service = SimpleNamespace(stage_position_source=SimpleNamespace(snapshot=failed))
    assert motion.position_snapshot(service) == {"available": False}


def test_motion_state_includes_only_explicit_position_source(monkeypatch):
    service = SimpleNamespace()
    app = Flask(__name__)
    blueprint = Blueprint("motion", __name__)
    motion.register(blueprint, service)
    app.register_blueprint(blueprint)
    monkeypatch.setattr(motion, "read", lambda *args: {"available": False})
    monkeypatch.setattr(
        motion,
        "get_sample_transfer",
        lambda *args: pytest.fail("Motion polling must not create a transfer manager."),
    )
    client = app.test_client()
    assert "stage_position" not in client.get("/state").json
    assert client.get("/state").json["stage_operation"] == {
        "status": "idle",
        "mine": False,
        "message": "",
        "settling": False,
    }
    assert not hasattr(service, "sample_transfer")
    service.stage_position_source = SimpleNamespace(
        snapshot=lambda: {"available": True, "values_rotations": {"insert": 25}}
    )
    assert client.get("/state").json["stage_position"] == {
        "available": True,
        "values_rotations": {"x": None, "y": None, "insert": 25},
        "limits_rotations": {},
    }
    assert getattr(service, "_twin_console", None) is None


def test_stage_projection_keeps_encoder_telemetry_and_hides_private_state():
    stage, _ = motion._stage(
        {
            "closed_loop": True,
            "telemetry_streaming": True,
            "stop_epoch": 7,
            "private": "hidden",
            "motors": [
                {
                    "id": "top",
                    "position_rotations": 0.125,
                    "motion_ready": True,
                    "positioning_ready": True,
                    "telemetry_age_ms": 20,
                    "limit_telemetry_age_ms": 30,
                    "switch_mask": 1,
                    "lower_limit_rotations": 0,
                    "upper_limit_rotations": 10,
                    "calibration": "done",
                    "direction_warning_count": 7,
                    "recalibration_recommended": True,
                    "private": "hidden",
                }
            ],
        }
    )
    assert stage["closed_loop"] is True
    assert stage["stop_epoch"] == 7
    motor = stage["motors"][0]
    assert motor["position_rotations"] == 0.125
    assert motor["positioning_ready"] is True
    assert motor["telemetry_age_ms"] == 20
    assert motor["switch_mask"] == 1
    assert motor["direction_warning_count"] == 7
    assert "private" not in stage and "private" not in motor


@pytest.mark.parametrize("speed", [0.001, 0.125, 0.5, True, 0, -1, "0.25"])
def test_stage_hold_route_accepts_fractional_speed_and_rejects_invalid_types(speed):
    calls = []
    service = SimpleNamespace(
        console=SimpleNamespace(
            stage_hold=lambda *args: calls.append(args) or {"outcome": "accepted"}
        )
    )
    app = Flask(__name__)
    blueprint = Blueprint("motion", __name__)
    motion.register(blueprint, service)
    app.register_blueprint(blueprint)
    app.register_error_handler(ValueError, lambda error: (str(error), 400))
    response = app.test_client().post(
        "/stage/hold",
        headers={"X-MG400-Client": "motion-tests"},
        json={"id": "hold-0123456789abcdef", "motor": "insert", "direction": "ccw", "speed": speed},
    )
    valid = type(speed) in (int, float) and speed > 0
    assert response.status_code == (200 if valid else 400)
    assert len(calls) == int(valid)
    if valid:
        assert calls[0][-1] == speed
