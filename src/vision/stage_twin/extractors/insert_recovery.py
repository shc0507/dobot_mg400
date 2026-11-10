# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/extractors/insert_recovery.py
"""Recover the insert tag from current pixels inside its calibrated travel path."""

from dataclasses import dataclass

import cv2
import numpy as np

from ..marker_registry import MARKER_GROUPS
from .aruco import CODEBOOK, diagonal_center, refine_bit_edges

INSERT_ID = 22
# Cap work even when an imported profile spans the whole 24 MP input image.
MAX_SEARCH_EDGE = 1024
MAX_SEARCH_PIXELS = 180_000
PEAKS_PER_SHAPE = 4
MIN_MATCH = 0.58
MIN_NATIVE_CORRELATION = 0.96
METHOD = "insert_corridor"


@dataclass(frozen=True)
class InsertSearch:
    expected_reference_ids: tuple
    dimensions: tuple
    # Two corner shapes centered on ID 22, indexed out/in. Corner order retains
    # the physical marker orientation from the original ArUco decodes.
    shapes: np.ndarray | None
    # Maps each fitted parent ID to the two relative center coordinates.
    relative_centers: dict


def build_insert_search(profile):
    """Prepare geometry from an evaluated profile; partial profiles may lack it."""
    if not profile:
        return None
    axis = profile.get("axes", {}).get("insert", {})
    parents = {
        track["reference_id"] for track in axis.get("tracks", ()) if track["moving_id"] == INSERT_ID
    }
    parents.intersection_update(MARKER_GROUPS["x"].marker_ids)
    if not parents:
        return None
    expected = tuple(sorted(parents))
    captures = [profile.get("endpoints", {}).get(key) for key in ("insert_out", "insert_in")]
    dimensions = profile.get("camera", {}).get("dimensions")
    empty = InsertSearch(expected, tuple(dimensions or ()), None, {})
    if not dimensions or any(not obs or obs["image_size_px"] != dimensions for obs in captures):
        return empty
    tags = [{tag["id"]: tag for tag in obs["detections"]} for obs in captures]
    if any(INSERT_ID not in endpoint for endpoint in tags):
        return empty
    parents &= tags[0].keys() & tags[1].keys()
    if not parents:
        return empty
    centers = np.float32([endpoint[INSERT_ID]["center_px"] for endpoint in tags])
    corners = np.float32([endpoint[INSERT_ID]["corners_px"] for endpoint in tags])
    relative = {
        parent: centers - np.float32([endpoint[parent]["center_px"] for endpoint in tags])
        for parent in sorted(parents)
    }
    return InsertSearch(expected, tuple(dimensions), corners - centers[:, None, :], relative)


def _search_image(gray, path, shapes, side):
    # Include the whole printed square and an additional 1/3-side allowance
    # for parallax as the X and Y stages translate beneath the angled camera.
    pad = side / 3
    corners = (path[:, None, :] + shapes).reshape(-1, 2)
    height, width = gray.shape
    lo = np.maximum(np.floor(corners.min(axis=0) - pad), 0).astype(int)
    hi = np.minimum(np.ceil(corners.max(axis=0) + pad + 1), [width, height]).astype(int)
    size = hi - lo
    if np.any(size < 12):
        return None
    scale = min(
        1.0,
        72 / side,
        MAX_SEARCH_EDGE / max(size),
        (MAX_SEARCH_PIXELS / (int(size[0]) * int(size[1]))) ** 0.5,
    )
    small_size = np.maximum(1, np.floor(size * scale)).astype(int)
    if min(small_size) < 12 or side * scale < 12:
        return None
    patch = gray[lo[1] : hi[1], lo[0] : hi[0]]
    small = cv2.resize(patch, tuple(small_size), interpolation=cv2.INTER_AREA)
    return small, lo, small_size / size


