"""The detector evaluation runs offline against a fake localizer."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/vision_eval.py"
# A 32 x 24 px box at (64, 48) in a 640 x 480 frame, as a YOLO polygon.
SQUARE = "0.1 0.1 0.15 0.1 0.15 0.15 0.1 0.15"


@pytest.fixture(scope="module")
def evaluation():
    spec = importlib.util.spec_from_file_location("vision_eval_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def detection(label, confidence, box):
    x1, y1, x2, y2 = box
    return {
        "label": label,
        "confidence": confidence,
        "xyxy": box,
        "center_xy": ((x1 + x2) / 2, (y1 + y2) / 2),
    }


class FakeLocalizer:
    def __init__(self, detections, weights=None):
        self.detections = detections
        self.weights = weights
        self.calls = []

    def predict(self, image, **options):
        self.calls.append(options)
        return None, {
            "image_size": {"width": 640, "height": 480},
            "preprocessing": "EXIF orientation, RGB, stretch 640x640",
            "confidence_threshold": options.get("confidence", 0.39),
            "detections": self.detections.get(Path(image).name, []),
        }


@pytest.fixture
def dataset(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "labels").mkdir()
    labels = {"a.jpg": f"0 {SQUARE}\n1 {SQUARE}\n", "b.jpg": f"1 {SQUARE}\n", "c.jpg": ""}
    for name, text in labels.items():
        (tmp_path / "images" / name).write_bytes(b"")
        (tmp_path / "labels" / name.replace(".jpg", ".txt")).write_text(text)
    (tmp_path / "images" / ".DS_Store").write_bytes(b"")
    return tmp_path


DETECTIONS = {
    "a.jpg": [
        detection("Chip", 0.9, (67.0, 52.0, 99.0, 76.0)),
        detection("Chip", 0.5, (64.0, 48.0, 96.0, 72.0)),
        detection("Robotic Arm", 0.8, (0.0, 0.0, 640.0, 480.0)),
    ],
    "b.jpg": [detection("Chip", 0.6, (300.0, 300.0, 320.0, 320.0))],
}


def test_labels_become_pixel_boxes(evaluation, tmp_path):
    path = tmp_path / "frame.txt"
    path.write_text(f"0 {SQUARE}\n\n1 0 0 1 0 1 1\n")
    labels = evaluation.read_labels(path, 640, 480)
    assert [class_id for class_id, _ in labels] == [0, 1]
    assert labels[0][1] == pytest.approx((64, 48, 96, 72))
    assert labels[1][1] == pytest.approx((0, 0, 640, 480))
    path.write_text("0 0.5 0.5 0.1 0.1\n")
    with pytest.raises(ValueError, match="frame.txt:1"):
        evaluation.read_labels(path, 640, 480)


def test_match_takes_the_most_confident_detection(evaluation):
    exact_first = DETECTIONS["a.jpg"][1::-1]
    errors, false_detections, missed = evaluation.match(exact_first, [(64, 48, 96, 72)])
    assert errors == pytest.approx([5.0])
    assert (false_detections, missed) == (1, 0)
    far = [detection("Chip", 0.9, (80.0, 60.0, 112.0, 84.0))]
    assert evaluation.match(far, [(64, 48, 96, 72)]) == ([], 1, 1)


def test_report_counts_chips_by_frame_group(evaluation, dataset):
    localizer = FakeLocalizer(DETECTIONS)
    frames, settings = evaluation.evaluate(dataset, localizer)
    assert localizer.calls == [{}, {}, {}]
    assert [f["group"] for f in frames] == ["chips", "arm", "empty"]
    text = evaluation.report(frames, settings=settings, px_per_mm=2.0)
    assert "stretch 640x640. Confidence threshold: 0.39." in text
    assert "| With chips | 1 | 1 | 2 | 1 | 1 | 0 |" in text
    assert "| Arm, no chips | 1 | 0 | 1 | 0 | 1 | 0 |" in text
    assert "| No labels | 1 | 0 | 0 | 0 | 0 | 0 |" in text
    assert "| All | 3 | 1 | 3 | 1 | 2 | 0 |" in text
    assert "Chip precision: 0.333 (1/3). Chip recall: 1.000 (1/1)." in text
    assert "| px | 5.00 | 5.00 | 5.00 | 5.00 |" in text
    assert "| mm (2.0 px/mm) | 2.50 | 2.50 | 2.50 | 2.50 |" in text
    assert "| `a.jpg` | 0 | 1 |\n| `b.jpg` | 0 | 1 |" in text


def test_command_passes_the_confidence_and_prints_the_weights(
    evaluation, dataset, tmp_path, monkeypatch, capsys
):
    weights = tmp_path / "model.pt"
    weights.write_bytes(b"weights")
    localizer = FakeLocalizer({}, weights)
    monkeypatch.setattr(evaluation, "Localizer", lambda path, device: localizer)
    evaluation.main([str(dataset), "--px-per-mm", "1.5", "--confidence", "0.5"])
    text = capsys.readouterr().out
    assert localizer.calls == [{"confidence": 0.5}] * 3
    assert f"`model.pt` (sha256 `{hashlib.sha256(b'weights').hexdigest()}`)" in text
    assert "Confidence threshold: 0.5." in text
    assert "Chip precision: n/a. Chip recall: 0.000 (0/1)." in text
    assert "Center error" not in text
    with pytest.raises(SystemExit):
        evaluation.main([str(dataset), "--px-per-mm", "0"])
