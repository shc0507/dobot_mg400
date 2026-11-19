"""Console Cameras workspace: proxy camera URLs, thermal and detection relays, and secrets."""

import base64
import io
import json
import socket
import struct
from types import SimpleNamespace

import pytest

from orchestrator.app import OrchestratorService, create_app
from orchestrator.web.workspaces import cameras as cameras_module

PRIVATE = "private-microscope-token-0123456789"
JPEG = b"\xff\xd8fake-jpeg\xff\xd9"
PNG = b"\x89PNGfake-png"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def thermal_payload(timestamp):
    temperatures = [[20.0 + (x + y) / 100 for x in range(160)] for y in range(120)]
    return {
        "sequence": 7,
        "timestamp": timestamp,
        "source": "demo",
        "width": 160,
        "height": 120,
        "stats": {
            "min": {"celsius": 20.0, "x": 0, "y": 0},
            "max": {"celsius": 22.77, "x": 159, "y": 119},
            "center": {"celsius": 21.4, "x": 80, "y": 60},
            "mean_celsius": 21.4,
        },
        "temperatures_c": temperatures,
        "raw": [[0] * 160 for _ in range(120)],
        "ir": [[0] * 160 for _ in range(120)],
        "raw_unit": "1/64 kelvin",
    }


class Sources:
    def __init__(self):
        self.fail = set()
        self.thermal_reads = 0
        self.thermal_timestamp = None
        self.detect_reads = 0

    def configured(self, view):
        return view != "camera3"

    def modes(self):
        return {"camera1": (640, 480, 30), "thermal": (160, 120, None)}

    def preview_fps(self):
        return {"camera1": 4.9, "microscope": 9.5}

    def thermal_frame(self):
        self.thermal_reads += 1
        if "thermal" in self.fail:
            raise cameras_module.SourceError("The thermal service did not answer.")
        return thermal_payload(self.thermal_timestamp)

    def thermal_describe(self):
        return {"regions": {"crucible": {"x0": 10, "y0": 20, "x1": 40, "y1": 60}}}

    def detections(self, camera):
        self.detect_reads += 1
        return {
            "camera": camera,
            "coordinate_system": "oriented_original_image_pixels",
            "image_size": {"width": 640, "height": 480},
            "detections": [
                {"label": "chip", "confidence": 0.87, "xyxy": [10, 20, 30, 40], "extra": PRIVATE}
            ],
        }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Camera test attempted real I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def rig(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    clock = Clock()
    service = OrchestratorService(
        settings={
            "MG400_ORCH_MICROSCOPE_TOKEN": PRIVATE,
            "MG400_ORCH_MICROSCOPE_URL": "http://private.invalid:8770",
            "MG400_CONSOLE_COMMIT": "abc1234",
        },
        recipes_dir=recipes,
        demo=True,
        client_factory=lambda required, identity: {},
        clock=clock,
    ).start(background=False)
    sources = Sources()
    service.camera_sources = sources
    app = create_app(service, base_path="/lab")
    yield SimpleNamespace(
        service=service, app=app, client=app.test_client(), clock=clock, sources=sources
    )
    service.close()


BASE = "/lab/api/console/ws/cameras/"


def headers(page="console-page-a"):
    return {"X-MG400-Client": page}


def status(rig):
    body = rig.client.get(BASE + "status", headers=headers()).get_json()
    return {row["id"]: row for row in body["views"]}


def test_routes_need_no_token_and_cameras_load_from_the_proxy(rig):
    assert rig.client.get(BASE + "status").status_code == 200
    # camera1 to camera4 are not relayed: the browser loads them from the proxy.
    assert rig.client.get(BASE + "stream/camera1").status_code == 404
    assert rig.client.get(BASE + "snapshot/camera1").status_code == 404
    assert rig.client.get(BASE + "stream/camera9").status_code == 404
    assert rig.client.post(BASE + "ticket", json={}, headers=headers()).status_code in {404, 405}
    rows = status(rig)
    assert rows["camera1"]["url"] == "/camera/camera1.mjpeg"
    # The mode has no configured rate: tiles show the measured one.
    assert rows["camera1"]["mode"] == "640×480"
    assert rows["camera1"]["fps"] == 4.9
    assert rows["camera2"]["fps"] is None
    assert rows["camera3"]["configured"] is False
    assert rows["microscope"]["url"] == "/camera/microscope.mjpeg"
    assert rows["microscope"]["fps"] == 9.5
    assert rig.client.get(BASE + "stream/microscope").status_code == 404
    assert rig.client.get(BASE + "snapshot/microscope").status_code == 404


def test_preview_fps_keeps_only_finite_camera_rates(monkeypatch):
    payloads = iter([b'{"camera1": 9.8, "camera2": null, "camera9": 3, "thermal": 1}', b"[]", b"{"])
    monkeypatch.setattr(cameras_module, "_open", lambda url, timeout: io.BytesIO(next(payloads)))
    source = cameras_module.HttpSources(SimpleNamespace())
    assert source.preview_fps() == {"camera1": 9.8}
    assert source.preview_fps() == {}
    assert source.preview_fps() == {}


def test_cameras_are_configured_by_the_camera_devices(rig):
    rig.service.camera_sources = None
    assert cameras_module.HttpSources(rig.service).configured("camera1") is False
    devices = "/dev/v4l/by-path/cam-a,/dev/v4l/by-path/cam-b"
    rig.service.settings = {**rig.service.settings, "MG400_CAMERA_DEVICES": devices}
    sources = cameras_module.HttpSources(rig.service)
    assert sources.configured("camera2") is True and sources.configured("camera3") is False


def test_thermal_frame_is_cached_trimmed_and_reports_its_age(rig):
    from datetime import datetime, timezone

    rig.sources.thermal_timestamp = datetime.now(timezone.utc).isoformat()
    path = BASE + "thermal/frame"
    body = json.loads(rig.client.get(path, headers=headers()).data)
    assert not {"temperatures_c", "raw", "ir"} & set(body)
    values = struct.unpack("<19200H", base64.b64decode(body["temperatures_u16"]))
    assert values[0] == round((20.0 + 273.15) * 64)
    assert values[-1] / 64 - 273.15 == pytest.approx(22.78, abs=1 / 128)
    assert len(rig.client.get(path, headers=headers()).data) < 60_000
    assert body["stats"]["max"]["celsius"] == 22.77 and body["age_s"] < 5
    assert rig.sources.thermal_reads == 1
    full = json.loads(rig.client.get(path + "?full=1", headers=headers()).data)
    assert len(full["raw"]) == len(full["temperatures_c"]) == 120
    assert full["raw_unit"] == "1/64 kelvin"
    assert status(rig)["thermal"]["state"] == "live"
    rig.sources.fail.add("thermal")
    rig.clock.value += 1
    assert rig.client.get(path, headers=headers()).status_code == 502
    assert status(rig)["thermal"]["state"] == "offline"
    regions = rig.client.get(BASE + "thermal/regions", headers=headers())
    assert regions.get_json() == {"regions": {"crucible": {"x0": 10, "y0": 20, "x1": 40, "y1": 60}}}


def test_detections_only_for_camera_4_and_only_display_fields(rig):
    path = BASE + "detections/"
    assert rig.client.get(path + "2", headers=headers()).status_code == 404
    body = rig.client.get(path + "4", headers=headers()).get_json()
    assert body["available"] is True
    assert body["detections"] == [
        {"label": "chip", "confidence": 0.87, "xyxy": [10.0, 20.0, 30.0, 40.0]}
    ]
    rig.client.get(path + "4", headers=headers())
    assert rig.sources.detect_reads == 1


def test_no_secret_in_any_camera_response(rig):
    texts = [
        rig.client.get("/lab/").get_data(as_text=True),
        rig.client.get("/lab/console-static/cameras.js").get_data(as_text=True),
        rig.client.get("/lab/console-static/cameras.css").get_data(as_text=True),
        json.dumps(rig.client.get(BASE + "status", headers=headers()).get_json()),
        json.dumps(rig.client.get(BASE + "detections/4", headers=headers()).get_json()),
        json.dumps(rig.client.get("/lab/api/console/state", headers=headers()).get_json()),
    ]
    for text in texts:
        assert PRIVATE not in text and "private.invalid" not in text


def test_http_sources_open_services_without_a_token(monkeypatch):
    calls = []

    def fake_open(url, timeout=None):
        calls.append(url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(cameras_module, "_open", fake_open)

    class Http:
        base_url = "http://127.0.0.1:8770/"

    service = SimpleNamespace(
        settings={},
        dashboard_client=lambda name, identity: SimpleNamespace(_http=Http()),
        dashboard_configured=lambda name: True,
    )
    sources = cameras_module.HttpSources(service)
    sources.thermal_frame()
    assert calls == ["http://127.0.0.1:8770/api/frame"]
    assert sources.configured("camera1") is False
