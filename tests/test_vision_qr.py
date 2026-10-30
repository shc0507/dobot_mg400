import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from vision.qr import detect_qr_codes, read_image, results_csv


def make_qr(text, scale=8):
    modules = cv2.QRCodeEncoder_create().encode(text)
    modules = cv2.copyMakeBorder(modules, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    return cv2.resize(modules, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)


def make_scene():
    image = np.full((650, 1000, 3), 255, np.uint8)
    expected = {}
    for text, x, y, rotated in [("first-code", 55, 65, False), ("second-code", 610, 300, True)]:
        tile = make_qr(text)
        if rotated:
            tile = cv2.rotate(tile, cv2.ROTATE_90_CLOCKWISE)
        height, width = tile.shape
        image[y : y + height, x : x + width] = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        ys, xs = np.where(tile < 128)
        expected[text] = (x + xs.min(), y + ys.min(), x + xs.max(), y + ys.max())
    return image, expected


class DetectionTests(unittest.TestCase):
    def test_single_code_coordinates(self):
        tile = make_qr("single-code")
        image = np.full((430, 580), 255, np.uint8)
        height, width = tile.shape
        image[53 : 53 + height, 127 : 127 + width] = tile
        codes = detect_qr_codes(image)
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0].text, "single-code")
        ys, xs = np.where(tile < 128)
        expected = (127 + xs.min(), 53 + ys.min(), 127 + xs.max(), 53 + ys.max())
        np.testing.assert_allclose(codes[0].bounds, expected, atol=2)
        np.testing.assert_allclose(
            codes[0].center,
            [(expected[0] + expected[2]) / 2, (expected[1] + expected[3]) / 2],
            atol=2,
        )

    def test_multiple_codes_including_rotation(self):
        image, expected = make_scene()
        codes = detect_qr_codes(image)
        self.assertEqual([code.text for code in codes], ["first-code", "second-code"])
        for code in codes:
            np.testing.assert_allclose(code.bounds, expected[code.text], atol=3)
            self.assertEqual(code.corners[0], min(code.corners, key=lambda p: (p[1], p[0])))
            points = np.array(code.corners)
            following = np.roll(points, -1, axis=0)
            # Positive shoelace area is clockwise when image y increases downward.
            area = np.sum(points[:, 0] * following[:, 1] - points[:, 1] * following[:, 0])
            self.assertGreater(area, 0)

    def test_arbitrary_rotation_keeps_original_coordinates(self):
        tile = make_qr("tilted-code", scale=10)
        image = np.full((700, 700), 255, np.uint8)
        height, width = tile.shape
        image[180 : 180 + height, 160 : 160 + width] = tile
        ys, xs = np.where(tile < 128)
        corners = np.float32(
            [
                [xs.min() + 160, ys.min() + 180],
                [xs.max() + 160, ys.min() + 180],
                [xs.max() + 160, ys.max() + 180],
                [xs.min() + 160, ys.max() + 180],
            ]
        )
        transform = cv2.getRotationMatrix2D((350, 350), 27, 1.0)
        rotated = cv2.warpAffine(image, transform, (700, 700), borderValue=255)
        expected = cv2.transform(corners.reshape(1, 4, 2), transform)[0]
        codes = detect_qr_codes(rotated)
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0].text, "tilted-code")
        for point in codes[0].corners:
            self.assertLess(np.linalg.norm(expected - point, axis=1).min(), 4)

    def test_blank_and_tiny_images(self):
        self.assertEqual(detect_qr_codes(np.full((350, 350, 3), 255, np.uint8)), [])
        self.assertEqual(detect_qr_codes(np.zeros((1, 1), np.uint8)), [])

    def test_invalid_image_array(self):
        for image in [
            None,
            np.array([], dtype=np.uint8),
            np.zeros((20, 20), np.float32),
            np.zeros((30, 30, 2), np.uint8),
        ]:
            with self.assertRaises(ValueError):
                detect_qr_codes(image)

    def test_failed_decode_retains_location_and_single_detection_fallback(self):
        polygon = np.float32([[[15, 20], [90, 20], [90, 95], [15, 95]]])
        detector = Mock()
        detector.detectMulti.return_value = (False, None)
        detector.detect.return_value = (True, polygon)
        detector.decode.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with patch("vision.qr.cv2.QRCodeDetector", return_value=detector):
            codes = detect_qr_codes(np.full((110, 110), 255, np.uint8))
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0].bounds, (15, 20, 90, 95))
        self.assertEqual(codes[0].text, "")

    def test_csv_round_trip_with_quoted_payload(self):
        payload = 'Hello, "QR"\nsecond line'
        codes = detect_qr_codes(make_qr(payload))
        self.assertEqual(len(codes), 1)
        rows = list(csv.DictReader(io.StringIO(results_csv(codes))))
        self.assertEqual(rows[0]["decoded_text"], payload)
        self.assertEqual(float(rows[0]["center_x"]), codes[0].center[0])
        self.assertEqual(float(rows[0]["p4_y"]), codes[0].corners[3][1])

    def test_unicode_path_and_alpha_composited_on_white(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "QR_测试_é.png"
            tile = make_qr("transparent-code")
            bgra = np.zeros((*tile.shape, 4), np.uint8)
            bgra[:, :, 3] = 255 - tile
            ok, encoded = cv2.imencode(".png", bgra)
            self.assertTrue(ok)
            path.write_bytes(encoded.tobytes())
            image = read_image(path)
            np.testing.assert_array_equal(image[0, 0], [255, 255, 255])
            self.assertEqual(detect_qr_codes(image)[0].text, "transparent-code")

    def test_16bit_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "16bit.png"
            tile = make_qr("sixteen-bit").astype(np.uint16) * 257
            ok, encoded = cv2.imencode(".png", tile)
            self.assertTrue(ok)
            path.write_bytes(encoded.tobytes())
            image = read_image(path)
            self.assertEqual(image.dtype, np.uint8)
            self.assertEqual(detect_qr_codes(image)[0].text, "sixteen-bit")

    def test_missing_empty_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.png"
            with self.assertRaises(FileNotFoundError):
                read_image(path)
            for content in (b"", b"not an image"):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_image(path)


if __name__ == "__main__":
    unittest.main()
