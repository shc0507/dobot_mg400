import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image

from vision import wrist_camera
from vision.service import VisionService, create_app

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
    records = simulated_records(0.9877, (-42.0, 43.0), 179.6)
    result = wrist_camera.fit(records)
    matrix = np.array(result["camera_matrix"])
    radius = np.mean([np.hypot(*record["pose"][:2]) for record in records])
    assert result["rms_px"] < 0.5 and result["max_px"] < wrist_camera.OUTLIER_PX
    assert matrix[0, 0] == pytest.approx(1235, rel=0.01)
    assert matrix[1, 1] == pytest.approx(1232, rel=0.01)
    assert result["arm_radial_scale"] == pytest.approx(0.9877, abs=0.002)
    assert result["calibration_radius_mm"] == pytest.approx(radius)
    # The stored offset is for the arm frame without the scale, at the mean radius.
    expected = [-42.0 - (1 - 0.9877) * radius, 43.0]
    assert result["mount"]["offset_xy_mm"] == pytest.approx(expected, abs=1.5)
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
    bad.write_text(wrist_camera.CONFIG.read_text().replace('"schema": 3', '"schema": 2'))
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
        "arm": dict(wrist_camera.NO_ARM),
        "tool": {"offset_xy_mm": [0.0, 0.0]},
    }


def simulated_pixel(model, point, pose):
    """Pixel of a point. Its z is the flange z at which the lens is at its height."""
    real = np.array([pose[0], pose[1], pose[2]])
    j1 = np.arctan2(pose[1], pose[0])
    linked = wrist_camera.link_points(
        np.array([point]), real, np.array([j1]), model["mount"]["offset_xy_mm"]
    )
    rotation = cv2.Rodrigues(np.array(model["mount"]["rotation_link_to_camera"]))[0]
    return wrist_camera.project(model["camera_matrix"], model["distortion"], rotation, linked)[0]


def test_pixels_map_to_arm_coordinates_on_a_surface_and_from_2_views():
    model = simulated_model()
    # A point at arm (150, -180), on a surface that the tip touches at flange z -70.
    point = np.array([150.0, -180.0, -70.0 - 35.0])
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


def test_arm_scales_and_cup_offset_change_the_arm_coordinates():
    plain = simulated_model()
    pose = (100.0, -230.0, 120.0, 30.0)
    pixel = simulated_pixel(plain, np.array([150.0, -180.0, -105.0]), pose)
    model = {
        **plain,
        "arm": {
            "radial_scale": 1.02,
            "radial_drift": -0.01,
            "radius_mm": 240,
        },
        "tool": {"offset_xy_mm": [1.0, 0.5]},
    }
    # The vector from the flange to the point, along the reach and sideways.
    reach = np.array(pose[:2]) / np.hypot(*pose[:2])
    side = np.array([-reach[1], reach[0]])
    vector = np.array([150.0, -180.0]) - pose[:2]
    along = 1.02 * (vector @ reach) - 0.01 * (np.hypot(*pose[:2]) - 240)
    cup = [np.cos(np.radians(30)) - 0.5 * np.sin(np.radians(30))]
    cup.append(np.sin(np.radians(30)) + 0.5 * np.cos(np.radians(30)))
    expected = pose[:2] + along * reach + (vector @ side) * side - cup
    found = wrist_camera.surface_points(model, [pixel], pose, -70.0)
    assert found[0] == pytest.approx(expected, abs=0.01)
    assert wrist_camera.surface_pixels(model, found, pose, -70.0)[0] == pytest.approx(
        pixel, abs=0.05
    )


