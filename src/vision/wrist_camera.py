"""Lens and mount calibration of the wrist camera, from arm poses over a printed board.

The camera is on the wrist bracket. That link stays level and turns with J1 only, so the
tool rotation R does not move the camera. For a fixed point P and a flange position T at
base angle J1, the point in the camera frame is

    X_c = R_cl (Rz(-J1) (P - T) - o)

R_cl is the camera rotation in the link frame and o is the camera position in the link
frame. o_z is 0: a change of o_z is the same as a shift of all P_z.

Each board corner is a free 3D point, so the paper does not need to be flat. Its square
size gives the real scale. The fit takes the reported z steps as true. Each pose gets a
small position correction for the arm accuracy.

The reported radial moves of the arm do not agree exactly with the board. The fit gives
them 1 scale factor about the mean radius of the poses. The mapping functions do not use
that factor: a scale about the base axis is 5 mm wrong away from those poses.

The board fit does not see the tool, so it cannot give the place of the cup against the
camera. `fit_touch` gives that from picks: the flange places where the cup sealed on a
chip, and looks before and after each move. It also gives the terms of the arm. The arm
reports a longer move than the real one between a look pose and a touch, by about 1%
along the reach and less sideways. The mapping multiplies the vector from the flange to
the point by these 2 scales in the link frame. It does not scale positions about the base.
"""

import json
from pathlib import Path

import cv2
import numpy as np

CONFIG = Path(__file__).resolve().parents[2] / "config/cameras/wrist.json"
IMAGE_SIZE = (3840, 2160)
NOMINAL_FOCAL_PX = 1241.0  # 1.8 mm lens, 1.45 um pixels
SQUARES = (11, 8)
SQUARE_MM = 24.0
MARKER_MM = 17.0
MIN_POSES = 8
PIXEL_SIGMA = 0.5
POSE_SIGMA_MM = 0.2
SQUARE_SIGMA_MM = 0.05
OUTLIER_PX = 2.5
MAX_RMS_PX = 1.0
# Without a touch fit: the arm moves as it reports and the cup is on the flange axis.
NO_ARM = {"radial_scale": 1.0, "sideways_scale": 1.0, "radial_drift": 0.0, "radius_mm": 0.0}
LOOK_SIGMA_MM = 0.15
MOVE_SIGMA_MM = 0.3
MIN_MOVES = 12
# Looks from 1 height do not tell a longer camera distance from larger arm scales.
MIN_HEIGHT_RANGE_MM = 20.0


def board():
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
    return cv2.aruco.CharucoBoard(SQUARES, SQUARE_MM, MARKER_MM, dictionary)


def board_image(pixels_per_square):
    """The board as a grayscale image without a margin."""
    size = (SQUARES[0] * pixels_per_square, SQUARES[1] * pixels_per_square)
    return board().generateImage(size, marginSize=0, borderBits=1)


def board_corners(gray):
    """Return {corner ID: (x, y)} for the chessboard corners of the board in a frame."""
    detector = cv2.aruco.CharucoDetector(board())
    best = {}
    for scale in (1, 2):
        image = gray if scale == 1 else cv2.resize(gray, None, fx=0.5, fy=0.5)
        corners, ids, _, _ = detector.detectBoard(image)
        if ids is None or len(ids) <= len(best):
            continue
        points = corners.reshape(-1, 1, 2).astype(np.float32) * scale + (scale - 1) / 2
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 0.01)
        points = cv2.cornerSubPix(gray, points, (7, 7), (-1, -1), criteria).reshape(-1, 2)
        best = {int(i): (float(x), float(y)) for i, (x, y) in zip(ids.ravel(), points)}
    return best


def link_points(points, flange, j1, offset):
    """Rz(-J1)(P - T) - o for each row."""
    delta = points - flange
    cos, sin = np.cos(j1), np.sin(j1)
    return np.column_stack(
        [
            cos * delta[:, 0] + sin * delta[:, 1] - offset[0],
            -sin * delta[:, 0] + cos * delta[:, 1] - offset[1],
            delta[:, 2],
        ]
    )


