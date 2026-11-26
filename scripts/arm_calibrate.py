"""Measure the errors of the arm with the wrist camera (camera5) and the holes of the table.

uv run python scripts/arm_calibrate.py run work/arm-calibration/run-1
uv run python scripts/arm_calibrate.py fit work/arm-calibration/run-1/frames.json

`run` moves the arm and makes no touch. Read the procedure in docs/vision.en.md first.
`fit` prints the ArmError for `src/mg400/kinematics.py` and the errors of the views.
"""

import argparse
import json
import math
import subprocess
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from labkit import ports
from mg400 import joint_limits, kinematics
from vision import arm_calibration, wrist_camera

SESSION = "arm-calibration"
# The moves were checked in the bench twin for this start pose (the tray look pose).
START = (-39.0, -236.0, 163.8)
START_TOLERANCE_MM = 5.0
SPEED = 10
SETTLE_S = 1.5
# Views over the tray sector, with the tool rotation of the start: J1 (deg), reach (mm) and
# flange Z (mm). At the lowest Z the tip of the cup is 28 mm above the tray plate.
VIEW_J1 = (-99.0, -91.0, -83.0)
VIEW_REACH = (236.0, 256.0, 276.0, 296.0, 316.0)
VIEW_Z = (163.8, 135.0, 107.0, 78.0, 50.0)
# Flange Z at which the tool tip touches the table. The fit corrects a small error.
TABLE_TOUCH_Z = -189.0
MIN_HOLES_AT_START = 10


def labctl(*arguments):
    command = ["sudo", "-n", "labctl", *arguments, "--session", SESSION]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": {"message": (result.stdout + result.stderr)[-300:]}}


def robot(*arguments):
    reply = labctl("robot", *arguments)
    if not reply.get("ok"):
        message = (reply.get("error") or {}).get("message")
        raise RuntimeError(f"robot {' '.join(arguments)}: {message}")
    return reply


def state():
    """Return the robot state. The feedback can be empty for a moment after an enable."""
    for _ in range(20):
        now = robot("state")["state"]
        if now["alarms"]:
            raise RuntimeError(f"The controller reports an alarm: {now['alarms']}")
        if all(isinstance(value, (int, float)) for value in now["actual"]):
            return now
        time.sleep(0.2)
    raise RuntimeError("The robot gives no position feedback")


def move(x, y, z, r):
    robot("move", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}", f"{r:.3f}", "--speed", str(SPEED))
    return state()


def frame():
    url = ports.url("go2rtc", "api/frame.jpeg?src=camera5")
    with urllib.request.urlopen(url, timeout=8) as response:
        data = response.read()
    gray = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.shape[::-1] != wrist_camera.IMAGE_SIZE:
        raise RuntimeError("camera5 must give 3840x2160 frames")
    return data, gray


def view_poses(r):
    """View poses with the tool rotation r, ordered so that each move is short."""
    poses = []
    for column, j1 in enumerate(VIEW_J1):
        angle = math.radians(j1)
        for reach in VIEW_REACH if column % 2 == 0 else VIEW_REACH[::-1]:
            heights = []
            for z in VIEW_Z:
                try:
                    j2, j3 = kinematics.joints_from_plane(reach, z)
                    joint_limits.validate_joints((j1, j2, j3, r - j1))
                except ValueError:
                    continue
                heights.append(z)
            # Start each column at the end that is nearer to the last height.
            if (
                poses
                and heights
                and abs(heights[-1] - poses[-1][2]) < abs(heights[0] - poses[-1][2])
            ):
                heights.reverse()
            poses += [(reach * math.cos(angle), reach * math.sin(angle), z, r) for z in heights]
    return poses


def check_start(actual):
    """Refuse a start that the moves were not checked for."""
    if max(abs(a - b) for a, b in zip(actual[:3], START)) > START_TOLERANCE_MM:
        raise RuntimeError(f"Park the arm at X {START[0]}, Y {START[1]}, Z {START[2]} first")


