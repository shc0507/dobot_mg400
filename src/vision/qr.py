"""QR localization and CSV export, independent of the GUI."""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import zxingcpp

from vision.qr_micro import recover_micro

Point = tuple[float, float]


@dataclass(frozen=True)
class QRCode:
    """Corners are clockwise, starting at the topmost (then leftmost) point."""

    corners: tuple[Point, Point, Point, Point]
    text: str = ""
    format: str = "QR Code"
    decoded: bool = False
    source: str = "OpenCV"

    @property
    def status(self) -> str:
        return "Decoded" if self.decoded else "Located; unverified"

    @property
    def center(self) -> Point:
        return (
            sum(point[0] for point in self.corners) / 4,
            sum(point[1] for point in self.corners) / 4,
        )

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """Outward-rounded x_min, y_min, x_max, y_max (inclusive)."""
        xs, ys = zip(*self.corners)
        return math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))


def read_image(path: str | Path) -> np.ndarray:
    """Read a Unicode path as 8-bit BGR, preserving stored pixel orientation.

    Transparent pixels are composited over white. 16-bit images are converted
    to 8-bit for detection. IMREAD_UNCHANGED deliberately ignores EXIF rotation.
    """
    encoded = Path(path).read_bytes()
    if not encoded:
        raise ValueError("The selected file is empty.")
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("Cannot read this image. Choose a PNG, JPEG, BMP, TIFF, or WebP file.")
    if image.dtype == np.uint16:
        image = (image / 257.0).round().astype(np.uint8)
    elif image.dtype != np.uint8:
        raise ValueError("Use an 8-bit or 16-bit image; floating-point images are not supported.")
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        return np.round(image[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    if image.shape[2] != 3:
        raise ValueError("Unsupported image color format.")
    return image


def _ordered_corners(points: np.ndarray) -> tuple[Point, Point, Point, Point]:
    points = np.asarray(points, dtype=np.float64).reshape(4, 2)
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]  # Clockwise in image coordinates (y down).
    first = np.lexsort((points[:, 0], points[:, 1]))[0]
    points = np.roll(points, -int(first), axis=0)
    return tuple((float(x), float(y)) for x, y in points)


class DetectionCancelled(Exception):
    """Cooperative cancellation between native detection calls."""


def _grayscale(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.size == 0:
        raise ValueError("Expected a nonempty uint8 image.")
    if image.ndim == 2:
        return np.ascontiguousarray(image)
    if image.ndim == 3 and image.shape[2] in (3, 4):
        if image.shape[2] == 4:
            alpha = image[:, :, 3:4].astype(np.float32) / 255.0
            image = np.round(image[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raise ValueError("Expected a grayscale, BGR, or BGRA image.")


def _scaled(image: np.ndarray, limit: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = min(1.0, limit / max(height, width))
    if scale == 1:
        return image
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _valid_polygon(points: np.ndarray, width: int, height: int) -> bool:
    """Some failed ZXing decodes return four (0, 0) points: never report those."""
    if points.shape != (4, 2) or not np.isfinite(points).all():
        return False
    if (points < -1).any() or (points[:, 0] > width + 1).any() or (points[:, 1] > height + 1).any():
        return False
    polygon = np.float32(_ordered_corners(points))
    edges = np.linalg.norm(polygon - np.roll(polygon, -1, axis=0), axis=1)
    return bool(
        edges.min() >= 3 and cv2.isContourConvex(polygon) and cv2.contourArea(polygon) >= 25
    )


def _same_location(first: QRCode, second: QRCode) -> bool:
    a, b = np.float32(first.corners), np.float32(second.corners)
    area_a, area_b = cv2.contourArea(a), cv2.contourArea(b)
    intersection, _ = cv2.intersectConvexConvex(a, b)
    union = area_a + area_b - intersection
    return union > 0 and intersection / union >= 0.45


def _merge_result(results: list[QRCode], candidate: QRCode) -> None:
    # Identical label text at different locations represents DIFFERENT codes.
    for index, previous in enumerate(results):
        if _same_location(previous, candidate):
            if candidate.decoded and not previous.decoded:
                results[index] = candidate
            return
    results.append(candidate)


def _tiles(width: int, height: int):
    """Overlapping windows, at most 7 per axis even for unusually large photos."""
    size = max(1024, math.ceil(max(width, height) / 5))
    step = max(1, size * 3 // 4)

    def starts(length):
        last = max(0, length - size)
        return sorted(set([*range(0, last + 1, step), last]))

    for y in starts(height):
        for x in starts(width):
            yield x, y, min(x + size, width), min(y + size, height)


def detect_qr_codes(
    image: np.ndarray,
    *,
    thorough: bool = True,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[QRCode]:
    """Find QR and Micro QR codes in photographs, in original raster coordinates.

    ZXing searches rotations, inverted codes, and a resolution pyramid. Thorough
    mode also checks contrast/sharpened views and overlapping detail tiles, then
    attempts verified Micro QR recovery from partially damaged finder patterns.
    OpenCV locates unreadable standard QR candidates at a bounded resolution.
    An unreadable candidate is explicitly unverified, not a confirmed QR code.
    Every external resize/crop is mapped back with its actual x and y scale.
    """
    gray = _grayscale(image)
    height, width = gray.shape
    if min(height, width) < 11:  # Micro QR starts at 11 x 11 modules.
        return []
    results: list[QRCode] = []

    def checkpoint():
        if cancelled is not None and cancelled():
            raise DetectionCancelled()

    def report(message: str):
        checkpoint()
        if progress is not None:
            progress(message)

    def scan(view: np.ndarray, region: tuple[int, int, int, int], name: str):
        report(name)
        x0, y0, x1, y1 = region
        view_height, view_width = view.shape
        scale = np.array([(x1 - x0) / view_width, (y1 - y0) / view_height])
        barcodes = zxingcpp.read_barcodes(
            np.ascontiguousarray(view),
            formats=[zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.MicroQRCode],
            try_rotate=True,
            try_downscale=True,
            try_invert=True,
            text_mode=zxingcpp.TextMode.Plain,
            return_errors=True,
        )
        for barcode in barcodes:
            position = barcode.position
            polygon = np.array(
                [
                    (point.x, point.y)
                    for point in (
                        position.top_left,
                        position.top_right,
                        position.bottom_right,
                        position.bottom_left,
                    )
                ],
                dtype=np.float64,
            )
            if not _valid_polygon(polygon, view_width, view_height):
                continue
            polygon = polygon * scale + (x0, y0)
            if not _valid_polygon(polygon, width, height):
                continue
            _merge_result(
                results,
                QRCode(
                    corners=_ordered_corners(polygon),
                    text=barcode.text if barcode.valid else "",
                    format=str(barcode.format),
                    decoded=bool(barcode.valid),
                    source=f"ZXing: {name}",
                ),
            )

    whole = (0, 0, width, height)
    scan(gray, whole, "Reading QR / Micro QR at original resolution…")
    if thorough:
        overview = _scaled(gray, 2000)
        equalizer = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = equalizer.apply(overview)
        scan(enhanced, whole, "Checking local contrast and shadows…")
        sharpened = cv2.addWeighted(overview, 1.6, cv2.GaussianBlur(overview, (0, 0), 1.0), -0.6, 0)
        scan(sharpened, whole, "Checking mildly blurred details…")
        if max(width, height) > 1600:
            regions = list(_tiles(width, height))
            for index, region in enumerate(regions, 1):
                x0, y0, x1, y1 = region
                tile = _scaled(gray[y0:y1, x0:x1], 1600)
                scan(tile, region, f"Searching small labels: tile {index}/{len(regions)}…")
                scan(
                    equalizer.apply(tile),
                    region,
                    f"Checking tile contrast: {index}/{len(regions)}…",
                )

        report("Recovering partly obscured Micro QR finder patterns…")
        known = [np.float32(code.corners) for code in results if code.decoded]
        for polygon, text in recover_micro(gray, known, checkpoint):
            if _valid_polygon(polygon, width, height):
                _merge_result(
                    results,
                    QRCode(
                        corners=_ordered_corners(polygon),
                        text=text,
                        format="Micro QR Code",
                        decoded=True,
                        source="ZXing: finder geometry recovery",
                    ),
                )

    # Do not run classical OpenCV on huge noisy frames: it can become very slow.
    # Separate localization from decoding to retain damaged standard QR codes.
    report("Checking unreadable standard QR candidates…")
    fallback = _scaled(gray, 960)
    detectors = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.insert(0, cv2.QRCodeDetectorAruco())
    for detector in detectors:
        report("Checking unreadable standard QR candidates…")
        try:
            found, points = detector.detectMulti(fallback)
            if not found or points is None or len(points) == 0:
                found, points = detector.detect(fallback)
        except cv2.error:
            continue  # A failure of one backend must not discard other results.
        if not found or points is None:
            continue
        for polygon in np.asarray(points, dtype=np.float32).reshape(-1, 4, 2):
            if not _valid_polygon(polygon, fallback.shape[1], fallback.shape[0]):
                continue
            original = polygon * np.array([width / fallback.shape[1], height / fallback.shape[0]])
            candidate = QRCode(corners=_ordered_corners(original))
            if any(code.decoded and _same_location(code, candidate) for code in results):
                continue
            try:
                text, _ = detector.decode(fallback, polygon.reshape(1, 4, 2))
            except (cv2.error, UnicodeDecodeError):
                text = ""
            _merge_result(results, QRCode(corners=candidate.corners, text=text, decoded=bool(text)))

    report("Detection complete.")
    return sorted(results, key=lambda code: (code.center[1], code.center[0]))


def results_csv(codes: list[QRCode]) -> str:
    """Serialize original-image coordinates; corner/center values retain precision."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["id", "center_x", "center_y", "x_min", "y_min", "x_max", "y_max"]
        + [f"p{index}_{axis}" for index in range(1, 5) for axis in ("x", "y")]
        + ["decoded_text", "format", "status", "source"]
    )
    for index, code in enumerate(codes, start=1):
        writer.writerow(
            [index, *code.center, *code.bounds]
            + [coordinate for point in code.corners for coordinate in point]
            + [code.text, code.format, code.status, code.source]
        )
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate QR and Micro QR codes in a photograph.")
    parser.add_argument(
        "image", type=Path, help="Image to scan (coordinates use its stored raster)"
    )
    parser.add_argument("--output", type=Path, help="Write CSV to this path instead of stdout")
    parser.add_argument(
        "--fast", action="store_true", help="Skip extra contrast and tiled recovery passes"
    )
    args = parser.parse_args()
    try:
        codes = detect_qr_codes(read_image(args.image), thorough=not args.fast)
        csv_text = results_csv(codes)
        if args.output is not None:
            with args.output.open("w", encoding="utf-8-sig", newline="") as output:
                output.write(csv_text)
        else:
            sys.stdout.write(csv_text)
    except (OSError, ValueError, cv2.error) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(
        f"{sum(code.decoded for code in codes)} decoded; "
        f"{sum(not code.decoded for code in codes)} unverified location(s).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
