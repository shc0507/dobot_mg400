"""One guarded camera calibration scan; no device access or import-time I/O."""

import json
import math
import os
import re
import statistics
import tomllib
from contextlib import contextmanager
from pathlib import Path

from ..stations import _check, _parent
from ..tool import Pose, fingertip_to_flange, flange_to_fingertip
from .robot import _Transfer

REFERENCE_FILE = Path("/var/lib/mg400/calibration/cameras/references.toml")


@contextmanager
def _occupied_zone(ctx, name):
    entry = ctx.zones.declare(name)
    try:
        yield
    except BaseException:
        ctx.zones.finish(entry, exceptional=True)
        raise
    else:
        # The scan ends inside this zone. Returning is not proof of departure.
        ctx.zones.finish(entry)


def _references(path):
    """Read owner-taught flange contacts; these are data, never motion targets."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Tray references require an absolute host settings path.")
    with _parent(path) as (parent, name, owner):
        pinned = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(pinned)
            _check(info, owner)
            if info.st_nlink != 1 or not 0 < info.st_size <= 4096:
                raise ValueError("Tray references need a private file of at most 4 KiB.")
            descriptor = os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                raw = stream.read(4097)
        finally:
            os.close(pinned)
    if len(raw) > 4096:
        raise ValueError("Tray references exceed 4 KiB.")
    value = tomllib.loads(raw.decode("utf-8"))
    rows = value.get("references")
    if set(value) != {"references"} or not isinstance(rows, list) or not 2 <= len(rows) <= 4:
        raise ValueError("Configure 2 to 4 tray ArUco references.")
    references, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "flange_mm"}:
            raise ValueError("Each tray reference needs id and measured flange_mm [X,Y,Z,R].")
        identity, pose = row["id"], row["flange_mm"]
        if type(identity) is not int or identity not in {0, 1, 2, 7} or identity in seen:
            raise ValueError("Tray reference IDs must be distinct members of 0, 1, 2 and 7.")
        if not isinstance(pose, list) or len(pose) != 4:
            raise ValueError("Tray reference flange poses need X, Y, Z and R.")
        checked = Pose(*pose)
        seen.add(identity)
        references.append((identity, checked))
    return references


def _vision(ctx, action, **params):
    """Dispatch once, then observe only the exact owned completion receipt."""
    client = ctx.clients["vision"]
    ctx.check()
    before = client.state()
    if (
        not isinstance(before, dict)
        or not client.owned(before)
        or before.get("busy") is not False
        or before.get("locked") is not False
        or before.get("connected") is not True
        or "fault" not in before
        or before["fault"] is not None
    ):
        raise RuntimeError("Calibration requires owned, idle, unlocked vision state.")
    response = client.command(action, **params)
    identity = response.get("command_id") if isinstance(response, dict) else None
    if not isinstance(identity, str) or not identity or len(identity) > 128:
        raise RuntimeError("Vision command has an unknown outcome; do not repeat it.")
    while True:
        ctx.check()
        state = client.state()
        if (
            not isinstance(state, dict)
            or not client.owned(state)
            or state.get("locked") is not False
        ):
            raise RuntimeError("Vision control was lost during calibration.")
        receipt = state.get("last_command")
        if isinstance(receipt, dict) and receipt.get("id") == identity:
            if (
                receipt.get("action") != action
                or receipt.get("client") != state.get("lease", {}).get("holder")
                or receipt.get("result") != "ok"
                or state.get("busy") is not False
                or state.get("connected") is not True
                or "fault" not in state
                or state["fault"] is not None
                or not isinstance(receipt.get("output"), dict)
            ):
                raise RuntimeError("Vision calibration failed: " + str(receipt.get("message", "")))
            return receipt["output"]
        if state.get("busy") is not True:
            raise RuntimeError("Vision command outcome is unknown; it will not be retried.")
        ctx.sleep(0.02)


class _Acquisition:
    def __init__(self, ctx, robot, camera):
        self.ctx, self.robot, self.camera = ctx, robot, camera
        self.device = self.size = None
        self.last_frame = None

    def stationary(self, expected):
        state, _ = self.robot.state()
        actual = state["actual"][:4]
        if any(abs(measured - target) > 0.1 for measured, target in zip(actual, expected)):
            raise RuntimeError("Robot moved outside the stationary calibration pose.")
        return actual

    def sample(self, expected, marker_ids):
        samples = {identity: [] for identity in marker_ids}
        hashes = set()
        with self.ctx.guard(lambda: self.stationary(expected)):
            for index in range(5):
                self.stationary(expected)
                result = _vision(self.ctx, "read_markers", camera=self.camera)
                self.stationary(expected)
                dimensions = result.get("image_size", {})
                size = (dimensions.get("width"), dimensions.get("height"))
                device = result.get("camera_device")
                fingerprint = result.get("frame_sha256")
                if (
                    result.get("camera") != self.camera
                    or not isinstance(device, str)
                    or re.fullmatch(r"/dev/v4l/by-path/[A-Za-z0-9_.:+-]{1,255}", device) is None
                    or any(type(value) is not int or value <= 0 for value in size)
                    or size[0] * size[1] > 16_000_000
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    or fingerprint in hashes
                    or fingerprint == self.last_frame
                ):
                    raise RuntimeError("Calibration requires 5 distinct bounded camera frames.")
                if self.device is None:
                    self.device, self.size = device, size
                elif self.device != device or self.size != size:
                    raise RuntimeError("Camera port or image size changed during calibration.")
                hashes.add(fingerprint)
                self.last_frame = fingerprint
                markers = result.get("markers")
                if not isinstance(markers, list) or len(markers) > 50:
                    raise RuntimeError("Invalid ArUco marker response.")
                for identity in marker_ids:
                    matches = [
                        item
                        for item in markers
                        if isinstance(item, dict) and item.get("id") == identity
                    ]
                    if len(matches) != 1:
                        raise RuntimeError(f"Frame must contain exactly one ArUco ID {identity}.")
                    center = matches[0].get("center_px")
                    if (
                        not isinstance(center, list)
                        or len(center) != 2
                        or any(
                            type(value) not in (int, float)
                            or not math.isfinite(value)
                            or not 0 <= value < bound
                            for value, bound in zip(center, size)
                        )
                    ):
                        raise RuntimeError("ArUco center is outside the camera image.")
                    samples[identity].append(center)
                if index < 4:
                    self.ctx.sleep(0.05)
        return {
            identity: [statistics.median(point[axis] for point in points) for axis in (0, 1)]
            for identity, points in samples.items()
        }


def calibration_skill(reference_provider):
    """Bind only demo reference data without mutating process-wide settings."""
    if not callable(reference_provider):
        raise ValueError("A demo calibration reference provider must be callable.")

    def demo_calibrate(
        ctx,
        camera: int,
        corner_a: str,
        corner_b: str,
        low_flange_z_mm: float,
        high_flange_z_mm: float,
    ):
        reference_file = reference_provider()
        if reference_file is None:
            raise ValueError("The owned demo calibration references are unavailable.")
        return _calibrate(
            ctx, camera, corner_a, corner_b, low_flange_z_mm, high_flange_z_mm, reference_file
        )

    return demo_calibrate


def calibrate(
    ctx,
    camera: int,
    corner_a: str,
    corner_b: str,
    low_flange_z_mm: float,
    high_flange_z_mm: float,
):
    return _calibrate(
        ctx,
        camera,
        corner_a,
        corner_b,
        low_flange_z_mm,
        high_flange_z_mm,
        os.environ.get("MG400_CAMERA_CALIBRATION_REFERENCES") or REFERENCE_FILE,
    )


def _calibrate(ctx, camera, corner_a, corner_b, low_flange_z_mm, high_flange_z_mm, reference_file):
    """Collect a 3 by 4 grid at 2 heights, then fit and publish through vision."""
    if type(camera) is not int or not 1 <= camera <= 4:
        raise ValueError("Calibration camera must be from 1 to 4.")
    if (
        type(low_flange_z_mm) not in (int, float)
        or type(high_flange_z_mm) not in (int, float)
        or not math.isfinite(low_flange_z_mm)
        or not math.isfinite(high_flange_z_mm)
        or high_flange_z_mm - low_flange_z_mm < 40
    ):
        raise ValueError("The 2 flange heights must be finite and at least 40 mm apart.")
    references = _references(reference_file)
    robot = _Transfer(ctx)
    if corner_a == corner_b or corner_a not in robot.stations or corner_b not in robot.stations:
        raise ValueError("Choose 2 distinct taught grid-corner stations.")
    first, second = robot.stations[corner_a], robot.stations[corner_b]
    if not first["zone"] or first["zone"] != second["zone"]:
        raise ValueError("Both grid corners must belong to the same explicit calibration zone.")
    zone = ctx.zone_definitions().get(first["zone"])
    if not isinstance(zone, dict) or zone.get("owner") != "robot":
        raise ValueError("The calibration grid zone must be configured for the robot.")
    a = fingertip_to_flange(robot.pose(first), robot.offset)
    b = fingertip_to_flange(robot.pose(second), robot.offset)
    if abs(a.x_mm - b.x_mm) < 1 or abs(a.y_mm - b.y_mm) < 1 or abs(a.r_deg - b.r_deg) > 0.1:
        raise ValueError("Grid corners need distinct X and Y and the same taught R.")
    poses = [
        Pose(
            a.x_mm + (b.x_mm - a.x_mm) * column / 3,
            a.y_mm + (b.y_mm - a.y_mm) * row / 2,
            height,
            a.r_deg,
        )
        for height in (low_flange_z_mm, high_flange_z_mm)
        for row in range(3)
        for column in range(4)
    ]
    for pose in poses:
        ctx.check()
        robot._inside(flange_to_fingertip(pose, robot.offset))
    tray_points = []
    for _, contact in references:
        corrected = flange_to_fingertip(contact, robot.offset)
        # Measured dx/dy rotates with this contact's R. Leave Z as flange Z:
        # the joint camera/D fit cancels the nominal tag height and tool dz.
        tray_points.append([corrected.x_mm, corrected.y_mm, contact.z_mm])
    acquisition = _Acquisition(ctx, robot, camera)
    initial = [poses[0].x_mm, poses[0].y_mm, poses[0].z_mm, poses[0].r_deg]
    # Entry routing is deliberately explicit: the caller reaches the first
    # pose through its taught safe route before invoking this scan.
    acquisition.stationary(initial)
    tag_points, tag_pixels = [], []
    with _occupied_zone(ctx, first["zone"]):
        for index, pose in enumerate(poses):
            if index:
                robot.move(flange_to_fingertip(pose, robot.offset))
            expected = [pose.x_mm, pose.y_mm, pose.z_mm, pose.r_deg]
            centers = acquisition.sample(expected, [4])
            measured = acquisition.stationary(expected)
            tag_points.append(measured[:3])
            tag_pixels.append(centers[4])
            ctx.store.event("camera_calibration_point", {"camera": camera, "point": index + 1})
        tray = acquisition.sample(expected, [identity for identity, _ in references])
        with ctx.guard(lambda: acquisition.stationary(expected)):
            result = _vision(
                ctx,
                "calibrate_camera",
                camera=camera,
                tag_points=tag_points,
                tag_pixels=tag_pixels,
                tray_points=tray_points,
                tray_pixels=[tray[identity] for identity, _ in references],
                image_size=list(acquisition.size),
                scan_r_deg=a.r_deg,
            )
    model = result.get("calibration")
    if (
        result.get("camera") != camera
        or result.get("calibrated") is not True
        or not isinstance(model, dict)
        or model.get("camera_device") != acquisition.device
        or model.get("image_size") != list(acquisition.size)
    ):
        raise RuntimeError("Calibration result does not match the sampled camera.")
    artifact = ctx.store.artifact(
        f"camera-{camera}-calibration.json",
        json.dumps(
            {
                **model,
                "reference_provenance": {
                    "flange_poses_mm": [
                        [pose.x_mm, pose.y_mm, pose.z_mm, pose.r_deg] for _, pose in references
                    ],
                    "marker_ids": [identity for identity, _ in references],
                    "tool_offset_mm": [robot.offset.dx_mm, robot.offset.dy_mm, robot.offset.dz_mm],
                },
            },
            allow_nan=False,
        ).encode("utf-8"),
    )
    return {
        "camera": camera,
        "artifact_id": artifact,
        "rms_mm": model["rms_mm"],
        "distance_mm": model["distance_mm"],
    }
