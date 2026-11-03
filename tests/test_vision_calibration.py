"""Synthetic camera geometry and fake service I/O; never open a camera."""

import copy
import hashlib
import importlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image
from test_orchestrator_transfer import Robot

from orchestrator.runner import Context, Revoked, _Authority
from orchestrator.skills.calibrate import calibrate
from orchestrator.stations import AXES, StationStore, Workspace
from orchestrator.tool import Pose, ToolOffset, fingertip_to_flange
from orchestrator.zones import ZonePlan, Zones
from vision.calibration import (
    VALIDATION_INDICES,
    CalibrationStore,
    _distance_guess,
    _dlt,
    _minimize_distance,
    _project_dlt,
    _ransac,
    detect_markers,
    fit_calibration,
    robot_xy,
    validate_model,
)
from vision.service import VisionService, create_app

DEVICE = "/dev/v4l/by-path/platform-offline-usb-0:1.0-video-index0"


def camera_model():
    return {
        "schema": 1,
        "camera_device": DEVICE,
        "camera_matrix": [[1000, 0, 100], [0, 1000, 80], [0, 0, 1]],
        "distortion": [0.01, -0.001, 0, 0, 0],
        "rotation": [math.pi, 0, 0],
        "translation": [0, 0, 1000],
        "tray_height_mm": 0,
        "distance_mm": 246,
        "nominal_tag_height_mm": 100,
        "nominal_distance_mm": 246,
        "rms_limit_mm": 1,
        "image_size": [200, 160],
        "rms_mm": {"held_out": 0.1, "tray": 0.2},
        "created_at": 1,
        "points": {
            "tag_flange_mm": [[0, 0, 0]] * 24,
            "tag_pixels": [[100, 80]] * 24,
            "tray_tip_xy_flange_z_mm": [[0, 0, 0]] * 3,
            "tray_pixels": [[100, 80]] * 3,
            "training_indices": [index for index in range(24) if index not in VALIDATION_INDICES],
            "validation_indices": list(VALIDATION_INDICES),
            "inlier_indices": [index for index in range(24) if index not in VALIDATION_INDICES],
        },
        "errors_mm": {"held_out": [0.1] * 6, "tray": [0.2] * 3},
    }


@pytest.fixture
def calibration_store(tmp_path):
    root = tmp_path / "calibrations"
    root.mkdir(mode=0o700)
    return CalibrationStore(root)


def test_store_roundtrip_uses_complete_port_identity_and_private_atomic_file(calibration_store):
    saved = calibration_store.save(DEVICE, camera_model())
    assert calibration_store.load(DEVICE) == saved
    assert calibration_store.load(DEVICE.replace("0:1.0", "0:2.0")) is None
    files = list(calibration_store.directory.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == hashlib.sha256(DEVICE.encode()).hexdigest() + ".json"
    assert files[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["file-link", "hard-link", "directory-link", "public-file"])
def test_store_refuses_links_and_public_data(calibration_store, tmp_path, kind):
    calibration_store.save(DEVICE, camera_model())
    path = next(calibration_store.directory.glob("*.json"))
    if kind == "file-link":
        other = tmp_path / "other.json"
        path.rename(other)
        path.symlink_to(other)
    elif kind == "hard-link":
        os.link(path, tmp_path / "other.json")
    elif kind == "directory-link":
        link = tmp_path / "linked"
        link.symlink_to(calibration_store.directory, target_is_directory=True)
        calibration_store = CalibrationStore(link)
    else:
        path.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        calibration_store.load(DEVICE)
    with pytest.raises((ValueError, OSError)):
        calibration_store.save(DEVICE, camera_model())


def test_revocation_before_atomic_publication_preserves_previous_model(calibration_store):
    initial = calibration_store.save(DEVICE, camera_model())

    @contextmanager
    def revoked():
        raise RuntimeError("Revoked before commit")
        yield

    changed = {**camera_model(), "tray_height_mm": 12}
    with pytest.raises(RuntimeError, match="Revoked"):
        calibration_store.save(DEVICE, changed, commit_guard=revoked)
    assert calibration_store.load(DEVICE) == initial
    assert not list(calibration_store.directory.glob(".calibration-*"))


@pytest.mark.parametrize("when", ["before", "after"])
def test_rebound_directory_never_reports_current_calibration_installed(
    calibration_store, monkeypatch, when
):
    initial = calibration_store.save(DEVICE, camera_model())
    root = calibration_store.directory
    detached = root.with_name("detached")

    def rebind():
        root.rename(detached)
        root.mkdir(mode=0o700)

    @contextmanager
    def before_commit():
        if when == "before":
            rebind()
        yield

    original = os.replace

    def after_replace(*args, **kwargs):
        original(*args, **kwargs)
        if when == "after":
            rebind()

    monkeypatch.setattr(os, "replace", after_replace)
    with pytest.raises(RuntimeError, match="directory changed"):
        calibration_store.save(
            DEVICE, {**camera_model(), "tray_height_mm": 12}, commit_guard=before_commit
        )
    assert calibration_store.load(DEVICE) is None
    assert CalibrationStore(detached).load(DEVICE)["tray_height_mm"] == (
        initial["tray_height_mm"] if when == "before" else 12
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("distortion", [0.1, 0.2, 0.01, 0, 0]),
        ("camera_matrix", [[0, 0, 100], [0, 1000, 80], [0, 0, 1]]),
        ("rotation", [math.nan, 0, 0]),
        ("image_size", [True, 160]),
        ("distance_mm", float("inf")),
        ("camera_device", "/dev/video0"),
    ],
)
def test_loaded_geometry_must_have_finite_supported_parameters(field, value):
    with pytest.raises(ValueError):
        validate_model({**camera_model(), field: value})


