"""Console Cameras workspace: tickets, the stream bound, relay end, states and secrets."""

import json
import socket
from types import SimpleNamespace

import pytest

from orchestrator.app import OrchestratorService, create_app
from orchestrator.web.workspaces import cameras as cameras_module

TOKEN = "console-operator-token-0123456789"
PRIVATE = "private-microscope-token-0123456789"
CAMERA_KEY = "camera-preview-key-0123456789abcdef0123"
JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class Upstream:
    """A fake MJPEG response. It yields frames until closed or until frames run out."""

    def __init__(self, frames=None, content_type="multipart/x-mixed-replace; boundary=frame"):
        self.headers = {"Content-Type": content_type}
        self.frames = frames
        self.closed = False
        self.reads = 0

    def read1(self, _size):
        if self.closed:
            return b""
        self.reads += 1
        if self.frames is not None and self.reads > self.frames:
            return b""
        return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"

    def close(self):
        self.closed = True


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
        self.streams = []
        self.fail = set()
        self.frames = None
        self.thermal_reads = 0
        self.thermal_timestamp = None
        self.detect_reads = 0

    def configured(self, view):
        return view != "camera3"

    def modes(self):
        return {"camera1": (640, 480, 30), "thermal": (160, 120, None)}

    def open_stream(self, view):
        if view in self.fail:
            raise cameras_module.SourceError("The source did not answer.")
        upstream = Upstream(self.frames)
        self.streams.append((view, upstream))
        return upstream

    def frame(self, view):
        if view in self.fail:
            raise cameras_module.SourceError("The source did not answer.")
        return JPEG

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
            "MG400_ORCH_CAMERA_URL": "http://127.0.0.1:1985",
            "MG400_CAMERA_KEY_FILE": str(tmp_path / "missing.key"),
            "MG400_CONSOLE_COMMIT": "abc1234",
        },
        recipes_dir=recipes,
        demo=True,
        client_factory=lambda required, identity: {},
        clock=clock,
    ).start(background=False)
    sources = Sources()
    service.camera_sources = sources
    app = create_app(service, token=TOKEN, base_path="/lab")
    yield SimpleNamespace(
        service=service, app=app, client=app.test_client(), clock=clock, sources=sources
    )
    service.close()


def headers(page="console-page-a"):
    return {"Authorization": f"Bearer {TOKEN}", "X-MG400-Client": page}


