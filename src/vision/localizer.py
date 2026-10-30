"""Local chip/arm inference. No cloud inference or camera/motor access.

Python: annotate_image('input.jpg', 'output.jpg', weights='models/weights.pt')
CLI: python -m vision.localizer input.jpg --output output.jpg --weights /path/to/weights.pt
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass(frozen=True)
class Detection:
    class_id: int
    label: str
    confidence: float
    xyxy: tuple[float, float, float, float]
    center_xy: tuple[float, float]
    polygon: tuple[tuple[float, float], ...] | None = None
    angle_deg: float | None = None
    size_px: tuple[float, float] | None = None


def _mask_geometry(polygon, width, height, input_size):
    """Map a contour, then return OpenCV's rectangle angle and width/height."""
    points = np.array(polygon, dtype=np.float32, copy=True)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3:
        return None, None, None
    if not np.all(np.isfinite(points)):
        return None, None, None
    points *= [width / input_size, height / input_size]
    points[:, 0] = np.clip(points[:, 0], 0, width)
    points[:, 1] = np.clip(points[:, 1], 0, height)
    _, size, angle = cv2.minAreaRect(points)
    mapped = tuple((float(x), float(y)) for x, y in points)
    return mapped, float(angle), (float(size[0]), float(size[1]))


def load_image(source: str | Path | Image.Image | np.ndarray) -> Image.Image:
    """Load a file/PIL RGB image or uint8 OpenCV BGR array; apply EXIF orientation."""
    if isinstance(source, (str, Path)):
        with Image.open(source) as image:
            return ImageOps.exif_transpose(image).convert("RGB").copy()
    if isinstance(source, Image.Image):
        return ImageOps.exif_transpose(source).convert("RGB").copy()
    if isinstance(source, np.ndarray):
        if source.ndim != 3 or source.shape[2] != 3 or source.dtype != np.uint8:
            raise ValueError("Array input must be a uint8 H x W x 3 OpenCV BGR image.")
        return Image.fromarray(source[:, :, ::-1].copy())
    raise TypeError("Expected an image path, PIL image, or OpenCV BGR array.")


def map_detections(
    boxes, class_ids, scores, names, width, height, input_size, confidence=0.39, *, polygons=None
) -> list[Detection]:
    """Map boxes and optional masks to the EXIF-oriented original image.

    Polygons correspond to input boxes before confidence filtering. Rectangle
    angles and sizes use OpenCV's convention in the original-image coordinates.
    """
    if polygons is not None and len(polygons) != len(boxes):
        raise ValueError("Each box must have one corresponding mask polygon.")
    mapped = []
    for index, (box, class_id, score) in enumerate(zip(boxes, class_ids, scores, strict=True)):
        if not np.isfinite(score) or float(score) < confidence:
            continue
        values = np.asarray(box, dtype=float)
        if values.shape != (4,) or not np.all(np.isfinite(values)):
            continue
        values *= [width / input_size, height / input_size, width / input_size, height / input_size]
        values[[0, 2]] = np.clip(values[[0, 2]], 0, width)
        values[[1, 3]] = np.clip(values[[1, 3]], 0, height)
        x1, y1, x2, y2 = (float(v) for v in values)
        if x2 <= x1 or y2 <= y1:
            continue
        cid = int(class_id)
        polygon, angle, size = (None, None, None)
        if polygons is not None:
            polygon, angle, size = _mask_geometry(polygons[index], width, height, input_size)
        mapped.append(
            Detection(
                cid,
                str(names[cid]),
                float(score),
                (x1, y1, x2, y2),
                ((x1 + x2) / 2, (y1 + y2) / 2),
                polygon,
                angle,
                size,
            )
        )
    return mapped