def test_distorted_pixel_ray_uses_each_points_own_height():
    model = camera_model()
    xyz = np.array([[30.0, -20.0, 45.0]])
    pixels = cv2.projectPoints(
        xyz,
        np.array(model["rotation"]),
        np.array(model["translation"], dtype=float),
        np.array(model["camera_matrix"], dtype=float),
        np.array(model["distortion"]),
    )[0][0, 0]
    assert robot_xy(model, pixels, (200, 160), height_mm=45) == pytest.approx([30, -20])
    assert robot_xy(model, pixels, (200, 160)) != pytest.approx([30, -20])
    with pytest.raises(ValueError, match="image size"):
        robot_xy(model, pixels, (400, 320))
    with pytest.raises(ValueError, match="behind"):
        robot_xy(model, pixels, (200, 160), height_mm=1100)


def test_aruco_reader_accepts_arm4_and_tray7_and_rejects_duplicates():
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    image = np.full((240, 400, 3), 255, np.uint8)
    for identity, x in ((4, 30), (7, 230)):
        marker = cv2.aruco.generateImageMarker(dictionary, identity, 120)
        image[60:180, x : x + 120] = marker[..., None]
    markers = detect_markers(Image.fromarray(image))
    assert {marker["id"] for marker in markers} == {4, 7}
    assert next(marker["center_px"] for marker in markers if marker["id"] == 4) == pytest.approx(
        [89.5, 119.5], abs=0.1
    )
    image[60:180, 230:350] = image[60:180, 30:150]
    with pytest.raises(ValueError, match="duplicate"):
        detect_markers(Image.fromarray(image))


@pytest.fixture
def calibrated_service(calibration_store):
    calibration_store.save(DEVICE, camera_model())
    image = Image.new("RGB", (200, 160), "white")
    frames = SimpleNamespace(
        camera_count=1,
        configuration=SimpleNamespace(devices=(DEVICE,)),
        read=lambda _: image,
    )

    class Model:
        def predict(self, current):
            return current, {"detections": [{"center_xy": [100, 80]}]}

    service = VisionService(
        demo=True,
        frames=frames,
        localizer_factory=Model,
        calibration_directory=calibration_store.directory,
    )
    service.start()
    yield service, frames
    service.close()


def test_service_adds_robot_mm_and_refuses_changed_image_size(calibrated_service):
    service, frames = calibrated_service
    result = service.read("detect", 1)
    assert result["calibrated"] is True
    assert result["detections"][0]["robot_mm"] == pytest.approx({"x": 0, "y": 0})
    assert service.snapshot()["calibrated_cameras"] == [1]
    frames.read = lambda _: Image.new("RGB", (400, 320))
    with pytest.raises(Exception, match="image size"):
        service.read("detect", 1)


def test_detection_robot_xy_is_invariant_under_nominal_tag_height_gauge(calibrated_service):
    service, _ = calibrated_service
    first = service.read("detect", 1)["detections"][0]["robot_mm"]
    model = service.calibrations.load(DEVICE)
    rotation = cv2.Rodrigues(np.asarray(model["rotation"], dtype=float))[0]
    model["nominal_tag_height_mm"] += 7
    model["tray_height_mm"] += 7
    model["translation"] = (np.asarray(model["translation"]) - rotation[:, 2] * 7).tolist()
    service.calibrations.save(DEVICE, model)
    second = service.read("detect", 1)["detections"][0]["robot_mm"]
    assert set(first) == set(second) == {"x", "y"}
    assert second == pytest.approx(first, abs=1e-9)


