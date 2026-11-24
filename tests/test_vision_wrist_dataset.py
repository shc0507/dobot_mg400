import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from vision import wrist_dataset

ROOT = Path(__file__).resolve().parents[1]
SIZE = (1920, 1080)
ORIGIN, FIRST, SECOND = np.array([40.0, -186.0]), np.array([0.0, -1.0]), np.array([-1.0, 0.0])
TRAY = (ORIGIN, FIRST, SECOND)
CHIPS = [
    (-80.0, -216.0, 5.0, 12.0, 30.0, (200, 120, 90)),
    (-15.0, -320.0, 4.0, 15.0, -70.0, (30, 30, 30)),
]
POSES = [(-39.0, -236.0, 163.8, 0.0), (1.0, -276.0, 163.8, 0.0), (-39.0, -316.0, 163.8, 0.0)]
TOP = wrist_dataset.PLATE_TOUCH_Z + wrist_dataset.CHIP_THICKNESS_MM


def camera():
    """A camera with half the pixels of the wrist camera: 3.5 pixels per mm at the tray."""
    return {
        "camera_matrix": np.array([[620.0, 0, 950], [0, 620.0, 560], [0, 0, 1]]),
        "distortion": np.zeros(8),
        "mount": {
            "offset_xy_mm": [-45.8, 43.6],
            "rotation_link_to_camera": [[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]],
            "camera_above_tip_mm": 31.5,
        },
    }


def polygon(image, model, pose, arm_points, touch_z, color):
    pixels = wrist_dataset.to_pixels(model, pose, arm_points, touch_z)
    cv2.fillPoly(image, [np.round(pixels).astype(np.int32)], color, lineType=cv2.LINE_AA)