def project(matrix, distortion, rotation, points):
    pixels = cv2.projectPoints(points.reshape(-1, 1, 3), rotation, np.zeros(3), matrix, distortion)
    return pixels[0].reshape(-1, 2)


def _minimize(function, x, iterations=80):
    """Levenberg-Marquardt with a forward-difference Jacobian."""
    residual = function(x)
    cost = residual @ residual
    damping = 1e-3
    for _ in range(iterations):
        jacobian = np.empty((len(residual), len(x)))
        for index in range(len(x)):
            step = 1e-6 * max(1.0, abs(x[index]))
            moved = x.copy()
            moved[index] += step
            jacobian[:, index] = (function(moved) - residual) / step
        normal, gradient = jacobian.T @ jacobian, jacobian.T @ residual
        done = True
        while damping < 1e12:
            scaled = normal + damping * np.diag(np.diag(normal) + 1e-12)
            trial = x + np.linalg.solve(scaled, -gradient)
            new = function(trial)
            new_cost = new @ new
            if np.isfinite(new_cost) and new_cost < cost:
                done = cost - new_cost < 1e-9 * cost
                x, residual, cost, damping = trial, new, new_cost, damping / 3
                break
            damping *= 4
        if done:
            break
    return x, residual


def _down(yaw):
    """Rotation of a camera that looks straight down, turned by yaw about the vertical."""
    cos, sin = np.cos(yaw), np.sin(yaw)
    return np.array([[cos, sin, 0.0], [sin, -cos, 0.0], [0.0, 0.0, -1.0]])


def _triangulate(rotation, flange, j1, pixels):
    """Point nearest to all viewing rays of 1 corner, for the nominal lens and no offset."""
    centre = np.array(IMAGE_SIZE) / 2
    rays = np.column_stack([(pixels - centre) / NOMINAL_FOCAL_PX, np.ones(len(pixels))])
    normal, target = np.zeros((3, 3)), np.zeros(3)
    for ray, position, angle in zip(rays @ rotation, flange, j1):
        cos, sin = np.cos(angle), np.sin(angle)
        direction = np.array([cos * ray[0] - sin * ray[1], sin * ray[0] + cos * ray[1], ray[2]])
        direction /= np.linalg.norm(direction)
        plane = np.eye(3) - np.outer(direction, direction)
        normal += plane
        target += plane @ position
    return np.linalg.solve(normal, target)


def _matrix(values):
    return np.array([[values[0], 0, values[2]], [0, values[1], values[3]], [0, 0, 1.0]])


