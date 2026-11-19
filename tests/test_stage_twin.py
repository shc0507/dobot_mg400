"""Exercise numerical calibration and synthetic images without device access."""

# Source: Chemistry-Integration/Digital_Twin/tests/scenarios/test_calibration_profiles.py
# Source: Chemistry-Integration/Digital_Twin/tests/scenarios/test_insert_recovery.py
# Source: Chemistry-Integration/Digital_Twin/tests/scenarios/test_tuning_policy.py

import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256

import cv2
import numpy as np
import pytest

from vision.stage_twin import (
    DEFAULT_TRAVEL_MM,
    MAX_PROFILE_BYTES,
    StageConverter,
    TuningPolicy,
    analyze_attempt,
    analyze_frame,
    capture_endpoint,
    evaluate_profile,
    monitor_preset_catalog,
    new_profile,
    read_profile_json,
)
from vision.stage_twin.calibration.stage import centers, estimate_axis


def observation(child, parent, position=(200, 200), dimensions=(640, 480)):
    def detection(marker, center):
        x, y = center
        return {
            "id": marker,
            "center_px": [x, y],
            "corners_px": [[x - 10, y - 10], [x + 10, y - 10], [x + 10, y + 10], [x - 10, y + 10]],
        }

    return {
        "image_size_px": list(dimensions),
        "detections": [detection(child, position), detection(parent, (100, 100))],
        "ambiguous_ids": [],
        "image_sha256": "0" * 64,
        "captured_at": "2026-01-01T00:00:00+00:00",
    }


def complete_profile():
    profile = new_profile(name="Stage camera")
    profile["camera"].update(device_id="stage", label="Stage")
    profile["endpoints"] = {
        "x_low": observation(20, 16),
        "x_high": observation(20, 16, (300, 200)),
        "y_low": observation(16, 12),
        "y_high": observation(16, 12, (200, 100)),
        "insert_out": observation(22, 20, (200, 300)),
        "insert_in": observation(22, 20, (200, 150)),
    }
    return evaluate_profile(profile)["profile"]


def test_draft_preserves_observations_without_fabricating_axis():
    profile = new_profile()
    assert profile["travel_mm"] == DEFAULT_TRAVEL_MM == {"x": 45, "y": 40, "insert": 150}
    assert profile["format"] == "furnace-twin.calibration-profile"
    assert profile["schema_version"] == 1
    profile["endpoints"]["insert_out"] = observation(22, 20)
    result = evaluate_profile(profile)
    assert result["profile"]["axes"] == {}
    assert result["profile"]["status"] == "partial"
    assert result["endpoint_status"]["insert_out"]["status"] == "captured"
    assert result["endpoint_status"]["insert_in"]["status"] == "missing"
    assert result["axis_status"]["insert"]["status"] == "pending"
    assert result["profile"]["endpoints"]["insert_out"] == profile["endpoints"]["insert_out"]


@pytest.mark.parametrize(
    "high",
    [
        observation(22, 20),
        observation(22, 19, (350, 300)),
        observation(22, 20, (350, 300), (800, 480)),
    ],
)
def test_invalid_endpoint_pair_remains_saveable_without_axis(high):
    profile = new_profile()
    profile["camera"]["dimensions"] = [640, 480]
    profile["endpoints"] = {"insert_out": observation(22, 20), "insert_in": high}
    result = evaluate_profile(profile)
    assert "insert" not in result["profile"]["axes"]
    assert result["axis_status"]["insert"]["status"] == "error"
    assert result["axis_status"]["insert"]["message"]
    assert json.loads(json.dumps(result))["profile"]["endpoints"]


def test_load_rebuilds_axes_from_evidence_and_bad_resnap_removes_only_affected_axis():
    profile = complete_profile()
    assert profile["status"] == "complete"
    profile["axes"]["insert"]["max"] = 99999
    checked = evaluate_profile(profile)["profile"]
    assert checked["axes"]["insert"]["max"] == 150
    checked["endpoints"]["insert_in"] = deepcopy(checked["endpoints"]["insert_out"])
    result = evaluate_profile(checked)["profile"]
    assert set(result["axes"]) == {"x", "y"}
    assert result["axis_status"]["insert"]["status"] == "error"


