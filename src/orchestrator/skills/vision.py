"""Read-only calibration drift checks under revocable robot and vision authority."""

import json
import math
import os
import re
import tomllib
from pathlib import Path

from .calibrate import _vision
from .robot import _opening, _RouteZones, _Transfer

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
    return _check_calibration(ctx, camera, _Transfer(ctx))


def _check_calibration(ctx, camera, robot):
    limit = _limit()
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


class _TrayInputs:
    """Explicit physical inputs, pinned once per guarded transfer."""

    def __init__(self, robot, slots):
        def setting(name):
            key = "MG400_ORCH_" + name
            value = os.environ.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Set the explicit tray-transfer input {key}.")
            return value

        def integer(name, low, high):
            value = setting(name)
            if not value.isascii() or not value.isdecimal() or not low <= int(value) <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}.")
            return int(value)

        def number(name):
            try:
                value = float(setting(name))
            except (ValueError, OverflowError):
                raise ValueError(f"{name} must be explicitly finite.") from None
            if not math.isfinite(value) or abs(value) > 1_000_000:
                raise ValueError(f"{name} must be explicitly finite and bounded.")
            return value

        self.camera = integer("TRAY_CAMERA", 1, 4)
        self.grip = integer("TRAY_GRIP", 0, 1000)
        self.release = integer("TRAY_RELEASE", 0, 1000)
        self.opening = _opening(self.grip)
        if self.release <= self.grip:
            raise ValueError("Tray release must exceed the grasp target.")
        self.axis = setting("CHIP_AXIS")
        if self.axis not in {"long", "short"}:
            raise ValueError("CHIP_AXIS must explicitly select long or short.")
        self.offset = number("CHIP_R_OFFSET_DEG")
        self.minimum, self.maximum = number("CHIP_R_MIN_DEG"), number("CHIP_R_MAX_DEG")
        if not 0 < self.maximum - self.minimum < 180:
            raise ValueError(
                "The taught chip-R interval must be nonempty and narrower than 180 degrees."
            )
        with Path(setting("TRAY_FILE")).open("rb") as stream:
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValueError("Tray model exceeds the command's 16 KiB bound.")
        self.tray = tomllib.loads(raw.decode("utf-8"))
        # The service performs full geometric model validation before returning
        # any observation. Locally reject missing/ambiguous exact identifiers.
        entries = self.tray.get("slots")
        if (
            not isinstance(entries, list)
            or not entries
            or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in entries
            )
        ):
            raise ValueError("Tray model requires exact slot IDs.")
        ids = [item["id"] for item in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Tray slot IDs are ambiguous.")
        self.targets = {}
        for slot in slots:
            if not isinstance(slot, str) or slot not in ids or slot not in robot.stations:
                raise ValueError("Each requested slot needs its exact explicitly taught station.")
            station = robot.stations[slot]
            self.targets[slot] = [station["x_mm"], station["y_mm"]]
        self.args = {
            "camera": self.camera,
            "tray": self.tray,
            "targets": self.targets,
            "axis": self.axis,
        }
        encoded = json.dumps({"action": "observe_slots", **self.args}, allow_nan=False).encode()
        if len(encoded) > 16 * 1024:
            raise ValueError("Complete slot observation request exceeds 16 KiB.")

    def flange_r(self, angle):
        base = angle + self.offset
        lower = math.ceil((self.minimum - base) / 180)
        upper = math.floor((self.maximum - base) / 180)
        if lower != upper:
            raise RuntimeError("Chip axis has no unique taught flange-R equivalent.")
        value = base + lower * 180
        if not self.minimum <= value <= self.maximum or not math.isfinite(value):
            raise RuntimeError("Chip flange R is outside its explicit interval.")
        return value


def _observe(ctx, robot, inputs, baseline):
    state, _ = robot.state()
    pose = list(state["actual"][:4])

    def stationary():
        current, _ = robot.state()
        if any(abs(a - b) > 0.1 for a, b in zip(current["actual"][:4], pose)):
            raise RuntimeError("Robot moved during slot observation.")

    with ctx.guard(stationary):
        result = _vision(ctx, "observe_slots", **inputs.args)
    ctx.check()
    fields = {
        "camera",
        "calibrated",
        "camera_device",
        "image_size",
        "model_created_at",
        "frame_sha256",
        "read_at",
        "axis",
        "slots",
    }
    if (
        not isinstance(result, dict)
        or set(result) != fields
        or result.get("calibrated") is not True
        or type(result.get("camera")) is not int
        or result["camera"] != inputs.camera
        or result.get("axis") != inputs.axis
        or any(
            result.get(key) != baseline.get(key)
            for key in ("camera_device", "image_size", "model_created_at")
        )
        or not isinstance(result.get("frame_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", result["frame_sha256"]) is None
        or not _number(result.get("read_at"), 0, 1_000_000_000_000)
        or result["read_at"] <= 0
        or result["read_at"] < baseline.get("read_at", 0)
        or not isinstance(result.get("slots"), dict)
        or set(result["slots"]) != set(inputs.targets)
    ):
        raise RuntimeError("Slot observation lacks fresh matching calibrated evidence.")
    for item in result["slots"].values():
        if (
            not isinstance(item, dict)
            or set(item) != {"state", "center_xy_mm", "angle_deg", "reason"}
            or item.get("reason") is not None
        ):
            raise RuntimeError("Slot observation is unknown or malformed.")
        if item["state"] == "empty":
            if item["center_xy_mm"] is not None or item["angle_deg"] is not None:
                raise RuntimeError("Empty slot evidence is inconsistent.")
        elif item["state"] == "present":
            point = item["center_xy_mm"]
            if (
                not isinstance(point, list)
                or len(point) != 2
                or any(not _number(value, -1_000_000, 1_000_000) for value in point)
                or not _number(item["angle_deg"], 0, 180)
                or item["angle_deg"] == 180
            ):
                raise RuntimeError("Chip geometry is missing or ambiguous.")
        else:
            raise RuntimeError("Slot observation is unknown.")
    return result


def slot_state(ctx, slot: str):
    """Return explicit present/empty evidence; missing evidence holds the step."""
    robot = _Transfer(ctx)
    inputs = _TrayInputs(robot, [slot])
    baseline = _check_calibration(ctx, inputs.camera, robot)
    return _observe(ctx, robot, inputs, baseline)["slots"][slot]


def find_chip(ctx, slot: str):
    result = slot_state(ctx, slot)
    if result["state"] != "present":
        raise RuntimeError("No chip is present in the requested source slot.")
    return result


def tray_transfer(ctx, from_slot: str, to_slot: str, *, speed: int = 1):
    """Keep current vision evidence local to one guarded transfer step."""
    if from_slot == to_slot:
        raise ValueError("Source and target slots must be different.")
    robot = _Transfer(ctx, speed)
    inputs = _TrayInputs(robot, [from_slot, to_slot])
    # Check every physical input and the speed cap before accepting image evidence.
    robot.check_speed()
    baseline = _check_calibration(ctx, inputs.camera, robot)
    before = _observe(ctx, robot, inputs, baseline)
    source, target = before["slots"][from_slot], before["slots"][to_slot]
    if source["state"] != "present":
        raise RuntimeError("No chip is present in the source slot.")
    if target["state"] != "empty":
        raise RuntimeError("The target slot is occupied.")
    route = robot.measured_source(
        from_slot,
        x_mm=source["center_xy_mm"][0],
        y_mm=source["center_xy_mm"][1],
        r_deg=inputs.flange_r(source["angle_deg"]),
    )
    source_route, target_route = robot.plan(route, robot.route(to_slot))
    with _RouteZones(ctx.zones) as zones:
        robot.pick(source_route, inputs.grip, inputs.opening, zones)
        robot.place(target_route, inputs.release, zones)
    after = _observe(ctx, robot, inputs, before)
    if after["slots"][to_slot]["state"] != "present":
        raise RuntimeError("The target chip was not confirmed after placement.")
    if after["slots"][from_slot]["state"] != "empty":
        raise RuntimeError("A chip remains in the source slot after placement.")
    return {"from_slot": from_slot, "to_slot": to_slot, "before": before, "after": after}