def test_marker_commands_share_access_and_provide_bounded_frame_identity(calibrated_service):
    service, _ = calibrated_service
    service._markers = lambda _: [{"id": 4, "center_px": [100, 80]}]
    client = create_app(service, token="x" * 24).test_client()
    body = {"action": "read_markers", "camera": 1}
    assert client.post("/api/command", json=body).status_code in (400, 401)
    headers = {"Authorization": "Bearer " + "x" * 24, "X-MG400-Client": "calibration"}
    assert client.post("/api/command", headers=headers, json=body).status_code == 409
    assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
    response = client.post("/api/command", headers=headers, json=body)
    assert response.status_code == 202
    deadline = time.monotonic() + 2
    while service.snapshot()["busy"] and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    receipt = service.snapshot()["last_command"]
    assert receipt["id"] == response.json["command_id"] and receipt["result"] == "ok"
    result = receipt["output"]
    assert result["camera_device"] == DEVICE and len(result["frame_sha256"]) == 64
    assert result["markers"] == [{"id": 4, "center_px": [100, 80]}]


@pytest.mark.parametrize("cancel", ["stop", "release-reacquire", "expiry"])
def test_interrupted_solve_cannot_publish_or_revive_after_same_client_reacquisition(
    calibrated_service, monkeypatch, cancel
):
    service, _ = calibrated_service
    original = service.calibrations.load(DEVICE)
    entered, finish = threading.Event(), threading.Event()

    def solve(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return {**camera_model(), "tray_height_mm": 12}

    monkeypatch.setattr("vision.calibration.fit_calibration", solve)
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "calibration"}
    assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
    response = client.post(
        "/api/command",
        headers=headers,
        json={
            "action": "calibrate_camera",
            "camera": 1,
            "tag_points": [[0, 0, 0]] * 24,
            "tag_pixels": [[100, 80]] * 24,
            "tray_points": [[0, 0, 0]] * 3,
            "tray_pixels": [[100, 80]] * 3,
            "image_size": [200, 160],
        },
    )
    try:
        assert response.status_code == 202 and entered.wait(2)
        if cancel == "stop":
            assert client.post("/api/stop", headers=headers, json={}).status_code == 200
        elif cancel == "release-reacquire":
            assert client.post("/api/release", headers=headers, json={}).status_code == 200
            assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
        else:
            service.lease.clock = lambda: time.monotonic() + 10
            service.lease.poll()
    finally:
        finish.set()
    deadline = time.monotonic() + 2
    while service.snapshot()["busy"] and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    receipt = service.snapshot()["last_command"]
    assert receipt["id"] == response.json["command_id"] and receipt["result"] == "stopped"
    assert receipt["output"] is None
    assert service.calibrations.load(DEVICE) == original


# The numerical cases use the same public ray mapping as service detections.
def _mapped_pixels(calibration, pixels, image_size, *, height_mm=None):
    return np.asarray(
        [
            robot_xy(calibration, pixel, image_size, height_mm=height_mm)
            for pixel in np.asarray(pixels).reshape(-1, 2)
        ]
    )


SIZE = (1920, 1080)


def synthetic(*, distance=250.0, noise=0.02, outliers=()):
    tags = np.array(
        [
            [x, y, z]
            for z in (80.0, 140.0)
            for y in (-330.0, -255.0, -180.0)
            for x in (-110.0, -60.0, -10.0, 40.0)
        ]
    )
    references = np.array(
        [[-107.26, -335.72, 24.10], [39.14, -187, 22.46], [-105.84, -187.09, 22.48]]
    )
    matrix = np.array([[1200.0, 0, 960], [0, 1180, 540], [0, 0, 1]])
    rotation = np.array([np.pi - 0.12, 0.04, -0.03])
    center = np.array([-35.0, -255.0, 600.0])
    translation = -cv2.Rodrigues(rotation)[0] @ center
    distortion = np.array([-0.07, 0.02, 0, 0, 0])
    random = np.random.default_rng(4)

    def project(objects):
        pixels = cv2.projectPoints(objects, rotation, translation, matrix, distortion)[0].reshape(
            -1, 2
        )
        return pixels + random.normal(0, noise, pixels.shape)

    pixels = project(tags + [0, 0, 100])
    reference_pixels = project(references + [0, 0, 100 - distance])
    for index in outliers:
        pixels[index] += [35, -28]
    return tags, pixels, references, reference_pixels


def test_joint_fit_distortion_outliers_and_own_height_validation():
    tags, pixels, references, reference_pixels = synthetic(outliers=(2, 14))
    result = fit_calibration(tags, pixels, references, reference_pixels, SIZE)
    assert abs(result["distance_mm"] - 250) < 0.5
    assert result["rms_mm"]["held_out"] < 0.2
    assert result["rms_mm"]["tray"] < 0.2
    assert not set(VALIDATION_INDICES) & set(result["points"]["inlier_indices"])
    assert not {2, 14} & set(result["points"]["inlier_indices"])
    assert result["distortion"][2:] == [0, 0, 0]
    json.dumps(result, allow_nan=False)
    for point, pixel in zip(references, reference_pixels):
        xy = _mapped_pixels(result, [pixel], SIZE, height_mm=point[2] + 100 - result["distance_mm"])
        assert np.linalg.norm(xy[0] - point[:2]) < 0.3
    with pytest.raises(ValueError, match="image size"):
        _mapped_pixels(result, [[100, 100]], (640, 480))