def fit(records, square_mm=SQUARE_MM):
    """Fit the lens, the mount and the arm radial scale.

    records: [{"pose": [x, y, z, r], "board": {corner ID: [u, v]}}]. Discard the frame that
    follows an enable without a move: the arm is not at its reported position then.
    """
    flange = np.array([record["pose"][:3] for record in records], float)
    j1 = np.arctan2(flange[:, 1], flange[:, 0])
    rows = [
        (pose, int(corner), pixel)
        for pose, record in enumerate(records)
        for corner, pixel in record["board"].items()
    ]
    seen = np.bincount([corner for _, corner, _ in rows])
    ids = [int(corner) for corner in np.flatnonzero(seen >= MIN_POSES)]
    if len(records) < MIN_POSES or len(ids) < 12:
        raise ValueError("The calibration needs 8 poses and 12 board corners in 8 poses each")
    index = {corner: number for number, corner in enumerate(ids)}
    rows = [row for row in rows if row[1] in index]
    columns = SQUARES[0] - 1
    pairs = [(index[a], index[a + 1]) for a in ids if (a + 1) % columns and a + 1 in index]
    pairs += [(index[a], index[a + columns]) for a in ids if a + columns in index]
    first, second = np.array(pairs).T

    def arrays(rows):
        pose = np.array([row[0] for row in rows])
        corner = np.array([index[row[1]] for row in rows])
        return pose, corner, np.array([row[2] for row in rows], float)

    pose, corner, pixels = arrays(rows)
    nominal = np.array([NOMINAL_FOCAL_PX, NOMINAL_FOCAL_PX, IMAGE_SIZE[0] / 2, IMAGE_SIZE[1] / 2])

    def start(yaw):
        rotation = _down(yaw)
        points = np.array(
            [
                _triangulate(
                    rotation, flange[pose[corner == n]], j1[pose[corner == n]], pixels[corner == n]
                )
                for n in range(len(ids))
            ]
        )
        return np.concatenate([cv2.Rodrigues(rotation)[0].ravel(), [0, 0], points.ravel()])

    def mount_error(values):
        linked = link_points(values[5:].reshape(-1, 3)[corner], flange[pose], j1[pose], values[3:5])
        return (project(_matrix(nominal), np.zeros(5), values[:3], linked) - pixels).ravel()

    # The yaw of the camera in the link is unknown. Take the best of 36 start values.
    starts = [start(yaw) for yaw in np.radians(np.arange(0, 360, 10))]
    mount, _ = _minimize(mount_error, min(starts, key=lambda v: float(np.sum(mount_error(v) ** 2))))

    count = 3 * len(ids)

    def unpack(x):
        points = x[17 : 17 + count].reshape(-1, 3)
        moved = x[17 + count : -1].reshape(-1, 3)
        return _matrix(x), x[4:12], x[12:15], x[15:17], points, moved, x[-1]

    def error(x):
        matrix, distortion, rotation, offset, points, moved, scale = unpack(x)
        position = flange * [scale, scale, 1.0] + moved
        linked = link_points(points[corner], position[pose], j1[pose], offset)
        spacing = np.linalg.norm(points[first] - points[second], axis=1)
        return np.concatenate(
            [
                ((project(matrix, distortion, rotation, linked) - pixels) / PIXEL_SIGMA).ravel(),
                (moved / POSE_SIGMA_MM).ravel(),
                (spacing - square_mm) / SQUARE_SIGMA_MM,
            ]
        )

    x = np.concatenate([nominal, np.zeros(8), mount, np.zeros(3 * len(records)), [1.0]])
    for _ in range(2):  # the second pass runs without the outliers of the first pass
        x, residual = _minimize(error, x)
        errors = np.linalg.norm(residual[: 2 * len(pixels)].reshape(-1, 2), axis=1) * PIXEL_SIGMA
        keep = errors <= OUTLIER_PX
        if keep.all():
            break
        rows = [row for row, good in zip(rows, keep) if good]
        pose, corner, pixels = arrays(rows)
    matrix, distortion, rotation, offset, _, moved, scale = unpack(x)
    errors = errors[keep] if not keep.all() else errors
    # The fit scales about the base axis. A scale about the mean radius is the same model
    # with a different radial offset, and it leaves positions at that radius unchanged.
    radius = float(np.mean(np.hypot(flange[:, 0], flange[:, 1])))
    offset = offset + [(scale - 1.0) * radius, 0.0]
    return {
        "schema": 3,
        "image_size": list(IMAGE_SIZE),
        "camera_matrix": matrix.tolist(),
        "distortion": distortion.tolist(),
        "mount": {
            "offset_xy_mm": offset.tolist(),
            "rotation_link_to_camera": cv2.Rodrigues(rotation)[0].tolist(),
        },
        "arm": dict(NO_ARM, radius_mm=radius),
        "tool": {"offset_xy_mm": [0.0, 0.0]},
        "arm_radial_scale": float(scale),
        "calibration_radius_mm": radius,
        "square_mm": float(square_mm),
        "poses": len(records),
        "observations": int(len(errors)),
        "rms_px": float(np.sqrt(np.mean(errors**2))),
        "max_px": float(errors.max()),
        "pose_correction_rms_mm": float(np.sqrt(np.mean(moved**2))),
    }


