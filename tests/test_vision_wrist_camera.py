import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from vision import wrist_camera

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "scripts/wrist_camera_calibrate.py"
    spec = importlib.util.spec_from_file_location("wrist_camera_calibrate_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulated_records(scale, offset, yaw_deg):
    """Frames of a flat 24 mm board from a camera on the wrist link, with 0.2 px noise."""
    rng = np.random.default_rng(3)
    matrix = np.array([[1235.0, 0, 1900], [0, 1232.0, 1140], [0, 0, 1]])
    distortion = np.array([0.22, 2.2, -0.0007, -0.0004, 0.03, 0.28, 2.1, 0.17])
    tilt = cv2.Rodrigues(np.radians([0.4, -0.3, 0.0]))[0]
    rotation = cv2.Rodrigues(tilt @ wrist_camera._down(np.radians(yaw_deg)))[0].ravel()
    corners = {
        row * 10 + column: np.array([-130 + 24.0 * column, -330 + 24.0 * row, -12.0])
        for row in range(7)
        for column in range(10)
    }
    records = []
    for dz, xs, ys in (
        (0, (-40, 0, 40, 80), (0, -40, -80)),
        (-20, (-40, 40), (0, -80)),
        (-38, (0,), (0, -40)),
    ):
        for dx in xs:
            for dy in ys:
                reported = np.array([-39.0 + dx, -236.0 + dy, 163.8 + dz])
                real = reported * [scale, scale, 1.0]
                j1 = np.arctan2(reported[1], reported[0])
                points = np.array(list(corners.values()))
                linked = wrist_camera.link_points(points, real, np.full(len(points), j1), offset)
                pixels = wrist_camera.project(matrix, distortion, rotation, linked)
                pixels += rng.normal(0, 0.2, pixels.shape)
                board = {
                    str(corner): pixel.tolist()
                    for corner, pixel in zip(corners, pixels)
                    if 20 < pixel[0] < 3820 and 20 < pixel[1] < 2140
                }
                records.append({"pose": [*reported, -35.0], "board": board})
    return records


def test_fit_recovers_the_lens_the_mount_and_the_arm_scale():
    result = wrist_camera.fit(simulated_records(0.9877, (-42.0, 43.0), 179.6))
    matrix = np.array(result["camera_matrix"])
    assert result["rms_px"] < 0.5 and result["max_px"] < wrist_camera.OUTLIER_PX
    assert matrix[0, 0] == pytest.approx(1235, rel=0.01)
    assert matrix[1, 1] == pytest.approx(1232, rel=0.01)
    assert result["arm_lateral_scale"] == pytest.approx(0.9877, abs=0.002)
    assert result["mount"]["offset_xy_mm"] == pytest.approx([-42.0, 43.0], abs=1.5)
    assert len(result["distortion"]) == 8 and result["poses"] == 18


def test_fit_refuses_too_few_poses():
    with pytest.raises(ValueError, match="8 poses"):
        wrist_camera.fit(simulated_records(1.0, (0.0, 0.0), 0.0)[:5])


def test_board_corners_are_found_in_the_board_image():
    page = np.full((8 * 60 + 80, 11 * 60 + 80), 255, np.uint8)
    page[40:-40, 40:-40] = wrist_camera.board_image(60)
    corners = wrist_camera.board_corners(page)
    assert sorted(corners) == list(range(70))
    assert corners[0] == pytest.approx((100, 100), abs=1.0)
    assert corners[69] == pytest.approx((640, 460), abs=1.0)


def test_stored_calibration_is_valid(tmp_path):
    stored = wrist_camera.load()
    assert stored["camera_matrix"][0, 0] == pytest.approx(wrist_camera.NOMINAL_FOCAL_PX, rel=0.05)
    assert stored["distortion"].shape == (8,) and stored["rms_px"] <= wrist_camera.MAX_RMS_PX
    assert 20 < stored["mount"]["camera_above_tip_mm"] < 50
    bad = tmp_path / "wrist.json"
    bad.write_text(wrist_camera.CONFIG.read_text().replace('"schema": 1', '"schema": 2'))
    with pytest.raises(ValueError, match="Invalid wrist camera calibration"):
        wrist_camera.load(bad)


def simulated_model():
    tilt = cv2.Rodrigues(np.radians([0.4, -0.3, 0.0]))[0]
    return {
        "camera_matrix": np.array([[1235.0, 0, 1900], [0, 1232.0, 1140], [0, 0, 1]]),
        "distortion": np.array([0.22, 2.2, -0.0007, -0.0004, 0.03, 0.28, 2.1, 0.17]),
        "mount": {
            "offset_xy_mm": [-42.0, 43.0],
            "rotation_link_to_camera": (tilt @ wrist_camera._down(np.radians(179.6))).tolist(),
            "camera_above_tip_mm": 35.0,
        },
        "arm_lateral_scale": 0.9877,
    }


def simulated_pixel(model, point, pose):
    """Pixel of a real point. Its z is the flange z at which the lens is at its height."""
    real = np.array([pose[0], pose[1], pose[2]]) * [0.9877, 0.9877, 1.0]
    j1 = np.arctan2(pose[1], pose[0])
    linked = wrist_camera.link_points(
        np.array([point]), real, np.array([j1]), model["mount"]["offset_xy_mm"]
    )
    rotation = cv2.Rodrigues(np.array(model["mount"]["rotation_link_to_camera"]))[0]
    return wrist_camera.project(model["camera_matrix"], model["distortion"], rotation, linked)[0]


def test_pixels_map_to_arm_coordinates_on_a_surface_and_from_2_views():
    model = simulated_model()
    # A point at reported arm (150, -180), on a surface that the tip touches at flange z -70.
    point = np.array([150 * 0.9877, -180 * 0.9877, -70.0 - 35.0])
    poses = [(100.0, -230.0, 120.0, 0.0), (160.0, -215.0, 120.0, 0.0), (130.0, -220.0, 60.0, 0.0)]
    pixels = [simulated_pixel(model, point, pose) for pose in poses]
    for pixel, pose in zip(pixels, poses):
        found = wrist_camera.surface_points(model, [pixel], pose, -70.0)[0]
        assert found == pytest.approx([150.0, -180.0], abs=0.01)
    x, y, touch_z, _ = wrist_camera.locate(model, pixels, poses)
    assert (x, y, touch_z) == pytest.approx((150.0, -180.0, -70.0), abs=0.01)
    with pytest.raises(ValueError, match="2 different viewing directions"):
        wrist_camera.locate(model, pixels[:1], poses[:1])
    del model["mount"]["camera_above_tip_mm"]
    assert wrist_camera.locate(model, pixels, poses)[2] is None
    with pytest.raises(ValueError, match="no camera height"):
        wrist_camera.surface_points(model, pixels[:1], poses[0], -70.0)


def test_a_new_fit_keeps_the_stored_camera_height(tmp_path, monkeypatch, capsys):
    script = load_script()
    out = tmp_path / "wrist.json"
    out.write_text(wrist_camera.CONFIG.read_text())
    stored = wrist_camera.load(out)
    result = {**json.loads(out.read_text()), "rms_px": 0.4}
    del result["mount"]["camera_above_tip_mm"]
    monkeypatch.setattr(script.wrist_camera, "fit", lambda records, square_mm: result)
    script.fit([], 24.0, out)
    assert wrist_camera.load(out)["mount"]["camera_above_tip_mm"] == pytest.approx(
        stored["mount"]["camera_above_tip_mm"]
    )
    result["rms_px"] = 1.5
    with pytest.raises(RuntimeError, match="above the limit"):
        script.fit([], 24.0, out)


def test_run_refuses_a_start_that_was_not_checked():
    script = load_script()
    near = {a: (1000.0 + 170.0 * (a % 10), 500.0 + 170.0 * (a // 10)) for a in range(70)}
    script.check_start(script.START, near, 24.0)
    with pytest.raises(RuntimeError, match="Park the arm"):
        script.check_start((335.0, -3.0, -28.0), near, 24.0)
    with pytest.raises(RuntimeError, match="does not see the board"):
        script.check_start(script.START, {}, 24.0)
    far = {a: (x / 2, y / 2) for a, (x, y) in near.items()}
    with pytest.raises(RuntimeError, match="below the camera"):
        script.check_start(script.START, far, 24.0)