def test_dlt_recovers_projective_camera_without_coordinate_scale_bias():
    tags, pixels, _, _ = synthetic(noise=0)
    known = np.array([[1.2, 0.1, 0.4, 10], [0.2, 1.1, 0.3, 50], [0.0001, 0.0002, 0.001, 1]])
    pixels = _project_dlt(known, tags)
    recovered = _dlt(tags, pixels)
    np.testing.assert_allclose(_project_dlt(recovered, tags), pixels, atol=1e-9)


def test_coplanar_dlt_is_rejected():
    tags, pixels, _, _ = synthetic()
    tags[:, 2] = 100
    with pytest.raises(ValueError, match="coplanar"):
        _dlt(tags, pixels)


def test_cancel_during_ransac_does_not_return_calibration():
    inputs = synthetic()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 8

    with pytest.raises(RuntimeError, match="cancelled"):
        fit_calibration(*inputs, SIZE, cancelled=cancelled)


def test_offset_gauge_changes_camera_translation_but_not_physical_distance():
    inputs = synthetic(noise=0.0)
    first = fit_calibration(*inputs, SIZE)
    second = fit_calibration(*inputs, SIZE, nominal_tag_height_mm=107.0)
    assert abs(first["distance_mm"] - 250) < 0.01
    assert abs(second["distance_mm"] - first["distance_mm"]) < 0.01
    assert abs(second["tray_height_mm"] - first["tray_height_mm"] - 7) < 0.01
    rotation = cv2.Rodrigues(np.asarray(first["rotation"]))[0]
    np.testing.assert_allclose(
        np.asarray(second["translation"]) - first["translation"],
        -rotation[:, 2] * 7,
        atol=0.02,
    )


def test_out_of_range_distance_is_recovered_then_refused_without_boundary_clipping():
    inputs = synthetic(distance=268, noise=0)
    with pytest.raises(ValueError, match="nominal difference"):
        fit_calibration(*inputs, SIZE)
    unaccepted = fit_calibration(*inputs, SIZE, distance_tolerance_mm=30)
    assert abs(unaccepted["distance_mm"] - 268) < 0.01


def test_holdout_pixels_cannot_change_fitted_model_or_distance():
    inputs = list(synthetic(noise=0))
    before = fit_calibration(*inputs, SIZE)
    inputs[1] = inputs[1].copy()
    inputs[1][list(VALIDATION_INDICES)] += [0.2, -0.2]
    after = fit_calibration(*inputs, SIZE)
    for field in ("camera_matrix", "distortion", "rotation", "translation", "distance_mm"):
        np.testing.assert_array_equal(after[field], before[field])
    assert after["rms_mm"]["held_out"] > before["rms_mm"]["held_out"]
    inputs[1][VALIDATION_INDICES[0]] += [40, 0]
    with pytest.raises(ValueError, match="RMS"):
        fit_calibration(*inputs, SIZE)


def test_reference_residuals_are_measured_at_each_own_height():
    inputs = synthetic(noise=0)
    result = fit_calibration(*inputs, SIZE)
    references, pixels = inputs[2:]
    expected_heights = references[:, 2] + 100 - result["distance_mm"]
    assert np.ptp(expected_heights) > 1
    own = np.array(
        [
            _mapped_pixels(result, [pixel], SIZE, height_mm=height)[0]
            for pixel, height in zip(pixels, expected_heights)
        ]
    )
    own_rms = np.sqrt(np.mean(np.sum((own - references[:, :2]) ** 2, axis=1)))
    flattened = _mapped_pixels(result, pixels, SIZE)
    flat_rms = np.sqrt(np.mean(np.sum((flattened - references[:, :2]) ** 2, axis=1)))
    assert own_rms == pytest.approx(result["rms_mm"]["tray"], abs=1e-9)
    assert flat_rms > own_rms + 0.05


def test_near_vertical_reference_rays_refuse_unobservable_distance():
    matrix = np.array([[1200.0, 0, 960], [0, 1200, 540], [0, 0, 1]])
    rotation = np.array([np.pi, 0.0, 0.0])
    translation = np.array([0.0, 0.0, 700.0])
    model = (0, matrix, np.zeros(5), rotation, translation)
    references = np.array([[0, 0, 20], [1e-9, 0, 20], [0, 1e-9, 20]])
    pixels = np.array([[960, 540], [960 + 1e-9, 540], [960, 540 + 1e-9]])
    with pytest.raises(ValueError, match="too vertical"):
        _distance_guess(model, references, pixels, 100)


def test_flat_profile_refuses_distance_instead_of_accepting_initial_guess():
    with pytest.raises(ValueError, match="not observable"):
        _minimize_distance(lambda distance: (0.0, None), 246, lambda: False)


