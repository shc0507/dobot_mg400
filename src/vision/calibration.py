"""Camera geometry and private calibration data; importing opens no device."""

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import cv2
import numpy as np

from cameras.config import DEVICE

DEFAULT_DIRECTORY = Path("/var/lib/mg400/calibration/cameras")
MAX_DOCUMENT = 64 * 1024


def camera_device(value):
    if (
        not isinstance(value, str)
        or DEVICE.fullmatch(value) is None
        or value.rsplit("/", 1)[-1] in {".", ".."}
    ):
        raise ValueError("Calibration requires the camera's stable /dev/v4l/by-path name.")
    return value


def _array(value, shape, label, maximum=1_000_000):
    try:
        data = np.asarray(value)
        if data.shape != shape or data.dtype.kind not in "ifu" or data.dtype.kind == "b":
            raise ValueError
        data = data.astype(np.float64)
        if not np.isfinite(data).all() or np.any(np.abs(data) > maximum):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Invalid finite {label}.") from None
    return data


def image_size(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(item) is not int or item <= 0 for item in value)
        or value[0] * value[1] > 16_000_000
    ):
        raise ValueError("Image size must contain 2 positive bounded pixel dimensions.")
    return tuple(value)


def validate_model(value, *, device=None):
    """Reject malformed geometry before it can produce motion coordinates."""
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("Unknown camera calibration schema.")
    camera_device(value.get("camera_device"))
    if device is not None and value["camera_device"] != camera_device(device):
        raise ValueError("Camera port does not match its calibration.")
    image_size(value.get("image_size"))
    matrix = _array(value.get("camera_matrix"), (3, 3), "camera matrix")
    distortion = _array(value.get("distortion"), (5,), "distortion", 10)
    _array(value.get("rotation"), (3,), "camera rotation", 10)
    _array(value.get("translation"), (3,), "camera translation")
    if (
        matrix[0, 0] <= 0
        or matrix[1, 1] <= 0
        or not np.allclose(matrix[2], [0, 0, 1], rtol=0, atol=1e-12)
        or abs(matrix[0, 1]) > 1e-12
        or abs(matrix[1, 0]) > 1e-12
        or np.any(distortion[2:] != 0)
    ):
        raise ValueError("Calibration must use a pinhole matrix and radial k1 and k2 only.")
    for name in ("tray_height_mm", "distance_mm", "nominal_tag_height_mm", "created_at"):
        number = value.get(name)
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError(f"Invalid calibration {name}.")
    if not 0 < value["distance_mm"] <= 1000 or value["created_at"] <= 0:
        raise ValueError("Invalid calibration distance or time.")
    rms = value.get("rms_mm")
    if not isinstance(rms, dict) or set(rms) != {"held_out", "tray"}:
        raise ValueError("Calibration needs separate held-out and tray residuals.")
    for number in rms.values():
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError("Calibration residuals must be finite and nonnegative.")
    # Decode a bounded JSON copy: callers cannot mutate stored state via arrays.
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("Calibration document exceeds 64 KiB.")
    return json.loads(encoded)


def robot_xy(model, pixel, live_size, *, height_mm=None):
    """Undistort a pixel and intersect its forward ray with a horizontal plane."""
    if image_size(live_size) != image_size(model["image_size"]):
        raise ValueError("Live image size differs from the stored camera calibration.")
    point = _array(pixel, (2,), "pixel")
    width, height = live_size
    if not 0 <= point[0] < width or not 0 <= point[1] < height:
        raise ValueError("Pixel lies outside the calibrated image.")
    plane = model["tray_height_mm"] if height_mm is None else height_mm
    if (
        isinstance(plane, (bool, np.bool_))
        or not isinstance(plane, (int, float, np.integer, np.floating))
        or not math.isfinite(plane)
    ):
        raise ValueError("Plane height must be finite millimetres.")
    geometry = (
        None,
        np.asarray(model["camera_matrix"], np.float64),
        np.asarray(model["distortion"], np.float64),
        np.asarray(model["rotation"], np.float64),
        np.asarray(model["translation"], np.float64),
    )
    result = _intersections(geometry, point.reshape(1, 2), np.array([plane]))[0]
    if not np.isfinite(result).all():
        raise ValueError("Camera ray has no finite intersection.")
    return result.tolist()


