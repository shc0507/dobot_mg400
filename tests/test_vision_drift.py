"""Synthetic marker images from a known camera, without cameras or services."""

import copy
import json
import math
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image
from test_deploy_mg400ctl import ctl as ctl
from test_orchestrator_runner import FakeService, until
from test_orchestrator_transfer import Robot
from test_orchestrator_transfer import geometry as geometry

from labkit.errors import ServiceError
from orchestrator.recipe import validate
from orchestrator.runner import Context, Runner, _Authority
from orchestrator.skills import Registry
from orchestrator.skills.vision import DRIFT_SETTING, check_calibration
from orchestrator.store import RunStore
from orchestrator.zones import ZonePlan, Zones
from vision.calibration import (
    VALIDATION_INDICES,
    detect_markers,
    drift_inputs,
    drift_limit,
    drift_proof,
)
from vision.service import VisionService, create_app

DEVICE = "/dev/v4l/by-path/platform-offline-drift-video-index0"
SIZE = (640, 480)
MATRIX = np.array([[700.0, 0, 320], [0, 700.0, 240], [0, 0, 1]])
DISTORTION = np.array([-0.05, 0.01, 0, 0, 0])
# The known camera: its center in robot millimetres and a slightly tilted downward view.
CAMERA_CENTER = np.array([230.0, 20.0, 600.0])
CAMERA_ROTATION = np.array([math.pi - 0.08, 0.05, 0.0])
# The scan collects 3 rows by 4 columns at 2 flange heights, all at R 0.
GRID = [[200 + 20 * col, 20 * row, z] for z in (50, 90) for row in range(3) for col in range(4)]
POSE = [230, 20, 70, 0]
DICTIONARY = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
# A 24 mm black square plus a 6 mm white margin on every side.
CORNERS = [[-18, 18, 0], [18, 18, 0], [18, -18, 0], [-18, -18, 0]]


def camera(shift=(0, 0, 0), turn=(0, 0, 0)):
    """Return the known camera's rotation vector and translation after a movement.

    The shift moves the camera center in robot millimetres. The turn rotates the
    camera about its own axes, in radians.
    """
    rotation = cv2.Rodrigues(np.asarray(turn, float))[0] @ cv2.Rodrigues(CAMERA_ROTATION)[0]
    return cv2.Rodrigues(rotation)[0].reshape(3), -rotation @ (CAMERA_CENTER + shift)


def marker_center(pose):
    """Marker 4 sits on the J4 axis, at the nominal 100 mm tag height above the flange."""
    return [pose[0], pose[1], pose[2] + 100]


def project(view, point):
    return cv2.projectPoints(np.array([point], float), *view, MATRIX, DISTORTION)[0].reshape(2)


def render(view, *markers):
    """Draw 24 mm markers, given as (ID, 3D center), at 4 times the scale, then reduce."""
    scale = 4
    fine = MATRIX * [[scale], [scale], [1]] + [[0, 0, 1.5], [0, 0, 1.5], [0, 0, 0]]
    image = np.full((SIZE[1] * scale, SIZE[0] * scale), 255, np.uint8)
    for identity, center in markers:
        printed = np.full((384, 384), 255, np.uint8)
        printed[64:320, 64:320] = cv2.aruco.generateImageMarker(DICTIONARY, identity, 256)
        corners = np.asarray(center, float) + CORNERS
        target = cv2.projectPoints(corners, *view, fine, DISTORTION)[0].reshape(4, 2)
        transform = cv2.getPerspectiveTransform(
            np.float32([[-0.5, -0.5], [383.5, -0.5], [383.5, 383.5], [-0.5, 383.5]]),
            target.astype(np.float32),
        )
        warped = cv2.warpPerspective(printed, transform, image.shape[::-1], borderValue=255)
        np.minimum(image, warped, out=image)
    reduced = cv2.resize(image, SIZE, interpolation=cv2.INTER_AREA)
    return Image.fromarray(reduced).convert("RGB")


def model(**changes):
    """The stored model is the known camera before any movement."""
    rotation, translation = camera()
    training = [index for index in range(24) if index not in VALIDATION_INDICES]
    return {
        "schema": 2,
        "scan_r_deg": 0,
        "camera_device": DEVICE,
        "image_size": list(SIZE),
        "camera_matrix": MATRIX.tolist(),
        "distortion": DISTORTION.tolist(),
        "rotation": rotation.tolist(),
        "translation": translation.tolist(),
        "tray_height_mm": 0,
        "distance_mm": 246,
        "nominal_tag_height_mm": 100,
        "nominal_distance_mm": 246,
        "rms_limit_mm": 1,
        "created_at": 1234,
        "rms_mm": {"held_out": 0.1, "tray": 0.2},
        "errors_mm": {"held_out": [0.1] * 6, "tray": [0.2] * 3},
        "points": {
            "tag_flange_mm": GRID,
            "tag_pixels": [[320, 240]] * 24,
            "tray_tip_xy_flange_z_mm": [[0, 0, 0]] * 3,
            "tray_pixels": [[320, 240]] * 3,
            "training_indices": training,
            "validation_indices": list(VALIDATION_INDICES),
            "inlier_indices": training,
        },
        **changes,
    }