def load(path=CONFIG):
    """Read a stored calibration. Return the document with arrays for the matrices."""
    document = json.loads(Path(path).read_text())
    matrix = np.array(document["camera_matrix"], float)
    distortion = np.array(document["distortion"], float)
    rotation = np.array(document["mount"]["rotation_link_to_camera"], float)
    offset = np.array(document["mount"]["offset_xy_mm"], float)
    height = document["mount"].get("camera_above_tip_mm")
    arm = document.get("arm", {})
    tool = np.array(document.get("tool", {}).get("offset_xy_mm", [np.nan]), float)
    terms = [arm.get(name, np.nan) for name in NO_ARM]
    if (
        document.get("schema") != 3
        or document.get("image_size") != list(IMAGE_SIZE)
        or matrix.shape != (3, 3)
        or distortion.shape != (8,)
        or rotation.shape != (3, 3)
        or offset.shape != (2,)
        or tool.shape != (2,)
        or not all(np.all(np.isfinite(v)) for v in (matrix, distortion, rotation, offset, tool))
        or not np.all(np.isfinite(terms))
        or not 0 < document["rms_px"] <= MAX_RMS_PX
        or not 0.9 < document["arm_radial_scale"] < 1.1
        or not 0.9 < arm["radial_scale"] < 1.1
        or not 0.9 < arm["sideways_scale"] < 1.1
        or not abs(arm["radial_drift"]) < 0.1
        or np.abs(tool).max() > 10
        or (height is not None and not 0 < height < 200)
    ):
        raise ValueError(f"Invalid wrist camera calibration: {path}")
    return {**document, "camera_matrix": matrix, "distortion": distortion}


def _normal(model, pixels):
    points = np.asarray(pixels, float).reshape(-1, 1, 2)
    return cv2.undistortPoints(points, model["camera_matrix"], model["distortion"]).reshape(-1, 2)


def _turn(poses, vectors):
    """Link frame to arm frame for each row: a turn by the J1 of the pose about z."""
    radius = np.hypot(poses[:, 0], poses[:, 1])
    cos, sin = poses[:, 0] / radius, poses[:, 1] / radius
    x, y = vectors[:, 0], vectors[:, 1]
    return np.column_stack([cos * x - sin * y, sin * x + cos * y, vectors[:, 2]])


def _lines(model, normal, poses, tool=True):
    """Return a start point and a direction for each row of normalised pixels and poses.

    The lines are in reported arm coordinates: the point start + t direction at the height
    of a surface is the flange x, y that puts the cup over the point of the surface, with
    the tool rotation r of the pose. tool=False gives the place of the flange axis.
    """
    mount, arm = model["mount"], model["arm"]
    rotation = np.array(mount["rotation_link_to_camera"], float)
    stretch = np.array([arm["radial_scale"], arm["sideways_scale"], 1.0])
    link = np.column_stack([normal, np.ones(len(normal))]) @ rotation
    start = np.tile(np.array([*mount["offset_xy_mm"], 0.0]) * stretch, (len(poses), 1))
    radius = np.hypot(poses[:, 0], poses[:, 1])
    start[:, 0] += arm["radial_drift"] * (radius - arm["radius_mm"])
    centre = poses[:, :3] + _turn(poses, start)
    if tool:
        angle = np.radians(poses[:, 3])
        x, y = model["tool"]["offset_xy_mm"]
        centre[:, 0] -= np.cos(angle) * x - np.sin(angle) * y
        centre[:, 1] -= np.sin(angle) * x + np.cos(angle) * y
    return centre, _turn(poses, link * stretch)


def _poses(pose, count):
    """1 pose for each row. A pose without r has the tool rotation 0."""
    pose = np.asarray(pose, float)
    return np.tile(np.append(pose, 0.0)[:4] if len(pose) < 4 else pose[:4], (count, 1))


def rays(model, pixels, pose):
    """Return the start point and a direction of the line of each pixel, for 1 arm pose.

    The frame is the arm frame. Its z is the flange z of the pose, so the start point has
    the z of the flange and not of the lens. The lines include the scales of the arm and
    the cup offset: see `surface_points`.
    """
    normal = _normal(model, pixels)
    centre, directions = _lines(model, normal, _poses(pose, len(normal)))
    return centre[0], directions