def detect_markers(image):
    """Measure DICT_4X4_50 centers in this image without marker-ID filtering."""
    pixels = np.asarray(image)
    if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3:
        raise ValueError("ArUco input must be an RGB image.")
    image_size((pixels.shape[1], pixels.shape[0]))
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    parameters = cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), parameters
    )
    corners, identities, _ = detector.detectMarkers(gray)
    markers = []
    seen = set()
    for boundary, identity in zip(corners, [] if identities is None else identities.flatten()):
        identity = int(identity)
        if identity in seen:
            raise ValueError("The frame contains duplicate ArUco IDs.")
        seen.add(identity)
        points = boundary.reshape(4, 2).astype(np.float64)
        diagonals = np.column_stack((points[2] - points[0], -(points[3] - points[1])))
        try:
            fraction = np.linalg.solve(diagonals, points[1] - points[0])[0]
        except np.linalg.LinAlgError as error:
            raise ValueError("ArUco marker has degenerate diagonals.") from error
        center = points[0] + fraction * (points[2] - points[0])
        if not np.isfinite(center).all():
            raise ValueError("ArUco marker center is not finite.")
        markers.append(
            {"id": identity, "center_px": center.tolist(), "corners_px": points.tolist()}
        )
    return markers


class CalibrationStore:
    """Atomic, bounded JSON keyed by the complete configured camera port path."""

    def __init__(self, directory=DEFAULT_DIRECTORY):
        self.directory = Path(directory)
        if (
            not self.directory.is_absolute()
            or ".." in self.directory.parts
            or self.directory == Path(self.directory.anchor)
        ):
            raise ValueError("Camera calibration directory must be an absolute private path.")

    @staticmethod
    def _name(device):
        return hashlib.sha256(camera_device(device).encode("ascii")).hexdigest() + ".json"

    @staticmethod
    def _private(info, *, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        modes = {0o700, 0o750} if directory else {0o600, 0o640}
        if (
            not kind(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in modes
            or (not directory and info.st_nlink != 1)
        ):
            raise ValueError("Camera calibration data must be owned, private and without links.")

    @contextmanager
    def _directory(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(self.directory.anchor, flags)
        try:
            for component in self.directory.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            self._private(os.fstat(descriptor), directory=True)
            yield descriptor
        finally:
            os.close(descriptor)

    def load(self, device):
        name = self._name(device)
        try:
            with self._directory() as parent:
                pinned = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                try:
                    info = os.fstat(pinned)
                    self._private(info)
                    if not 0 < info.st_size <= MAX_DOCUMENT:
                        raise ValueError("Camera calibration file exceeds 64 KiB.")
                    descriptor = os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
                    with os.fdopen(descriptor, "rb") as stream:
                        data = stream.read(MAX_DOCUMENT + 1)
                    if len(data) > MAX_DOCUMENT:
                        raise ValueError("Camera calibration file exceeds 64 KiB.")
                finally:
                    os.close(pinned)
        except FileNotFoundError:
            return None
        return validate_model(json.loads(data), device=device)

    def save(self, device, model, *, commit_guard=nullcontext):
        data = validate_model(
            {**model, "camera_device": camera_device(device), "created_at": time.time()},
            device=device,
        )
        encoded = json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")
        name, temporary = self._name(device), ".calibration-" + uuid.uuid4().hex
        with self._directory() as parent:
            try:
                lock = os.open(
                    ".calibration.lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                pinned = os.open(
                    ".calibration.lock", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
                )
                try:
                    self._private(os.fstat(pinned))
                    lock = os.open(f"/proc/self/fd/{pinned}", os.O_RDWR | os.O_CLOEXEC)
                finally:
                    os.close(pinned)
            try:
                self._private(os.fstat(lock))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    self._private(os.stat(name, dir_fd=parent, follow_symlinks=False))
                except FileNotFoundError:
                    pass
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    with commit_guard():
                        self._current_directory(parent)
                        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                        self._current_directory(parent)
                    os.fsync(parent)
                    self._current_directory(parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(lock)
        return data

    def _current_directory(self, pinned):
        expected = os.fstat(pinned)
        with self._directory() as current:
            actual = os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise RuntimeError("Camera calibration directory changed before publication.")


VALIDATION_INDICES = (0, 5, 10, 13, 18, 23)


def _cancel(cancelled):
    if cancelled():
        raise RuntimeError("Calibration cancelled")


def _points(value, shape, name):
    return _array(value, shape, name)


def _normalize(points):
    center = points.mean(axis=0)
    radius = np.sqrt(np.mean(np.sum((points - center) ** 2, axis=1)))
    if radius <= 1e-10:
        raise ValueError("Calibration points do not span a usable volume/image")
    size = points.shape[1]
    scale = math.sqrt(size) / radius
    transform = np.eye(size + 1)
    transform[:size, :size] *= scale
    transform[:size, size] = -scale * center
    return (points - center) * scale, transform


def _dlt(xyz, pixels):
    """Normalized non-planar 3D-to-2D projective fit, without lens distortion."""
    if len(xyz) < 6:
        raise ValueError("DLT requires at least 6 noncoplanar points")
    spread = np.linalg.svd(xyz - xyz.mean(axis=0), compute_uv=False)
    if spread[-1] <= spread[0] * 1e-6:
        raise ValueError("DLT points are coplanar or numerically degenerate")
    objects, world_transform = _normalize(xyz)
    images, image_transform = _normalize(pixels)
    homogeneous = np.column_stack((objects, np.ones(len(objects))))
    rows = np.zeros((len(objects) * 2, 12))
    rows[0::2, :4] = -homogeneous
    rows[1::2, 4:8] = -homogeneous
    rows[0::2, 8:] = images[:, 0, None] * homogeneous
    rows[1::2, 8:] = images[:, 1, None] * homogeneous
    _, singular, vectors = np.linalg.svd(rows, full_matrices=False)
    if singular[-2] <= singular[0] * 1e-10:
        raise ValueError("DLT projection is not identifiable")
    projection = np.linalg.inv(image_transform) @ vectors[-1].reshape(3, 4) @ world_transform
    if np.linalg.det(projection[:, :3]) < 0:
        projection = -projection
    return projection / np.linalg.norm(projection[2, :3])


def _project_dlt(projection, xyz):
    projected = np.column_stack((xyz, np.ones(len(xyz)))) @ projection.T
    if np.any(np.abs(projected[:, 2]) < 1e-10):
        raise ValueError("DLT projects points onto the camera plane")
    return projected[:, :2] / projected[:, 2, None]


def _ransac(xyz, pixels, cancelled, trials=512):
    random = np.random.default_rng(0)
    best = None
    for _ in range(trials):
        _cancel(cancelled)
        sample = random.choice(len(xyz), 6, replace=False)
        try:
            candidate = _dlt(xyz[sample], pixels[sample])
            errors = np.linalg.norm(_project_dlt(candidate, xyz) - pixels, axis=1)
        except (ValueError, np.linalg.LinAlgError):
            continue
        mask = errors <= 2.0
        count = int(mask.sum())
        score = count, -float(np.median(errors[mask])) if count else -math.inf
        if best is None or score > best[0]:
            best = score, mask
    if best is None or best[0][0] < 12:
        raise ValueError("Fewer than 12 of the 18 training tags agree within 2 pixels")
    mask = best[1]
    projection = _dlt(xyz[mask], pixels[mask])
    return projection, mask


def _initial_intrinsics(projection, image_size):
    matrix = cv2.decomposeProjectionMatrix(projection)[0]
    matrix /= matrix[2, 2]
    matrix[0, 1] = 0.0  # OpenCV's pinhole calibration does not fit skew.
    width, height = image_size
    if (
        not np.all(np.isfinite(matrix))
        or matrix[0, 0] <= 0
        or matrix[1, 1] <= 0
        or not 0 < matrix[0, 2] < width
        or not 0 < matrix[1, 2] < height
    ):
        raise ValueError("DLT did not provide a valid intrinsic camera guess")
    return matrix


def _calibrate(xyz, pixels, image_size, initial_matrix, initial_distortion=None):
    flags = (
        cv2.CALIB_USE_INTRINSIC_GUESS
        | cv2.CALIB_ZERO_TANGENT_DIST
        | cv2.CALIB_FIX_K3
        | cv2.CALIB_FIX_K4
        | cv2.CALIB_FIX_K5
        | cv2.CALIB_FIX_K6
    )
    distortion = np.zeros(5) if initial_distortion is None else initial_distortion.copy()
    rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
        [xyz.astype(np.float32)],
        [pixels.astype(np.float32)],
        tuple(image_size),
        initial_matrix.copy(),
        distortion,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-10),
    )
    rotation, translation = rotations[0].reshape(3), translations[0].reshape(3)
    distortion = distortion.reshape(-1)
    if not all(np.all(np.isfinite(array)) for array in (matrix, distortion, rotation, translation)):
        raise ValueError("Camera refinement returned nonfinite parameters")
    depths = (xyz @ cv2.Rodrigues(rotation)[0].T + translation)[:, 2]
    if matrix[0, 0] <= 0 or matrix[1, 1] <= 0 or np.any(depths <= 0):
        raise ValueError("Camera refinement placed points behind the lens")
    return float(rms), matrix, distortion, rotation, translation


def _rays(matrix, distortion, rotation, translation, pixels):
    normalized = cv2.undistortPointsIter(
        pixels.reshape(-1, 1, 2),
        matrix,
        distortion,
        None,
        None,
        (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-12),
    ).reshape(-1, 2)
    camera_rays = np.column_stack((normalized, np.ones(len(normalized))))
    reprojection = cv2.projectPoints(camera_rays, np.zeros(3), np.zeros(3), matrix, distortion)[
        0
    ].reshape(-1, 2)
    if not np.all(np.isfinite(reprojection)) or np.any(
        np.linalg.norm(reprojection - pixels, axis=1) > 1e-6
    ):
        raise ValueError("Camera ray undistortion did not converge")
    camera_rotation = cv2.Rodrigues(rotation)[0]
    origin = -camera_rotation.T @ translation
    directions = camera_rays @ camera_rotation
    if np.any(np.abs(directions[:, 2]) < 1e-10):
        raise ValueError("Camera ray is parallel to the requested horizontal plane")
    return origin, directions


def _intersections(model, pixels, heights):
    _, matrix, distortion, rotation, translation = model
    origin, rays = _rays(matrix, distortion, rotation, translation, pixels)
    factors = (np.asarray(heights) - origin[2]) / rays[:, 2]
    if np.any(factors <= 0) or not np.all(np.isfinite(factors)):
        raise ValueError("Requested plane is behind the camera")
    return origin[:2] + factors[:, None] * rays[:, :2]


def _distance_guess(model, references, pixels, nominal_height):
    _, matrix, distortion, rotation, translation = model
    origin, rays = _rays(matrix, distortion, rotation, translation, pixels)
    slopes = rays[:, :2] / rays[:, 2, None]
    information = float(np.sum(slopes**2))
    if information < 1e-8:
        raise ValueError("Tray rays are too vertical to determine tag-to-tip distance")
    intercept = origin[:2] + (references[:, 2, None] + nominal_height - origin[2]) * slopes
    return float(np.sum(slopes * (intercept - references[:, :2])) / information)


def _minimize_distance(objective, guess, cancelled):
    """Bracket freely, then minimize; acceptance bounds never constrain fitting."""
    step = max(5.0, abs(guess) * 0.05)
    left, middle, right = guess - step, guess, guess + step
    values = [objective(left)[0], objective(middle)[0], objective(right)[0]]
    for _ in range(16):
        _cancel(cancelled)
        if values[1] <= values[0] and values[1] <= values[2]:
            break
        step *= 2
        if values[0] < values[2]:
            right, middle, left = middle, left, left - step
            values = [objective(left)[0], values[0], values[1]]
        else:
            left, middle, right = middle, right, right + step
            values = [values[1], values[2], objective(right)[0]]
    else:
        raise ValueError("Tag-to-tip distance has no identifiable finite minimum")
    ratio = (math.sqrt(5) - 1) / 2
    first, second = right - ratio * (right - left), left + ratio * (right - left)
    first_value, second_value = objective(first)[0], objective(second)[0]
    for _ in range(64):
        _cancel(cancelled)
        if right - left < 1e-4:
            break
        if first_value < second_value:
            right, second, second_value = second, first, first_value
            first = right - ratio * (right - left)
            first_value = objective(first)[0]
        else:
            left, first, first_value = first, second, second_value
            second = left + ratio * (right - left)
            second_value = objective(second)[0]
    distance = (left + right) / 2
    score, model = objective(distance)
    curvature = (objective(distance - 1)[0] + objective(distance + 1)[0] - 2 * score) / 2
    if not math.isfinite(score) or not math.isfinite(curvature) or curvature < 1e-6:
        raise ValueError("Tag-to-tip distance is not observable after camera refinement")
    return distance, model


def fit_calibration(
    tag_points,
    tag_pixels,
    tray_flange_points,
    tray_pixels,
    image_size,
    *,
    nominal_tag_height_mm=100.0,
    nominal_distance_mm=246.0,
    distance_tolerance_mm=10.0,
    rms_limit_mm=1.0,
    cancelled=lambda: False,
):
    """Fit 18 tags + tray constraints; the 6 tag holdouts never affect fitting.

    Points are no-tool flange XYZ. Tags use Z+h, tray points use Z+h-D.
    The scalar mapping plane is the mean tray height; reference validation uses
    each reference's own height and reports fitted residuals, not holdout error.
    """
    _cancel(cancelled)
    if (
        not isinstance(image_size, (list, tuple))
        or len(image_size) != 2
        or any(type(value) is not int or value <= 0 for value in image_size)
        or image_size[0] * image_size[1] > 16_000_000
    ):
        raise ValueError("Invalid calibration image size")
    for value in (
        nominal_tag_height_mm,
        nominal_distance_mm,
        distance_tolerance_mm,
        rms_limit_mm,
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Calibration distances and error limits must be positive")
    tags = _points(tag_points, (24, 3), "tag flange points")
    pixels = _points(tag_pixels, (24, 2), "tag pixels")
    count = len(tray_flange_points)
    if not 2 <= count <= 4:
        raise ValueError("Calibration requires 2 to 4 tray references")
    references = _points(tray_flange_points, (count, 3), "tray flange points")
    reference_pixels = _points(tray_pixels, (count, 2), "tray pixels")
    for values in (pixels, reference_pixels):
        if np.any(values < 0) or np.any(values >= np.asarray(image_size)):
            raise ValueError("Calibration pixels are outside the image")
    # Height-major 3x4 grids; hold out one different column per row and height.
    validation = np.array(VALIDATION_INDICES)
    training = np.array([index for index in range(24) if index not in VALIDATION_INDICES])
    xyz = tags + [0, 0, nominal_tag_height_mm]
    projection, mask = _ransac(xyz[training], pixels[training], cancelled)
    inliers = training[mask]
    initial_matrix = _initial_intrinsics(projection, image_size)
    _cancel(cancelled)
    tag_model = _calibrate(xyz[inliers], pixels[inliers], image_size, initial_matrix)
    guess = _distance_guess(tag_model, references, reference_pixels, nominal_tag_height_mm)
    combined_pixels = np.vstack((pixels[inliers], reference_pixels))

    def objective(distance):
        _cancel(cancelled)
        reference_xyz = references + [0, 0, nominal_tag_height_mm - distance]
        objects = np.vstack((xyz[inliers], reference_xyz))
        model = _calibrate(objects, combined_pixels, image_size, tag_model[1], tag_model[2])
        return model[0] ** 2 * len(objects), model

    distance, model = _minimize_distance(objective, guess, cancelled)
    if abs(distance - nominal_distance_mm) > distance_tolerance_mm:
        raise ValueError("Recovered tag-to-tip distance exceeds the permitted nominal difference")
    reference_heights = references[:, 2] + nominal_tag_height_mm - distance
    held_errors = np.linalg.norm(
        _intersections(model, pixels[validation], xyz[validation, 2]) - xyz[validation, :2], axis=1
    )
    reference_errors = np.linalg.norm(
        _intersections(model, reference_pixels, reference_heights) - references[:, :2], axis=1
    )
    held_rms = float(np.sqrt(np.mean(held_errors**2)))
    reference_rms = float(np.sqrt(np.mean(reference_errors**2)))
    if max(held_rms, reference_rms) > rms_limit_mm:
        raise ValueError("Calibration held-out or tray fit-residual RMS exceeds the limit")
    return {
        "schema": 1,
        "camera_matrix": model[1].tolist(),
        "distortion": model[2].tolist(),
        "rotation": model[3].tolist(),
        "translation": model[4].tolist(),
        "tray_height_mm": float(np.mean(reference_heights)),
        "distance_mm": float(distance),
        "nominal_tag_height_mm": float(nominal_tag_height_mm),
        "image_size": list(image_size),
        "points": {
            "tag_flange_mm": tags.tolist(),
            "tag_pixels": pixels.tolist(),
            "tray_flange_mm": references.tolist(),
            "tray_pixels": reference_pixels.tolist(),
            "training_indices": training.tolist(),
            "validation_indices": validation.tolist(),
            "inlier_indices": inliers.tolist(),
        },
        "rms_mm": {"held_out": held_rms, "tray": reference_rms},
        "errors_mm": {"held_out": held_errors.tolist(), "tray": reference_errors.tolist()},
    }
