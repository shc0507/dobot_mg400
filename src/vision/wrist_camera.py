"""Lens and mount calibration of the wrist camera, from arm poses over a printed board.

The camera is on the wrist bracket. That link stays level and turns with J1 only, so the
tool rotation R does not move the camera. For a fixed point P and a flange position T at
base angle J1, the point in the camera frame is

    X_c = R_cl (Rz(-J1) (P - T) - o)

R_cl is the camera rotation in the link frame and o is the camera position in the link
frame. o_z is 0: a change of o_z is the same as a shift of all P_z.

Each board corner is a free 3D point, so the paper does not need to be flat. Its square
size gives the real scale. The fit multiplies the reported arm x and y by 1 factor, and
it takes the reported z steps as true. Each pose gets a small position correction for
the arm accuracy.
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
    """Fit the lens, the mount and the arm lateral scale.

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
    return {
        "schema": 1,
        "image_size": list(IMAGE_SIZE),
        "camera_matrix": matrix.tolist(),
        "distortion": distortion.tolist(),
        "mount": {
            "offset_xy_mm": offset.tolist(),
            "rotation_link_to_camera": cv2.Rodrigues(rotation)[0].tolist(),
        },
        "arm_lateral_scale": float(scale),
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
    if (
        document.get("schema") != 1
        or document.get("image_size") != list(IMAGE_SIZE)
        or matrix.shape != (3, 3)
        or distortion.shape != (8,)
        or rotation.shape != (3, 3)
        or offset.shape != (2,)
        or not all(np.all(np.isfinite(v)) for v in (matrix, distortion, rotation, offset))
        or not 0 < document["rms_px"] <= MAX_RMS_PX
    ):
        raise ValueError(f"Invalid wrist camera calibration: {path}")
    return {**document, "camera_matrix": matrix, "distortion": distortion}
