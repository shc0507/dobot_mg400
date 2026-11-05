import importlib.util
import io
import itertools
import socket
import urllib.error
from contextlib import nullcontext
from http.client import HTTPConnection, IncompleteRead
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load_preview():
    spec = importlib.util.spec_from_file_location(
        "camera_preview_test", Path(__file__).parents[1] / "scripts/camera_preview.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setenv(
        "MG400_CAMERA_DEVICES",
        ",".join(f"/dev/v4l/by-path/fake-camera-{camera}" for camera in (1, 2, 3)),
    )
    for setting in (
        "MG400_CAMERA_MODES",
        "MG400_CAMERA_CAPTURE_SIZES",
        "MG400_MICROSCOPE_SIDE_STREAM",
    ):
        monkeypatch.delenv(setting, raising=False)
    module = load_preview()
    upstream = Mock(
        side_effect=lambda *_args, **_kwargs: io.BytesIO(b"\xff\xd8camera-frame\xff\xd9")
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", upstream)
    return module, upstream


def request(module, route, connection=None):
    handler = module.Preview.__new__(module.Preview)
    handler.path = f"/{route}"
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {handler.path} HTTP/1.1"
    handler.wfile = io.BytesIO()
    handler.connection = connection or Mock()
    handler.close_connection = False
    handler.do_GET()
    head, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status, *lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines)
    return int(status.split()[1]), headers, body


@pytest.mark.parametrize("camera", [1, 2, 3])
def test_snapshot_uses_fixed_upstream_and_complete_uncached_jpeg(preview, camera):
    module, upstream = preview
    status, headers, body = request(module, f"camera{camera}.jpg")
    assert status == 200
    assert body == b"\xff\xd8camera-frame\xff\xd9"
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Content-Length"] == str(len(body))
    assert headers["Cache-Control"] == "no-store"
    upstream.assert_called_once_with(
        f"http://127.0.0.1:1984/api/frame.jpeg?src=camera{camera}", timeout=2
    )


def test_snapshots_reuse_http_connection_without_truncating_frames(preview):
    module, upstream = preview
    frames = [b"\xff\xd8first-frame\xff\xd9", b"\xff\xd8second\r\nlonger-frame\xff\xd9"]
    responses = iter(frames)
    upstream.side_effect = lambda *_args, **_kwargs: io.BytesIO(next(responses))
    with module.ThreadingHTTPServer(("127.0.0.1", 0), module.Preview) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.connect()
            socket = connection.sock
            for camera, frame in enumerate(frames, start=1):
                connection.request("GET", f"/camera{camera}.jpg")
                response = connection.getresponse()
                assert response.status == 200
                assert response.version == 11
                assert not response.will_close
                assert response.getheader("Content-Length") == str(len(frame))
                assert response.read() == frame
                assert connection.sock is socket
            assert upstream.call_count == 2
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "route",
    [
        "camera4.jpg",
        "camera4.mjpeg",
        "camera1.jpg?src=admin",
        "camera1.mjpeg?src=admin",
        "camera1.mjpeg?fps=0",
        "camera1.mjpeg?fps=31",
        "camera1.mjpeg?fps=05",
        "camera1.mjpeg?fps=5.5",
        "camera1.mjpeg?fps=5&src=admin",
        "camera1.jpg?fps=5",
        "camera1.mjpeg/extra",
        "camera0/detections.json",
        "camera4/detections.json",
        "camera01/detections.json",
        "camera1/detections.json?camera=2",
        "camera1/detections.json/extra",
        "api/config",
        "../api/config",
        "%2e%2e%2fapi%2fconfig",
        "http://example.com/",
        "preview.js?x=1",
    ],
)
def test_unknown_routes_cannot_proxy_go2rtc(preview, route):
    module, upstream = preview
    assert request(module, route)[0] == 404
    upstream.assert_not_called()


def test_upstream_failure_returns_uncached_error(preview):
    module, upstream = preview
    upstream.side_effect = urllib.error.URLError("Camera unavailable")
    status, headers, _ = request(module, "camera1.jpg")
    assert status == 502
    assert headers["Cache-Control"] == "no-store"


def vision_result(camera=1):
    return {
        "camera": camera,
        "image_size": {"width": 640, "height": 480},
        "coordinate_system": "oriented_original_image_pixels",
        "detections": [],
        "calibrated": False,
    }


