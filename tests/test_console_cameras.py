"""Console Cameras workspace: proxy camera URLs, the microscope relay, states and secrets."""

import io
import json
import re
import socket
import threading
import time
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


class Upstream:
    """A fake MJPEG response at about 200 fps. It yields frames until closed or until
    frames run out. With stall, it then blocks like a frozen camera until closed."""

    def __init__(
        self, frames=None, content_type="multipart/x-mixed-replace; boundary=frame", stall=False
    ):
        self.headers = {"Content-Type": content_type}
        self.frames = frames
        self.stall = stall
        self.closed = False
        self.reads = 0
        self.numbered = False
        self.woken = threading.Event()

    def read1(self, _size):
        if self.closed:
            return b""
        self.reads += 1
        if self.frames is not None and self.reads > self.frames:
            if self.stall:
                self.woken.wait(5)
            return b""
        time.sleep(0.005)
        frame = b"\xff\xd8" + b"n%08d" % self.reads + b"\xff\xd9" if self.numbered else JPEG
        # The camera preview and the microscope both send Content-Length.
        return (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(frame)).encode()
            + b"\r\n\r\n"
            + frame
            + b"\r\n"
        )

    def close(self):
        self.closed = True
        self.woken.set()


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
        self.stall = False
        self.numbered = False
        self.opened = 0
        self.thermal_reads = 0
        self.thermal_timestamp = None
        self.detect_reads = 0

    def configured(self, view):
        return view != "camera3"

    def modes(self):
        return {"camera1": (640, 480, 30), "thermal": (160, 120, None)}

    def preview_fps(self):
        return {"camera1": 4.9}

    def open_stream(self, view):
        if view in self.fail:
            raise cameras_module.SourceError("The source did not answer.")
        self.opened += 1
        upstream = Upstream(self.frames, stall=self.stall)
        upstream.numbered = self.numbered
        self.streams.append((view, upstream))
        return upstream

    def still(self, view):
        if view in self.fail:
            raise cameras_module.SourceError("The source did not answer.")
        return PNG

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


def open_stream(rig, fps=None):
    query = "" if fps is None else f"?fps={fps}"
    return rig.client.get(BASE + "stream/microscope" + query, buffered=False)


PART = re.compile(rb"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: (\d+)\r\n\r\n")


def parts(response, count):
    """Read count multipart parts. Return each JPEG and the time it arrived."""
    chunks, buffer, result = response.response, b"", []
    while len(result) < count:
        buffer += next(chunks)
        while (match := PART.search(buffer)) and len(buffer) >= match.end() + int(match[1]) + 2:
            end = match.end() + int(match[1])
            result.append((buffer[match.end() : end], time.monotonic()))
            buffer = buffer[end + 2 :]
    return result


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
    assert "url" not in rows["microscope"]


def test_relay_reports_the_frames_each_microscope_viewer_gets():
    relay = cameras_module.Relay(clock=lambda: 10.0)
    slot = relay.acquire("microscope")
    assert relay.view_state("microscope")["fps"] is None
    # 11 frames over 2 s: 5 a second.
    for step in range(11):
        relay.sent_frame("microscope", at=8.0 + step * 0.2)
    assert relay.view_state("microscope")["fps"] == 5.0
    assert relay.view_state("thermal")["fps"] is None
    slot.release()
    assert relay.view_state("microscope")["fps"] is None


def test_preview_fps_keeps_only_finite_camera_rates(monkeypatch):
    payloads = iter([b'{"camera1": 9.8, "camera2": null, "camera9": 3, "thermal": 1}', b"[]", b"{"])
    monkeypatch.setattr(cameras_module, "_open", lambda url, timeout: io.BytesIO(next(payloads)))
    source = cameras_module.HttpSources(SimpleNamespace())
    assert source.preview_fps() == {"camera1": 9.8}
    assert source.preview_fps() == {}
    assert source.preview_fps() == {}


def test_microscope_snapshot_is_a_full_size_png_attachment(rig):
    response = rig.client.get(BASE + "snapshot/microscope")
    assert response.status_code == 200 and response.data == PNG
    assert response.headers["Content-Type"] == "image/png"
    disposition = response.headers["Content-Disposition"]
    assert "attachment" in disposition and disposition.endswith('.png"')


def test_stream_passes_frames_and_ends_the_relay_when_the_viewer_leaves(rig):
    response = open_stream(rig)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    chunks = response.response
    data = next(chunks) + next(chunks)
    assert data.count(b"--frame") == 2 and JPEG in data
    view, upstream = rig.sources.streams[-1]
    assert view == "microscope" and not upstream.closed
    row = status(rig)["microscope"]
    assert row["state"] == "live" and row["streams"] == 1 and row["age_s"] == 0.0
    response.close()
    assert upstream.closed
    assert status(rig)["microscope"]["streams"] == 0


def test_microscope_stream_bound(rig):
    open_ = [open_stream(rig) for _ in range(cameras_module.MAX_MICROSCOPE_STREAMS)]
    assert all(item.status_code == 200 for item in open_)
    refused = open_stream(rig)
    assert refused.status_code == 503 and "limit" in refused.get_json()["error"]
    open_[0].close()
    again = open_stream(rig)
    assert again.status_code == 200
    for item in [*open_, again]:
        item.close()
    assert sum(rig.service.camera_relay.streams().values()) == 0
    assert all(upstream.closed for _, upstream in rig.sources.streams)


