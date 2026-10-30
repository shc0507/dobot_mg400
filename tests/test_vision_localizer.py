"""Geometry and IO checks only. These tests do not establish model accuracy."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

from vision.localizer import Detection, Localizer, draw_detections, load_image, map_detections


class GeometryTests(unittest.TestCase):
    def test_stretched_boxes_return_to_wide_original(self):
        found = map_detections([[320, 160, 480, 320]], [0], [0.9], {0: "Chip"}, 3840, 2160, 640)
        self.assertEqual(found[0].xyxy, (1920, 540, 2880, 1080))
        self.assertEqual(found[0].center_xy, (2400, 810))

    def test_invalid_and_low_confidence_boxes_are_excluded(self):
        found = map_detections(
            [[20, 30, 10, 40], [0, 0, 1, 1], [0, 0, 10, 10]],
            [0, 0, 0],
            [0.9, 0.1, float("nan")],
            {0: "Chip"},
            640,
            640,
            640,
        )
        self.assertEqual(found, [])

    def test_exif_rotation_and_original_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (100, 50)).save(p, exif=exif)
            before = p.read_bytes()
            self.assertEqual(load_image(p).size, (50, 100))
            self.assertEqual(p.read_bytes(), before)

    def test_opencv_input_is_bgr(self):
        frame = np.zeros((20, 30, 3), dtype=np.uint8)
        frame[:, :, 2] = 255
        self.assertEqual(load_image(frame).getpixel((0, 0)), (255, 0, 0))

    def test_overlay_keeps_size_and_input_unmodified(self):
        image = Image.new("RGB", (640, 360), "black")
        result = draw_detections(image, [Detection(0, "Chip", 0.9, (50, 50, 100, 100), (75, 75))])
        self.assertEqual(result.size, image.size)
        self.assertEqual(image.getpixel((50, 50)), (0, 0, 0))
        self.assertNotEqual(result.getpixel((50, 50)), (0, 0, 0))


def test_masks_restore_polygon_and_angle_after_anisotropic_stretch():
    original = cv2.boxPoints(((600, 350), (200, 80), 30))
    stretched = original / [2, 1]
    before = stretched.copy()
    found = map_detections(
        [[200, 200, 400, 500]],
        [0],
        [0.9],
        {0: "Chip"},
        1280,
        640,
        640,
        polygons=[stretched],
    )
    item = found[0]
    np.testing.assert_allclose(item.polygon, original, atol=0.0001)
    assert item.xyxy == (400, 200, 800, 500)
    assert item.center_xy == (600, 350)
    # OpenCV versions can swap rectangle axes and offset the angle by 90 degrees.
    assert item.angle_deg % 90 == pytest.approx(30, abs=0.0001)
    assert sorted(item.size_px) == pytest.approx([80, 200], abs=0.0001)
    np.testing.assert_array_equal(stretched, before)


def test_masks_keep_alignment_when_earlier_boxes_are_filtered():
    first = [[0, 0], [20, 0], [20, 20]]
    second = [[100, 100], [200, 100], [200, 200], [100, 200]]
    found = map_detections(
        [[0, 0, 20, 20], [100, 100, 200, 200]],
        [0, 1],
        [0.1, 0.9],
        {0: "Chip", 1: "Robotic Arm"},
        1280,
        640,
        640,
        polygons=[first, second],
    )
    assert len(found) == 1
    assert found[0].label == "Robotic Arm"
    assert found[0].polygon == ((200, 100), (400, 100), (400, 200), (200, 200))


def test_polygons_clip_to_the_original_image_bounds():
    found = map_detections(
        [[0, 0, 640, 640]],
        [0],
        [0.9],
        {0: "Chip"},
        1280,
        320,
        640,
        polygons=[[[-10, -10], [650, -10], [650, 650], [-10, 650]]],
    )
    assert found[0].polygon == ((0, 0), (1280, 0), (1280, 320), (0, 320))


@pytest.mark.parametrize("polygon", [[], [[1, 2], [3, 4]], [[0, 0], [1, 1], [float("nan"), 2]]])
def test_invalid_mask_geometry_keeps_the_valid_box(polygon):
    found = map_detections(
        [[0, 0, 50, 50]],
        [0],
        [0.9],
        {0: "Chip"},
        640,
        640,
        640,
        polygons=[polygon],
    )
    assert len(found) == 1
    assert found[0].xyxy == (0, 0, 50, 50)
    assert (found[0].polygon, found[0].angle_deg, found[0].size_px) == (None, None, None)


def test_missing_masks_leave_box_only_geometry():
    found = map_detections([[0, 0, 50, 50]], [0], [0.9], {0: "Chip"}, 640, 640, 640)
    assert (found[0].polygon, found[0].angle_deg, found[0].size_px) == (None, None, None)


def test_mismatched_masks_cannot_silently_attach_to_another_box():
    with pytest.raises(ValueError, match="Each box must have one corresponding mask polygon"):
        map_detections([[0, 0, 50, 50]], [0], [0.9], {0: "Chip"}, 640, 640, 640, polygons=[])


@pytest.fixture
def fake_detector(monkeypatch, tmp_path):
    calls = []
    model = SimpleNamespace()

    def load(weights, **options):
        calls.append((weights, options))
        return model

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=load))
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"mock weights, never parsed")
    return weights, calls, model


def test_explicit_weights_and_cache_settings_override_other_environment(fake_detector, monkeypatch):
    weights, calls, model = fake_detector
    monkeypatch.setenv("MG400_VISION_WEIGHTS", "/missing/other.pt")
    monkeypatch.setenv("MG400_VISION_CACHE_DIR", "/configured/vision-cache")
    monkeypatch.setenv("YOLO_CONFIG_DIR", "/old/cache")
    detector = Localizer(weights)
    assert detector.model is model
    assert calls == [(str(weights), {"task": "segment"})]
    assert os.environ["YOLO_CONFIG_DIR"] == "/configured/vision-cache"


def test_environment_weights_and_default_cache(fake_detector, monkeypatch):
    weights, calls, _ = fake_detector
    monkeypatch.setenv("MG400_VISION_WEIGHTS", str(weights))
    monkeypatch.delenv("MG400_VISION_CACHE_DIR", raising=False)
    monkeypatch.setenv("YOLO_CONFIG_DIR", "/old/cache")
    Localizer()
    assert calls == [(str(weights), {"task": "segment"})]
    assert os.environ["YOLO_CONFIG_DIR"] == "/var/lib/mg400/vision/yolo"


def test_weights_errors_happen_before_loading_the_model(fake_detector, monkeypatch, tmp_path):
    _, calls, _ = fake_detector
    monkeypatch.delenv("MG400_VISION_WEIGHTS", raising=False)
    with pytest.raises(FileNotFoundError, match="MG400_VISION_WEIGHTS"):
        Localizer()
    with pytest.raises(FileNotFoundError, match="Model weights file does not exist"):
        Localizer(tmp_path / "missing.pt")
    unsupported = tmp_path / "weights.txt"
    unsupported.write_text("unsupported model")
    with pytest.raises(ValueError, match="trained .pt or .onnx"):
        Localizer(unsupported)
    assert calls == []


def tensor(values):
    data = np.asarray(values, dtype=np.float32)
    return SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: data))


def test_predict_serializes_masks_in_original_image_coordinates(fake_detector, monkeypatch):
    weights, _, model = fake_detector
    monkeypatch.setenv("YOLO_CONFIG_DIR", "/old/cache")
    captured = {}
    prediction = SimpleNamespace(
        names={0: "Chip", 1: "Robotic Arm"},
        boxes=SimpleNamespace(
            xyxy=tensor([[100, 100, 200, 200]]), cls=tensor([0]), conf=tensor([0.9])
        ),
        masks=SimpleNamespace(xy=[np.array([[100, 100], [200, 100], [200, 200], [100, 200]])]),
        speed={"inference": 1.0},
    )

    def predict(**options):
        captured.update(options)
        return [prediction]

    model.predict = predict
    image = Image.new("RGB", (1280, 640), (255, 0, 0))
    annotated, metadata = Localizer(weights).predict(image)
    assert captured["source"].shape == (640, 640, 3)
    assert captured["source"][0, 0].tolist() == [0, 0, 255]
    assert captured["rect"] is False and captured["save"] is False
    assert annotated.size == image.size
    assert image.getpixel((200, 100)) == (255, 0, 0)
    item = json.loads(json.dumps(metadata))["detections"][0]
    assert item["xyxy"] == [200, 100, 400, 200]
    assert item["polygon"] == [[200, 100], [400, 100], [400, 200], [200, 200]]
    assert sorted(item["size_px"]) == pytest.approx([100, 200])
    assert metadata["counts"] == {"Chip": 1}

    prediction.masks = None
    _, metadata = Localizer(weights).predict(image)
    assert metadata["detections"][0]["polygon"] is None


def test_import_needs_no_detector_runtime_and_creates_no_files(tmp_path):
    env = os.environ.copy()
    env["MG400_VISION_CACHE_DIR"] = str(tmp_path / "cache")
    env["YOLO_CONFIG_DIR"] = str(tmp_path / "existing-setting")
    script = """
import builtins
import os
from pathlib import Path
original_import = builtins.__import__
def reject_runtime(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'ultralytics'}:
        raise AssertionError(f'Unexpected inference-runtime import: {name}')
    return original_import(name, *args, **kwargs)
builtins.__import__ = reject_runtime
import vision
import vision.localizer
assert os.environ['YOLO_CONFIG_DIR'].endswith('existing-setting')
assert not Path(os.environ['MG400_VISION_CACHE_DIR']).exists()
assert not list(Path.cwd().iterdir())
"""
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True)
