"""Make a labeled chip dataset from wrist camera frames of the tray. No person marks a chip.

Each LAYOUT folder holds the frames of 1 layout of the chips and `frames.json`:
[{"image": "name.jpg", "pose": [x, y, z, r]}], with the flange pose of each frame. OUT gets
`images/` and `labels/` with YOLO polygons of class 0 (Chip), the layout that
`scripts/vision_eval.py` reads. Datasets do not go into Git.

SHAPES holds the outline of each chip in mm. If the file does not exist, the first LAYOUT gives
the outlines and the script writes the file. A later layout gets these outlines at the new
places. SAM is a Segment Anything weights file of `ultralytics`: run the script in an
environment that has `ultralytics`, such as the vision environment.

python scripts/wrist_chip_dataset.py OUT LAYOUT [LAYOUT ...] --shapes SHAPES --sam SAM
"""

import argparse
import json
import shutil
from pathlib import Path

import cv2
import numpy as np

from vision import wrist_camera, wrist_dataset

MARGIN_PX = 24  # around the box of an object, for the crop that the model gets
PAD_PX = 6


def sam_segmenter(weights):
    """Return segment(image, boxes) that gives the outline of the object in each box."""
    from ultralytics import SAM

    model = SAM(str(weights))

    def segment(image, boxes):
        height, width = image.shape[:2]
        outlines = []
        for x0, y0, x1, y1 in boxes:
            half = int(max(x1 - x0, y1 - y0) * 1.1 + MARGIN_PX)
            left, top = int(max((x0 + x1) / 2 - half, 0)), int(max((y0 + y1) / 2 - half, 0))
            right, bottom = (
                int(min((x0 + x1) / 2 + half, width)),
                int(min((y0 + y1) / 2 + half, height)),
            )
            crop = np.ascontiguousarray(image[top:bottom, left:right])
            box = [x0 - left - PAD_PX, y0 - top - PAD_PX, x1 - left + PAD_PX, y1 - top + PAD_PX]
            result = model.predict(crop, bboxes=[box], verbose=False)[0]
            if result.masks is None or len(result.masks.data) == 0:
                outlines.append([])
                continue
            mask = result.masks.data[0].cpu().numpy().astype(np.uint8)
            if mask.shape != crop.shape[:2]:
                size = (crop.shape[1], crop.shape[0])
                mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
            contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
            largest = max(contours, key=cv2.contourArea) if contours else np.zeros((0, 1, 2))
            outlines.append(largest.reshape(-1, 2) + [left, top])
        return outlines

    return segment


def build(out_dir, layouts, shapes_file, segment, model=None):
    """Label the frames of each layout. Return the counts."""
    model = model or wrist_camera.load()
    shapes = None
    if shapes_file.exists():
        shapes = [np.array(shape) for shape in json.loads(shapes_file.read_text())["shapes_mm"]]
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "labels").mkdir(parents=True, exist_ok=True)
    counts = {"frames": 0, "kept": 0, "labels": 0, "problems": {}, "layouts": {}}
    for layout in layouts:
        entries = json.loads((layout / "frames.json").read_text())
        images = [cv2.imread(str(layout / entry["image"])) for entry in entries]
        if any(image is None for image in images):
            raise ValueError(f"A frame of {layout / 'frames.json'} is missing or is not an image")
        poses = [entry["pose"] for entry in entries]
        seen = [
            (wrist_dataset.tray_markers(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)), pose)
            for image, pose in zip(images, poses)
        ]
        tray = wrist_dataset.locate_tray(model, seen)
        frames = [
            wrist_dataset.frame_polygons(model, tray, image, pose, segment)
            for image, pose in zip(images, poses)
        ]
        if shapes is None:
            shapes = wrist_dataset.layout_shapes(frames)
            document = {"shapes_mm": [np.round(shape, 3).tolist() for shape in shapes]}
            shapes_file.write_text(json.dumps(document))
        sizes = [(image.shape[1], image.shape[0]) for image in images]
        lines, problems, places = wrist_dataset.label_layout(
            model, tray, shapes, frames, poses, sizes
        )
        for name, number in problems.items():
            counts["problems"][name] = counts["problems"].get(name, 0) + number
        for entry, found in zip(entries, lines):
            counts["frames"] += 1
            if found is None:
                continue
            name = f"{layout.name}_{entry['image']}"
            shutil.copyfile(layout / entry["image"], out_dir / "images" / name)
            (out_dir / "labels" / name).with_suffix(".txt").write_text(
                "".join(line + "\n" for line in found)
            )
            counts["kept"] += 1
            counts["labels"] += len(found)
        counts["layouts"][layout.name] = {
            str(index): [round(value, 2) for value in place]
            for index, place in sorted(places.items())
        }
    counts["chips"] = len(shapes or [])
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("layouts", type=Path, nargs="+")
    parser.add_argument("--shapes", type=Path, required=True)
    parser.add_argument("--sam", type=Path, required=True)
    arguments = parser.parse_args()
    segment = sam_segmenter(arguments.sam)
    print(json.dumps(build(arguments.out, arguments.layouts, arguments.shapes, segment)))


if __name__ == "__main__":
    main()
