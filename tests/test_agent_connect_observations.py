"""Supplement reads stay independent of subscribers and preserve source evidence."""

import copy

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.observations import (
    SharedObservations,
    thermal_summary,
    vision_summary,
)

INSTANCE = "a" * 32


class Clock:
    now = 0

    def __call__(self):
        return self.now


class Adapter:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []
        self.closed = False

    def request(self, path):
        self.paths.append(path)
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def close(self):
        self.closed = True


def thermal():
    return {
        "instance": INSTANCE,
        "sequence": 17,
        "source": "camera",
        "width": 160,
        "height": 120,
        "timestamp": "2026-11-20T12:00:00+00:00",
        "timestamp_basis": "read_completed",
        "capture_timestamp": None,
        "frame_age_s": 0.5,
        "temperature_unit": "celsius",
        "stats": {
            "mean_celsius": 25,
            "min": {"celsius": 20, "x": 1, "y": 2},
            "max": {"celsius": 30, "x": 2, "y": 3},
            "center": {"celsius": 24, "x": 80, "y": 60},
        },
        "regions": {
            "crucible": {"x0": 1, "y0": 1, "x1": 3, "y1": 3, "min_c": 21, "mean_c": 24, "max_c": 27}
        },
    }


def vision(job="1"):
    return {
        "instance": INSTANCE,
        "job_id": job,
        "available": True,
        "action": "read_qr",
        "camera": 1,
        "status": "ok",
        "finished_at": 99,
        "observed_at": None,
        "output": {
            "calibrated": False,
            "coordinate_system": "oriented_original_image_pixels",
            "labels": [
                {
                    "text": "chip-1",
                    "format": "QR",
                    "decoded": True,
                    "center_px": [12, 23],
                    "corners_px": [[0, 0]] * 1000,
                }
            ],
        },
    }


class Service:
    def __init__(self):
        self.devices = self
        self.rows = {
            name: {"phase": "ready", "state": {"instance": INSTANCE}}
            for name in ("thermal", "vision")
        }
        self.rows["vision"]["state"]["last_result"] = {"instance": INSTANCE, "job_id": "1"}
        self.thermal = Adapter({"api/frame?summary=1": thermal()})
        self.vision = Adapter({f"api/results/1?instance={INSTANCE}": vision()})
        self.clients = []
        self.cache_reads = []

    def get(self, name):
        self.cache_reads.append(name)
        return copy.deepcopy(self.rows[name])

    def dashboard_configured(self, name):
        return True

    def console_client(self, name, identity):
        self.clients.append((name, identity))
        return getattr(self, name)


def test_shared_reader_has_no_constructor_or_snapshot_io_and_no_lease_calls():
    service, clock = Service(), Clock()
    observations = SharedObservations(service, clock=clock, wall_clock=lambda: 100)
    assert service.clients == []
    for _ in range(50):
        assert observations.snapshot()["thermal"]["available"] is False
    assert service.cache_reads == []
    observations.refresh()
    for _ in range(50):
        observations.snapshot()
        observations.refresh()
    assert service.thermal.paths == ["api/frame?summary=1"]
    assert service.vision.paths == [f"api/results/1?instance={INSTANCE}"]
    assert len(service.clients) == 2
    clock.now = 1
    observations.refresh()
    assert len(service.thermal.paths) == 2
    assert len(service.vision.paths) == 1
    observations.close()
    assert service.thermal.closed and service.vision.closed


def test_capture_time_is_not_invented_and_age_advances_without_io():
    service, clock = Service(), Clock()
    observations = SharedObservations(service, clock=clock, wall_clock=lambda: 100)
    observations.refresh()
    result = observations.snapshot()
    assert result["thermal"]["capture_timestamp"] is None
    assert result["thermal"]["source_timestamp"] == thermal()["timestamp"]
    assert result["thermal"]["timestamp_basis"] == "read_completed"
    assert result["thermal"]["observed_at"] == 100
    assert result["thermal"]["frame_age_s"] == 0.5
    assert result["vision"]["quality"] == "unknown"
    assert result["vision"]["source_observed_at"] is None
    assert result["vision"]["finished_at"] == 99
    clock.now = 3
    result = observations.snapshot()
    assert result["thermal"]["frame_age_s"] == 3.5
    assert result["thermal"]["quality"] == "stale"
    assert result["vision"]["cache_age_s"] == 3
    assert result["vision"]["quality"] == "unknown"
    assert len(service.thermal.paths) == len(service.vision.paths) == 1
    observations.close()