def touch_data(truth, touch_z=23.5):
    """Looks at fixed points and chip moves, as a camera with the calibration `truth` sees them."""
    rng = np.random.default_rng(5)
    look = np.array([-39.0, -236.0, 163.8, -25.2])
    poses = [look + [dx, dy, 0, 0] for dx in (-40, 20, 80) for dy in (0, -40, -80)]

    def pixel(place, pose):
        return wrist_camera.surface_pixels(truth, [place], pose, touch_z)[0].tolist()

    def turned(degrees, vector):
        cos, sin = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
        return np.array([cos * vector[0] - sin * vector[1], sin * vector[0] + cos * vector[1]])

    looks, moves = [], []
    for number in range(12):
        place = [rng.uniform(-100, 30), rng.uniform(-330, -200)]
        looks += [{"point": number, "pose": p.tolist(), "pixel": pixel(place, p)} for p in poses]
    cup = turned(look[3], truth["tool"]["offset_xy_mm"])
    for _ in range(16):
        pick, put = ([rng.uniform(-100, 30), rng.uniform(-330, -200)] for _ in range(2))
        turn = float(rng.uniform(-60, 60))
        long = turned(rng.uniform(0, 180), [1.0, 0.0])
        # The cup seals at a place along the length of the chip, in the middle of its width.
        picked = np.array(pick) + rng.uniform(-3, 3) * long
        seen_after = np.array(put) + turned(turn, np.array(pick) + cup - picked) - cup
        moves.append(
            {
                "pose_before": look.tolist(),
                "pixel_before": pixel(pick, look),
                "picked_at": picked.tolist(),
                "across": [-long[1], long[0]],
                "turn": turn,
                "put": put,
                "pose_after": look.tolist(),
                "pixel_after": pixel(seen_after, look),
            }
        )
    return {"touch_z": touch_z, "looks": looks, "moves": moves}


def touch_truth():
    stored = wrist_camera.load()
    mount = {**stored["mount"], "offset_xy_mm": [-44.0, 42.5], "camera_above_tip_mm": 35.0}
    arm = {"radial_scale": 1.02, "radial_drift": -0.004}
    arm["radius_mm"] = stored["arm"]["radius_mm"]
    return {**stored, "mount": mount, "arm": arm, "tool": {"offset_xy_mm": [-0.6, 0.4]}}


def test_touch_fit_recovers_the_cup_offset_and_the_arm_scales():
    truth = touch_truth()
    data = touch_data(truth)
    result = wrist_camera.fit_touch(wrist_camera.load(), data["looks"], data["moves"], 23.5)
    assert result["tool"]["offset_xy_mm"] == pytest.approx([-0.6, 0.4], abs=0.02)
    assert result["arm"]["radial_scale"] == pytest.approx(1.02, abs=0.0005)
    assert result["arm"]["radial_drift"] == pytest.approx(-0.004, abs=0.0005)
    assert result["mount"]["camera_above_tip_mm"] == pytest.approx(35.0, abs=0.05)
    assert result["touch_fit"]["seal_rms_mm"] < 0.01 and result["touch_fit"]["moves"] == 16
    # The fitted calibration gives the pick point of each chip: the sealed place across the width.
    for move in data["moves"]:
        found = wrist_camera.surface_points(
            result, [move["pixel_before"]], move["pose_before"], 23.5
        )
        assert (found[0] - move["picked_at"]) @ move["across"] == pytest.approx(0, abs=0.02)
    with pytest.raises(ValueError, match="12 moves"):
        wrist_camera.fit_touch(wrist_camera.load(), data["looks"], data["moves"][:5], 23.5)


def test_touch_command_stores_the_fit(tmp_path, capsys):
    script = load_script()
    out = tmp_path / "wrist.json"
    out.write_text(wrist_camera.CONFIG.read_text())
    data = tmp_path / "touch.json"
    data.write_text(json.dumps(touch_data(touch_truth())))
    script.touch(data, out)
    stored = wrist_camera.load(out)
    assert stored["tool"]["offset_xy_mm"] == pytest.approx([-0.6, 0.4], abs=0.02)
    assert stored["touch_fit"]["looks"] == 108 and "Stored" in capsys.readouterr().out
    wrong = touch_data(touch_truth())
    for number, move in enumerate(wrong["moves"]):
        shift = 1.5 * (-1) ** number
        move["picked_at"] = [a + shift * b for a, b in zip(move["picked_at"], move["across"])]
    data.write_text(json.dumps(wrong))
    before = out.read_text()
    with pytest.raises(RuntimeError, match="above the limit"):
        script.touch(data, out)
    assert out.read_text() == before