class VisionResponse(io.BytesIO):
    status = 200

    def __init__(self, body, camera=1):
        super().__init__(body)
        self.url = f"http://127.0.0.1:8771/api/detect?camera={camera}"

    def geturl(self):
        return self.url


def fake_vision(monkeypatch, module, response):
    opener = Mock(return_value=response)
    builder = Mock(return_value=SimpleNamespace(open=opener))
    monkeypatch.setattr(module.urllib.request, "build_opener", builder)
    return opener, builder


class ChunkedUpstream:
    def __init__(self, body, chunks=(1, 19, 2, 7, 3, 41)):
        self.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        self.body = io.BytesIO(body)
        self.chunks = itertools.cycle(chunks)

    def read1(self, size):
        return self.body.read(min(size, next(self.chunks)))


def multipart(frames, lengths=True):
    parts = []
    for frame in frames:
        headers = b"--frame\r\nContent-Type: image/jpeg\r\n"
        if lengths:
            headers += f"Content-Length: {len(frame)}\r\n".encode()
        parts.append(headers + b"\r\n" + frame + b"\r\n")
    return b"".join(parts) + b"--frame--\r\n"


@pytest.mark.parametrize("lengths", [False, True])
@pytest.mark.parametrize("chunks", [(1,), (2, 19, 1, 5, 64), (65536,)])
def test_mjpeg_parser_handles_headers_and_boundaries_across_chunks(preview, lengths, chunks):
    module, _ = preview
    frames = [
        b"\xff\xd8first\xff\xd9",
        b"\xff\xd8binary\x00\r\n--frameX\r\nsecond\xff\xd9",
    ]
    upstream = ChunkedUpstream(multipart(frames, lengths), chunks)
    assert list(module.iter_mjpeg(upstream)) == frames


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"--frame\r\nContent-Type: image/jpeg\r\n",
        b"--frame\r\nContent-Length: 0\r\n\r\n\r\n--frame--\r\n",
        b"--frame\r\n\r\n\r\n--frame--\r\n",
        b"--frame\r\nContent-Length: 9\r\n\r\n\xff\xd8cut",
        b"--frame\r\n\r\n\xff\xd8cut",
        b"--frame\r\nContent-Length: 6\r\n\r\n\xff\xd8ok\xff\xd9",
        b"--frame\r\n\r\n\xff\xd8ok\xff\xd9\r\n--fra",
        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8ok\xff\xd9\r\n--frame",
        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8ok\xff\xd9\r\n--frame\r",
    ],
)
def test_mjpeg_parser_never_emits_empty_or_truncated_parts(preview, body):
    module, _ = preview
    frames = []
    try:
        frames.extend(module.iter_mjpeg(ChunkedUpstream(body)))
    except ValueError:
        pass
    assert frames == []


@pytest.mark.parametrize("length", [5, 7])
def test_mjpeg_parser_rejects_content_length_mismatch(preview, length):
    module, _ = preview
    body = (
        f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {length}\r\n\r\n".encode()
        + b"\xff\xd8ok\xff\xd9\r\n--frame--\r\n"
    )
    with pytest.raises(ValueError):
        next(module.iter_mjpeg(ChunkedUpstream(body)))


@pytest.mark.parametrize("frame", [b"not-jpeg", b"\xff\xd8no-end", b"no-start\xff\xd9"])
@pytest.mark.parametrize("lengths", [False, True])
def test_mjpeg_parser_rejects_missing_jpeg_markers(preview, frame, lengths):
    module, _ = preview
    with pytest.raises(ValueError):
        next(module.iter_mjpeg(ChunkedUpstream(multipart([frame], lengths))))


@pytest.mark.parametrize("lengths", [False, True])
def test_mjpeg_parser_rejects_frames_over_four_mib(preview, lengths):
    module, _ = preview
    frame = b"\xff\xd8" + b"x" * (4 * 1024 * 1024 - 3) + b"\xff\xd9"
    upstream = ChunkedUpstream(multipart([frame], lengths), (65536,))
    with pytest.raises(ValueError):
        next(module.iter_mjpeg(upstream))


@pytest.mark.parametrize("lengths", [False, True])
def test_mjpeg_parser_accepts_frame_at_four_mib_limit(preview, lengths):
    module, _ = preview
    frame = b"\xff\xd8" + b"x" * (4 * 1024 * 1024 - 4) + b"\xff\xd9"
    body = multipart([frame], lengths)
    cut = body.rfind(b"--frame--") + len(b"--frame-")
    chunks = (65536,) * 64 + (cut - len(frame), 1, 65536)
    upstream = ChunkedUpstream(body, chunks)
    assert list(module.iter_mjpeg(upstream)) == [frame]