def _candidate_peaks(image, local_path, shape, side):
    """Return a few local template peaks, restricted to the center travel tube."""
    top_left = np.floor(shape.min(axis=0)).astype(int)
    size = np.ceil(shape.max(axis=0)).astype(int) - top_left + 1
    if min(size) < 12 or np.any(size > image.shape[::-1]):
        return []
    offsets = np.float32(shape - top_left)
    if not cv2.isContourConvex(offsets):
        return []
    origin = np.float32([[-0.5, -0.5], [119.5, -0.5], [119.5, 119.5], [-0.5, 119.5]])
    transform = cv2.getPerspectiveTransform(origin, offsets)
    pattern = cv2.aruco.generateImageMarker(CODEBOOK, INSERT_ID, 120)
    template = cv2.warpPerspective(pattern, transform, tuple(size), borderValue=128)
    # Compare the printed tag interior. Areas outside the oriented quad can
    # be bright fixtures, screws or shadows in the actual camera raster.
    mask = np.zeros(template.shape, np.uint8)
    cv2.fillConvexPoly(mask, np.int32(np.round(offsets)), 255)
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    scores = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED, mask=mask)
    # A uniform patch has undefined normalized correlation. Some OpenCV
    # builds produce large finite outliers as well as NaN in that case.
    scores[~np.isfinite(scores) | (scores > 1.001)] = -1
    allowed = np.zeros(scores.shape, np.uint8)
    a, b = np.round(local_path + top_left).astype(int)
    cv2.line(allowed, tuple(a), tuple(b), 1, max(2, int(np.ceil(side * 2 / 3))))
    scores[allowed == 0] = -1
    peaks = []
    for _ in range(PEAKS_PER_SHAPE):
        _, score, _, location = cv2.minMaxLoc(scores)
        if score < MIN_MATCH:
            break
        peaks.append((score, np.float32(location) + offsets))
        cv2.circle(scores, location, max(3, round(side * 0.3)), -1, -1)
    return peaks


def recover_insert(gray, observation, search):
    """Try a local search only for a missing tag with a current fitted parent.

    The input is the same native grayscale raster used for the detector's
    observation. Return a new observation only on recovery or ambiguity.
    """
    detections = {tag["id"]: tag for tag in observation["detections"]}
    ambiguous = set(observation.get("ambiguous_ids", ()))
    expected = list(search.expected_reference_ids) if search is not None else []
    refs = [ref for ref in expected if ref in detections and ref not in ambiguous]
    detail = {"target_id": INSERT_ID, "reference_ids": refs, "expected_reference_ids": expected}

    def finish(status, *, result=observation, **extra):
        return result, {**detail, "status": status, **extra}

    if search is None:
        return finish("unavailable")
    if not refs:
        return finish("missing_reference")
    if INSERT_ID in ambiguous:
        return finish("ambiguous")
    if INSERT_ID in detections:
        return finish("detected")

    anchors = [ref for ref in refs if ref in search.relative_centers]
    if not anchors or search.shapes is None or search.dimensions != gray.shape[::-1]:
        return finish("unavailable")
    paths = np.array(
        [np.asarray(detections[ref]["center_px"]) + search.relative_centers[ref] for ref in anchors]
    )
    path = np.median(paths, axis=0)
    side = float(np.mean(np.linalg.norm(search.shapes - np.roll(search.shapes, 1, axis=1), axis=2)))
    if side < 12 or np.max(np.linalg.norm(paths - path, axis=2)) > side / 2:
        return finish("unreadable", reason="inconsistent_or_small_geometry")
    roi = _search_image(gray, path, search.shapes, side)
    if roi is None:
        return finish("unreadable", reason="insufficient_search_resolution")
    image, offset, scale = roi
    local_path = (path - offset + 0.5) * scale - 0.5
    peaks = []
    for fraction in (0, 0.5, 1):
        shape = (1 - fraction) * search.shapes[0] + fraction * search.shapes[1]
        peaks.extend(_candidate_peaks(image, local_path, shape * scale, side * min(scale)))

    accepted = None
    for score, quad in sorted(peaks, key=lambda peak: peak[0], reverse=True):
        seed = np.float32((quad + 0.5) / scale - 0.5 + offset)
        if accepted and np.linalg.norm(diagonal_center(seed) - accepted["center_px"]) < 0.2 * side:
            continue
        fitted, validation, refinement = refine_bit_edges(
            gray, seed, INSERT_ID, None, min_correlation=MIN_NATIVE_CORRELATION, max_shift_px=24
        )
        if validation is None or not refinement["applied"]:
            continue
        center = diagonal_center(fitted)
        if accepted:
            if np.linalg.norm(center - accepted["center_px"]) <= 0.2 * side:
                continue
            result = {**observation, "ambiguous_ids": sorted(ambiguous | {INSERT_ID})}
            return finish("ambiguous", result=result, reason="multiple_verified_locations")
        refinement.update(proposal_method=METHOD, template_correlation=float(score))
        accepted = {
            "id": INSERT_ID,
            "center_px": center.tolist(),
            "corners_px": fitted.tolist(),
            "method": METHOD,
            "refinement": refinement,
            "verified_passes": [METHOD],
            "center_spread_px": 0.0,
            "validation": validation,
        }
    if not accepted:
        return finish("unreadable", reason="no_verified_location")
    result = {
        **observation,
        "detections": sorted([*observation["detections"], accepted], key=lambda tag: tag["id"]),
        "unverified_candidates": [
            item for item in observation.get("unverified_candidates", []) if item != INSERT_ID
        ],
    }
    return finish("recovered", result=result)
