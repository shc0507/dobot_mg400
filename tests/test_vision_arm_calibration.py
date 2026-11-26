import importlib.util
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from mg400 import kinematics
from mg400.kinematics import ArmError
from vision import arm_calibration, wrist_camera

ROOT = Path(__file__).resolve().parents[1]
TABLE_TOUCH_Z = -189.0
REAL = ArmError(j2_zero_deg=0.4, j3_zero_deg=0.9, rear_arm_mm=-1.2, forearm_mm=0.5)


@pytest.fixture(autouse=True)
def arm_without_error(monkeypatch):
    """These tests simulate an arm that moves as it reports."""
    monkeypatch.setattr(kinematics, "ERROR", kinematics.ArmError())


def load_script():
    path = ROOT / "scripts/arm_calibrate.py"
    spec = importlib.util.spec_from_file_location("arm_calibrate_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulated_views(real, noise_px=0.3, poses=None, false_blobs=2):
    """Views of a table hole grid from the wrist camera of an arm with the error `real`."""
    rng = np.random.default_rng(5)
    model = wrist_camera.load()
    matrix, distortion = model["camera_matrix"], model["distortion"]
    rotation = np.array(model["mount"]["rotation_link_to_camera"], float)
    offset = np.add(model["mount"]["offset_xy_mm"], (0.8, -0.5))
    yaw, origin = math.radians(0.7), np.array([-150.0, 230.0])
    plane = TABLE_TOUCH_Z + 0.6 - model["mount"]["camera_above_tip_mm"]
    steps = np.arange(-9, 10)
    across, along = (PITCH * v.ravel() for v in np.meshgrid(steps, steps))
    holes = np.column_stack(
        [
            origin[0] + math.cos(yaw) * across - math.sin(yaw) * along,
            origin[1] + math.sin(yaw) * across + math.cos(yaw) * along,
            np.full(len(across), plane) + 0.0004 * across,
        ]
    )
    views = []
    for pose in poses or plan_poses():
        flange = np.array(kinematics.true_pose(pose, real)[:3])
        j1 = math.atan2(pose[1], pose[0])
        linked = wrist_camera.link_points(holes, flange, np.full(len(holes), j1), offset)
        camera = linked @ rotation.T
        seen = (camera[:, 2] > 0) & (np.hypot(camera[:, 0], camera[:, 1]) < 0.9 * camera[:, 2])
        pixels = wrist_camera.project(matrix, distortion, np.zeros(3), camera[seen])
        inside = (pixels > 40).all(1) & (pixels[:, 0] < 3800) & (pixels[:, 1] < 2120)
        pixels = pixels[inside] + rng.normal(0, noise_px, (int(inside.sum()), 2))
        blobs = rng.uniform((1200, 600), (2600, 1500), (false_blobs, 2))
        views.append({"pose": list(pose), "holes": np.vstack([pixels, blobs]).tolist()})
    return model, views


PITCH = arm_calibration.PITCH_MM


def plan_poses():
    poses = []
    for j1 in (105.0, 115.0, 125.0):
        for reach in (235, 265, 295, 325, 350):
            for z in (150, 110, 70, 30, -10, -50):
                try:
                    kinematics.joints_from_plane(reach, z)
                except ValueError:
                    continue
                angle = math.radians(j1)
                poses.append((reach * math.cos(angle), reach * math.sin(angle), float(z), j1 + 74))
    return poses


def test_holes_are_found_at_their_centres():
    scale = 4
    image = np.full((600 * scale, 800 * scale), 205, np.uint8)
    centres = [(120.3, 110.6), (300.75, 140.2), (520.1, 300.9), (200.5, 420.25), (650.4, 480.0)]
    for u, v in centres:
        cv2.circle(
            image,
            (round(u * scale * 16), round(v * scale * 16)),
            13 * scale * 16,
            25,
            -1,
            cv2.LINE_AA,
            4,
        )
    cv2.rectangle(image, (400 * scale, 60 * scale), (470 * scale, 78 * scale), 25, -1)  # a chip
    cv2.circle(image, (700 * scale, 100 * scale), 3 * scale, 25, -1)  # too small
    image = cv2.resize(image, (800, 600), interpolation=cv2.INTER_AREA)

    found = arm_calibration.find_holes(image)

    assert len(found) == len(centres)
    for centre in centres:
        assert np.min(np.linalg.norm(found - (np.array(centre) - 0.5 + 0.5 / scale), axis=1)) < 0.2


def test_fit_recovers_the_arm_error_from_views_of_the_hole_grid():
    model, views = simulated_views(REAL)

    result = arm_calibration.fit(model, views, TABLE_TOUCH_Z)

    assert result["views"] == len(views)
    for name, tolerance in (
        ("j2_zero_deg", 0.05),
        ("j3_zero_deg", 0.05),
        ("rear_arm_mm", 0.15),
        ("forearm_mm", 0.15),
    ):
        assert getattr(result["error"], name) == pytest.approx(getattr(REAL, name), abs=tolerance)
        assert result["sigma"][name] < tolerance
    assert result["grid_yaw_deg"] == pytest.approx(0.7, abs=0.02)
    assert result["after"]["rms_px"] < 0.5 < result["before"]["rms_px"]
    before, after = (result[key]["view_shift_rms_mm"] for key in ("before", "after"))
    assert before["reach"] > 0.15 and before["height"] > 0.3
    assert max(after.values()) < 0.1
    assert before["sideways"] < 0.1


def test_fit_gives_no_error_for_an_arm_without_error():
    model, views = simulated_views(ArmError())

    result = arm_calibration.fit(model, views, TABLE_TOUCH_Z)

    for name in arm_calibration.TERMS:
        assert abs(getattr(result["error"], name)) < 3 * result["sigma"][name] + 0.01
    assert result["before"]["rms_px"] == pytest.approx(result["after"]["rms_px"], abs=0.02)


def test_a_narrow_run_shows_a_large_sigma():
    # 1 height and a short reach range: the joint zeros and the lengths are not separate.
    angle = math.radians(115.0)
    poses = [
        (reach * math.cos(angle + side), reach * math.sin(angle + side), 150.0, 189.0)
        for reach in (250, 260, 270, 280)
        for side in (-0.1, 0.0, 0.1)
    ]
    model, views = simulated_views(REAL, poses=poses)

    result = arm_calibration.fit(model, views, TABLE_TOUCH_Z)

    assert result["sigma"]["forearm_mm"] > 0.5
    assert result["sigma"]["j3_zero_deg"] > 0.1


def test_fit_refuses_too_few_views():
    model, views = simulated_views(REAL)
    with pytest.raises(ValueError, match="12 views"):
        arm_calibration.fit(model, views[:5], TABLE_TOUCH_Z)
    with pytest.raises(ValueError, match="no holes"):
        arm_calibration.fit(model, [{"pose": views[0]["pose"], "holes": []}], TABLE_TOUCH_Z)


def test_view_poses_are_inside_the_joint_ranges_and_cover_j2_and_j3():
    script = load_script()

    poses = script.view_poses(74.0)

    joints = [kinematics.joints_from_plane(math.hypot(x, y), z) for x, y, z, _ in poses]
    assert len(poses) >= 80
    assert max(j2 for j2, _ in joints) - min(j2 for j2, _ in joints) > 50
    assert max(j3 for _, j3 in joints) - min(j3 for _, j3 in joints) > 70
    assert min(z for _, _, z, _ in poses) == -50.0
    steps = [math.dist(a[:3], b[:3]) for a, b in zip(poses, poses[1:])]
    assert max(steps) < 75


def test_the_arc_goes_from_the_start_to_the_first_view_at_the_start_height():
    script = load_script()

    arc = script.arc_poses(-99.3, 74.0)

    assert all(z == script.START[2] for _, _, z, _ in arc)
    assert all(math.hypot(x, y) == pytest.approx(250.0) for x, y, _, _ in arc)
    assert math.degrees(math.atan2(arc[-1][1], arc[-1][0])) == pytest.approx(105.0)
    assert arc[-1][3] == pytest.approx(179.0)


def test_run_refuses_a_start_that_was_not_checked():
    script = load_script()
    script.check_start((-38.6, -236.1, 163.76, -25.3))
    with pytest.raises(RuntimeError, match="Park the arm"):
        script.check_start((300.0, 0.0, 100.0, 0.0))


def test_fit_command_prints_the_error_and_the_bands(tmp_path, capsys):
    script = load_script()
    _, views = simulated_views(REAL)
    path = tmp_path / "frames.json"
    path.write_text(__import__("json").dumps(views))

    result = script.fit(path, arm_calibration.TERMS)

    assert result["error"]["j3_zero_deg"] == pytest.approx(0.9, abs=0.05)
    assert any(row["band"].startswith("reach") for row in result["after"]["bands"])
    assert "ERROR = ArmError(j2_zero_deg=0.4" in capsys.readouterr().out
