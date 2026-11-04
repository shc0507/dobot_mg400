"""Independent image measurements relative to four supplied reference poses.

No filename, frame sequence, motor reading or previous prediction enters locate().
Coordinates and offsets are ORIGINAL image pixels. A reference defines zero;
this is not a measurement of the optical axis or a guarantee of mechanical seating.
"""

import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from .paths import calibration_path, directory, private_open, run_path

REFERENCES = {
    0: "side_20261020_222952_249760200.png",
    1: "side_20261020_222935_908004300.png",
    2: "side_20261020_222913_668929300.png",
    3: "side_20261020_222857_582616300.png",
}
COLORS = {0: "red", 1: "yellow", 2: "green", 3: "blue"}


def read_gray(path):
    im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise ValueError(f"Cannot read {path}")
    return im


def marker_center(corners):
    # Intersection of diagonals: projected square center under perspective.
    c = np.asarray(corners, np.float64).reshape(4, 2)
    a = np.column_stack((c[2] - c[0], -(c[3] - c[1])))
    t = np.linalg.solve(a, c[1] - c[0])[0]
    return c[0] + t * (c[2] - c[0])


def detect(gray, target_id=None):
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if float(gray.mean()) < 2:
        return {"shape": [h, w], "status": "black_frame", "markers": []}
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), p)
    scale = min(1.0, 1920 / w)
    small = cv2.resize(gray, (round(w * scale), round(h * scale)))
    cs, ids, _ = detector.detectMarkers(small)
    origin_y = 0
    method = "half_resolution"
    initial_detection = (cs, ids, scale, origin_y, method)

    def found_target(found_ids):
        return found_ids is not None and any(
            int(i) in COLORS and (target_id is None or int(i) == target_id)
            for i in found_ids.flatten()
        )

    if not found_target(ids):
        # Independent retries on THIS image only. Never relax dictionary bit correction.
        origin_y = round(h * 0.35)
        crop = gray[origin_y : round(h * 0.85)]
        scale = 1.0
        for variant in ("native", "threshold3", "threshold11", "contrast"):
            retry = cv2.aruco.DetectorParameters()
            retry.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
            source = crop
            if variant.startswith("threshold"):
                retry.adaptiveThreshConstant = float(variant[9:])
                retry.adaptiveThreshWinSizeMax = 63
                retry.perspectiveRemovePixelPerCell = 12
            if variant == "contrast":
                retry.adaptiveThreshWinSizeMax = 63
                retry.adaptiveThreshWinSizeStep = 4
                source = cv2.createCLAHE(2.0, (8, 8)).apply(crop)
            cs, ids, _ = cv2.aruco.ArucoDetector(
                cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), retry
            ).detectMarkers(source)
            if found_target(ids):
                method = variant
                scale = 1.0
                break
    sources = [(cs, ids, scale, origin_y, method)]
    if target_id is not None and not found_target(initial_detection[1]):
        # Keep other IDs found earlier in THIS image. They are needed to tell
        # a central objective from a target marker merely visible at the edge.
        sources.append(initial_detection)
    markers = []
    for cs, ids, scale, origin_y, method in sources:
        if ids is None:
            continue
        known = [int(i) for i in ids.flatten() if int(i) in COLORS]
        if len(known) != len(set(known)):
            return {"shape": [h, w], "status": "duplicate_ids", "markers": []}
        for corners, ident in zip(cs, ids.flatten()):
            if int(ident) not in COLORS:
                continue
            if any(m["id"] == int(ident) for m in markers):
                continue
            c = (corners.reshape(4, 2) / scale).astype(np.float32)
            c[:, 1] += origin_y
            cv2.cornerSubPix(
                gray,
                c.reshape(-1, 1, 2),
                (4, 4),
                (-1, -1),
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.001),
            )
            markers.append(
                {
                    "id": int(ident),
                    "color": COLORS[int(ident)],
                    "corners": c.tolist(),
                    "center": marker_center(c).tolist(),
                    "method": method,
                }
            )
    return {"shape": [h, w], "status": "ok" if markers else "not_detected", "markers": markers}