def test_cancel_during_joint_distance_search_stops_refinement():
    inputs = synthetic()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 520

    with pytest.raises(RuntimeError, match="cancelled"):
        fit_calibration(*inputs, SIZE, cancelled=cancelled)
    assert calls == 520


def test_ray_plane_intersection_refuses_parallel_or_behind_camera():
    result = {
        "image_size": list(SIZE),
        "camera_matrix": np.eye(3).tolist(),
        "distortion": [0.0] * 5,
        "rotation": [0.0, 0.0, 0.0],
        "translation": [0.0, 0.0, 0.0],
        "tray_height_mm": -1.0,
    }
    with pytest.raises(ValueError, match="behind"):
        _mapped_pixels(result, [[0.0, 0.0]], SIZE)
    result["rotation"] = [0.0, np.pi / 2, 0.0]
    with pytest.raises(ValueError, match="parallel"):
        _mapped_pixels(result, [[0.0, 0.0]], SIZE)


@pytest.mark.parametrize("outlier_count", [6, 7])
def test_ransac_requires_twelve_training_tag_inliers(outlier_count):
    tags, _, _, _ = synthetic()
    training = [index for index in range(24) if index not in VALIDATION_INDICES]
    points = tags[training]
    projection = np.array([[1.2, 0.1, 0.4, 10], [0.2, 1.1, 0.3, 50], [0.0001, 0.0002, 0.001, 1]])
    pixels = _project_dlt(projection, points)
    random = np.random.default_rng(17)
    pixels[-outlier_count:] += random.uniform(100, 1000, (outlier_count, 2))
    if outlier_count == 7:
        with pytest.raises(ValueError, match="Fewer than 12"):
            _ransac(points, pixels, lambda: False)
    else:
        fitted, mask = _ransac(points, pixels, lambda: False)
        np.testing.assert_array_equal(np.flatnonzero(mask), np.arange(12))
        np.testing.assert_allclose(_project_dlt(fitted, points[:12]), pixels[:12], atol=1e-8)


def test_public_robot_xy_agrees_with_fitting_rays_at_distorted_image_edges():
    from vision.calibration import robot_xy

    model = {
        "image_size": list(SIZE),
        "camera_matrix": [[800.0, 0, 960], [0, 780.0, 540], [0, 0, 1]],
        "distortion": [-0.25, 0.06, 0, 0, 0],
        "rotation": [np.pi, 0.0, 0.0],
        "translation": [0.0, 0.0, 700.0],
        "tray_height_mm": -120.0,
    }
    pixels = np.array([[8, 8], [1910, 8], [1910, 1070], [8, 1070]], dtype=np.float64)
    previous = cv2.undistortPointsIter(
        pixels.reshape(-1, 1, 2),
        np.asarray(model["camera_matrix"]),
        np.asarray(model["distortion"]),
        None,
        None,
        (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-12),
    ).reshape(-1, 2)
    previous_projection = cv2.projectPoints(
        np.column_stack((previous, np.ones(4))),
        np.zeros(3),
        np.zeros(3),
        np.asarray(model["camera_matrix"]),
        np.asarray(model["distortion"]),
    )[0].reshape(-1, 2)
    assert np.max(np.abs(previous_projection - pixels)) > 0.01
    expected = _mapped_pixels(model, pixels, SIZE)
    actual = np.array([robot_xy(model, pixel.tolist(), SIZE) for pixel in pixels])
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-9)
    world = np.column_stack((actual, np.full(4, model["tray_height_mm"])))
    reprojection = cv2.projectPoints(
        world,
        np.asarray(model["rotation"]),
        np.asarray(model["translation"]),
        np.asarray(model["camera_matrix"]),
        np.asarray(model["distortion"]),
    )[0].reshape(-1, 2)
    np.testing.assert_allclose(reprojection, pixels, rtol=0, atol=1e-7)


def test_ray_inversion_refuses_unconverged_normalized_coordinates(monkeypatch):
    model = {
        "image_size": list(SIZE),
        "camera_matrix": [[800.0, 0, 960], [0, 780.0, 540], [0, 0, 1]],
        "distortion": [-0.25, 0.06, 0, 0, 0],
        "rotation": [np.pi, 0.0, 0.0],
        "translation": [0.0, 0.0, 700.0],
        "tray_height_mm": -120.0,
    }
    monkeypatch.setattr(cv2, "undistortPointsIter", lambda *args: np.zeros((1, 1, 2)))
    with pytest.raises(ValueError, match="did not converge"):
        _mapped_pixels(model, [[8.0, 8.0]], SIZE)