def expected(pose=POSE):
    return project(camera(), marker_center(pose))


def at(offset, identity=4, pose=POSE):
    """A detection placed at an exact pixel offset from the expected marker 4 center."""
    return {"id": identity, "center_px": (expected(pose) + offset).tolist()}


def proof(markers=None, pose=POSE, **kwargs):
    if markers is None:
        markers = detect_markers(render(camera(), (4, marker_center(pose))))
    return drift_proof(model(), pose, markers, list(SIZE), device=DEVICE, **kwargs)


def test_marker_proof_is_explicit_and_json_safe():
    result = proof()
    assert result == {
        "passed": True,
        "reason": None,
        "detected_center_px": pytest.approx(expected().tolist(), abs=0.25),
        "expected_center_px": pytest.approx(expected().tolist(), abs=1e-9),
        "distance_px": pytest.approx(0, abs=0.25),
        "limit_px": 3,
        "camera_device": DEVICE,
        "image_size": {"width": 640, "height": 480},
        "model_created_at": 1234,
    }
    assert result["distance_px"] == pytest.approx(
        math.dist(result["detected_center_px"], result["expected_center_px"]), abs=1e-12
    )
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "shift,turn,passes",
    [
        ((1, 0, 0), (0, 0, 0), True),
        ((3, 0, 0), (0, 0, 0), False),
        ((0, 0, 0), (0, math.radians(0.12), 0), True),
        ((0, 0, 0), (math.radians(0.4), 0, 0), False),
    ],
)
def test_camera_movement_beyond_the_limit_fails_and_smaller_movement_passes(shift, turn, passes):
    moved = camera(shift, turn)
    movement = np.linalg.norm(project(moved, marker_center(POSE)) - expected())
    assert abs(movement - 3) > 1  # Each case stays clear of the 3 px limit.
    result = proof(detect_markers(render(moved, (4, marker_center(POSE)))))
    assert result["distance_px"] == pytest.approx(movement, abs=0.3)
    assert result["passed"] is passes
    assert result["reason"] == (None if passes else "marker_beyond_limit")


def test_expected_center_matches_an_independent_radial_pinhole_formula():
    rotation, translation = camera()
    point = cv2.Rodrigues(rotation)[0] @ marker_center(POSE) + translation
    x, y = point[:2] / point[2]
    radial = 1 - 0.05 * (x * x + y * y) + 0.01 * (x * x + y * y) ** 2
    result = proof([at([0, 0])])
    assert result["passed"]
    assert result["expected_center_px"] == pytest.approx(
        [320 + 700 * x * radial, 240 + 700 * y * radial]
    )


def test_nominal_height_gauge_preserves_the_expected_center():
    original = model()
    shifted = copy.deepcopy(original)
    rotation = cv2.Rodrigues(np.asarray(original["rotation"]))[0]
    shifted["nominal_tag_height_mm"] += 7
    shifted["tray_height_mm"] += 7
    shifted["translation"] = (np.asarray(original["translation"]) - rotation[:, 2] * 7).tolist()
    markers = [at([1, 0])]
    before = drift_proof(original, POSE, markers, list(SIZE), device=DEVICE)
    after = drift_proof(shifted, POSE, markers, list(SIZE), device=DEVICE)
    assert before["passed"] and after["passed"]
    assert after["expected_center_px"] == pytest.approx(before["expected_center_px"], abs=1e-9)
    assert original == model()


@pytest.mark.parametrize("z", [-2000, 0])
def test_expected_center_behind_the_camera_never_passes(z):
    calibration = model(translation=[0, 0, z])
    result = drift_proof(calibration, POSE, [at([0, 0])], list(SIZE), device=DEVICE)
    assert result["passed"] is False and result["reason"] == "expected_behind_camera"
    assert result["expected_center_px"] is None


def test_offimage_expected_center_cannot_be_rescued_by_any_marker():
    calibration = model()
    calibration["camera_matrix"][0][2] += 1000
    markers = [{"id": 4, "center_px": [639, 295]}]
    result = drift_proof(calibration, POSE, markers, list(SIZE), device=DEVICE)
    assert result["passed"] is False and result["reason"] == "expected_outside_image"
    assert result["expected_center_px"][0] >= 640
    assert result["detected_center_px"] is None


@pytest.mark.parametrize(
    "markers,reason",
    [
        ([], "marker_missing"),
        ([at([0, 0], identity=7)], "marker_missing"),
        ([at([0, 0]), at([40, 0])], "marker_duplicate"),
        ([at([0, 0]), at([0, 0])], "marker_duplicate"),
        ([None], "marker_detection_malformed"),
        ([{}], "marker_detection_malformed"),
        ([at([0, 0]), {"id": True, "center_px": [1, 1]}], "marker_detection_malformed"),
        ([{"id": "4", "center_px": [1, 1]}], "marker_detection_malformed"),
        ([{"id": 4.0, "center_px": [1, 1]}], "marker_detection_malformed"),
        ([at([0, 0], identity=7)] * 1025, "marker_detection_malformed"),
        ({"markers": []}, "marker_detection_malformed"),
    ],
)
def test_missing_duplicate_or_malformed_marker_evidence_is_negative(markers, reason):
    result = proof(markers)
    assert result["passed"] is False and result["reason"] == reason
    assert result["detected_center_px"] is None and result["distance_px"] is None
    assert result["expected_center_px"] == pytest.approx(expected().tolist())