def test_stale_and_offline_states(rig):
    rows = status(rig)
    assert rows["microscope"]["state"] == "offline" and rows["microscope"]["age_s"] is None
    rig.sources.frames, rig.sources.stall = 1, True
    response = open_stream(rig)
    next(response.response)
    assert status(rig)["microscope"]["state"] == "live"
    rig.clock.value += cameras_module.STALE_S + 0.5
    row = status(rig)["microscope"]
    assert row["state"] == "stale" and row["age_s"] == 2.5
    response.close()
    rig.clock.value += cameras_module.RECENT_S
    assert status(rig)["microscope"]["state"] == "offline"
    rig.sources.fail.add("microscope")
    assert open_stream(rig).status_code == 502
    row = status(rig)["microscope"]
    assert row["state"] == "offline" and row["error"] == "The source did not answer."
    assert sum(rig.service.camera_relay.streams().values()) == 0


def test_cameras_are_configured_by_the_camera_devices(rig):
    rig.service.camera_sources = None
    assert cameras_module.HttpSources(rig.service).configured("camera1") is False
    devices = "/dev/v4l/by-path/cam-a,/dev/v4l/by-path/cam-b"
    rig.service.settings = {**rig.service.settings, "MG400_CAMERA_DEVICES": devices}
    sources = cameras_module.HttpSources(rig.service)
    assert sources.configured("camera2") is True and sources.configured("camera3") is False


def test_a_slow_viewer_gets_the_newest_frame_and_no_backlog(rig):
    rig.sources.numbered = True
    response = open_stream(rig, fps=30)
    ((first, _),) = parts(response, 1)
    _, upstream = rig.sources.streams[-1]
    # The viewer stalls while the upstream sends about 60 more frames.
    time.sleep(0.3)
    ((second, _),) = parts(response, 1)
    newest = upstream.reads
    response.close()
    number = int(second[3:-2])
    assert number - int(first[3:-2]) > 10
    # The next frame is at most a few frames behind the upstream: no backlog.
    assert newest - number <= 10


@pytest.mark.parametrize(("fps", "least"), [(None, 0.1), (5, 0.2)])
def test_the_frame_rate_cap_holds(rig, fps, least):
    response = open_stream(rig, fps)
    times = [moment for _, moment in parts(response, 5)]
    response.close()
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    # The upstream sends about 200 fps. The default cap is 10 fps.
    assert min(gaps) >= least * 0.9


@pytest.mark.parametrize("fps", ["0", "31", "2.5", "x", ""])
def test_a_bad_frame_rate_is_refused(rig, fps):
    assert open_stream(rig, fps).status_code == 400
    assert sum(rig.service.camera_relay.streams().values()) == 0


def test_two_viewers_share_one_upstream_reader(rig):
    first = open_stream(rig)
    second = open_stream(rig, fps=30)
    assert parts(first, 2) and parts(second, 2)
    assert rig.sources.opened == 1
    _, upstream = rig.sources.streams[-1]
    first.close()
    assert not upstream.closed and parts(second, 1)
    second.close()
    # The reader closes the upstream when its last viewer leaves.
    assert upstream.closed
    again = open_stream(rig)
    assert parts(again, 1) and rig.sources.opened == 2
    again.close()
    assert sum(rig.service.camera_relay.streams().values()) == 0


def test_an_ended_upstream_releases_its_slot(rig):
    rig.sources.frames = 1
    response = open_stream(rig)
    assert b"".join(response.response).count(b"--frame") == 1
    response.close()
    assert rig.service.camera_relay.streams()["microscope"] == 0


def test_non_mjpeg_upstream_is_refused(rig, monkeypatch):
    monkeypatch.setattr(rig.sources, "open_stream", lambda view: Upstream(content_type="text/html"))
    assert open_stream(rig).status_code == 502
    assert rig.service.camera_relay.streams()["microscope"] == 0


def test_thermal_frame_is_cached_trimmed_and_reports_its_age(rig):
    from datetime import datetime, timezone

    rig.sources.thermal_timestamp = datetime.now(timezone.utc).isoformat()
    path = BASE + "thermal/frame"
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
    stream = open_stream(rig)
    texts.append(next(stream.response).decode("latin-1"))
    texts.append(str(dict(stream.headers)))
    stream.close()
    for text in texts:
        assert PRIVATE not in text and "private.invalid" not in text


def test_http_sources_open_the_microscope_service_without_a_token(monkeypatch):
    calls = []

    def fake_open(url, timeout=None):
        calls.append(url)
        return Upstream()

    monkeypatch.setattr(cameras_module, "_open", fake_open)

    class Http:
        base_url = "http://127.0.0.1:8770/"

    service = SimpleNamespace(
        settings={},
        dashboard_client=lambda name, identity: SimpleNamespace(_http=Http()),
        dashboard_configured=lambda name: True,
    )
    sources = cameras_module.HttpSources(service)
    sources.open_stream("microscope")
    assert calls == ["http://127.0.0.1:8770/api/camera/microscope/mjpeg"]
    assert sources.configured("camera1") is False