def _height(model):
    height = model["mount"].get("camera_above_tip_mm")
    if height is None:
        raise ValueError("The calibration has no camera height above the tool tip")
    return height


def surface_points(model, pixels, pose, touch_z):
    """Return the arm x, y of pixels on a level surface.

    touch_z is the flange z at which the tool tip touches the surface. A move to the
    result with the tool rotation r of the pose puts the cup over the point. Use a pose
    with the lens 170 mm or more above the surface: a nearer view can be 2 mm off.
    """
    centre, directions = rays(model, pixels, pose)
    distance = (touch_z - _height(model) - centre[2]) / directions[:, 2]
    if np.any(distance <= 0):
        raise ValueError("A pixel does not look at the surface")
    return (centre + distance[:, None] * directions)[:, :2]


def surface_pixels(model, points, pose, touch_z):
    """Return the pixels of arm x, y points on a level surface: the reverse of surface_points."""
    points = np.asarray(points, float).reshape(-1, 2)
    poses = _poses(pose, len(points))
    centre, _ = _lines(model, np.zeros((len(points), 2)), poses)
    drop = touch_z - _height(model) - centre[:, 2]
    arm = np.column_stack([(points - centre[:, :2]) / drop[:, None], np.ones(len(points))])
    stretch = [model["arm"]["radial_scale"], model["arm"]["sideways_scale"], 1.0]
    turned = poses * [1.0, -1.0, 1.0, 1.0]  # the turn by -J1
    link = _turn(turned, arm) / stretch * np.sign(drop)[:, None]
    camera = link @ np.array(model["mount"]["rotation_link_to_camera"], float).T
    return project(model["camera_matrix"], model["distortion"], np.zeros(3), camera)


def locate(model, pixels, poses):
    """Return the arm x, y and the touch z of 1 fixed point, from its pixel in 2 or more poses."""
    normal, target = np.zeros((3, 3)), np.zeros(3)
    for pixel, pose in zip(pixels, poses, strict=True):
        centre, direction = rays(model, [pixel], pose)
        direction = direction[0] / np.linalg.norm(direction[0])
        plane = np.eye(3) - np.outer(direction, direction)
        normal += plane
        target += plane @ centre
    if len(poses) < 2 or np.linalg.cond(normal) > 1e4:
        raise ValueError("The poses do not give 2 different viewing directions")
    point = np.linalg.solve(normal, target)
    height = model["mount"].get("camera_above_tip_mm")
    touch_z = None if height is None else float(point[2] + height)
    return float(point[0]), float(point[1]), touch_z, float(point[2])


def _spin(degrees, vectors):
    """Turn each row of x, y vectors by its angle about z."""
    cos, sin = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    return np.column_stack(
        [cos * vectors[:, 0] - sin * vectors[:, 1], sin * vectors[:, 0] + cos * vectors[:, 1]]
    )