@pytest.mark.parametrize(
    "center",
    [
        [640, 240],
        [-1, 240],
        [320, 480],
        [float("nan"), 240],
        [float("inf"), 240],
        [True, 240],
        [320],
        "bad",
        None,
        [10**400, 240],
    ],
)
def test_marker_center_must_be_finite_and_inside_the_frame(center):
    result = proof([{"id": 4, "center_px": center}])
    assert result["passed"] is False and result["reason"] == "marker_detection_malformed"
    assert result["detected_center_px"] is None


def test_rendered_frame_without_marker_4_never_passes():
    frame = render(camera(), (7, [240, 30, 170]))
    assert [marker["id"] for marker in detect_markers(frame)] == [7]
    assert proof(detect_markers(frame))["reason"] == "marker_missing"


def test_limit_defaults_to_3_px_and_a_lower_limit_is_stricter():
    assert proof([at([2.9, 0])])["passed"]
    assert proof([at([3.1, 0])])["reason"] == "marker_beyond_limit"
    assert proof([at([0, 1.5])], limit_px=2)["passed"]
    result = proof([at([0, 2.5])], limit_px=2)
    assert result["reason"] == "marker_beyond_limit" and result["limit_px"] == 2
    assert result["distance_px"] == pytest.approx(2.5)


@pytest.mark.parametrize(
    "value", [True, 0, -1, 3.001, 4096, float("nan"), float("inf"), 10**400, "1", None]
)
def test_limit_rejects_invalid_or_raised_values(value):
    with pytest.raises(ValueError):
        drift_limit(value)
    with pytest.raises(ValueError):
        proof([at([0, 0])], limit_px=value)


@pytest.mark.parametrize(
    "flange",
    [
        [True, 20, 70, 0],
        [230, 20, 70],
        [230, 20, 70, float("nan")],
        [230, 20, 70, 10**400],
        [230, 20, np.inf, 0],
        "bad",
    ],
)
def test_flange_pose_rejects_mixed_bool_nonfinite_or_unbounded_values(flange):
    with pytest.raises(ValueError):
        drift_inputs(flange)
    with pytest.raises(ValueError):
        drift_proof(model(), flange, [at([0, 0])], list(SIZE), device=DEVICE)


@pytest.mark.parametrize(
    "pose",
    [
        [199.9, 20, 70, 0],
        [260.1, 20, 70, 0],
        [230, -0.1, 70, 0],
        [230, 40.1, 70, 0],
        [230, 20, 49.9, 0],
        [230, 20, 90.1, 0],
        [230, 20, 70, 0.2],
        [230, 20, 70, -0.2],
        [230, 20, 70, 180],
    ],
)
def test_pose_outside_the_grid_or_away_from_the_scan_r_is_refused(pose):
    with pytest.raises(ValueError, match="inside the calibration grid at the scan R"):
        drift_proof(model(), pose, [at([0, 0], pose=pose)], list(SIZE), device=DEVICE)


@pytest.mark.parametrize(
    "pose",
    [[200, 0, 50, 0.1], [260, 40, 90, -0.1], [230, 20, 70, 360.05], [230, 20, 70, -719.95]],
)
def test_grid_corners_and_whole_turns_of_the_scan_r_are_inside(pose):
    result = drift_proof(model(), pose, [at([0, 0], pose=pose)], list(SIZE), device=DEVICE)
    assert result["passed"]


def test_check_uses_the_stored_scan_r():
    calibration = model(scan_r_deg=-90)
    pose = [230, 20, 70, 270]
    assert drift_proof(calibration, pose, [at([0, 0])], list(SIZE), device=DEVICE)["passed"]
    with pytest.raises(ValueError, match="scan R"):
        drift_proof(calibration, POSE, [at([0, 0])], list(SIZE), device=DEVICE)


def test_older_model_without_scan_r_is_refused():
    calibration = model(schema=1)
    del calibration["scan_r_deg"]
    with pytest.raises(ValueError, match="^camera moved, recalibrate: "):
        drift_proof(calibration, POSE, [at([0, 0])], list(SIZE), device=DEVICE)


def test_wrong_port_size_or_model_is_a_configuration_error():
    with pytest.raises(ValueError, match="port"):
        drift_proof(model(), POSE, [at([0, 0])], list(SIZE), device=DEVICE + "-other")
    with pytest.raises(ValueError, match="image size"):
        drift_proof(model(), POSE, [at([0, 0])], [1280, 960], device=DEVICE)
    calibration = model()
    calibration["translation"][0] = float("nan")
    with pytest.raises(ValueError):
        drift_proof(calibration, POSE, [at([0, 0])], list(SIZE), device=DEVICE)
    for value in (None, float("nan"), True, "0", 1_000_001):
        with pytest.raises(ValueError, match="scan R"):
            drift_proof(model(scan_r_deg=value), POSE, [at([0, 0])], list(SIZE), device=DEVICE)