def draw_detections(image: Image.Image, detections: list[Detection]) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    font_size = max(12, round(min(result.size) / 65))
    font = ImageFont.load_default(size=font_size)
    line_width = max(2, round(min(result.size) / 450))
    occupied_labels = []
    object_boxes = [d.xyxy for d in detections]

    def overlap(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))

    for item in detections:
        color = "#18b85a" if item.label.lower() == "chip" else "#ed8a24"
        x1, y1, x2, y2 = item.xyxy
        draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
    for item in detections:
        color = "#18b85a" if item.label.lower() == "chip" else "#ed8a24"
        x1, y1, x2, y2 = item.xyxy
        label = f"{item.label} {item.confidence:.2f}"
        text_box = draw.textbbox((0, 0), label, font=font)
        tw, th = text_box[2] - text_box[0], text_box[3] - text_box[1]
        lw, lh = tw + 8, th + 8
        candidates = []
        for row in range(5):
            for px, py in [
                (x1, y1 - lh * (row + 1) - 3),
                (x1, y2 + lh * row + 3),
                (x2 + 3, y1 + lh * row),
                (x1 - lw - 3, y1 + lh * row),
            ]:
                px = max(0, min(px, result.width - lw))
                py = max(0, min(py, result.height - lh))
                rect = (px, py, px + lw, py + lh)
                cost = (
                    100 * sum(overlap(rect, old) for old in occupied_labels)
                    + 4 * sum(overlap(rect, box) for box in object_boxes)
                    + abs(px - x1)
                    + abs(py - (y1 - lh))
                )
                candidates.append((cost, rect))
        _, rect = min(candidates, key=lambda candidate: candidate[0])
        occupied_labels.append(rect)
        tx, ty = rect[:2]
        # A short leader associates a displaced label with its detection box.
        anchor = (max(tx, min((x1 + x2) / 2, tx + lw)), max(ty, min((y1 + y2) / 2, ty + lh)))
        box_edge = (max(x1, min(anchor[0], x2)), max(y1, min(anchor[1], y2)))
        draw.line((anchor, box_edge), fill=color, width=max(1, line_width // 2))
        draw.rectangle(rect, fill=color)
        draw.text((tx + 4, ty + 4 - text_box[1]), label, fill="white", font=font)
    return result


class Localizer:
    """Load local weights once and reuse this object for subsequent images."""

    def __init__(self, weights=None, *, input_size=640, device="cpu"):
        selected = weights if weights is not None else os.environ.get("MG400_VISION_WEIGHTS")
        if not selected:
            raise FileNotFoundError(
                "Pass weights or set MG400_VISION_WEIGHTS to a local .pt or .onnx file. "
                "No model is downloaded during prediction."
            )
        self.weights = Path(selected).expanduser().resolve()
        if not self.weights.is_file():
            raise FileNotFoundError(f"Model weights file does not exist: {self.weights}")
        if self.weights.suffix.lower() not in {".pt", ".onnx"}:
            raise ValueError("Expected a trained .pt or .onnx model.")
        if input_size <= 0 or input_size % 32:
            raise ValueError("input_size must be a positive multiple of 32.")
        os.environ["YOLO_CONFIG_DIR"] = os.environ.get(
            "MG400_VISION_CACHE_DIR", "/var/lib/mg400/vision/yolo"
        )
        from ultralytics import YOLO

        self.model = YOLO(str(self.weights), task="segment")
        self.input_size = input_size
        self.device = device

    def predict(self, image, *, confidence=0.39):
        """Return (annotated PIL RGB image, JSON-serializable metadata)."""
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1.")
        started = time.perf_counter()
        original = load_image(image)
        width, height = original.size
        # Dataset v1 uses STRETCH to 640 x 640. Resize explicitly before YOLO's
        # usual letterbox preprocessing, then restore boxes to original pixels.
        resized = original.resize((self.input_size, self.input_size), Image.Resampling.BILINEAR)
        bgr = np.asarray(resized)[:, :, ::-1].copy()
        prediction = self.model.predict(
            source=bgr,
            imgsz=self.input_size,
            conf=confidence,
            device=self.device,
            verbose=False,
            save=False,
            rect=False,
        )[0]
        labels = {
            str(n).lower().replace(" ", "").replace("_", "") for n in prediction.names.values()
        }
        if labels != {"chip", "roboticarm"}:
            raise ValueError(
                f"Wrong model classes: {prediction.names}. Expected Chip and Robotic Arm."
            )
        boxes = prediction.boxes
        detections = (
            []
            if boxes is None
            else map_detections(
                boxes.xyxy.cpu().numpy(),
                boxes.cls.cpu().numpy(),
                boxes.conf.cpu().numpy(),
                prediction.names,
                width,
                height,
                self.input_size,
                confidence,
                polygons=None if prediction.masks is None else prediction.masks.xy,
            )
        )
        annotated = draw_detections(original, detections)
        metadata = {
            "weights": str(self.weights),
            "image_size": {"width": width, "height": height},
            "preprocessing": f"EXIF orientation, RGB, stretch {self.input_size}x{self.input_size}",
            "confidence_threshold": confidence,
            "coordinate_system": "oriented_original_image_pixels",
            "counts": dict(Counter(d.label for d in detections)),
            "detections": [asdict(d) for d in detections],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "engine_speed_ms": prediction.speed,
        }
        return annotated, metadata

    def annotate(self, image, output, *, confidence=0.39, save_json=True):
        output = Path(output)
        if isinstance(image, (str, Path)) and Path(image).resolve() == output.resolve():
            raise ValueError("Output must differ from the original input image.")
        annotated, metadata = self.predict(image, confidence=confidence)
        output.parent.mkdir(parents=True, exist_ok=True)
        annotated.save(output)
        metadata["output_image"] = str(output.resolve())
        if isinstance(image, (str, Path)):
            metadata["input_image"] = str(Path(image).resolve())
        if save_json:
            output.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return metadata


def annotate_image(input_image, output_image, *, weights=None, confidence=0.39):
    """Simple file-in/file-out interface. Return the output path."""
    Localizer(weights).annotate(input_image, output_image, confidence=confidence)
    return Path(output_image).resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--confidence", type=float, default=0.39)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("Input image does not exist.")
    output = args.output or args.input.with_name(args.input.stem + "_annotated.png")
    try:
        metadata = Localizer(args.weights, device=args.device).annotate(
            args.input, output, confidence=args.confidence
        )
    except (FileNotFoundError, ValueError, ImportError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