FIXED = "/dev/v4l/by-path/fake-camera-1"
WRIST = "/dev/v4l/by-id/usb-HDMI_USB_Camera_2023121018-video-index0"
POSE = [100.0, -230.0, 120.0, 0.0]


@pytest.fixture
def wrist_service(tmp_path):
    model = {**simulated_model(), "image_size": [3840, 2160]}
    # 1 marker at arm (150, -180), on a surface that the tip touches at flange z -70.
    pixel = simulated_pixel(model, np.array([150.0, -180.0, -105.0]), POSE)
    frames = SimpleNamespace(
        camera_count=2,
        configuration=SimpleNamespace(devices=(FIXED, WRIST)),
        read=Mock(return_value=Image.new("RGB", (3840, 2160))),
    )
    service = VisionService(
        demo=True,
        frames=frames,
        localizer_factory=lambda: SimpleNamespace(
            predict=lambda image: (image, {"detections": [{"center_xy": pixel.tolist()}]})
        ),
        marker_detector=lambda _image: [{"id": 18, "center_px": pixel.tolist()}],
        wrist_model=lambda: model,
        calibration_directory=tmp_path,
    )
    service.start()
    yield service
    service.close(wait_s=1)


def run_command(service, **body):
    client = create_app(service).test_client()
    headers = {"X-MG400-Client": "wrist-test"}
    assert client.post("/api/heartbeat", headers=headers, json={}).status_code == 200
    response = client.post("/api/command", headers=headers, json=body)
    if response.status_code != 202:
        return response.status_code, response.json
    deadline = time.monotonic() + 5
    while service.snapshot()["busy"] and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    receipt = service.snapshot()["last_command"]
    return receipt["result"], receipt.get("output")


def test_marker_command_gives_arm_coordinates_for_the_wrist_camera(wrist_service):
    body = {"action": "read_markers", "camera": 2}
    result, output = run_command(wrist_service, **body, flange_mm=POSE, touch_z_mm=-70.0)
    assert result == "ok" and output["camera_device"] == WRIST
    assert output["markers"][0]["id"] == 18 and output["calibrated"] is True
    assert output["markers"][0]["robot_mm"] == pytest.approx({"x": 150.0, "y": -180.0}, abs=0.01)
    result, output = run_command(wrist_service, **body)
    assert result == "ok" and "robot_mm" not in output["markers"][0]
    assert output["calibrated"] is False


def test_detect_command_gives_arm_coordinates_for_the_wrist_camera(wrist_service):
    body = {"action": "detect", "camera": 2}
    result, output = run_command(wrist_service, **body, flange_mm=POSE, touch_z_mm=-70.0)
    assert result == "ok" and output["calibrated"] is True
    assert output["detections"][0]["robot_mm"] == pytest.approx({"x": 150.0, "y": -180.0}, abs=0.01)
    result, output = run_command(wrist_service, **body)
    assert result == "ok" and "robot_mm" not in output["detections"][0]


@pytest.mark.parametrize(
    "arguments",
    [
        {"camera": 1, "flange_mm": POSE, "touch_z_mm": -70.0},  # not the wrist camera
        {"camera": 2, "flange_mm": POSE},
        {"camera": 2, "touch_z_mm": -70.0},
        {"camera": 2, "flange_mm": POSE[:3], "touch_z_mm": -70.0},
        {"camera": 2, "flange_mm": POSE, "touch_z_mm": "low"},
    ],
)
@pytest.mark.parametrize("action", ["read_markers", "detect"])
def test_commands_refuse_incomplete_arm_arguments(wrist_service, arguments, action):
    status, body = run_command(wrist_service, action=action, **arguments)
    assert status == 400 and "Arm coordinates need" in body["error"]