@pytest.mark.parametrize("device", [None, "", "/dev/video0", "/dev/v4l/by-path/..", True])
def test_drift_requires_explicit_stable_caller_identity(device):
    with pytest.raises(ValueError, match="stable"):
        drift_proof(model(), POSE, [at([0, 0])], list(SIZE), device=device)


@pytest.fixture
def drift_service(tmp_path):
    frames = SimpleNamespace(
        camera_count=1,
        configuration=SimpleNamespace(devices=(DEVICE,)),
        read=Mock(return_value=render(camera(), (4, marker_center(POSE)))),
    )
    predictor = SimpleNamespace(predict=Mock(side_effect=lambda image: (image, {"detections": []})))
    detector = Mock(side_effect=detect_markers)
    service = VisionService(
        demo=True,
        frames=frames,
        localizer_factory=lambda: predictor,
        marker_detector=detector,
        calibration_directory=tmp_path,
    )
    service.calibrations.save(DEVICE, model())
    service.start()
    predictor.predict.reset_mock()
    service.calibrations.load = Mock(wraps=service.calibrations.load)
    service.calibrations.save = Mock(
        side_effect=AssertionError("Drift must not rewrite calibration")
    )
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "drift-test"}
    yield SimpleNamespace(
        service=service,
        frames=frames,
        predictor=predictor,
        detector=detector,
        client=client,
        headers=headers,
    )
    service.close(wait_s=1)


def drift_command(**changes):
    return {"action": "check_calibration", "camera": 1, "flange_mm": POSE, **changes}


def submit(rig, **changes):
    assert rig.client.post("/api/heartbeat", headers=rig.headers, json={}).status_code == 200
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command(**changes))
    assert response.status_code == 202, response.json
    return response.json["command_id"]


def receipt(rig, identity):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = rig.service.snapshot()
        command = state["last_command"]
        if command is not None and command["id"] == identity:
            return command
        threading.Event().wait(0.001)
    pytest.fail("Drift command did not drain")


def test_service_checks_one_owned_frame_model_and_marker_read_without_yolo(
    drift_service, monkeypatch
):
    rig = drift_service
    monkeypatch.setattr("vision.service.robot_xy", Mock(side_effect=AssertionError("No tray map")))
    result = receipt(rig, submit(rig))
    assert result["result"] == "ok" and result["output"]["passed"] is True
    output = result["output"]
    assert output["camera"] == 1 and output["calibrated"] is True
    assert output["camera_device"] == DEVICE and output["limit_px"] == 3
    assert output["distance_px"] < 0.25
    rig.frames.read.assert_called_once_with(1)
    rig.service.calibrations.load.assert_called_once_with(DEVICE)
    rig.detector.assert_called_once_with(rig.frames.read.return_value)
    rig.predictor.predict.assert_not_called()
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("change", ["missing-marker", "moved-camera", "malformed"])
def test_negative_visual_proof_is_explicit_success_not_positive_evidence(drift_service, change):
    rig = drift_service
    if change == "missing-marker":
        rig.frames.read.return_value = render(camera(), (7, [240, 30, 170]))
    elif change == "moved-camera":
        rig.frames.read.return_value = render(camera((3, 0, 0)), (4, marker_center(POSE)))
    else:
        rig.detector.side_effect = lambda image: {"markers": []}
    result = receipt(rig, submit(rig))
    assert result["result"] == "ok"
    assert result["output"]["passed"] is False and result["output"]["reason"]
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("failure", ["missing-model", "wrong-size"])
def test_missing_or_mismatched_model_fails_before_marker_detection(drift_service, failure):
    rig = drift_service
    if failure == "missing-model":
        rig.service.calibrations.load.side_effect = lambda _: None
    else:
        rig.frames.read.return_value = Image.new("RGB", (320, 240))
    result = receipt(rig, submit(rig))
    assert result["result"] == "failed" and result["output"] is None
    assert "recalibrate" in result["message"]
    rig.detector.assert_not_called()


@pytest.mark.parametrize(
    "case,message",
    [
        ("older-model", "camera moved, recalibrate: "),
        ("outside-grid", "inside the calibration grid"),
        ("other-r", "at the scan R"),
        ("duplicate-marker", "duplicate"),
    ],
)
def test_refused_model_pose_or_marker_identity_publishes_no_proof(drift_service, case, message):
    rig = drift_service
    changes = {}
    if case == "older-model":
        older = model(schema=1)
        del older["scan_r_deg"]
        rig.service.calibrations.load.side_effect = lambda _: older
    elif case == "outside-grid":
        changes = dict(flange_mm=[260.5, 20, 70, 0])
    elif case == "other-r":
        changes = dict(flange_mm=[230, 20, 70, 0.5])
    else:
        rig.frames.read.return_value = render(
            camera(), (4, marker_center(POSE)), (4, [180, 60, 170])
        )
    result = receipt(rig, submit(rig, **changes))
    assert result["result"] == "failed" and result["output"] is None
    assert message in result["message"]
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        dict(limit_px=True),
        dict(limit_px=0),
        dict(limit_px=3.5),
        dict(limit_px="1"),
        dict(flange_mm=[230, 20, 70]),
        dict(flange_mm=[230, False, 70, 0]),
        dict(tool_offset_mm=[0, 0, 146]),
        dict(margin_px=0),
        dict(camera=True),
        dict(extra="not-allowed"),
    ],
)
def test_invalid_drift_command_never_reads_a_frame(drift_service, bad):
    rig = drift_service
    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command(**bad))
    assert response.status_code == 400
    rig.frames.read.assert_not_called()