@pytest.mark.parametrize("change", ["nonfinite", "negative", "version", "image", "marker_group"])
def test_invalid_profile_structure_is_rejected(change):
    profile = complete_profile()
    if change == "nonfinite":
        profile["endpoints"]["x_low"]["detections"][0]["center_px"][0] = float("nan")
    elif change == "negative":
        profile["travel_mm"]["insert"] = -150
    elif change == "version":
        profile["schema_version"] = 999
    elif change == "image":
        profile["image_base64"] = "embedded image"
    else:
        profile["marker_groups"]["x"].append(22)
    with pytest.raises(ValueError):
        evaluate_profile(profile)


@pytest.mark.parametrize(
    "axis,high,span", [("x", "x_high", 45), ("y", "y_high", 40), ("insert", "insert_in", 150)]
)
def test_axis_uses_parent_relative_positions_and_reports_missing_and_extrapolated(axis, high, span):
    profile = complete_profile()
    model = profile["axes"][axis]
    points = centers(profile["endpoints"][high])
    translated = {marker: [point[0] + 100, point[1] - 70] for marker, point in points.items()}
    assert estimate_axis(model, translated)["value_mm"] == pytest.approx(span)
    assert estimate_axis(model, {}) == {
        "value_mm": None,
        "status": "unknown",
        "reason": "no_calibrated_pair_visible",
    }
    track = model["tracks"][0]
    moving = translated[track["moving_id"]]
    vector = track["travel_vector_px"]
    translated[track["moving_id"]] = [moving[0] + vector[0], moving[1] + vector[1]]
    extrapolated = estimate_axis(model, translated)
    assert extrapolated["value_mm"] == pytest.approx(2 * span)
    assert extrapolated["status"] == "extrapolated"
    assert estimate_axis(None, points)["reason"] == "calibration_missing"


@pytest.mark.parametrize(
    "payload", [b'{"x":NaN}', b'{"x":Infinity}', b"[]", b"{", b" " * (MAX_PROFILE_BYTES + 1)]
)
def test_profile_parser_rejects_invalid_json(payload):
    with pytest.raises(ValueError):
        read_profile_json(payload)


