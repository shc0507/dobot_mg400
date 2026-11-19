# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/extractors/aruco_presets.py
"""ArUco detector presets shared by monitor measurements and calibration snaps."""

from functools import partial

# Different proposal thresholds separate
# the printed perimeter from dark holes, reflective metal or uneven lighting.
# Values are max image edge in pixels, operation, and thresholds or CLAHE limits.
_RETRIES = (
    (1280, "threshold", (40, 32, 39, 56)),
    (1600, "threshold", (130, 158, 180)),
    (1600, "contrast", (2, 5)),
    (1920, "threshold", (48, 70, 28)),
    (1280, "local", (31, 61, 95)),
    (1920, "threshold", (96, 144, 196)),
    (1920, "contrast", (3, 8)),
)


def _preset_label(preset, monitor=False):
    if preset == 1:
        return "Standard"
    edge, operation, levels = _RETRIES[preset - 2]
    name = {
        "threshold": "Brightness threshold",
        "contrast": "Local contrast",
        "local": "Adaptive threshold",
    }[operation]
    if not monitor:
        return f"{name} ({edge} px)"
    if preset == 2:
        return f"Dark thresholds + red channel ({edge} px)"
    settings = ", ".join(map(str, levels))
    return f"{name} {settings} ({edge} px)"


def monitor_preset_catalog():
    return [
        {"id": number, "label": _preset_label(number, monitor=True)}
        for number in range(1, len(_RETRIES) + 2)
    ]


def detector_for_preset(preset, targets, *, monitor=False):
    """Return a one-frame detector, or the default analyze_frame path.

    Import the camera dependencies only for detection. Both the dashboard config
    and numerical profile operations can describe presets without opening a camera.
    """
    if type(preset) is not int or not 1 <= preset <= len(_RETRIES) + 1:
        raise ValueError(f"Detector preset must be an integer from 1 to {len(_RETRIES) + 1}.")
    if preset == 1 or not targets:
        return None
    return partial(_tuned_detection, targets=frozenset(targets), preset=preset, monitor=monitor)


def _proposal_images(small, operation, levels, red):
    import cv2

    for level in levels:
        if operation == "threshold":
            yield cv2.threshold(small, level, 255, cv2.THRESH_BINARY)[1]
        elif operation == "contrast":
            yield cv2.createCLAHE(level, (8, 8)).apply(small)
        else:
            yield cv2.adaptiveThreshold(
                small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, level, 5
            )

    if red is not None:
        # The orange insert housing can merge with a tag in grayscale. In the
        # red channel the housing brightens and its boundary separates.
        yield cv2.morphologyEx(
            red, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        )


def _same_location(seed, marker):
    import numpy as np

    from .aruco import diagonal_center

    corners = np.float32(marker["corners_px"])
    side = np.mean(np.linalg.norm(np.roll(corners, 1, axis=0) - corners, axis=1))
    return np.linalg.norm(diagonal_center(seed) - marker["center_px"]) <= 0.18 * side


def _tuned_detection(gray, color, *, targets, preset, monitor):
    import cv2
    import numpy as np

    from .aruco import CODEBOOK, detect_markers, diagonal_center, refine_bit_edges

    baseline = detect_markers(gray, color)
    accepted = {marker["id"]: marker for marker in baseline["detections"]}
    if targets <= accepted.keys():
        return {**baseline, "method": "Standard"}

    edge, operation, levels = _RETRIES[preset - 2]
    height, width = gray.shape
    scale = min(1.0, edge / max(height, width))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    to_native = np.float32((width / size[0], height / size[1]))

    params = cv2.aruco.DetectorParameters()
    params.errorCorrectionRate = 0
    params.adaptiveThreshWinSizeMax = 73
    detector = cv2.aruco.ArucoDetector(CODEBOOK, params)
    ambiguous = set(baseline["ambiguous_ids"])
    red = None
    if monitor and preset == 2:
        red = cv2.resize(color[:, :, 2], size, interpolation=cv2.INTER_AREA)
    for proposal in _proposal_images(small, operation, levels, red):
        corners, ids, _ = detector.detectMarkers(proposal)
        if ids is None:
            continue
        for quad, candidate in zip(corners, ids.flatten()):
            marker_id = int(candidate)
            if marker_id not in targets or marker_id in ambiguous:
                continue
            seed = (quad[0] + 0.5) * to_native - 0.5
            existing = accepted.get(marker_id)
            if existing is not None and _same_location(seed, existing):
                continue
            # Only the decoded ID is proposed by an enhanced image. Fit its
            # grid on the original pixels, check every bit/border cell there,
            # and reject excessive shifts or ambiguous repeated physical IDs.
            fitted, verified, detail = refine_bit_edges(
                gray, seed, marker_id, None, min_correlation=0.96, max_shift_px=24
            )
            if verified is None or not detail["applied"]:
                continue
            if existing is None:
                method = _preset_label(preset, monitor)
                accepted[marker_id] = {
                    "id": marker_id,
                    "corners_px": fitted.tolist(),
                    "center_px": diagonal_center(fitted).tolist(),
                    "method": method,
                    "refinement": detail,
                    "verified_passes": [method],
                    "validation": verified,
                }
            elif not _same_location(fitted, existing):
                del accepted[marker_id]
                ambiguous.add(marker_id)
        if targets <= accepted.keys():
            break
    # Credit a Monitor trial only when its extra pass changed what was read
    # on this same frame. A later frame can improve from visibility alone.
    if monitor and (
        accepted.keys() == {item["id"] for item in baseline["detections"]}
        and ambiguous == set(baseline["ambiguous_ids"])
    ):
        return {**baseline, "method": "Standard"}
    return {
        **baseline,
        "detections": [accepted[key] for key in sorted(accepted)],
        "ambiguous_ids": sorted(ambiguous),
        "method": _preset_label(preset, monitor),
    }
