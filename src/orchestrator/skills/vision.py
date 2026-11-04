"""Read-only calibration drift checks under revocable robot and vision authority."""

import math
import os
import re

from .calibrate import _vision
from .robot import _Transfer

DRIFT_SETTING = "MG400_ORCH_DRIFT_LIMIT_PX"
DRIFT_SETTINGS = {DRIFT_SETTING}
DRIFT_LIMIT_PX = 3.0
_PROOF_FIELDS = {
    "camera",
    "calibrated",
    "passed",
    "reason",
    "detected_center_px",
    "expected_center_px",
    "distance_px",
    "limit_px",
    "camera_device",
    "image_size",
    "model_created_at",
}


def _number(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _limit():
    """The setting may lower the 3 px limit but not raise it."""
    message = f"{DRIFT_SETTING} must be greater than 0 and at most 3 pixels."
    try:
        value = float(os.environ.get(DRIFT_SETTING, DRIFT_LIMIT_PX))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(message) from None
    if not 0 < value <= DRIFT_LIMIT_PX:
        raise ValueError(message)
    return value


def _positive_proof(result, camera, limit):
    if (
        not isinstance(result, dict)
        or set(result) != _PROOF_FIELDS
        or type(result.get("camera")) is not int
        or result["camera"] != camera
        or result.get("calibrated") is not True
        or result.get("passed") is not True
        or result.get("reason") is not None
        or not _number(result.get("limit_px"), 0, DRIFT_LIMIT_PX)
        or result["limit_px"] != limit
        or not _number(result.get("model_created_at"), 0, 1_000_000_000_000)
        or result["model_created_at"] == 0
        or not isinstance(result.get("camera_device"), str)
        or re.fullmatch(r"/dev/v4l/by-path/[A-Za-z0-9_.:+-]{1,255}", result["camera_device"])
        is None
        or result["camera_device"].rsplit("/", 1)[-1] in {".", ".."}
    ):
        return False
    size = result.get("image_size")
    if (
        not isinstance(size, dict)
        or set(size) != {"width", "height"}
        or any(type(size[name]) is not int or not 0 < size[name] <= 16_000_000 for name in size)
        or size["width"] * size["height"] > 16_000_000
    ):
        return False
    detected, expected = result.get("detected_center_px"), result.get("expected_center_px")
    if (
        not isinstance(detected, list)
        or len(detected) != 2
        or not isinstance(expected, list)
        or len(expected) != 2
        or any(not _number(value, 0, 1_000_000) for value in detected + expected)
        or not _number(result.get("distance_px"), 0, limit)
    ):
        return False
    # Recompute the distance; the reported one must agree with the reported centers.
    distance = math.dist(detected, expected)
    return (
        all(0 <= x < size["width"] and 0 <= y < size["height"] for x, y in (detected, expected))
        and distance <= limit
        and abs(distance - result["distance_px"]) <= 1e-9
    )


def check_calibration(ctx, camera: int):
    """Hold on negative evidence; never move or recover a robot to obtain it."""
    if type(camera) is not int or not 1 <= camera <= 4:
        raise ValueError("Drift camera must be from 1 to 4.")
    limit = _limit()
    robot = _Transfer(ctx)
    state, _ = robot.state()
    expected = list(state["actual"][:4])

    def stationary():
        current, _ = robot.state()
        if any(abs(value - target) > 0.1 for value, target in zip(current["actual"][:4], expected)):
            raise RuntimeError("Robot moved during calibration drift checking.")

    with ctx.guard(stationary):
        result = _vision(
            ctx, "check_calibration", camera=camera, flange_mm=expected, limit_px=limit
        )
    # Guard exit drains every accepted validator read. Preserve an earlier
    # cancellation or ownership cause before interpreting the returned proof.
    ctx.check()
    if not _positive_proof(result, camera, limit):
        raise RuntimeError("camera moved, recalibrate")
    return result