def camera_frame(positions=(), *, shift=(0, 0), references=(19, 20), clean=False, printed_id=22):
    """Render insert travel at 2 pixels/mm relative to the X stage."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    frame = np.full((520, 720), 60, np.uint8)

    def place(marker_id, center_x, center_y, side, isolated):
        left, top = center_x - side // 2 + shift[0], center_y - side // 2 + shift[1]
        if isolated:
            frame[top - 10 : top + side + 10, left - 10 : left + side + 10] = 230
        cells = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
        frame[top : top + side, left : left + side] = np.where(cells, 230, 60)

    for reference in references:
        place(reference, {18: 65, 19: 120, 20: 550}[reference], 220, 60, True)
    for position in positions:
        place(printed_id, 346, 400 - round(2 * position), 72, clean)
    return cv2.imencode(".png", frame)[1].tobytes()


@pytest.fixture(scope="module")
def insert_converter():
    profile = new_profile()
    profile["endpoints"] = {
        key: capture_endpoint(camera_frame([mm], clean=True), key)
        for key, mm in (("insert_out", 0), ("insert_in", 150))
    }
    return StageConverter.from_profile(profile)


@pytest.mark.parametrize(
    "mm,shift,refs", [(63, (0, 0), (19, 20)), (150, (78, 27), (19,)), (0, (-38, 19), (20,))]
)
def test_insert_recovery_reads_current_pixels_with_parent_motion(insert_converter, mm, shift, refs):
    data = camera_frame([mm], shift=shift, references=refs)
    report = insert_converter.measure(data)
    assert report["dimensions"] == [720, 520]
    assert report["image_sha256"] == sha256(data).hexdigest()
    assert datetime.fromisoformat(report["captured_at"]).tzinfo is not None
    assert report["insert_recovery"]["status"] == "recovered"
    assert set(report["insert_recovery"]["reference_ids"]) == set(refs)
    assert report["axes"]["insert"]["value_mm"] == pytest.approx(mm, abs=0.5)
    marker = next(item for item in report["detections"] if item["id"] == 22)
    assert marker["center_px"] == pytest.approx(
        [345.5 + shift[0], 399.5 - 2 * mm + shift[1]], abs=1.0
    )
    assert marker["validation"]["data_bit_errors"] == 0
    assert marker["validation"]["border_cell_errors"] == 0


@pytest.mark.parametrize("positions,printed_id", [((), 22), ((90,), 23), ((18, 115), 22)])
def test_occlusion_wrong_code_and_duplicate_ids_do_not_reuse_positions(
    insert_converter, positions, printed_id
):
    report = insert_converter.measure(camera_frame(positions, printed_id=printed_id))
    assert report["axes"]["insert"]["value_mm"] is None
    assert 22 not in {item["id"] for item in report["detections"]}
    if len(positions) == 2:
        assert report["insert_recovery"]["status"] == "ambiguous"
        assert 22 in report["ambiguous_ids"]


def test_visible_insert_requires_a_current_calibrated_parent(insert_converter):
    result = insert_converter.measure(camera_frame([75], references=(18,), clean=True))
    assert {item["id"] for item in result["detections"]} == {18, 22}
    assert result["insert_recovery"]["status"] == "missing_reference"
    assert result["axes"]["insert"]["value_mm"] is None


def test_blank_frame_returns_capture_diagnostics_and_no_coordinates():
    data = cv2.imencode(".png", np.full((480, 640, 3), 255, np.uint8))[1].tobytes()
    attempt = analyze_attempt(data, "insert_out")
    assert attempt["capture"]["detections"] == []
    assert attempt["tuning"]["usable"] is False
    assert attempt["tuning"]["complete"] is False
    assert attempt["tuning"]["reason"]
    report = StageConverter.from_profile(new_profile()).measure(data)
    assert all(axis["reason"] == "calibration_missing" for axis in report["axes"].values())
    with pytest.raises(ValueError, match="Missing"):
        capture_endpoint(data, "insert_out")
    with pytest.raises(ValueError, match="Unknown"):
        analyze_attempt(data, "bad_endpoint")


def test_frame_resolution_and_invalid_raster_cannot_produce_measurements(insert_converter):
    data = cv2.imencode(".png", np.full((480, 640), 255, np.uint8))[1].tobytes()
    with pytest.raises(ValueError, match="requires 720 x 520"):
        insert_converter.measure(data)
    with pytest.raises(ValueError, match="readable JPEG or PNG"):
        analyze_frame(b"not a camera raster")
    with pytest.raises(ValueError, match="16 MiB"):
        analyze_frame(b"")


@pytest.mark.parametrize("dimensions", [(1, 1), (2, 2), (640, 1)])
def test_tiny_valid_raster_has_no_detections(dimensions):
    width, height = dimensions
    data = cv2.imencode(".png", np.full((height, width), 255, np.uint8))[1].tobytes()
    result = analyze_frame(data)
    assert result["image_size_px"] == [width, height]
    assert result["detections"] == []


@pytest.mark.parametrize("preset", [0, 9, True, "2"])
def test_unknown_detector_preset_is_rejected(preset):
    with pytest.raises(ValueError, match="preset"):
        StageConverter.from_profile(new_profile()).measure(b"unused", preset=preset)


def report(visible, *, preset=1, method="Standard", held=False):
    return {
        "axes": {
            axis: {
                "value_mm": 0 if axis in visible else None,
                "reason": None if axis in visible else "no_calibrated_pair_visible",
                "tracks": [{}] if axis in visible else [],
                "held": held,
            }
            for axis in ("x", "y", "insert")
        },
        "detector_preset": preset,
        "detector_method": method,
    }


def test_tuning_uses_only_current_calibrated_axes_and_standard_detection_gets_credit():
    policy = TuningPolicy(("x", "insert"), max_frames=1)
    policy.observe(report(("x", "insert")))
    assert not policy.searching
    policy.observe(report(("x",)))
    assert policy.searching
    assert policy.next_preset() == 2
    policy.observe(report(("x", "insert"), preset=2))
    assert policy.active_preset == 1
    assert policy.snapshot()["outcome"] == "complete"


def test_tuning_manual_search_and_deadline_do_not_need_hardware():
    now = [0.0]
    policy = TuningPolicy(("insert",), automatic=False, clock=lambda: now[0])
    policy.observe(report(()))
    assert not policy.searching
    assert policy.start_manual()
    now[0] = 31
    assert policy.next_preset() == 1
    assert policy.snapshot()["outcome"] == "expired"
    assert [item["id"] for item in monitor_preset_catalog()] == list(range(1, 9))
