"""Turret profile geometry and storage, without camera or motor hardware."""

import base64
import copy
import json

import cv2
import numpy as np
import pytest

from microscope import turret_calibration as turret
from microscope.aruco_locator import detect, detect_objectives, marker_center


def image_with_markers(objective=0, *, reference=False, offset=0, duplicate=None):
    image = np.full((800, 1200), 240, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    def marker(ident, x, y, size):
        image[y : y + size, x : x + size] = cv2.aruco.generateImageMarker(dictionary, ident, size)

    if reference:
        marker(35, 40, 30, 150)
    if objective is not None:
        marker(objective, 650 + offset, 450, 80)
    if duplicate is not None:
        marker(duplicate, 350, 230, 100)
    return image


@pytest.fixture(scope="module")
def complete_profile():
    profile = turret.new_profile("Test turret", "side", 0)
    profile["captures"] = {str(i): turret.capture(image_with_markers(i), i) for i in range(4)}
    return profile


@pytest.mark.parametrize("focus_position", [None, 0, 30])
def test_complete_profile_uses_captures_without_requiring_recorded_focus(
    tmp_path, complete_profile, focus_position
):
    profile = dict(complete_profile, focus_position=focus_position)
    assert turret.is_complete(profile)
    locator = turret.ProfileLocator(profile)
    measurement = locator.locate(image_with_markers(2), target_id=2)
    assert measurement["profile_id"] == profile["profile_id"]
    assert measurement["markers"][0]["dx_px"] == pytest.approx(0)
    assert locator.profile["focus_position"] == focus_position
    store = turret.TurretProfileStore(tmp_path)
    store.save(profile)
    assert store.select(profile["profile_id"]) == profile
    assert turret.TurretProfileStore(tmp_path).selected() == profile
    summary = store.list()[0]
    assert summary["complete"] and summary["focus_position"] == focus_position


def test_capture_records_only_objective_coordinates():
    image = image_with_markers()
    record = turret.capture(image, 0)
    assert record["shape"] == [800, 1200]
    assert set(record) == {"shape", "objective_corners", "preview_jpeg"}
    assert marker_center(record["objective_corners"]) == pytest.approx([689.5, 489.5], abs=0.1)
    preview = cv2.imdecode(np.frombuffer(base64.b64decode(record["preview_jpeg"]), np.uint8), 0)
    assert max(preview.shape) == 640
    result = detect_objectives(image)
    assert "reference" not in result
    assert [marker["id"] for marker in result["markers"]] == [0]
    assert [marker["id"] for marker in detect(image)["markers"]] == [0]


@pytest.mark.parametrize(
    "options,status",
    [
        ({"objective": None}, "target_not_visible"),
        ({"objective": 1}, "target_not_visible"),
        ({"duplicate": 0}, "duplicate_ids"),
    ],
)
def test_capture_requires_unique_expected_objective(options, status):
    with pytest.raises(ValueError, match=status):
        turret.capture(image_with_markers(**options), 0)


@pytest.mark.parametrize("ident", [4, 35, 49])
def test_detector_ignores_non_objective_markers(ident):
    result = detect_objectives(image_with_markers(objective=ident, duplicate=ident))
    assert result["status"] == "not_detected"
    assert result["markers"] == []
    with pytest.raises(ValueError, match="Objective ID"):
        turret.capture(image_with_markers(), ident)


@pytest.mark.parametrize("blur_reference", [False, True])
def test_fixed_markers_do_not_affect_capture_or_alignment(complete_profile, blur_reference):
    image = image_with_markers(reference=True, duplicate=35)
    if blur_reference:
        image[:200, :250] = cv2.GaussianBlur(image[:200, :250], (65, 65), 15)
    record = turret.capture(image, 0)
    assert record["objective_corners"] == complete_profile["captures"]["0"]["objective_corners"]
    result = turret.ProfileLocator(complete_profile).locate(image, 0)
    assert result["status"] == "ok"
    assert result["markers"][0]["dx_px"] == pytest.approx(0, abs=0.01)
    assert result["markers"][0]["dy_px"] == pytest.approx(0, abs=0.01)


def detection_for(profile, displacement=(0, 0), ident=0, shape=(800, 1200)):
    record = profile["captures"][str(ident)]
    # Displacement uses width-3840 pixels, independent of either image size.
    corners = (np.array(record["objective_corners"]) + 0.5) * shape[1] / record["shape"][1] - 0.5
    corners += np.array(displacement) * shape[1] / 3840
    corners = corners.tolist()
    return {
        "shape": list(shape),
        "status": "ok",
        "markers": [
            {
                "id": ident,
                "color": "red",
                "corners": corners,
                "center": marker_center(corners).tolist(),
            }
        ],
    }


@pytest.mark.parametrize(
    "displacement,direction",
    [
        ((9.6, -6.4), "right"),
        ((-9.6, 6.4), "left"),
        ((0, 0), "zero"),
    ],
)
def test_locator_compares_saved_image_coordinates(
    complete_profile, monkeypatch, displacement, direction
):
    detection = detection_for(complete_profile, displacement=displacement)
    monkeypatch.setattr(turret, "detect_objectives", lambda *_a, **_k: detection)
    result = turret.ProfileLocator(complete_profile).locate(image_with_markers(), 0)
    assert result["status"] == "ok"
    assert result["offset_width_px"] == 3840
    marker = result["markers"][0]
    assert marker["dx_px"] == pytest.approx(displacement[0], abs=0.005)
    assert marker["dy_px"] == pytest.approx(displacement[1], abs=0.005)
    assert marker["direction"] == direction


@pytest.mark.parametrize("capture_scale", [0.5, 1, 2])
@pytest.mark.parametrize("frame_scale", [0.5, 1, 2])
@pytest.mark.parametrize("offset", [-5, 0, 5])
def test_resolution_does_not_change_five_pixel_tolerance(
    complete_profile, monkeypatch, capture_scale, frame_scale, offset
):
    profile = copy.deepcopy(complete_profile)
    for record in profile["captures"].values():
        record["shape"] = [round(value * capture_scale) for value in record["shape"]]
        record["objective_corners"] = (
            (np.array(record["objective_corners"]) + 0.5) * capture_scale - 0.5
        ).tolist()
    shape = (round(800 * frame_scale), round(1200 * frame_scale))
    detection = detection_for(profile, displacement=(offset, 0.0), shape=shape)
    monkeypatch.setattr(turret, "detect_objectives", lambda *_a, **_k: detection)
    result = turret.ProfileLocator(profile).locate(image_with_markers(), 0)
    assert result["markers"][0]["dx_px"] == pytest.approx(offset, abs=0.005)


@pytest.mark.parametrize("offset", [-25, 25])
def test_actual_images_report_camera_motion_as_alignment_error(complete_profile, offset):
    transform = np.array([[1.0, 0, offset], [0, 1.0, 18.0]])
    moved = cv2.warpAffine(
        image_with_markers(reference=True), transform, (1200, 800), borderValue=240
    )
    result = turret.ProfileLocator(complete_profile).locate(moved, 0)
    assert result["status"] == "ok"
    assert result["markers"][0]["dx_px"] == pytest.approx(offset * 3.2, abs=0.1)
    assert result["markers"][0]["dy_px"] == pytest.approx(18 * 3.2, abs=0.1)


@pytest.mark.parametrize("target_id", [4, 35, True, 0.0])
def test_profile_locator_rejects_unknown_or_invalid_ids(complete_profile, target_id):
    with pytest.raises(ValueError, match="Unknown objective"):
        turret.ProfileLocator(complete_profile).locate(image_with_markers(), target_id)


@pytest.mark.parametrize("objective", [None, 1])
def test_locator_reports_missing_target_without_offsets(complete_profile, objective):
    result = turret.ProfileLocator(complete_profile).locate(image_with_markers(objective), 0)
    assert result["status"] == "target_not_visible"
    assert result["markers"] == []


def test_locator_preserves_other_objectives_only_when_requested(complete_profile):
    image = image_with_markers(0, duplicate=1)
    locator = turret.ProfileLocator(complete_profile)
    assert [marker["id"] for marker in locator.locate(image, 0)["markers"]] == [0]
    assert [marker["id"] for marker in locator.locate(image, 0, only_target=False)["markers"]] == [
        0,
        1,
    ]


def test_stable_capture_rejects_motion():
    image = image_with_markers()
    assert turret.stable_capture([image, image.copy(), image.copy()], 0)["shape"] == [800, 1200]
    with pytest.raises(ValueError, match="moved during"):
        turret.stable_capture([image, image, image_with_markers(offset=4)], 0)
    with pytest.raises(ValueError, match="at least 3"):
        turret.stable_capture([image], 0)


def test_stable_capture_does_not_correct_camera_motion():
    image = image_with_markers(reference=True)
    transform = np.array([[1.0, 0, 4.0], [0, 1.0, 0]])
    moved = cv2.warpAffine(image, transform, (1200, 800), borderValue=240)
    with pytest.raises(ValueError, match="moved during"):
        turret.stable_capture([image, image, moved], 0)


def test_stable_capture_normalizes_image_resolution(complete_profile, monkeypatch):
    record = complete_profile["captures"]["0"]
    smaller = copy.deepcopy(record)
    smaller["shape"] = [400, 600]
    smaller["objective_corners"] = (
        (np.array(record["objective_corners"]) + 0.5) / 2 - 0.5
    ).tolist()
    records = iter([record, smaller, record])
    monkeypatch.setattr(turret, "capture", lambda *_args: next(records))
    assert turret.stable_capture([None] * 3, 0) == record


def test_stable_capture_rejects_image_aspect_change(complete_profile, monkeypatch):
    record = complete_profile["captures"]["0"]
    records = iter([record, record, dict(record, shape=[800, 1000])])
    monkeypatch.setattr(turret, "capture", lambda *_args: next(records))
    with pytest.raises(ValueError, match="aspect ratios"):
        turret.stable_capture([None] * 3, 0)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=3),
        lambda p: p.update(schema_version=True),
        lambda p: p.update(profile_id="../escape"),
        lambda p: p.update(focus_position=float("nan")),
        lambda p: p.update(focus_position=True),
        lambda p: p.update(camera={"name": "side"}),
        lambda p: p.update(transform=np.eye(3).tolist()),
        lambda p: p.update(dictionary="DICT_5X5_50"),
        lambda p: p.update(reference={"id": 35}),
        lambda p: p["captures"].update({"35": p["captures"]["0"]}),
        lambda p: p["captures"]["0"].update(shape=[800.0, 1200]),
        lambda p: p["captures"]["0"].update(objective_corners=[[1, 1]] * 4),
        lambda p: p["captures"]["0"]["objective_corners"][0].__setitem__(0, float("inf")),
        lambda p: p["captures"]["0"]["objective_corners"][0].__setitem__(0, -2),
        lambda p: p["captures"]["0"].update(reference_corners=[[1, 1]] * 4),
        lambda p: p["captures"]["0"].update(preview_jpeg="not-a-jpeg"),
    ],
)
def test_profile_rejects_invalid_fields(complete_profile, mutation):
    profile = copy.deepcopy(complete_profile)
    mutation(profile)
    with pytest.raises(ValueError):
        turret.validate_profile(profile)


