"""Errors of the arm from wrist camera views of the hole grid of the optical table.

The table has threaded holes at a pitch of 25.4 mm. The grid is the length reference: the
run needs no board, no touch and no other measurement. The wrist camera is on a link that
stays level and turns with J1, so a view gives the place of the camera against the grid
without the arm. Hole (i, j) is at

    G = g + Rz(yaw) [25.4 i, 25.4 j, 0] + [0, 0, tilt . (25.4 i, 25.4 j)]

in real arm coordinates. A view at the real flange place T sees it at

    X_c = R_cl (Rz(-J1) (G - T) - o)

T is `mg400.kinematics.true_pose` of the reported pose. The fit gives the ArmError, the
grid, the camera place o and a small turn of R_cl. The lens stays as stored.

Not in the result: a constant error of the reach (o hides it), the J1 zero, and the place
of the cup. The height error comes from the lens height above the table only.
"""

import math

import cv2
import numpy as np

from mg400 import kinematics
from mg400.kinematics import ArmError

from . import wrist_camera

PITCH_MM = 25.4
TERMS = ("j2_zero_deg", "j3_zero_deg", "rear_arm_mm", "forearm_mm")
HOLE_PX = (14.0, 90.0)  # diameter of a hole in a 3840x2160 frame, lens 135 to 600 mm above
ROUND_LIMIT = 0.12  # spread of the rim radius of a hole, as a part of the radius
MATCH_MM = 4.0
REMATCH_PX = 6.0
OUTLIER_SIGMAS = 4.0
MIN_VIEWS, MIN_HOLES = 12, 8


def _rim(blur, centre, sizes):
    """Points of the steepest rise of brightness on rays from the centre of a dark blob."""
    points = []
    reach = np.arange(0.25 * sizes[0], 0.8 * sizes[1], 0.25)
    for angle in np.linspace(0, 2 * np.pi, 48, endpoint=False):
        xs = (centre[0] + reach * np.cos(angle)).astype(np.float32)
        ys = (centre[1] + reach * np.sin(angle)).astype(np.float32)
        if (
            xs.min() < 1
            or ys.min() < 1
            or xs.max() > blur.shape[1] - 2
            or ys.max() > blur.shape[0] - 2
        ):
            return []
        rise = np.gradient(
            cv2.remap(blur, xs[None], ys[None], cv2.INTER_LINEAR).ravel().astype(float)
        )
        k = int(np.argmax(rise))
        if 0 < k < len(reach) - 1 and rise[k] > 3:
            curve = rise[k - 1] - 2 * rise[k] + rise[k + 1]
            part = 0.5 * (rise[k - 1] - rise[k + 1]) / curve if curve else 0.0
            at = reach[k] + 0.25 * part
            points.append([centre[0] + at * np.cos(angle), centre[1] + at * np.sin(angle)])
    return points


def find_holes(gray):
    """Return the pixel centres of dark round holes in a bright surface, as rows (u, v)."""
    blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
    dark = (blur.astype(np.int16) - cv2.blur(blur, (101, 101)) < -35).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    holes = []
    for contour in contours:
        area, border = cv2.contourArea(contour), cv2.arcLength(contour, True)
        if len(contour) < 20 or 4 * np.pi * area < 0.8 * border**2:
            continue
        (u, v), (a, b), angle = cv2.fitEllipse(contour)
        small, big = min(a, b), max(a, b)
        if not HOLE_PX[0] <= big <= HOLE_PX[1] or small < 0.6 * big:
            continue
        if abs(area - np.pi * a * b / 4) > 0.12 * area:
            continue
        # The surface around a hole is bright: compare a ring at 1.7 radii with the middle.
        turn = np.linspace(0, 2 * np.pi, 32, endpoint=False)
        cos, sin = np.cos(np.radians(angle)), np.sin(np.radians(angle))
        ex, ey = 0.85 * a * np.cos(turn), 0.85 * b * np.sin(turn)
        xs, ys = (u + cos * ex - sin * ey).astype(int), (v + sin * ex + cos * ey).astype(int)
        inside = (xs >= 0) & (ys >= 0) & (xs < gray.shape[1]) & (ys < gray.shape[0])
        if inside.sum() < 24:
            continue
        if np.percentile(blur[ys[inside], xs[inside]], 25) - float(blur[int(v), int(u)]) < 40:
            continue
        rim = _rim(blur, (u, v), (small, big))
        if len(rim) < 36:
            continue
        rim = np.array(rim, np.float32)
        (u2, v2), (a2, b2), angle = cv2.fitEllipse(rim)
        cos, sin = np.cos(np.radians(angle)), np.sin(np.radians(angle))
        dx, dy = rim[:, 0] - u2, rim[:, 1] - v2
        radius = np.hypot((cos * dx + sin * dy) / (a2 / 2), (-sin * dx + cos * dy) / (b2 / 2))
        # The rim of a hole is an ellipse. A light patch in the hole can bend it a little.
        if math.hypot(u2 - u, v2 - v) < 0.25 * big and np.std(radius) < ROUND_LIMIT:
            holes.append((u2, v2))
    return np.array(holes, float).reshape(-1, 2)


