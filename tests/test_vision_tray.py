"""Synthetic RGB markers and supplied geometry only; never open a camera."""

import copy
import itertools
import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from vision import tray as tray_module
from vision.calibration import VALIDATION_INDICES, detect_markers
from vision.tray import (
    TrayCheckError,
    _fit_similarity,
    _sample_missing,
    _transform,
    cross_check_slots,
    load_tray,
    locate_tray,
    validate_tray,
)

DEVICE = "/dev/v4l/by-path/platform-offline-tray-video-index0"
CONFIG = Path(__file__).parents[1] / "config/trays/grooved-platform-v2.toml"


@pytest.fixture
def tray():
    return load_tray(CONFIG)


def _camera_model():
    training = [index for index in range(24) if index not in VALIDATION_INDICES]
    return {
        "schema": 1,
        "camera_device": DEVICE,
        "image_size": [1000, 1000],
        "camera_matrix": [[1200, 0, 500], [0, 1200, 500], [0, 0, 1]],
        "distortion": [0, 0, 0, 0, 0],
        "rotation": [math.pi, 0, 0],
        "translation": [-90, 90, 400],
        "tray_height_mm": 0,
        "distance_mm": 246,
        "nominal_tag_height_mm": 100,
        "nominal_distance_mm": 246,
        "rms_limit_mm": 1,
        "created_at": 1,
        "rms_mm": {"held_out": 0.1, "tray": 0.2},
        "errors_mm": {"held_out": [0.1] * 6, "tray": [0.2] * 3},
        "points": {
            "tag_flange_mm": [[0, 0, 0]] * 24,
            "tag_pixels": [[500, 500]] * 24,
            "tray_tip_xy_flange_z_mm": [[0, 0, 0]] * 3,
            "tray_pixels": [[500, 500]] * 3,
            "training_indices": training,
            "validation_indices": list(VALIDATION_INDICES),
            "inlier_indices": training,
        },
    }


@pytest.fixture
def calibration():
    return _camera_model()


def _project(points, calibration):
    return cv2.projectPoints(
        np.asarray(points, np.float64),
        np.asarray(calibration["rotation"], np.float64),
        np.asarray(calibration["translation"], np.float64),
        np.asarray(calibration["camera_matrix"], np.float64),
        np.asarray(calibration["distortion"], np.float64),
    )[0].reshape(-1, 2)