@pytest.mark.parametrize("device", [None, "", "/dev/video0"])
def test_invalid_configured_identity_refuses_before_frame_acquisition(drift_service, device):
    rig = drift_service
    rig.service._devices = (device,)
    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command())
    assert response.status_code == 400
    rig.frames.read.assert_not_called()


def test_drift_keeps_lease_and_declared_parameter_guards(drift_service):
    rig = drift_service
    assert rig.client.post("/api/command", json=drift_command()).status_code == 400
    assert (
        rig.client.post("/api/command", headers=rig.headers, json=drift_command()).status_code
        == 409
    )
    params = rig.service.describe()["commands"]["check_calibration"]["params"]
    assert set(params["required"]) == {"camera", "flange_mm"}
    assert set(params["properties"]) == {"camera", "flange_mm", "limit_px"}
    assert params["properties"]["limit_px"]["default"] == 3
    assert params["properties"]["limit_px"]["maximum"] == 3
    rig.frames.read.assert_not_called()


@pytest.mark.parametrize("cancel", ["stop", "release-reacquire", "expiry-reacquire", "close"])
@pytest.mark.parametrize("positive", [True, False])
def test_interrupted_marker_read_never_publishes_or_revives_a_drift_proof(
    drift_service, cancel, positive
):
    rig = drift_service
    entered, finish = threading.Event(), threading.Event()

    def detect(image):
        entered.set()
        assert finish.wait(2)
        return detect_markers(image) if positive else []

    rig.detector.side_effect = detect
    identity = submit(rig)
    try:
        assert entered.wait(2)
        if cancel == "stop":
            assert rig.service.stop("another-authorized-client") == "pending"
        elif cancel == "close":
            assert rig.service.close(wait_s=0) == "pending"
        else:
            if cancel == "release-reacquire":
                rig.service.lease.release("drift-test")
            else:
                rig.service.lease.clock = lambda: time.monotonic() + 10
                rig.service.lease.poll()
            rig.service.lease.acquire("drift-test")
        assert rig.service.snapshot()["busy"]
        with pytest.raises(ServiceError):
            rig.service.command(
                "check_calibration",
                {k: v for k, v in drift_command().items() if k != "action"},
                "drift-test",
            )
    finally:
        finish.set()
    result = receipt(rig, identity)
    assert result["result"] == "stopped" and result["output"] is None
    rig.frames.read.assert_called_once_with(1)
    rig.detector.assert_called_once()
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("positive", [True, False])
def test_publication_checks_expiry_even_when_background_poll_has_not_run(
    drift_service, monkeypatch, positive
):
    rig = drift_service
    # Stop only the test instance's poll loop, leaving service admission unchanged.
    rig.service._quit.set()
    rig.service._poller.join(timeout=1)
    assert not rig.service._poller.is_alive()
    original = rig.service._process

    def processed(job):
        result, image = original(job)
        if not positive:
            result.update(
                passed=False, reason="marker_missing", detected_center_px=None, distance_px=None
            )
        rig.service.lease.clock = lambda: time.monotonic() + 10
        assert not job["cancel"].is_set()  # The publication fence must discover expiry itself.
        return result, image

    monkeypatch.setattr(rig.service, "_process", processed)
    result = receipt(rig, submit(rig))
    assert result["result"] == "stopped" and result["output"] is None
    assert rig.service.expiry_count == 1


class DriftVision:
    def __init__(self):
        self.calls = []
        self.after = None
        self.output = {**proof(), "camera": 1, "calibrated": True}
        self.value = dict(
            connected=True,
            busy=False,
            locked=False,
            fault=None,
            lease=dict(holder="owned-run", remaining_s=2),
            last_command=None,
        )

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("lease", {}).get("holder") == "owned-run"

    def command(self, action, **params):
        self.calls.append((action, params))
        self.value["last_command"] = dict(
            id="drift-1",
            action=action,
            client="owned-run",
            result="ok",
            output=copy.deepcopy(self.output),
        )
        if self.after:
            self.after()
        return {"command_id": "drift-1"}


@pytest.fixture
def drift_rig(geometry, monkeypatch, tmp_path):
    monkeypatch.delenv(DRIFT_SETTING, raising=False)
    robot, vision = Robot(), DriftVision()
    plan = ZonePlan({}, ("robot", "vision"), tmp_path / "occupancy.json", "d" * 64)
    zones = Zones(plan, dict(robot=robot, vision=vision), holder="owned-run", audit=lambda *a: True)
    authority = _Authority(time.monotonic() + 3)
    storage = SimpleNamespace(event=Mock())
    ctx = Context(
        authority, dict(robot=robot, vision=vision), storage, "owned-run", 1, "chip", 0, zones
    )
    return SimpleNamespace(ctx=ctx, robot=robot, vision=vision, authority=authority, zones=zones)


