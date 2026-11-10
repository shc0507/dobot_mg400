# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/extractors/aruco.py
"""Decode and verify DICT_4X4_50 markers in their original pixel coordinates."""

import cv2
import numpy as np

from ..marker_registry import ARUCO_DICTIONARY

CODEBOOK = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICTIONARY))
CANONICAL = np.float32([[0, 0], [119, 0], [119, 119], [0, 119]])
# Brightening local contrast helps on grooves. Closing isolates tags that
# touch dark bench holes. These passes only propose original-image quadrilaterals.
PASSES = [
    (1.0, "gray", 0),
    (0.5, "gray", 0),
    (0.75, "gray", 0),
    (0.5, "clahe", 0),
    (0.33, "clahe", 0),
    (0.25, "clahe", 0),
    (0.75, "close", 3),
    (0.75, "close", 5),
    (0.75, "close", 9),
    (0.5, "close", 3),
    (0.33, "close", 5),
    (0.25, "close", 5),
]


def verify(gray, corners, marker_id):
    """Re-read the candidate's 16 code cells and 20 border cells on original pixels."""
    matrix = cv2.getPerspectiveTransform(np.float32(corners), CANONICAL)
    patch = cv2.warpPerspective(gray, matrix, (120, 120))
    threshold = cv2.threshold(patch, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[0]
    cells = patch.reshape(6, 20, 6, 20).transpose(0, 2, 1, 3)[:, :, 5:15, 5:15]
    expected = cv2.aruco.generateImageMarker(CODEBOOK, marker_id, 6) > 0
    light = cells > threshold
    agreeing = np.mean(light == expected[:, :, None, None], axis=(2, 3))
    bits = np.median(cells, axis=(2, 3)) > threshold
    mismatches = bits != expected
    data_errors = int(mismatches[1:5, 1:5].sum())
    border_errors = int(mismatches.sum() - data_errors)
    contrast = float(np.median(cells[expected]) - np.median(cells[~expected]))
    if data_errors or border_errors or contrast < 32 or agreeing.min() < 0.75:
        return None
    return dict(
        data_bit_errors=data_errors,
        border_cell_errors=border_errors,
        min_cell_agreement=float(agreeing.min()),
        white_black_contrast=contrast,
        otsu_threshold=float(threshold),
    )


def diagonal_center(corners):
    a, b, c, d = np.float64(corners)
    t, _ = np.linalg.solve(np.column_stack((c - a, b - d)), b - a)
    return a + t * (c - a)


def refine_bit_edges(gray, seed, marker_id, validation, *, min_correlation=0.92, max_shift_px=16):
    """Fit a decoded marker grid, accepting only verified original pixel intensities."""
    detail = dict(
        applied=False, seed_corners_px=seed.tolist(), seed_center_px=diagonal_center(seed).tolist()
    )
    # The black perimeter spans the six-cell raster from -0.5 to 119.5.
    # Exclude the outermost strip from the fit where bench holes touch ink.
    square = np.float32([[-0.5, -0.5], [119.5, -0.5], [119.5, 119.5], [-0.5, 119.5]])
    rectifier = cv2.getPerspectiveTransform(seed, square)
    patch = cv2.warpPerspective(gray, rectifier, (120, 120))
    pattern = cv2.aruco.generateImageMarker(CODEBOOK, marker_id, 120)
    mask = np.zeros_like(pattern)
    mask[7:-7, 7:-7] = 255
    try:
        score, warp = cv2.findTransformECC(
            pattern,
            patch,
            np.eye(3, dtype=np.float32),
            cv2.MOTION_HOMOGRAPHY,
            (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-6),
            mask,
            5,
        )
        result = cv2.perspectiveTransform(square[None], np.linalg.solve(rectifier, warp))[0]
    except (cv2.error, np.linalg.LinAlgError):
        detail["status"] = "fit_failed"
        return seed, validation, detail
    if not np.isfinite(score) or not np.isfinite(result).all():
        detail["status"] = "nonfinite_fit"
        return seed, validation, detail
    detail["ecc_correlation"] = float(score)
    if score < min_correlation:
        detail["status"] = "low_correlation"
        return seed, validation, detail
    side = np.mean(np.linalg.norm(np.roll(seed, 1, axis=0) - seed, axis=1))
    shift = float(np.max(np.linalg.norm(result - seed, axis=1)))
    detail["max_corner_shift_px"] = shift
    height, width = gray.shape
    outside = (result < 0).any() or (result > [width - 1, height - 1]).any()
    if outside or not cv2.isContourConvex(result) or shift > min(max_shift_px, 0.14 * side):
        detail["status"] = "geometry_rejected"
        return seed, validation, detail
    checked = verify(gray, result, marker_id)
    if checked is None:
        detail["status"] = "native_cells_rejected"
        return seed, validation, detail
    detail.update(applied=True, status="accepted")
    return result, checked, detail


def decode_gray(data: bytes) -> np.ndarray:
    """Decode the stored raster using the same grayscale convention as calibration."""
    if not data:
        raise ValueError("The image is empty.")
    try:
        gray = cv2.imdecode(
            np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION
        )
    except cv2.error as exc:
        raise ValueError("The image could not be decoded.") from exc
    if gray is None:
        raise ValueError("The image could not be decoded.")
    return gray


def decode_color(data: bytes) -> np.ndarray:
    """Decode color without EXIF rotation for optional colored-background proposals."""
    if not data:
        raise ValueError("The image is empty.")
    try:
        color = cv2.imdecode(
            np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
        )
    except cv2.error as exc:
        raise ValueError("The image could not be decoded.") from exc
    if color is None:
        raise ValueError("The image could not be decoded.")
    return color


def _tight_margin_proposals(gray, color, existing_ids):
    """Propose tags whose black perimeter touches hardware, then verify native pixels.

    Saturated supports can hide a gray marker's perimeter. Whiten those supports
    only in the proposal image; the original grayscale frame remains the sole
    source of measured corners, code cells, and contrast. A second threshold pass
    separates dark support. Neither pass uses tag IDs or expected locations.
    """
    params = cv2.aruco.DetectorParameters()
    params.errorCorrectionRate = 0
    params.adaptiveThreshWinSizeMax = 103
    detector = cv2.aruco.ArucoDetector(CODEBOOK, params)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    thresholded = cv2.threshold(small, 80, 255, cv2.THRESH_BINARY)[1]
    images = [
        (
            "threshold80_0.5_k5",
            cv2.morphologyEx(thresholded, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)),
        )
    ]
    if color is not None:
        if color.shape[:2] != gray.shape:
            raise ValueError("Color and grayscale frames must use the same raster.")
        saturation = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)[:, :, 1]
        masked = gray.copy()
        masked[saturation > 120] = 255
        masked = cv2.resize(masked, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        images.append(
            (
                "saturation120_0.5_k3",
                cv2.morphologyEx(masked, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)),
            )
        )
    for method, proposal_image in images:
        corners, ids, _ = detector.detectMarkers(proposal_image)
        if ids is None:
            continue
        for candidate, raw_id in zip(corners, ids.flatten()):
            marker_id = int(raw_id)
            if marker_id in existing_ids:
                continue
            seed = ((candidate[0] + 0.5) / 0.5 - 0.5).astype(np.float32)
            # This proposal may have distorted perimeter corners. It is never
            # accepted directly. The stricter ECC fit must pass every native
            # data/border cell check, plus the same relative displacement bound.
            fitted, validation, detail = refine_bit_edges(
                gray, seed, marker_id, None, min_correlation=0.98, max_shift_px=24
            )
            detail["proposal_method"] = method
            detail["required_ecc_correlation"] = 0.98
            side = np.mean(np.linalg.norm(np.roll(seed, 1, axis=0) - seed, axis=1))
            detail["max_allowed_corner_shift_px"] = float(min(24, 0.14 * side))
            yield method, marker_id, fitted, validation, detail