@pytest.fixture
def scan_rig(monkeypatch):
    module = importlib.import_module("orchestrator.skills.calibrate")
    position = [10.0, 20.0, 100.0, 0.0]
    moves, commands, checked, zones, artifacts = [], [], [], [], []
    state = SimpleNamespace(failure=None, count=0, revoked=False, zone_owner="robot", limit=1000)

    class Transfer:
        offset = ToolOffset(0, 0, 146)
        stations = {
            "a": {"x_mm": 10, "y_mm": 20, "z_mm": -46, "r_deg": 0, "zone": "tray"},
            "b": {"x_mm": 40, "y_mm": 60, "z_mm": -46, "r_deg": 0, "zone": "tray"},
        }

        def __init__(self, ctx):
            pass

        def pose(self, station):
            return Pose(*[station[key] for key in ("x_mm", "y_mm", "z_mm", "r_deg")])

        def _inside(self, pose):
            checked.append(pose)
            if pose.x_mm > state.limit:
                raise ValueError("Outside configured workspace")
            return pose

        def state(self):
            return {"actual": list(position) + [1000]}, None

        def move(self, pose):
            check()
            flange = fingertip_to_flange(pose, self.offset)
            position[:] = [flange.x_mm, flange.y_mm, flange.z_mm, flange.r_deg]
            moves.append(list(position))

    class Vision:
        receipt = None

        def owned(self, snapshot):
            return snapshot["lease"]["holder"] == "calibration"

        def state(self):
            return {
                "lease": {"holder": "calibration"},
                "busy": False,
                "locked": False,
                "fault": None,
                "connected": True,
                "last_command": self.receipt,
            }

        def command(self, action, **params):
            check()
            commands.append((action, params))
            identity = str(len(commands))
            if action == "read_markers":
                state.count += 1
                jitter = (state.count - 1) % 5 - 2
                fingerprint = hashlib.sha256(str(state.count).encode()).hexdigest()
                result = {
                    "camera": 1,
                    "camera_device": DEVICE,
                    "image_size": {"width": 200, "height": 160},
                    "frame_sha256": fingerprint,
                    "markers": [
                        {"id": marker, "center_px": [100 + position[0] / 10 + jitter, 80 + jitter]}
                        for marker in (1, 2, 4)
                    ],
                }
                if state.count == 3:
                    if state.failure == "move":
                        position[0] += 0.2
                    elif state.failure == "duplicate":
                        result["frame_sha256"] = hashlib.sha256(b"2").hexdigest()
                    elif state.failure == "camera":
                        result["camera_device"] = DEVICE.replace("0:1.0", "0:2.0")
                    elif state.failure == "size":
                        result["image_size"]["width"] = 400
                    elif state.failure == "missing":
                        result["markers"] = []
                    elif state.failure == "revoke":
                        state.revoked = True
            else:
                result = {"camera": 1, "calibrated": True, "calibration": camera_model()}
            self.receipt = {
                "id": identity,
                "action": action,
                "client": "calibration",
                "result": "ok",
                "output": result,
            }
            if state.failure == "receipt" and state.count == 3:
                self.receipt["action"] = "unrelated"
            return {"command_id": identity}

    def check():
        if state.revoked:
            raise RuntimeError("Authority revoked")

    @contextmanager
    def guard(operation):
        operation()
        yield
        operation()

    def event(*args):
        check()

    ctx = SimpleNamespace(
        clients={"vision": Vision()},
        check=check,
        sleep=lambda _: check(),
        guard=guard,
        zone_definitions=lambda: {"tray": {"owner": state.zone_owner}},
        zones=SimpleNamespace(
            declare=lambda name: zones.append(("enter", name)) or name,
            finish=lambda entry, **kwargs: zones.append(("finish", entry, kwargs)),
        ),
        store=SimpleNamespace(event=event, artifact=lambda name, data: artifacts.append(data) or 1),
    )
    monkeypatch.setattr(module, "_Transfer", Transfer)
    # This measured contact is deliberately outside the grid. It must remain data.
    monkeypatch.setattr(
        module,
        "_references",
        lambda _: [
            (1, Pose(-107, -335, 24, -78)),
            (2, Pose(39, -187, 22, 51)),
        ],
    )
    return SimpleNamespace(
        module=module,
        ctx=ctx,
        state=state,
        moves=moves,
        commands=commands,
        checked=checked,
        position=position,
        zones=zones,
        artifacts=artifacts,
        transfer_type=Transfer,
    )


def test_scan_collects_exact_grid_and_five_frame_medians_without_commanding_contacts(scan_rig):
    rig = scan_rig
    result = rig.module.calibrate(rig.ctx, 1, "a", "b", 100, 140)
    assert result["camera"] == 1
    assert len(rig.checked) == 24 and len(rig.moves) == 23
    assert len([action for action, _ in rig.commands if action == "read_markers"]) == 125
    action, sent = rig.commands[-1]
    assert action == "calibrate_camera"
    expected = [[x, y, z] for z in (100, 140) for y in (20, 40, 60) for x in (10, 20, 30, 40)]
    assert sent["tag_points"] == expected
    assert sent["tag_pixels"] == [[100 + point[0] / 10, 80] for point in expected]
    assert sent["tray_points"] == [[-107, -335, 24], [39, -187, 22]]
    assert [move[:3] for move in rig.moves] == expected[1:]
    assert rig.zones == [("enter", "tray"), ("finish", "tray", {})]
    assert len(rig.artifacts) == 1


