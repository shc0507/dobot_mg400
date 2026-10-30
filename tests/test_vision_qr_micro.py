"""Verify the optional finder recovery with real Micro QR encodings."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import zxingcpp

from vision.qr_micro import recover_micro


def micro_image():
    barcode = zxingcpp.create_barcode("2C", zxingcpp.BarcodeFormat.MicroQRCode)
    rendered = barcode.to_image(scale=10)
    return np.array(rendered, copy=True)


class MicroRecoveryTests(unittest.TestCase):
    def test_original_coordinates_and_known_code_exclusion(self):
        tile = micro_image()
        height, width = tile.shape
        image = np.full((500, 600), 235, np.uint8)
        image[120 : 120 + height, 200 : 200 + width] = tile
        found = recover_micro(image, [], lambda: None)
        self.assertEqual(len(found), 1)
        polygon, text = found[0]
        self.assertEqual(text, "2C")
        ys, xs = np.where(tile < 128)
        expected = np.float32(
            [
                [200 + xs.min(), 120 + ys.min()],
                [200 + xs.max(), 120 + ys.min()],
                [200 + xs.max(), 120 + ys.max()],
                [200 + xs.min(), 120 + ys.max()],
            ]
        )
        np.testing.assert_allclose(polygon, expected, atol=3)
        self.assertEqual(recover_micro(image, [expected], lambda: None), [])

    def test_close_up_code(self):
        found = recover_micro(micro_image(), [], lambda: None)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "2C")

    def test_clutter_with_finder_like_squares_is_not_a_code(self):
        rng = np.random.default_rng(2710)
        image = np.full((500, 600), 210, np.uint8)
        for _ in range(40):
            x, y = map(int, rng.integers([0, 0], [500, 400]))
            w, h = map(int, rng.integers(5, 75, size=2))
            cv2.rectangle(image, (x, y), (x + w, y + h), int(rng.integers(0, 256)), 2)
        for x, y in [(55, 60), (260, 190), (400, 320)]:
            for margin, color in [(0, 0), (5, 255), (10, 0)]:
                cv2.rectangle(
                    image, (x + margin, y + margin), (x + 35 - margin, y + 35 - margin), color, -1
                )
        self.assertEqual(recover_micro(image, [], lambda: None), [])

    def test_cancellation_propagates(self):
        def cancelled():
            raise InterruptedError("cancelled")

        with self.assertRaisesRegex(InterruptedError, "cancelled"):
            recover_micro(np.full((200, 200), 255, np.uint8), [], cancelled)

    def test_one_expansion_cannot_supply_multiple_confirmations(self):
        square = np.float32([[200, 200], [230, 200], [230, 230], [200, 230]])
        position = SimpleNamespace(
            top_left=SimpleNamespace(x=20, y=20),
            top_right=SimpleNamespace(x=149, y=20),
            bottom_right=SimpleNamespace(x=149, y=149),
            bottom_left=SimpleNamespace(x=20, y=149),
        )
        result = SimpleNamespace(
            valid=True,
            format=zxingcpp.BarcodeFormat.MicroQRCode,
            extra={"Version": "M2"},
            text="2C",
            position=position,
        )
        calls = 0

        def decode(image, **kwargs):
            nonlocal calls
            calls += 1
            # Only the first expansion's four M2 orientations report success.
            return [result] if 5 <= calls <= 8 else []

        # Freeze mapped coordinates so independent rotations of one fit would
        # otherwise look geometrically consistent and falsely confirm it.
        stable_polygon = np.float32([[180, 180], [310, 180], [310, 310], [180, 310]])
        with (
            patch("vision.qr_micro._finder_squares", return_value=iter([square])),
            patch("vision.qr_micro.zxingcpp.read_barcodes", side_effect=decode),
            patch("vision.qr_micro._clockwise", return_value=stable_polygon),
        ):
            found = recover_micro(np.full((600, 600), 255, np.uint8), [], lambda: None)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