def test_summary_keeps_bounded_items_and_omits_all_image_arrays():
    payload = thermal()
    payload["raw"] = payload["ir"] = payload["temperatures_c"] = [[1]]
    payload["regions"] = {str(i): payload["regions"]["crucible"] for i in range(40)}
    compact = thermal_summary(payload)
    assert compact["count"] == 32 and compact["total"] == 40 and not compact["complete"]
    assert not {"raw", "ir", "temperatures_c"} & compact.keys()
    payload = vision()
    payload["output"]["labels"] *= 70
    payload["output"]["labels"][0]["text"] = "x" * 1000
    compact = vision_summary(payload)
    assert compact["count"] == 50 and compact["total"] == 70 and not compact["complete"]
    assert len(compact["items"][0]["text"]) == 256
    assert compact["items"][0]["text_truncated"]
    assert "corners_px" not in compact["items"][0]
    assert compact["items"][0]["center_px"] == [12, 23]
    assert "output" not in compact


def test_vision_lookup_tracks_exact_latest_job_and_service_instance():
    service, clock = Service(), Clock()
    observations = SharedObservations(service, clock=clock)
    observations.refresh()
    service.rows["vision"]["state"]["last_command"] = {
        "id": "2",
        "instance": INSTANCE,
        "result": "failed",
    }
    response = {**vision("2"), "status": "failed", "output": None}
    service.vision.responses[f"api/results/2?instance={INSTANCE}"] = response
    clock.now = 1
    observations.refresh()
    compact = observations.snapshot()["vision"]
    assert compact["job_id"] == "2" and compact["status"] == "failed"
    assert compact["items"] == []
    new_instance = "b" * 32
    service.rows["vision"]["state"] = {
        "instance": new_instance,
        "last_result": {"instance": new_instance, "job_id": "1"},
    }
    service.vision.responses[f"api/results/1?instance={new_instance}"] = {
        **vision(),
        "instance": new_instance,
    }
    clock.now = 2
    observations.refresh()
    assert observations.snapshot()["vision"]["instance"] == new_instance
    assert len(service.vision.paths) == 3
    observations.close()


@pytest.mark.parametrize("kind", ["pending", "expired_or_evicted", "restarted", "mismatch"])
def test_missing_results_do_not_use_latest_successful_result(kind):
    service, clock = Service(), Clock()
    response = {
        "instance": INSTANCE,
        "job_id": "1",
        "available": False,
        "reason": kind,
        "status": "pending" if kind == "pending" else "unavailable",
    }
    if kind == "restarted":
        response["instance"] = "b" * 32
    if kind == "mismatch":
        response["job_id"] = "9"
    service.vision.responses[f"api/results/1?instance={INSTANCE}"] = response
    observations = SharedObservations(service, clock=clock)
    observations.refresh()
    assert observations.snapshot()["vision"]["available"] is False
    assert not observations.snapshot()["vision"].get("items")
    clock.now = 1
    observations.refresh()
    assert len(service.vision.paths) == (1 if kind == "expired_or_evicted" else 2)
    observations.close()


def test_failure_or_unavailable_cache_does_not_present_prior_statistics_as_valid():
    service, clock = Service(), Clock()
    observations = SharedObservations(service, clock=clock)
    observations.refresh()
    service.thermal.responses["api/frame?summary=1"] = RuntimeError("camera unavailable")
    service.rows["vision"]["phase"] = "stale"
    clock.now = 1
    observations.refresh()
    result = observations.snapshot()
    assert result["thermal"]["reason"] == "read_failed"
    assert result["thermal"]["quality"] == "unavailable"
    assert "stats" not in result["thermal"]
    assert result["vision"]["reason"] == "source_unavailable"
    assert len(service.vision.paths) == 1
    observations.close()


def test_expired_http_result_stops_retries_until_a_new_job():
    service, clock = Service(), Clock()
    path = f"api/results/1?instance={INSTANCE}"
    service.vision.responses[path] = ServiceError(410, "Gone")
    observations = SharedObservations(service, clock=clock)
    observations.refresh()
    result = observations.snapshot()["vision"]
    assert result["reason"] == "result_expired_or_unavailable"
    assert result["instance"] == INSTANCE and not result["available"]
    clock.now = 1
    observations.refresh()
    assert service.vision.paths == [path]
    observations.close()


def test_detection_marker_and_slot_points_keep_source_coordinate_names():
    payload = vision()
    payload["output"] = {
        "detections": [
            {
                "class_id": 1,
                "label": "chip",
                "confidence": 0.9,
                "center_xy": [1, 2],
                "robot_mm": {"x": 3, "y": 4},
            }
        ],
        "markers": [{"id": 8, "center_px": [5, 6]}],
        "slots": {"A1": {"state": "present", "center_xy_mm": [7, 8]}},
    }
    compact = vision_summary(payload)
    assert compact["total"] == compact["count"] == 3
    assert compact["items"][0]["robot_mm"] == {"x": 3, "y": 4}
    assert compact["items"][1]["center_px"] == [5, 6]
    assert compact["items"][2]["center_xy_mm"] == [7, 8]