class Locator:
    def __init__(self, near_px=50.0):
        self.near_px = near_px
        self.references = {}
        for ident, name in REFERENCES.items():
            d = detect(read_gray(calibration_path("side_camera_images") / name), target_id=ident)
            matches = [m for m in d["markers"] if m["id"] == ident]
            if len(matches) != 1:
                raise ValueError(f"Reference {name}: expected one ID {ident}")
            self.references[ident] = dict(matches[0], shape=d["shape"], file=name)

    def classify(self, detection):
        result = dict(detection, markers=[])
        for marker in detection["markers"]:
            ref = self.references[marker["id"]]
            if detection["shape"] != ref["shape"]:
                return dict(
                    detection,
                    status="image_size_mismatch",
                    reference_shape=ref["shape"],
                    markers=[],
                )
            dx = marker["center"][0] - ref["center"][0]
            dy = marker["center"][1] - ref["center"][1]
            direction = "left" if dx < 0 else "right" if dx > 0 else "zero"
            near = abs(dx) <= self.near_px
            result["markers"].append(
                dict(
                    marker, dx_px=dx, dy_px=dy, range="near" if near else "far", direction=direction
                )
            )
        return result

    def locate(self, image, target_id=None, *, only_target=True):
        if target_id is not None and target_id not in self.references:
            raise ValueError(f"Unknown objective ID {target_id}")
        result = self.classify(detect(image, target_id=target_id))
        if target_id is not None:
            present = any(m["id"] == target_id for m in result["markers"])
            if only_target:
                result["markers"] = [m for m in result["markers"] if m["id"] == target_id]
            if not present and result["status"] == "ok":
                result["status"] = "target_not_visible"
        return result


def annotate(path, result, references, output):
    im = cv2.imread(str(path))
    scale = 1280 / im.shape[1]
    im = cv2.resize(im, (1280, round(im.shape[0] * scale)))
    for m in result["markers"]:
        c = np.round(np.array(m["corners"]) * scale).astype(np.int32)
        cv2.polylines(im, [c], True, (0, 255, 0), 2)
        x, y = np.round(np.array(m["center"]) * scale).astype(int)
        rx = round(references[m["id"]]["center"][0] * scale)
        cv2.line(im, (rx, max(0, y - 100)), (rx, min(im.shape[0] - 1, y + 100)), (255, 255, 0), 2)
        cv2.drawMarker(im, (x, y), (0, 0, 255), cv2.MARKER_CROSS, 20, 2)
        label = f"{m['color']} {m['range']} {m['direction']}"
        if m["range"] == "near":
            label += f" {abs(m['dx_px']):.2f}px"
        cv2.putText(im, label, (max(5, x - 130), max(35, y - 65)), 0, 0.65, (0, 255, 255), 2)
    if not result["markers"]:
        cv2.putText(im, result["status"], (25, 45), 0, 1, (0, 0, 255), 2)
    ok, encoded = cv2.imencode(Path(output).suffix, im)
    if not ok:
        raise RuntimeError("Cannot encode annotated image")
    with private_open(output, "wb") as image_file:
        image_file.write(encoded.tobytes())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=Path)
    ap.add_argument("--folder", type=Path)
    ap.add_argument("--output", type=Path, default=run_path("microscope/scan_position_results"))
    ap.add_argument("--near-px", type=float, default=50.0)
    ap.add_argument("--id", type=int, choices=list(COLORS), help="Only report this objective ID")
    args = ap.parse_args()
    cv2.setNumThreads(1)
    loc = Locator(args.near_px)
    args.output = run_path(args.output)
    directory(args.output)
    files = [args.image] if args.image else sorted(args.folder.glob("*.png"))
    results = []

    def run(path):
        result = dict(loc.locate(read_gray(path), target_id=args.id), file=path.name)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, result in enumerate(pool.map(run, files)):
            results.append(result)
            if (i + 1) % 30 == 0:
                print(f"{i + 1}/{len(files)}", flush=True)
    payload = {"near_px": args.near_px, "references": loc.references, "results": results}
    with private_open(args.output / "detections.json", "w", encoding="utf-8") as output:
        output.write(json.dumps(payload, indent=2))
    with private_open(args.output / "positions.csv", "w", newline="", encoding="utf-8-sig") as f:
        fields = ["file", "status", "id", "color", "dx_px", "dy_px", "range", "direction"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            for m in r["markers"] or [{}]:
                writer.writerow(
                    dict(
                        file=r["file"],
                        status=r["status"],
                        **{k: m[k] for k in fields[2:] if k in m},
                    )
                )
    # Near poses and sparse full-rotation samples; avoid writing 360 huge images.
    previews = args.output / "previews"
    directory(previews)
    for i, (path, result) in enumerate(zip(files, results)):
        if i % 15 == 0 or any(m["range"] == "near" for m in result["markers"]):
            annotate(path, result, loc.references, previews / (path.stem + ".jpg"))
    print(f"Saved {len(results)} independent predictions to {args.output}", flush=True)
    if args.image:
        print(json.dumps(results[0], indent=2), flush=True)


if __name__ == "__main__":
    main()
