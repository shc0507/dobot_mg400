"""Exercise Digital Twin routes with camera frames and isolated profile storage."""

import base64
import copy
import json
from hashlib import sha256
from http.client import HTTPConnection
from io import BytesIO
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import cv2
import numpy as np
import pytest
from flask import Blueprint, Flask, jsonify, request
from PIL import Image
from werkzeug.exceptions import HTTPException

from orchestrator.web import twin
from orchestrator.web.workspaces import twin as workspace
from vision.frames import FrameError, Go2RTCSource
from vision.stage_twin import TuningPolicy, analyze_frame, new_profile


class Frames:
    def __init__(self):
        self.data = twin.CameraFrames({}, demo=True).read("camera1")
        self.reads = []

    def cameras(self):
        return [{"id": "camera1", "label": "Stage"}, {"id": "camera2", "label": "Insert rail"}]

    def identity(self, camera):
        if not isinstance(camera, str) or camera not in ("camera1", "camera2"):
            raise ValueError("Choose a configured Console camera.")
        return "device:" + camera

    def read(self, camera):
        self.identity(camera)
        self.reads.append(camera)
        return self.data

    def sample(self, camera, dimensions=None):
        return self.read(camera)


def marker_frame(markers):
    pixels = np.full((480, 640), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for marker, left, top in markers:
        pixels[top : top + 60, left : left + 60] = cv2.aruco.generateImageMarker(
            dictionary, marker, 60
        )
    return cv2.imencode(".jpg", pixels)[1].tobytes()


def calibrate_x(manager, frames, page):
    for endpoint, x in (("x_low", 0), ("x_high", 90)):
        frames.data = marker_frame(((10, 40, 40), (14, 180, 160), (18, 320 + x, 160)))
        assert manager.capture(page, {"endpoint": endpoint, "automatic": False})["accepted"]
    assert page.profile["axis_status"]["x"]["status"] == "calibrated"


@pytest.fixture
def console(tmp_path):
    frames = Frames()
    return twin.TwinConsole(tmp_path / "profiles", frames), frames


def app_for(service):
    """Mount the actual workspace on a small app with Console error status codes."""
    app = Flask(__name__)
    app.testing = True
    blueprint = Blueprint("twin_test", __name__, url_prefix="/twin")
    workspace.register(blueprint, service)
    app.register_blueprint(blueprint)

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(RuntimeError)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(OSError)
    def storage(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    return app


@pytest.fixture
def routes(tmp_path, monkeypatch):
    frames = Frames()
    monkeypatch.setattr(workspace, "CameraFrames", lambda settings, demo: frames)
    service = SimpleNamespace(demo=True, temporary=SimpleNamespace(name=str(tmp_path)), settings={})
    return app_for(service).test_client(), frames, tmp_path / "calibration" / "twin"


def headers(client="page-a"):
    return {"X-MG400-Client": client}


def profile_from(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["calibration"]["profile"]


def test_default_stage_camera_and_partial_profile_resume_across_restart(console):
    manager, frames = console
    page = manager.page("page-a")
    assert page.camera == "camera1"
    assert page.profile["camera"]["device_id"] == "device:camera1"
    captured = manager.capture(page, {"endpoint": "x_low", "automatic": False})
    assert captured["accepted"] is True
    assert page.profile["axes"] == {}
    saved = manager.mutate(page, "profile/save", {"name": "Partial calibration"})
    identifier = saved["selected_profile"]
    restarted = twin.TwinConsole(manager.store.root, frames)
    resumed = restarted.page("page-b")
    assert resumed.selected == identifier
    assert resumed.profile["name"] == "Partial calibration"
    assert resumed.profile["endpoints"] == page.profile["endpoints"]
    assert resumed.profile["axis_status"]["x"]["status"] == "pending"


def test_measure_image_hash_and_detections_refer_to_the_same_frame(routes):
    client, frames, root = routes
    response = client.post("/twin/measure", json={}, headers=headers())
    assert response.status_code == 200
    report = response.get_json()
    payload = base64.b64decode(report["image"].split(",", 1)[1])
    assert payload == frames.data
    assert report["image_sha256"] == sha256(payload).hexdigest()
    assert report["dimensions"] == [640, 480]
    assert {marker["id"] for marker in report["detections"]} == {10, 14, 18, 22}
    assert all(axis["value_mm"] is None for axis in report["axes"].values())
    assert frames.reads == ["camera1"]
    assert not root.exists()


def test_endpoint_capture_save_reload_and_measure_use_actual_marker_positions(console):
    manager, frames = console
    page = manager.page("page-a")

    calibrate_x(manager, frames, page)
    manager.mutate(page, "profile/save", {})
    restarted = twin.TwinConsole(manager.store.root, frames)
    frames.data = marker_frame(((10, 40, 40), (14, 180, 160), (18, 365, 160)))
    report = restarted.measure(restarted.page("page-b"), {})
    assert report["axes"]["x"]["value_mm"] == pytest.approx(22.5, abs=0.1)
    assert report["axes"]["x"]["status"] == "provisional_interpolation"
    assert report["axes"]["y"]["value_mm"] is None


def test_profiles_cannot_cross_camera_identity(console):
    manager, _ = console
    page = manager.page("page-a")
    saved = manager.mutate(page, "profile/save", {})["selected_profile"]
    manager.mutate(page, "source", {"camera": "camera2"})
    assert manager.state(page)["profiles"] == []
    original = copy.deepcopy(page.profile)
    with pytest.raises(ValueError, match="another camera"):
        manager.mutate(page, "profile/select", {"id": saved})
    assert page.profile == original
    assert page.selected is None
    manager.mutate(page, "source", {"camera": "camera1"})
    assert page.selected == saved


def test_page_drafts_do_not_change_another_pages_active_profile(console):
    manager, _ = console
    first, second = manager.page("first"), manager.page("second")
    original = copy.deepcopy(second.profile)
    manager.mutate(
        first,
        "profile/edit",
        {"name": "First page", "travel_mm": {"x": 46, "y": 40, "insert": 150}},
    )
    manager.capture(first, {"endpoint": "x_low", "automatic": False})
    manager.mutate(first, "profile/save", {})
    assert second.profile == original
    assert second.selected is None
    assert manager.state(second)["profiles"][0]["name"] == "First page"


def test_failed_capture_preserves_prior_endpoint_and_returns_current_image(console):
    manager, frames = console
    page = manager.page("page-a")
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    original = copy.deepcopy(page.profile)
    frames.data = cv2.imencode(".jpg", np.full((480, 640), 255, np.uint8))[1].tobytes()
    result = manager.capture(page, {"endpoint": "x_low", "automatic": False})
    assert result["accepted"] is False
    assert result["sample"]["detections"] == []
    assert result["sample"]["image_sha256"] == sha256(frames.data).hexdigest()
    assert page.profile == original


def test_new_partial_capture_is_retained_and_can_resume_after_save(console):
    manager, frames = console
    page = manager.page("page-a")
    frames.data = marker_frame(((18, 320, 160),))
    result = manager.capture(page, {"endpoint": "x_low", "automatic": False})
    assert result["accepted"] is True
    assert result["tuning"]["usable"] is False
    assert "reference" in result["tuning"]["reason"]
    assert page.profile["endpoints"]["x_low"]["detections"][0]["id"] == 18
    assert page.profile["endpoint_status"]["x_low"]["status"] == "error"
    manager.mutate(page, "profile/save", {})
    restarted = twin.TwinConsole(manager.store.root, frames)
    assert restarted.page("resumed").profile["endpoints"] == page.profile["endpoints"]


@pytest.mark.parametrize("automatic", [False, True])
def test_partial_resnap_cannot_replace_a_usable_endpoint(console, automatic):
    manager, frames = console
    page = manager.page("page-a")
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    original = copy.deepcopy(page.profile)
    frames.data = marker_frame(((18, 320, 160),))
    result = manager.capture(page, {"endpoint": "x_low", "automatic": automatic, "max_frames": 1})
    assert result["accepted"] is False
    assert result["tuning"]["usable"] is False
    assert "Previous capture kept" in result["tuning"]["reason"]
    assert page.profile == original


@pytest.mark.parametrize("failure", ["different_pair", "same_position"])
def test_usable_resnap_cannot_destroy_a_calibrated_axis(console, failure):
    manager, frames = console
    page = manager.page("page-a")
    calibrate_x(manager, frames, page)
    original = copy.deepcopy(page.profile)
    markers = (
        ((10, 40, 40), (15, 180, 160), (19, 410, 160))
        if failure == "different_pair"
        else ((10, 40, 40), (14, 180, 160), (18, 320, 160))
    )
    frames.data = marker_frame(markers)
    result = manager.capture(page, {"endpoint": "x_high", "automatic": False})
    assert result["accepted"] is False
    assert result["tuning"]["usable"] is True
    assert "Previous capture kept" in result["tuning"]["reason"]
    assert page.profile == original
    manager.mutate(page, "clear", {"endpoint": "x_high"})
    assert "x" not in page.profile["axes"]


def test_capture_prefers_a_matching_pair_over_a_frame_with_more_markers(console, monkeypatch):
    manager, frames = console
    page = manager.page("page-a")
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    fitting = marker_frame(((10, 40, 40), (14, 180, 160), (18, 410, 160)))
    incompatible = marker_frame(
        (
            (10, 40, 40),
            (11, 140, 40),
            (12, 240, 40),
            (15, 180, 160),
            (16, 180, 300),
            (19, 410, 160),
            (20, 500, 300),
        )
    )
    pending = iter((fitting, incompatible))
    monkeypatch.setattr(frames, "read", lambda camera: next(pending))
    result = manager.capture(page, {"endpoint": "x_high", "max_frames": 2})
    assert result["accepted"] is True
    assert result["tuning"]["tested"] == 2
    assert result["sample"]["image_sha256"] == sha256(fitting).hexdigest()
    assert page.profile["axis_status"]["x"]["status"] == "calibrated"


def test_monitor_uses_saved_snapshot_until_draft_is_saved(console):
    manager, frames = console
    page = manager.page("page-a")
    calibrate_x(manager, frames, page)
    frames.data = marker_frame(((10, 40, 40), (14, 180, 160), (18, 365, 160)))
    assert manager.measure(page, {})["axes"]["x"]["value_mm"] is None
    manager.mutate(page, "profile/save", {})
    saved = copy.deepcopy(page.active_profile)
    policy = page.policy
    other = manager.page("other-page")
    manager.mutate(page, "profile/edit", {"travel_mm": {"x": 90, "y": 40, "insert": 150}})
    assert page.profile["axes"]["x"]["max"] == 90
    assert page.active_profile == saved
    assert page.policy is policy
    assert manager.measure(page, {})["axes"]["x"]["value_mm"] == pytest.approx(22.5, abs=0.1)
    manager.mutate(page, "profile/save", {})
    assert page.policy is not policy
    assert manager.measure(page, {})["axes"]["x"]["value_mm"] == pytest.approx(45, abs=0.1)
    assert other.active_profile == saved
    assert manager.measure(other, {})["axes"]["x"]["value_mm"] == pytest.approx(22.5, abs=0.1)
    active = copy.deepcopy(page.active_profile)
    selected, policy = page.selected, page.policy
    manager.mutate(page, "profile/new", {"name": "Unsaved replacement"})
    assert page.selected == selected
    assert page.active_profile == active
    assert manager.state(page)["active_profile"] == active
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    manager.mutate(page, "clear", {"endpoint": "x_low"})
    assert page.policy is policy
    assert page.active_profile == active
    assert manager.measure(page, {})["profile_id"] == selected


def test_dimension_change_does_not_replace_calibration_or_measurements(console):
    manager, frames = console
    page = manager.page("page-a")
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    manager.mutate(page, "profile/save", {})
    original = copy.deepcopy(page.profile)
    frames.data = cv2.imencode(".jpg", np.full((600, 800), 255, np.uint8))[1].tobytes()
    with pytest.raises(ValueError, match="resolution differs"):
        manager.capture(page, {"endpoint": "x_high", "automatic": False})
    with pytest.raises(ValueError, match="requires 640 x 480"):
        manager.measure(page, {})
    assert page.profile == original


def test_processing_busy_does_not_read_camera(console):
    manager, frames = console
    page = manager.page("page-a")
    with manager.processing():
        with pytest.raises(RuntimeError, match="processing a frame"):
            manager.measure(page, {})
        with pytest.raises(RuntimeError, match="processing a frame"):
            manager.capture(page, {"endpoint": "x_low"})
    assert frames.reads == []
    assert manager.measure(page, {})["camera"] == "camera1"


def test_cancel_tuning_route_preserves_profile_and_does_not_read_camera(console, monkeypatch):
    manager, frames = console
    page = manager.page("page-a")
    policy = page.policy = TuningPolicy(("x",), automatic=False)
    assert policy.start_manual()
    before = copy.deepcopy(page.profile)
    monkeypatch.setattr(workspace, "TwinConsole", lambda root, frames: manager)
    client = app_for(SimpleNamespace(demo=False, settings={})).test_client()
    invalid = client.post("/twin/tuning/cancel", json={"tune": False}, headers=headers())
    assert invalid.status_code == 400
    assert policy.searching
    with manager.processing():
        response = client.post("/twin/tuning/cancel", json={}, headers=headers())
    assert profile_from(response) == before
    assert page.profile == before
    assert page.policy is policy
    assert policy.snapshot()["outcome"] == "cancelled"
    assert not policy.searching
    assert frames.reads == []
    assert client.post("/twin/tuning/cancel", json={}, headers=headers()).status_code == 200


def test_failed_atomic_write_preserves_saved_profile_and_selection(console, monkeypatch):
    manager, _ = console
    page = manager.page("page-a")
    manager.mutate(page, "profile/save", {"name": "Saved"})
    profile = copy.deepcopy(page.profile)
    paths = list(manager.store.root.rglob("*.json"))
    before = {path: path.read_bytes() for path in paths}

    def fail_replace(*args):
        raise OSError("Disk write failed")

    monkeypatch.setattr(twin.os, "replace", fail_replace)
    with pytest.raises(OSError, match="Disk write"):
        manager.mutate(page, "profile/save", {"name": "Unsaved"})
    assert page.profile == profile
    assert {path: path.read_bytes() for path in paths} == before
    assert not list(manager.store.root.rglob("*.tmp"))


def test_saved_profile_reload_refits_untrusted_derived_axes(console):
    manager, _ = console
    page = manager.page("page-a")
    manager.mutate(page, "profile/save", {})
    path = manager.store.root / "profiles" / (page.selected + ".json")
    saved = json.loads(path.read_text())
    saved["axes"] = {"x": {"max": 999}}
    path.write_text(json.dumps(saved))
    loaded = manager.store.load(page.selected)
    assert loaded["axes"] == {}
    assert loaded["status"] == "empty"


def test_failed_selection_write_does_not_leave_a_new_profile(console, monkeypatch):
    manager, _ = console
    page = manager.page("page-a")
    manager.mutate(page, "profile/save", {"name": "Saved"})
    original_profile = copy.deepcopy(page.profile)
    before = {path: path.read_bytes() for path in manager.store.root.rglob("*.json")}
    original_replace = twin.os.replace

    def fail_selection(source, destination):
        if destination.name == "selection.json":
            raise OSError("Selection write failed")
        return original_replace(source, destination)

    monkeypatch.setattr(twin.os, "replace", fail_selection)
    with pytest.raises(OSError, match="Selection write failed"):
        manager.mutate(page, "profile/save", {"name": "Unsaved"})
    assert page.profile == original_profile
    assert {path: path.read_bytes() for path in manager.store.root.rglob("*.json")} == before
    assert not list(manager.store.root.rglob("*.tmp"))


def test_missing_or_wrong_camera_selected_profile_starts_uncalibrated(console):
    manager, frames = console
    manager.store.select("device:camera1", str(uuid4()))
    page = manager.page("missing")
    assert page.selected is None
    assert page.profile["axes"] == {}
    assert "unavailable" in page.warning
    other = new_profile()
    other["camera"]["device_id"] = "device:camera2"
    other = manager.store.save(other)
    manager.store.select("device:camera1", other["id"])
    restarted = twin.TwinConsole(manager.store.root, frames)
    page = restarted.page("mismatch")
    assert page.selected is None
    assert page.profile["camera"]["device_id"] == "device:camera1"
    assert "another camera" in page.warning


def test_clear_last_endpoint_clears_bound_resolution(console):
    manager, _ = console
    page = manager.page("page-a")
    manager.capture(page, {"endpoint": "x_low", "automatic": False})
    manager.mutate(page, "clear", {"endpoint": "x_low"})
    assert page.profile["endpoints"] == {}
    assert page.profile["camera"]["dimensions"] is None


@pytest.mark.parametrize(
    "action,payload",
    [
        ("source", {"camera": []}),
        ("source", {"camera": "camera9"}),
        ("profile/new", {"name": []}),
        ("profile/edit", {"travel_mm": {"x": -1, "y": 40, "insert": 150}}),
        ("profile/select", {"id": "../profile"}),
        ("capture", {"endpoint": []}),
        ("capture", {"endpoint": "x_low", "automatic": 1}),
        ("capture", {"endpoint": "x_low", "max_frames": True}),
        ("measure", {"automatic": "true"}),
        ("measure", {"max_frames": 9}),
        ("measure", {"tune": 1}),
        ("measure", {"extra": True}),
        ("measure", []),
    ],
)
def test_invalid_route_payload_preserves_existing_draft(routes, action, payload):
    client, frames, _ = routes
    before = profile_from(client.get("/twin/state", headers=headers()))
    response = client.post("/twin/" + action, json=payload, headers=headers())
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error"]
    assert profile_from(client.get("/twin/state", headers=headers())) == before
    assert frames.reads == []


def test_routes_validate_json_and_page_identity(routes):
    client, frames, _ = routes
    assert client.get("/twin/state").status_code == 400
    assert (
        client.post(
            "/twin/measure", data="{", content_type="application/json", headers=headers()
        ).status_code
        == 400
    )
    assert client.post("/twin/measure", data="text", headers=headers()).status_code == 415
    assert frames.reads == []


def test_route_save_selection_and_page_drafts(routes):
    client, _, root = routes
    first = profile_from(
        client.post("/twin/profile/new", json={"name": "Saved profile"}, headers=headers())
    )
    saved = profile_from(client.post("/twin/profile/save", json={}, headers=headers()))
    assert saved["id"] != first["id"]
    assert (root / "profiles" / (saved["id"] + ".json")).is_file()
    resumed = profile_from(client.get("/twin/state", headers=headers("page-b")))
    assert resumed["id"] == saved["id"]
    profile_from(
        client.post("/twin/profile/edit", json={"name": "Unsaved draft"}, headers=headers())
    )
    assert (
        profile_from(client.get("/twin/state", headers=headers("page-b")))["name"]
        == "Saved profile"
    )
    selected = profile_from(
        client.post("/twin/profile/select", json={"id": saved["id"]}, headers=headers())
    )
    assert selected["name"] == "Saved profile"


def test_upload_rebinds_camera_and_saves_new_snapshot_without_camera_access(routes):
    client, frames, root = routes
    source = new_profile(name="Portable calibration")
    source["camera"].update(device_id="external-camera", label="External camera")
    source["axes"] = {"x": {"max": 99999}}
    uploaded = profile_from(
        client.post("/twin/profile/upload", json={"profile": json.dumps(source)}, headers=headers())
    )
    assert uploaded["id"] != source["id"]
    assert uploaded["camera"] == {
        "device_id": "device:camera1",
        "label": "Stage",
        "dimensions": None,
    }
    assert uploaded["name"] == source["name"]
    assert uploaded["axes"] == {}
    assert (root / "profiles" / (uploaded["id"] + ".json")).is_file()
    assert profile_from(client.get("/twin/state", headers=headers("page-b"))) == uploaded
    second = profile_from(
        client.post("/twin/profile/upload", json={"profile": json.dumps(source)}, headers=headers())
    )
    assert second["id"] not in {source["id"], uploaded["id"]}
    assert len(list((root / "profiles").glob("*.json"))) == 2
    assert frames.reads == []


def test_upload_preserves_partial_observations_and_refits_axes(console):
    manager, frames = console
    page = manager.page("source")
    calibrate_x(manager, frames, page)
    source = copy.deepcopy(page.profile)
    source["camera"].update(device_id="other-machine", label="Other machine")
    source["axes"]["x"]["max"] = 99999
    frames.reads.clear()
    result = manager.mutate(page, "profile/upload", {"profile": json.dumps(source)})
    imported = result["calibration"]["profile"]
    assert imported["endpoints"] == source["endpoints"]
    assert imported["camera"]["dimensions"] == [640, 480]
    assert imported["status"] == "partial"
    assert imported["axes"]["x"]["max"] == 45
    assert result["active_profile"] == imported
    assert result["selected_profile"] == imported["id"]
    assert frames.reads == []


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "object",
        "broken",
        "array",
        "version",
        "dictionary",
        "extra",
        "nonfinite",
        "negative",
        "duplicate",
        "surrogate",
        "escaped_surrogate",
        "oversize",
    ],
)
def test_invalid_upload_preserves_draft_saved_selection_and_catalog(routes, invalid):
    client, frames, root = routes
    profile_from(client.post("/twin/profile/save", json={}, headers=headers()))
    profile_from(client.post("/twin/profile/edit", json={"name": "Draft"}, headers=headers()))
    before = client.get("/twin/state", headers=headers()).get_json()
    files = {path: path.read_bytes() for path in root.rglob("*.json")}
    source = new_profile()
    if invalid == "version":
        source["schema_version"] = 2
    elif invalid == "dictionary":
        source["dictionary"] = "DICT_5X5_50"
    elif invalid == "extra":
        source["image_base64"] = "image"
    elif invalid == "nonfinite":
        source["travel_mm"]["x"] = float("nan")
    elif invalid == "negative":
        source["travel_mm"]["x"] = -1
    elif invalid == "escaped_surrogate":
        source["name"] = "\ud800"
    raw = json.dumps(source)
    if invalid == "broken":
        raw = "{"
    elif invalid == "array":
        raw = "[]"
    elif invalid == "duplicate":
        raw = '{"schema_version": 999, ' + raw[1:]
    elif invalid == "surrogate":
        raw = "\ud800"
    elif invalid == "oversize":
        source["axes"] = {"ignored": "中" * (twin.MAX_PROFILE_BYTES // 3 + 1)}
        raw = json.dumps(source, ensure_ascii=False)
        assert len(raw) < twin.MAX_PROFILE_BYTES < len(raw.encode("utf-8"))
    payload = {} if invalid == "missing" else {"profile": source if invalid == "object" else raw}
    response = client.post("/twin/profile/upload", json=payload, headers=headers())
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error"]
    assert client.get("/twin/state", headers=headers()).get_json() == before
    assert {path: path.read_bytes() for path in root.rglob("*.json")} == files
    assert frames.reads == []


def test_upload_request_limit_precedes_app_json_parsing(tmp_path):
    service = SimpleNamespace(demo=True, temporary=SimpleNamespace(name=str(tmp_path)), settings={})
    app = app_for(service)
    app.config["MAX_CONTENT_LENGTH"] = 65536

    @app.before_request
    def parse_post_body():
        if request.method == "POST":
            request.get_json()

    client = app.test_client()
    raw = json.dumps(new_profile()).ljust(twin.MAX_PROFILE_BYTES)
    response = client.post("/twin/profile/upload", json={"profile": raw}, headers=headers())
    assert response.status_code == 200, response.get_data(as_text=True)
    response = client.post("/twin/profile/upload", json={"profile": raw + " "}, headers=headers())
    assert response.status_code == 400
    response = client.post("/twin/profile/upload", json={"profile": raw * 7}, headers=headers())
    assert response.status_code == 413
    response = client.post("/twin/profile/edit", json={"name": " " * 65536}, headers=headers())
    assert response.status_code == 413


def test_waitress_accepts_bounded_profile_upload_and_keeps_normal_route_limit(tmp_path):
    from waitress import create_server, wasyncore

    from orchestrator.__main__ import SERVER_OPTIONS

    service = SimpleNamespace(demo=True, temporary=SimpleNamespace(name=str(tmp_path)), settings={})
    app = app_for(service)
    app.config["MAX_CONTENT_LENGTH"] = 65536

    @app.before_request
    def parse_post_body():
        if request.method == "POST":
            request.get_json()

    socket_map = {}
    server = create_server(
        app,
        host="127.0.0.1",
        port=0,
        map=socket_map,
        asyncore_loop_timeout=0.05,
        **SERVER_OPTIONS,
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()

    def post(path, body=None, *, declared_length=None):
        connection = HTTPConnection("127.0.0.1", int(server.effective_port), timeout=5)
        request_headers = {
            **headers(),
            "Content-Type": "application/json",
            "Connection": "close",
        }
        if declared_length is not None:
            request_headers["Content-Length"] = str(declared_length)
        try:
            connection.request("POST", path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        raw = json.dumps(new_profile()).ljust(twin.MAX_PROFILE_BYTES)
        # Exercise the largest JSON string expansion, not only a short upload.
        encoded = '{"profile":"' + "".join(f"\\u{ord(char):04x}" for char in raw) + '"}'
        status, body = post("/twin/profile/upload", encoded.encode("ascii"))
        assert status == 200, body
        uploaded = json.loads(body)
        assert uploaded["selected_profile"] == uploaded["calibration"]["profile"]["id"]
        assert post("/twin/profile/upload", json.dumps({"profile": raw + " "}))[0] == 400
        assert post("/twin/profile/edit", json.dumps({"name": " " * 65536}))[0] == 413
        # Waitress must reject this header without waiting for a request body.
        assert (
            post(
                "/twin/profile/upload",
                declared_length=SERVER_OPTIONS["max_request_body_size"] + 1,
            )[0]
            == 413
        )
    finally:
        server.task_dispatcher.shutdown()
        wasyncore.close_all(socket_map)
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_delete_catalog_row_preserves_other_selected_snapshot_and_draft(console):
    manager, frames = console
    page = manager.page("page-a")
    deleted = manager.mutate(page, "profile/save", {"name": "Old"})["selected_profile"]
    manager.mutate(page, "profile/save", {"name": "Current"})
    manager.mutate(page, "profile/edit", {"name": "Unsaved draft"})
    draft, active, selected = (
        copy.deepcopy(page.profile),
        copy.deepcopy(page.active_profile),
        page.selected,
    )
    result = manager.mutate(page, "profile/delete", {"id": deleted})
    assert result["selected_profile"] == selected
    assert result["active_profile"] == active
    assert result["calibration"]["profile"] == draft
    assert [item["id"] for item in result["profiles"]] == [selected]
    assert manager.store.selections() == {"device:camera1": selected}
    assert frames.reads == []


def test_delete_active_snapshot_invalidates_other_page_before_measure_and_preserves_drafts(console):
    manager, frames = console
    first = manager.page("first")
    calibrate_x(manager, frames, first)
    deleted = manager.mutate(first, "profile/save", {})["selected_profile"]
    second = manager.page("second")
    manager.mutate(first, "profile/edit", {"name": "First draft"})
    manager.mutate(second, "profile/edit", {"name": "Second draft"})
    drafts = [copy.deepcopy(page.profile) for page in (first, second)]
    old_policy = second.policy
    result = manager.mutate(first, "profile/delete", {"id": deleted})
    assert result["selected_profile"] is None
    assert result["active_profile"]["axes"] == {}
    assert result["profiles"] == []
    assert manager.store.selections() == {}
    assert not (manager.store.root / "profiles" / (deleted + ".json")).exists()
    report = manager.measure(second, {})
    assert report["profile_id"] != deleted
    assert report["axes"]["x"]["value_mm"] is None
    assert second.selected is None
    assert second.policy is not old_policy
    assert [page.profile for page in (first, second)] == drafts
    resumed = twin.TwinConsole(manager.store.root, frames).page("resumed")
    assert resumed.selected is None
    assert resumed.profile["axes"] == {}


def test_delete_rejects_another_camera_profile_without_changes(console):
    manager, _ = console
    page = manager.page("page-a")
    deleted = manager.mutate(page, "profile/save", {})["selected_profile"]
    manager.mutate(page, "source", {"camera": "camera2"})
    before = manager.state(page)
    with pytest.raises(ValueError, match="another camera"):
        manager.mutate(page, "profile/delete", {"id": deleted})
    assert manager.state(page) == before
    assert manager.store.load(deleted)["id"] == deleted


@pytest.mark.parametrize("failure", ["selection", "unlink"])
def test_failed_delete_preserves_files_selection_and_page(console, monkeypatch, failure):
    manager, _ = console
    page = manager.page("page-a")
    identifier = manager.mutate(page, "profile/save", {})["selected_profile"]
    manager.mutate(page, "profile/edit", {"name": "Draft"})
    before = manager.state(page)
    files = {path: path.read_bytes() for path in manager.store.root.rglob("*.json")}
    if failure == "selection":
        original_replace = twin.os.replace

        def fail_selection(source, destination):
            if destination.name == "selection.json":
                raise OSError("Selection write failed")
            return original_replace(source, destination)

        monkeypatch.setattr(twin.os, "replace", fail_selection)
    else:
        original_unlink = twin.Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path.name == identifier + ".json":
                raise OSError("Profile deletion failed")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(twin.Path, "unlink", fail_unlink)
    with pytest.raises(OSError):
        manager.mutate(page, "profile/delete", {"id": identifier})
    assert manager.state(page) == before
    assert {path: path.read_bytes() for path in manager.store.root.rglob("*.json")} == files
    assert not list(manager.store.root.rglob("*.tmp"))


def test_failed_upload_selection_write_preserves_draft_and_saved_files(console, monkeypatch):
    manager, _ = console
    page = manager.page("page-a")
    manager.mutate(page, "profile/save", {})
    manager.mutate(page, "profile/edit", {"name": "Draft"})
    before = manager.state(page)
    files = {path: path.read_bytes() for path in manager.store.root.rglob("*.json")}
    original_replace = twin.os.replace

    def fail_selection(source, destination):
        if destination.name == "selection.json":
            raise OSError("Selection write failed")
        return original_replace(source, destination)

    monkeypatch.setattr(twin.os, "replace", fail_selection)
    with pytest.raises(OSError, match="Selection write failed"):
        manager.mutate(page, "profile/upload", {"profile": json.dumps(new_profile())})
    assert manager.state(page) == before
    assert {path: path.read_bytes() for path in manager.store.root.rglob("*.json")} == files
    assert not list(manager.store.root.rglob("*.tmp"))


def test_frame_route_returns_the_selected_camera_bytes_without_profile_write(routes):
    client, frames, root = routes
    response = client.get("/twin/frame?camera=camera2")
    assert response.status_code == 200
    assert response.data == frames.data
    assert response.mimetype == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    assert frames.reads == ["camera2"]
    assert not root.exists()


def test_preview_frame_remains_available_while_detector_is_busy(console, monkeypatch):
    manager, frames = console
    monkeypatch.setattr(workspace, "TwinConsole", lambda root, frames: manager)
    client = app_for(SimpleNamespace(demo=False, settings={})).test_client()
    with manager.processing():
        preview = client.get("/twin/frame?camera=camera2")
        measure = client.post("/twin/measure", json={}, headers=headers())
        capture = client.post("/twin/capture", json={"endpoint": "x_low"}, headers=headers())
    assert preview.status_code == 200
    assert preview.data == frames.data
    assert preview.mimetype == "image/jpeg"
    assert measure.status_code == 409
    assert capture.status_code == 409
    assert frames.reads == ["camera2"]


def test_demo_starts_without_camera_network_or_profile_directories(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Demo attempted live camera access")

    monkeypatch.setattr("vision.frames.Go2RTCSource", forbidden)
    service = SimpleNamespace(demo=True, temporary=SimpleNamespace(name=str(tmp_path)), settings={})
    app = app_for(service)
    root = tmp_path / "calibration" / "twin"
    assert not root.exists()
    client = app.test_client()
    state = client.get("/twin/state", headers=headers())
    assert profile_from(state)["camera"]["device_id"] == "demo:camera1"
    assert len(state.get_json()["cameras"]) == 4
    frame = client.get("/twin/frame")
    assert frame.status_code == 200
    assert analyze_frame(frame.data)["detections"]
    assert not root.exists()
    profile_from(client.post("/twin/profile/save", json={}, headers=headers()))
    assert root.is_dir()


def test_demo_service_must_start_before_first_request(tmp_path):
    service = SimpleNamespace(demo=True, temporary=None, settings={})
    client = app_for(service).test_client()
    response = client.get("/twin/state", headers=headers())
    assert response.status_code == 409
    assert "has not started" in response.get_json()["error"]
    assert not list(tmp_path.iterdir())


def test_unconfigured_live_camera_reports_empty_state_without_device_access(tmp_path):
    manager = twin.TwinConsole(tmp_path / "profiles", twin.CameraFrames({}))
    page = manager.page("page-a")
    assert manager.state(page)["cameras"] == []
    assert page.camera is None
    with pytest.raises(ValueError, match="No Console camera"):
        manager.measure(page, {})
    assert not manager.store.root.exists()


def test_live_camera_choices_use_configured_streams_and_stable_device_identity():
    frames = twin.CameraFrames(
        {"MG400_CAMERA_DEVICES": "/dev/v4l/by-path/usb-stage,/dev/v4l/by-path/usb-insert"}
    )
    assert frames.cameras() == [
        {"id": "camera1", "label": "Stage"},
        {"id": "camera2", "label": "Insert rail"},
    ]
    assert frames.identity("camera1") == "/dev/v4l/by-path/usb-stage"
    with pytest.raises(ValueError, match="configured Console camera"):
        frames.identity([])
    with pytest.raises(ValueError, match="configured Console camera"):
        frames.identity("camera3")


def test_native_still_uses_requested_size_without_resizing_preview():
    content = BytesIO()
    Image.new("RGB", (3840, 2160), "white").save(content, format="JPEG")
    calls = []

    def open_frame(url, timeout):
        calls.append((url, timeout))
        return BytesIO(content.getvalue())

    settings = {"MG400_CAMERA_DEVICES": "/dev/v4l/by-path/usb-stage"}
    source = Go2RTCSource(settings=settings, opener=open_frame, timeout=30)
    assert source.full_still(1, [3840, 2160]).size == (3840, 2160)
    assert calls == [("http://127.0.0.1:1985/camera1.full.jpg?size=3840x2160", 30)]

    content = BytesIO()
    Image.new("RGB", (1280, 720), "white").save(content, format="JPEG")
    with pytest.raises(FrameError, match="1280x720.*3840x2160"):
        source.full_still(1, [3840, 2160])
    assert len(calls) == 2


@pytest.mark.parametrize("dimensions", [None, [3840], [True, 2160], [123, 456], "3840x2160"])
def test_native_still_rejects_unsupported_dimensions_before_camera_access(dimensions):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported resolution opened the camera source")

    source = Go2RTCSource(
        settings={"MG400_CAMERA_DEVICES": "/dev/v4l/by-path/usb-stage"}, opener=forbidden
    )
    with pytest.raises(ValueError, match="unsupported camera resolution"):
        source.full_still(1, dimensions)


def test_live_twin_samples_default_to_4k_and_reuse_matching_preview(monkeypatch):
    calls = []

    class Source:
        def __init__(self, *, timeout, settings):
            self.timeout = timeout

        def still(self, camera):
            calls.append(("preview", camera, self.timeout))
            return Image.new("RGB", (1280, 720))

        def full_still(self, camera, dimensions):
            calls.append(("native", camera, self.timeout, tuple(dimensions)))
            return Image.new("RGB", dimensions)

    monkeypatch.setattr("vision.frames.Go2RTCSource", Source)
    frames = twin.CameraFrames({"MG400_CAMERA_DEVICES": "/dev/v4l/by-path/usb-stage"})
    assert Image.open(BytesIO(frames.read("camera1"))).size == (1280, 720)
    assert Image.open(BytesIO(frames.sample("camera1"))).size == (3840, 2160)
    assert Image.open(BytesIO(frames.sample("camera1", [1280, 720]))).size == (1280, 720)
    assert Image.open(BytesIO(frames.sample("camera1", [1920, 1080]))).size == (1920, 1080)
    assert calls == [
        ("preview", 1, 2),
        ("native", 1, 30, (3840, 2160)),
        ("preview", 1, 2),
        ("native", 1, 30, (1920, 1080)),
    ]


def test_live_4k_stage_samples_reuse_capture_without_mode_changes(monkeypatch):
    content = BytesIO()
    Image.new("RGB", (3840, 2160), "white").save(content, format="JPEG")
    calls = []

    def open_frame(url, timeout):
        calls.append((url, timeout))
        assert url == "http://127.0.0.1:1984/api/frame.jpeg?src=camera1"
        return BytesIO(content.getvalue())

    monkeypatch.setattr("vision.frames.build_opener", lambda *args: open_frame)
    frames = twin.CameraFrames(
        {
            "MG400_CAMERA_DEVICES": ",".join(
                f"/dev/v4l/by-path/usb-camera{number}" for number in range(1, 5)
            ),
            "MG400_CAMERA_MODES": "3840x2160@30,1280x720@30,1280x720@30,1280x720@30",
        }
    )
    for dimensions in (None, [3840, 2160], [3840, 2160]):
        with Image.open(BytesIO(frames.sample("camera1", dimensions))) as image:
            image.load()
            assert image.size == (3840, 2160)
    assert len(calls) == 3
    assert all(timeout == 2 for _, timeout in calls)


def test_monitor_requests_saved_dimensions_while_capture_requests_draft_dimensions(
    console, monkeypatch
):
    manager, frames = console
    page = manager.page("page-a")
    calibrate_x(manager, frames, page)
    manager.mutate(page, "profile/save", {})
    requested = []

    def sample(camera, dimensions=None):
        requested.append(dimensions)
        return frames.read(camera)

    monkeypatch.setattr(frames, "sample", sample)
    page.profile["camera"]["dimensions"] = [3840, 2160]
    manager.measure(page, {})
    with pytest.raises(ValueError, match="resolution differs"):
        manager.capture(page, {"endpoint": "x_low", "automatic": False})
    assert requested == [[640, 480], [3840, 2160]]