def _render(
    tray, calibration, *, missing=(), replacement=None, extra=(), rotated=0, mirrored=False
):
    image = np.full((1000, 1000, 3), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    markers = [(m["id"], m["center_mm"]) for m in tray["markers"]] + list(extra)
    for ident, center in markers:
        if ident in missing:
            continue
        printed_id = replacement.get(ident, ident) if replacement else ident
        marker = cv2.aruco.generateImageMarker(dictionary, printed_id, 120)
        if ident == 0:
            marker = np.rot90(marker, rotated).copy()
            if mirrored:
                marker = marker[:, ::-1].copy()
        corners = np.array([[-12, 12, 0], [12, 12, 0], [12, -12, 0], [-12, -12, 0]], float)
        corners[:, :2] += center
        target = _project(corners, calibration).astype(np.float32)
        transform = cv2.getPerspectiveTransform(
            np.array([[0, 0], [119, 0], [119, 119], [0, 119]], np.float32), target
        )
        warped = cv2.warpPerspective(marker, transform, (1000, 1000), borderValue=255)
        mask = np.zeros((1000, 1000), np.uint8)
        cv2.fillConvexPoly(mask, np.round(target).astype(np.int32), 255)
        image[mask != 0] = warped[mask != 0, None]
    return image


def _locate(image, calibration, tray, **kwargs):
    return locate_tray(image, calibration, tray, device=DEVICE, **kwargs)


def test_owner_geometry_has_all_32_bounded_grooves_and_no_chip_labels(tray):
    assert {m["id"]: m["center_mm"] for m in tray["markers"]} == {
        1: [15.15, 15.0],
        0: [162.65, 15.0],
        7: [15.15, 162.8],
        2: [162.65, 162.8],
    }
    assert tray["origin_marker_id"] == 1
    assert tray["marker_size_mm"] == 24
    assert tray["groove_width_mm"] == 4.85
    assert len(tray["slots"]) == 32
    for k, slot in enumerate(tray["slots"]):
        x = 5.975 + 5.35 * k
        low, high = (0, 177.8) if 5 <= k <= 26 else (30, 147.8)
        assert slot["id"] == f"groove_{k:02d}"
        assert np.asarray(slot["centerline_mm"]) == pytest.approx(np.array([[x, low], [x, high]]))
        assert slot["reference_mm"] == pytest.approx([x, 88.9])
        assert "label_area" not in slot


def test_real_detector_localizes_all_markers_and_returns_xy_segments(tray, calibration):
    result = _locate(_render(tray, calibration), calibration, tray)
    assert {marker["id"] for marker in result["markers"]} == {0, 1, 2, 7}
    assert result["missing_marker"] is None
    assert result["transform"]["scale"] == pytest.approx(1, abs=0.002)
    assert len(result["slots"]) == 32
    for slot in tray["slots"]:
        observed = result["slots"][slot["id"]]
        assert np.asarray(observed["centerline_xy_mm"]) == pytest.approx(
            np.asarray(slot["centerline_mm"]), abs=0.2
        )
        assert set(observed) == {"centerline_xy_mm", "reference_xy_mm", "width_mm"}


@pytest.mark.parametrize("missing", [0, 1, 2, 7])
def test_three_rendered_corners_suffice_without_inventing_fourth_measurement(
    tray, calibration, missing
):
    result = _locate(_render(tray, calibration, missing=[missing]), calibration, tray)
    assert len(result["markers"]) == 3
    diagnostic = result["missing_marker"]
    assert diagnostic["missing_id"] == missing
    assert diagnostic["status"] == "unavailable"
    assert diagnostic["used_for_pose"] is False
    assert len(result["slots"]) == 32


@pytest.mark.parametrize("missing", [0, 1, 2, 7])
def test_predicted_cells_match_a_real_rendered_marker_missed_by_detector(
    monkeypatch, tray, calibration, missing
):
    image = _render(tray, calibration)
    measured = detect_markers(image)
    monkeypatch.setattr(
        tray_module, "detect_markers", lambda _: [m for m in measured if m["id"] != missing]
    )
    result = _locate(image, calibration, tray)
    check = result["missing_marker"]
    assert check["status"] == "match"
    assert check["data_bit_errors"] == 0
    assert check["border_errors"] == 0
    assert check["used_for_pose"] is False
    assert len(result["markers"]) == 3


@pytest.mark.parametrize("rotation", [1, 2, 3])
def test_fourth_marker_rotation_is_not_a_mirror(monkeypatch, tray, calibration, rotation):
    image = _render(tray, calibration, rotated=rotation)
    measured = detect_markers(image)
    monkeypatch.setattr(
        tray_module, "detect_markers", lambda _: [m for m in measured if m["id"] != 0]
    )
    assert _locate(image, calibration, tray)["missing_marker"]["status"] == "match"


@pytest.mark.parametrize("variant", ["wrong_id", "mirror", "occluded"])
def test_invalid_fourth_is_diagnostic_and_does_not_replace_three_corner_pose(
    monkeypatch, tray, calibration, variant
):
    image = _render(
        tray,
        calibration,
        replacement={0: 10} if variant == "wrong_id" else None,
        mirrored=variant == "mirror",
    )
    measured = detect_markers(_render(tray, calibration, missing=[0]))
    monkeypatch.setattr(tray_module, "detect_markers", lambda _: measured)
    if variant == "occluded":
        point = _project([[162.65, 15, 0]], calibration)[0].astype(int)
        image[point[1] - 16 : point[1] + 16, point[0] - 16 : point[0] + 16] = 127
    result = _locate(image, calibration, tray)
    assert result["missing_marker"]["status"] != "match"
    assert len(result["markers"]) == 3
    assert len(result["slots"]) == 32


def test_actual_duplicate_corner_refuses_before_fitting(monkeypatch, tray, calibration):
    image = _render(tray, calibration, extra=[(1, [88, 88])])

    def forbidden_fit(*args):
        pytest.fail("The tray fit must not run after duplicate corner detections.")

    monkeypatch.setattr(tray_module, "_fit_similarity", forbidden_fit)
    with pytest.raises(ValueError, match="duplicate"):
        _locate(image, calibration, tray)


def test_two_actual_markers_cannot_localize(tray, calibration):
    with pytest.raises(ValueError, match="at least 3"):
        _locate(_render(tray, calibration, missing=[0, 1]), calibration, tray)


def test_inconsistent_fourth_measurement_is_not_dropped_to_force_a_fit(tray, calibration):
    displaced = copy.deepcopy(tray)
    next(marker for marker in displaced["markers"] if marker["id"] == 0)["center_mm"][0] -= 20
    image = _render(displaced, calibration)
    assert {marker["id"] for marker in detect_markers(image)} == {0, 1, 2, 7}
    with pytest.raises(ValueError, match="residual"):
        _locate(image, calibration, tray)


@pytest.mark.parametrize("variant", ["port", "size", "nonfinite", "rgb"])
def test_incompatible_model_or_image_refuses_before_detection(
    monkeypatch, tray, calibration, variant
):
    image = np.zeros((1000, 1000, 3), np.uint8)
    if variant == "port":
        calibration["camera_device"] += "-other"
    elif variant == "size":
        image = image[:-1]
    elif variant == "nonfinite":
        calibration["translation"][0] = float("nan")
    else:
        image = image[..., 0]
    monkeypatch.setattr(
        tray_module, "detect_markers", lambda _: pytest.fail("Validation must precede detection.")
    )
    with pytest.raises(ValueError):
        _locate(image, calibration, tray)


@pytest.fixture
def location(tray):
    return {
        "coordinate_system": "robot_xy_mm",
        "tolerance_mm": 2,
        "slots": {
            slot["id"]: {
                "centerline_xy_mm": slot["centerline_mm"],
                "reference_xy_mm": slot["reference_mm"],
            }
            for slot in tray["slots"]
        },
    }


def _evidence(location):
    stations = {ident: slot["reference_xy_mm"] for ident, slot in location["slots"].items()}
    chips = {ident: {"state": "empty"} for ident in stations}
    return stations, chips


def test_chip_y_is_measured_and_can_differ_from_station_y(location):
    stations, chips = _evidence(location)
    stations["groove_06"] = [38.075, 10]
    chips["groove_06"] = {"state": "present", "center_xy_mm": [38.075, 160]}
    report = cross_check_slots(location, taught_stations=stations, chips=chips)
    assert report["ok"] is True
    assert report["slots"]["groove_06"]["station_chip_distance_mm"] == 150
    assert report["slots"]["groove_06"]["chip_residual_mm"] == 0
    assert chips["groove_06"]["center_xy_mm"][1] == 160


def test_explicit_same_location_target_checks_full_xy_and_preserves_report(location):
    stations, chips = _evidence(location)
    chips["groove_06"] = {"state": "present", "center_xy_mm": [38.075, 160]}
    with pytest.raises(TrayCheckError) as caught:
        cross_check_slots(
            location, taught_stations=stations, chips=chips, same_location_slots=["groove_06"]
        )
    report = caught.value.report
    assert report["ok"] is False
    assert len(report["slots"]) == 32
    assert report["slots"]["groove_06"]["failures"] == ["same_location_xy_mismatch"]


def test_exact_two_mm_is_allowed_and_tighter_configurations_are_enforced(location):
    stations, chips = _evidence(location)
    stations["groove_00"] = [5.975, 88]
    chips["groove_00"] = {"state": "present", "center_xy_mm": [5.975, 90]}
    assert cross_check_slots(
        location, taught_stations=stations, chips=chips, same_location_slots=["groove_00"]
    )["ok"]
    with pytest.raises(TrayCheckError):
        cross_check_slots(
            location,
            taught_stations=stations,
            chips=chips,
            same_location_slots=["groove_00"],
            tolerance_mm=1,
        )
    location["tolerance_mm"] = 1
    chips["groove_00"] = {"state": "present", "center_xy_mm": [7.5, 88]}
    with pytest.raises(TrayCheckError):
        cross_check_slots(location, taught_stations=stations, chips=chips)


def test_same_location_requires_an_actual_observed_chip(location):
    stations, chips = _evidence(location)
    with pytest.raises(TrayCheckError) as caught:
        cross_check_slots(
            location, taught_stations=stations, chips=chips, same_location_slots=["groove_00"]
        )
    assert caught.value.report["slots"]["groove_00"]["missing"] == ["same_location_position"]


@pytest.mark.parametrize("kind", ["station", "chip"])
@pytest.mark.parametrize("point", [[8.0, 80], [5.975, 27.9], [7.5, 28.5]])
def test_cross_checks_fail_for_perpendicular_and_end_distance(location, kind, point):
    stations, chips = _evidence(location)
    if kind == "station":
        stations["groove_00"] = point
    else:
        chips["groove_00"] = {"state": "present", "center_xy_mm": point}
    with pytest.raises(TrayCheckError) as caught:
        cross_check_slots(location, taught_stations=stations, chips=chips)
    item = caught.value.report["slots"]["groove_00"]
    assert item[f"{kind}_residual_mm"] > 2
    assert item["failures"] == [f"{kind}_outside_groove"]


@pytest.mark.parametrize("missing", ["station", "chip", "explicit_unknown"])
def test_missing_evidence_is_incomplete_not_empty_or_success(location, missing):
    stations, chips = _evidence(location)
    if missing == "station":
        del stations["groove_00"]
    elif missing == "chip":
        del chips["groove_00"]
    else:
        chips["groove_00"] = {"state": "unknown"}
    with pytest.raises(TrayCheckError) as caught:
        cross_check_slots(location, taught_stations=stations, chips=chips)
    item = caught.value.report["slots"]["groove_00"]
    assert item["status"] == "incomplete"
    assert item["missing"]
    if missing != "station":
        assert item["chip_state"] == "unknown"


@pytest.mark.parametrize("invalid", [True, 0, -1, 2.001, float("nan"), float("inf"), 10**400])
def test_tolerance_is_finite_positive_and_never_above_two(location, invalid):
    stations, chips = _evidence(location)
    with pytest.raises(ValueError, match="at most 2"):
        cross_check_slots(location, taught_stations=stations, chips=chips, tolerance_mm=invalid)


def test_malformed_chip_and_unknown_slot_are_not_silently_ignored(location):
    stations, chips = _evidence(location)
    for observation in (
        None,
        {},
        {"state": "present"},
        {"state": "empty", "center_xy_mm": [1, 2]},
        {"state": "present", "center_xy_mm": [True, 2]},
    ):
        chips["groove_00"] = observation
        with pytest.raises(ValueError):
            cross_check_slots(location, taught_stations=stations, chips=chips)
    with pytest.raises(ValueError, match="unknown slot"):
        cross_check_slots(location, taught_stations={"absent": [1, 2]}, chips={})


def test_configuration_refuses_duplicates_degeneracy_and_midpoint_substitution(tray, tmp_path):
    variants = []
    value = copy.deepcopy(tray)
    value["markers"][0]["id"] = value["markers"][1]["id"]
    variants.append(value)
    value = copy.deepcopy(tray)
    value["tolerance_mm"] = 2.001
    variants.append(value)
    value = copy.deepcopy(tray)
    value["slots"][0]["reference_mm"][1] = 80
    variants.append(value)
    value = copy.deepcopy(tray)
    value["slots"][0]["centerline_mm"] = [[1, 2], [1, 2]]
    variants.append(value)
    value = copy.deepcopy(tray)
    for marker in value["markers"]:
        marker["center_mm"][1] = 1
    variants.append(value)
    for value in variants:
        with pytest.raises(ValueError):
            validate_tray(value)
    oversized = tmp_path / "large.toml"
    oversized.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        load_tray(oversized)


CORNERS = np.array([[15.15, 15], [162.65, 15], [15.15, 162.8], [162.65, 162.8]])


def similarity(angle=0.25, scale=1.1, translation=(0, 0)):
    return {
        "scale": scale,
        "rotation": [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]],
        "translation": list(translation),
        "residuals_mm": [],
    }