def fit_touch(model, looks, moves, touch_z):
    """Fit the cup offset, the camera place and the arm scales to looks and chip moves.

    looks: [{"point": key, "pose": [x, y, z, r], "pixel": [u, v]}]. Each point is fixed on a
    level surface with the touch height touch_z, and several poses see it.
    moves: 1 record for each chip that the cup picked and put down again:
    {"pose_before", "pixel_before", "picked_at": [x, y], "across": [x, y], "turn",
    "put": [x, y], "pose_after", "pixel_after"}. The pick has the tool rotation of
    pose_before, and the put has that rotation plus `turn` degrees. `across` is the unit
    vector across the width of the chip: the cup seals only near the middle of the width.

    The chip turns about the flange axis, so the looks before and after a move give the
    place of the flange axis against the camera. The sealed places then give the cup offset.
    Return the model with a new mount offset, yaw, camera height, arm terms and cup offset.
    The camera height stays as stored when all looks are from nearly 1 height.
    """
    keys = sorted({str(look["point"]) for look in looks})
    if len(moves) < MIN_MOVES or len(looks) < 8 * len(keys) or not keys:
        raise ValueError("The touch fit needs 12 moves and 8 looks of each fixed point")
    group = np.array([keys.index(str(look["point"])) for look in looks])
    seen = _normal(model, [look["pixel"] for look in looks])
    seen_from = np.array([look["pose"] for look in looks], float)[:, :4]
    before = _normal(model, [move["pixel_before"] for move in moves])
    after = _normal(model, [move["pixel_after"] for move in moves])
    pose_before = np.array([move["pose_before"] for move in moves], float)[:, :4]
    pose_after = np.array([move["pose_after"] for move in moves], float)[:, :4]
    picked, put = (np.array([move[key] for move in moves], float) for key in ("picked_at", "put"))
    across = np.array([move["across"] for move in moves], float)
    turn = np.array([move["turn"] for move in moves], float)
    radius = float(np.mean(np.hypot(seen_from[:, 0], seen_from[:, 1])))
    mount, arm = model["mount"], model["arm"]
    rotation = np.array(mount["rotation_link_to_camera"], float)

    def candidate(x):
        yaw = cv2.Rodrigues(np.array([0.0, 0.0, x[3]]))[0]
        return {
            **model,
            "mount": {
                "offset_xy_mm": [float(x[0]), float(x[1])],
                "rotation_link_to_camera": (rotation @ yaw.T).tolist(),
                "camera_above_tip_mm": float(x[2]),
            },
            "arm": {
                "radial_scale": float(x[4]),
                "sideways_scale": float(x[5]),
                "radial_drift": float(x[6]),
                "radius_mm": radius,
            },
            "tool": {"offset_xy_mm": [float(x[7]), float(x[8])]},
        }

    def places(trial, normal, poses):
        """Flange x, y that puts the flange axis over each point."""
        centre, directions = _lines(trial, normal, poses, tool=False)
        distance = (touch_z - _height(trial) - centre[:, 2]) / directions[:, 2]
        return (centre + distance[:, None] * directions)[:, :2]

    def parts(x):
        trial = candidate(x)
        point = places(trial, seen, seen_from)
        sums = np.zeros((len(keys), 2))
        np.add.at(sums, group, point)
        spread = point - (sums / np.bincount(group, minlength=len(keys))[:, None])[group]
        # The chip from the flange axis: at the pick, and after the put.
        held = places(trial, before, pose_before) - picked
        left = places(trial, after, pose_after) - put
        cup = _spin(pose_before[:, 3], np.tile(x[7:9], (len(moves), 1)))
        return spread, left - _spin(turn, held), np.einsum("ij,ij->i", across, held - cup)

    heights = np.concatenate([seen_from[:, 2], pose_before[:, 2], pose_after[:, 2]])
    keep_height = 1e3 if np.ptp(heights) < MIN_HEIGHT_RANGE_MM else 0.0

    def error(x):
        spread, closure, seal = parts(x)
        return np.concatenate(
            [
                spread.ravel() / LOOK_SIGMA_MM,
                closure.ravel() / MOVE_SIGMA_MM,
                seal / MOVE_SIGMA_MM,
                [keep_height * (x[2] - _height(model))],
            ]
        )

    x = [*mount["offset_xy_mm"], _height(model), 0.0, arm["radial_scale"], arm["sideways_scale"]]
    x, _ = _minimize(error, np.array([*x, arm["radial_drift"], *model["tool"]["offset_xy_mm"]]))
    spread, closure, seal = parts(x)
    result = candidate(x)
    result["touch_fit"] = {
        "touch_z_mm": float(touch_z),
        "points": len(keys),
        "looks": len(looks),
        "moves": len(moves),
        "look_rms_mm": float(np.sqrt((spread**2).sum(1).mean())),
        "move_rms_mm": float(np.sqrt((closure**2).sum(1).mean())),
        "seal_rms_mm": float(np.sqrt((seal**2).mean())),
    }
    return result
