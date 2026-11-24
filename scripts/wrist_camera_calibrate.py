"""Calibrate the lens and the mount of the wrist camera (camera5) with a printed board.

uv run python scripts/wrist_camera_calibrate.py board work/wrist-camera/board.pdf
uv run python scripts/wrist_camera_calibrate.py run --board-mm 264
uv run python scripts/wrist_camera_calibrate.py fit work/wrist-camera/run.json --board-mm 264

`run` moves the arm. Read the procedure in docs/vision.en.md first and watch the arm.
"""

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from labkit import ports
from vision import wrist_camera

SESSION = "wrist-calibration"
RUN_DATA = Path("work/wrist-camera/run.json")
# The motion box was checked for this start pose, with the board on the tray table.
START = (-39.0, -236.0, 163.8)
START_TOLERANCE_MM = 5.0
BOARD_DISTANCE_MM = (160.0, 190.0)
SPEED = 15
SETTLE_S = 1.2
# (z offset, x offsets, y offsets) from the start pose, in mm. All y offsets go away from
# the robot base: a point nearer to the base is outside the joint range at this height.
LEVELS = (
    (0.0, (-40, -20, 0, 20, 40, 60, 80), (0, -20, -40, -60, -80)),
    (-20.0, (-40, 0, 40), (0, -40, -80)),
    (-38.0, (-40, 0, 40), (0, -40)),
)


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


def pose():
    """Return the flange pose. The feedback can be empty for a moment after an enable."""
    for _ in range(20):
        state = robot("state")["state"]
        if state["alarms"]:
            raise RuntimeError(f"The controller reports an alarm: {state['alarms']}")
        if all(isinstance(value, (int, float)) for value in state["actual"]):
            return state["actual"]
        time.sleep(0.2)
    raise RuntimeError("The robot gives no position feedback")


def move(x, y, z, r):
    robot("move", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}", f"{r:.3f}", "--speed", str(SPEED))
    return pose()


def frame():
    url = ports.url("go2rtc", "api/frame.jpeg?src=camera5")
    with urllib.request.urlopen(url, timeout=8) as response:
        data = np.frombuffer(response.read(), np.uint8)
    gray = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.shape[::-1] != wrist_camera.IMAGE_SIZE:
        raise RuntimeError("camera5 must give 3840x2160 frames")
    return gray


def check_start(actual, corners, square_mm):
    """Refuse a start that the motion box was not checked for."""
    if max(abs(a - b) for a, b in zip(actual[:3], START)) > START_TOLERANCE_MM:
        raise RuntimeError(f"Park the arm at X {START[0]}, Y {START[1]}, Z {START[2]} first")
    columns = wrist_camera.SQUARES[0] - 1
    sides = [
        np.linalg.norm(np.subtract(corners[a], corners[a + 1]))
        for a in corners
        if (a + 1) % columns and a + 1 in corners
    ]
    if len(sides) < 20:
        raise RuntimeError("The camera does not see the board")
    distance = wrist_camera.NOMINAL_FOCAL_PX * square_mm / float(np.median(sides))
    if not BOARD_DISTANCE_MM[0] <= distance <= BOARD_DISTANCE_MM[1]:
        raise RuntimeError(f"The board is {distance:.0f} mm below the camera, not 160 to 190 mm")


def collect(square_mm):
    """Move over the grid and return a record for each pose."""
    robot("take")
    time.sleep(3)
    x0, y0, z0, r0 = pose()
    check_start((x0, y0, z0), wrist_camera.board_corners(frame()), square_mm)
    robot("enable")
    time.sleep(1)
    records = []
    try:
        for dz, xs, ys in LEVELS:
            for column, dx in enumerate(xs):
                for dy in ys if column % 2 == 0 else ys[::-1]:
                    actual = move(x0 + dx, y0 + dy, z0 + dz, r0)
                    time.sleep(SETTLE_S)
                    corners = wrist_camera.board_corners(frame())
                    records.append(
                        {"pose": actual, "board": {str(k): v for k, v in corners.items()}}
                    )
                    print(
                        f"{len(records):3d} pose {actual[0]:8.2f} {actual[1]:8.2f} {actual[2]:7.2f}"
                        f"  corners {len(corners)}",
                        flush=True,
                    )
            move(x0, y0, z0 + dz, r0)
    finally:
        now = pose()
        move(now[0], now[1], z0, r0)
        move(x0, y0, z0, r0)
        robot("release")
    return records


def fit(records, square_mm, out):
    result = wrist_camera.fit(records, square_mm)
    print(json.dumps({key: result[key] for key in ("rms_px", "max_px", "arm_radial_scale")}))
    if result["rms_px"] > wrist_camera.MAX_RMS_PX:
        raise RuntimeError("The fit error is above the limit. The calibration is not stored")
    out = Path(out)
    if out.exists():
        # The camera height above the tool tip comes from a touch, not from this fit.
        height = json.loads(out.read_text())["mount"].get("camera_above_tip_mm")
        if height is not None:
            result["mount"]["camera_above_tip_mm"] = height
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(f"Stored {out}")


def write_board(path):
    """Write the board as an A4 landscape PDF at 600 dpi. Print it at 100%."""
    from PIL import Image

    dpi, cell = 600, 567  # 24 mm at 600 dpi
    image = wrist_camera.board_image(cell)
    page = np.full((round(210 / 25.4 * dpi), round(297 / 25.4 * dpi)), 255, np.uint8)
    top, left = (page.shape[0] - image.shape[0]) // 2, (page.shape[1] - image.shape[1]) // 2
    page[top : top + image.shape[0], left : left + image.shape[1]] = image
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(page).save(path, resolution=dpi)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("board").add_argument("path", type=Path)
    for name in ("run", "fit"):
        command = commands.add_parser(name)
        if name == "fit":
            command.add_argument("data", type=Path)
        command.add_argument(
            "--board-mm",
            type=float,
            required=True,
            help="measured long side of the printed pattern, 264 at 100%%",
        )
        command.add_argument("--out", type=Path, default=wrist_camera.CONFIG)
    arguments = parser.parse_args()
    if arguments.command == "board":
        write_board(arguments.path)
        return
    square_mm = arguments.board_mm / wrist_camera.SQUARES[0]
    if arguments.command == "run":
        records = collect(square_mm)
        RUN_DATA.parent.mkdir(parents=True, exist_ok=True)
        RUN_DATA.write_text(json.dumps(records))
    else:
        records = json.loads(arguments.data.read_text())
    fit(records, square_mm, arguments.out)


if __name__ == "__main__":
    main()