@pytest.mark.parametrize("indices", list(itertools.combinations(range(4), 3)) + [(0, 1, 2, 3)])
def test_any_three_or_all_four_recover_proper_similarity(indices):
    expected = similarity(0.47, 1.08, (-80, 55))
    source = CORNERS[list(indices)]
    target = _transform(source, expected)
    actual = _fit_similarity(source, target, 2)
    assert actual["scale"] == pytest.approx(expected["scale"])
    assert np.asarray(actual["rotation"]) == pytest.approx(np.asarray(expected["rotation"]))
    assert actual["translation"] == pytest.approx(expected["translation"])
    assert max(actual["residuals_mm"]) < 1e-10
    assert _transform(CORNERS, actual) == pytest.approx(_transform(CORNERS, expected))
    json.dumps(actual, allow_nan=False)


@pytest.mark.parametrize(
    "fault",
    [
        "two",
        "duplicate-source",
        "duplicate-target",
        "reflection",
        "collinear",
        "collapsed",
        "nonfinite",
        "mixed-bool",
        "fourth-outlier",
    ],
)
def test_bad_similarity_refuses_without_dropping_any_observation(fault):
    source = CORNERS.copy()
    target = _transform(source, similarity())
    if fault == "two":
        source, target = source[:2], target[:2]
    elif fault == "duplicate-source":
        source[1] = source[0]
    elif fault == "duplicate-target":
        target[1] = target[0]
    elif fault == "reflection":
        target[:, 0] *= -1
    elif fault == "collinear":
        target[:, 1] = 0
    elif fault == "collapsed":
        target *= 1e-14
    elif fault == "nonfinite":
        target[1, 0] = np.inf
    elif fault == "mixed-bool":
        target = target.tolist()
        target[1][0] = True
    elif fault == "fourth-outlier":
        target[-1] += [10, 0]
    with pytest.raises(ValueError):
        _fit_similarity(source, target, 2)


