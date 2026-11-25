"""Label wrist camera frames of chips on the tray without a person and without a trained model.

The tray plate is plain grey, so an object on it differs from the tray around it. A
segmentation model that needs no training gives the outline of each object in each frame.
The arm pose and the wrist camera calibration put these outlines on the tray in mm.

A chip does not change its shape. Its outline is taken once, from several views of 1 layout.
After that, only its place and its angle change: a frame gets the stored outline of each chip,
fitted to the object in that frame with a shift and a turn.
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
TOP_Z = PLATE_TOUCH_Z + CHIP_THICKNESS_MM  # flange Z at which the tool tip touches a chip
EDGE_MM = 8.0
TILE_MM = 20.0
BORDER_MM = 3.0
BACKGROUND_MM = 18.0  # larger than a chip
CONTRAST = 9.0  # Lab distance from the local tray colour
MIN_AREA_MM2 = 12.0
MAX_AREA_MM2 = 400.0
MATCH_MM = 5.0
MIN_SHARE = 0.6
RASTER_MM = 0.05  # mm for each pixel of a mask on the tray
WINDOW_MM = 16.0  # half of the mask window around a chip
AGREE = 0.8  # the top face of a chip is the area that this share of the views has
FIT_SHARE = 0.6  # a stored outline with less common area than this does not fit an object
POINTS = 32  # the most corners of a label


def pixels_per_mm(model, pose, touch_z):
    focal = float(model["camera_matrix"][0][0])
    return focal / (pose[2] - touch_z + model["mount"]["camera_above_tip_mm"])


def to_pixels(model, pose, points, touch_z):
    """Pixels of arm x, y points on the level surface that the tip touches at touch_z."""
    return wrist_camera.surface_pixels(model, points, pose, touch_z)


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
    top = TOP_Z
    mask = tray_mask(model, pose, tray, image.shape)
    outlines = find_objects(image, mask, pixels_per_mm(model, pose, top))
    centres = [_centre(outline) for outline in outlines]
    if not outlines:
        return []
    arm = wrist_camera.surface_points(model, centres, pose, top)
    return list(zip(outlines, centres, arm))


def frame_polygons(model, tray, image, pose, segment):
    """Return the outline of each object in 1 frame, as polygons in arm mm at the chip top.

    segment(image, boxes) returns 1 outline in pixels for each box (x0, y0, x1, y1), or an
    empty outline. The boxes come from the object finder.
    """
    boxes = []
    for found, _, _ in frame_objects(model, tray, image, pose):
        x, y, w, h = cv2.boundingRect(found.astype(np.int32))
        boxes.append((x, y, x + w, y + h))
    outlines = [np.asarray(o, float).reshape(-1, 2) for o in segment(image, boxes)] if boxes else []
    return [wrist_camera.surface_points(model, o, pose, TOP_Z) for o in outlines if len(o) >= 8]


def raster(polygon, centre):
    """Mask of a polygon in arm mm on the window around `centre`."""
    size = int(2 * WINDOW_MM / RASTER_MM)
    pixels = (np.asarray(polygon, float) - centre + WINDOW_MM) / RASTER_MM
    mask = np.zeros((size, size), np.uint8)
    cv2.fillPoly(mask, [np.round(pixels).astype(np.int32)], 1)
    return mask


def centred(polygon):
    """Return (the polygon about its area centre, the centre)."""
    moments = cv2.moments(np.asarray(polygon, np.float32))
    centre = np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]])
    return np.asarray(polygon, float) - centre, centre


def turned(polygon, degrees):
    cos, sin = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    return np.asarray(polygon, float) @ np.array([[cos, sin], [-sin, cos]])


def _shared(polygons):
    """Outline of the area that AGREE of the polygons have in common, about its centre."""
    origin = np.zeros(2)
    total = sum(raster(polygon, origin).astype(np.float32) for polygon in polygons)
    mask = (total >= AGREE * len(polygons)).astype(np.uint8)
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
    if not contours:
        raise ValueError("The views of a chip have no common area")
    points = cv2.approxPolyDP(max(contours, key=cv2.contourArea), 0.06 / RASTER_MM, True)
    return centred(points.reshape(-1, 2) * RASTER_MM - WINDOW_MM)[0]


def _axis(mask):
    moments = cv2.moments(mask, True)
    return 0.5 * np.degrees(np.arctan2(2 * moments["mu11"], moments["mu20"] - moments["mu02"]))


def fit(shape, polygon, near=None, turn=True):
    """Fit a stored outline (mm, about its centre) to the outline of an object in arm mm.

    near: the expected angle in degrees. Without it, both directions of the long axis are tried.
    turn=False: shift only, at the angle `near`.
    Return (x, y, angle, share). share is the common area over the joined area.
    """
    origin = np.zeros(2)
    centre = centred(polygon)[1]
    target = raster(polygon, centre).astype(np.float32)
    area = float(target.sum())

    def score(angle):
        own = raster(turned(shape, angle), origin)
        x, y, w, h = cv2.boundingRect(own)
        part = own[y : y + h, x : x + w].astype(np.float32)
        result = cv2.matchTemplate(target, part, cv2.TM_CCORR)
        common = float(result.max())
        rows, columns = np.nonzero(result >= common - 0.5)  # all the best places: use the middle
        shift = np.array([columns.mean() - x, rows.mean() - y]) * RASTER_MM
        return common / (area + part.sum() - common), shift

    if near is None:
        base = _axis(target.astype(np.uint8)) - _axis(raster(shape, origin))
        starts = [base, base + 180.0]
    else:
        starts = [near]
    best = (-1.0, 0.0, origin)
    for start in starts:
        local = (*score(start), start)
        local = (local[0], start, local[1])
        for span, step in ((8.0, 1.0), (1.0, 0.2), (0.2, 0.05)) if turn else ():
            middle = local[1]
            for angle in np.arange(middle - span, middle + span + 1e-9, step):
                share, shift = score(angle)
                if share > local[0]:
                    local = (share, float(angle), shift)
        if local[0] > best[0]:
            best = local
    share, angle, shift = best
    return float(centre[0] + shift[0]), float(centre[1] + shift[1]), angle % 360.0, float(share)


def chip_shape(polygons):
    """Return the outline of 1 chip about its centre, from its outlines in several views.

    The views do not agree on the place of a chip to better than about 1 mm, so each view is
    shifted onto the others first. A side face shows on a different side in each view: the
    area that most views share is the top face.
    """
    views = [centred(polygon)[0] for polygon in polygons]
    shape = _shared(views)
    for _ in range(2):
        moved = []
        for view in views:
            x, y, _, _ = fit(shape, view, near=0.0, turn=False)
            moved.append(view - [x, y])
        views = moved
        shape = _shared(views)
    return shape


def group_views(frames):
    """Group the objects of the frames of 1 layout by place.

    frames: [polygons of 1 frame]. Return [[(frame index, polygon)]], 1 list for each object
    that is at 1 place in MIN_SHARE of the frames. Other objects are shadows or reflections.
    """
    groups = []
    for index, polygons in enumerate(frames):
        for polygon in polygons:
            centre = centred(polygon)[1]
            for members in groups:
                if np.linalg.norm(np.mean([m[2] for m in members], axis=0) - centre) < MATCH_MM:
                    members.append((index, polygon, centre))
                    break
            else:
                groups.append([(index, polygon, centre)])
    kept = [members for members in groups if len(members) >= MIN_SHARE * len(frames)]
    return [[(index, polygon) for index, polygon, _ in members] for members in kept]


def match_shapes(shapes, groups):
    """Return {group index: (shape index, angle)}: the stored outline that fits each group best.

    Each group is tried in up to 3 of its views. A group that no stored outline fits to
    FIT_SHARE gets no entry.
    """
    tries = []
    for number, members in enumerate(groups):
        area = np.mean([cv2.contourArea(polygon.astype(np.float32)) for _, polygon in members])
        for index, shape in enumerate(shapes):
            if abs(area / cv2.contourArea(np.asarray(shape, np.float32)) - 1.0) > 0.3:
                continue
            fits = [fit(shape, polygon) for _, polygon in members[:: max(1, len(members) // 3)][:3]]
            share = float(np.mean([found[3] for found in fits]))
            angles = np.radians([found[2] for found in fits])
            angle = float(np.degrees(np.arctan2(np.sin(angles).mean(), np.cos(angles).mean())))
            tries.append((share, number, index, angle))
    matched, used = {}, set()
    for share, number, index, angle in sorted(tries, reverse=True):
        if share >= FIT_SHARE and number not in matched and index not in used:
            matched[number] = (index, angle)
            used.add(index)
    return matched


def label_line(model, pose, shape, x, y, angle, size):
    """YOLO polygon line of class 0 for a stored outline at a fitted place in 1 frame."""
    points = np.asarray(shape, np.float32)
    step = 0.06
    while len(points) > POINTS:
        step *= 1.5
        points = cv2.approxPolyDP(np.asarray(shape, np.float32), step, True).reshape(-1, 2)
    pixels = to_pixels(model, pose, turned(points, angle) + [x, y], TOP_Z)
    values = np.clip(pixels / [size[0], size[1]], 0, 1)
    return "0 " + " ".join(f"{value:.6f}" for value in values.ravel())


def label_layout(model, tray, shapes, frames, poses, sizes):
    """Label the frames of 1 layout with the stored outlines.

    frames: [polygons of 1 frame] from frame_polygons. shapes: the stored outlines. A group of
    objects that fits no stored outline, with shapes given, is not a known chip.
    Return ([label lines of 1 frame, or None for a frame with a problem], problems, places).
    places: {shape index: (x, y, angle)} of each chip in this layout.

    A problem is a chip in view without an object at its place, or an object that is not a
    chip. A frame with a problem must not go into a training set.
    """
    groups = group_views(frames)
    matched = match_shapes(shapes, groups)
    lines = [[] for _ in frames]
    bad, problems, places = set(), Counter(), {}
    known = {id(polygon) for members in groups for _, polygon in members}
    for index, polygons in enumerate(frames):
        strangers = sum(id(polygon) not in known for polygon in polygons)
        if strangers:
            problems["object that is not a chip"] += strangers
            bad.add(index)
    for number, members in enumerate(groups):
        if number not in matched:
            problems["object that is not a chip"] += len(members)
            bad.update(index for index, _ in members)
            continue
        shape_index, angle = matched[number]
        fits = {index: fit(shapes[shape_index], polygon, near=angle) for index, polygon in members}
        place = np.median([found[:2] for found in fits.values()], axis=0)
        places[shape_index] = (float(place[0]), float(place[1]), angle % 360.0)
        for index, (pose, size) in enumerate(zip(poses, sizes)):
            if index in fits:
                x, y, turn, _ = fits[index]
                lines[index].append(label_line(model, pose, shapes[shape_index], x, y, turn, size))
                continue
            mask = tray_mask(model, pose, tray, (size[1], size[0]))
            border = int(BORDER_MM * pixels_per_mm(model, pose, TOP_Z)) | 1
            inner = cv2.erode(mask, np.ones((border, border), np.uint8))
            column, row = np.round(to_pixels(model, pose, [place], TOP_Z)[0]).astype(int)
            if 0 <= column < size[0] and 0 <= row < size[1] and inner[row, column]:
                problems["chip without an object"] += 1
                bad.add(index)
    return [None if index in bad else found for index, found in enumerate(lines)], +problems, places


def layout_shapes(frames):
    """Return the outline of each chip of 1 layout: the first round, before any stored outline."""
    return [chip_shape([polygon for _, polygon in members]) for members in group_views(frames)]
