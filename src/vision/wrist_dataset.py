"""Label wrist camera frames of chips on the tray without a person and without a trained model.

The tray plate is plain grey, so an object on it differs from the tray around it. The
arm pose and the wrist camera calibration give the place of the tray and of each known
chip in a frame. A frame gets a label for a chip only where an object is at the place of
that chip. The label is the outline of that object in that frame.
"""

from collections import Counter

import cv2
import numpy as np

from vision import wrist_camera

TRAY_MARKERS = (0, 1, 2)  # corner tiles: 0 at the origin, 1 and 2 along the 2 edges
MARKER_PITCH_MM = 149.4
MARKER_HALF_MM = 14.2  # the plate edge is this far outside the marker centres
PLATE_TOUCH_Z = 22.0  # flange Z at which the tool tip touches the tray plate
CHIP_THICKNESS_MM = 1.4
EDGE_MM = 8.0
TILE_MM = 20.0
BORDER_MM = 3.0
BACKGROUND_MM = 18.0  # larger than a chip
CONTRAST = 9.0  # Lab distance from the local tray colour
MIN_AREA_MM2 = 12.0
MAX_AREA_MM2 = 400.0
MATCH_MM = 5.0
MIN_SHARE = 0.6


def pixels_per_mm(model, pose, touch_z):
    focal = float(model["camera_matrix"][0][0])
    return focal / (pose[2] - touch_z + model["mount"]["camera_above_tip_mm"])


def to_pixels(model, pose, points, touch_z):
    """Pixels of arm x, y points on the level surface that the tip touches at touch_z."""
    points = np.asarray(points, float).reshape(-1, 2)
    height = touch_z - model["mount"]["camera_above_tip_mm"]
    arm = np.column_stack([points, np.full(len(points), height)])
    j1 = np.full(len(arm), np.arctan2(pose[1], pose[0]))
    flange = np.array(pose[:3], float)
    linked = wrist_camera.link_points(arm, flange, j1, model["mount"]["offset_xy_mm"])
    rotation = cv2.Rodrigues(np.array(model["mount"]["rotation_link_to_camera"], float))[0]
    return wrist_camera.project(model["camera_matrix"], model["distortion"], rotation, linked)


def tray_markers(gray):
    """Return {marker ID: centre pixel} of the tray corner tiles in 1 frame.

    The tiles are dark on a grey plate, so the detector also reads 2 smaller copies.
    """
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    found = {}
    for scale in (1, 2, 4):
        image = gray if scale == 1 else cv2.resize(gray, None, fx=1 / scale, fy=1 / scale)
        corners, ids, _ = detector.detectMarkers(image)
        for quad, marker in zip(corners, [] if ids is None else ids.ravel()):
            if int(marker) in TRAY_MARKERS:
                centre = quad.reshape(4, 2).mean(0) * scale + (scale - 1) / 2
                found.setdefault(int(marker), centre)
    return found


def locate_tray(model, observations):
    """Return the tray frame (origin, 2 unit axes) from [(markers, pose)] of 2 or more frames."""
    centres = {}
    for marker in TRAY_MARKERS:
        seen = [(found[marker], pose) for found, pose in observations if marker in found]
        if len(seen) < 2:
            raise ValueError(f"Tray marker {marker} is in fewer than 2 frames")
        x, y, _, _ = wrist_camera.locate(model, [p for p, _ in seen], [q for _, q in seen])
        centres[marker] = np.array([x, y])
    origin, first, second = (centres[marker] for marker in TRAY_MARKERS)
    lengths = np.linalg.norm(first - origin), np.linalg.norm(second - origin)
    if max(abs(length - MARKER_PITCH_MM) for length in lengths) > 5:
        raise ValueError("The tray markers are not 149.4 mm apart")
    return origin, (first - origin) / lengths[0], (second - origin) / lengths[1]


def tray_mask(model, pose, tray, shape):
    """Mask of the tray plate in a frame, without its edges and its 4 corner tiles."""
    origin, first, second = tray

    def corners(low_a, high_a, low_b, high_b):
        arm = [
            origin + a * first + b * second
            for a, b in ((low_a, low_b), (high_a, low_b), (high_a, high_b), (low_a, high_b))
        ]
        return to_pixels(model, pose, arm, PLATE_TOUCH_Z).astype(np.int32)

    low, high = -MARKER_HALF_MM + EDGE_MM, MARKER_PITCH_MM + MARKER_HALF_MM - EDGE_MM
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [corners(low, high, low, high)], 1)
    for a in (0.0, MARKER_PITCH_MM):
        for b in (0.0, MARKER_PITCH_MM):
            cv2.fillPoly(mask, [corners(a - TILE_MM, a + TILE_MM, b - TILE_MM, b + TILE_MM)], 0)
    return mask