def test_guarded_skill_checks_without_robot_commands_or_zone_clearance(drift_rig):
    rig = drift_rig
    result = check_calibration(rig.ctx, 1)
    assert result == rig.vision.output and result["passed"] is True
    assert rig.vision.calls == [
        ("check_calibration", dict(camera=1, flange_mm=[200, 0, 50, 0], limit_px=3))
    ]
    assert rig.robot.reads >= 3 and rig.robot.calls == []
    assert rig.zones.snapshot() == {}
    assert rig.zones.error is None


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False, "reason": "marker_beyond_limit"},
        {"passed": 1},
        {"calibrated": 1},
        {"camera": True},
        {"camera": 2},
        {"limit_px": 2},
        {"limit_px": float("nan")},
        {"reason": "unexpected"},
        {"model_created_at": 0},
        {"model_created_at": 10**400},
        {"camera_device": None},
        {"camera_device": "/dev/video0"},
        {"image_size": {"width": True, "height": 480}},
        {"image_size": {"width": 640, "height": 480, "extra": 1}},
        {"image_size": {"width": 200, "height": 480}},
        {"detected_center_px": [float("nan"), 240]},
        {"detected_center_px": [True, 240]},
        {"expected_center_px": [-1, 240]},
        {"expected_center_px": None},
        {"distance_px": 3.5},
        {"distance_px": -0.1},
        {"unknown": "extra-field"},
    ],
)
def test_negative_or_malformed_proof_raises_drift_failure_without_motion(drift_rig, changes):
    rig = drift_rig
    rig.vision.output.update(changes)
    with pytest.raises(RuntimeError, match="^camera moved, recalibrate$"):
        check_calibration(rig.ctx, 1)
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


@pytest.mark.parametrize("change", ["far-center", "wrong-distance"])
def test_reported_distance_must_agree_with_the_reported_centers(drift_rig, change):
    output = drift_rig.vision.output
    if change == "far-center":
        x, y = output["expected_center_px"]
        output["detected_center_px"] = [x + 4, y]
    else:
        output["distance_px"] += 0.5
    with pytest.raises(RuntimeError, match="^camera moved, recalibrate$"):
        check_calibration(drift_rig.ctx, 1)


@pytest.mark.parametrize(
    "field",
    [
        "camera_device",
        "detected_center_px",
        "expected_center_px",
        "distance_px",
        "limit_px",
        "model_created_at",
    ],
)
def test_missing_proof_evidence_is_not_success(drift_rig, field):
    del drift_rig.vision.output[field]
    with pytest.raises(RuntimeError, match="^camera moved, recalibrate$"):
        check_calibration(drift_rig.ctx, 1)


@pytest.mark.parametrize("mutation", ["x", "r", "owner", "instance", "stale", "disabled"])
def test_robot_guard_preserves_motion_or_identity_failure_before_proof(drift_rig, mutation):
    rig = drift_rig
    rig.vision.output.update(passed=False, reason="marker_missing")

    def change():
        if mutation in {"x", "r"}:
            rig.robot.value["actual"][0 if mutation == "x" else 3] += 1
        elif mutation == "owner":
            rig.robot.value["controller_id"] = "foreign-client"
        elif mutation == "instance":
            rig.robot.value["instance"] = "restarted"
        else:
            rig.robot.value["feedback_fresh" if mutation == "stale" else "enabled"] = False

    rig.vision.after = change
    with pytest.raises(RuntimeError) as caught:
        check_calibration(rig.ctx, 1)
    assert str(caught.value) != "camera moved, recalibrate"
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


def test_prior_cancellation_cause_is_not_replaced_with_drift(drift_rig):
    rig = drift_rig
    cause = RuntimeError("operator Stop won first")
    rig.vision.after = lambda: rig.authority.revoke(cause)
    rig.vision.output.update(passed=False, reason="marker_missing")
    with pytest.raises(RuntimeError, match="operator Stop won first") as caught:
        check_calibration(rig.ctx, 1)
    assert str(caught.value) == str(cause)
    assert rig.authority.revocation_error is cause and rig.robot.calls == []


@pytest.mark.parametrize(
    "field,value",
    [("id", "other"), ("action", "detect"), ("client", "foreign"), ("result", "failed")],
)
def test_skill_requires_exact_action_client_and_success_receipt(drift_rig, field, value):
    rig = drift_rig
    rig.vision.after = lambda: rig.vision.value["last_command"].update({field: value})
    with pytest.raises(RuntimeError, match="Vision"):
        check_calibration(rig.ctx, 1)
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


@pytest.mark.parametrize("value", ["", "nan", "inf", "-1", "0", "3.01", "4096", "not-a-number"])
def test_bad_or_raised_limit_setting_fails_before_service_or_robot_reads(
    drift_rig, monkeypatch, value
):
    monkeypatch.setenv(DRIFT_SETTING, value)
    with pytest.raises(ValueError, match=DRIFT_SETTING):
        check_calibration(drift_rig.ctx, 1)
    assert drift_rig.robot.reads == 0 and drift_rig.vision.calls == []


def test_lower_limit_setting_is_forwarded_and_must_match_the_receipt(drift_rig, monkeypatch):
    monkeypatch.setenv(DRIFT_SETTING, "2.5")
    drift_rig.vision.output["limit_px"] = 2.5
    assert check_calibration(drift_rig.ctx, 1)["limit_px"] == 2.5
    assert drift_rig.vision.calls[0][1]["limit_px"] == 2.5