def render(model, pose, chips=CHIPS, speck=None):
    """A frame of the tray: shaded grey plate, 3 marker tiles, chips, an optional speck."""
    shade = np.linspace(150, 175, SIZE[0], dtype=np.float32)[None, :].repeat(SIZE[1], 0)
    image = np.dstack([shade + 4, shade, shade - 3]).astype(np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker, centre in ((0, ORIGIN), (1, ORIGIN + 149.4 * FIRST), (2, ORIGIN + 149.4 * SECOND)):
        tile = cv2.aruco.generateImageMarker(dictionary, marker, 120)
        arm = [
            centre + a * FIRST + b * SECOND for a, b in ((-14, -14), (14, -14), (14, 14), (-14, 14))
        ]
        target = wrist_dataset.to_pixels(model, pose, arm, wrist_dataset.PLATE_TOUCH_Z)
        source = np.float32([[0, 0], [119, 0], [119, 119], [0, 119]])
        warp = cv2.getPerspectiveTransform(source, target.astype(np.float32))
        white = cv2.warpPerspective(np.full_like(tile, 255), warp, SIZE)
        drawn = cv2.warpPerspective(tile, warp, SIZE)
        image[white > 0] = np.dstack([drawn] * 3)[white > 0]
    for x, y, width, length, angle, color in chips:
        box = cv2.boxPoints(((x, y), (width, length), angle))
        polygon(image, model, pose, box, TOP, color)
    if speck is not None:
        box = cv2.boxPoints((speck, (5.0, 5.0), 0.0))
        polygon(image, model, pose, box, TOP, (60, 60, 60))
    return image


def test_objects_on_a_shaded_tray_are_found_and_the_border_is_ignored():
    shade = np.linspace(140, 180, 800, dtype=np.float32)[None, :].repeat(600, 0)
    image = np.dstack([shade, shade, shade]).astype(np.uint8)
    mask = np.zeros((600, 800), np.uint8)
    mask[50:550, 50:750] = 1
    for centre, size, angle, color in (
        ((300, 200), (35, 84), 25, (200, 120, 90)),
        ((560, 380), (28, 105), -60, (25, 25, 25)),
        ((60, 300), (40, 60), 0, (20, 20, 20)),  # half outside the mask
    ):
        box = np.round(cv2.boxPoints((centre, size, angle))).astype(np.int32)
        cv2.fillPoly(image, [box], color)
    found = wrist_dataset.find_objects(image, mask, 7.0)
    centres = sorted(tuple(np.round(outline.mean(0))) for outline in found)
    assert len(found) == 2
    assert centres[0] == pytest.approx((300, 200), abs=3)
    assert centres[1] == pytest.approx((560, 380), abs=3)
    sizes = sorted(sorted(cv2.minAreaRect(outline.astype(np.float32))[1]) for outline in found)
    assert sizes[0] == pytest.approx([28, 105], abs=4) and sizes[1] == pytest.approx(
        [35, 84], abs=4
    )


def test_tray_is_located_from_its_markers_and_masked_without_the_tiles():
    model = camera()
    seen = []
    for pose in POSES:
        gray = cv2.cvtColor(render(model, pose), cv2.COLOR_BGR2GRAY)
        seen.append((wrist_dataset.tray_markers(gray), pose))
    # A marker can be outside the picture in a pose. Each must be in 2 poses or more.
    assert all(sum(marker in found for found, _ in seen) >= 2 for marker in (0, 1, 2))
    origin, first, second = wrist_dataset.locate_tray(model, seen)
    assert origin == pytest.approx(ORIGIN, abs=0.5)
    assert first == pytest.approx(FIRST, abs=0.01) and second == pytest.approx(SECOND, abs=0.01)
    mask = wrist_dataset.tray_mask(model, POSES[0], TRAY, (SIZE[1], SIZE[0]))
    centre, tile, outside = wrist_dataset.to_pixels(
        model, POSES[0], [ORIGIN + 75 * FIRST + 75 * SECOND, ORIGIN, ORIGIN - 40 * FIRST], 22.0
    ).astype(int)
    assert mask[centre[1], centre[0]] == 1 and mask[tile[1], tile[0]] == 0
    assert mask[outside[1], outside[0]] == 0
    with pytest.raises(ValueError, match="fewer than 2 frames"):
        wrist_dataset.locate_tray(model, seen[:1])


def test_chips_are_registered_and_labeled_and_problems_are_reported():
    model = camera()
    frames = [(render(model, pose), pose) for pose in POSES]
    chips = wrist_dataset.register_chips(model, TRAY, frames)
    assert len(chips) == 2
    found = sorted(tuple(chip) for chip in chips)
    assert found[0] == pytest.approx((-80.0, -216.0), abs=0.5)
    assert found[1] == pytest.approx((-15.0, -320.0), abs=0.5)
    lines, problems = wrist_dataset.label_frame(model, TRAY, chips, *frames[0])
    assert len(lines) == 2 and not problems
    values = np.array(lines[0].split()[1:], float).reshape(4, 2)
    assert lines[0].startswith("0 ") and np.all((values >= 0) & (values <= 1))
    speck = render(model, POSES[0], speck=(-40.0, -260.0))
    assert wrist_dataset.label_frame(model, TRAY, chips, speck, POSES[0])[1] == {
        "object that is not a chip": 1
    }
    gone = render(model, POSES[0], chips=CHIPS[:1])
    lines, problems = wrist_dataset.label_frame(model, TRAY, chips, gone, POSES[0])
    assert len(lines) == 1 and problems == {"chip without an object": 1}


def test_dataset_script_writes_images_and_labels_and_drops_problem_frames(tmp_path):
    path = ROOT / "scripts/wrist_chip_dataset.py"
    spec = importlib.util.spec_from_file_location("wrist_chip_dataset_test", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    model, frames_dir, out = camera(), tmp_path / "frames", tmp_path / "out"
    frames_dir.mkdir()
    entries = []
    for number, pose in enumerate([*POSES, POSES[0]]):
        image = render(model, pose, speck=(-40.0, -260.0) if number == 3 else None)
        cv2.imwrite(str(frames_dir / f"f{number}.png"), image)
        entries.append({"image": f"f{number}.png", "pose": list(pose)})
    (frames_dir / "frames.json").write_text(json.dumps(entries))
    counts = script.build(frames_dir, out, model=model)
    assert counts["frames"] == 4 and counts["chips"] == 2 and counts["kept"] == 3
    assert counts["labels"] == 6 and counts["problems"] == {"object that is not a chip": 1}
    assert sorted(p.name for p in (out / "labels").iterdir()) == ["f0.txt", "f1.txt", "f2.txt"]
    assert len((out / "labels/f0.txt").read_text().splitlines()) == 2
    assert (out / "images/f1.png").exists() and not (out / "images/f3.png").exists()