def test_tray_contact_xy_uses_each_recorded_r_and_tool_xy_without_applying_dz(
    scan_rig, monkeypatch
):
    rig = scan_rig
    rig.transfer_type.offset = ToolOffset(2, 3, 146)
    rig.position[:2] = [8, 17]
    monkeypatch.setattr(
        rig.module,
        "_references",
        lambda _: [
            (1, Pose(10, 20, 24, 0)),
            (2, Pose(30, 40, 22, 90)),
        ],
    )
    rig.module.calibrate(rig.ctx, 1, "a", "b", 100, 140)
    sent = rig.commands[-1][1]
    assert sent["tray_points"] == [[12, 23, 24], [27, 42, 22]]
    # Tag locations stay measured flange XYZ, independent of tip XY and dz.
    assert sent["tag_points"][0] == [8, 17, 100]
    provenance = json.loads(rig.artifacts[0])["reference_provenance"]
    assert provenance == {
        "flange_poses_mm": [[10, 20, 24, 0], [30, 40, 22, 90]],
        "marker_ids": [1, 2],
        "tool_offset_mm": [2, 3, 146],
    }


@pytest.mark.parametrize(
    "failure", ["move", "duplicate", "camera", "size", "missing", "revoke", "receipt"]
)
def test_failed_acquisition_never_moves_retries_or_fits(scan_rig, failure):
    rig = scan_rig
    rig.state.failure = failure
    with pytest.raises(RuntimeError):
        rig.module.calibrate(rig.ctx, 1, "a", "b", 100, 140)
    assert not rig.moves and not rig.artifacts
    assert len(rig.commands) == 3 and all(action == "read_markers" for action, _ in rig.commands)
    assert rig.zones[-1] == ("finish", "tray", {"exceptional": True})


@pytest.mark.parametrize("failure", ["wrong-start", "grid-workspace", "vision-zone", "height"])
def test_full_scan_admission_refuses_before_any_capture_or_motion(scan_rig, failure):
    rig = scan_rig
    high = 140
    if failure == "wrong-start":
        rig.position[0] += 1
    elif failure == "grid-workspace":
        rig.state.limit = 39
    elif failure == "vision-zone":
        rig.state.zone_owner = "vision"
    else:
        high = 139.9
    with pytest.raises((ValueError, RuntimeError)):
        rig.module.calibrate(rig.ctx, 1, "a", "b", 100, high)
    assert not rig.commands and not rig.moves and not rig.zones


class AuthorityVision:
    def __init__(self):
        self.reads = 0
        self.calls = []
        self.after = None
        self.value = dict(
            connected=True,
            busy=False,
            locked=False,
            fault=None,
            instance="vision-review",
            lease=dict(holder="owned-run", remaining_s=2),
            last_command=None,
        )

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("lease", {}).get("holder") == "owned-run"

    def healthy(self, state):
        return state.get("connected") is True

    def command(self, action, **params):
        self.calls.append((action, params))
        identity = str(len(self.calls))
        if action == "read_markers":
            self.reads += 1
            output = dict(
                camera=1,
                camera_device="/dev/v4l/by-path/fake-review",
                image_size=dict(width=1280, height=720),
                frame_sha256=f"{self.reads:064x}",
                markers=[dict(id=i, center_px=[100 + i, 200]) for i in (0, 1, 4)],
            )
        else:
            output = dict(
                camera=1,
                calibrated=True,
                calibration=dict(
                    camera_device="/dev/v4l/by-path/fake-review",
                    image_size=[1280, 720],
                    rms_mm={"held_out": 0.1, "tray": 0.1},
                    distance_mm=246,
                ),
            )
        self.value["last_command"] = dict(
            id=identity, action=action, client="owned-run", result="ok", output=output
        )
        if self.after:
            self.after(action)
        return {"command_id": identity}