@pytest.mark.parametrize("tolerance", [True, 0, -1, 2.001, float("nan"), float("inf"), 10**400])
def test_similarity_tolerance_is_bounded_without_float_overflow(tolerance):
    with pytest.raises(ValueError):
        _fit_similarity(CORNERS, CORNERS, tolerance)


def test_similarity_scale_has_no_invented_physical_acceptance_window():
    expected = similarity(scale=2.5)
    fit = _fit_similarity(CORNERS, _transform(CORNERS, expected), 2)
    assert fit["scale"] == pytest.approx(2.5)


def scene_model():
    model = _camera_model()
    model.update(
        image_size=[640, 480],
        camera_matrix=[[800, 0, 320], [0, 780, 240], [0, 0, 1]],
        distortion=[-0.3, 0.08, 0, 0, 0],
        rotation=[math.pi - 0.45, 0.25, 0.1],
        translation=[-100, 80, 500],
    )
    return model


def draw(model, fit, identity=0, turns=0, mirror=False, center=(162.65, 15), size=24):
    # Render each physical cell polygon independently, including lens distortion.
    # No image-space similarity or sampling helper is used to create the image.
    width, height = model["image_size"]
    image = np.full((height, width, 3), 255, np.uint8)
    bits = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), identity, 6
    )
    bits = np.rot90(bits, turns)
    if mirror:
        bits = np.fliplr(bits)
    rotation = np.array(fit["rotation"])
    for row in range(6):
        for column in range(6):
            cell = size / 6
            local = (
                np.array(
                    [
                        [(column - 3) * cell, (3 - row) * cell],
                        [(column - 2) * cell, (3 - row) * cell],
                        [(column - 2) * cell, (2 - row) * cell],
                        [(column - 3) * cell, (2 - row) * cell],
                    ]
                )
                + center
            )
            xy = fit["scale"] * local @ rotation.T + fit["translation"]
            xyz = np.column_stack((xy, np.full(4, model["tray_height_mm"])))
            projected = cv2.projectPoints(
                xyz,
                np.array(model["rotation"], float),
                np.array(model["translation"], float),
                np.array(model["camera_matrix"], float),
                np.array(model["distortion"], float),
            )[0].reshape(-1, 2)
            cv2.fillConvexPoly(
                image, np.rint(projected).astype(np.int32), (int(bits[row, column]),) * 3
            )
    return Image.fromarray(image)


