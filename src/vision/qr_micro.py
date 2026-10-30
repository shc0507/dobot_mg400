"""Bounded recovery of Micro QR codes with a damaged outer finder ring.

An intact central finder square proposes a grid; ZXing must independently decode
consistent content from multiple grid fits before the proposal is returned.
There are no image-specific coordinates or payload assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

import cv2
import numpy as np
import zxingcpp

_MAX_CANDIDATES = 40
_MAX_TRIALS = 1800
_GRID_VERSIONS = {11: "M1", 13: "M2", 15: "M3", 17: "M4"}


def _clockwise(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    return np.roll(points, -np.lexsort((points[:, 0], points[:, 1]))[0], axis=0)


def _inside_known(center: np.ndarray, known: list[np.ndarray]) -> bool:
    return any(
        cv2.pointPolygonTest(np.asarray(polygon, np.float32), tuple(map(float, center)), False) >= 0
        for polygon in known
    )


def _finder_squares(gray: np.ndarray, known: list[np.ndarray], check: Callable[[], None]):
    """Find central 3x3 squares whose surrounding pixels resemble a 7x7 finder."""
    expected = np.ones((7, 7), dtype=bool)
    expected[1:6, 1:6] = False
    expected[2:5, 2:5] = True
    white_ring = np.zeros((7, 7), dtype=bool)
    white_ring[1:6, 1:6] = True
    white_ring[2:5, 2:5] = False
    outer_ring = np.ones((7, 7), dtype=bool)
    outer_ring[1:6, 1:6] = False
    seen: list[tuple[np.ndarray, float]] = []
    target = np.float32([[20, 20], [50, 20], [50, 50], [20, 50]])
    for block_size in (0, 31, 61):
        check()
        if block_size == 0:
            binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
        else:
            binary = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block_size, 5
            )
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        # Large-to-small ordering makes the cap deterministic and favors better
        # resolved finder squares. Reject giant regions before sorting.
        choices = [(abs(cv2.contourArea(contour)), contour) for contour in contours]
        choices = [(area, contour) for area, contour in choices if 20 <= area <= gray.size * 0.1]
        choices.sort(key=lambda item: item[0], reverse=True)
        for index, (area, contour) in enumerate(choices[:5000]):
            if index % 64 == 0:
                check()
            perimeter = cv2.arcLength(contour, True)
            quad = cv2.approxPolyDP(contour, perimeter * 0.04, True)
            if len(quad) != 4 or not cv2.isContourConvex(quad):
                continue
            quad = _clockwise(quad)
            center = quad.mean(axis=0)
            if _inside_known(center, known):
                continue
            edges = np.linalg.norm(quad - np.roll(quad, 1, axis=0), axis=1)
            if edges.min() < 3 or edges.min() / edges.max() < 0.6:
                continue
            side = float(np.sqrt(area))
            if any(
                np.linalg.norm(center - old_center) < 3 and abs(side - old_side) < 3
                for old_center, old_side in seen
            ):
                continue
            transform = cv2.getPerspectiveTransform(quad, target)
            patch = cv2.warpPerspective(gray, transform, (70, 70), borderValue=255)
            samples = patch[5::10, 5::10]
            if int(samples.max()) - int(samples.min()) < 30:
                continue
            bits = cv2.threshold(samples, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1] > 0
            # Also allow light-on-dark finders.
            if np.count_nonzero(bits != expected) > np.count_nonzero(~bits != expected):
                bits = ~bits
            if (
                np.count_nonzero(bits != expected) > 10
                or np.count_nonzero(bits[2:5, 2:5]) < 7
                or np.count_nonzero(~bits[white_ring]) < 12
                or np.count_nonzero(bits[outer_ring]) < 17
            ):
                continue
            seen.append((center, side))
            yield quad
            if len(seen) >= _MAX_CANDIDATES:
                return


def _valid_quad(quad: np.ndarray, shape: tuple[int, int]) -> bool:
    height, width = shape
    return bool(
        np.isfinite(quad).all()
        and cv2.isContourConvex(np.asarray(quad, np.float32))
        and abs(cv2.contourArea(quad)) >= 40
        and np.all(quad[:, 0] >= -1)
        and np.all(quad[:, 1] >= -1)
        and np.all(quad[:, 0] <= width)
        and np.all(quad[:, 1] <= height)
    )


def recover_micro(
    gray: np.ndarray,
    known_polygons: list[np.ndarray],
    check: Callable[[], None],
) -> list[tuple[np.ndarray, str]]:
    """Return verified Micro QR polygons/text in this original grayscale image.

    `check` is called between bounded batches and may raise for cancellation.
    Full-resolution contours matter: errors in the small central square are
    magnified when extrapolating the full symbol. Only verified Micro QR results
    with a matching decoded version and repeated matching fits are accepted.
    """
    if gray.dtype != np.uint8 or gray.ndim != 2:
        raise ValueError("Micro QR recovery expects a uint8 grayscale image.")
    if min(gray.shape) < 21:
        return []
    found: list[tuple[np.ndarray, str]] = []
    known = list(known_polygons)
    trials = 0
    unit, quiet = 10, 20
    target = np.float32([[20, 20], [50, 20], [50, 50], [20, 50]])
    for square in _finder_squares(gray, known, check):
        check()
        center = square.mean(axis=0)
        if _inside_known(center, known):
            continue
        hits: dict[tuple[str, str], list[tuple[float, np.ndarray]]] = defaultdict(list)
        accepted = False
        # Contours track the last dark pixel, slightly inside the true module
        # boundary. Small independent expansions account for that uncertainty.
        for expansion in (1.02, 1.06, 1.10):
            expanded = np.asarray((square - center) * expansion + center, np.float32)
            for modules, version in _GRID_VERSIONS.items():
                for rotation in range(4):
                    check()
                    trials += 1
                    if trials > _MAX_TRIALS:
                        return found
                    transform = cv2.getPerspectiveTransform(
                        np.roll(expanded, rotation, axis=0), target
                    )
                    inverse = np.linalg.inv(transform)
                    extent = modules * unit
                    frame = np.float32([[[0, 0], [extent, 0], [extent, extent], [0, extent]]])
                    predicted = cv2.perspectiveTransform(frame, inverse)[0]
                    if not _valid_quad(predicted, gray.shape):
                        continue
                    rectified = cv2.warpPerspective(
                        gray, transform, (extent, extent), flags=cv2.INTER_CUBIC, borderValue=255
                    )
                    rectified = cv2.copyMakeBorder(
                        rectified, quiet, quiet, quiet, quiet, cv2.BORDER_CONSTANT, value=255
                    )
                    decoded = zxingcpp.read_barcodes(
                        rectified,
                        formats=[zxingcpp.BarcodeFormat.MicroQRCode],
                        is_pure=True,
                        try_rotate=True,
                        try_downscale=False,
                        try_invert=True,
                        text_mode=zxingcpp.TextMode.Plain,
                    )
                    for code in decoded:
                        if not code.valid or code.format != zxingcpp.BarcodeFormat.MicroQRCode:
                            continue
                        metadata = getattr(code, "extra", None)
                        if not isinstance(metadata, dict) or metadata.get("Version") != version:
                            continue
                        position = code.position
                        points = np.float32(
                            [
                                [
                                    (p.x - quiet, p.y - quiet)
                                    for p in (
                                        position.top_left,
                                        position.top_right,
                                        position.bottom_right,
                                        position.bottom_left,
                                    )
                                ]
                            ]
                        )
                        polygon = _clockwise(cv2.perspectiveTransform(points, inverse)[0])
                        if not _valid_quad(polygon, gray.shape) or _inside_known(
                            polygon.mean(axis=0), known
                        ):
                            continue
                        key = (version, code.text)
                        previous = hits[key]
                        if any(old_expansion == expansion for old_expansion, _ in previous):
                            continue
                        if previous:
                            tolerance = 0.1 * np.sqrt(abs(cv2.contourArea(polygon)))
                            if np.max(np.linalg.norm(polygon - previous[0][1], axis=1)) > tolerance:
                                continue
                        previous.append((expansion, polygon))
                        # M1 has no error correction; require all three fits.
                        if len(previous) >= (3 if version == "M1" else 2):
                            found.append((previous[0][1], code.text))
                            known.append(previous[0][1])
                            accepted = True
                            break
                    if accepted:
                        break
                if accepted:
                    break
            if accepted:
                break
    return found