def test_registry_exposes_only_the_typed_camera_scalar():
    from orchestrator.skills import default_registry

    skill = default_registry()["vision.check_calibration"]
    assert skill.services == ("robot", "vision")
    assert skill.validate({"camera": 1}) == {"camera": 1}
    for params in ({}, {"camera": True}, {"camera": 1, "limit_px": 1}):
        with pytest.raises(ValueError):
            skill.validate(params)


@pytest.mark.parametrize("present", [False, True])
def test_owned_demo_filters_preserve_only_supplied_limit(monkeypatch, present):
    import os

    from orchestrator.app import demo_environment
    from orchestrator.cli import _demo_environment

    poisoned = {
        "MG400_VISION_TOKEN": "private",
        "MG400_CAMERA_DEVICES": "/dev/not-owned",
        "MG400_VISION_CALIBRATION_DIR": "/host/not-owned",
        "HTTP_PROXY": "bad",
        "PYTHONPATH": "/host/python",
        "MG400_CAMERA_CALIBRATION_REFERENCES": "/host/ref",
        "MG400_ORCH_DRIFT_MARGIN_PX": "0",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    if present:
        monkeypatch.setenv(DRIFT_SETTING, "2.5")
    else:
        monkeypatch.delenv(DRIFT_SETTING, raising=False)
    original = dict(os.environ)
    filtered = demo_environment(original)
    assert filtered.get(DRIFT_SETTING) == ("2.5" if present else None)
    assert not poisoned.keys() & filtered.keys()
    with _demo_environment():
        assert os.environ.get(DRIFT_SETTING) == ("2.5" if present else None)
        assert not poisoned.keys() & os.environ.keys()
    assert dict(os.environ) == original


def runner_proof():
    return {
        "camera": 1,
        "calibrated": True,
        "passed": True,
        "reason": None,
        "detected_center_px": [321.0, 240.0],
        "expected_center_px": [320.0, 240.0],
        "distance_px": 1.0,
        "limit_px": 3.0,
        "camera_device": DEVICE,
        "image_size": {"width": 640, "height": 480},
        "model_created_at": 1234,
    }


class RunnerDriftVision(FakeService):
    def __init__(self, result):
        super().__init__("vision")
        self.value["lease"] = {"holder": self.identity}
        self.result = result
        self.entered = threading.Event()
        self.finish = threading.Event()

    def command(self, action, **params):
        assert action == "check_calibration"
        assert params == {"camera": 1, "flange_mm": [200, 0, 50, 0], "limit_px": 3}
        self.events.append(action)
        self.value["busy"] = True
        self.entered.set()
        assert self.finish.wait(4), "The test must release the accepted fake command"
        self.value.update(
            busy=False,
            last_command={
                "id": "drift-command-1",
                "action": action,
                "client": self.identity,
                "result": "ok",
                "output": copy.deepcopy(self.result),
            },
        )
        return {"command_id": "drift-command-1"}


@contextmanager
def drift_runner(tmp_path, monkeypatch, result):
    monkeypatch.setenv(DRIFT_SETTING, "3")
    robot, vision, registry = FakeService(), RunnerDriftVision(result), Registry()
    robot.value.update(
        enabled=True,
        robot_mode=5,
        gripper_available=True,
        gripper_busy=False,
        keyboard_enabled=False,
        actual=[200, 0, 50, 0, 1000],
        gripper={"outcome": None},
    )

    def occupy(ctx):
        ctx.check()

    def later_move(ctx):
        ctx.clients["robot"].command("later-move")

    registry.register("occupy", occupy, services=("robot",))
    registry.register("vision.check_calibration", check_calibration, services=("robot", "vision"))
    registry.register("later.move", later_move, services=("robot",))
    recipe = validate(
        {
            "name": "Drift before further movement",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": "occupy", "params": {}, "timeout_s": 5, "zone": "tray"},
                {
                    "skill": "vision.check_calibration",
                    "params": {"camera": 1},
                    "timeout_s": 5,
                },
                {"skill": "later.move", "params": {}, "timeout_s": 5},
            ],
        },
        registry,
    )
    directory = tmp_path / "zones"
    directory.mkdir(mode=0o750)
    plan = ZonePlan(
        {"tray": {"owner": "robot", "conditions": ()}},
        ("robot", "vision"),
        directory / "occupancy.json",
        "d" * 64,
    )
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(
            recipe, registry, {"robot": robot, "vision": vision}, store, zone_plan=plan
        ).start()
        try:
            assert vision.entered.wait(3), runner.snapshot()
            claim = runner.snapshot()["zones"]["tray"]
            assert claim["status"] == "occupied"
            assert claim["holder"] == runner.run_id
            assert store.get(runner.run_id)["steps"][0]["status"] == "completed"
            yield runner, robot, vision, store, claim
        finally:
            runner.abort()
            vision.finish.set()
            assert runner.wait_closed(5), runner.snapshot()