@pytest.mark.parametrize("turns", range(4))
def test_perspective_distorted_known_label_matches_all_proper_rotations(turns):
    model, fit = scene_model(), similarity()
    result = _sample_missing(draw(model, fit, turns=turns), model, fit, 0, [162.65, 15])
    assert result["status"] == "match", result
    assert result["rotation_deg"] == 90 * turns
    assert result["data_bit_errors"] == result["border_errors"] == 0
    assert result["used_for_pose"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fault", ["wrong-id", "mirror", "occluded", "blank"])
def test_bad_fourth_marker_is_only_a_diagnostic(fault):
    model, fit = scene_model(), similarity()
    image = draw(model, fit, identity=7 if fault == "wrong-id" else 0, mirror=fault == "mirror")
    if fault == "blank":
        image = Image.new("RGB", image.size, (140, 140, 140))
    elif fault == "occluded":
        pixels = np.asarray(image).copy()
        point = _sample_missing(image, model, fit, 0, [162.65, 15])["predicted_center_px"]
        x, y = np.rint(point).astype(int)
        pixels[y - 10 : y + 10, x - 30 : x + 30] = 255
        image = Image.fromarray(pixels)
    result = _sample_missing(image, model, fit, 0, [162.65, 15])
    assert result["status"] in {"mismatch", "unavailable"}, result
    assert result["used_for_pose"] is False
    if fault == "blank":
        assert result["reason"] == "low_contrast"
    elif fault in {"wrong-id", "mirror"}:
        assert result["data_bit_errors"] > 0
        assert result["border_errors"] == 0


def test_out_of_frame_has_no_wrapped_or_clamped_samples():
    model, fit = scene_model(), similarity()
    result = _sample_missing(Image.new("RGB", (640, 480)), model, fit, 0, [900, 900])
    assert result["status"] == "unavailable" and result["reason"] == "out_of_frame"
    assert result["data_bit_errors"] is result["border_errors"] is None


def test_insufficient_resolution_and_behind_camera_are_unavailable():
    model, fit = scene_model(), similarity()
    model["translation"][2] = 50000
    result = _sample_missing(Image.new("RGB", (640, 480)), model, fit, 0, [162.65, 15])
    assert result["reason"] == "insufficient_resolution"
    model["translation"][2] = -500
    result = _sample_missing(Image.new("RGB", (640, 480)), model, fit, 0, [162.65, 15])
    assert result["reason"] == "invalid_camera_depth_or_projection"


def test_nominal_tag_height_gauge_does_not_change_marker_projection():
    model, fit = scene_model(), similarity()
    image = draw(model, fit)
    before = _sample_missing(image, model, fit, 0, [162.65, 15])
    shifted = copy.deepcopy(model)
    shifted["nominal_tag_height_mm"] += 7
    shifted["tray_height_mm"] += 7
    rotation = cv2.Rodrigues(np.array(model["rotation"]))[0]
    shifted["translation"] = (np.array(model["translation"]) - rotation[:, 2] * 7).tolist()
    after = _sample_missing(image, shifted, fit, 0, [162.65, 15])
    assert before["status"] == after["status"] == "match"
    assert before["predicted_center_px"] == pytest.approx(after["predicted_center_px"], abs=1e-9)
    assert before["data_bit_errors"] == after["data_bit_errors"] == 0


def test_sampling_rejects_wrong_image_size_and_nonfinite_model():
    model, fit = scene_model(), similarity()
    with pytest.raises(ValueError, match="image size"):
        _sample_missing(Image.new("RGB", (320, 240)), model, fit, 0, [162.65, 15])
    model["translation"][0] = float("nan")
    with pytest.raises(ValueError):
        _sample_missing(Image.new("RGB", (640, 480)), model, fit, 0, [162.65, 15])
