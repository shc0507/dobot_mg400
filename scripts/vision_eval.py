"""Evaluate the chip detector on a labeled set. No training and no camera access.

The dataset directory holds images/ and labels/ with matching file names.
Labels are YOLO polygons. Class 0 is Chip. Other classes only group the frames.
Each frame goes through Localizer.predict, as in the vision service.

uv run python scripts/vision_eval.py DATASET --weights WEIGHTS --px-per-mm 1.554
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path

import numpy as np

from vision.localizer import Localizer

CHIP_CLASS = 0
IOU_THRESHOLD = 0.5
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
GROUPS = {"chips": "With chips", "arm": "Arm, no chips", "empty": "No labels"}


def read_labels(path, width, height):
    """Return (class ID, pixel box) for each YOLO polygon line."""
    labels = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        class_id, *values = line.split()
        if len(values) < 6 or len(values) % 2:
            raise ValueError(f"{path}:{number}: expected a class ID and at least 3 polygon points.")
        xs = [float(x) * width for x in values[0::2]]
        ys = [float(y) * height for y in values[1::2]]
        labels.append((int(class_id), (min(xs), min(ys), max(xs), max(ys))))
    return labels


def box_iou(a, b):
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    overlap = width * height
    return overlap / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap)


def match(detections, boxes, iou_threshold=IOU_THRESHOLD):
    """Match detections to label boxes in descending confidence.

    Return the center errors of the matches in pixels, the count of false
    detections and the count of missed labels. A label matches at most 1 detection.
    """
    free = list(boxes)
    errors, false_detections = [], 0
    for detection in sorted(detections, key=lambda d: d["confidence"], reverse=True):
        best = max(free, key=lambda box: box_iou(detection["xyxy"], box), default=None)
        if best is None or box_iou(detection["xyxy"], best) < iou_threshold:
            false_detections += 1
            continue
        free.remove(best)
        x, y = detection["center_xy"]
        errors.append(math.hypot(x - (best[0] + best[2]) / 2, y - (best[1] + best[3]) / 2))
    return errors, false_detections, len(free)


def evaluate(dataset, localizer, *, confidence=None, iou_threshold=IOU_THRESHOLD):
    """Return one result per frame and the prediction settings.

    Without a confidence, predict uses its default, as the vision service does.
    """
    dataset = Path(dataset)
    images = sorted(p for p in (dataset / "images").iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    options = {} if confidence is None else {"confidence": confidence}
    frames, settings = [], None
    for image in images:
        _, result = localizer.predict(image, **options)
        width, height = result["image_size"]["width"], result["image_size"]["height"]
        labels = read_labels(dataset / "labels" / f"{image.stem}.txt", width, height)
        chips = [box for class_id, box in labels if class_id == CHIP_CLASS]
        detections = [d for d in result["detections"] if d["label"].lower() == "chip"]
        errors, false_detections, missed = match(detections, chips, iou_threshold)
        frames.append(
            {
                "name": image.name,
                "group": "chips" if chips else "arm" if labels else "empty",
                "labels": len(chips),
                "detections": len(detections),
                "matched": len(errors),
                "errors": errors,
                "false": false_detections,
                "missed": missed,
            }
        )
        settings = {key: result[key] for key in ("preprocessing", "confidence_threshold")}
    if not frames:
        raise ValueError(f"No images in {dataset / 'images'}.")
    return frames, settings


def ratio(numerator, denominator):
    return f"{numerator / denominator:.3f} ({numerator}/{denominator})" if denominator else "n/a"


def report(frames, *, settings, px_per_mm, weights=None, iou_threshold=IOU_THRESHOLD):
    """Return the Chip results as Markdown."""
    lines = []
    if weights is not None:
        with open(weights, "rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        lines.append(f"Weights: `{Path(weights).name}` (sha256 `{digest}`)")
    lines += [
        f"Model input: {settings['preprocessing']}. "
        f"Confidence threshold: {settings['confidence_threshold']}.",
        f"A match needs a box IoU of at least {iou_threshold}. "
        "A label matches at most 1 detection.",
        "",
        "| Frames | Count | Chip labels | Chip detections | Matched | False | Missed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    keys = ("labels", "detections", "matched", "false", "missed")
    for group, title in [*GROUPS.items(), (None, "All")]:
        chosen = [f for f in frames if group is None or f["group"] == group]
        counts = [len(chosen), *(sum(f[key] for f in chosen) for key in keys)]
        lines.append(f"| {title} | " + " | ".join(str(c) for c in counts) + " |")
    total = {key: sum(f[key] for f in frames) for key in keys}
    lines += [
        "",
        f"Chip precision: {ratio(total['matched'], total['detections'])}. "
        f"Chip recall: {ratio(total['matched'], total['labels'])}.",
    ]
    errors = [e for f in frames for e in f["errors"]]
    if errors:
        lines += [
            "",
            f"| Center error, {len(errors)} matches | Mean | Median | 95th percentile | Max |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        stats = [np.mean(errors), np.median(errors), np.percentile(errors, 95), max(errors)]
        for unit, scale in (("px", 1), (f"mm ({px_per_mm} px/mm)", px_per_mm)):
            lines.append(f"| {unit} | " + " | ".join(f"{s / scale:.2f}" for s in stats) + " |")
    wrong = [f for f in frames if f["false"] or f["missed"]]
    if wrong:
        lines += ["", "| Frame with errors | Missed | False |", "| --- | ---: | ---: |"]
        lines += [f"| `{f['name']}` | {f['missed']} | {f['false']} |" for f in wrong]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("dataset", type=Path, help="directory with images/ and labels/")
    parser.add_argument("--weights", type=Path, help="model file; default: MG400_VISION_WEIGHTS")
    parser.add_argument("--px-per-mm", type=float, required=True, help="image scale on the tray")
    parser.add_argument("--confidence", type=float, help="default: the vision service threshold")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    if not args.px_per_mm > 0:
        parser.error("--px-per-mm must be positive.")
    try:
        localizer = Localizer(args.weights, device=args.device)
        frames, settings = evaluate(args.dataset, localizer, confidence=args.confidence)
    except (FileNotFoundError, ValueError, ImportError) as error:
        parser.exit(1, f"{error}\n")
    print(report(frames, settings=settings, px_per_mm=args.px_per_mm, weights=localizer.weights))


if __name__ == "__main__":
    main()