def test_feed_keeps_only_latest_frame_for_slow_consumer(preview):
    module, _ = preview
    feed = module.CameraFeed("camera1")
    with pytest.raises(OSError):
        feed.wait_after(0, timeout=0.01)
    feed.publish(b"first")
    sequence, frame = feed.wait_after(0)
    assert (sequence, frame) == (1, b"first")
    feed.publish(b"second")
    feed.publish(b"third")
    assert feed.wait_after(sequence) == (3, b"third")
    with pytest.raises(OSError):
        feed.wait_after(3, timeout=0.01)


def test_feed_rejects_stale_frame_until_new_frame_arrives(preview, monkeypatch):
    module, _ = preview
    monotonic = Mock(return_value=1000.0)
    monkeypatch.setattr(module.time, "monotonic", monotonic)
    feed = module.CameraFeed("camera1")
    feed.publish(b"old")
    monotonic.return_value = 1001.5
    assert feed.wait_after(0, timeout=0) == (1, b"old")
    monotonic.return_value = 1001.5001
    with pytest.raises(OSError):
        feed.wait_after(0, timeout=0)
    feed.publish(b"new")
    assert feed.wait_after(0, timeout=0) == (2, b"new")


def test_feed_disconnect_clears_frame_and_wakes_waiter(preview):
    module, _ = preview
    feed = module.CameraFeed("camera1")
    feed.publish(b"old")
    waiting = Event()
    result = []

    def wait_for_frame():
        waiting.set()
        try:
            result.append(feed.wait_after(1, timeout=2))
        except OSError as error:
            result.append(error)

    waiter = Thread(target=wait_for_frame, daemon=True)
    waiter.start()
    assert waiting.wait(timeout=1)
    feed.disconnect()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], OSError)
    with pytest.raises(OSError):
        feed.wait_after(0, timeout=0.01)
    feed.publish(b"new")
    assert feed.wait_after(1) == (2, b"new")


def test_feed_start_creates_one_reader_for_fixed_local_upstream(preview, monkeypatch):
    module, upstream = preview
    thread = Mock()
    thread_factory = Mock(return_value=thread)
    monkeypatch.setattr(module, "Thread", thread_factory)
    feed = module.CameraFeed("camera1")
    feed.start()
    feed.start()
    thread_factory.assert_called_once()
    thread.start.assert_called_once()

    class EndReader(BaseException):
        pass

    upstream.side_effect = EndReader
    target = thread_factory.call_args.kwargs["target"]
    with pytest.raises(EndReader):
        target()
    assert upstream.call_args.args == ("http://127.0.0.1:1984/api/stream.mjpeg?src=camera1",)
    assert upstream.call_args.kwargs["timeout"] > 0


def test_feed_clears_old_frame_before_retrying_truncated_http_stream(preview, monkeypatch):
    module, upstream = preview
    response = Mock()
    response.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
    response.read1.side_effect = IncompleteRead(b"truncated HTTP chunk")
    upstream.side_effect = lambda *_args, **_kwargs: nullcontext(response)
    feed = module.CameraFeed("camera1")
    feed.publish(b"old")

    class EndRetry(BaseException):
        pass

    retry = Mock(side_effect=EndRetry)
    monkeypatch.setattr(module.time, "sleep", retry)
    with pytest.raises(EndRetry):
        feed.run()
    retry.assert_called_once()
    with pytest.raises(OSError):
        feed.wait_after(0, timeout=0)


