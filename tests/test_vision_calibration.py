"""Synthetic camera geometry and fake service I/O; never open a camera."""

import hashlib
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
        "image_size": [200, 160],
        "rms_mm": {"held_out": 0.1, "tray": 0.2},
        "created_at": 1,
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
    assert result["detections"][0]["robot_mm"] == pytest.approx({"x": 0, "y": 0, "z": 0})
    assert service.snapshot()["calibrated_cameras"] == [1]
    frames.read = lambda _: Image.new("RGB", (400, 320))
    with pytest.raises(Exception, match="image size"):
        service.read("detect", 1)


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