def test_marker_command_fails_when_the_surface_is_above_the_lens(wrist_service):
    body = {"action": "read_markers", "camera": 2, "flange_mm": POSE, "touch_z_mm": 500.0}
    result, _ = run_command(wrist_service, **body)
    assert result != "ok"


@pytest.mark.parametrize("installed", [True, False])
def test_the_wrist_camera_uses_its_own_weights_at_a_larger_input(tmp_path, installed):
    wrist_weights = tmp_path / "wrist.pt"
    if installed:
        wrist_weights.write_bytes(b"weights")
    loads, warmups = [], []

    def factory(weights, **options):
        loads.append((weights, options))

        def predict(image):
            warmups.append(image.size)
            return image, {"detections": [], "weights": str(weights)}

        return SimpleNamespace(predict=predict)

    frames = SimpleNamespace(
        camera_count=2,
        configuration=SimpleNamespace(devices=(FIXED, WRIST)),
        read=Mock(return_value=Image.new("RGB", (1280, 720))),
    )
    service = VisionService(
        frames=frames,
        weights="main.pt",
        wrist_weights=wrist_weights,
        torch_loader=lambda: SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
        localizer_factory=factory,
        calibration_directory=tmp_path,
    )
    service.start()
    try:
        wrist_load = [(wrist_weights, {"device": "cpu", "input_size": 1280})]
        assert loads == [("main.pt", {"device": "cpu"})] + wrist_load * installed
        assert warmups == [(640, 640)] + [(1280, 1280)] * installed
        _, output = run_command(service, action="detect", camera=2)
        assert output["weights"] == (str(wrist_weights) if installed else "main.pt")
        _, output = run_command(service, action="detect", camera=1)
        assert output["weights"] == "main.pt"
    finally:
        service.close(wait_s=1)


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


def test_the_mapping_uses_the_real_place_of_the_flange(monkeypatch):
    from mg400 import kinematics

    error = kinematics.ArmError(0.4, 0.9, -2.5, 0.6)
    monkeypatch.setattr(kinematics, "ERROR", error)
    model = wrist_camera.load()
    model = {**model, "arm": dict(wrist_camera.NO_ARM), "tool": {"offset_xy_mm": [0.0, 0.0]}}
    mount = model["mount"]
    rotation = np.array(mount["rotation_link_to_camera"], float)
    touch = (-20.0, -290.0, 22.5)  # the reported pose at which the cup touches the point
    point = np.array(kinematics.true_pose(touch))
    point[2] -= mount["camera_above_tip_mm"]
    looks, pixels = [(-39.0, -236.0, 163.8, -25.2), (1.0, -276.0, 125.8, -25.2)], []
    for look in looks:
        flange = np.array([kinematics.true_pose(look)[:3]])
        j1 = np.array([np.arctan2(look[1], look[0])])
        linked = wrist_camera.link_points(point[None], flange, j1, mount["offset_xy_mm"])
        camera = linked @ rotation.T
        pixels.append(
            wrist_camera.project(model["camera_matrix"], model["distortion"], np.zeros(3), camera)[
                0
            ]
        )

    place = wrist_camera.surface_points(model, [pixels[0]], looks[0], touch[2])[0]
    assert place == pytest.approx(touch[:2], abs=0.01)
    back = wrist_camera.surface_pixels(model, [touch[:2]], looks[0], touch[2])[0]
    assert back == pytest.approx(pixels[0], abs=0.05)
    x, y, touch_z, _ = wrist_camera.locate(model, pixels, looks)
    assert (x, y, touch_z) == pytest.approx(touch, abs=0.02)

    # Without the stored error the same pixel maps to another place: the arm error.
    monkeypatch.setattr(kinematics, "ERROR", kinematics.ArmError())
    plain = wrist_camera.surface_points(model, [pixels[0]], looks[0], touch[2])[0]
    assert np.linalg.norm(plain - touch[:2]) > 0.3