def find_objects(image, mask, px_per_mm):
    """Return the outlines of the things inside the mask that differ from the tray around them."""
    small = cv2.resize(image, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    area = cv2.resize(mask, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    if not area.any():
        return []
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
    for channel in range(3):
        lab[:, :, channel][area == 0] = int(np.median(lab[:, :, channel][area > 0]))
    kernel = max(3, int(BACKGROUND_MM * px_per_mm / 4) | 1)
    size = (image.shape[1], image.shape[0])
    background = cv2.resize(cv2.medianBlur(lab, min(kernel, 255)), size).astype(np.float32)
    frame = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32), (0, 0), 1)
    differs = ((np.linalg.norm(frame - background, axis=2) > CONTRAST) & (mask > 0)).astype(
        np.uint8
    )
    differs = cv2.morphologyEx(differs, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    differs = cv2.morphologyEx(differs, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    border = int(BORDER_MM * px_per_mm) | 1
    inner = cv2.erode(mask, np.ones((border, border), np.uint8))
    outlines = []
    for outline in cv2.findContours(differs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, w, h = cv2.boundingRect(outline)
        on = differs[y : y + h, x : x + w] > 0
        if np.any(inner[y : y + h, x : x + w][on] == 0):
            continue  # at the border: shading at the tray edge, or an object half outside
        if MIN_AREA_MM2 <= cv2.contourArea(outline) / px_per_mm**2 <= MAX_AREA_MM2:
            outlines.append(outline.reshape(-1, 2))
    return outlines


def _centre(outline):
    moments = cv2.moments(outline.astype(np.float32))
    return np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]])


def frame_objects(model, tray, image, pose):
    """Return [(outline, centre pixel, arm x y)] of the objects on the tray in 1 frame."""
    top = PLATE_TOUCH_Z + CHIP_THICKNESS_MM
    mask = tray_mask(model, pose, tray, image.shape)
    outlines = find_objects(image, mask, pixels_per_mm(model, pose, top))
    centres = [_centre(outline) for outline in outlines]
    if not outlines:
        return []
    arm = wrist_camera.surface_points(model, centres, pose, top)
    return list(zip(outlines, centres, arm))


def register_chips(model, tray, frames):
    """Return the arm x, y of each chip: an object at 1 place in most of the frames.

    frames: [(image, pose)]. An object that is in few frames is a shadow or a reflection.
    """
    groups = []
    for image, pose in frames:
        for _, _, arm in frame_objects(model, tray, image, pose):
            for group in groups:
                if np.linalg.norm(np.mean(group, axis=0) - arm) < MATCH_MM:
                    group.append(arm)
                    break
            else:
                groups.append([arm])
    return [np.mean(group, axis=0) for group in groups if len(group) >= MIN_SHARE * len(frames)]


def label_frame(model, tray, chips, image, pose):
    """Return (YOLO polygon lines of class 0, problems) for 1 frame.

    A problem is a chip in view without an object at its place, or an object that is not
    at the place of a chip. A frame with a problem must not go into a training set.
    """
    height, width = image.shape[:2]
    top = PLATE_TOUCH_Z + CHIP_THICKNESS_MM
    scale = pixels_per_mm(model, pose, top)
    mask = tray_mask(model, pose, tray, image.shape)
    border = int(BORDER_MM * scale) | 1
    inner = cv2.erode(mask, np.ones((border, border), np.uint8))
    objects = frame_objects(model, tray, image, pose)
    lines, problems, used = [], Counter(), set()
    for chip, pixel in zip(chips, to_pixels(model, pose, chips, top) if len(chips) else []):
        column, row = int(round(pixel[0])), int(round(pixel[1]))
        near = [(np.linalg.norm(arm - chip), n) for n, (_, _, arm) in enumerate(objects)]
        if near and min(near)[0] < MATCH_MM:
            used.add(min(near)[1])
            box = cv2.boxPoints(cv2.minAreaRect(objects[min(near)[1]][0].astype(np.float32)))
            box = np.clip(box / [width, height], 0, 1)
            lines.append("0 " + " ".join(f"{value:.6f}" for value in box.ravel()))
        elif 0 <= column < width and 0 <= row < height and inner[row, column]:
            problems["chip without an object"] += 1
    problems["object that is not a chip"] += len(objects) - len(used)
    return lines, +problems
