import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from vision import wrist_camera, wrist_dataset

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
        "arm": dict(wrist_camera.NO_ARM),
        "tool": {"offset_xy_mm": [0.0, 0.0]},
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


def segment(image, boxes):
    """Outline of the thing in each box that differs from the plate: stands in for the model."""
    outlines = []
    for x0, y0, x1, y1 in boxes:
        left, top = max(int(x0) - 8, 0), max(int(y0) - 8, 0)
        crop = image[top : int(y1) + 8, left : int(x1) + 8].astype(np.int32)
        plate = np.median(crop.reshape(-1, 3), axis=0)
        differs = (np.abs(crop - plate).max(axis=2) > 30).astype(np.uint8)
        contours = cv2.findContours(differs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
        outlines.append(max(contours, key=cv2.contourArea).reshape(-1, 2) + [left, top])
    return outlines


# A chip with a notch: its 2 ends differ.
NOTCHED = wrist_dataset.centred(
    np.array([(-6, -2.2), (6, -2.2), (6, 0.4), (1.5, 0.4), (1.5, 2.2), (-6, 2.2)], float)
)[0]


def test_a_stored_outline_is_fitted_with_a_shift_and_a_turn():
    for angle, place in ((33.0, (-40.0, -250.0)), (200.0, (10.3, -300.7))):
        polygon = wrist_dataset.turned(NOTCHED, angle) + place
        x, y, found, share = wrist_dataset.fit(NOTCHED, polygon)
        assert (x, y) == pytest.approx(place, abs=0.1) and share > 0.95
        assert (found - angle + 180) % 360 - 180 == pytest.approx(0, abs=1.0)
        x, y, found, share = wrist_dataset.fit(NOTCHED, polygon, near=angle + 5)
        assert (x, y) == pytest.approx(place, abs=0.1) and found == pytest.approx(angle, abs=1.0)
    moved = NOTCHED + (0.7, -0.4)
    x, y, found, share = wrist_dataset.fit(NOTCHED, moved, near=0.0, turn=False)
    assert (x, y, found) == pytest.approx((0.7, -0.4, 0.0), abs=0.1) and share > 0.95


def test_the_outline_of_a_chip_comes_from_views_that_do_not_agree_on_its_place():
    views = []
    for number in range(6):
        direction = np.radians(60 * number)
        side = 0.7 * np.array([np.cos(direction), np.sin(direction)])  # the side face in this view
        hull = cv2.convexHull(np.vstack([NOTCHED, NOTCHED + side]).astype(np.float32))
        seen = hull.reshape(-1, 2) if number % 2 else NOTCHED
        views.append(
            seen + (-80.0, -216.0) + 0.8 * np.array([np.sin(direction), np.cos(direction)])
        )
    shape = wrist_dataset.chip_shape(views)
    area = cv2.contourArea(shape.astype(np.float32))
    assert area == pytest.approx(cv2.contourArea(NOTCHED.astype(np.float32)), rel=0.05)
    assert wrist_dataset.fit(shape, NOTCHED, near=0.0)[3] > 0.93


def write_layout(folder, model, chips, speck_in=None):
    folder.mkdir()
    entries = []
    for number, pose in enumerate([*POSES, POSES[0]]):
        image = render(model, pose, chips, speck=(-40.0, -260.0) if number == speck_in else None)
        cv2.imwrite(str(folder / f"f{number}.png"), image)
        entries.append({"image": f"f{number}.png", "pose": list(pose)})
    (folder / "frames.json").write_text(json.dumps(entries))


def label_masks(model, path, pose):
    """The labels of 1 frame as masks."""
    masks = []
    for line in path.read_text().splitlines():
        points = np.array(line.split()[1:], float).reshape(-1, 2) * SIZE
        mask = np.zeros((SIZE[1], SIZE[0]), np.uint8)
        cv2.fillPoly(mask, [np.round(points).astype(np.int32)], 1)
        masks.append(mask)
    return masks


def test_layouts_are_labeled_with_the_stored_outlines_and_problem_frames_are_dropped(tmp_path):
    path = ROOT / "scripts/wrist_chip_dataset.py"
    spec = importlib.util.spec_from_file_location("wrist_chip_dataset_test", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    model, out, shapes = camera(), tmp_path / "out", tmp_path / "shapes.json"
    # After a shuffle the same 2 chips lie at other places with other angles.
    moved = [
        (-30.0, -300.0, 5.0, 12.0, 100.0, CHIPS[0][5]),
        (-70.0, -240.0, 4.0, 15.0, 20.0, CHIPS[1][5]),
    ]
    write_layout(tmp_path / "first", model, CHIPS, speck_in=3)
    write_layout(tmp_path / "second", model, moved)
    counts = script.build(out, [tmp_path / "first"], shapes, segment, model=model)
    assert counts["frames"] == 4 and counts["chips"] == 2 and counts["kept"] == 3
    assert counts["labels"] == 6 and counts["problems"] == {"object that is not a chip": 1}
    stored = json.loads(shapes.read_text())["shapes_mm"]
    sizes = sorted(sorted(cv2.minAreaRect(np.float32(shape))[1]) for shape in stored)
    assert sizes[0] == pytest.approx([4, 15], abs=0.7) and sizes[1] == pytest.approx(
        [5, 12], abs=0.7
    )
    assert not (out / "images/first_f3.png").exists()

    counts = script.build(out, [tmp_path / "second"], shapes, segment, model=model)
    assert counts["kept"] == 4 and counts["labels"] == 8 and not counts["problems"]
    assert json.loads(shapes.read_text())["shapes_mm"] == stored  # the outlines stay as they are
    places = sorted(counts["layouts"]["second"].values())
    assert places[0][:2] == pytest.approx([-70.0, -240.0], abs=0.6)
    assert places[1][:2] == pytest.approx([-30.0, -300.0], abs=0.6)
    for number, pose in enumerate([*POSES, POSES[0]]):
        truth = []
        for x, y, width, length, angle, _ in moved:
            mask = np.zeros((SIZE[1], SIZE[0]), np.uint8)
            polygon(mask, model, pose, cv2.boxPoints(((x, y), (width, length), angle)), TOP, 1)
            truth.append(mask)
        for label in label_masks(model, out / f"labels/second_f{number}.txt", pose):
            share = max((label & mask).sum() / (label | mask).sum() for mask in truth)
            assert share > 0.8

    # A chip that is gone from 1 frame: that frame is not written.
    gone = cv2.imread(str(tmp_path / "second/f1.png"))
    cv2.imwrite(str(tmp_path / "second/f1.png"), render(model, POSES[1], moved[:1]))
    counts = script.build(tmp_path / "again", [tmp_path / "second"], shapes, segment, model=model)
    assert gone is not None and counts["kept"] == 3
    assert counts["problems"] == {"chip without an object": 1}