def collect(out):
    """Move over the views and store a still and the reported pose of each."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    robot("take")
    time.sleep(3)
    start = state()["actual"]
    check_start(start)
    seen = len(arm_calibration.find_holes(frame()[1]))
    if seen < MIN_HOLES_AT_START:
        robot("release")
        raise RuntimeError(f"The camera sees {seen} holes of the table, not {MIN_HOLES_AT_START}")
    robot("enable")
    time.sleep(1)
    records = []
    try:
        for target in view_poses(start[3]):
            now = move(*target)
            time.sleep(SETTLE_S)
            data, gray = frame()
            name = f"view{len(records):03d}.jpg"
            (out / name).write_bytes(data)
            holes = arm_calibration.find_holes(gray)
            records.append(
                {
                    "pose": now["actual"],
                    "joints": now["joints"],
                    "image": name,
                    "holes": holes.tolist(),
                }
            )
            (out / "frames.json").write_text(json.dumps(records))
            print(
                f"{len(records):3d} pose {now['actual'][0]:8.2f} {now['actual'][1]:8.2f}"
                f" {now['actual'][2]:7.2f}  holes {len(holes)}",
                flush=True,
            )
        now = state()["actual"]
        move(now[0], now[1], START[2], now[3])
        move(*start[:2], START[2], start[3])
    finally:
        # After a failure the arm stays where it is. The release disables it.
        robot("release")
    return records


def bands(records, shifts):
    """Mean camera shift (reach, sideways, height) of the views in bands of reach and of Z."""
    poses = np.array([record["pose"] for record in records], float)
    reach, shifts = np.hypot(poses[:, 0], poses[:, 1]), np.asarray(shifts, float)
    rows = []
    for name, values, edges in (
        ("reach", reach, (0, 250, 280, 310, 340, 1000)),
        ("z", poses[:, 2], (-1000, -30, 10, 50, 90, 130, 1000)),
    ):
        for low, high in zip(edges, edges[1:]):
            inside = (values >= low) & (values < high) & np.isfinite(shifts[:, 0])
            if inside.any():
                mean = np.round(shifts[inside].mean(0), 3).tolist()
                rows.append(
                    {"band": f"{name} {low} to {high}", "views": int(inside.sum()), "mm": mean}
                )
    return rows


def left_out(records, terms):
    """Smallest and largest value of each term when the views of 1 J1 direction are left out.

    A term whose values differ by much more than its sigma is not known from these views.
    """
    j1 = [round(math.degrees(math.atan2(r["pose"][1], r["pose"][0]))) for r in records]
    values = {name: [] for name in terms}
    for group in sorted(set(j1)) if len(set(j1)) >= 3 else ():
        rest = [record for record, angle in zip(records, j1) if angle != group]
        try:
            error = arm_calibration.fit(wrist_camera.load(), rest, TABLE_TOUCH_Z, terms)["error"]
        except ValueError:
            continue
        for name in terms:
            values[name].append(getattr(error, name))
    return {name: [min(found), max(found)] for name, found in values.items() if found}


def fit(path, terms):
    records = json.loads(Path(path).read_text())
    for record in records:
        if "holes" not in record:
            gray = cv2.imread(str(Path(path).parent / record["image"]), cv2.IMREAD_GRAYSCALE)
            record["holes"] = arm_calibration.find_holes(gray).tolist()
    result = arm_calibration.fit(wrist_camera.load(), records, TABLE_TOUCH_Z, terms)
    for key in ("before", "after"):
        result[key]["bands"] = bands(records, result[key].pop("view_shift_mm"))
    result["left_out"] = left_out(records, terms)
    error = result.pop("error")
    result["error"] = error._asdict()
    print(json.dumps(result, indent=1))
    values = ", ".join(f"{name}={value:.4g}" for name, value in error._asdict().items() if value)
    print(f"ERROR = ArmError({values})")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("out")
    fit_parser = commands.add_parser("fit")
    fit_parser.add_argument("frames")
    fit_parser.add_argument("--terms", default="j3_zero_deg,rear_arm_mm")
    arguments = parser.parse_args()
    if arguments.command == "run":
        collect(arguments.out)
    else:
        fit(arguments.frames, tuple(arguments.terms.split(",")))


if __name__ == "__main__":
    main()