def legacy_profile(profile):
    legacy = copy.deepcopy(profile)
    legacy["schema_version"] = 1
    legacy["reference"] = {"dictionary": legacy.pop("dictionary"), "id": 35, "side_mm": 30.0}
    for record in legacy["captures"].values():
        record["reference_corners"] = [[40, 30], [189, 30], [189, 179], [40, 179]]
    return legacy


def test_legacy_profile_migrates_on_read_without_rewriting_file(tmp_path, complete_profile):
    legacy = legacy_profile(complete_profile)
    path = tmp_path / f"{legacy['profile_id']}.json"
    original = json.dumps(legacy).encode()
    path.write_bytes(original)
    selection = {"ever_selected": True, "profile_id": legacy["profile_id"]}
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection))
    store = turret.TurretProfileStore(tmp_path)

    assert store.load(legacy["profile_id"]) == complete_profile
    assert store.selected() == complete_profile
    assert json.loads(store.export(legacy["profile_id"])) == complete_profile
    assert store.list()[0]["profile_id"] == legacy["profile_id"]
    result = turret.ProfileLocator(legacy).locate(image_with_markers(), 0)
    assert result["status"] == "ok"
    assert result["markers"][0]["dx_px"] == pytest.approx(0, abs=0.01)
    assert path.read_bytes() == original
    assert json.loads(selection_path.read_text()) == selection
    assert "reference_corners" in legacy["captures"]["0"]

    saved = store.save(legacy)
    assert saved == complete_profile
    assert json.loads(path.read_bytes()) == complete_profile
    assert store.selection_status() == selection


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["reference"].update(id=0),
        lambda p: p["reference"].update(id=35.0),
        lambda p: p["reference"].update(side_mm=29),
        lambda p: p["reference"].update(dictionary="DICT_5X5_50"),
        lambda p: p["captures"]["0"].pop("reference_corners"),
        lambda p: p["captures"]["0"].update(reference_corners=[[1, 1]] * 4),
        lambda p: p["captures"]["0"]["reference_corners"][0].__setitem__(0, float("inf")),
        lambda p: p["captures"]["0"]["reference_corners"][0].__setitem__(0, -2),
        lambda p: p["captures"]["0"].update(shape=[600, 1200]),
    ],
)
def test_legacy_profile_is_validated_before_migration(complete_profile, mutation):
    profile = legacy_profile(complete_profile)
    mutation(profile)
    with pytest.raises(ValueError):
        turret.validate_profile(profile)