def ticket(rig, view, kind="stream"):
    response = rig.client.post(
        "/lab/api/console/ws/cameras/ticket", json={"view": view, "kind": kind}, headers=headers()
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["url"]


def status(rig):
    body = rig.client.get("/lab/api/console/ws/cameras/status", headers=headers()).get_json()
    return {row["id"]: row for row in body["views"]}


def open_stream(rig, view):
    response = rig.client.get(ticket(rig, view), buffered=False)
    return response


def test_routes_need_the_operator_token_or_a_ticket(rig):
    base = "/lab/api/console/ws/cameras/"
    assert rig.client.get(base + "status").status_code == 401
    assert (
        rig.client.post(base + "ticket", json={"view": "camera1", "kind": "stream"}).status_code
        == 401
    )
    assert rig.client.get(base + "thermal/frame").status_code == 401
    assert rig.client.get(base + "stream/camera1").status_code == 401
    assert rig.client.get(base + "stream/camera1?ticket=forged").status_code == 401
    assert rig.client.get(base + "snapshot/camera1?ticket=forged").status_code == 401
    bad = rig.client.post(
        base + "ticket", json={"view": "thermal", "kind": "stream"}, headers=headers()
    )
    assert bad.status_code == 400
    unknown = rig.client.post(
        base + "ticket", json={"view": "camera9", "kind": "stream"}, headers=headers()
    )
    assert unknown.status_code == 404


def test_ticket_is_single_use_bound_to_its_view_and_expires(rig):
    url = ticket(rig, "camera1")
    assert "/lab/api/console/ws/cameras/stream/camera1?ticket=" in url
    other = url.replace("stream/camera1", "stream/camera2")
    assert rig.client.get(other).status_code == 401
    # Any use spends the ticket, also a refused one.
    assert rig.client.get(url).status_code == 401
    first = open_stream(rig, "camera1")
    assert first.status_code == 200
    first.close()
    snap = ticket(rig, "camera1", "snapshot")
    assert rig.client.get(snap.replace("snapshot/", "stream/")).status_code == 401
    late = ticket(rig, "camera1", "snapshot")
    rig.clock.value += cameras_module.TICKET_TTL_S + 1
    assert rig.client.get(late).status_code == 401
    fresh = rig.client.get(ticket(rig, "camera1", "snapshot"))
    assert fresh.status_code == 200 and fresh.data == JPEG
    assert fresh.headers["Content-Type"] == "image/jpeg"
    assert "attachment" in fresh.headers["Content-Disposition"]


def test_stream_passes_frames_and_ends_the_relay_when_the_viewer_leaves(rig):
    response = open_stream(rig, "camera2")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    chunks = response.response
    data = next(chunks) + next(chunks)
    assert data.count(b"--frame") == 2 and JPEG in data
    view, upstream = rig.sources.streams[-1]
    assert view == "camera2" and not upstream.closed
    row = status(rig)["camera2"]
    assert row["state"] == "live" and row["streams"] == 1 and row["age_s"] == 0.0
    response.close()
    assert upstream.closed
    assert status(rig)["camera2"]["streams"] == 0


def test_stream_bound_per_view_microscope_and_total(rig):
    open_ = []
    for _ in range(cameras_module.MAX_STREAMS_PER_VIEW):
        open_.append(open_stream(rig, "camera1"))
    assert all(item.status_code == 200 for item in open_)
    assert open_stream(rig, "camera1").status_code == 503
    for _ in range(cameras_module.MAX_MICROSCOPE_STREAMS):
        open_.append(open_stream(rig, "microscope"))
    assert open_stream(rig, "microscope").status_code == 503
    open_.append(open_stream(rig, "camera2"))
    open_.append(open_stream(rig, "camera2"))
    assert sum(cameras_module.Relay.streams(rig.service.camera_relay).values()) == 6
    refused = open_stream(rig, "camera4")
    assert refused.status_code == 503
    assert "limit" in refused.get_json()["error"]
    open_[0].close()
    again = open_stream(rig, "camera4")
    assert again.status_code == 200
    for item in [*open_, again]:
        item.close()
    assert sum(rig.service.camera_relay.streams().values()) == 0
    assert all(upstream.closed for _, upstream in rig.sources.streams)


def test_stale_and_offline_states(rig):
    rows = status(rig)
    assert rows["camera1"]["state"] == "offline" and rows["camera1"]["age_s"] is None
    assert rows["camera3"]["configured"] is False
    assert rows["camera1"]["mode"] == "640×480 · 30 fps"
    response = open_stream(rig, "camera1")
    next(response.response)
    assert status(rig)["camera1"]["state"] == "live"
    rig.clock.value += cameras_module.STALE_S + 0.5
    row = status(rig)["camera1"]
    assert row["state"] == "stale" and row["age_s"] == 2.5
    response.close()
    rig.clock.value += cameras_module.RECENT_S
    assert status(rig)["camera1"]["state"] == "offline"
    rig.sources.fail.add("camera4")
    failed = open_stream(rig, "camera4")
    assert failed.status_code == 502
    row = status(rig)["camera4"]
    assert row["state"] == "offline" and row["error"] == "The source did not answer."
    assert sum(rig.service.camera_relay.streams().values()) == 0


def test_console_state_reports_the_preview_source_without_its_url(rig, monkeypatch):
    def preview():
        body = rig.client.get("/lab/api/console/state", headers=headers()).get_json()
        assert "1985" not in json.dumps(body["camera_preview"])
        return body["camera_preview"]["state"]

    assert preview() == "idle"
    response = open_stream(rig, "camera1")
    next(response.response)
    assert preview() == "live"
    rig.clock.value += cameras_module.STALE_S + 0.5
    assert preview() == "stale"
    response.close()
    rig.clock.value += cameras_module.RECENT_S
    assert preview() == "idle"
    rig.sources.fail.add("camera2")
    assert open_stream(rig, "camera2").status_code == 502
    assert preview() == "unavailable"
    monkeypatch.setattr(rig.sources, "configured", lambda view: False)
    assert preview() == "unconfigured"


def test_preview_state_follows_the_console_camera_setting(rig):
    rig.service.camera_sources = None
    assert cameras_module.preview_state(rig.service)["state"] == "idle"
    rig.service.settings = {**rig.service.settings, "MG400_ORCH_CAMERA_URL": ""}
    assert cameras_module.preview_state(rig.service)["state"] == "unconfigured"


def test_an_ended_upstream_releases_its_slot(rig):
    rig.sources.frames = 1
    response = open_stream(rig, "camera1")
    assert b"".join(response.response).count(b"--frame") == 1
    response.close()
    assert rig.service.camera_relay.streams()["camera1"] == 0


def test_non_mjpeg_upstream_is_refused(rig, monkeypatch):
    monkeypatch.setattr(rig.sources, "open_stream", lambda view: Upstream(content_type="text/html"))
    assert open_stream(rig, "camera1").status_code == 502
    assert rig.service.camera_relay.streams()["camera1"] == 0


def test_thermal_frame_is_cached_trimmed_and_reports_its_age(rig):
    from datetime import datetime, timezone

    rig.sources.thermal_timestamp = datetime.now(timezone.utc).isoformat()
    path = "/lab/api/console/ws/cameras/thermal/frame"
    body = json.loads(rig.client.get(path, headers=headers()).data)
    assert len(body["temperatures_c"]) == 120 and "raw" not in body and "ir" not in body
    assert body["stats"]["max"]["celsius"] == 22.77 and body["age_s"] < 5
    rig.client.get(path, headers=headers())
    assert rig.sources.thermal_reads == 1
    full = json.loads(rig.client.get(path + "?full=1", headers=headers()).data)
    assert len(full["raw"]) == 120 and full["raw_unit"] == "1/64 kelvin"
    assert status(rig)["thermal"]["state"] == "live"
    rig.sources.fail.add("thermal")
    rig.clock.value += 1
    assert rig.client.get(path, headers=headers()).status_code == 502
    assert status(rig)["thermal"]["state"] == "offline"
    regions = rig.client.get("/lab/api/console/ws/cameras/thermal/regions", headers=headers())
    assert regions.get_json() == {"regions": {"crucible": {"x0": 10, "y0": 20, "x1": 40, "y1": 60}}}


def test_detections_only_for_camera_4_and_only_display_fields(rig):
    path = "/lab/api/console/ws/cameras/detections/"
    assert rig.client.get(path + "2", headers=headers()).status_code == 404
    body = rig.client.get(path + "4", headers=headers()).get_json()
    assert body["available"] is True
    assert body["detections"] == [
        {"label": "chip", "confidence": 0.87, "xyxy": [10.0, 20.0, 30.0, 40.0]}
    ]
    rig.client.get(path + "4", headers=headers())
    assert rig.sources.detect_reads == 1


def test_no_secret_in_any_camera_response(rig, tmp_path):
    key = tmp_path / "preview.key"
    key.write_text(CAMERA_KEY)
    rig.service.settings["MG400_CAMERA_KEY_FILE"] = str(key)
    texts = [
        rig.client.get("/lab/").get_data(as_text=True),
        rig.client.get("/lab/console-static/cameras.js").get_data(as_text=True),
        rig.client.get("/lab/console-static/cameras.css").get_data(as_text=True),
        json.dumps(
            rig.client.get("/lab/api/console/ws/cameras/status", headers=headers()).get_json()
        ),
        ticket(rig, "camera1"),
        ticket(rig, "microscope", "snapshot"),
        json.dumps(
            rig.client.get("/lab/api/console/ws/cameras/detections/4", headers=headers()).get_json()
        ),
        json.dumps(rig.client.get("/lab/api/console/state", headers=headers()).get_json()),
    ]
    stream = open_stream(rig, "camera1")
    texts.append(next(stream.response).decode("latin-1"))
    texts.append(str(dict(stream.headers)))
    stream.close()
    for text in texts:
        assert TOKEN not in text and PRIVATE not in text and CAMERA_KEY not in text


def test_http_sources_build_keyed_preview_and_token_urls(tmp_path, monkeypatch):
    key = tmp_path / "preview.key"
    key.write_text(CAMERA_KEY + "\n")
    calls = []

    def fake_open(url, token=None, timeout=None):
        calls.append((url, token))
        return Upstream()

    monkeypatch.setattr(cameras_module, "_open", fake_open)

    class Http:
        base_url = "http://127.0.0.1:8770/"
        token = PRIVATE

    service = SimpleNamespace(
        settings={
            "MG400_ORCH_CAMERA_URL": "http://127.0.0.1:1985/",
            "MG400_CAMERA_KEY_FILE": str(key),
        },
        dashboard_client=lambda name, identity: SimpleNamespace(_http=Http()),
        dashboard_configured=lambda name: True,
    )
    sources = cameras_module.HttpSources(service)
    sources.open_stream("camera2")
    sources.open_stream("microscope")
    assert calls == [
        (f"http://127.0.0.1:1985/{CAMERA_KEY}/camera2.mjpeg", None),
        ("http://127.0.0.1:8770/api/camera/microscope/mjpeg", PRIVATE),
    ]
    key.write_text("short")
    with pytest.raises(cameras_module.SourceError) as error:
        sources.open_stream("camera1")
    assert "short" not in str(error.value)
    service.settings["MG400_ORCH_CAMERA_URL"] = ""
    assert sources.configured("camera1") is False
