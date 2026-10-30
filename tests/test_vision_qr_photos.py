"""Integration checks for camera-like scenes, including Micro QR laboratory labels."""

import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import zxingcpp

from vision.qr import DetectionCancelled, detect_qr_codes, read_image


def cluttered_background(width=1600, height=1000):
    """Deterministic screws, wires, markings, and texture without QR finders."""
    rng = np.random.default_rng(179)
    image = rng.normal(160, 8, (height, width)).clip(0, 255).astype(np.uint8)
    for _ in range(70):
        center = tuple(int(value) for value in rng.integers([0, 0], [width, height]))
        cv2.circle(image, center, int(rng.integers(4, 18)), int(rng.integers(20, 110)), -1)
    for _ in range(20):
        points = rng.integers([0, 0], [width, height], size=(4, 2)).astype(np.int32)
        cv2.polylines(image, [points], False, int(rng.integers(25, 90)), int(rng.integers(2, 7)))
    for index, y in enumerate(range(80, height, 170)):
        cv2.putText(
            image,
            f"LASER CONTROLLER / CHANNEL {index}",
            (20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            220,
            2,
            cv2.LINE_AA,
        )
    return image


def place_code(scene, payload, quadrilateral, *, micro=True, inverted=False, scale=8):
    """Project a generated symbol and return its true, quiet-zone-free corners."""
    format_ = zxingcpp.BarcodeFormat.MicroQRCode if micro else zxingcpp.BarcodeFormat.QRCode
    barcode = zxingcpp.create_barcode(payload, format_)
    modules = np.asarray(barcode.to_image(scale=1, add_quiet_zones=False))
    tile = cv2.copyMakeBorder(modules, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    tile = cv2.resize(tile, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    if inverted:
        tile = 255 - tile
    height, width = tile.shape
    source = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    transform = cv2.getPerspectiveTransform(source, np.float32(quadrilateral))
    size = (scene.shape[1], scene.shape[0])
    warped = cv2.warpPerspective(tile, transform, size, borderValue=255)
    coverage = cv2.warpPerspective(np.full_like(tile, 255), transform, size)
    scene[coverage > 254] = warped[coverage > 254]
    border = 4 * scale
    corners = np.float32(
        [
            [border, border],
            [width - border - 1, border],
            [width - border - 1, height - border - 1],
            [border, height - border - 1],
        ]
    )
    return cv2.perspectiveTransform(corners.reshape(1, 4, 2), transform)[0]


def camera_degradation(scene):
    """Uneven light, mild optical blur, sensor noise, then JPEG compression."""
    height, width = scene.shape
    illumination = np.linspace(0.55, 1.0, width)[None, :]
    illumination = illumination * np.linspace(0.82, 1.0, height)[:, None]
    image = scene.astype(np.float32) * illumination
    image = cv2.GaussianBlur(image, (3, 3), 0.55)
    image += np.random.default_rng(812).normal(0, 2.5, scene.shape)
    image = image.clip(0, 255).astype(np.uint8)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    assert ok
    return cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)


def make_camera_scene():
    image = cluttered_background()
    expected = []
    placements = [
        ("Stamp", True, [[105, 155], [285, 135], [300, 325], [120, 340]]),
        ("Stamp", True, [[1070, 570], [1250, 615], [1225, 800], [1040, 765]]),
        ("LAB-QR-07", False, [[800, 130], [1060, 165], [1045, 420], [765, 385]]),
    ]
    for payload, micro, quadrilateral in placements:
        corners = place_code(image, payload, quadrilateral, micro=micro)
        expected.append((payload, corners))
    return camera_degradation(image), expected


class PhotoDetectionTests(unittest.TestCase):
    def assert_matches(self, codes, expected, tolerance=6):
        self.assertEqual(
            Counter(code.text for code in codes), Counter(text for text, _ in expected)
        )
        unmatched = list(codes)
        for payload, corners in expected:
            candidates = [code for code in unmatched if code.text == payload]
            center = corners.mean(axis=0)
            code = min(
                candidates, key=lambda item: np.linalg.norm(np.asarray(item.center) - center)
            )
            unmatched.remove(code)
            for point in code.corners:
                self.assertLess(np.linalg.norm(corners - point, axis=1).min(), tolerance)
            np.testing.assert_allclose(code.center, center, atol=tolerance)

    def test_clutter_perspective_shadow_noise_and_repeated_payload(self):
        image, expected = make_camera_scene()
        self.assert_matches(detect_qr_codes(image), expected)

    def test_inverted_micro_qr(self):
        image = cluttered_background(900, 600)
        corners = place_code(
            image, "Stamp", [[340, 180], [520, 160], [540, 350], [350, 370]], inverted=True
        )
        self.assert_matches(detect_qr_codes(camera_degradation(image)), [("Stamp", corners)])

    def test_small_micro_qr_in_large_frame_preserves_coordinates(self):
        image = cluttered_background(2400, 1800)
        corners = place_code(image, "2D", [[2010, 1390], [2110, 1395], [2105, 1500], [2005, 1495]])
        self.assert_matches(detect_qr_codes(image), [("2D", corners)])

    def test_tile_only_detection_maps_back_to_original_image(self):
        image = cluttered_background(2400, 1800)
        corners = place_code(image, "2D", [[2010, 1390], [2110, 1395], [2105, 1500], [2005, 1495]])
        actual_reader = zxingcpp.read_barcodes
        calls = []

        def read_tiles_only(view, **kwargs):
            calls.append(view.shape)
            # Force original, contrast overview, and sharpened overview to miss.
            # Subsequent calls use the real detector on actual cropped tiles.
            if len(calls) <= 3:
                return []
            return actual_reader(view, **kwargs)

        with patch("vision.qr.zxingcpp.read_barcodes", side_effect=read_tiles_only):
            codes = detect_qr_codes(image)
        self.assertGreater(len(calls), 3)
        self.assertTrue(all(shape[0] <= 1024 and shape[1] <= 1024 for shape in calls[3:]))
        self.assert_matches(codes, [("2D", corners)])
        self.assertIn("tile", codes[0].source.lower())
        self.assertGreater(codes[0].center[0], 2000)
        self.assertGreater(codes[0].center[1], 1400)

    def test_degenerate_failed_decode_position_is_discarded(self):
        point = SimpleNamespace(x=0, y=0)
        failed = SimpleNamespace(
            position=SimpleNamespace(
                top_left=point, top_right=point, bottom_right=point, bottom_left=point
            ),
            valid=False,
            text="",
            format=zxingcpp.BarcodeFormat.MicroQRCode,
        )
        with patch("vision.qr.zxingcpp.read_barcodes", return_value=[failed]):
            self.assertEqual(detect_qr_codes(np.full((400, 500), 255, np.uint8)), [])

    def test_cancellation_before_and_between_native_detection_calls(self):
        image = np.full((400, 500), 255, np.uint8)
        with patch("vision.qr.zxingcpp.read_barcodes") as reader:
            with self.assertRaises(DetectionCancelled):
                detect_qr_codes(image, cancelled=lambda: True)
            reader.assert_not_called()

        stop = False

        def cancel_after_first_scan(*args, **kwargs):
            nonlocal stop
            stop = True
            return []

        with patch(
            "vision.qr.zxingcpp.read_barcodes", side_effect=cancel_after_first_scan
        ) as reader:
            with self.assertRaises(DetectionCancelled):
                detect_qr_codes(image, cancelled=lambda: stop)
            reader.assert_called_once()

    def test_clutter_without_codes(self):
        self.assertEqual(detect_qr_codes(camera_degradation(cluttered_background())), [])

    def test_laboratory_label_crops(self):
        directory = Path(__file__).resolve().parent / "fixtures/qr"
        # Crop boxes use source image2.jpg coordinates; right and bottom are exclusive.
        cases = [
            ("image2-2d-1.png", "2D", (377, 109, 832, 519), (604.0, 314.0)),
            ("image2-stamp-1.png", "Stamp", (258, 224, 563, 519), (410.75, 371.0)),
            ("image2-stamp-2.png", "Stamp", (254, 330, 569, 625), (410.5, 477.5)),
            ("image2-stamp-3.png", "Stamp", (266, 420, 576, 735), (420.25, 577.5)),
            ("image2-stamp-4.png", "Stamp", (265, 523, 570, 848), (416.75, 684.75)),
            (
                "image2-2c-1.png",
                "2C",
                (400, 625, 870, 1086),
                (634.5630493164062, 859.5630493164062),
            ),
        ]
        self.assertEqual({p.name for p in directory.glob("*.png")}, {case[0] for case in cases})
        self.assertLessEqual(
            sum(path.stat().st_size for path in directory.glob("*.png")), 1_000_000
        )
        decoded_payloads = []
        for name, payload, box, source_center in cases:
            with self.subTest(image=name):
                path = directory / name
                self.assertLessEqual(path.stat().st_size, 300_000)
                image = read_image(path)
                x0, y0, x1, y1 = box
                self.assertEqual(image.shape[:2], (y1 - y0, x1 - x0))
                codes = detect_qr_codes(image)
                target = np.asarray(source_center) - (x0, y0)
                # Margins overlap nearby labels. Select this crop's designated label.
                candidates = [code for code in codes if code.decoded and code.text == payload]
                self.assertTrue(candidates, f"No decoded {payload} in {name}")
                code = min(
                    candidates, key=lambda item: np.linalg.norm(np.asarray(item.center) - target)
                )
                np.testing.assert_allclose(code.center, target, atol=6)
                self.assertEqual(code.format, "Micro QR Code")
                decoded_payloads.append(code.text)
                height, width = image.shape[:2]
                corners = np.float32(code.corners)
                self.assertTrue(np.isfinite(corners).all())
                self.assertGreater(abs(cv2.contourArea(corners)), 16)
                self.assertTrue(cv2.isContourConvex(corners))
                self.assertGreaterEqual(corners[:, 0].min(), -1)
                self.assertGreaterEqual(corners[:, 1].min(), -1)
                self.assertLessEqual(corners[:, 0].max(), width)
                self.assertLessEqual(corners[:, 1].max(), height)
        self.assertEqual(Counter(decoded_payloads), Counter({"Stamp": 4, "2D": 1, "2C": 1}))


if __name__ == "__main__":
    unittest.main()