def test_profile_rejects_large_preview_before_image_decode(complete_profile, monkeypatch):
    profile = copy.deepcopy(complete_profile)
    # A small JPEG header advertises an image beyond the preview bound.
    jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x10\x00\x10\x00\x01\x01\x11\x00\xff\xd9"
    profile["captures"]["0"]["preview_jpeg"] = base64.b64encode(jpeg).decode()
    monkeypatch.setattr(cv2, "imdecode", lambda *_args: pytest.fail("Decoded oversized JPEG"))
    with pytest.raises(ValueError, match="640 pixels"):
        turret.validate_profile(profile)


def test_store_round_trip_drafts_selection_and_delete(tmp_path, complete_profile):
    store = turret.TurretProfileStore(tmp_path)
    assert store.selection_status() == {"ever_selected": False, "profile_id": None}
    draft = store.save(turret.new_profile("Draft", "side"))
    with pytest.raises(ValueError, match="incomplete"):
        store.select(draft["profile_id"])
    store.save(complete_profile)
    assert store.select(complete_profile["profile_id"]) == complete_profile
    assert turret.TurretProfileStore(tmp_path).selected() == complete_profile
    exported = store.export(complete_profile["profile_id"])
    imported = turret.TurretProfileStore(tmp_path / "imported").upload(exported)
    assert imported["profile_id"] != complete_profile["profile_id"]
    assert dict(imported, profile_id=complete_profile["profile_id"]) == complete_profile
    summaries = store.list()
    assert sorted(item["complete"] for item in summaries) == [False, True]
    assert all(item["updated_at"] for item in summaries)
    store.delete(complete_profile["profile_id"])
    assert store.selection_status() == {"ever_selected": True, "profile_id": None}
    assert store.selected() is None