def _lattice(points):
    """Yaw and origin of the 25.4 mm grid that the points fit best."""
    best = None
    for yaw in np.radians(np.arange(-45.0, 45.0, 0.05)):
        cos, sin = math.cos(yaw), math.sin(yaw)
        turned = np.column_stack(
            [cos * points[:, 0] + sin * points[:, 1], -sin * points[:, 0] + cos * points[:, 1]]
        )
        phase = np.exp(2j * np.pi * turned / PITCH_MM).mean(0)
        score = abs(phase[0]) + abs(phase[1])
        if best is None or score > best[0]:
            best = (score, yaw, np.angle(phase) / (2 * np.pi) * PITCH_MM)
    return best[1], best[2]


def match_grid(model, views, table_touch_z):
    """Give each seen hole its grid place.

    views: [{"pose": [x, y, z, r], "holes": [[u, v], ...]}]. table_touch_z is the flange z
    at which the tool tip touches the table, within some millimetres. Returns the view
    number, the normalised pixel and the grid place (i, j) of each blob, a mark for the
    blobs that are on the grid, and the yaw and the origin of the grid in arm coordinates.
    """
    matrix, distortion = model["camera_matrix"], model["distortion"]
    view, normal, place = [], [], []
    for number, record in enumerate(views):
        pixels = np.asarray(record["holes"], float).reshape(-1, 2)
        if len(pixels):
            view += [number] * len(pixels)
            normal.append(
                cv2.undistortPoints(pixels.reshape(-1, 1, 2), matrix, distortion).reshape(-1, 2)
            )
            place.append(wrist_camera.surface_points(model, pixels, record["pose"], table_touch_z))
    if not view:
        raise ValueError("The views show no holes")
    view, normal, place = np.array(view), np.vstack(normal), np.vstack(place)
    yaw, origin = _lattice(place)
    cos, sin = math.cos(yaw), math.sin(yaw)
    turned = np.column_stack(
        [cos * place[:, 0] + sin * place[:, 1], -sin * place[:, 0] + cos * place[:, 1]]
    )
    grid = np.round((turned - origin) / PITCH_MM)
    keep = np.linalg.norm(turned - origin - grid * PITCH_MM, axis=1) < MATCH_MM
    start = np.array([cos * origin[0] - sin * origin[1], sin * origin[0] + cos * origin[1]])
    return view, normal, grid, keep, yaw, start


def _predictor(model, poses, terms):
    """Return predict(x, view, grid): the place in the picture of grid holes, in focal pixels."""
    rotation = cv2.Rodrigues(np.array(model["mount"]["rotation_link_to_camera"], float))[0].ravel()
    focal = float(model["camera_matrix"][0][0])
    angles = np.arctan2(poses[:, 1], poses[:, 0])
    count = len(terms)

    def predict(x, view, grid):
        arm = ArmError(**dict(zip(terms, x[:count])))
        flange = np.array([kinematics.true_pose(pose, arm)[:3] for pose in poses])[view]
        gx, gy, gz, yaw, tilt_x, tilt_y = x[count + 5 :]
        cos, sin = math.cos(yaw), math.sin(yaw)
        across, along = PITCH_MM * grid[:, 0], PITCH_MM * grid[:, 1]
        points = np.column_stack(
            [
                gx + cos * across - sin * along,
                gy + sin * across + cos * along,
                gz + tilt_x * across + tilt_y * along,
            ]
        )
        linked = wrist_camera.link_points(points, flange, angles[view], x[count : count + 2])
        camera = linked @ cv2.Rodrigues(rotation + x[count + 2 : count + 5])[0].T
        return camera[:, :2] / camera[:, 2:3] * focal

    return predict, focal


def _solve(model, poses, view, normal, grid, x, terms):
    predict, focal = _predictor(model, poses, terms)

    def error(values):
        return (predict(values, view, grid) - normal * focal).ravel()

    x, residual = wrist_camera._minimize(error, x)
    return x, residual.reshape(-1, 2), error


