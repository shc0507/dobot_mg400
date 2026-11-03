"""Read-only calibration drift checks under revocable robot and vision authority."""

import math
import os
import re

from .calibrate import _vision
from .robot import _Transfer

DRIFT_SETTING = "MG400_ORCH_DRIFT_MARGIN_PX"
DRIFT_SETTINGS = {DRIFT_SETTING}
_PROOF_FIELDS = {
    "camera",
    "calibrated",
    "passed",
    "reason",
    "projected_tip_px",
    "arm_box_px",
    "margin_px",
    "camera_device",
    "image_size",
    "model_created_at",
}


def _number(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _margin():
    raw = os.environ.get(DRIFT_SETTING, "0")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{DRIFT_SETTING} must be a number from 0 to 4096 pixels.") from None
    if not _number(value, 0, 4096):
        raise ValueError(f"{DRIFT_SETTING} must be a number from 0 to 4096 pixels.")
    return value


def _positive_proof(result, camera, margin):
    if (
        not isinstance(result, dict)
        or set(result) != _PROOF_FIELDS
        or type(result.get("camera")) is not int
        or result["camera"] != camera
        or result.get("calibrated") is not True
        or result.get("passed") is not True
        or result.get("reason") is not None
        or not _number(result.get("margin_px"), 0, 4096)
        or result["margin_px"] != margin
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
    point, box = result.get("projected_tip_px"), result.get("arm_box_px")
    if (
        not isinstance(point, list)
        or len(point) != 2
        or not isinstance(box, list)
        or len(box) != 4
        or any(not _number(value, 0, 1_000_000) for value in point + box)
    ):
        return False
    x, y = point
    left, top, right, bottom = box
    return (
        0 <= x < size["width"]
        and 0 <= y < size["height"]
        and 0 <= left < right <= size["width"]
        and 0 <= top < bottom <= size["height"]
        and left + margin < right - margin
        and top + margin < bottom - margin
        and left + margin <= x <= right - margin
        and top + margin <= y <= bottom - margin
    )


def check_calibration(ctx, camera: int):
    """Hold on negative evidence; never move or recover a robot to obtain it."""
    if type(camera) is not int or not 1 <= camera <= 4:
        raise ValueError("Drift camera must be from 1 to 4.")
    margin = _margin()
    robot = _Transfer(ctx)
    state, _ = robot.state()
    expected = list(state["actual"][:4])

    def stationary():
        current, _ = robot.state()
        if any(abs(value - target) > 0.1 for value, target in zip(current["actual"][:4], expected)):
            raise RuntimeError("Robot moved during calibration drift checking.")

    with ctx.guard(stationary):
        result = _vision(
            ctx,
            "check_calibration",
            camera=camera,
            flange_mm=expected,
            tool_offset_mm=[robot.offset.dx_mm, robot.offset.dy_mm, robot.offset.dz_mm],
            margin_px=margin,
        )
    # Guard exit drains every accepted validator read. Preserve an earlier
    # cancellation or ownership cause before interpreting the returned proof.
    ctx.check()
    if not _positive_proof(result, camera, margin):
        raise RuntimeError("camera moved, recalibrate")
    return result
