"""Make a labeled chip dataset from wrist camera frames of the tray. No person marks a chip.

FRAMES holds the frames and `frames.json`: [{"image": "name.jpg", "pose": [x, y, z, r]}], with
the flange pose of each frame. OUT gets `images/` and `labels/` with YOLO polygons of class 0
(Chip), the layout that `scripts/vision_eval.py` reads. Datasets do not go into Git.

uv run python scripts/wrist_chip_dataset.py FRAMES OUT
"""

import argparse
import json
import shutil
from pathlib import Path

import cv2

from vision import wrist_camera, wrist_dataset


def build(frames_dir, out_dir, model=None):
    """Label the frames. Return the counts."""
    model = model or wrist_camera.load()
    entries = json.loads((frames_dir / "frames.json").read_text())
    frames = [(cv2.imread(str(frames_dir / entry["image"])), entry["pose"]) for entry in entries]
    if any(image is None for image, _ in frames):
        raise ValueError("A frame of frames.json is missing or is not an image")
    seen = [
        (wrist_dataset.tray_markers(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)), pose)
        for image, pose in frames
    ]
    tray = wrist_dataset.locate_tray(model, seen)
    chips = wrist_dataset.register_chips(model, tray, frames)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "labels").mkdir(parents=True, exist_ok=True)
    counts = {"frames": len(frames), "chips": len(chips), "kept": 0, "labels": 0, "problems": {}}
    for entry, (image, pose) in zip(entries, frames):
        lines, problems = wrist_dataset.label_frame(model, tray, chips, image, pose)
        if problems:
            for name, number in problems.items():
                counts["problems"][name] = counts["problems"].get(name, 0) + number
            continue
        shutil.copyfile(frames_dir / entry["image"], out_dir / "images" / entry["image"])
        label = (out_dir / "labels" / entry["image"]).with_suffix(".txt")
        label.write_text("".join(line + "\n" for line in lines))
        counts["kept"] += 1
        counts["labels"] += len(lines)
    counts["chip_positions_mm"] = [[round(float(v), 2) for v in chip] for chip in chips]
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("frames", type=Path)
    parser.add_argument("out", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.frames, arguments.out)))


if __name__ == "__main__":
    main()