def assert_drift_held(runner, robot, vision, store, claim, reason):
    until(lambda: runner.snapshot()["settled"])
    state = runner.snapshot()
    assert state["status"] == "held"
    assert state["detail"] == reason
    assert "later-move" not in robot.events
    assert vision.events.count("check_calibration") == 1
    for service in (robot, vision):
        assert "stop" in service.events
        assert service.events.count("acquire") == 1
        assert "release" not in service.events
    retained = state["zones"]["tray"]
    assert retained["status"] == "unknown"
    for field in ("holder", "device", "generation", "config"):
        assert retained[field] == claim[field]
    steps = store.get(runner.run_id)["steps"]
    assert steps[0]["status"] == "completed"
    assert steps[1]["status"] == "failed"
    assert steps[2]["status"] == "pending"


@pytest.mark.parametrize("kind", ["negative", "malformed", "malformed-envelope"])
def test_drift_proof_holds_before_next_move_and_retains_prior_zone(
    geometry, tmp_path, monkeypatch, kind
):
    result = runner_proof()
    reason = "camera moved, recalibrate"
    if kind == "negative":
        result.update(passed=False, reason="marker_beyond_limit")
    elif kind == "malformed":
        del result["model_created_at"]
    else:
        result = None
        reason = "Vision calibration failed: "
    with drift_runner(tmp_path, monkeypatch, result) as (runner, robot, vision, store, claim):
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


def test_drift_valid_proof_can_reach_the_later_fake_move(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, runner_proof()) as (runner, robot, vision, store, _):
        vision.finish.set()
        assert runner.wait(3) == "completed"
        assert robot.events.count("later-move") == 1
        assert vision.events.count("check_calibration") == 1
        assert [step["status"] for step in store.get(runner.run_id)["steps"]] == [
            "completed",
            "completed",
            "completed",
        ]


def test_drift_late_bad_proof_preserves_earlier_operator_hold(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, {}) as (runner, robot, vision, store, claim):
        reason = "Operator Stop preceded the drift result"
        assert runner.hold(reason)
        until(lambda: runner.status == "held" and all("stop" in s.events for s in (robot, vision)))
        assert not runner.snapshot()["settled"]
        assert all("release" not in s.events for s in (robot, vision))
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


def test_drift_late_bad_proof_preserves_first_instance_revocation(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, {}) as (runner, robot, vision, store, claim):
        robot.value["instance"] = "replacement-robot-instance"
        until(lambda: runner.status == "held" and all("stop" in s.events for s in (robot, vision)))
        reason = runner.snapshot()["detail"]
        assert reason in {
            "A zone service instance changed.",
            "Robot ownership or instance changed during transfer.",
        }
        assert not runner.snapshot()["settled"]
        assert all("release" not in s.events for s in (robot, vision))
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_worker_bootstrap_cannot_admit_a_marker_read_or_hide_an_active_job(
    drift_service, monkeypatch, late, interrupt
):
    rig = drift_service
    original = threading.Thread.start
    workers = []

    def failed_start(worker):
        if worker.name != "vision-inference":
            return original(worker)
        if late:
            original(worker)
            workers.append(worker)
        if interrupt:
            raise KeyboardInterrupt("injected startup interruption")
        raise RuntimeError("injected startup failure")

    rig.service.lease.acquire("drift-test")
    args = {key: value for key, value in drift_command().items() if key != "action"}
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    try:
        with pytest.raises(KeyboardInterrupt if interrupt else ServiceError):
            rig.service.command("check_calibration", args, "drift-test")
    finally:
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    rig.frames.read.assert_not_called()
    rig.detector.assert_not_called()
    state = rig.service.snapshot()
    assert not state["busy"] and state["last_command"]["result"] == "failed"
    assert state["last_command"]["output"] is None
    assert rig.service.stop("other-authorized-client") == "confirmed"


@pytest.mark.parametrize("cancel", ["stop", "close"])
def test_stop_during_successful_worker_bootstrap_drains_without_frame_access(
    drift_service, monkeypatch, cancel
):
    rig = drift_service
    original = threading.Thread.start
    workers = []

    def stopped_start(worker):
        original(worker)
        if worker.name == "vision-inference":
            workers.append(worker)
            outcome = rig.service.stop("other") if cancel == "stop" else rig.service.close(0)
            assert outcome == "pending"
            assert rig.service.snapshot()["busy"]

    monkeypatch.setattr(threading.Thread, "start", stopped_start)
    identity = submit(rig)
    result = receipt(rig, identity)
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert result["result"] == "stopped" and result["output"] is None
    rig.frames.read.assert_not_called()
    rig.detector.assert_not_called()


def test_denied_bootstrap_cannot_clobber_a_later_successful_job(drift_service, monkeypatch):
    rig = drift_service
    original = threading.Thread.start
    denied = []

    def failed_start(worker):
        if worker.name == "vision-inference":
            denied.append(worker)
            raise RuntimeError("start denied before bootstrap")
        return original(worker)

    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", failed_start)
        response = rig.client.post("/api/command", headers=rig.headers, json=drift_command())
        assert response.status_code == 503
    successful = receipt(rig, submit(rig))
    assert successful["result"] == "ok" and successful["output"]["passed"]
    assert len(denied) == 1
    original(denied[0])
    denied[0].join(timeout=1)
    assert not denied[0].is_alive()
    assert rig.service.snapshot()["last_command"] == successful
    rig.frames.read.assert_called_once_with(1)
    rig.detector.assert_called_once()