def _rematch(model, poses, x, terms, view, normal, grid):
    """Give each blob the grid hole that the fitted model puts nearest to it in the picture."""
    predict, focal = _predictor(model, poses, terms)
    low, high = grid.min(0) - 3, grid.max(0) + 3
    steps = [np.arange(low[k], high[k] + 1) for k in (0, 1)]
    holes = np.array([(i, j) for i in steps[0] for j in steps[1]], float)
    keep, place = np.zeros(len(view), bool), np.zeros((len(view), 2))
    for number in np.unique(view):
        rows = np.flatnonzero(view == number)
        seen = predict(x, np.full(len(holes), number), holes)
        gap = np.linalg.norm(normal[rows, None] * focal - seen[None], axis=2)
        nearest = gap.argmin(1)
        keep[rows] = gap[np.arange(len(rows)), nearest] < REMATCH_PX
        place[rows] = holes[nearest]
    return keep, place


def _view_shifts(error, x, view, views):
    """Shift of the camera of each view (reach, sideways, height in mm) that its holes ask for."""
    base = error(x).reshape(-1, 2)
    slopes = []
    for index in (len(x) - 11, len(x) - 10, len(x) - 4):  # o_x, o_y and the grid height
        moved = x.copy()
        moved[index] += 0.01
        slopes.append((error(moved).reshape(-1, 2) - base) / 0.01)
    shifts = np.full((views, 3), np.nan)
    for number in range(views):
        rows = view == number
        if rows.sum() >= 3:
            jacobian = np.column_stack([slope[rows].ravel() for slope in slopes])
            step = np.linalg.lstsq(jacobian, -base[rows].ravel(), rcond=None)[0]
            # A larger o moves the camera out and sideways; a higher grid is a lower camera.
            shifts[number] = step[0], step[1], -step[2]
    return shifts


def _rms(values):
    return np.sqrt(np.nanmean(np.asarray(values, float) ** 2, axis=0))


def fit(model, views, table_touch_z, terms=TERMS):
    """Fit the terms of the ArmError to views of the hole grid.

    Returns the error, its 1-sigma values, and the pixel error and the camera shifts of the
    views without and with the error. Take views over the full range of J2 and J3: a term
    that the views do not separate from another term has a large sigma.
    """
    poses = np.array([record["pose"] for record in views], float)
    all_view, all_normal, first_grid, on_grid, yaw, start = match_grid(model, views, table_touch_z)
    holes = np.unique(first_grid[on_grid], axis=0)
    if len(np.unique(all_view[on_grid])) < MIN_VIEWS or len(holes) < MIN_HOLES:
        raise ValueError(f"The fit needs {MIN_VIEWS} views of {MIN_HOLES} holes of the table")
    plane = table_touch_z - model["mount"]["camera_above_tip_mm"]
    tail = [*model["mount"]["offset_xy_mm"], 0, 0, 0, *start, plane, yaw, 0, 0]
    result = {}
    for name, used in (("before", ()), ("after", tuple(terms))):
        x = np.array([0.0] * len(used) + tail)
        # Round 1 uses the blobs that the stored mapping puts on the grid. The next rounds
        # match with the fitted model, so the result does not change with the stored ArmError.
        keep, grid = on_grid, first_grid
        for _ in range(4):
            data = all_view[keep], all_normal[keep], grid[keep]
            x, residual, error = _solve(model, poses, *data, x, used)
            size = np.linalg.norm(residual, axis=1)
            good = size <= OUTLIER_SIGMAS * 1.4826 * np.median(size) + 0.5
            again, new_grid = _rematch(model, poses, x, used, all_view, all_normal, holes)
            again[np.flatnonzero(keep)[~good]] = False
            if np.array_equal(again, keep) and np.array_equal(new_grid[keep], grid[keep]):
                break
            keep, grid = again, new_grid
        seen = np.unique(data[0])
        shifts = _view_shifts(error, x, data[0], len(views))
        result[name] = {
            "rms_px": float(np.sqrt(np.mean(size**2))),
            "holes": int(len(size)),
            "view_shift_rms_mm": dict(zip(("reach", "sideways", "height"), _rms(shifts).tolist())),
            "view_shift_mm": shifts.tolist(),
        }
    count = len(terms)
    flat = error(x)
    jacobian = np.empty((len(flat), len(x)))
    for index in range(len(x)):
        moved = x.copy()
        moved[index] += 1e-6
        jacobian[:, index] = (error(moved) - flat) / 1e-6
    cover = np.linalg.pinv(jacobian.T @ jacobian) * (flat @ flat) / max(1, len(flat) - len(x))
    sigma = np.sqrt(np.diag(cover))
    result.update(
        {
            "error": ArmError(**dict(zip(terms, map(float, x[:count])))),
            "sigma": dict(zip(terms, sigma[:count].tolist())),
            "camera_offset_xy_mm": x[count : count + 2].tolist(),
            "grid_yaw_deg": math.degrees(x[count + 8]),
            "table_plane_z_mm": float(x[count + 7]),
            "views": int(len(seen)),
        }
    )
    return result