def detect_markers(gray: np.ndarray, color: np.ndarray | None = None) -> dict:
    """Detect independently in one native-resolution grayscale frame."""
    h, w = gray.shape
    if min(h, w) < 3:
        return {
            "image_size_px": [w, h],
            "detections": [],
            "ambiguous_ids": [],
            "unverified_candidates": [],
        }
    params = cv2.aruco.DetectorParameters()
    params.errorCorrectionRate = 0
    detector = cv2.aruco.ArucoDetector(CODEBOOK, params)
    proposals = {}
    rejected_ids = set()
    for scale, operation, kernel in PASSES:
        small = (
            gray
            if scale == 1
            else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        )
        if operation == "clahe":
            small = cv2.createCLAHE(4, (12, 12)).apply(small)
        elif operation == "close":
            small = cv2.morphologyEx(small, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
        corners, ids, _ = detector.detectMarkers(small)
        if ids is None:
            continue
        for candidate, raw_id in zip(corners, ids.flatten()):
            marker_id = int(raw_id)
            xy = ((candidate[0] + 0.5) / scale - 0.5).astype(np.float32)
            # Refinement only uses the original image. A missing outer corner
            # can pull the fit toward background, so bound its displacement.
            polished = cv2.cornerSubPix(
                gray,
                xy.copy(),
                (4, 4),
                (-1, -1),
                (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 0.02),
            )
            if np.max(np.linalg.norm(xy - polished, axis=1)) > 4:
                polished = xy
            validation = verify(gray, polished, marker_id)
            if validation is None:
                rejected_ids.add(marker_id)
                continue
            tag = f"{operation}_{scale:g}" + (f"_k{kernel}" if kernel else "")
            proposals.setdefault(marker_id, []).append((tag, polished, validation))

    # Existing accepted IDs retain their original proposal selection and values.
    # The fallback can only add an independently verified missing marker.
    fallback_details = {}
    for method, marker_id, corners, validation, detail in _tight_margin_proposals(
        gray, color, set(proposals)
    ):
        if validation is None or not detail["applied"]:
            rejected_ids.add(marker_id)
            continue
        proposals.setdefault(marker_id, []).append((method, corners, validation))
        fallback_details[(method, marker_id)] = detail

    detections, ambiguous = [], []
    for marker_id, candidates in sorted(proposals.items()):
        centers = np.asarray([diagonal_center(row[1]) for row in candidates])
        med = np.median(centers, axis=0)
        # A reused ID in distant image locations has ambiguous identity.
        corners = candidates[0][1]
        side = np.mean(np.linalg.norm(np.roll(corners, 1, axis=0) - corners, axis=1))
        if np.max(np.linalg.norm(centers - med, axis=1)) > 0.18 * side:
            ambiguous.append(marker_id)
            continue
        # Use the native image solution when available. Otherwise select the
        # observed quad nearest the median center of accepted fallback passes.
        ix = next(
            (i for i, (tag, _, _) in enumerate(candidates) if tag == "gray_1"),
            int(np.argmin(np.linalg.norm(centers - med, axis=1))),
        )
        method, corners, validation = candidates[ix]
        refinement = fallback_details.get((method, marker_id))
        if refinement is None:
            corners, validation, refinement = refine_bit_edges(gray, corners, marker_id, validation)
        detections.append(
            dict(
                id=marker_id,
                center_px=diagonal_center(corners).tolist(),
                corners_px=corners.tolist(),
                method=method,
                refinement=refinement,
                verified_passes=[row[0] for row in candidates],
                center_spread_px=float(np.max(np.linalg.norm(centers - med, axis=1))),
                validation=validation,
            )
        )
    return dict(
        image_size_px=[w, h],
        detections=detections,
        ambiguous_ids=ambiguous,
        unverified_candidates=sorted(rejected_ids - proposals.keys()),
    )