def test_missing_selected_profile_never_looks_unselected(tmp_path, complete_profile):
    store = turret.TurretProfileStore(tmp_path)
    store.save(complete_profile)
    store.select(complete_profile["profile_id"])
    (tmp_path / f"{complete_profile['profile_id']}.json").unlink()
    with pytest.raises(ValueError, match="selected turret profile is missing"):
        store.selected()
    assert store.selection_status()["ever_selected"]


def test_failed_atomic_save_preserves_previous_profile(tmp_path, complete_profile, monkeypatch):
    store = turret.TurretProfileStore(tmp_path)
    store.save(complete_profile)
    changed = dict(complete_profile, name="Replacement")

    def reject(*_args):
        raise OSError("Replace failed")

    monkeypatch.setattr(turret.os, "replace", reject)
    with pytest.raises(OSError, match="Replace failed"):
        store.save(changed)
    assert store.load(complete_profile["profile_id"])["name"] == complete_profile["name"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "data",
    [
        b"x" * (turret.MAX_UPLOAD_BYTES + 1),
        b"{}",
        b"[]",
        b"\xff",
        b'{"schema_version":1,"schema_version":1}',
    ],
)
def test_bad_upload_does_not_write_files(tmp_path, data):
    store = turret.TurretProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.upload(data)
    assert not list(tmp_path.iterdir())


def test_upload_rejects_nonfinite_json(tmp_path, complete_profile):
    profile = dict(complete_profile, focus_position=float("inf"))
    with pytest.raises(ValueError, match="finite"):
        turret.TurretProfileStore(tmp_path).upload(json.dumps(profile).encode())


def test_store_rejects_path_escape(tmp_path):
    store = turret.TurretProfileStore(tmp_path)
    with pytest.raises(ValueError, match="profile ID"):
        store.load("../outside")


def test_locator_rejects_different_image_aspect_ratio(complete_profile):
    image = cv2.resize(image_with_markers(), (1000, 800))
    result = turret.ProfileLocator(complete_profile).locate(image, 0)
    assert result["status"] == "image_size_mismatch"
    assert not result["markers"]


def test_failed_delete_retains_selected_profile(tmp_path, complete_profile, monkeypatch):
    store = turret.TurretProfileStore(tmp_path)
    store.save(complete_profile)
    store.select(complete_profile["profile_id"])
    original_unlink = turret.Path.unlink

    def reject_selected(path, *args, **kwargs):
        if path.name == f"{complete_profile['profile_id']}.json":
            raise OSError("Delete failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(turret.Path, "unlink", reject_selected)
    with pytest.raises(OSError, match="Delete failed"):
        store.delete(complete_profile["profile_id"])
    assert store.selected() == complete_profile


def test_upload_collision_preserves_selected_profile(tmp_path, complete_profile):
    store = turret.TurretProfileStore(tmp_path)
    store.save(complete_profile)
    store.select(complete_profile["profile_id"])
    duplicate = dict(complete_profile, name="Uploaded copy")
    uploaded = store.upload(json.dumps(duplicate).encode())
    assert uploaded["profile_id"] != complete_profile["profile_id"]
    assert store.selected() == complete_profile