@pytest.mark.parametrize("camera", [1, 2, 3])
def test_mjpeg_route_streams_complete_parts_and_closes_connection(preview, monkeypatch, camera):
    module, upstream = preview
    frames = [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"]
    feed = Mock()
    feed.wait_after.side_effect = [(1, frames[0]), (4, frames[1]), OSError("Disconnected")]
    monkeypatch.setitem(module.FEEDS, f"camera{camera}", feed)
    connection = Mock()
    status, headers, body = request(module, f"camera{camera}.mjpeg", connection=connection)
    assert status == 200
    assert headers["Content-Type"] == "multipart/x-mixed-replace; boundary=frame"
    assert headers["Connection"].lower() == "close"
    assert "Content-Length" not in headers
    assert headers["Cache-Control"] == "no-store"
    assert body == multipart(frames).removesuffix(b"--frame--\r\n")
    connection.settimeout.assert_called_with(1.5)
    connection.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    feed.start.assert_called_once_with()
    feed.release.assert_called_once_with()
    assert [call.args[0] for call in feed.wait_after.call_args_list] == [0, 1, 4]
    upstream.assert_not_called()


@pytest.mark.parametrize(
    ("query", "interval"), [("", 1 / 30), ("?fps=30", 1 / 30), ("?fps=10", 0.1), ("?fps=1", 1.0)]
)
def test_mjpeg_route_caps_the_frame_rate_with_fps(preview, monkeypatch, query, interval):
    module, _ = preview
    frames = [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"]
    feed = Mock()
    feed.wait_after.side_effect = [(1, frames[0]), (2, frames[1]), OSError("Disconnected")]
    monkeypatch.setitem(module.FEEDS, "camera1", feed)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    status, _, body = request(module, f"camera1.mjpeg{query}")
    assert status == 200
    assert body == multipart(frames).removesuffix(b"--frame--\r\n")
    assert sleeps == [interval, interval]


def test_mjpeg_route_returns_error_when_no_first_frame(preview, monkeypatch):
    module, upstream = preview
    feed = Mock()
    feed.wait_after.side_effect = OSError("No camera frames")
    monkeypatch.setitem(module.FEEDS, "camera1", feed)
    status, headers, _ = request(module, "camera1.mjpeg")
    assert status == 502
    assert headers["Cache-Control"] == "no-store"
    feed.release.assert_called_once_with()
    upstream.assert_not_called()


def test_mjpeg_stream_stops_reading_when_client_write_times_out(preview):
    module, _ = preview
    handler = module.Preview.__new__(module.Preview)
    handler.connection = Mock()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = TimeoutError("Slow viewer")
    feed = Mock()
    feed.wait_after.return_value = (1, b"\xff\xd8frame\xff\xd9")
    handler.stream(feed)
    assert handler.close_connection is True
    handler.connection.settimeout.assert_called_with(1.5)
    feed.wait_after.assert_called_once_with(0)
    feed.release.assert_called_once_with()


def test_mjpeg_parser_rejects_oversized_headers_even_with_complete_body_in_one_chunk(preview):
    module, _ = preview
    body = multipart([b"\xff\xd8ok\xff\xd9"]).replace(
        b"Content-Type:", b"X-Long: " + b"x" * 8192 + b"\r\nContent-Type:", 1
    )
    with pytest.raises(ValueError, match="headers are too large"):
        next(module.iter_mjpeg(ChunkedUpstream(body, (65536,))))


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_preview_routes_and_tiles_follow_only_configured_cameras(preview, monkeypatch, count):
    _, upstream = preview
    monkeypatch.setenv(
        "MG400_CAMERA_DEVICES",
        ",".join(f"/dev/v4l/by-path/fake-camera-{camera}" for camera in range(1, count + 1)),
    )
    module = load_preview()
    assert request(module, "")[0] == 404
    assert tuple(module.FEEDS) == tuple(f"camera{camera}" for camera in range(1, count + 1))
    assert all(feed.thread is None for feed in module.FEEDS.values())
    assert not any(feed.on_demand for feed in module.FEEDS.values())
    for camera in range(count + 1, 6):
        for route in (
            f"camera{camera}.jpg",
            f"camera{camera}.mjpeg",
        ):
            assert request(module, route)[0] == 404
    upstream.assert_not_called()


def test_fourth_camera_still_route_keeps_fixed_destination(preview, monkeypatch):
    _, upstream = preview
    monkeypatch.setenv(
        "MG400_CAMERA_DEVICES",
        ",".join(f"/dev/v4l/by-path/fake-camera-{camera}" for camera in range(1, 5)),
    )
    monkeypatch.setenv("MG400_CAMERA_MODES", "640x480@30,3840x2160@30,640x480@30,1280x720@30")
    module = load_preview()
    assert [feed.on_demand for feed in module.FEEDS.values()] == [False, True, False, True]
    for route in ("", "preview.js", "camera4/detections.json"):
        assert request(module, route)[0] == 404
    upstream.assert_not_called()
    assert request(module, "camera4.jpg")[2] == b"\xff\xd8camera-frame\xff\xd9"
    upstream.assert_called_once_with("http://127.0.0.1:1984/api/frame.jpeg?src=camera4", timeout=2)
    assert all(feed.thread is None for feed in module.FEEDS.values())


def test_preview_configuration_uses_shared_legacy_size_and_side_stream_rules(preview, monkeypatch):
    monkeypatch.setenv("MG400_MICROSCOPE_SIDE_STREAM", "camera2")
    module = load_preview()
    assert module.CAMERAS.mode(2) == (3840, 2160, 30)
    assert list(module.FEEDS) == ["camera1", "camera3"]
    monkeypatch.setenv("MG400_CAMERA_CAPTURE_SIZES", "640x360,1280x720,320x240")
    module = load_preview()
    assert module.CAMERAS.mode(2) == (1280, 720, 30)
    assert list(module.FEEDS) == ["camera1", "camera3"]
    monkeypatch.setenv("MG400_CAMERA_CAPTURE_SIZES", "640x360,640x480,1280x720")
    module = load_preview()
    assert [feed.on_demand for feed in module.FEEDS.values()] == [False, False, True]
    monkeypatch.setenv("MG400_CAMERA_MODES", "1920x1080@60,640x480@30,640x480@30")
    with pytest.raises(ValueError):
        load_preview()
    monkeypatch.delenv("MG400_CAMERA_CAPTURE_SIZES")
    module = load_preview()
    assert module.CAMERAS.mode(1) == (1920, 1080, 60)


def test_side_stream_above_640x480_is_not_relayed(preview, monkeypatch):
    _, upstream = preview
    monkeypatch.setenv("MG400_MICROSCOPE_SIDE_STREAM", "camera3")
    monkeypatch.setenv("MG400_CAMERA_MODES", "640x480@30,640x480@30,3840x2160@30")
    module = load_preview()
    for route in ("camera3.jpg", "camera3.mjpeg"):
        assert request(module, route)[0] == 404
    upstream.assert_not_called()
    assert request(module, "camera2.jpg")[0] == 200


def test_on_demand_feed_releases_last_viewer_and_can_start_again(preview, monkeypatch):
    module, upstream = preview
    threads = Mock(side_effect=lambda **kwargs: Mock(target=kwargs["target"]))
    monkeypatch.setattr(module, "Thread", threads)
    feed = module.CameraFeed("camera4", on_demand=True)
    feed.run()
    upstream.assert_not_called()
    feed.start()
    feed.start()
    threads.assert_called_once()
    assert feed.viewers == 2
    feed.publish(b"frame")
    feed.release()
    assert feed.viewers == 1 and feed.active()
    assert feed.wait_after(0, timeout=0) == (1, b"frame")
    feed.release()
    assert feed.viewers == 0 and not feed.active()
    assert feed.frame is None
    feed.publish(b"late frame")
    assert feed.frame is None
    feed.run()
    assert feed.thread is None
    feed.start()
    assert threads.call_count == 2 and feed.viewers == 1
    assert not feed.error
    upstream.assert_not_called()


def test_high_mode_reader_closes_mid_frame_when_last_viewer_leaves(preview, monkeypatch):
    module, upstream = preview
    monkeypatch.setattr(module, "Thread", Mock())
    feed = module.CameraFeed("camera4", on_demand=True)
    feed.start()
    response = Mock()
    response.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}

    def first_chunk(size):
        feed.release()
        return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8partial"

    response.read1.side_effect = first_chunk
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    upstream.return_value = response
    upstream.side_effect = None
    feed.run()
    response.read1.assert_called_once()
    response.__exit__.assert_called_once()
    assert feed.thread is None and feed.frame is None and feed.viewers == 0
    upstream.assert_called_once_with(
        "http://127.0.0.1:1984/api/stream.mjpeg?src=camera4", timeout=2
    )


def test_high_mode_reader_hands_off_to_new_viewer_without_duplicate_reader(preview, monkeypatch):
    module, upstream = preview
    factory = Mock()
    monkeypatch.setattr(module, "Thread", factory)
    feed = module.CameraFeed("camera4", on_demand=True)
    feed.start()
    active_thread = feed.thread
    feed.release()
    feed.start()
    assert feed.thread is active_thread and feed.viewers == 1
    factory.assert_called_once()
    feed.release()
    feed.run()
    assert feed.thread is None
    upstream.assert_not_called()