@pytest.fixture
def authority_rig(tmp_path, monkeypatch):
    workspace = Workspace(dict(x_mm=[0, 500], y_mm=[-200, 200], z_mm=[0, 250], r_deg=[-360, 360]))
    for field, axis in AXES.items():
        for n, side in enumerate(("MIN", "MAX")):
            monkeypatch.setenv(
                f"MG400_ORCH_{axis}_{side}_{'DEG' if axis == 'R' else 'MM'}",
                str(workspace.bounds[field][n]),
            )
    store = StationStore(workspace, tmp_path / "stations.toml")
    store.save("a", Pose(200, 0, 50, 0), approach_mm=10, zone="scan")
    store.save("b", Pose(260, 40, 50, 0), approach_mm=10, zone="scan")
    tool = tmp_path / "tool.toml"
    tool.write_text("offset_mm=[0,0,0]\n")
    tool.chmod(0o640)
    refs = tmp_path / "references.toml"
    refs.write_text(
        "[[references]]\nid=0\nflange_mm=[200,0,20,0]\n[[references]]\nid=1\nflange_mm=[260,40,20,0]\n"
    )
    refs.chmod(0o640)
    monkeypatch.setenv("MG400_ORCH_STATIONS_FILE", str(store.path))
    monkeypatch.setenv("MG400_ORCH_TOOL_FILE", str(tool))
    monkeypatch.setenv("MG400_CAMERA_CALIBRATION_REFERENCES", str(refs))
    robot = Robot()
    vision = AuthorityVision()
    robot.healthy = lambda state: (
        state.get("syncing") is True and state.get("feedback_fresh") is True
    )
    definitions = {"scan": dict(owner="robot", conditions=())}
    plan = ZonePlan(definitions, ("robot", "vision"), tmp_path / "occupancy.json", "a" * 64)
    zones = Zones(
        plan, dict(robot=robot, vision=vision), holder="review-run", audit=lambda *args: True
    )
    authority = _Authority(time.monotonic() + 5)
    artifacts = []
    storage = SimpleNamespace(
        event=lambda *args, **kwargs: None,
        artifact=lambda *args, **kwargs: artifacts.append(args) or "artifact",
    )
    ctx = Context(
        authority, dict(robot=robot, vision=vision), storage, "review-run", 1, "chip", 0, zones
    )
    return SimpleNamespace(
        ctx=ctx, robot=robot, vision=vision, zones=zones, authority=authority, artifacts=artifacts
    )


def test_grid_uses_exact_first_pose_23_guarded_moves_and_retains_zone(authority_rig):
    result = calibrate(authority_rig.ctx, 1, "a", "b", 50, 90)
    assert result["artifact_id"] == "artifact"
    assert len(authority_rig.robot.calls) == 23
    assert all(
        action == "execute" and args["speed"] == 1 for action, args in authority_rig.robot.calls
    )
    points = authority_rig.vision.calls[-1][1]["tag_points"]
    assert points == [
        [200 + 20 * col, 20 * row, z] for z in (50, 90) for row in range(3) for col in range(4)
    ]
    assert authority_rig.vision.reads == 125  # 24*5 arm frames plus5 tray frames.
    assert authority_rig.zones.snapshot()["scan"]["device"] == "robot"
    assert authority_rig.zones.snapshot()["scan"]["status"] == "occupied"


@pytest.mark.parametrize("bad", ["first", "height", "workspace", "zone"])
def test_preflight_refuses_before_any_motion_or_vision_command(authority_rig, bad):
    high = 90
    if bad == "first":
        authority_rig.robot.value["actual"][0] += 1
    if bad == "height":
        high = 89
    if bad == "workspace":
        high = 251
    if bad == "zone":
        authority_rig.ctx._Context__zone_definitions["scan"]["owner"] = "vision"
    with pytest.raises((ValueError, RuntimeError)):
        calibrate(authority_rig.ctx, 1, "a", "b", 50, high)
    assert (
        not authority_rig.robot.calls
        and not authority_rig.vision.calls
        and not authority_rig.artifacts
    )


@pytest.mark.parametrize("bad", ["moving", "owner", "receipt", "cancel"])
def test_capture_failure_stops_scan_before_further_motion_or_publication(authority_rig, bad):
    def change(action):
        if action != "read_markers":
            return
        if bad == "moving":
            authority_rig.robot.value["actual"][0] += 1
        if bad == "owner":
            authority_rig.robot.value["controller_id"] = "other"
        if bad == "receipt":
            authority_rig.vision.value["last_command"]["id"] = "superseding"
        if bad == "cancel":
            authority_rig.authority.revoke(RuntimeError("review cancellation"))

    authority_rig.vision.after = change
    with pytest.raises((RuntimeError, Revoked)):
        calibrate(authority_rig.ctx, 1, "a", "b", 50, 90)
    assert not authority_rig.robot.calls and not authority_rig.artifacts
    assert authority_rig.zones.snapshot()["scan"]["status"] == "unknown"
    assert len(authority_rig.vision.calls) == 1


@pytest.mark.parametrize("health", ["fault-missing", "disconnected"])
def test_unknown_vision_health_refuses_before_dispatch(authority_rig, health):
    if health == "fault-missing":
        authority_rig.vision.value.pop("fault")
    else:
        authority_rig.vision.value["connected"] = False
    with pytest.raises(RuntimeError):
        calibrate(authority_rig.ctx, 1, "a", "b", 50, 90)
    assert (
        authority_rig.vision.calls == []
        and authority_rig.robot.calls == []
        and authority_rig.artifacts == []
    )
